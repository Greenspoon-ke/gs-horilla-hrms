"""
Tests for Multiple Approvals (two-approver leave): steps are approved in
order, the request and the balance change only after the last step, nobody
outside the chain approves or rejects it (except a superuser), a person
appears once in a chain and never on their own request, and HR, deputies and
the approver whose turn it is see the request.

Synthetic data only. The test runner never touches the real database.
"""

from datetime import date, timedelta
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.test import Client, TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from base.models import (
    Company,
    Department,
    MultipleApprovalCondition,
    MultipleApprovalManagers,
)
from employee.models import Employee, EmployeeWorkInformation
from gs_custom.models import DepartmentDeputy
from horilla.horilla_middlewares import _thread_locals
from leave.models import (
    AvailableLeave,
    LeaveRequest,
    LeaveRequestConditionApproval,
    LeaveType,
)
from leave.tests import add_missing_user_column, make_employee


class TwoApproverTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        add_missing_user_column()
        super().setUpClass()

    def setUp(self):
        self.reset_thread()
        self.addCleanup(self.reset_thread)
        patcher = mock.patch("leave.views.LeaveMailSendThread")
        patcher.start()
        self.addCleanup(patcher.stop)

        self.company = Company.objects.create(
            company="Synthetic Co",
            address="x",
            country="x",
            state="x",
            city="x",
            zip="0",
        )
        # Department.save passes save() kwargs on to clean(), so no objects.create.
        self.dept = Department(department="Warehouse")
        self.dept.save()
        self.dept.company_id.add(self.company)

        self.lead_user, self.lead = make_employee("teamlead")
        self.senior_user, self.senior = make_employee("senior")
        self.top_user, self.top = make_employee("topboss")
        self.staff_user, self.staff = make_employee("requester")
        self.hr_user, self.hr = make_employee("hruser")
        self.hr_user.user_permissions.add(
            *Permission.objects.filter(
                codename__in=["view_leaverequest", "change_leaverequest"],
                content_type__app_label="leave",
            )
        )
        self.su = User.objects.create_superuser("root", password="not-a-real-password")
        _, su_emp = make_employee("rootemp", with_user=False)
        su_emp.employee_user_id = self.su
        su_emp.save()

        # requester -> teamlead -> senior -> topboss, all in Warehouse.
        for emp, mgr in [
            (self.staff, self.lead),
            (self.lead, self.senior),
            (self.senior, self.top),
        ]:
            EmployeeWorkInformation.objects.update_or_create(
                employee_id=emp,
                defaults={
                    "department_id": self.dept,
                    "company_id": self.company,
                    "reporting_manager_id": mgr,
                },
            )

        self.leave_type = LeaveType.objects.create(name="Annual", total_days=24)
        self.balances = {
            emp.pk: AvailableLeave.objects.create(
                employee_id=emp, leave_type_id=self.leave_type, available_days=10
            )
            for emp in (self.staff, self.lead, self.senior)
        }

        # The configuration HR will use: every request in the department, the
        # requester's reporting manager first, then a named senior approver.
        condition = MultipleApprovalCondition.objects.create(
            department=self.dept,
            condition_field="requested_days",
            condition_operator="ge",
            condition_value="0",
            company_id=self.company,
        )
        MultipleApprovalManagers.objects.create(
            condition_id=condition, sequence=1, reporting_manager="reporting_manager_id"
        )
        MultipleApprovalManagers.objects.create(
            condition_id=condition, sequence=2, employee_id=self.senior.id
        )

    @staticmethod
    def reset_thread():
        _thread_locals.request = None

    def make_request(self, employee):
        start = date.today() + timedelta(days=7)
        leave = LeaveRequest.objects.create(
            # Fresh instance: make_employee's caches its work info before the update.
            employee_id=Employee.objects.get(pk=employee.pk),
            leave_type_id=self.leave_type,
            start_date=start,
            end_date=start,
            description="synthetic",
        )
        self.reset_thread()
        return leave

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def api_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def approve(self, user, leave):
        response = self.client_for(user).get(
            reverse("request-approve", args=[leave.id]), HTTP_REFERER="/"
        )
        self.reset_thread()
        return response

    def reject(self, user, leave):
        response = self.client_for(user).post(
            reverse("request-cancel", args=[leave.id]), {"reason": "no"}
        )
        self.reset_thread()
        return response

    def get(self, user, name):
        response = self.client_for(user).get(reverse(name), HTTP_HX_REQUEST="true")
        self.reset_thread()
        return response

    def steps(self, leave):
        return list(
            LeaveRequestConditionApproval.objects.filter(leave_request_id=leave)
            .order_by("sequence")
            .values_list("manager_id", "is_approved", "is_rejected")
        )

    def status(self, leave):
        leave.refresh_from_db()
        return leave.status

    def days(self, employee):
        balance = self.balances[employee.pk]
        balance.refresh_from_db()
        return balance.available_days + balance.carryforward_days


