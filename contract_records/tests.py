"""
Tests for sync_contract_records. Synthetic data only: employee codes are
built at run time from a prefix and numbers no real employee uses, names are
invented, and files live in a temporary folder outside the repository.
"""

import csv
import os
import sys
import tempfile
import types
from datetime import date
from io import StringIO
from pathlib import Path
from unittest import mock

from apscheduler.schedulers.background import BackgroundScheduler
from auditlog.models import LogEntry
from django.apps import apps
from django.conf import settings
from django.core.management import CommandError, call_command
from django.db import connections
from django.test import TestCase

from contract_records.management.commands.sync_contract_records import (
    stop_in_process_schedulers,
)
from contract_records.models import ContractRecord
from contract_records.sync import (
    ADVISORY_LOCK_KEY,
    HEADER,
    SALT_ENV,
    SyncRefused,
    _rows,
    name_tokens,
    normalise_code,
    run_joining_report,
    run_sync,
    run_undo,
)
from employee.models import Employee, EmployeeWorkInformation
from horilla.horilla_middlewares import _thread_locals
from leave.tests import add_missing_user_column, make_employee

SALT = "unit-test-salt-not-a-real-one"
P = "G" + "SL"  # the real prefix, kept out of the source as one literal
EXPORTED = "2026-10-13T09:00:00+03:00"
TODAY = date(2026, 10, 13)


def code(number, sep=""):
    return f"{P}{sep}{number}"


def tokens(name):
    return " ".join(sorted(name_tokens(name, SALT)))


def make(label, badge, first, last="", active=True):
    _user, employee = make_employee(label)
    Employee.objects.filter(pk=employee.pk).update(
        badge_id=badge,
        employee_first_name=first,
        employee_last_name=last,
        is_active=active,
    )
    employee.refresh_from_db()
    return employee


def set_work(employee, **values):
    # Creating an Employee already creates its work information row.
    work = EmployeeWorkInformation.objects.entire().get(employee_id=employee)
    for name, value in values.items():
        setattr(work, name, value)
    work.save()


