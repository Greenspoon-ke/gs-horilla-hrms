"""
Tests for the API's default permission (horilla/rest_conf.py), the public
login view and TaxBracketView's permissions (p1-04).

Tokens come from the real JWT path, sent as a Bearer header, so the tests use
the same authentication the mobile app does.

Synthetic data only. The test runner never touches the real database.
"""

from datetime import date, timedelta

from django.contrib.auth.models import Group, Permission
from django.test import Client, RequestFactory, TestCase
from django.urls import URLResolver, get_resolver
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.settings import api_settings
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from base.models import Company
from horilla.horilla_middlewares import _thread_locals
from leave.models import Holiday
from leave.tests import PASSWORD, add_missing_user_column, make_employee
from payroll.models.models import FilingStatus, Reimbursement
from payroll.models.tax_models import TaxBracket

# Views that may be called without a token. Adding one here needs review.
PUBLIC_API_VIEWS = {"LoginAPIView"}

TAX_URL = "/api/payroll/tax-bracket/"


def api_views(patterns=None, prefix=""):
    """Yield (route, view class) for every mounted DRF view, wherever it is."""
    for pattern in get_resolver().url_patterns if patterns is None else patterns:
        route = prefix + str(pattern.pattern)
        if isinstance(pattern, URLResolver):
            yield from api_views(pattern.url_patterns, route)
        else:
            cls = getattr(pattern.callback, "cls", None)  # set by APIView.as_view()
            if isinstance(cls, type) and issubclass(cls, APIView):
                yield route, cls


def grant(user, *codenames):
    for codename in codenames:
        app_label, codename = codename.split(".")
        user.user_permissions.add(
            Permission.objects.get(content_type__app_label=app_label, codename=codename)
        )