class ChainBuildTests(TwoApproverTestCase):
    def test_reporting_manager_then_named_approver(self):
        leave = self.make_request(self.staff)
        self.assertEqual(
            self.steps(leave),
            [(self.lead.id, False, False), (self.senior.id, False, False)],
        )

    def test_lead_who_is_also_second_approver_appears_once(self):
        leave = self.make_request(self.lead)
        self.assertEqual(self.steps(leave), [(self.senior.id, False, False)])

    def test_approvers_own_request_goes_to_the_rest_of_the_chain(self):
        # The senior's reporting manager is topboss; the senior's own step is dropped.
        leave = self.make_request(self.senior)
        self.assertEqual(self.steps(leave), [(self.top.id, False, False)])


class WebApproveTests(TwoApproverTestCase):
    def test_steps_in_order_and_final_only_at_the_end(self):
        leave = self.make_request(self.staff)
        self.approve(self.senior_user, leave)
        self.assertEqual(self.status(leave), "requested")
        self.assertEqual(self.steps(leave)[1], (self.senior.id, False, False))
        self.approve(self.lead_user, leave)
        self.assertEqual(self.status(leave), "requested")
        self.assertEqual(self.days(self.staff), 10)
        self.approve(self.senior_user, leave)
        self.assertEqual(self.status(leave), "approved")
        self.assertEqual(self.days(self.staff), 9)

    def test_second_click_by_same_approver_changes_nothing(self):
        leave = self.make_request(self.staff)
        self.approve(self.lead_user, leave)
        self.approve(self.lead_user, leave)
        self.assertEqual(self.status(leave), "requested")
        self.assertEqual(self.days(self.staff), 10)

    def test_outside_the_chain_is_refused_not_a_500(self):
        leave = self.make_request(self.staff)
        # HR passes the object check and is stopped by the chain (redirect with
        # a message); topboss manages nobody here and is stopped before it.
        self.assertEqual(self.approve(self.hr_user, leave).status_code, 302)
        self.assertEqual(self.approve(self.top_user, leave).status_code, 403)
        self.assertEqual(self.status(leave), "requested")
        self.assertFalse(any(step[1] for step in self.steps(leave)))

    def test_single_step_chain_approves_at_once(self):
        leave = self.make_request(self.lead)
        self.approve(self.senior_user, leave)
        self.assertEqual(self.status(leave), "approved")
        self.assertEqual(self.days(self.lead), 9)

    def test_superuser_approves_every_step(self):
        leave = self.make_request(self.staff)
        self.approve(self.su, leave)
        self.assertEqual(self.status(leave), "approved")
        self.assertTrue(all(step[1] for step in self.steps(leave)))


class WebRejectTests(TwoApproverTestCase):
    def test_any_approver_rejects_the_whole_request(self):
        for user in ("lead_user", "senior_user"):
            with self.subTest(user=user):
                leave = self.make_request(self.staff)
                self.reject(getattr(self, user), leave)
                self.assertEqual(self.status(leave), "rejected")
                leave.delete()

    def test_outside_the_chain_cannot_reject(self):
        leave = self.make_request(self.staff)
        self.assertEqual(self.reject(self.hr_user, leave).status_code, 200)
        self.assertEqual(self.reject(self.top_user, leave).status_code, 403)
        self.assertEqual(self.status(leave), "requested")