class Base(TestCase):
    @classmethod
    def setUpClass(cls):
        add_missing_user_column()
        super().setUpClass()

    def setUp(self):
        _thread_locals.request = None
        self.addCleanup(setattr, _thread_locals, "request", None)
        patcher = mock.patch.dict(os.environ, {SALT_ENV: SALT})
        patcher.start()
        self.addCleanup(patcher.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        self.files = 0
        self.alpha = make("alpha", "TEST001", "Alpha", "Tester")
        self.bravo = make("bravo", "TEST002", "Bravo", "Sample")

    def row(self, **values):
        base = {
            "ContractID": "CON-T1",
            "EmployeeCode": "TEST001",
            "NameTokens": tokens("Alpha Tester"),
            "ContractType": "Fixed-term 1 Year",
            "StartDate": "2026-01-05",
            "EndDate": "2027-01-04",
            "Status": "Active",
            "Department": "Packing",
            "Role": "Packer",
            "DocURL": "https://docs.google.com/document/d/abc123",
            "SignedURL": "",
            "SignedAt": "",
            "ProbationConfirmed": "FALSE",
            "GeneratedAt": "2026-01-04T10:00:00+03:00",
            "ExportedAt": EXPORTED,
        }
        base.update(values)
        return base

    def write(self, rows, header=HEADER, encoding="utf-8", newline="\n", delimiter=","):
        self.files += 1
        path = self.dir / f"export-{self.files}.csv"
        with open(path, "w", newline="", encoding=encoding) as handle:
            writer = csv.writer(handle, delimiter=delimiter, lineterminator=newline)
            writer.writerow(header)
            for row in rows:
                writer.writerow([row.get(column, "") for column in header])
        return path

    def sync(self, rows_or_path, **options):
        path = (
            rows_or_path if isinstance(rows_or_path, Path) else self.write(rows_or_path)
        )
        self.files += 1
        options.setdefault("review_out", self.dir / f"review-{self.files}.csv")
        options.setdefault("today", TODAY)
        return run_sync(path, **options)

    def review(self, result):
        with open(result["review_file"], newline="") as handle:
            return list(csv.DictReader(handle))

    def record(self, ref="CON-T1"):
        return ContractRecord.objects.get(external_ref=ref)


class MatchingTests(Base):
    """Edge cases A1-A9."""

    def test_a1_blank_code_goes_to_review(self):
        result = self.sync([self.row(EmployeeCode="  ")], apply=True)
        self.assertEqual(result["buckets"]["review"], 1)
        self.assertEqual(result["reasons"]["review:BLANK_CODE"], 1)
        self.assertFalse(ContractRecord.objects.exists())

    def test_a2_case_spaces_hyphen_and_zeros_match(self):
        self.assertEqual(normalise_code(f" {P.lower()}043 "), code(43))
        self.assertEqual(normalise_code(code("043", " ")), code(43))
        self.assertEqual(normalise_code(code("043", "-")), code(43))
        self.assertEqual(normalise_code(code("000")), code(0))
        employee = make("charlie", code("0881"), "Charlie", "Example")
        variants = [
            code("0881").lower(),
            code("0881", " "),
            code("0881", "-"),
            code(881),
        ]
        rows = [
            self.row(
                ContractID=f"CON-V{i}",
                EmployeeCode=v,
                NameTokens=tokens("Charlie Example"),
            )
            for i, v in enumerate(variants)
        ]
        result = self.sync(rows, apply=True)
        self.assertEqual(result["buckets"]["created"], 4)
        self.assertEqual(ContractRecord.objects.filter(employee=employee).count(), 4)
        self.assertEqual(
            set(ContractRecord.objects.values_list("employee_code", flat=True)),
            {code(881)},
        )

    def test_a3_irregular_codes_exact_first_else_review(self):
        odd = make("delta", code("9001/99"), "Delta", "Example")
        long_digits = make("echo", code("000000088123456"), "Echo", "Example")
        rows = [
            self.row(
                ContractID="CON-1",
                EmployeeCode=code("9001/99"),
                NameTokens=tokens("Delta Example"),
            ),
            self.row(
                ContractID="CON-2",
                EmployeeCode=code("9001-99"),
                NameTokens=tokens("Delta Example"),
            ),
            self.row(
                ContractID="CON-3",
                EmployeeCode=code("88123456"),
                NameTokens=tokens("Echo Example"),
            ),
        ]
        result = self.sync(rows, apply=True)
        self.assertEqual(self.record("CON-1").employee, odd)
        self.assertEqual(self.record("CON-3").employee, long_digits)
        self.assertEqual(result["reasons"]["review:CODE_NOT_FOUND"], 1)

    def test_a4_two_employees_normalising_alike_is_ambiguous(self):
        make("foxtrot", code("0882"), "Foxtrot", "Example")
        make("golf", code("882", "-"), "Foxtrot", "Example")
        result = self.sync(
            [
                self.row(
                    EmployeeCode=code("882", " "), NameTokens=tokens("Foxtrot Example")
                )
            ],
            apply=True,
        )
        self.assertEqual(result["reasons"]["review:CODE_AMBIGUOUS"], 1)
        # An exact code is not a guess.
        result = self.sync(
            [
                self.row(
                    ContractID="CON-X",
                    EmployeeCode=code("0882"),
                    NameTokens=tokens("Foxtrot Example"),
                )
            ],
            apply=True,
            force=True,
        )
        self.assertEqual(result["buckets"]["created"], 1)

    def test_a5_unknown_code_goes_to_review(self):
        result = self.sync([self.row(EmployeeCode="TEST999")], apply=True)
        self.assertEqual(result["reasons"]["review:CODE_NOT_FOUND"], 1)
        self.assertEqual(self.review(result)[0]["employee_code"], "TEST999")

    def test_a6_wrong_person_is_held_until_hr_accepts(self):
        result = self.sync([self.row(NameTokens=tokens("Bravo Sample"))], apply=True)
        self.assertEqual(result["reasons"]["review:NAME_MISMATCH"], 1)
        self.assertFalse(ContractRecord.objects.exists())
        # A nickname shares one word of two: still held, not lost.
        result = self.sync([self.row(NameTokens=tokens("Al Tester"))], apply=True)
        self.assertEqual(result["reasons"]["review:NAME_MISMATCH"], 1)
        accept = self.dir / "accept.csv"
        accept.write_text("employee_code\nTEST001\n")
        result = self.sync(
            [self.row(NameTokens=tokens("Al Tester"))], apply=True, accept_path=accept
        )
        self.assertEqual(result["buckets"]["created"], 1)
        self.assertEqual(result["flags"]["ACCEPTED_BY_HR"], 1)

    def test_a6_one_word_names_need_one_shared_word(self):
        mono = make("hotel", "TEST003", "Hotel")
        result = self.sync(
            [
                self.row(
                    ContractID="CON-1",
                    EmployeeCode="TEST003",
                    NameTokens=tokens("Hotel Extra"),
                ),
                self.row(
                    ContractID="CON-2",
                    EmployeeCode="TEST003",
                    NameTokens=tokens("Other Person"),
                ),
                self.row(ContractID="CON-3", EmployeeCode="TEST003", NameTokens=""),
            ],
            apply=True,
        )
        self.assertEqual(self.record("CON-1").employee, mono)
        # Held as CODE_NAMES_DIFFER: the code's two rows name different people.
        self.assertEqual(result["reasons"]["review:CODE_NAMES_DIFFER"], 1)
        self.assertEqual(result["reasons"]["review:NO_NAME_TOKENS"], 1)

    def test_a7_one_code_two_people_cannot_be_accepted(self):
        accept = self.dir / "accept.csv"
        accept.write_text("employee_code\nTEST001\n")
        result = self.sync(
            [
                self.row(ContractID="CON-1"),
                self.row(ContractID="CON-2", NameTokens=tokens("Zulu Other")),
            ],
            apply=True,
            accept_path=accept,
        )
        self.assertEqual(result["buckets"]["created"], 1)
        self.assertEqual(result["reasons"]["review:CODE_NAMES_DIFFER"], 1)
        self.assertEqual(result["codes_with_differing_names"], 1)

    def test_a7_one_person_under_two_codes_is_counted(self):
        result = self.sync(
            [
                self.row(ContractID="CON-1"),
                self.row(ContractID="CON-2", EmployeeCode="TEST002"),
            ],
            apply=True,
        )
        self.assertEqual(result["names_on_several_codes"], 1)
        self.assertEqual(result["reasons"]["review:NAME_MISMATCH"], 1)

    def test_hunt_corrected_code_moves_record_and_is_flagged(self):
        self.sync([self.row()], apply=True)
        result = self.sync(
            [self.row(EmployeeCode="TEST002", NameTokens=tokens("Bravo Sample"))],
            apply=True,
        )
        self.assertEqual(result["buckets"]["updated"], 1)
        self.assertEqual(result["flags"]["EMPLOYEE_CHANGED"], 1)
        self.assertEqual(self.record().employee, self.bravo)

    def test_a8_archived_employee_matches_and_deletion_keeps_record(self):
        Employee.objects.filter(pk=self.alpha.pk).update(is_active=False)
        self.sync([self.row()], apply=True)
        self.assertEqual(self.record().employee_id, self.alpha.pk)
        # The payroll signal's draft contract (PROTECT) would block the delete.
        from payroll.models.models import Contract

        Contract.objects.entire().filter(employee_id=self.alpha).delete()
        Employee.objects.get(pk=self.alpha.pk).delete()
        record = self.record()
        self.assertIsNone(record.employee)
        self.assertEqual(record.employee_code, "TEST001")

    def test_a9_badge_edit_after_sync_keeps_link_and_is_counted(self):
        self.sync([self.row()], apply=True)
        Employee.objects.filter(pk=self.alpha.pk).update(badge_id="TEST101")
        result = self.sync([self.row()])
        self.assertEqual(self.record().employee_id, self.alpha.pk)
        self.assertEqual(result["reasons"]["review:CODE_NOT_FOUND"], 1)
        self.assertEqual(
            result["reconciliation"]["employees_whose_code_changed_since_sync"], 1
        )


class KeyTests(Base):
    """Edge cases B10-B14."""

    def test_b10_blank_contract_id_is_rejected_not_invented(self):
        result = self.sync([self.row(ContractID="")], apply=True)
        self.assertEqual(result["reasons"]["rejected:BLANK_CONTRACT_ID"], 1)
        self.assertFalse(ContractRecord.objects.exists())
        self.assertEqual(self.review(result)[0]["reason"], "BLANK_CONTRACT_ID")

    def test_b11_b13_duplicate_ids_reject_the_later_rows(self):
        rows = [
            self.row(
                ContractID="CON-IMPORT-TEST001", StartDate="2025-01-01", EndDate=""
            ),
            self.row(ContractID="con-import-test001 ", StartDate="2026-01-01"),
            self.row(ContractID="CON-IMPORT-TEST001"),
        ]
        result = self.sync(rows, apply=True)
        self.assertEqual(result["buckets"]["created"], 1)
        self.assertEqual(result["reasons"]["rejected:DUPLICATE_CONTRACT_ID"], 2)
        self.assertEqual(self.record("CON-IMPORT-TEST001").start_date, date(2025, 1, 1))

    def test_b12_duplicate_looking_rows_are_both_stored(self):
        result = self.sync(
            [self.row(ContractID="CON-1"), self.row(ContractID="CON-2")], apply=True
        )
        self.assertEqual(result["buckets"]["created"], 2)
        self.assertEqual(result["quality"]["duplicate_looking_groups"], 1)

    def test_b14_edited_id_makes_a_second_record_and_an_orphan(self):
        self.sync([self.row(ContractID="CON-OLD")], apply=True)
        result = self.sync([self.row(ContractID="CON-NEW")], apply=True)
        self.assertEqual(ContractRecord.objects.count(), 2)
        self.assertEqual(result["reconciliation"]["records_missing_from_file"], 1)


class DateTests(Base):
    """Edge cases C15-C23."""

    def test_c15_text_or_blank_dates_store_null_and_are_listed(self):
        result = self.sync(
            [
                self.row(ContractID="CON-1", StartDate="Commence date not stated"),
                self.row(ContractID="CON-2", StartDate="Create New V", EndDate=""),
            ],
            apply=True,
        )
        self.assertEqual(result["buckets"]["created"], 2)
        self.assertIsNone(self.record("CON-1").start_date)
        self.assertEqual(result["flags"]["DATE_TEXT_STORED_NULL"], 2)
        listed = [r["reason"] for r in self.review(result)]
        self.assertEqual(listed.count("DATE_TEXT_STORED_NULL"), 2)

    def test_c16_c20_only_strict_iso_dates(self):
        bad = [
            "05/01/2026",
            "46027",
            "1st Jan 2026",
            "2026-1-5",
            "2026-02-30",
            "2026-01-05T00:00:00Z",
        ]
        rows = [
            self.row(ContractID=f"CON-{i}", StartDate=value)
            for i, value in enumerate(bad)
        ]
        result = self.sync(rows, apply=True)
        self.assertEqual(result["buckets"]["rejected"], len(bad))
        self.assertFalse(ContractRecord.objects.exists())

    def test_c20_timestamps_need_a_timezone(self):
        result = self.sync([self.row(SignedAt="2026-01-05T10:00:00")], apply=True)
        self.assertEqual(result["reasons"]["rejected:BAD_TIMESTAMP"], 1)
        result = self.sync([self.row(SignedAt="2026-01-05T10:00:00Z")], apply=True)
        self.assertEqual(result["buckets"]["created"], 1)

    def test_c17_c18_c19_c21_odd_dates_are_stored_and_counted(self):
        rows = [
            self.row(ContractID="CON-1", StartDate="2026-05-01", EndDate="2026-04-01"),
            self.row(ContractID="CON-2", StartDate="2026-05-01", EndDate="2026-05-01"),
            self.row(ContractID="CON-3", StartDate="1926-05-01", EndDate=""),
            self.row(
                ContractID="CON-4",
                ContractType="Permanent Employment",
                EndDate="2027-01-01",
            ),
            self.row(
                ContractID="CON-5", ContractType="Fixed-term 3 Months", EndDate=""
            ),
            # A "6 month" label over 12 months, and 31 Aug + 6 months by setMonth.
            self.row(
                ContractID="CON-6",
                ContractType="6 Month Fixed-Term",
                StartDate="2026-01-01",
                EndDate="2026-12-31",
            ),
            self.row(
                ContractID="CON-7",
                ContractType="Fixed-term 6 Months",
                StartDate="2025-08-31",
                EndDate="2026-03-03",
            ),
            self.row(ContractID="CON-8", StartDate="2025-01-01", EndDate="2025-12-31"),
        ]
        result = self.sync(rows, apply=True)
        self.assertEqual(result["buckets"]["created"], 8)
        quality = result["quality"]
        self.assertEqual(quality["end_before_start"], 1)
        self.assertEqual(quality["end_equals_start"], 1)
        self.assertEqual(quality["implausible_date"], 1)
        self.assertEqual(quality["permanent_with_end_date"], 1)
        self.assertEqual(quality["fixed_term_without_end_date"], 2)
        self.assertEqual(quality["term_length_not_matching_label"], 1)
        self.assertEqual(quality["term_end_off_by_1_to_31_days"], 1)
        self.assertEqual(quality["active_but_end_date_past"], 4)
        self.assertEqual(self.record("CON-7").end_date, date(2026, 3, 3))

    def test_c22_overlapping_active_contracts_are_counted(self):
        rows = [
            self.row(ContractID="CON-1", StartDate="2026-01-01", EndDate="2026-12-31"),
            self.row(ContractID="CON-2", StartDate="2026-06-01", EndDate=""),
        ]
        result = self.sync(rows, apply=True)
        self.assertEqual(
            result["reconciliation"]["employees_with_overlapping_active_contracts"], 1
        )

    def test_c23_d26_joining_report_is_read_only(self):
        set_work(self.alpha, date_joining=date(2026, 3, 1))
        set_work(self.bravo, date_joining=date(2025, 1, 1))
        before = list(
            EmployeeWorkInformation.objects.values_list("employee_id", "date_joining")
        )
        path = self.write(
            [
                self.row(ContractID="CON-1", StartDate="2026-03-01"),
                self.row(
                    ContractID="CON-2", StartDate="2025-01-01", ContractType="Addendum"
                ),
                self.row(ContractID="CON-3", StartDate="2024-01-01", Status="Deleted"),
                self.row(
                    ContractID="CON-4",
                    EmployeeCode="TEST002",
                    NameTokens=tokens("Bravo Sample"),
                    StartDate="2025-06-01",
                ),
            ]
        )
        result = run_joining_report(path, review_out=self.dir / "joining.csv")
        self.assertEqual(result["counts"]["equal"], 1)
        self.assertEqual(result["counts"]["hrms_earlier"], 1)
        self.assertEqual(
            list(
                EmployeeWorkInformation.objects.values_list(
                    "employee_id", "date_joining"
                )
            ),
            before,
        )
        self.assertFalse(ContractRecord.objects.exists())


class TextTests(Base):
    """Edge cases D24-D30 and E31-E33."""

    def test_d24_d25_unknown_labels_become_other_with_raw_kept(self):
        result = self.sync(
            [self.row(ContractType="Internship 2 Weeks", Status="On Hold")], apply=True
        )
        record = self.record()
        self.assertEqual(
            (record.contract_type, record.contract_type_raw),
            ("other", "Internship 2 Weeks"),
        )
        self.assertEqual((record.status, record.status_raw), ("other", "On Hold"))
        self.assertEqual(result["quality"]["type_other"], 1)
        self.assertEqual(result["quality"]["status_other"], 1)

    def test_d24_all_eight_known_labels(self):
        labels = {
            "Permanent Employment": "permanent",
            "Fixed-term 1 Year": "fixed_1y",
            "Fixed-term Employment": "fixed_1y",
            "Fixed-term 6 Months": "fixed_6m",
            "6 Month Fixed-Term": "fixed_6m",
            "Fixed-term 3 Months": "fixed_3m",
            "Casual": "casual",
            "Addendum": "addendum",
        }
        rows = [
            self.row(ContractID=f"CON-{i}", ContractType=f" {label.upper()} ")
            for i, label in enumerate(labels)
        ]
        self.sync(rows, apply=True)
        stored = [self.record(f"CON-{i}").contract_type for i in range(len(labels))]
        self.assertEqual(stored, list(labels.values()))

    def test_d27_deleted_rows_mark_never_remove_and_skip_if_new(self):
        self.sync([self.row(ContractID="CON-1")], apply=True)
        result = self.sync(
            [
                self.row(ContractID="CON-1", Status="Deleted", StartDate="05/01/2026"),
                self.row(ContractID="CON-2", Status="Deleted"),
            ],
            apply=True,
            force=True,
        )
        self.assertEqual(self.record("CON-1").status, "deleted")
        self.assertEqual(result["buckets"]["updated"], 1)
        self.assertEqual(result["buckets"]["skipped_deleted_never_synced"], 1)
        self.assertEqual(ContractRecord.objects.count(), 1)

    def test_d28_generated_status(self):
        self.sync([self.row(Status="Generated")], apply=True)
        self.assertEqual(self.record().status, "generated")

    def test_d29_text_is_tidied_and_overlong_text_rejected(self):
        messy = "  Private Label,\n “North”   wing "
        result = self.sync(
            [
                self.row(ContractID="CON-1", Department=messy),
                self.row(ContractID="CON-2", Role="x" * 256),
            ],
            apply=True,
        )
        self.assertEqual(
            self.record("CON-1").department_text, "Private Label, “North” wing"
        )
        self.assertEqual(result["reasons"]["rejected:TOO_LONG"], 1)

    def test_d30_review_file_cells_cannot_run_as_formulas(self):
        result = self.sync([self.row(EmployeeCode="=HYPERLINK(1)")], apply=True)
        self.assertEqual(self.review(result)[0]["employee_code"], "'=HYPERLINK(1)")

    def test_a6_name_hash_ignores_unicode_composition(self):
        composed, decomposed = "Jos\u00e9 Tester", "Jose\u0301 Tester"
        self.assertEqual(name_tokens(composed, SALT), name_tokens(decomposed, SALT))

    def test_d30_formula_like_cells_are_text(self):
        self.sync([self.row(Department="'=SUM(A1:A9)", Role="+Lead")], apply=True)
        record = self.record()
        self.assertEqual(
            (record.department_text, record.role_text), ("=SUM(A1:A9)", "+Lead")
        )

    def test_e31_no_pdf_link_is_stored(self):
        names = {field.name for field in ContractRecord._meta.get_fields()}
        self.assertFalse({"pdf_url", "pdf_id", "notes"} & names)

    def test_e32_only_https_google_links(self):
        bad = [
            "javascript:alert(1)",
            "http://docs.google.com/document/d/abc",
            "https://example.org/doc",
            "https://docs.google.com.evil.example/doc",
        ]
        rows = [
            self.row(ContractID=f"CON-{i}", DocURL=url) for i, url in enumerate(bad)
        ]
        rows.append(
            self.row(
                ContractID="CON-OK", DocURL="https://drive.google.com/file/d/abc/view"
            )
        )
        result = self.sync(rows, apply=True)
        self.assertEqual(result["buckets"]["created"], 5)
        self.assertEqual(result["flags"]["LINK_REJECTED"], 4)
        self.assertEqual(self.record("CON-0").doc_url, "")
        self.assertTrue(self.record("CON-OK").doc_url)

    def test_e33_signed_link_and_date_must_go_together(self):
        result = self.sync(
            [
                self.row(
                    ContractID="CON-1", SignedURL="https://drive.google.com/file/d/s1"
                ),
                self.row(ContractID="CON-2", SignedAt="2026-01-06T09:00:00+03:00"),
            ],
            apply=True,
        )
        self.assertEqual(result["quality"]["signed_link_and_date_mismatch"], 2)


class FileTests(Base):
    """Edge cases F35-F43 and the whole-file refusals."""

    def test_f35_rerun_changes_nothing(self):
        path = self.write(
            [self.row(ContractID="CON-1"), self.row(ContractID="CON-2", EndDate="")]
        )
        first = self.sync(path, apply=True)
        second = self.sync(path, apply=True)
        self.assertEqual(first["buckets"]["created"], 2)
        self.assertEqual(second["buckets"]["created"], 0)
        self.assertEqual(second["buckets"]["updated"], 0)
        self.assertEqual(second["buckets"]["unchanged"], 2)

    def test_f36_older_file_is_refused_unless_forced(self):
        self.sync([self.row()], apply=True)
        older = [self.row(ExportedAt="2026-10-12T09:00:00+03:00", Status="Expired")]
        with self.assertRaisesMessage(SyncRefused, "Stale file"):
            self.sync(older, apply=True)
        result = self.sync(older, apply=True, force=True)
        self.assertEqual(result["buckets"]["updated"], 1)

    def test_f37_partial_export_is_refused_unless_forced(self):
        self.sync([self.row(ContractID=f"CON-{i}") for i in range(10)], apply=True)
        with self.assertRaisesMessage(SyncRefused, "Partial export"):
            self.sync([self.row(ContractID="CON-1")], apply=True)
        self.sync([self.row(ContractID=f"CON-{i}") for i in range(8)], apply=True)
        with self.assertRaisesMessage(SyncRefused, "min-ratio"):
            self.sync([self.row()], min_ratio=0)
        self.sync([self.row(ContractID="CON-1")], apply=True, force=True)

    def test_f38_records_missing_from_file_are_only_counted(self):
        self.sync([self.row(ContractID=f"CON-{i}") for i in range(5)], apply=True)
        result = self.sync(
            [self.row(ContractID=f"CON-{i}") for i in range(4)], apply=True
        )
        self.assertEqual(result["reconciliation"]["records_missing_from_file"], 1)
        self.assertEqual(ContractRecord.objects.count(), 5)

    def test_f39_bom_and_crlf_are_read(self):
        path = self.write([self.row()], encoding="utf-8-sig", newline="\r\n")
        self.assertEqual(self.sync(path, apply=True)["buckets"]["created"], 1)

    def test_f39_bad_files_are_refused_whole(self):
        cases = {
            "comma-separated": self.write([self.row()], delimiter=";"),
            "not UTF-8": self.write([self.row(Department="Café")], encoding="latin-1"),
            "header row is repeated": self.write(
                [self.row(), dict(zip(HEADER, HEADER))]
            ),
            "empty": self.dir / "empty.csv",
        }
        (self.dir / "empty.csv").write_text("")
        ragged = self.dir / "ragged.csv"
        ragged.write_text(",".join(HEADER) + "\nCON-1,TEST001,too,few\n")
        cases["cells, expected"] = ragged
        for message, path in cases.items():
            with self.subTest(message), self.assertRaisesMessage(SyncRefused, message):
                self.sync(path, apply=True)
        self.assertFalse(ContractRecord.objects.exists())

    def test_f39_exported_at_must_be_one_timestamp(self):
        rows = [
            self.row(ContractID="CON-1"),
            self.row(ContractID="CON-2", ExportedAt="2026-10-13T10:00:00+03:00"),
        ]
        with self.assertRaisesMessage(SyncRefused, "same on every row"):
            self.sync(rows)
        with self.assertRaisesMessage(SyncRefused, "ISO timestamp"):
            self.sync([self.row(ExportedAt="13/10/2026")])

    def test_f40_a_second_run_cannot_overlap(self):
        other = connections.create_connection("default")
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", [ADVISORY_LOCK_KEY])
            with self.assertRaisesMessage(SyncRefused, "holds the lock"):
                self.sync([self.row()], apply=True)
            with self.assertRaisesMessage(SyncRefused, "holds the lock"):
                run_undo("any", apply=True)
        finally:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [ADVISORY_LOCK_KEY])
            other.close()

    def test_f41_crash_midway_leaves_whole_rows_and_rerun_completes(self):
        from contract_records import sync

        path = self.write([self.row(ContractID=f"CON-{i}") for i in range(3)])
        real_save = sync._save
        calls = []

        def crash_on_second(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real_save(*args, **kwargs)

        with mock.patch.object(sync, "_save", crash_on_second):
            with self.assertRaises(KeyboardInterrupt):
                self.sync(path, apply=True)
        self.assertEqual(
            list(ContractRecord.objects.values_list("external_ref", flat=True)),
            ["CON-0"],
        )
        result = self.sync(path, apply=True)
        self.assertEqual(
            (result["buckets"]["created"], result["buckets"]["unchanged"]), (2, 1)
        )

    def test_f42_command_stops_this_process_schedulers(self):
        scheduler = BackgroundScheduler()
        scheduler.add_job(lambda: None, "interval", seconds=20)
        scheduler.start()
        module = types.ModuleType("fakeapp.scheduler")
        module.scheduler = scheduler
        with mock.patch.dict(sys.modules, {"fakeapp.scheduler": module}):
            self.assertGreaterEqual(stop_in_process_schedulers(), 1)
        self.assertFalse(scheduler.running)

    def test_f43_rows_are_streamed(self):
        path = self.write([self.row()])
        self.assertIsInstance(_rows(path), types.GeneratorType)


class SafetyTests(Base):
    """Dry run, undo, protected data, privacy and request-independence."""

    def protected(self):
        """Every value this command must never change."""
        state = {
            "work": list(
                EmployeeWorkInformation.objects.entire()
                .order_by("pk")
                .values_list("pk", "date_joining", "basic_salary", "salary_hour")
            ),
            "active": list(
                Employee.objects.entire().order_by("pk").values_list("pk", "is_active")
            ),
        }
        for model in apps.get_app_config("leave").get_models():
            state[model._meta.label] = list(model._base_manager.order_by("pk").values())
        if apps.is_installed("payroll"):
            from payroll.models.models import Contract

            state["contract"] = list(Contract._base_manager.order_by("pk").values())
        return state

    def counts(self):
        models = [ContractRecord, LogEntry, Employee, EmployeeWorkInformation]
        models += list(apps.get_app_config("leave").get_models())
        if apps.is_installed("payroll"):
            from payroll.models.models import Contract

            models.append(Contract)
        return {m._meta.label: m._base_manager.count() for m in models}

    def setUp(self):
        super().setUp()
        set_work(self.alpha, date_joining=date(2024, 2, 1), basic_salary=1000)

    def test_dry_run_writes_nothing(self):
        before = self.counts()
        result = self.sync([self.row(ContractID="CON-1"), self.row(ContractID="CON-2")])
        self.assertEqual(result["buckets"]["created"], 2)
        self.assertEqual(self.counts(), before)
        self.assertFalse(result["apply"])

    def test_never_touches_joining_salary_active_or_leave(self):
        before = self.protected()
        self.sync([self.row(StartDate="2020-01-01")], apply=True)
        self.sync([self.row(Status="Terminated")], apply=True)
        run_joining_report(self.write([self.row()]), review_out=self.dir / "j.csv")
        self.assertEqual(self.protected(), before)

    def test_undo_removes_only_rows_that_batch_created(self):
        first = self.sync(
            [self.row(ContractID="CON-1"), self.row(ContractID="CON-2")], apply=True
        )
        later = "2026-10-14T09:00:00+03:00"
        second = self.sync(
            [
                self.row(ContractID="CON-1", ExportedAt=later),
                self.row(ContractID="CON-2", ExportedAt=later, Status="Expired"),
                self.row(ContractID="CON-3", ExportedAt=later),
            ],
            apply=True,
        )
        self.assertNotEqual(first["batch"], second["batch"])
        self.assertEqual(run_undo(second["batch"])["deleted"], 1)
        self.assertEqual(ContractRecord.objects.count(), 3)
        run_undo(second["batch"], apply=True)
        self.assertEqual(
            sorted(ContractRecord.objects.values_list("external_ref", flat=True)),
            ["CON-1", "CON-2"],
        )
        # Updated rows are not rolled back by undo; re-run the older export.
        self.assertEqual(self.record("CON-2").status, "expired")

    def test_personal_data_columns_are_refused(self):
        for extra in ("FullName", "Email", "BasicSalary", "Notes", "ManagerName"):
            header = HEADER + [extra]
            with self.subTest(extra), self.assertRaisesMessage(
                SyncRefused, "personal-data"
            ):
                self.sync(self.write([self.row()], header=header))
        with self.assertRaisesMessage(SyncRefused, "Unexpected: Extra"):
            self.sync(self.write([self.row()], header=HEADER + ["Extra"]))

    def test_names_and_emails_in_cells_are_refused(self):
        with self.assertRaisesMessage(SyncRefused, "12-character hex"):
            self.sync([self.row(NameTokens="alpha tester")])
        with self.assertRaisesMessage(SyncRefused, "email"):
            self.sync([self.row(Role="@".join(["someone", "example.invalid"]))])

    def test_paths_inside_the_repository_are_refused(self):
        inside = Path(settings.BASE_DIR) / "contracts.csv"
        with self.assertRaisesMessage(SyncRefused, "inside the repository"):
            run_sync(inside)
        with self.assertRaisesMessage(SyncRefused, "inside the repository"):
            self.sync([self.row()], review_out=Path(settings.BASE_DIR) / "review.csv")

    def test_symlink_into_the_repository_is_refused(self):
        link = self.dir / "export-link.csv"
        link.symlink_to(Path(settings.BASE_DIR) / "manage.py")
        with self.assertRaisesMessage(SyncRefused, "inside the repository"):
            run_sync(link)

    def test_missing_salt_is_refused(self):
        with mock.patch.dict(os.environ, {SALT_ENV: ""}):
            with self.assertRaisesMessage(SyncRefused, SALT_ENV):
                self.sync([self.row()])

    def test_runs_without_a_request(self):
        class ExplodingRequest:
            def __getattr__(self, name):
                raise AssertionError(f"request.{name} was read")

        _thread_locals.request = ExplodingRequest()
        result = self.sync([self.row()], apply=True)
        run_joining_report(self.write([self.row()]), review_out=self.dir / "j.csv")
        run_undo(result["batch"], apply=True)
        self.assertEqual(result["buckets"]["created"], 1)

    def test_rows_read_equals_sum_of_buckets(self):
        rows = [
            self.row(ContractID="CON-1"),
            self.row(ContractID="CON-1"),
            self.row(ContractID=""),
            self.row(ContractID="CON-2", EmployeeCode="TEST404"),
            self.row(ContractID="CON-3", Status="Deleted"),
        ]
        result = self.sync(rows, apply=True)
        buckets = result["buckets"]
        self.assertTrue(result["identity_ok"])
        self.assertEqual(buckets["rows_read"], 5)
        self.assertEqual(
            (
                buckets["created"],
                buckets["rejected"],
                buckets["review"],
                buckets["skipped_deleted_never_synced"],
            ),
            (1, 2, 1, 1),
        )

    def test_auditlog_rows_per_write(self):
        before = LogEntry.objects.count()
        result = self.sync(
            [self.row(ContractID=f"CON-{i}") for i in range(10)], apply=True
        )
        self.assertEqual(result["buckets"]["created"], 10)
        self.assertEqual(LogEntry.objects.count() - before, 10)

    def test_command_prints_counts_and_codes_only(self):
        path = self.write(
            [self.row(), self.row(ContractID="CON-2", EmployeeCode="TEST002")]
        )
        out = StringIO()
        call_command(
            "sync_contract_records",
            csv=str(path),
            apply=True,
            review_out=str(self.dir / "cmd-review.csv"),
            stdout=out,
        )
        text = out.getvalue() + (self.dir / "cmd-review.csv").read_text()
        self.assertIn("created: 1", text)
        self.assertIn("identity (rows_read = sum of buckets): OK", text)
        for secret in ("Alpha", "Tester", "Bravo", "Sample", SALT):
            self.assertNotIn(secret, text)
        self.assertEqual(
            oct((self.dir / "cmd-review.csv").stat().st_mode & 0o777), "0o600"
        )

    def test_command_fails_when_counts_do_not_add_up(self):
        broken = {
            "apply": False,
            "batch": "b",
            "sha256": "0",
            "file_rows": 0,
            "exported_at": None,
            "buckets": {
                "rows_read": 1,
                **{
                    k: 0
                    for k in (
                        "created",
                        "updated",
                        "unchanged",
                        "skipped_deleted_never_synced",
                        "rejected",
                        "review",
                    )
                },
            },
            "identity_ok": False,
            "reasons": {},
            "flags": {},
            "accepted_codes_unused": 0,
            "codes_with_differing_names": 0,
            "names_on_several_codes": 0,
            "quality": {},
            "reconciliation": {},
            "review_file": "x",
        }
        with mock.patch(
            "contract_records.management.commands.sync_contract_records.run_sync",
            return_value=broken,
        ):
            with self.assertRaisesMessage(CommandError, "do not add up"):
                call_command("sync_contract_records", csv="x", stdout=StringIO())

    def test_reconciliation_counts(self):
        make("india", "TEST004", "India", "Example")
        archived = make("juliet", "TEST005", "Juliet", "Example", active=False)
        rows = [
            self.row(ContractID="CON-1", Status="Terminated"),
            self.row(
                ContractID="CON-2",
                EmployeeCode="TEST005",
                NameTokens=tokens("Juliet Example"),
            ),
        ]
        result = self.sync(rows, apply=True)
        counts = result["reconciliation"]
        # bravo and india are active with no record.
        self.assertEqual(counts["active_employees_without_record"], 2)
        self.assertEqual(counts["active_employees_latest_exited_or_terminated"], 1)
        self.assertEqual(counts["archived_employees_latest_active"], 1)
        self.assertEqual(
            ContractRecord.objects.get(external_ref="CON-2").employee, archived
        )
        # alpha's work information made a draft payroll contract (payroll signal).
        self.assertIn("employees_with_payroll_contract_but_no_record", counts)


class AdminTests(Base):
    """B: the admin page is view-only, for staff holding the view permission."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth.models import Permission

        self.sync([self.row()], apply=True)
        self.viewer, _ = make_employee("viewer")
        self.viewer.is_staff = True
        self.viewer.save()
        self.viewer.user_permissions.add(
            Permission.objects.get(codename="view_contractrecord")
        )
        self.client.force_login(self.viewer)

    def test_staff_viewer_can_list_and_open_but_not_change(self):
        record = self.record()
        base = "/admin/contract_records/contractrecord/"
        self.assertEqual(self.client.get(base).status_code, 200)
        self.assertEqual(self.client.get(f"{base}?q=TEST001").status_code, 200)
        self.assertEqual(self.client.get(f"{base}{record.pk}/change/").status_code, 200)
        self.assertEqual(self.client.get(f"{base}add/").status_code, 403)
        self.assertEqual(self.client.get(f"{base}{record.pk}/delete/").status_code, 403)
        self.client.post(f"{base}{record.pk}/change/", {"status": "exited"})
        self.assertEqual(self.record().status, "active")

    def test_superuser_cannot_change_either(self):
        self.viewer.is_superuser = True
        self.viewer.save()
        base = "/admin/contract_records/contractrecord/"
        self.assertEqual(self.client.get(f"{base}add/").status_code, 403)
        self.assertEqual(
            self.client.get(f"{base}{self.record().pk}/delete/").status_code, 403
        )
