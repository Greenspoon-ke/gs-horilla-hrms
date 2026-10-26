"""
Permission and method checks on the endpoints that change data or expose pay
data (p1-03). Each endpoint is checked with the permission that already shows
its control in the UI: refused without it, allowed with it, no change on GET,
and no 500 on bad input.

Synthetic data only. The test runner never touches the real database.
"""

import json
from datetime import date

from django.contrib.auth.models import AnonymousUser, Permission
from django.template import engines
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from accessibility.models import DefaultAccessibility
from base.models import Company, Department, DynamicEmailConfiguration, JobPosition
from horilla.horilla_middlewares import _thread_locals
from horilla_audit.models import AuditTag
from leave.tests import add_missing_user_column, make_employee
from payroll.models.models import Payslip
from project.models import Project, Task, TimeSheet
from recruitment.models import Candidate, Recruitment

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def grant(user, *perms):
    for perm in perms:
        app_label, codename = perm.split(".")
        user.user_permissions.add(
            Permission.objects.get(content_type__app_label=app_label, codename=codename)
        )


def set_manager(employee, manager):
    work_info = employee.employee_work_info
    work_info.reporting_manager_id = manager
    work_info.save()


class PermissionTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        add_missing_user_column()
        super().setUpClass()

    def setUp(self):
        # ThreadLocalMiddleware never clears the request; reset it per test.
        _thread_locals.request = None
        self.addCleanup(setattr, _thread_locals, "request", None)

    def client_for(self, user=None):
        client = Client(raise_request_exception=False)
        if user:
            client.force_login(user)
        return client


class ProfileEditAccessTests(PermissionTestCase):
    def setUp(self):
        super().setUp()
        self.hr, _ = make_employee("hr")
        grant(self.hr, "employee.change_employee")
        self.manager, manager_employee = make_employee("manager")
        self.other_manager, _ = make_employee("othermanager")
        self.worker, self.worker_employee = make_employee("worker")
        set_manager(self.worker_employee, manager_employee)
        self.plain, self.plain_employee = make_employee("plain")
        self.profile_edit = DefaultAccessibility.objects.create(
            feature="profile_edit", filter={}
        )
        self.detailed = DefaultAccessibility.objects.create(
            feature="employee_detailed_view", filter={}
        )

    def url(self, employee, feature="profile_edit"):
        return (
            reverse("profile-edit-access", args=[employee.id]) + f"?feature={feature}"
        )

    def enabled(self, employee, accessibility=None):
        accessibility = accessibility or self.profile_edit
        return accessibility.employees.filter(id=employee.id).exists()

    def test_plain_employee_cannot_toggle(self):
        self.client_for(self.plain).post(self.url(self.plain_employee))
        self.assertFalse(self.enabled(self.plain_employee))

    def test_cannot_grant_other_features(self):
        client = self.client_for(self.hr)
        for feature in ("employee_detailed_view", "%E2%9C%93", ""):
            with self.subTest(feature=feature):
                response = client.post(self.url(self.worker_employee, feature))
                self.assertNotEqual(response.status_code, 500)
        self.assertFalse(self.enabled(self.worker_employee, self.detailed))

    def test_hr_toggles_on_and_off(self):
        client = self.client_for(self.hr)
        client.post(self.url(self.worker_employee))
        self.assertTrue(self.enabled(self.worker_employee))
        client.post(self.url(self.worker_employee))
        self.assertFalse(self.enabled(self.worker_employee))

    def test_reporting_manager_can_toggle_report(self):
        self.client_for(self.manager).post(self.url(self.worker_employee))
        self.assertTrue(self.enabled(self.worker_employee))

    def test_other_manager_refused(self):
        self.client_for(self.other_manager).post(self.url(self.worker_employee))
        self.assertFalse(self.enabled(self.worker_employee))

    def test_get_changes_nothing(self):
        response = self.client_for(self.hr).get(self.url(self.worker_employee))
        self.assertTemplateUsed(response, "405.html")
        self.assertFalse(self.enabled(self.worker_employee))

    def test_anonymous_changes_nothing(self):
        response = self.client_for().post(self.url(self.worker_employee))
        self.assertTrue(response["Location"].startswith(reverse("login")))
        self.assertFalse(self.enabled(self.worker_employee))

    def test_unknown_employee_refused(self):
        response = self.client_for(self.hr).post(
            reverse("profile-edit-access", args=[999999]) + "?feature=profile_edit"
        )
        self.assertNotEqual(response.status_code, 500)
        self.assertEqual(self.profile_edit.employees.count(), 0)


