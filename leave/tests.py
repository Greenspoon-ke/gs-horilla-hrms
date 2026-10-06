"""
Tests for the HR-only available leave edit (audit sections 4, 6, 9).

Synthetic data only. The test runner never touches the real database.
"""

from django.contrib.auth.models import Permission, User
from django.db import connection
from django.test import Client, TestCase
from django.urls import reverse

from employee.models import Employee, EmployeeWorkInformation
from horilla.horilla_middlewares import _thread_locals
from leave.models import AvailableLeave, LeaveType

PASSWORD = "not-a-real-password"


def add_missing_user_column():
    """
    base.models attaches auth_user.is_new_employee at import time, but no
    migration in this repo creates it, so a clean test database lacks the
    column and every User insert fails. Call outside the per-class transaction
    so it persists with --keepdb; harmless if the column already exists.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE auth_user ADD COLUMN IF NOT EXISTS "
            "is_new_employee boolean NOT NULL DEFAULT false"
        )


def make_employee(label, with_user=True):
    """Create an Employee (and optionally a login User) with throwaway data."""
    user = None
    if with_user:
        user = User.objects.create_user(username=label, password=PASSWORD)
    employee = Employee.objects.create(
        employee_user_id=user,
        employee_first_name=label,
        email=f"{label}@example.invalid",
        phone="0700000000",
    )
    return user, employee


class AvailableLeaveEditTests(TestCase):
    @classmethod
    def setUpClass(cls):
        add_missing_user_column()
        super().setUpClass()

    def setUp(self):
        # ThreadLocalMiddleware never clears the request, so one test's request
        # (and its rolled-back user) would otherwise leak into the next test's
        # setup and break HorillaModel.save's created_by/modified_by lookup.
        _thread_locals.request = None
        self.addCleanup(setattr, _thread_locals, "request", None)
        self.hr_user, self.hr_emp = make_employee("hr")
        # HR holds view as well as change, as a real HR group would; without
        # view the list only shows the user's own subordinates.
        self.hr_user.user_permissions.add(
            *Permission.objects.filter(
                codename__in=["view_availableleave", "change_availableleave"],
                content_type__app_label="leave",
            )
        )
        self.mgr_user, self.mgr_emp = make_employee("mgr")
        _, self.worker = make_employee("worker", with_user=False)
        # Creating an Employee already creates its work-information row.
        EmployeeWorkInformation.objects.update_or_create(
            employee_id=self.worker, defaults={"reporting_manager_id": self.mgr_emp}
        )
        self.leave_type = LeaveType.objects.create(name="Annual", total_days=24)
        self.balance = AvailableLeave.objects.create(
            leave_type_id=self.leave_type,
            employee_id=self.worker,
            available_days=5,
            carryforward_days=2,
        )
        self.url = reverse("available-leave-update", args=[self.balance.id])

    def post(self, user, **data):
        client = Client()
        client.force_login(user)
        # The view is HTMX-only (hx_request_required).
        return client.post(self.url, data, HTTP_HX_REQUEST="true")

    def reload(self):
        self.balance.refresh_from_db()
        return self.balance

    def test_manager_without_permission_cannot_edit(self):
        # Precondition: the user really is a reporting manager, the case that
        # used to be let through by manager_can_enter.
        self.assertTrue(
            EmployeeWorkInformation.objects.filter(
                reporting_manager_id=self.mgr_emp
            ).exists()
        )
        response = self.post(
            self.mgr_user,
            available_days=99,
            carryforward_days=99,
            history_description="should be refused",
        )
        self.assertNotContains(response, "history_description")
        self.assertEqual(self.reload().available_days, 5)
        self.assertEqual(self.balance.carryforward_days, 2)

    def test_user_with_permission_can_edit(self):
        response = self.post(
            self.hr_user,
            available_days=7.5,
            carryforward_days=3,
            history_description="Corrected after review",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.reload().available_days, 7.5)
        self.assertEqual(self.balance.carryforward_days, 3)
        self.assertEqual(self.balance.total_leave_days, 10.5)

    def test_missing_reason_is_rejected(self):
        response = self.post(self.hr_user, available_days=7.5, carryforward_days=3)
        self.assertContains(response, "This field is required")
        self.assertEqual(self.reload().available_days, 5)

    def test_blank_reason_is_rejected(self):
        self.post(
            self.hr_user,
            available_days=7.5,
            carryforward_days=3,
            history_description="   ",
        )
        self.assertEqual(self.reload().available_days, 5)

    def test_reason_is_stored_in_history(self):
        self.post(
            self.hr_user,
            available_days=7.5,
            carryforward_days=3,
            history_description="Corrected after review",
        )
        latest = self.balance.history.first()
        self.assertEqual(latest.history_description, "Corrected after review")
        self.assertEqual(latest.history_user_id, self.hr_user.id)

    def test_edit_does_not_deactivate_the_record(self):
        # The old form included is_active but never rendered it, so each edit
        # posted it as unchecked and switched the record off.
        self.post(
            self.hr_user,
            available_days=7.5,
            carryforward_days=3,
            history_description="Corrected after review",
        )
        self.assertTrue(self.reload().is_active)

    def test_edit_button_hidden_from_manager_without_permission(self):
        client = Client()
        client.force_login(self.mgr_user)
        response = client.get(
            reverse("assign-filter") + "?field=leave_type_id", HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.worker.employee_first_name)
        self.assertNotContains(response, self.url)

    def test_edit_button_shown_to_user_with_permission(self):
        client = Client()
        client.force_login(self.hr_user)
        response = client.get(
            reverse("assign-filter") + "?field=leave_type_id", HTTP_HX_REQUEST="true"
        )
        self.assertContains(response, self.url)
