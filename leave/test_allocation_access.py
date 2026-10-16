"""
Tests for who may use leave allocation requests and act on leave requests:
the allocation page is for HR and managers only, managers act on their own
reportees only (audit S1), and a manager's or HR's grant applies at once.

Synthetic data only. The test runner never touches the real database.
"""

from datetime import date, timedelta

from django.contrib.auth.models import Group, Permission
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from employee.models import EmployeeWorkInformation
from horilla.horilla_middlewares import _thread_locals
from leave.methods import HR_LEAVE_GROUP
from leave.models import (
    AvailableLeave,
    LeaveAllocationRequest,
    LeaveallocationrequestComment,
    LeaveRequest,
    LeaveType,
)
from leave.sidebar import allocation_accessibility
from leave.tests import add_missing_user_column, make_employee
from notifications.models import Notification

HR_PERMS = [
    "view_leaveallocationrequest",
    "add_leaveallocationrequest",
    "change_leaveallocationrequest",
    "view_availableleave",
    "change_availableleave",
]


class AllocationAccessTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        add_missing_user_column()
        super().setUpClass()

    def setUp(self):
        _thread_locals.request = None
        self.addCleanup(setattr, _thread_locals, "request", None)
        self.hr_user, self.hr = make_employee("hr")
        group = Group.objects.create(name=HR_LEAVE_GROUP)
        group.permissions.add(
            *Permission.objects.filter(
                codename__in=HR_PERMS, content_type__app_label="leave"
            )
        )
        self.hr_user.groups.add(group)
        self.mgr_user, self.mgr = make_employee("mgr")
        self.other_mgr_user, self.other_mgr = make_employee("othermgr")
        self.staff_user, self.staff = make_employee("staff")
        _, self.outsider = make_employee("outsider", with_user=False)
        self.report_to(self.staff, self.mgr)
        self.report_to(self.outsider, self.other_mgr)
        self.leave_type = LeaveType.objects.create(name="Sick Leave", total_days=14)
        self.staff_balance = self.balance(self.staff, 5)
        self.outsider_balance = self.balance(self.outsider, 5)
        self.mgr_balance = self.balance(self.mgr, 5)
        self.own_alloc = self.allocation(self.staff)
        self.other_alloc = self.allocation(self.outsider)

    @staticmethod
    def report_to(employee, manager):
        EmployeeWorkInformation.objects.update_or_create(
            employee_id=employee, defaults={"reporting_manager_id": manager}
        )

    def balance(self, employee, days):
        return AvailableLeave.objects.create(
            employee_id=employee, leave_type_id=self.leave_type, available_days=days
        )

    def allocation(self, employee, days=2):
        return LeaveAllocationRequest.objects.create(
            employee_id=employee,
            leave_type_id=self.leave_type,
            requested_days=days,
            description="synthetic",
        )

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def api_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def days(self, row):
        row.refresh_from_db()
        return row.available_days

    def status(self, obj):
        obj.refresh_from_db()
        return obj.status


class SidebarTests(AllocationAccessTestCase):
    def visible(self, user):
        request = RequestFactory().get("/")
        request.user = user
        return allocation_accessibility(request, {}, [])

    def test_menu_only_for_managers_and_hr(self):
        self.assertFalse(self.visible(self.staff_user))
        self.assertTrue(self.visible(self.mgr_user))
        self.assertTrue(self.visible(self.hr_user))