def bearer(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


class ApiTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        add_missing_user_column()
        super().setUpClass()

    def setUp(self):
        _thread_locals.request = None
        self.addCleanup(setattr, _thread_locals, "request", None)
        Company.objects.create(
            company="Test Co",
            hq=True,
            address="-",
            country="-",
            state="-",
            city="-",
            zip="-",
        )
        self.client = Client(raise_request_exception=False)


class ApiDefaultsTests(ApiTestCase):
    def test_default_permission_is_is_authenticated(self):
        self.assertEqual(api_settings.DEFAULT_PERMISSION_CLASSES, [IsAuthenticated])

    def test_every_api_view_needs_login_unless_listed(self):
        routes = list(api_views())
        self.assertGreater(len(routes), 100)  # proves the walk reached the API
        for route, cls in routes:
            permissions = list(cls.permission_classes)
            with self.subTest(route=route, view=cls.__name__):
                if cls.__name__ in PUBLIC_API_VIEWS:
                    self.assertEqual(permissions, [AllowAny])
                else:
                    self.assertTrue(
                        any(
                            isinstance(p, type) and issubclass(p, IsAuthenticated)
                            for p in permissions
                        ),
                        f"/{route} ({cls.__name__}) is reachable without login",
                    )


class LoginStillOpenTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.user, _ = make_employee("apiuser")

    def test_wrong_password_is_invalid_credentials(self):
        response = self.client.post(
            "/api/auth/login/",
            {"username": "apiuser", "password": "wrong"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"error": "Invalid credentials"})

    def test_bad_bearer_header_unchanged(self):
        response = self.client.post(
            "/api/auth/login/",
            {"username": "apiuser", "password": PASSWORD},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer x",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "token_not_valid")

    def test_empty_body_reaches_view(self):
        response = self.client.post(
            "/api/auth/login/", {}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"error": "Please provide Username and Password"}
        )

    def test_login_new_employee_unchanged(self):
        self.user.is_new_employee = True
        self.user.save()
        response = self.client.post(
            "/api/auth/login/",
            {"username": "apiuser", "password": PASSWORD},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.json())


class TaxBracketApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.filing = FilingStatus.objects.create(filing_status="Single")
        self.bracket = TaxBracket.objects.create(
            filing_status_id=self.filing, min_income=0, max_income=1000, tax_rate=10
        )
        self.detail = f"{TAX_URL}{self.bracket.pk}"
        self.employee, _ = make_employee("employee")
        self.manager, manager_employee = make_employee("manager")
        _, report = make_employee("report", with_user=False)
        work_info = report.employee_work_info
        work_info.reporting_manager_id = manager_employee
        work_info.save()
        self.viewer, _ = make_employee("viewer")
        grant(self.viewer, "payroll.view_taxbracket")
        self.payroll, _ = make_employee("payroll")
        grant(
            self.payroll,
            "payroll.view_taxbracket",
            "payroll.add_taxbracket",
            "payroll.change_taxbracket",
            "payroll.delete_taxbracket",
        )

    def new_bracket(self):
        return {"filing_status_id": self.filing.pk, "min_income": 1001, "tax_rate": 20}

    def assertUnchanged(self):
        self.assertEqual(TaxBracket.objects.count(), 1)
        self.bracket.refresh_from_db()
        self.assertEqual(self.bracket.tax_rate, 10)

    # Anonymous

    def test_anonymous_list_401(self):
        response = self.client.get(TAX_URL)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["WWW-Authenticate"], 'Bearer realm="api"')

    def test_anonymous_detail_401(self):
        self.assertEqual(self.client.get(self.detail).status_code, 401)

    def test_anonymous_post_401_no_row(self):
        response = self.client.post(
            TAX_URL, self.new_bracket(), content_type="application/json"
        )
        self.assertEqual(response.status_code, 401)
        self.assertUnchanged()

    def test_anonymous_put_401_row_unchanged(self):
        for url in (self.detail, TAX_URL):
            with self.subTest(url=url):
                response = self.client.put(
                    url, {"tax_rate": 50}, content_type="application/json"
                )
                self.assertEqual(response.status_code, 401)
        self.assertUnchanged()

    def test_anonymous_delete_401_row_kept(self):
        self.assertEqual(self.client.delete(self.detail).status_code, 401)
        self.assertUnchanged()

    def test_anonymous_options_and_head_401(self):
        self.assertEqual(self.client.options(TAX_URL).status_code, 401)
        self.assertEqual(self.client.head(TAX_URL).status_code, 401)

    # Logged in without the permission

    def test_employee_get_403(self):
        for url in (TAX_URL, self.detail):
            with self.subTest(url=url):
                response = self.client.get(url, **bearer(self.employee))
                self.assertEqual(response.status_code, 403)

    def test_employee_writes_403_no_change(self):
        headers = bearer(self.employee)
        post = self.client.post(
            TAX_URL, self.new_bracket(), content_type="application/json", **headers
        )
        put = self.client.put(
            self.detail, {"tax_rate": 50}, content_type="application/json", **headers
        )
        delete = self.client.delete(self.detail, **headers)
        # PUT has no handler any more: 405 for any token holder.
        self.assertEqual(
            [post.status_code, put.status_code, delete.status_code], [403, 405, 403]
        )
        self.assertUnchanged()

    def test_manager_without_perm_403(self):
        headers = bearer(self.manager)
        self.assertEqual(self.client.get(TAX_URL, **headers).status_code, 403)
        self.assertEqual(self.client.delete(self.detail, **headers).status_code, 403)
        self.assertUnchanged()

    def test_view_perm_reads_only(self):
        headers = bearer(self.viewer)
        response = self.client.get(TAX_URL, **headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(self.client.get(self.detail, **headers).status_code, 200)
        post = self.client.post(
            TAX_URL, self.new_bracket(), content_type="application/json", **headers
        )
        delete = self.client.delete(self.detail, **headers)
        self.assertEqual([post.status_code, delete.status_code], [403, 403])
        self.assertUnchanged()

    def test_missing_pk_without_perm_403(self):
        response = self.client.get(f"{TAX_URL}999999", **bearer(self.employee))
        self.assertEqual(response.status_code, 403)

    # Permission holders

    def test_add_perm_creates(self):
        response = self.client.post(
            TAX_URL,
            self.new_bracket(),
            content_type="application/json",
            **bearer(self.payroll),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TaxBracket.objects.count(), 2)

    def test_add_perm_invalid_400(self):
        bad = {
            "missing min_income": {"filing_status_id": self.filing.pk, "tax_rate": 1},
            "missing filing status": {"min_income": 1, "tax_rate": 1},
            "text tax rate": {**self.new_bracket(), "tax_rate": "abc"},
            "huge number": {**self.new_bracket(), "tax_rate": "9" * 1001},
            "empty body": {},
        }
        for name, data in bad.items():
            with self.subTest(name):
                response = self.client.post(
                    TAX_URL,
                    data,
                    content_type="application/json",
                    **bearer(self.payroll),
                )
                self.assertEqual(response.status_code, 400)
        self.assertUnchanged()

    def test_put_not_allowed(self):
        # PUT never worked (it saved before validating); edits stay in the
        # web form, which validates them.
        for data in ({"tax_rate": 12.5}, {"tax_rate": "abc"}):
            with self.subTest(data=data):
                response = self.client.put(
                    self.detail,
                    data,
                    content_type="application/json",
                    **bearer(self.payroll),
                )
                self.assertEqual(response.status_code, 405)
        self.assertUnchanged()

    def test_post_runs_web_form_checks(self):
        cases = {
            "duplicate": {
                "filing_status_id": self.filing.pk,
                "min_income": 0,
                "max_income": 1000,
                "tax_rate": 10,
            },
            "min not below max": {
                "filing_status_id": self.filing.pk,
                "min_income": 5000,
                "max_income": 4000,
                "tax_rate": 5,
            },
            "overlaps existing": {
                "filing_status_id": self.filing.pk,
                "min_income": 500,
                "max_income": 2000,
                "tax_rate": 5,
            },
            "NaN rate": {**self.new_bracket(), "tax_rate": "NaN"},
        }
        for name, data in cases.items():
            with self.subTest(name):
                response = self.client.post(
                    TAX_URL,
                    data,
                    content_type="application/json",
                    **bearer(self.payroll),
                )
                self.assertEqual(response.status_code, 400)
        self.assertUnchanged()

    def test_post_ignores_audit_and_active_fields(self):
        response = self.client.post(
            TAX_URL,
            {
                **self.new_bracket(),
                "is_active": False,
                "created_by": self.employee.pk,
                "modified_by": self.employee.pk,
            },
            content_type="application/json",
            **bearer(self.payroll),
        )
        self.assertEqual(response.status_code, 200)
        created = TaxBracket.objects.exclude(pk=self.bracket.pk).get()
        self.assertTrue(created.is_active)
        # Recorded as the real caller, never the value sent in the body.
        self.assertEqual(created.created_by, self.payroll)
        self.assertNotEqual(created.modified_by, self.employee)
        # A blank maximum means no upper limit, as in the web form; the API
        # shows it as null because JSON has no infinity.
        self.assertEqual(created.max_income, float("inf"))
        self.assertIsNone(response.json()["max_income"])
        listed = self.client.get(TAX_URL, **bearer(self.viewer))
        self.assertEqual(listed.status_code, 200)

    def test_delete_perm_deletes(self):
        response = self.client.delete(self.detail, **bearer(self.payroll))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(TaxBracket.objects.exists())

    def test_missing_pk_404(self):
        missing = f"{TAX_URL}999999"
        headers = bearer(self.payroll)
        responses = [
            self.client.get(missing, **headers),
            self.client.delete(missing, **headers),
        ]
        self.assertEqual([r.status_code for r in responses], [404] * 2)

    def test_non_numeric_pk_404(self):
        response = self.client.get(f"{TAX_URL}abc", **bearer(self.payroll))
        self.assertEqual(response.status_code, 404)

    def test_superuser_full_access(self):
        root, _ = make_employee("root")
        root.is_superuser = True
        root.save()
        headers = bearer(root)
        self.assertEqual(self.client.get(TAX_URL, **headers).status_code, 200)
        post = self.client.post(
            TAX_URL, self.new_bracket(), content_type="application/json", **headers
        )
        self.assertEqual(post.status_code, 200)
        self.assertEqual(self.client.delete(self.detail, **headers).status_code, 200)

    def test_group_perm_works(self):
        member, _ = make_employee("member")
        group = Group.objects.create(name="Tax readers")
        group.permissions.add(Permission.objects.get(codename="view_taxbracket"))
        member.groups.add(group)
        self.assertEqual(self.client.get(TAX_URL, **bearer(member)).status_code, 200)

    def test_bad_tokens_401(self):
        expired = AccessToken.for_user(self.payroll)
        expired.set_exp(lifetime=-timedelta(minutes=1))
        tokens = {
            "malformed": "not-a-token",
            "expired": str(expired),
            "wrong signature": str(AccessToken.for_user(self.payroll))[:-4] + "abcd",
        }
        for name, token in tokens.items():
            with self.subTest(name):
                response = self.client.get(
                    TAX_URL, HTTP_AUTHORIZATION=f"Bearer {token}"
                )
                self.assertEqual(response.status_code, 401)

    def test_inactive_user_401(self):
        headers = bearer(self.payroll)
        self.payroll.is_active = False
        self.payroll.save()
        self.assertEqual(self.client.get(TAX_URL, **headers).status_code, 401)

    def test_session_cookie_not_accepted(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(TAX_URL).status_code, 401)

    def test_htmx_header_irrelevant(self):
        response = self.client.get(TAX_URL, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 401)
        response = self.client.get(
            TAX_URL, HTTP_HX_REQUEST="true", **bearer(self.viewer)
        )
        self.assertEqual(response.status_code, 200)

    def test_patch_not_allowed(self):
        self.assertEqual(self.client.patch(self.detail).status_code, 401)
        response = self.client.patch(self.detail, **bearer(self.payroll))
        self.assertEqual(response.status_code, 405)

    def test_authenticated_options_leaks_no_rows(self):
        response = self.client.options(TAX_URL, **bearer(self.employee))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("min_income", response.content.decode())

    def test_head_follows_view_perm(self):
        self.assertEqual(
            self.client.head(TAX_URL, **bearer(self.employee)).status_code, 403
        )
        response = self.client.head(TAX_URL, **bearer(self.viewer))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")


class OtherAllowAnyViewsTests(ApiTestCase):
    """The two other views that relied on the old AllowAny default."""

    def setUp(self):
        super().setUp()
        self.user, _ = make_employee("apiuser")
        self.holiday = Holiday.objects.create(
            name="Madaraka", start_date=date(2026, 6, 1)
        )

    def test_check_user_level_anonymous_401(self):
        response = self.client.get("/api/base/check-user-level?perm=x")
        self.assertEqual(response.status_code, 401)

    def test_check_user_level_authenticated(self):
        grant(self.user, "leave.view_holiday")
        headers = bearer(self.user)
        cases = {
            "leave.view_holiday": 200,
            "leave.delete_holiday": 400,
            "é": 400,
        }
        for perm, status in cases.items():
            with self.subTest(perm=perm):
                response = self.client.get(
                    "/api/base/check-user-level", {"perm": perm}, **headers
                )
                self.assertEqual(response.status_code, status)
        response = self.client.get("/api/base/check-user-level", **headers)
        self.assertEqual(response.status_code, 400)

    def test_holiday_detail_anonymous_401(self):
        response = self.client.get(f"/api/leave/holiday/{self.holiday.pk}/")
        self.assertEqual(response.status_code, 401)

    def test_holiday_detail_perm(self):
        url = f"/api/leave/holiday/{self.holiday.pk}/"
        self.assertEqual(self.client.get(url, **bearer(self.user)).status_code, 403)
        grant(self.user, "leave.view_holiday")
        self.assertEqual(self.client.get(url, **bearer(self.user)).status_code, 200)

    def test_explicit_view_unchanged(self):
        url = "/api/leave/holiday/"
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.client.get(url, **bearer(self.user)).status_code, 403)
        grant(self.user, "leave.view_holiday")
        self.assertEqual(self.client.get(url, **bearer(self.user)).status_code, 200)

    def test_jwt_new_employee_not_redirected(self):
        self.user.is_new_employee = True
        self.user.save()
        response = self.client.get("/api/leave/holiday/", **bearer(self.user))
        self.assertEqual(response.status_code, 403)


