"""
Tests for removing past pending leave requests as not taken
(leave/past_pending.py and record_hr_leave --past-pending).

Synthetic data only. The test runner never touches the real database.
"""

import shutil
import tempfile
from contextlib import nullcontext
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models.signals import pre_save
from django.test import TestCase

from attendance.models import WorkRecords
from base.models import CompanyLeaves, Department
from employee.models import Employee, EmployeeWorkInformation
from horilla.horilla_middlewares import _thread_locals
from leave import conversion, hr_records, past_pending
from leave.accrual import nairobi_today
from leave.conversion import ConversionRefused
from leave.models import AvailableLeave, LeaveRequest, LeaveType
from leave.tests import add_missing_user_column, make_employee
from notifications.models import Notification


class PastPendingTests(TestCase):
    @classmethod
    def setUpClass(cls):
        add_missing_user_column()
        super().setUpClass()

    def setUp(self):
        _thread_locals.request = None
        self.addCleanup(setattr, _thread_locals, "request", None)
        self.out_dir = tempfile.mkdtemp(prefix="past-pending-test-")
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)
        for weekday in ("5", "6"):
            CompanyLeaves(based_on_week=None, based_on_week_day=weekday).save()
        self.annual = LeaveType.objects.create(
            name="Annual Leave", total_days=24, exclude_company_leave="yes", exclude_holiday="yes"
        )
        self.sick = LeaveType.objects.create(name="Sick Leave", total_days=10)
        _, self.worker = make_employee("worker")
        Employee.objects.filter(pk=self.worker.pk).update(badge_id="TST001")
        _, self.leaver = make_employee("leaver")
        Employee.objects.filter(pk=self.leaver.pk).update(badge_id="TST002", is_active=False)
        self.leaver.refresh_from_db()
        for employee in (self.worker, self.leaver):
            for leave_type in (self.annual, self.sick):
                AvailableLeave.objects.create(
                    employee_id=employee, leave_type_id=leave_type, available_days=10
                )

        today = nairobi_today()

        def day(n):
            return today + timedelta(days=n)

        self.past = self.request(day(-30), day(-26))
        self.past_sick = self.request(day(-9), day(-9), self.sick)
        self.ended_yesterday = self.request(day(-3), day(-1))
        # Not removed:
        self.ends_today = self.request(day(-2), day(0))
        self.future = self.request(day(5), day(7))
        self.approved = self.request(day(-60), day(-58), status="approved")
        self.rejected = self.request(day(-50), day(-48), status="rejected")
        self.converted = self.request(
            day(-40), day(-38), description=conversion.marker(7) + "time off"
        )
        self.recorded = self.request(
            day(-20), day(-18), description=hr_records.marker("rota-1") + "rota"
        )
        self.of_leaver = self.request(day(-30), day(-26), employee=self.leaver)
        self.removable = {self.past.pk, self.past_sick.pk, self.ended_yesterday.pk}

        # Made after the requests: saving a request would act on these.
        self.real_leave = WorkRecords.objects.create(
            employee_id=self.worker,
            date=day(-28),
            is_leave_record=True,
            work_record_type="ABS",
            note="real leave",
        )
        self.attendance = WorkRecords.objects.create(
            employee_id=self.worker, date=day(-2), work_record_type="FDP", note="worked"
        )

    def request(self, start, end, leave_type=None, status="requested", description="time off",
                employee=None):
        leave_request = LeaveRequest(
            employee_id=employee or self.worker,
            leave_type_id=leave_type or self.annual,
            start_date=start,
            end_date=end,
            description=description,
        )
        leave_request.save()
        LeaveRequest.objects.filter(pk=leave_request.pk).update(status=status)
        return leave_request

    def run_cmd(self, *args):
        out = StringIO()
        call_command("record_hr_leave", "--out-dir", self.out_dir, *args, stdout=out)
        return out.getvalue()

    def statuses(self):
        return dict(LeaveRequest.objects.values_list("pk", "status"))

    def balances(self):
        return list(
            AvailableLeave.objects.order_by("pk").values_list(
                "pk", "available_days", "carryforward_days", "total_leave_days"
            )
        )

    def work_records(self):
        return list(WorkRecords.objects.order_by("pk").values())

    def mapping(self):
        return str(next(Path(self.out_dir).glob("past-pending-*-mapping.csv")))

    def test_rejects_only_past_pending_without_side_effects(self):
        before = self.statuses()
        balances, records = self.balances(), self.work_records()

        dry = self.run_cmd("--past-pending")
        self.assertIn("would remove: 3 | Annual Leave 2 | Sick Leave 1", dry)
        self.assertEqual(self.statuses(), before)  # a dry run saves nothing

        self.run_cmd("--past-pending", "--apply", "--confirm-rows", "3")
        after = self.statuses()
        for pk, status in before.items():
            expected = "rejected" if pk in self.removable else status
            self.assertEqual(after[pk], expected, pk)
        self.assertEqual(
            set(
                LeaveRequest.objects.filter(reject_reason=past_pending.REASON).values_list(
                    "pk", flat=True
                )
            ),
            self.removable,
        )
        # The request ending today and the future one stay pending.
        self.assertEqual(after[self.ends_today.pk], "requested")
        self.assertEqual(after[self.future.pk], "requested")
        self.assertEqual(self.balances(), balances)
        self.assertEqual(self.work_records(), records)  # incl. the real leave record
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(Notification.objects.exists())
        self.assertTrue(pre_save.has_listeners(LeaveRequest))  # signal back on
        history = self.past.history_set.order_by("-history_date").first()
        self.assertEqual(
            (history.status, history.history_change_reason),
            ("rejected", past_pending.CHANGE_REASON),
        )
        self.assertIn("would remove: 0", self.run_cmd("--past-pending"))

    def test_clashes_are_recounted_once_removed(self):
        _, colleague = make_employee("colleague")
        department = Department(department="Packing").save()
        EmployeeWorkInformation.objects.filter(
            employee_id__in=[self.worker, colleague]
        ).update(department_id=department)
        overlapping = self.request(
            self.past.start_date, self.past.end_date, employee=colleague, status="approved"
        )
        overlapping.refresh_from_db()
        # Counts the pending one (and the leaver's: no job position matches).
        before = overlapping.leave_clashes_count
        self.assertGreaterEqual(before, 1)
        self.run_cmd("--past-pending", "--apply", "--confirm-rows", "3")
        overlapping.refresh_from_db()
        self.assertEqual(overlapping.leave_clashes_count, before - 1)

    def test_reverse_restores_requested(self):
        self.run_cmd("--past-pending", "--apply", "--confirm-rows", "3")
        # A manager changed one since: the reverse leaves it alone.
        LeaveRequest.objects.filter(pk=self.past_sick.pk).update(reject_reason="manager said no")
        records = self.work_records()

        reverse = ("--past-pending", "--reverse", "--mapping", self.mapping())
        self.assertIn("would reverse: 2", self.run_cmd(*reverse))
        self.run_cmd(*reverse, "--apply", "--confirm-rows", "2")
        for leave_request in (self.past, self.ended_yesterday):
            leave_request.refresh_from_db()
            self.assertEqual(
                (leave_request.status, leave_request.reject_reason), ("requested", "")
            )
        self.assertEqual(
            LeaveRequest.objects.get(pk=self.past_sick.pk).reject_reason, "manager said no"
        )
        self.assertEqual(self.work_records(), records)
        self.assertIn("would reverse: 0", self.run_cmd(*reverse))

    def test_delete_option(self):
        balances, records = self.balances(), self.work_records()
        self.run_cmd("--past-pending", "--delete", "--apply", "--confirm-rows", "3")
        self.assertFalse(LeaveRequest.objects.filter(pk__in=self.removable).exists())
        self.assertEqual(LeaveRequest.objects.count(), 7)
        self.assertEqual(self.balances(), balances)
        self.assertEqual(self.work_records(), records)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(Notification.objects.exists())
        self.assertIn("would remove: 0", self.run_cmd("--past-pending", "--delete"))
        with self.assertRaisesMessage(CommandError, "backup"):
            self.run_cmd("--past-pending", "--reverse", "--mapping", self.mapping())

    def test_delete_refuses_when_work_records_point_to_a_request(self):
        WorkRecords.objects.filter(pk=self.real_leave.pk).update(leave_request_id=self.past)
        with self.assertRaisesMessage(CommandError, "reject instead"):
            self.run_cmd("--past-pending", "--delete")
        self.assertEqual(LeaveRequest.objects.count(), 10)

    def test_balance_or_work_record_change_rolls_everything_back(self):
        # With the attendance sync left on, rejecting deletes the real leave
        # record: the check must catch it and undo the whole run.
        before, records = self.statuses(), self.work_records()
        with mock.patch.object(past_pending, "attendance_sync_off", nullcontext):
            with self.assertRaisesMessage(CommandError, "rolled back"):
                self.run_cmd("--past-pending", "--apply", "--confirm-rows", "3")
        self.assertEqual(self.statuses(), before)
        self.assertEqual(self.work_records(), records)

    def test_argument_and_count_guards(self):
        before = self.statuses()
        for args, message in [
            (("--past-pending", "--csv", "x.csv"), "without --csv"),
            (("--delete",), "only goes with --past-pending"),
            (("--past-pending", "--delete", "--reverse", "--mapping", "x"), "cannot be combined"),
            (("--past-pending", "--apply", "--confirm-rows", "2"), "--confirm-rows must equal"),
            (("--past-pending", "--apply"), "--confirm-rows must equal"),
        ]:
            with self.subTest(args=args), self.assertRaisesMessage(CommandError, message):
                self.run_cmd(*args)
        self.assertEqual(self.statuses(), before)

    def test_count_is_checked_again_under_the_lock(self):
        with self.assertRaisesMessage(ConversionRefused, "not 4"):
            past_pending.apply_removals(nairobi_today(), 4, Path(self.out_dir) / "m.csv")
        self.assertEqual(LeaveRequest.objects.filter(status="rejected").count(), 1)

    def test_refuses_inside_a_web_request(self):
        _thread_locals.request = object()
        with self.assertRaisesMessage(CommandError, "management command"):
            self.run_cmd("--past-pending", "--apply", "--confirm-rows", "3")
        _thread_locals.request = None  # the company-scoped manager reads it
        self.assertEqual(LeaveRequest.objects.filter(status="rejected").count(), 1)

    def test_marker_prefixes_match_the_steps_that_write_them(self):
        self.assertTrue(conversion.marker(12).startswith(past_pending.CONVERTED_PREFIX))
        self.assertTrue(hr_records.marker("rota-1").startswith(past_pending.RECORDED_PREFIX))

