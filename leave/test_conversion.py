"""
Tests for the leave allocation -> leave request conversion (leave/conversion.py
and the convert_leave_allocations command).

Synthetic data only. The test runner never touches the real database.
"""

import csv
import shutil
import tempfile
from datetime import date, datetime
from io import StringIO
from pathlib import Path

from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models.signals import pre_save
from django.test import TestCase, override_settings

from attendance.models import WorkRecords
from base.models import CompanyLeaves
from horilla.horilla_middlewares import _thread_locals
from leave.accrual import NAIROBI
from leave.conversion import plan_all
from leave.models import (
    AvailableLeave,
    LeaveAllocationRequest,
    LeaveRequest,
    LeaveType,
)
from leave.tests import add_missing_user_column, make_employee
from notifications.models import Notification


@override_settings(LEAVE_ALLOCATION_CUTOVER="2026-10-01")
class ConversionTests(TestCase):
    @classmethod
    def setUpClass(cls):
        add_missing_user_column()
        super().setUpClass()

    def setUp(self):
        _thread_locals.request = None
        self.addCleanup(setattr, _thread_locals, "request", None)
        self.out_dir = tempfile.mkdtemp(prefix="conversion-test-")
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)
        # Saturdays and Sundays are company leave, as configured in production.
        for weekday in ("5", "6"):
            CompanyLeaves(based_on_week=None, based_on_week_day=weekday).save()
        self.annual = LeaveType.objects.create(
            name="Annual Leave",
            total_days=24,
            exclude_company_leave="yes",
            exclude_holiday="yes",
        )
        self.user, self.worker = make_employee("worker")
        _, self.colleague = make_employee("colleague", with_user=False)
        self.balance = AvailableLeave.objects.create(
            employee_id=self.worker, leave_type_id=self.annual, available_days=10
        )

    def allocation(self, days, created, status="approved", employee=None, reason=""):
        allocation = LeaveAllocationRequest.objects.create(
            employee_id=employee or self.worker,
            leave_type_id=self.annual,
            requested_days=days,
            description="time off",
            status=status,
            reject_reason=reason,
        )
        LeaveAllocationRequest.objects.filter(pk=allocation.pk).update(created_at=created)
        allocation.refresh_from_db()
        return allocation

    @staticmethod
    def at(year, month, day, hour=9, minute=0, second=0):
        return datetime(year, month, day, hour, minute, second, tzinfo=NAIROBI)

    def convert(self, *extra, apply=False):
        out = StringIO()
        args = ["convert_leave_allocations", "--out-dir", self.out_dir, *extra]
        if apply:
            dry = StringIO()
            call_command(*args, stdout=dry)
            count = self.changes(dry.getvalue())
            args += ["--apply", "--confirm-rows", str(count)]
        call_command(*args, stdout=out)
        return out.getvalue()

    @staticmethod
    def changes(output):
        line = next(l for l in output.splitlines() if l.startswith("allocations:"))
        parts = dict(
            part.strip().rsplit(" ", 1) for part in line.split("|")[1:] if part.strip()
        )
        return sum(int(v) for k, v in parts.items() if k != "none")

    def converted(self, allocation):
        return LeaveRequest.objects.get(
            description__startswith=f"[Allocation #{allocation.pk}] "
        )

    def mapping_file(self):
        return next(Path(self.out_dir).glob("*-mapping.csv"))

    def write_decisions(self, rows):
        path = Path(self.out_dir) / "decisions.csv"
        with open(path, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["alloc_id", "keep_drop", "start_date", "end_date"])
            writer.writerows(rows)
        return str(path)

    def test_assumed_dates_reproduce_the_days_without_deducting(self):
        # Wed 29 Jul: Wed-Fri, then Mon-Tue = 5 working days.
        approved = self.allocation(5, self.at(2026, 7, 29))
        # Sat 1 Aug: moves to Mon 3 Aug; 2.5 days ends on Wed's first half.
        pending = self.allocation(2.5, self.at(2026, 8, 1), status="requested")
        self.convert(apply=True)
        first, second = self.converted(approved), self.converted(pending)
        self.assertEqual(
            (first.start_date, first.end_date, first.requested_days, first.status),
            (date(2026, 7, 29), date(2026, 8, 4), 5, "approved"),
        )
        self.assertEqual(
            (second.start_date, second.end_date, second.end_date_breakdown),
            (date(2026, 8, 3), date(2026, 8, 5), "first_half"),
        )
        self.assertEqual((second.requested_days, second.status), (2.5, "requested"))
        # Nothing deducted, nothing refundable, no mail, no notification.
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.available_days, 10)
        self.assertEqual(
            (first.approved_available_days, first.approved_carryforward_days), (0, 0)
        )
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(Notification.objects.exists())
        approved.refresh_from_db()
        self.assertEqual(approved.status, "rejected")
        self.assertEqual(
            approved.reject_reason,
            f"Converted to leave request #{first.pk} (leave clean-up 2026)",
        )

    def test_work_records_are_untouched(self):
        # A real leave record inside the pending allocation's assumed dates
        # would be deleted by the attendance signal if it were left on.
        existing = WorkRecords.objects.create(
            employee_id=self.worker,
            date=date(2026, 8, 4),
            is_leave_record=True,
            work_record_type="ABS",
            note="real leave",
        )
        self.allocation(5, self.at(2026, 7, 29))
        self.allocation(3, self.at(2026, 8, 3), status="requested")
        self.convert(apply=True)
        self.assertEqual(list(WorkRecords.objects.all()), [existing])
        # The signal is connected again afterwards.
        self.assertTrue(pre_save.has_listeners(LeaveRequest))

    def test_second_run_changes_nothing(self):
        self.allocation(5, self.at(2026, 7, 29))
        self.allocation(1, self.at(2026, 8, 3), status="rejected", reason="no")
        self.convert(apply=True)
        output = self.convert()
        self.assertEqual(self.changes(output), 0)
        self.assertEqual(LeaveRequest.objects.count(), 2)
        rejected = LeaveRequest.objects.get(status="rejected")
        self.assertEqual(rejected.requested_days, 1)

    def test_duplicates_dropped_and_hr_can_switch_keep_drop(self):
        first = self.allocation(7, self.at(2026, 7, 30, 7, 17, 32))
        second = self.allocation(7, self.at(2026, 7, 30, 7, 17, 34))
        self.convert(apply=True)
        second.refresh_from_db()
        self.assertEqual(LeaveRequest.objects.count(), 1)
        self.assertEqual(
            second.reject_reason, f"Duplicate of #{first.pk} (leave clean-up 2026)"
        )
        # HR: keep both (drop -> keep), then drop the first (keep -> drop).
        decisions = self.write_decisions([[second.pk, "keep", "", ""]])
        self.convert("--decisions", decisions, apply=True)
        self.assertEqual(LeaveRequest.objects.count(), 2)
        second.refresh_from_db()
        self.assertEqual(second.status, "rejected")
        self.assertIn("Converted to leave request #", second.reject_reason)
        decisions = self.write_decisions(
            [[second.pk, "keep", "", ""], [first.pk, "drop", "", ""]]
        )
        self.convert("--decisions", decisions, apply=True)
        self.assertEqual(
            list(LeaveRequest.objects.values_list("description", flat=True)),
            [f"[Allocation #{second.pk}] time off"],
        )
        self.assertEqual(self.changes(self.convert("--decisions", decisions)), 0)

    def test_hr_dates_replace_the_assumed_ones(self):
        allocation = self.allocation(5, self.at(2026, 7, 29))
        self.convert(apply=True)
        decisions = self.write_decisions([[allocation.pk, "", "2026-09-07", ""]])
        self.convert("--decisions", decisions, apply=True)
        leave_request = self.converted(allocation)
        self.assertEqual(
            (leave_request.start_date, leave_request.end_date, leave_request.requested_days),
            (date(2026, 9, 7), date(2026, 9, 11), 5),
        )
        self.assertEqual(LeaveRequest.objects.count(), 1)

    def test_reverse_restores_everything(self):
        approved = self.allocation(5, self.at(2026, 7, 29))
        refused = self.allocation(2, self.at(2026, 8, 3), status="rejected", reason="no")
        duplicate = self.allocation(5, self.at(2026, 7, 29, 9, 0, 2))
        self.convert(apply=True)
        mapping = str(self.mapping_file())
        dry = StringIO()
        call_command("convert_leave_allocations", "--reverse", "--mapping", mapping, stdout=dry)
        self.assertIn("would reverse: 3", dry.getvalue())
        call_command(
            "convert_leave_allocations", "--reverse", "--mapping", mapping,
            "--apply", "--confirm-rows", "3", stdout=StringIO(),
        )
        self.assertFalse(LeaveRequest.objects.exists())
        for allocation, status, reason in [
            (approved, "approved", ""),
            (refused, "rejected", "no"),
            (duplicate, "approved", ""),
        ]:
            allocation.refresh_from_db()
            self.assertEqual((allocation.status, allocation.reject_reason), (status, reason))
        again = StringIO()
        call_command("convert_leave_allocations", "--reverse", "--mapping", mapping, stdout=again)
        self.assertIn("would reverse: 0", again.getvalue())

    def test_edited_description_is_still_recognised(self):
        allocation = self.allocation(5, self.at(2026, 7, 29))
        self.convert(apply=True)
        LeaveRequest.objects.filter(pk=self.converted(allocation).pk).update(
            description="edited by HR"
        )
        self.assertEqual(self.changes(self.convert()), 0)

    def test_allocations_from_the_cutover_are_not_touched(self):
        grant = self.allocation(3, self.at(2026, 10, 1, 0, 0, 1))
        plans, _, _ = plan_all()
        self.assertEqual(plans, [])
        self.convert(apply=True)
        grant.refresh_from_db()
        self.assertEqual(grant.status, "approved")
        self.assertFalse(LeaveRequest.objects.exists())

    def test_requests_approved_since_are_left_to_the_ui(self):
        allocation = self.allocation(2, self.at(2026, 8, 3), status="requested")
        self.convert(apply=True)
        # A manager approves the converted request: the view deducts 2 days.
        LeaveRequest.objects.filter(pk=self.converted(allocation).pk).update(
            status="approved", approved_available_days=2
        )
        decisions = self.write_decisions([[allocation.pk, "drop", "", ""]])
        with self.assertRaises(CommandError):
            self.convert("--decisions", decisions)
        mapping = str(self.mapping_file())
        with self.assertRaises(CommandError):
            call_command(
                "convert_leave_allocations", "--reverse", "--mapping", mapping,
                stdout=StringIO(),
            )
        self.assertTrue(LeaveRequest.objects.filter(status="approved").exists())

    def test_refusals_write_nothing(self):
        allocation = self.allocation(5, self.at(2026, 7, 29))
        cases = [
            ["--apply", "--confirm-rows", "7"],
            ["--apply"],
            ["--decisions", self.write_decisions([[999999, "drop", "", ""]])],
            ["--decisions", self.write_decisions([[allocation.pk, "maybe", "", ""]])],
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                with self.assertRaises(CommandError):
                    call_command(
                        "convert_leave_allocations", "--out-dir", self.out_dir, *extra,
                        stdout=StringIO(),
                    )
        self.assertFalse(LeaveRequest.objects.exists())
        allocation.refresh_from_db()
        self.assertEqual(allocation.status, "approved")
