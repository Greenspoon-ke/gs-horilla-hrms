"""
Tests for /media/ serving (base/views.py protected_media and
is_jwt_token_valid).

Every test uses a temporary MEDIA_ROOT with small synthetic files. The test
runner never touches /var/www/mediafiles or the real database.
"""

import os
import shutil
import tempfile
from urllib.parse import quote

from django.contrib.auth.models import Permission, User
from django.test import TestCase, override_settings
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from horilla.horilla_middlewares import _thread_locals
from leave.tests import add_missing_user_column, make_employee

PRIVATE_PDF = "employee/documents/a.pdf"
PRIVATE_BYTES = b"%PDF-1.4 private test document"
OUTSIDE_BYTES = b"outside media root"


class ProtectedMediaTests(TestCase):
    @classmethod
    def setUpClass(cls):
        add_missing_user_column()
        super().setUpClass()

    def setUp(self):
        _thread_locals.request = None
        self.addCleanup(setattr, _thread_locals, "request", None)

        # A parent folder holds the media root plus files beside it, so
        # traversal tests have something real to try to reach.
        self.parent = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.parent, ignore_errors=True)
        self.root = os.path.join(self.parent, "media")
        os.makedirs(self.root)
        self.write("../outside.txt", OUTSIDE_BYTES)
        self.write("../media-evil/x.txt", OUTSIDE_BYTES)
        self.write(PRIVATE_PDF, PRIVATE_BYTES)
        media = override_settings(MEDIA_ROOT=self.root)
        media.enable()
        self.addCleanup(media.disable)

        self.user, _ = make_employee("worker")

    def write(self, relative_path, content=b"data"):
        full_path = os.path.normpath(os.path.join(self.root, relative_path))
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "wb") as handle:
            handle.write(content)
        return full_path

    def get(self, path, user=None, method="get", **extra):
        if user:
            self.client.force_login(user)
        else:
            self.client.logout()
        return getattr(self.client, method)(f"/media/{path}", **extra)

    def body(self, response):
        return (
            b"".join(response.streaming_content)
            if response.streaming
            else response.content
        )

    def assertLoginRedirect(self, response):
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/login/")
        self.assertIn("private", response.get("Cache-Control", ""))
        self.assertNotIn(PRIVATE_BYTES, response.content)

    def assertServed(self, response, content, inline):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.body(response), content)
        disposition = response["Content-Disposition"]
        self.assertTrue(disposition.startswith("inline" if inline else "attachment"))
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    # Anonymous callers

    def test_anonymous_private_file_redirects_to_login(self):
        self.assertLoginRedirect(self.get(PRIVATE_PDF))

    def test_referer_no_longer_grants_access(self):
        pages = [
            "/login",
            "/login/",
            "/forgot-password",
            "/change-username",
            "/change-password",
            "/employee-reset-password",
            "/recruitment/candidate-survey",
            "/recruitment/open-recruitments",
            "/recruitment/candidate-self-status-tracking",
        ]
        for page in pages:
            for referer in (page, f"https://hr.example.invalid{page}"):
                with self.subTest(referer=referer):
                    response = self.get(PRIVATE_PDF, HTTP_REFERER=referer)
                    self.assertLoginRedirect(response)

    def test_anonymous_company_logo_old_folder(self):
        self.write("base/icon/logo.png", b"old logo")
        response = self.get("base/icon/logo.png")
        self.assertServed(response, b"old logo", inline=True)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertNotIn("private", response.get("Cache-Control", ""))

    def test_anonymous_company_logo_new_folder(self):
        self.write("base/company/icon/logo-1a2b3c4d.png", b"new logo")
        response = self.get("base/company/icon/logo-1a2b3c4d.png")
        self.assertServed(response, b"new logo", inline=True)
        self.assertNotIn("private", response.get("Cache-Control", ""))

    def test_public_prefix_with_dotdot_is_not_public(self):
        path = f"base/company/icon/../../../{PRIVATE_PDF}"
        self.assertLoginRedirect(self.get(path))

    def test_lookalike_prefixes_are_private(self):
        paths = [
            "base/company/logo.png",
            "base/company/stamp/a.png",
            "base/companyX/a.png",
            "Base/Company/Icon/a.png",
            "base/icons/a.png",
            "base/company/icon",
            "base/icon",
        ]
        for path in paths:
            if "." in os.path.basename(path):
                self.write(path)
            with self.subTest(path=path):
                self.assertLoginRedirect(self.get(path))

    def test_anonymous_missing_private_file_redirects(self):
        # Same answer as for a file that exists: no existence oracle.
        self.assertLoginRedirect(self.get("employee/documents/missing.pdf"))

    def test_anonymous_missing_logo_404(self):
        self.assertEqual(self.get("base/company/icon/missing.png").status_code, 404)

    # Paths outside MEDIA_ROOT

    def test_traversal_outside_root_is_404(self):
        paths = [
            "../outside.txt",
            "../../etc/hostname",
            "%2e%2e/outside.txt",
            "..%2foutside.txt",
            "/etc/hostname",
            "../media-evil/x.txt",
            "employee/../../outside.txt",
        ]
        for user in (None, self.user):
            for path in paths:
                with self.subTest(path=path, logged_in=bool(user)):
                    response = self.get(path, user)
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn(OUTSIDE_BYTES, self.body(response))

    def test_backslash_is_not_traversal(self):
        # On Linux and macOS a backslash is part of the file name.
        self.assertLoginRedirect(self.get("..%5Coutside.txt"))
        response = self.get("..%5Coutside.txt", self.user)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(OUTSIDE_BYTES, self.body(response))

    def test_newline_in_path_404(self):
        for path in ("a%0a.pdf", "a%0d.pdf"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, self.user).status_code, 404)

    def test_null_byte_is_not_500(self):
        self.assertLoginRedirect(self.get("a%00.pdf"))
        self.assertEqual(self.get("a%00.pdf", self.user).status_code, 404)

    # Logged-in callers

    def test_logged_in_missing_file_404(self):
        response = self.get("employee/documents/missing.pdf", self.user)
        self.assertEqual(response.status_code, 404)

    def test_directory_and_empty_path_404(self):
        for path in ("employee/", "employee", ""):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, self.user).status_code, 404)
                self.assertLoginRedirect(self.get(path))

    def test_employee_pdf_inline(self):
        response = self.get(PRIVATE_PDF, self.user)
        self.assertServed(response, PRIVATE_BYTES, inline=True)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["Content-Disposition"], 'inline; filename="a.pdf"')
        self.assertIn("private", response["Cache-Control"])

    def test_inline_types(self):
        for name in ("a.png", "a.jpg", "a.jpeg", "a.gif", "a.webp", "b.PDF", "b.PNG"):
            path = self.write(f"employee/files/{name}", name.encode())
            with self.subTest(name=name):
                response = self.get(f"employee/files/{name}", self.user)
                self.assertServed(response, name.encode(), inline=True)
            os.remove(path)

    def test_active_and_other_types_download(self):
        names = [
            "a.html",
            "a.htm",
            "a.svg",
            "a.xml",
            "a.js",
            "a.txt",
            "a.docx",
            "a.xlsx",
            "a.csv",
            "noextension",
            "a.abc",
        ]
        for name in names:
            self.write(f"employee/files/{name}", b"<script>alert(1)</script>")
            with self.subTest(name=name):
                response = self.get(f"employee/files/{name}", self.user)
                self.assertServed(response, b"<script>alert(1)</script>", inline=False)
                self.assertIn("private", response["Cache-Control"])

    def test_svg_logo_downloads(self):
        self.write("base/company/icon/logo.svg", b"<svg></svg>")
        for user in (None, self.user):
            with self.subTest(logged_in=bool(user)):
                response = self.get("base/company/icon/logo.svg", user)
                self.assertServed(response, b"<svg></svg>", inline=False)

    def test_odd_file_names_have_safe_disposition(self):
        names = ["résumé ñ.pdf", 'cv "quoted";x.pdf', "with space.pdf", "a%20b.pdf"]
        for name in names:
            self.write(f"employee/files/{name}", b"odd")
            with self.subTest(name=name):
                response = self.get(f"employee/files/{quote(name)}", self.user)
                self.assertEqual(response.status_code, 200)
                disposition = response["Content-Disposition"]
                self.assertNotIn("\n", disposition)
                self.assertNotIn("\r", disposition)
                if not name.isascii():
                    self.assertIn("filename*=utf-8''", disposition)

    def test_head_allowed(self):
        response = self.get(PRIVATE_PDF, self.user, method="head")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(self.body(response), b"")

    def test_unsafe_methods_not_served(self):
        # MethodNotAllowedMiddleware renders 405.html with status 200.
        for method in ("post", "put", "delete"):
            for user in (None, self.user):
                with self.subTest(method=method, logged_in=bool(user)):
                    response = self.get(PRIVATE_PDF, user, method=method)
                    self.assertTemplateUsed(response, "405.html")
                    self.assertNotIn(PRIVATE_BYTES, self.body(response))

    def test_htmx_same_behaviour(self):
        self.assertLoginRedirect(self.get(PRIVATE_PDF, HTTP_HX_REQUEST="true"))
        response = self.get(PRIVATE_PDF, self.user, HTTP_HX_REQUEST="true")
        self.assertServed(response, PRIVATE_BYTES, inline=True)

    def test_all_logged_in_roles_served(self):
        hr_user, _ = make_employee("hr")
        hr_user.user_permissions.add(
            *Permission.objects.filter(codename__in=["view_employee"])
        )
        manager, manager_employee = make_employee("manager")
        work_info = self.user.employee_get.employee_work_info
        work_info.reporting_manager_id = manager_employee
        work_info.save()
        # CompanyMiddleware logs out any user without an Employee.
        root, _ = make_employee("root")
        root.is_superuser = root.is_staff = True
        root.save()
        for user in (hr_user, manager, root):
            with self.subTest(user=user.username):
                response = self.get(PRIVATE_PDF, user)
                self.assertServed(response, PRIVATE_BYTES, inline=True)

    def test_unreadable_file_404(self):
        if os.geteuid() == 0:
            self.skipTest("root can read a chmod 000 file")
        path = self.write("employee/documents/locked.pdf", b"locked")
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o600)
        response = self.get("employee/documents/locked.pdf", self.user)
        self.assertEqual(response.status_code, 404)

    def test_repeat_request_identical(self):
        full_path = os.path.join(self.root, PRIVATE_PDF)
        mtime = os.stat(full_path).st_mtime_ns
        first = self.get(PRIVATE_PDF, self.user)
        second = self.get(PRIVATE_PDF, self.user)
        self.assertEqual(first.status_code, second.status_code)
        self.assertEqual(self.body(first), self.body(second))
        for header in ("Content-Type", "Content-Disposition", "Cache-Control"):
            self.assertEqual(first[header], second[header])
        self.assertEqual(os.stat(full_path).st_mtime_ns, mtime)

    def test_new_employee_still_sent_to_change_password(self):
        # Existing ForcePasswordChangeMiddleware behaviour, not this view's.
        self.user.is_new_employee = True
        self.user.save()
        response = self.get(PRIVATE_PDF, self.user)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/change-password")

    # API tokens

    def test_valid_jwt_served(self):
        token = AccessToken.for_user(self.user)
        response = self.get(PRIVATE_PDF, HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertServed(response, PRIVATE_BYTES, inline=True)

    def test_bad_jwt_redirects(self):
        refresh = str(RefreshToken.for_user(self.user))
        headers = [
            "Bearer x",
            "Bearer",
            "Bearer ",
            "Basic dXNlcjpwYXNz",
            f"Bearer {refresh}",
        ]
        for header in headers:
            with self.subTest(header=header[:12]):
                response = self.get(PRIVATE_PDF, HTTP_AUTHORIZATION=header)
                self.assertLoginRedirect(response)

    def test_jwt_for_inactive_or_deleted_user_redirects(self):
        inactive, _ = make_employee("inactive")
        inactive_token = AccessToken.for_user(inactive)
        inactive.is_active = False
        inactive.save()
        deleted = User.objects.create_user(username="deleted", password="x")
        deleted_token = AccessToken.for_user(deleted)
        deleted.delete()
        for name, token in (("inactive", inactive_token), ("deleted", deleted_token)):
            with self.subTest(name):
                response = self.get(PRIVATE_PDF, HTTP_AUTHORIZATION=f"Bearer {token}")
                self.assertLoginRedirect(response)