class PayslipStatusTests(PermissionTestCase):
    def setUp(self):
        super().setUp()
        self.payroll, _ = make_employee("payroll")
        grant(self.payroll, "payroll.change_payslip")
        self.plain, employee = make_employee("plain")
        self.payslip = Payslip.objects.create(
            employee_id=employee,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            pay_head_data={},
        )
        self.url = reverse("payslip-status-update-no-id")
        self.data = {"ids": json.dumps([self.payslip.id]), "status": "paid"}

    def status(self):
        self.payslip.refresh_from_db()
        return self.payslip.status

    def test_without_perm_nothing_changes(self):
        self.client_for(self.plain).post(self.url, self.data)
        self.assertEqual(self.status(), "draft")

    def test_with_perm_updates(self):
        response = self.client_for(self.payroll).post(self.url, self.data)
        self.assertEqual(self.status(), "paid")
        self.assertEqual(response.json()["type"], "success")

    def test_get_changes_nothing(self):
        response = self.client_for(self.payroll).get(self.url, self.data)
        self.assertTemplateUsed(response, "405.html")
        self.assertEqual(self.status(), "draft")

    def test_bad_ids_change_nothing(self):
        # Pre-existing: bad JSON raises. horilla.decorators.login_required
        # reads DEBUG from horilla.settings (.env), so locally and in CI it
        # re-raises (500); in production it renders went_wrong.html.
        self.client_for(self.payroll).post(
            self.url, {"ids": "not json", "status": "paid"}
        )
        self.assertEqual(self.status(), "draft")


class PayrollDashboardDataTests(PermissionTestCase):
    def setUp(self):
        super().setUp()
        # The dashboard page's own gate, without access to pay data.
        self.contracts_only, _ = make_employee("contracts")
        grant(self.contracts_only, "payroll.view_contract")
        self.payroll, _ = make_employee("payroll")
        grant(self.payroll, "payroll.view_contract", "payroll.view_payslip")
        self.plain, _ = make_employee("plain")
        self.period = {"period": "2026-01"}

    def test_payslip_details_needs_view_payslip(self):
        url = reverse("dashboard-payslip-details")
        for user in (self.plain, self.contracts_only):
            with self.subTest(user=user.username):
                response = self.client_for(user).get(url, self.period)
                self.assertNotIn(b"no_of_emp", response.content)

    def test_contract_ending_needs_view_contract(self):
        url = reverse("dashboard-contract-ending")
        response = self.client_for(self.plain).get(url, self.period)
        self.assertNotIn(b"contract_end", response.content)

    def test_payslip_export_needs_view_payslip(self):
        url = reverse("dashboard-export")
        data = {"start_date": "2026-01-01", "end_date": "2026-01-31"}
        for user in (self.plain, self.contracts_only):
            with self.subTest(user=user.username):
                response = self.client_for(user).post(url, data)
                self.assertNotEqual(response.get("Content-Type"), XLSX_TYPE)

    def test_holder_gets_same_keys(self):
        client = self.client_for(self.payroll)
        response = client.get(reverse("dashboard-payslip-details"), self.period)
        self.assertEqual(set(response.json()), {"no_of_emp", "total_amount"})
        response = client.get(reverse("dashboard-contract-ending"), self.period)
        self.assertEqual(set(response.json()), {"contract_end", "message"})

    def test_dashboard_hides_export_without_view_payslip(self):
        url = reverse("view-payroll-dashboard")
        export_url = reverse("dashboard-export")
        response = self.client_for(self.contracts_only).get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, export_url)
        response = self.client_for(self.payroll).get(url)
        self.assertContains(response, export_url)