class ReimbursementApiTests(ApiTestCase):
    """Approve/reject and edit need the same permissions as the web pages."""

    def setUp(self):
        super().setUp()
        self.requester, requester_employee = make_employee("requester")
        self.approver, _ = make_employee("approver")
        grant(self.approver, "payroll.change_reimbursement")
        self.deleter, _ = make_employee("deleter")
        grant(self.deleter, "payroll.delete_reimbursement")
        # Reimbursement.save reads the request user from the thread-local.
        request = RequestFactory().get("/")
        request.user = self.requester
        request.session = {}
        _thread_locals.request = request
        self.reimbursement = Reimbursement.objects.create(
            title="Taxi",
            type="reimbursement",
            employee_id=requester_employee,
            allowance_on=date(2026, 1, 15),
            attachment="payroll/reimbursement/taxi.pdf",
            amount=100,
        )
        _thread_locals.request = None
        self.approve_url = (
            f"/api/payroll/reimbusement-approve-reject/{self.reimbursement.pk}"
        )

    def approve(self, user, data):
        return self.client.post(
            self.approve_url, data, content_type="application/json", **bearer(user)
        )

    def state(self):
        self.reimbursement.refresh_from_db()
        return self.reimbursement.status, self.reimbursement.amount

    def test_anonymous_approve_401(self):
        response = self.client.post(self.approve_url, {"status": "approved"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.state(), ("requested", 100))

    def test_employee_cannot_approve_or_change_amount(self):
        for user in (self.requester, self.deleter):
            with self.subTest(user=user.username):
                response = self.approve(user, {"status": "approved", "amount": "5000"})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.state(), ("requested", 100))

    def test_approver_approves_and_rejects(self):
        response = self.approve(self.approver, {"status": "approved"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.state(), ("approved", 100))
        response = self.approve(self.approver, {"status": "canceled"})
        self.assertEqual(response.json(), {"status": "rejected"})

    def test_approver_amount_as_number_or_text(self):
        # A negative amount becomes 0, which leaves the amount as it was.
        for amount, expected in ((250, 250), ("300", 300), ("-5", 300)):
            with self.subTest(amount=amount):
                response = self.approve(
                    self.approver, {"status": "approved", "amount": amount}
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.state()[1], expected)

    def test_approver_bad_input_400(self):
        cases = [
            {"status": "paid"},
            {},
            {"status": "approved", "amount": "abc"},
            {"status": "approved", "amount": "[1]"},
            {"status": "approved", "amount": "9" * 5000 + "**9"},
        ]
        for data in cases:
            with self.subTest(data=str(data)[:40]):
                self.assertEqual(self.approve(self.approver, data).status_code, 400)
        self.assertEqual(self.state(), ("requested", 100))

    def test_missing_reimbursement_404(self):
        response = self.client.post(
            "/api/payroll/reimbusement-approve-reject/999999",
            {"status": "approved"},
            content_type="application/json",
            **bearer(self.approver),
        )
        self.assertEqual(response.status_code, 404)

    def test_edit_and_delete_without_perm_403(self):
        url = f"/api/payroll/reimbusement/{self.reimbursement.pk}"
        headers = bearer(self.requester)
        put = self.client.put(
            url, {"amount": 1}, content_type="application/json", **headers
        )
        delete = self.client.delete(url, **headers)
        self.assertEqual([put.status_code, delete.status_code], [403, 403])
        self.assertTrue(Reimbursement.objects.filter(pk=self.reimbursement.pk).exists())

    def test_delete_with_perm_passes_the_permission_check(self):
        # Pre-existing (ticket p2-24): Reimbursement.delete() only deletes rows
        # that have an allowance and otherwise raises, on the web too. Here we
        # only check that the permission holder is not refused.
        url = f"/api/payroll/reimbusement/{self.reimbursement.pk}"
        response = self.client.delete(url, **bearer(self.deleter))
        self.assertNotIn(response.status_code, (401, 403))
