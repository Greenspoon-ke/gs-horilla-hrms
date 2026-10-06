"""
Tests for the adjust_leave_balances command (audit section 8, goal C).

Synthetic data only. CSV files are written to a temporary directory, which is
outside the repository, as the command requires.
"""

import tempfile
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from horilla.horilla_middlewares import _thread_locals
from leave.models import AvailableLeave, LeaveType
from leave.tests import add_missing_user_column, make_employee

HEADER = "email,leave_type,available_days,carryforward_days,reason\n"


class AdjustLeaveBalancesTests(TestCase):
    @classmethod
    def setUpClass(cls):
        add_missing_user_column()
        super().setUpClass()

    def setUp(self):
        _thread_locals.request = None
        self.addCleanup(setattr, _thread_locals, "request", None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.annual = LeaveType.objects.create(name="Annual", total_days=24)
        self.sick = LeaveType.objects.create(name="Sick", total_days=14)
        _, self.emp1 = make_employee("one", with_user=False)
        _, self.emp2 = make_employee("two", with_user=False)
        self.row1 = AvailableLeave.objects.create(
            leave_type_id=self.annual,
            employee_id=self.emp1,
            available_days=24,
            carryforward_days=10,
        )
        self.row2 = AvailableLeave.objects.create(
            leave_type_id=self.annual,
            employee_id=self.emp2,
            available_days=24,
            carryforward_days=0,
        )

    def write_csv(self, *lines):
        path = self.tmp / "balances.csv"
        path.write_text(HEADER + "".join(line + "\n" for line in lines))
        return path

    def run_command(self, path, apply=False):
        out = StringIO()
        args = ["--csv", str(path)] + (["--apply"] if apply else [])
        call_command("adjust_leave_balances", *args, stdout=out)
        return out.getvalue()

    def reload(self, row):
        row.refresh_from_db()
        return row

    def good_line(self):
        return f"{self.emp1.email},Annual,6,12,Corrected after review"

    def test_dry_run_reports_but_saves_nothing(self):
        output = self.run_command(self.write_csv(self.good_line()))
        self.assertIn("DRY RUN", output)
        self.assertIn("available 24.0 -> 6.0", output)
        self.assertIn("carryforward 10.0 -> 12.0", output)
        self.assertEqual(self.reload(self.row1).available_days, 24)
        self.assertEqual(self.row1.carryforward_days, 10)

    def test_apply_sets_absolute_values_and_records_the_reason(self):
        self.run_command(self.write_csv(self.good_line()), apply=True)
        row = self.reload(self.row1)
        self.assertEqual((row.available_days, row.carryforward_days), (6, 12))
        self.assertEqual(row.total_leave_days, 18)
        latest = row.history.first()
        self.assertEqual(latest.history_change_reason, "Corrected after review")
        self.assertIsNone(latest.history_user_id)

    def test_applying_the_same_file_twice_changes_nothing(self):
        path = self.write_csv(self.good_line())
        self.run_command(path, apply=True)
        history_rows = self.row1.history.count()
        output = self.run_command(path, apply=True)
        self.assertIn("rows changed: 0, unchanged: 1", output)
        self.assertEqual(self.row1.history.count(), history_rows)

    def test_bad_rows_are_rejected_without_stopping_the_batch(self):
        path = self.write_csv(
            f"{self.emp1.email},Annual,6,12,",  # empty reason
            "nobody@example.invalid,Annual,6,12,Typo",  # unknown employee
            f"{self.emp1.email},Nonexistent,6,12,Typo",  # unknown leave type
            f"{self.emp1.email},Annual,-1,12,Negative",  # negative days
            f"{self.emp1.email},Annual,abc,12,Not a number",
            f"{self.emp1.email},Sick,3,0,No sick balance exists",
            f"{self.emp1.email},Annual,{'x' * 101},0,Bad",  # bad number first
            f"{self.emp1.email},Annual,5,5,{'r' * 101}",  # reason too long
            self.good_line(),
            f"{self.emp1.email},Annual,9,9,Duplicate target",
            f"{self.emp2.email},Annual,20,2,Second employee",
        )
        output = self.run_command(path, apply=True)
        self.assertIn("rows changed: 2, unchanged: 0, rejected: 9", output)
        row1 = self.reload(self.row1)
        self.assertEqual((row1.available_days, row1.carryforward_days), (6, 12))
        row2 = self.reload(self.row2)
        self.assertEqual((row2.available_days, row2.carryforward_days), (20, 2))

    def test_output_never_contains_names_or_emails(self):
        path = self.write_csv(self.good_line(), "nobody@example.invalid,Annual,1,1,x")
        output = self.run_command(path)
        self.assertNotIn("@example.invalid", output)
        self.assertNotIn(self.emp1.employee_first_name, output)
        self.assertIn(f"employee={self.emp1.pk}", output)

    def test_email_match_ignores_case_and_header_spacing(self):
        path = self.tmp / "loose.csv"
        path.write_text(
            " Email , Leave_Type ,available_days,carryforward_days,REASON\n"
            f"{self.emp1.email.upper()}, annual ,6,12,Corrected\n"
        )
        self.run_command(path, apply=True)
        self.assertEqual(self.reload(self.row1).available_days, 6)

    def test_missing_column_is_an_error(self):
        path = self.tmp / "short.csv"
        path.write_text("email,leave_type\na@example.invalid,Annual\n")
        with self.assertRaisesMessage(CommandError, "Missing column(s)"):
            self.run_command(path)

    def test_missing_file_is_an_error(self):
        with self.assertRaisesMessage(CommandError, "not found"):
            self.run_command(self.tmp / "nope.csv")

    def test_csv_inside_the_repository_is_refused(self):
        inside = Path(settings.BASE_DIR) / "balances.csv"
        with self.assertRaisesMessage(CommandError, "inside the repository"):
            self.run_command(inside)