class ReplacePrimaryMailTests(PermissionTestCase):
    def setUp(self):
        super().setUp()
        self.admin, _ = make_employee("mailadmin")
        grant(self.admin, "base.delete_dynamicemailconfiguration")
        self.plain, _ = make_employee("plain")
        # Each config needs its own company: save() deletes a second config
        # with the same company_id (including None).
        self.primary = DynamicEmailConfiguration.objects.create(
            username="a@example.invalid",
            company_id=Company.objects.create(company="Co A"),
            is_primary=True,
        )
        self.other = DynamicEmailConfiguration.objects.create(
            username="b@example.invalid",
            company_id=Company.objects.create(company="Co B"),
        )
        self.url = reverse("replace-primary-mail")

    def rows(self):
        return sorted(DynamicEmailConfiguration.objects.values_list("id", "is_primary"))

    def test_without_perm_nothing_changes(self):
        before = self.rows()
        self.client_for(self.plain).post(self.url, {"replace_mail": self.other.id})
        self.assertEqual(self.rows(), before)

    def test_get_changes_nothing(self):
        before = self.rows()
        response = self.client_for(self.admin).get(
            self.url, {"replace_mail": self.other.id}
        )
        self.assertTemplateUsed(response, "405.html")
        self.assertEqual(self.rows(), before)

    def test_with_perm_runs(self):
        response = self.client_for(self.admin).post(
            self.url, {"replace_mail": self.other.id}
        )
        self.assertRedirects(
            response, reverse("mail-server-conf"), fetch_redirect_response=False
        )
        # Existing behaviour, kept here: one row is deleted. Which one is the
        # bug tracked in p2-23 (it deletes the replacement just promoted).
        self.assertEqual(DynamicEmailConfiguration.objects.count(), 1)

    def test_unknown_id_changes_nothing(self):
        before = self.rows()
        # Pre-existing DoesNotExist (500 where DEBUG is on); nothing changes.
        self.client_for(self.admin).post(self.url, {"replace_mail": 999999})
        self.assertEqual(self.rows(), before)


