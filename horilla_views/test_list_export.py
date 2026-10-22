"""
Tests for the list-view quick export (horilla_views/generic/cbv/views.py,
HorillaListView.export_data).

The expected outputs below were captured from the export code as it was
before the exec() removal, so they prove that legitimate exports keep the
same bytes (CSV, JSON) and cells (XLSX). No output contains row ids, so the
goldens do not depend on database sequences and hold under --keepdb.

Synthetic data only. The test runner never touches the real database.
"""

import io
import json

from django.contrib.auth.models import Group, User
from django.contrib.sessions.backends.db import SessionStore
from django.test import RequestFactory, TestCase
from openpyxl import load_workbook

from employee.models import Employee
from horilla.horilla_middlewares import _thread_locals
from horilla_views.generic.cbv.views import HorillaListView
from leave.tests import add_missing_user_column
from pms.cbvs import BonusPointSettingListView
from pms.models import BonusPointSetting

PASSWORD = "not-a-real-password"
COLUMNS = [["Name", "name"], ["Key", "natural_key"]]
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

GOLDEN_CSV = "".join(
    [
        "Name,Key\r\n",
        "Alpha,\"('Alpha',)\"\r\n",
        "Beta   Team,\"('Beta   Team',)\"\r\n",
    ]
)
GOLDEN_JSON = (
    "[\n"
    "    {\n"
    '        "Name": "Alpha",\n'
    '        "Key": "(\'Alpha\',)"\n'
    "    },\n"
    "    {\n"
    '        "Name": "Beta   Team",\n'
    '        "Key": "(\'Beta   Team\',)"\n'
    "    }\n"
    "]"
)
GOLDEN_XLSX_ROWS = [
    ["Name", "Key"],
    ["Alpha", "('Alpha',)"],
    ["Beta   Team", "('Beta   Team',)"],
]
GOLDEN_PMS_CSV = (
    "Model,Applicable For,Bonus For,Condition,Points,Is Active\r\n"
    "Objective,Owner,Completing,Completion Date < End Date,5,\r\n"
)


class GroupExportList(HorillaListView):
    """Test-only list over auth.Group: one model field and one method column."""

    model = Group
    columns = [("Name", "name"), ("Key", "natural_key")]
    export_fields = []


def make_login(label, superuser=False):
    """A login with its Employee, as every real user has; menus read it."""
    create = User.objects.create_superuser if superuser else User.objects.create_user
    user = create(username=label, password=PASSWORD)
    employee = Employee.objects.create(
        employee_user_id=user,
        employee_first_name=label,
        email=f"{label}@example.invalid",
        phone="0700000000",
    )
    return user, employee


def xlsx_rows(response):
    """Sheet title and cell values; the file bytes carry a fresh timestamp."""
    sheet = load_workbook(io.BytesIO(response.content)).active
    return sheet.title, [list(row) for row in sheet.iter_rows(values_only=True)]


class ExportTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        add_missing_user_column()
        super().setUpClass()

    def setUp(self):
        # ThreadLocalMiddleware never clears the request; reset it per test.
        _thread_locals.request = None
        self.addCleanup(setattr, _thread_locals, "request", None)
        self.user, _ = make_login("exporter")
        self.alpha = Group.objects.create(name="Alpha")
        self.beta = Group.objects.create(name="Beta   Team")
        self.ids = [self.alpha.pk, self.beta.pk]

    def export_request(self, data, user=None):
        """A quick-export POST as the modal sends it."""
        request = RequestFactory().post("/test-export/", data)
        request.user = user or self.user
        # Display methods such as is_active_toggle render templates whose
        # context processors read the session key.
        request.session = SessionStore()
        request.session.create()
        return request

    def build_view(self, request, view_cls=GroupExportList):
        # HorillaListView.__init__ reads the request from the thread-local and
        # filters ToggleColumn by its user, which fails for an AnonymousUser.
        if request.user.is_authenticated:
            _thread_locals.request = request
        view = view_cls()
        _thread_locals.request = request
        view.request = request
        return view

    def export(self, columns, ids=None, fmt="csv", view_cls=GroupExportList):
        """Call export_data directly with a well-formed POST."""
        request = self.export_request(
            {
                "ids": json.dumps(self.ids if ids is None else ids),
                "columns": json.dumps(columns),
                "format": fmt,
            }
        )
        return self.build_view(request, view_cls).export_data(request)


class ExportOutputUnchangedTests(ExportTestCase):
    """Legitimate exports give the same output as before the fix."""

    def test_csv_output_unchanged(self):
        response = self.export(COLUMNS, fmt="csv")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertEqual(
            response["Content-Disposition"], 'attachment; filename="quick_export.csv"'
        )
        self.assertEqual(response.content.decode(), GOLDEN_CSV)

    def test_json_output_unchanged(self):
        response = self.export(COLUMNS, fmt="json")
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(
            response["Content-Disposition"], 'attachment; filename="quick_export.json"'
        )
        self.assertEqual(response.content.decode(), GOLDEN_JSON)

    def test_xlsx_cells_unchanged(self):
        response = self.export(COLUMNS, fmt="xlsx")
        self.assertEqual(response["Content-Type"], XLSX_TYPE)
        self.assertEqual(
            response["Content-Disposition"], 'attachment; filename="quick_export.xlsx"'
        )
        self.assertEqual(xlsx_rows(response), ("Quick Export", GOLDEN_XLSX_ROWS))

    def test_pdf_still_generated(self):
        response = self.export(COLUMNS, fmt="pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(
            response["Content-Disposition"], 'attachment; filename="quick_export.pdf"'
        )
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_unknown_format_falls_back_to_xlsx(self):
        response = self.export(COLUMNS, fmt="xml")
        self.assertEqual(response["Content-Type"], XLSX_TYPE)
        self.assertEqual(xlsx_rows(response), ("Quick Export", GOLDEN_XLSX_ROWS))

    def test_duplicate_and_reordered_columns_unchanged(self):
        columns = [["Key", "natural_key"], ["Name", "name"], ["Name", "name"]]
        response = self.export(columns)
        self.assertEqual(
            response.content.decode(),
            "Key,Name\r\n"
            "\"('Alpha',)\",Alpha\r\n"
            "\"('Beta   Team',)\",Beta   Team\r\n",
        )

    def test_unicode_label_unchanged(self):
        response = self.export([["Jina ✓ <b>", "name"]])
        self.assertEqual(
            response.content.decode(), "Jina ✓ <b>\r\nAlpha\r\nBeta   Team\r\n"
        )

    def test_empty_ids_exports_headers_only(self):
        response = self.export(COLUMNS, ids=[])
        self.assertEqual(response.content.decode(), "Name,Key\r\n")

    def test_export_twice_identical(self):
        from horilla.urls import urlpatterns

        before = len(urlpatterns)
        first = self.export(COLUMNS).content
        second = self.export(COLUMNS).content
        self.assertEqual(first, second)
        self.assertEqual(len(urlpatterns), before)

    def test_pms_export_direct_call_unchanged(self):
        # Covers the <input> branch of remove_extra_spaces (is_active_toggle).
        setting = BonusPointSetting.objects.create(
            model="pms.models.EmployeeObjective",
            applicable_for="owner",
            bonus_for="completed",
            field_1="complition_date",
            conditions="<",
            field_2="end_date",
            points=5,
            is_active=True,
        )
        columns = [list(column) for column in BonusPointSettingListView.columns]
        response = self.export(
            columns, ids=[setting.pk], view_cls=BonusPointSettingListView
        )
        self.assertEqual(response.content.decode(), GOLDEN_PMS_CSV)
