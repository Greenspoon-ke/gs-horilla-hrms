"""
Tests for recording leave HR confirms was taken (leave/hr_records.py and the
record_hr_leave command).

Synthetic data only. The test runner never touches the real database.
"""

import shutil
import tempfile
from datetime import date
from io import StringIO
from pathlib import Path

from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models.signals import pre_save
from django.test import TestCase

from attendance.models import WorkRecords
from base.models import CompanyLeaves
from employee.models import Employee
from horilla.horilla_middlewares import _thread_locals
from leave.models import AvailableLeave, LeaveRequest, LeaveType
from leave.tests import add_missing_user_column, make_employee

HEADER = "ref,code,leave_type,start_date,end_date,status\n"


class HrRecordTests(TestCase):
    @classmethod
    def setUpClass(cls):
        add_missing_user_column()
        super().setUpClass()

    def setUp(self):
        _thread_locals.request = None
        self.addCleanup(setattr, _thread_locals, "request", None)
        self.out_dir = tempfile.mkdtemp(prefix="hr-records-test-")
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)
        for weekday in ("5", "6"):
            CompanyLeaves(based_on_week=None, based_on_week_day=weekday).save()
        self.annual = LeaveType.objects.create(
            name="Annual Leave",
            total_days=24,
            exclude_company_leave="yes",
            exclude_holiday="yes",
        )
        _, self.worker = make_employee("worker")
        Employee.objects.filter(pk=self.worker.pk).update(badge_id="TST001")
        self.balance = AvailableLeave.objects.create(
            employee_id=self.worker, leave_type_id=self.annual, available_days=10
        )

    def write(self, *lines):
        path = Path(self.out_dir) / "records.csv"
        path.write_text(HEADER + "".join(line + "\n" for line in lines))
        return str(path)

    def run_cmd(self, *args):
        out = StringIO()
        call_command("record_hr_leave", "--out-dir", self.out_dir, *args, stdout=out)
        return out.getvalue()

    def apply(self, path):
        dry = self.run_cmd("--csv", path)
        line = next(l for l in dry.splitlines() if l.startswith("records:"))
        count = sum(
            int(part.strip().rsplit(" ", 1)[1])
            for part in line.split("|")[1:]
            if part.strip() and not part.strip().startswith("none")
        )
        return self.run_cmd("--csv", path, "--apply", "--confirm-rows", str(count))

    def test_records_a_past_week_without_side_effects(self):
        # A real leave record in that week must survive (attendance sync off).
        existing = WorkRecords.objects.create(
            employee_id=self.worker,
            date=date(2026, 7, 28),
            is_leave_record=True,
            work_record_type="ABS",
            note="real leave",
        )
        path = self.write("rota-17,tst001,Annual Leave,2026-07-27,2026-08-02,approved")
        self.apply(path)
        leave_request = LeaveRequest.objects.get()
        self.assertEqual(
            (
                leave_request.status,
                leave_request.start_date,
                leave_request.end_date,
                leave_request.requested_days,
            ),
            ("approved", date(2026, 7, 27), date(2026, 8, 2), 5),
        )
        self.assertTrue(leave_request.description.startswith("[HR leave rota-17] "))
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.available_days, 10)
        self.assertEqual(list(WorkRecords.objects.all()), [existing])
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(pre_save.has_listeners(LeaveRequest))
        # Re-run: nothing to do.
        self.assertIn("none 1", self.run_cmd("--csv", path))

    def test_changes_update_the_same_request_without_attendance_records(self):
        # Re-saving a request is where the attendance signal would act: it
        # writes ABS records for an approved request and deletes leave
        # records for any other status.
        existing = WorkRecords.objects.create(
            employee_id=self.worker,
            date=date(2026, 7, 21),
            is_leave_record=True,
            work_record_type="ABS",
            note="real leave",
        )
        self.apply(self.write("rota-17,TST001,Annual Leave,2026-07-27,2026-08-02,approved"))
        self.apply(self.write("rota-17,TST001,Annual Leave,2026-07-20,2026-07-24,approved"))
        leave_request = LeaveRequest.objects.get()
        self.assertEqual((leave_request.start_date, leave_request.requested_days), (date(2026, 7, 20), 5))
        self.apply(self.write("rota-17,TST001,Annual Leave,2026-07-20,2026-07-24,requested"))
        self.assertEqual(LeaveRequest.objects.get().status, "requested")
        self.assertEqual(list(WorkRecords.objects.all()), [existing])

    def test_reverse_deletes_what_a_run_created(self):
        self.apply(self.write("rota-17,TST001,Annual Leave,2026-07-27,2026-08-02,approved"))
        mapping = str(next(Path(self.out_dir).glob("*-mapping.csv")))
        self.assertIn("would reverse: 1", self.run_cmd("--reverse", "--mapping", mapping))
        self.run_cmd("--reverse", "--mapping", mapping, "--apply", "--confirm-rows", "1")
        self.assertFalse(LeaveRequest.objects.exists())
        self.assertIn("would reverse: 0", self.run_cmd("--reverse", "--mapping", mapping))

    def test_deducted_requests_are_left_to_the_ui(self):
        path = self.write("rota-17,TST001,Annual Leave,2026-07-27,2026-08-02,approved")
        self.apply(path)
        LeaveRequest.objects.update(approved_available_days=5)
        with self.assertRaises(CommandError):
            self.run_cmd("--csv", self.write("rota-17,TST001,Annual Leave,2026-07-20,2026-07-24,approved"))

    def test_bad_lines_refuse_and_write_nothing(self):
        cases = [
            "rota-1,NOPE,Annual Leave,2026-07-27,2026-08-02,approved",
            "rota-1,TST001,Holiday Leave,2026-07-27,2026-08-02,approved",
            "rota-1,TST001,Annual Leave,2026-08-02,2026-07-27,approved",
            "rota-1,TST001,Annual Leave,2026-07-27,2026-08-02,maybe",
            "rota 1,TST001,Annual Leave,2026-07-27,2026-08-02,approved",
        ]
        for line in cases:
            with self.subTest(line=line):
                with self.assertRaises(CommandError):
                    self.run_cmd("--csv", self.write(line))
        with self.assertRaises(CommandError):
            self.run_cmd(
                "--csv",
                self.write(
                    "rota-1,TST001,Annual Leave,2026-07-27,2026-08-02,approved",
                    "rota-1,TST001,Annual Leave,2026-07-20,2026-07-24,approved",
                ),
            )
        good = self.write("rota-1,TST001,Annual Leave,2026-07-27,2026-08-02,approved")
        with self.assertRaises(CommandError):
            self.run_cmd("--csv", good, "--apply", "--confirm-rows", "3")
        self.assertFalse(LeaveRequest.objects.exists())