class OnboardingBulkTests(PermissionTestCase):
    def setUp(self):
        super().setUp()
        self.recruiter, _ = make_employee("recruiter")
        grant(
            self.recruiter,
            "recruitment.change_recruitment",
            "onboarding.view_onboardingcandidate",
        )
        self.deleter, _ = make_employee("deleter")
        grant(
            self.deleter,
            "onboarding.delete_onboardingcandidate",
            "onboarding.view_onboardingcandidate",
        )
        self.viewer, _ = make_employee("viewer")
        grant(self.viewer, "onboarding.view_onboardingcandidate")
        # Department.save passes save() kwargs to clean(), so create() fails.
        department = Department(department="Kitchen")
        department.save()
        job = JobPosition(job_position="Cook", department_id=department)
        job.save()
        recruitment = Recruitment.objects.create(title="Cooks", job_position_id=job)
        recruitment.open_positions.add(job)
        stage = recruitment.stage_set.first()
        self.first, self.second = (
            Candidate.objects.create(
                name=name,
                email=f"{name}@example.invalid",
                recruitment_id=recruitment,
                job_position_id=job,
                stage_id=stage,
            )
            for name in ("first", "second")
        )
        self.status_url = reverse("offer-letter-bulk-status-update")
        self.delete_url = reverse("onboarding-candidate-bulk-delete")

    def offer_status(self, candidate):
        candidate.refresh_from_db()
        return candidate.offer_letter_status

    def remaining(self):
        return set(Candidate.objects.values_list("id", flat=True))

    def test_offer_status_get_changes_nothing(self):
        response = self.client_for(self.recruiter).get(
            self.status_url, {"ids": json.dumps([self.first.id]), "status": "sent"}
        )
        self.assertTemplateUsed(response, "405.html")
        self.assertEqual(self.offer_status(self.first), "not_sent")

    def test_offer_status_without_perm(self):
        self.client_for(self.viewer).post(
            self.status_url, {"ids": json.dumps([self.first.id]), "status": "sent"}
        )
        self.assertEqual(self.offer_status(self.first), "not_sent")

    def test_offer_status_updates_and_skips_missing(self):
        response = self.client_for(self.recruiter).post(
            self.status_url,
            {"ids": json.dumps([self.first.id, 999999]), "status": "sent"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.offer_status(self.first), "sent")
        self.assertEqual(self.offer_status(self.second), "not_sent")

    def test_offer_status_rejects_unknown_value(self):
        self.client_for(self.recruiter).post(
            self.status_url, {"ids": json.dumps([self.first.id]), "status": "hacked"}
        )
        self.assertEqual(self.offer_status(self.first), "not_sent")

    def test_delete_get_deletes_nothing(self):
        before = self.remaining()
        response = self.client_for(self.deleter).get(
            self.delete_url, {"ids": json.dumps([self.first.id])}
        )
        self.assertTemplateUsed(response, "405.html")
        self.assertEqual(self.remaining(), before)

    def test_delete_without_perm(self):
        before = self.remaining()
        self.client_for(self.recruiter).post(
            self.delete_url, {"ids": json.dumps([self.first.id])}
        )
        self.assertEqual(self.remaining(), before)

    def test_delete_with_perm_only_listed(self):
        self.client_for(self.deleter).post(
            self.delete_url, {"ids": json.dumps([self.first.id])}
        )
        self.assertEqual(self.remaining(), {self.second.id})

    def test_missing_ids_is_empty(self):
        before = self.remaining()
        client = self.client_for(self.deleter)
        for data in ({}, {"ids": "[]"}):
            with self.subTest(data=data):
                response = client.post(self.delete_url, data)
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.remaining(), before)


class TimesheetBulkDeleteTests(PermissionTestCase):
    def setUp(self):
        super().setUp()
        self.owner, owner_employee = make_employee("owner")
        self.other, _ = make_employee("other")
        self.task_manager, task_manager_employee = make_employee("taskmanager")
        self.project_manager, project_manager_employee = make_employee("projmanager")
        self.deleter, _ = make_employee("deleter")
        grant(self.deleter, "project.delete_timesheet")
        # Project.save reads the selected company from the request session.
        request = RequestFactory().get("/")
        request.session = {}
        request.user = self.owner
        _thread_locals.request = request
        project = Project.objects.create(title="Kitchen", start_date=date(2026, 1, 1))
        project.managers.add(project_manager_employee)
        task = Task.objects.create(title="Prep", project=project)
        task.task_managers.add(task_manager_employee)
        _thread_locals.request = None
        self.timesheet = TimeSheet.objects.create(
            employee_id=owner_employee, task_id=task, project_id=project
        )
        self.without_task = TimeSheet.objects.create(employee_id=owner_employee)
        self.url = reverse("time-sheet-bulk-delete")

    def delete(self, user, ids, method="post"):
        client = self.client_for(user)
        return getattr(client, method)(self.url, {"ids": json.dumps(ids)})

    def exists(self, timesheet):
        return TimeSheet.objects.filter(id=timesheet.id).exists()

    def test_owner_deletes_own(self):
        self.delete(self.owner, [self.timesheet.id])
        self.assertFalse(self.exists(self.timesheet))

    def test_other_employee_cannot_delete(self):
        response = self.delete(self.other, [self.timesheet.id])
        self.assertEqual(response.json(), {"message": "Success"})
        self.assertTrue(self.exists(self.timesheet))

    def test_task_manager_can_delete(self):
        self.delete(self.task_manager, [self.timesheet.id])
        self.assertFalse(self.exists(self.timesheet))

    def test_project_manager_can_delete(self):
        self.delete(self.project_manager, [self.timesheet.id])
        self.assertFalse(self.exists(self.timesheet))

    def test_delete_perm_can_delete(self):
        self.delete(self.deleter, [self.timesheet.id])
        self.assertFalse(self.exists(self.timesheet))

    def test_timesheet_without_task_no_error(self):
        response = self.delete(self.other, [self.without_task.id])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.exists(self.without_task))

    def test_missing_id_skipped(self):
        response = self.delete(self.owner, [999999, self.timesheet.id])
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.exists(self.timesheet))

    def test_second_identical_request_harmless(self):
        self.delete(self.owner, [self.timesheet.id])
        response = self.delete(self.owner, [self.timesheet.id])
        self.assertEqual(response.json(), {"message": "Success"})

    def test_get_deletes_nothing(self):
        response = self.delete(self.owner, [self.timesheet.id], method="get")
        self.assertTemplateUsed(response, "405.html")
        self.assertTrue(self.exists(self.timesheet))

    def test_single_delete_missing_no_500(self):
        response = self.client_for(self.owner).get(
            reverse("delete-time-sheet", args=[999999])
        )
        self.assertNotEqual(response.status_code, 500)


