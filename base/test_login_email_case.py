"""
Tests for case-insensitive login and password reset, and lowercase storage
of login usernames (base/views.py, base/forms.py, horilla_api auth,
employee/models.py, employee/forms.py, employee/methods/methods.py).

Synthetic data only. The test runner never touches the real database.
"""

import pandas as pd
from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase
from django.urls import reverse

from base.forms import ChangeUsernameForm
from base.models import Company
from employee.forms import EmployeeForm
from employee.methods.methods import (
    bulk_create_employee_import,
    bulk_create_user_import,
    process_employee_records,
)
from employee.models import Employee
from horilla.horilla_middlewares import _thread_locals
from leave.tests import add_missing_user_column

PHONE = "0700000000"


def make_employee(email, first_name="Test"):
    """Create an Employee and let Employee.save create its login account."""
    return Employee.objects.create(
        employee_first_name=first_name, email=email, phone=PHONE
    )


def import_frame(email, badge_id="IMP001", first_name="Imported"):
    """One import row; empty template columns arrive as NaN, as from Excel."""
    empty = float("nan")
    return pd.DataFrame(
        [
            {
                "Badge ID": badge_id,
                "First Name": first_name,
                "Last Name": "Person",
                "Email": email,
                "Phone": "0711111111",
                "Gender": "male",
                "Company": empty,
                "Basic Salary": empty,
                "Salary Hour": empty,
                "Date Joining": empty,
                "Contract End Date": empty,
            }
        ]
    )


class LoginEmailCaseTests(TestCase):
    @classmethod
    def setUpClass(cls):
        add_missing_user_column()
        super().setUpClass()

    def setUp(self):
        _thread_locals.request = None
        self.addCleanup(setattr, _thread_locals, "request", None)
        # The API login reads the employee's company; without one it fails
        # on an unrelated unbound variable.
        Company.objects.create(
            company="Test Co",
            hq=True,
            address="-",
            country="-",
            state="-",
            city="-",
            zip="-",
        )
        self.employee = make_employee("Testuser1@Example.invalid")
        self.user = self.employee.employee_user_id

    # Writes

    def test_new_employee_login_is_stored_lowercase(self):
        self.assertEqual(self.user.username, "testuser1@example.invalid")
        self.assertEqual(self.user.email, "testuser1@example.invalid")

    def test_profile_edit_stores_new_login_lowercase(self):
        form = EmployeeForm(
            data={
                "employee_first_name": "Test",
                "email": "  New.Address@Example.invalid ",
                "phone": PHONE,
                "gender": "male",
            },
            instance=self.employee,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "new.address@example.invalid")
        self.assertEqual(self.user.email, "new.address@example.invalid")

    def test_new_employee_with_case_variant_of_existing_login_is_refused(self):
        form = EmployeeForm(
            data={
                "employee_first_name": "Other",
                "email": "TESTUSER1@example.invalid",
                "phone": PHONE,
                "gender": "male",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)
        self.assertEqual(
            User.objects.filter(
                username__iexact="testuser1@example.invalid"
            ).count(),
            1,
        )

    def test_username_change_refuses_case_variant_and_stores_lowercase(self):
        other = make_employee("other@example.invalid", "Other").employee_user_id
        form = ChangeUsernameForm(
            other,
            data={
                "old_username": other.username,
                "username": "TestUser1@example.invalid",
                "password": PHONE,
            },
        )
        self.assertFalse(form.is_valid())

        form = ChangeUsernameForm(
            other,
            data={
                "old_username": other.username,
                "username": " Fresh@Example.invalid",
                "password": PHONE,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["username"], "fresh@example.invalid")

    def test_import_stores_login_lowercase(self):
        create_list, _, errors, created, _ = process_employee_records(
            import_frame("Mixed.Case@Example.invalid")
        )
        self.assertEqual((created, errors), (1, []))
        bulk_create_user_import(create_list)
        bulk_create_employee_import(create_list)
        user = User.objects.get(username="mixed.case@example.invalid")
        self.assertEqual(user.email, "mixed.case@example.invalid")
        self.assertEqual(user.employee_get.badge_id, "IMP001")

    def test_import_does_not_duplicate_a_case_variant_login(self):
        # A legacy account stored in mixed case, as before the clean-up.
        User.objects.create_user(username="Legacy@Example.invalid", password=PHONE)
        create_list, _, errors, created, _ = process_employee_records(
            import_frame("legacy@example.invalid")
        )
        self.assertEqual(created, 0)
        self.assertEqual(len(errors), 1)
        # Even if a row got past validation, the write itself must not
        # create a second account.
        bulk_create_user_import([{"Email": "legacy@example.invalid", "Phone": ""}])
        self.assertEqual(
            User.objects.filter(username__iexact="legacy@example.invalid").count(), 1
        )

    # Lookups

    def test_web_login_with_different_case(self):
        response = Client().post(
            reverse("login"),
            {"username": "TESTUSER1@example.invalid", "password": PHONE},
        )
        self.assertRedirects(response, "/", fetch_redirect_response=False)

    def test_api_login_with_different_case(self):
        response = Client().post(
            "/api/auth/login/",
            {"username": "TestUser1@example.invalid", "password": PHONE},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("access", response.json())

    def test_password_reset_with_different_case(self):
        response = Client().post(
            reverse("forgot-password"), {"email": "TestUser1@Example.invalid"}
        )
        self.assertRedirects(
            response, reverse("reset-send-success"), fetch_redirect_response=False
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["Testuser1@Example.invalid"])

    def test_password_reset_for_legacy_mixed_case_login(self):
        User.objects.filter(pk=self.user.pk).update(
            username="Testuser1@Example.invalid"
        )
        response = Client().post(
            reverse("forgot-password"), {"email": "testuser1@example.invalid"}
        )
        self.assertRedirects(
            response, reverse("reset-send-success"), fetch_redirect_response=False
        )
        self.assertEqual(len(mail.outbox), 1)

    def test_employee_password_reset_with_different_case(self):
        response = Client().post(
            reverse("employee-reset-password"),
            {"email": "TESTUSER1@EXAMPLE.INVALID"},
            HTTP_REFERER="/",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)

    def test_password_reset_unknown_address_keeps_existing_message(self):
        response = Client().post(
            reverse("forgot-password"), {"email": "nobody@example.invalid"}, follow=True
        )
        self.assertEqual(len(mail.outbox), 0)
        self.assertContains(response, "No user found with the username")
