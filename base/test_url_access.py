"""
Every route needs a login unless base.middleware.PUBLIC_ROUTES lists it.

The walk builds a sample path for every URL pattern and sends it anonymously.
No view code runs for a private route: the middleware answers first. A new
view added without a login check therefore fails this test whatever
decorators it uses, and a stale PUBLIC_ROUTES entry fails it too.

Synthetic data only. The test runner never touches the real database.
"""

import re

from django.conf import settings
from django.template import engines
from django.test import Client, TestCase
from django.urls import Resolver404, URLResolver, get_resolver, resolve, reverse
from django.urls.resolvers import RegexPattern

from base.middleware import PUBLIC_ROUTES, SKIPPED_ROUTE_PREFIXES
from horilla.horilla_middlewares import _thread_locals
from leave.tests import add_missing_user_column, make_employee

SAMPLE = {"int": "1", "uuid": "00000000-0000-0000-0000-000000000000"}


def sample_path(pattern):
    """A concrete path that the pattern matches."""
    text = str(pattern)
    if isinstance(pattern, RegexPattern):
        text = text.lstrip("^").rstrip("$")
        # Named groups become "1"; alternations like (auth|base) take the first.
        text = re.sub(r"\(\?P<\w+>[^)]*\)", "1", text)
        return re.sub(r"\(([^)|]*)(?:\|[^)]*)?\)", r"\1", text)
    return re.sub(r"<(?:(\w+):)?\w+>", lambda m: SAMPLE.get(m.group(1), "x"), text)


def all_urls(patterns=None, prefix=""):
    for pattern in get_resolver().url_patterns if patterns is None else patterns:
        if isinstance(pattern, URLResolver):
            yield from all_urls(
                pattern.url_patterns, prefix + sample_path(pattern.pattern)
            )
        else:
            yield "/" + prefix + sample_path(pattern.pattern), pattern.callback


def is_login_redirect(response):
    return response.status_code == 302 and response["Location"].startswith(
        reverse("login")
    )


class UrlAccessTests(TestCase):
    @classmethod
    def setUpClass(cls):
        add_missing_user_column()
        # Some apps append routes when their context processors are imported
        # (horilla-audit-log, for example). Import them all now so the walk
        # does not depend on whether an earlier test rendered a template.
        engines["django"].engine.template_context_processors
        super().setUpClass()

    def setUp(self):
        _thread_locals.request = None
        self.addCleanup(setattr, _thread_locals, "request", None)
        self.client = Client(raise_request_exception=False)

    def reachable(self):
        """(path, route) for every pattern a request can actually reach."""
        found, unbuilt = [], []
        for path, callback in all_urls():
            # Left to their own auth, as in the middleware; tested below.
            if path.lstrip("/").startswith(SKIPPED_ROUTE_PREFIXES):
                continue
            try:
                match = resolve(path)
            except Resolver404:
                unbuilt.append(path)
                continue
            if match.func is callback:  # otherwise shadowed: unreachable
                found.append((path, match.route))
        self.assertEqual(unbuilt, [], "extend sample_path() for these patterns")
        return found

    def test_private_routes_send_anonymous_users_to_login(self):
        failures = []
        checked = 0
        for path, route in self.reachable():
            if route in PUBLIC_ROUTES or route.startswith(SKIPPED_ROUTE_PREFIXES):
                continue
            checked += 1
            response = self.client.get(path)
            if not is_login_redirect(response):
                failures.append(("GET", path, response.status_code))
            response = self.client.post(path, HTTP_HX_REQUEST="true")
            if response.status_code != 204 or response.get("HX-Refresh") != "true":
                failures.append(("HX POST", path, response.status_code))
        if failures:
            self.fail(
                f"{len(failures)} anonymous requests were not sent to login "
                "(add a login check, or list the route in PUBLIC_ROUTES):\n"
                + "\n".join(f"{m} {p} -> {code}" for m, p, code in failures)
            )
        self.assertGreater(checked, 500)

    def test_every_public_route_exists(self):
        routes = {route for _, route in self.reachable()}
        self.assertEqual(PUBLIC_ROUTES - routes, set())

    def test_audit_log_route_is_walked(self):
        # Appended at runtime; a 404 here would hide a regression as "nothing
        # created".
        routes = {route for _, route in self.reachable()}
        self.assertIn("horilla-audit-log", routes)

    def test_public_pages_open_without_login(self):
        pages = [
            ("get", "/login/", {}),
            ("get", "/forgot-password", {}),
            ("get", "/reset-send-success", {}),
            ("get", "/recruitment/candidate-login", {}),
            ("get", "/recruitment/open-recruitments", {}),
            ("get", "/recruitment/application-form", {}),
            ("get", "/onboarding/user-creation/not-a-token", {}),
            ("get", "/health/", {}),
            ("get", "/inbox/notifications/api/unread_count/", {}),
            ("get", "/reload-messages", {"HTTP_HX_REQUEST": "true"}),
            ("post", "/i18n/setlang/", {}),
        ]
        for method, path, extra in pages:
            with self.subTest(path=path):
                response = getattr(self.client, method)(path, **extra)
                self.assertFalse(is_login_redirect(response), response.status_code)
                self.assertNotEqual(response.status_code, 500)

    def test_password_reset_link_opens_without_login(self):
        response = self.client.get("/accounts/reset/MQ/set-password/", follow=True)
        self.assertFalse(
            any(url.startswith(reverse("login")) for url, _ in response.redirect_chain)
        )
        response = self.client.get("/accounts/reset/MQ/bad-token/", follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            any(url.startswith(reverse("login")) for url, _ in response.redirect_chain)
        )

    def test_candidate_portal_uses_its_own_login(self):
        response = self.client.get("/recruitment/candidate-self-status-tracking/")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(response["Location"].startswith(reverse("login")))

    def test_admin_and_api_are_left_to_their_own_auth(self):
        response = self.client.get("/admin/")
        self.assertTrue(response["Location"].startswith("/admin/login/"))
        response = self.client.post(
            "/api/auth/login/", {"username": "nobody", "password": "x"}
        )
        self.assertFalse(is_login_redirect(response))
        # Plain web views under api/ keep their own Django @login_required.
        for path in ("/api/facedetection/", "/api/geofencing/config/"):
            with self.subTest(path=path):
                response = self.client.get(path, HTTP_HX_REQUEST="true")
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response["Location"].startswith(settings.LOGIN_URL))

    def test_logged_in_user_is_not_redirected(self):
        user, _ = make_employee("walker")
        self.client.force_login(user)
        response = self.client.get("/payroll/get-language-code/")
        self.assertFalse(is_login_redirect(response))

    def test_login_redirect_keeps_next(self):
        response = self.client.get("/payroll/view-payroll-dashboard/?month=1")
        self.assertEqual(
            response["Location"],
            "/login/?next=/payroll/view-payroll-dashboard/%3Fmonth%3D1",
        )

    def test_middleware_registered_once(self):
        self.assertEqual(
            settings.MIDDLEWARE.count("base.middleware.LoginRequiredMiddleware"), 1
        )