class StageDataTests(PermissionTestCase):
    def setUp(self):
        super().setUp()
        self.plain, _ = make_employee("plain")
        self.recruiter, _ = make_employee("recruiter")
        grant(self.recruiter, "recruitment.view_stage")
        self.recruitment = Recruitment.objects.create(title="Cooks")
        self.url = reverse("stage-data", args=[self.recruitment.id])

    def test_without_perm_refused(self):
        response = self.client_for(self.plain).get(self.url, HTTP_HX_REQUEST="true")
        self.assertTemplateUsed(response, "decorator_404.html")

    def test_with_perm_renders(self):
        response = self.client_for(self.recruiter).get(self.url, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateNotUsed(response, "decorator_404.html")


class HistoryRevertTests(PermissionTestCase):
    def setUp(self):
        super().setUp()
        self.plain, _ = make_employee("plain")
        self.editor, _ = make_employee("editor")
        grant(self.editor, "employee.change_bonuspoint")
        _, employee = make_employee("subject")
        # Auto-created for every employee; audited with HorillaAuditLog.
        self.bonus = employee.bonus_point
        self.bonus.points = 5
        self.bonus.save()
        self.old = self.bonus.history_set.filter(points=5).first()
        self.bonus.points = 10
        self.bonus.save()

    def revert(self, user, model="employee.BonusPoint"):
        url = reverse("history-revert", args=[self.bonus.pk, self.old.history_id])
        return self.client_for(user).post(
            f"{url}?model={model}", HTTP_HX_REQUEST="true"
        )

    def points(self):
        self.bonus.refresh_from_db()
        return self.bonus.points

    def test_without_perm_403_and_unchanged(self):
        response = self.revert(self.plain)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.points(), 10)

    def test_with_perm_reverts(self):
        self.revert(self.editor)
        self.assertEqual(self.points(), 5)

    def test_unknown_model_changes_nothing(self):
        self.revert(self.editor, model="employee.NoSuchModel")
        self.assertEqual(self.points(), 10)

    def test_anonymous_changes_nothing(self):
        response = self.revert(None)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Refresh"], "true")
        self.assertEqual(self.points(), 10)


class AuditTagTests(PermissionTestCase):
    def test_anonymous_creates_nothing(self):
        # The route is appended when the context processors are imported.
        engines["django"].engine.template_context_processors
        response = self.client_for().post("/horilla-audit-log", {"title": "x"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(reverse("login")))
        self.assertFalse(AuditTag.objects.exists())