class ApiTests(TwoApproverTestCase):
    def test_api_approve_follows_the_chain(self):
        leave = self.make_request(self.staff)
        url = f"/api/leave/approve/{leave.id}/"
        self.assertEqual(self.api_for(self.senior_user).put(url).status_code, 403)
        self.assertEqual(self.api_for(self.hr_user).put(url).status_code, 403)
        self.assertEqual(self.api_for(self.lead_user).put(url).status_code, 200)
        self.assertEqual(self.status(leave), "requested")
        self.assertEqual(self.api_for(self.senior_user).put(url).status_code, 200)
        self.assertEqual(self.status(leave), "approved")
        self.assertEqual(self.days(self.staff), 9)

    def test_api_bulk_approve_no_longer_skips_the_chain(self):
        leave = self.make_request(self.staff)
        url = "/api/leave/request-bulk-action/"
        body = {"leave_request_id": [leave.id]}
        response = self.api_for(self.hr_user).put(url, body)
        self.assertEqual(response.json(), {"skipped": [leave.id]})
        self.api_for(self.lead_user).put(url, body)
        self.assertEqual(self.status(leave), "requested")
        self.api_for(self.senior_user).put(url, body)
        self.assertEqual(self.status(leave), "approved")

    def test_api_reject_needs_an_approver_in_the_chain(self):
        leave = self.make_request(self.staff)
        url = f"/api/leave/reject/{leave.id}/"
        self.assertEqual(self.api_for(self.hr_user).put(url).status_code, 403)
        self.assertEqual(self.status(leave), "requested")
        self.assertEqual(self.api_for(self.senior_user).put(url).status_code, 200)
        self.assertEqual(self.status(leave), "rejected")


class VisibilityTests(TwoApproverTestCase):
    def test_hr_sees_pending_request_without_an_approve_button(self):
        leave = self.make_request(self.staff)
        response = self.get(self.hr_user, "request-filter")
        self.assertContains(response, "requester")
        self.assertNotContains(response, reverse("request-approve", args=[leave.id]))

    def test_approver_sees_it_on_their_turn_with_the_button(self):
        leave = self.make_request(self.staff)
        approve_url = reverse("request-approve", args=[leave.id])
        self.assertContains(self.get(self.lead_user, "request-filter"), approve_url)
        self.assertNotContains(
            self.get(self.senior_user, "request-filter"), "requester"
        )
        self.approve(self.lead_user, leave)
        self.assertContains(self.get(self.senior_user, "request-filter"), approve_url)

    def test_deputy_sees_pending_request_in_their_department(self):
        deputy_user, deputy = make_employee("deputy")
        DepartmentDeputy.objects.create(employee=deputy, department=self.dept)
        self.make_request(self.staff)
        self.assertContains(self.get(deputy_user, "request-filter"), "requester")

    def test_rejected_request_reappears_for_the_reporting_manager(self):
        # A fixed-approver chain leaves the requester's own manager outside it.
        MultipleApprovalManagers.objects.filter(
            reporting_manager__isnull=False
        ).delete()
        leave = self.make_request(self.staff)
        self.assertEqual(self.steps(leave), [(self.senior.id, False, False)])
        self.assertNotContains(self.get(self.lead_user, "request-filter"), "requester")
        self.reject(self.senior_user, leave)
        self.assertEqual(self.status(leave), "rejected")
        self.assertContains(self.get(self.lead_user, "request-filter"), "requester")

    def test_dashboard_shows_the_request_to_whoever_is_next(self):
        leave = self.make_request(self.staff)
        self.assertContains(
            self.get(self.lead_user, "leave-request-and-approve"), "requester"
        )
        self.assertNotContains(
            self.get(self.senior_user, "leave-request-and-approve"), "requester"
        )
        self.approve(self.lead_user, leave)
        self.assertNotContains(
            self.get(self.lead_user, "leave-request-and-approve"), "requester"
        )
        self.assertContains(
            self.get(self.senior_user, "leave-request-and-approve"), "requester"
        )
