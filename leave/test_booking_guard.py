"""
Tests for the booking guard on accrual rows (HR decision D6): days not yet
earned cannot be requested or approved.

Synthetic data only. The test runner never touches the real database.
"""

from datetime import date, timedelta

from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from horilla.horilla_middlewares import _thread_locals
from leave.models import AvailableLeave, LeaveRequest, LeaveType
from leave.tests import add_missing_user_column, make_employee


class BookingGuardTests(TestCase):
    @classmethod
    def setUpClass(cls):
        add_missing_user_column()
        super().setUpClass()

    def setUp(self):
        _thread_locals.request = None
        self.addCleanup(setattr, _thread_locals, "request", None)
        self.hr_user, _ = make_employee("hr")
        self.hr_user.user_permissions.add(
            *Permission.objects.filter(
                codename__in=["view_leaverequest", "change_leaverequest"],
                content_type__app_label="leave",
            )
        )
        self.worker_user, self.worker = make_employee("worker")
        # Yearly reset on 1 January, as Annual Leave is configured.
        self.leave_type = LeaveType.objects.create(
            name="Annual Leave",
            total_days=24,
            reset=True,
            reset_based="yearly",
            reset_month="1",
            reset_day="1",
            carryforward_type="carryforward",
            carryforward_max=10,
        )
        self.balance = AvailableLeave.objects.create(
            leave_type_id=self.leave_type,
            employee_id=self.worker,
            available_days=2,
        )
        # A full week inside next leave year: the forecast would add 24 days.
        self.next_year = self.leave_type.leave_type_next_reset_date()
        self.start = self.next_year + timedelta(days=7)
        self.end = self.start + timedelta(days=4)

    def enrol(self):
        self.balance.last_accrual_date = date(2026, 10, 1)
        self.balance.save()

    def new_request(self):
        return LeaveRequest(
            employee_id=self.worker,
            leave_type_id=self.leave_type,
            start_date=self.start,
            end_date=self.end,
            description="trip",
        )

    def clean_as(self, user, leave_request):
        # LeaveRequest.clean reads the current request's user, and the company
        # manager it queries through reads the session.
        request = RequestFactory().get("/")
        request.user = user
        request.session = {}
        _thread_locals.request = request
        leave_request.clean()

    def test_forecast_is_zero_for_accrual_rows(self):
        self.assertEqual(self.balance.forcasted_leaves(self.start), 24)
        self.enrol()
        self.assertEqual(self.balance.forcasted_leaves(self.start), 0)

    def test_rows_not_on_accrual_keep_the_forecast(self):
        # Unchanged behaviour: next year's 24 days still count for them.
        self.clean_as(self.worker_user, self.new_request())

    def test_request_beyond_earned_days_is_rejected(self):
        self.enrol()
        with self.assertRaisesMessage(ValidationError, "sufficient leave balance"):
            self.clean_as(self.worker_user, self.new_request())

    def test_request_within_earned_days_is_accepted(self):
        self.enrol()
        leave_request = self.new_request()
        leave_request.end_date = self.start + timedelta(days=1)
        self.clean_as(self.worker_user, leave_request)

    def test_superuser_request_is_also_checked(self):
        # The superuser bypass skips restricted-day checks, not the balance.
        self.enrol()
        self.hr_user.is_superuser = True
        self.hr_user.save()
        with self.assertRaisesMessage(ValidationError, "sufficient leave balance"):
            self.clean_as(self.hr_user, self.new_request())

    def test_approval_beyond_balance_is_blocked(self):
        self.enrol()
        # Saved without clean(), as a request made before enrolment would be.
        leave_request = self.new_request()
        leave_request.save()
        client = Client()
        client.force_login(self.hr_user)
        client.get(reverse("request-approve", args=[leave_request.id]))
        leave_request.refresh_from_db()
        self.balance.refresh_from_db()
        self.assertEqual(leave_request.status, "requested")
        self.assertEqual(self.balance.available_days, 2)

    def test_approval_within_balance_succeeds(self):
        self.enrol()
        leave_request = self.new_request()
        leave_request.end_date = self.start + timedelta(days=1)
        leave_request.save()
        client = Client()
        client.force_login(self.hr_user)
        client.get(reverse("request-approve", args=[leave_request.id]))
        leave_request.refresh_from_db()
        self.balance.refresh_from_db()
        self.assertEqual(leave_request.status, "approved")
        self.assertEqual(self.balance.available_days, 0)