class StaffTests(AllocationAccessTestCase):
    def test_every_web_route_refuses_staff(self):
        own, other = self.own_alloc.id, self.other_alloc.id
        hx = {"HTTP_HX_REQUEST": "true"}
        routes = [
            ("get", reverse("leave-allocation-request-view"), {}),
            ("get", reverse("leave-allocation-request-filter"), hx),
            ("get", reverse("leave-allocation-request-single-view", args=[own]), hx),
            ("get", reverse("leave-allocation-request-create"), hx),
            ("post", reverse("leave-allocation-request-create"), hx),
            ("get", reverse("leave-allocation-request-update", args=[own]), hx),
            ("post", reverse("leave-allocation-request-approve", args=[other]), hx),
            ("get", reverse("leave-allocation-request-reject", args=[other]), hx),
            ("post", reverse("leave-allocation-request-delete", args=[own]), hx),
            ("get", reverse("allocation-request-add-comment", args=[own]), hx),
            ("get", reverse("allocation-request-view-comment", args=[own]), hx),
        ]
        client = self.client_for(self.staff_user)
        for method, url, extra in routes:
            with self.subTest(url=url, method=method):
                response = getattr(client, method)(url, **extra)
                self.assertEqual(response.status_code, 403)
        self.assertTrue(LeaveAllocationRequest.objects.filter(id=own).exists())
        self.assertEqual(self.status(self.other_alloc), "requested")

    def test_crafted_post_for_another_employee_creates_nothing(self):
        before = LeaveAllocationRequest.objects.count()
        self.client_for(self.staff_user).post(
            reverse("leave-allocation-request-create"),
            {
                "employee_id": self.outsider.id,
                "leave_type_id": self.leave_type.id,
                "requested_days": 9,
                "description": "crafted",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(LeaveAllocationRequest.objects.count(), before)

    def test_api_refuses_staff(self):
        api = self.api_for(self.staff_user)
        body = {
            "employee_id": self.outsider.id,
            "leave_type_id": self.leave_type.id,
            "requested_days": 9,
            "description": "crafted",
        }
        responses = [
            api.post("/api/leave/allocation-request/", body, format="json"),
            api.post("/api/leave/user-allocation-request/", body, format="json"),
            api.put(f"/api/leave/user-allocation-request/{self.own_alloc.id}/", body),
            api.delete(f"/api/leave/user-allocation-request/{self.own_alloc.id}/"),
            api.put(f"/api/leave/allocation-approve/{self.other_alloc.id}/"),
            api.get(f"/api/leave/allocation-request/{self.other_alloc.id}/"),
        ]
        self.assertEqual([r.status_code for r in responses], [403] * len(responses))
        self.assertEqual(LeaveAllocationRequest.objects.count(), 2)
        self.assertEqual(self.status(self.other_alloc), "requested")


class ManagerTests(AllocationAccessTestCase):
    def create_as(self, user, employee, days=3):
        return self.client_for(user).post(
            reverse("leave-allocation-request-create"),
            {
                "employee_id": employee.id,
                "leave_type_id": self.leave_type.id,
                "requested_days": days,
                "description": "extra days",
            },
            HTTP_HX_REQUEST="true",
        )

    def test_grant_for_own_reportee_applies_at_once_and_notifies(self):
        self.create_as(self.mgr_user, self.staff, 3)
        created = LeaveAllocationRequest.objects.latest("id")
        self.assertEqual(created.employee_id, self.staff)
        self.assertEqual(created.status, "approved")
        self.assertEqual(self.days(self.staff_balance), 8)
        self.assertTrue(
            Notification.objects.filter(recipient=self.staff_user).exists()
        )
        # HR decision Q7: HR-group members hear about direct grants.
        self.assertTrue(Notification.objects.filter(recipient=self.hr_user).exists())

    def test_cannot_create_for_a_non_reportee_or_self(self):
        for employee in (self.outsider, self.mgr):
            with self.subTest(employee=employee.employee_first_name):
                before = LeaveAllocationRequest.objects.count()
                self.create_as(self.mgr_user, employee)
                self.assertEqual(LeaveAllocationRequest.objects.count(), before)
        self.assertEqual(self.days(self.outsider_balance), 5)
        self.assertEqual(self.days(self.mgr_balance), 5)

    def test_sees_only_own_reportees(self):
        response = self.client_for(self.mgr_user).get(
            reverse("leave-allocation-request-filter"), HTTP_HX_REQUEST="true"
        )
        self.assertContains(response, self.staff.employee_first_name)
        self.assertNotContains(response, self.outsider.employee_first_name)
        single = self.client_for(self.mgr_user).get(
            reverse("leave-allocation-request-single-view", args=[self.other_alloc.id])
            + "?instances_ids=[]",
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(single.status_code, 403)

    def test_approves_own_reportee_post_only(self):
        url = reverse("leave-allocation-request-approve", args=[self.own_alloc.id])
        client = self.client_for(self.mgr_user)
        # MethodNotAllowedMiddleware renders every 405 as the 405.html page.
        self.assertTemplateUsed(client.get(url), "405.html")
        self.assertEqual(self.status(self.own_alloc), "requested")
        client.post(url, HTTP_HX_REQUEST="true")
        self.assertEqual(self.status(self.own_alloc), "approved")
        self.assertEqual(self.days(self.staff_balance), 7)
        # A second click grants nothing more.
        client.post(url, HTTP_HX_REQUEST="true")
        self.assertEqual(self.days(self.staff_balance), 7)

    def test_cannot_act_on_a_non_reportees_allocation(self):
        other = self.other_alloc.id
        client = self.client_for(self.mgr_user)
        approve = client.post(
            reverse("leave-allocation-request-approve", args=[other]),
            HTTP_HX_REQUEST="true",
        )
        reject = client.post(
            reverse("leave-allocation-request-reject", args=[other]),
            {"reason": "no"},
            HTTP_HX_REQUEST="true",
        )
        comment = client.post(
            reverse("allocation-request-add-comment", args=[other]),
            {"comment": "crafted"},
            HTTP_HX_REQUEST="true",
        )
        api = self.api_for(self.mgr_user)
        api_approve = api.put(f"/api/leave/allocation-approve/{other}/")
        api_reject = api.put(f"/api/leave/allocation-reject/{other}/")
        api_get = api.get(f"/api/leave/allocation-request/{other}/")
        self.assertEqual(
            [
                r.status_code
                for r in (approve, reject, comment, api_approve, api_reject, api_get)
            ],
            [403] * 6,
        )
        self.assertEqual(self.status(self.other_alloc), "requested")
        self.assertEqual(self.days(self.outsider_balance), 5)
        self.assertFalse(LeaveallocationrequestComment.objects.exists())

    def test_api_create_scope_and_direct_grant(self):
        api = self.api_for(self.mgr_user)
        body = {
            "leave_type_id": self.leave_type.id,
            "requested_days": 2,
            "description": "extra days",
        }
        refused = api.post(
            "/api/leave/allocation-request/",
            {**body, "employee_id": self.outsider.id},
            format="json",
        )
        granted = api.post(
            "/api/leave/allocation-request/",
            {**body, "employee_id": self.staff.id},
            format="json",
        )
        self.assertEqual((refused.status_code, granted.status_code), (403, 201))
        created = LeaveAllocationRequest.objects.latest("id")
        self.assertEqual(created.status, "approved")
        self.assertEqual(created.created_by, self.mgr_user)
        self.assertEqual(self.days(self.staff_balance), 7)
        self.assertEqual(self.days(self.outsider_balance), 5)

    def test_rejecting_a_grant_on_a_negative_balance_goes_further_negative(self):
        AvailableLeave.objects.filter(pk=self.staff_balance.pk).update(
            available_days=-1
        )
        LeaveAllocationRequest.objects.filter(pk=self.own_alloc.pk).update(
            status="approved"
        )
        self.client_for(self.mgr_user).post(
            reverse("leave-allocation-request-reject", args=[self.own_alloc.id]),
            {"reason": "granted by mistake"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(self.status(self.own_alloc), "rejected")
        # No floor at 0: the debt stays visible (HR decision 3).
        self.assertEqual(self.days(self.staff_balance), -3)


class HrTests(AllocationAccessTestCase):
    def test_sees_everything(self):
        response = self.client_for(self.hr_user).get(
            reverse("leave-allocation-request-filter"), HTTP_HX_REQUEST="true"
        )
        self.assertContains(response, self.staff.employee_first_name)
        self.assertContains(response, self.outsider.employee_first_name)

    def test_grants_to_anyone_at_once_but_not_to_self(self):
        client = self.client_for(self.hr_user)
        for employee in (self.outsider, self.hr):
            client.post(
                reverse("leave-allocation-request-create"),
                {
                    "employee_id": employee.id,
                    "leave_type_id": self.leave_type.id,
                    "requested_days": 4,
                    "description": "extra days",
                },
                HTTP_HX_REQUEST="true",
            )
        outsider_grant = LeaveAllocationRequest.objects.get(
            employee_id=self.outsider, description="extra days"
        )
        own = LeaveAllocationRequest.objects.get(employee_id=self.hr)
        self.assertEqual(outsider_grant.status, "approved")
        self.assertEqual(self.days(self.outsider_balance), 9)
        self.assertEqual(own.status, "requested")

    def test_approves_any_allocation(self):
        self.client_for(self.hr_user).post(
            reverse("leave-allocation-request-approve", args=[self.other_alloc.id]),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(self.status(self.other_alloc), "approved")
        self.assertEqual(self.days(self.outsider_balance), 7)


class LeaveRequestObjectCheckTests(AllocationAccessTestCase):
    """Audit S1: a manager acts on their own reportees' leave requests only."""

    def setUp(self):
        super().setUp()
        start = date.today() + timedelta(days=30)
        self.own_request = self.leave_request(self.staff, start)
        self.other_request = self.leave_request(self.outsider, start)

    def leave_request(self, employee, start):
        leave_request = LeaveRequest(
            employee_id=employee,
            leave_type_id=self.leave_type,
            start_date=start,
            end_date=start,
            description="synthetic",
        )
        leave_request.save()
        return leave_request

    def test_web_approve_and_reject(self):
        client = self.client_for(self.mgr_user)
        refused = client.get(reverse("request-approve", args=[self.other_request.id]))
        refused_reject = client.post(
            reverse("request-cancel", args=[self.other_request.id]),
            {"reason": "no"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual((refused.status_code, refused_reject.status_code), (403, 403))
        self.assertEqual(self.status(self.other_request), "requested")
        client.get(reverse("request-approve", args=[self.own_request.id]))
        self.assertEqual(self.status(self.own_request), "approved")

    def test_web_bulk_approve_skips_non_reportees(self):
        self.client_for(self.mgr_user).post(
            reverse("leave-requests-bulk-approve"),
            {"ids": [self.own_request.id, self.other_request.id]},
        )
        self.assertEqual(self.status(self.own_request), "approved")
        self.assertEqual(self.status(self.other_request), "requested")
        self.assertEqual(self.days(self.outsider_balance), 5)

    def test_web_bulk_reject_skips_non_reportees(self):
        self.client_for(self.mgr_user).post(
            reverse("leave-requests-bulk-reject"),
            {"request_ids": [self.own_request.id, self.other_request.id], "reason": "x"},
        )
        self.assertEqual(self.status(self.own_request), "rejected")
        self.assertEqual(self.status(self.other_request), "requested")

    def test_api_approve_reject_and_bulk(self):
        api = self.api_for(self.mgr_user)
        approve = api.put(f"/api/leave/approve/{self.other_request.id}/")
        reject = api.put(f"/api/leave/reject/{self.other_request.id}/")
        self.assertEqual((approve.status_code, reject.status_code), (403, 403))
        bulk = api.put(
            "/api/leave/request-bulk-action/",
            {"leave_request_id": [self.own_request.id, self.other_request.id]},
        )
        self.assertEqual(bulk.json()["skipped"], [self.other_request.id])
        self.assertEqual(self.status(self.own_request), "approved")
        self.assertEqual(self.status(self.other_request), "requested")

    def test_api_reject_refunds_the_employee_not_the_approver(self):
        LeaveRequest.objects.filter(pk=self.own_request.pk).update(
            status="approved", approved_available_days=1
        )
        response = self.api_for(self.mgr_user).put(
            f"/api/leave/reject/{self.own_request.id}/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.days(self.staff_balance), 6)
        self.assertEqual(self.days(self.mgr_balance), 5)
