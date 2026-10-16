"""
Tests for leave_cleanup_preview (leave/preview.py): run read-only, it must
predict exactly what the real clean-up steps then do, and it must refuse a
writable connection.

Synthetic data only. The test runner never touches the real database.
"""

import csv
import os
from datetime import date, datetime
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test import override_settings

from base.models import CompanyLeaves
from leave import conversion, hr_records, past_pending
from leave.accrual import NAIROBI, run_rebuild
from leave.models import AvailableLeave, LeaveAllocationRequest, LeaveRequest, LeaveType
from leave.preview import PreviewRefused, run_preview, writes_refused
from leave.test_carry_rules import TEAMS, CarryRulesTestCase

TODAY = date(2026, 10, 15)
THROUGH = date(2026, 9, 30)
SINCE = datetime(2026, 10, 14, 9, 45, tzinfo=NAIROBI)


@override_settings(
    LEAVE_ALLOCATION_CUTOVER="2026-10-01", LEAVE_CARRY_EXEMPT_DEPARTMENTS=TEAMS
)
class PreviewTests(CarryRulesTestCase):
    def setUp(self):
        super().setUp()
        for weekday in ("5", "6"):
            CompanyLeaves(based_on_week=None, based_on_week_day=weekday).save()
        # Annual Leave as it is in production before leave_cleanup_setup: the
        # preview has to simulate the new settings, not require them.
        LeaveType.objects.filter(pk=self.annual.pk).update(
            reset=True,
            reset_based="yearly",
            reset_month="1",
            reset_day="1",
            carryforward_type="carryforward expire",
            carryforward_expire_in=6,
            carryforward_expire_period="month",
            carryforward_expire_date=date(2027, 3, 16),
            exclude_company_leave="yes",
            exclude_holiday="yes",
        )
        self.annual.refresh_from_db()
        old = date(2025, 1, 2)
        staff = {}
        for code, department, joined, values in [
            ("GSL001", self.finance, date(2023, 2, 1), (24, 6)),  # restored 4
            ("GSL002", self.packing, date(2022, 5, 2), (20, 3)),  # exempt
            ("GSL003", self.finance, date(2026, 6, 1), (24, 0)),  # 2026 joiner
            ("GSL004", self.finance, date(2024, 1, 8), (12, 0)),  # excluded
            ("GSL005", self.finance, date(2026, 8, 5), (4, 0)),  # override
            ("GSL006", self.finance, date(2021, 9, 6), (24, 0)),  # overdrawn
        ]:
            employee = self.in_department(self.employee(code, joined), department)
            assigned = None if code in ("GSL003", "GSL005") else old
            self.balance(employee, *values, assigned=assigned)
            staff[code] = employee
        self.approved(staff["GSL001"], date(2026, 3, 9), 3)
        self.approved(staff["GSL006"], date(2026, 8, 10), 20)
        converted = self.allocation(staff["GSL001"], 5, date(2026, 7, 29))
        self.allocation(staff["GSL002"], 2, date(2026, 8, 3), status="requested")
        self.allocation(staff["GSL002"], 2, date(2026, 8, 5), status="requested")
        # Step 3b removes this one (5 days > GSL003's new 3, so PENDING_EXCEEDS
        # unless the preview counts the removal); the November one stays.
        self.past_pending = self.pending(staff["GSL003"], date(2026, 9, 14), date(2026, 9, 18))
        self.pending(staff["GSL003"], date(2026, 11, 2), date(2026, 11, 3))

        self.decisions = self.write(
            "decisions.csv",
            ["alloc_id", "keep_drop", "start_date", "end_date"],
            [[converted.pk, "keep", "2026-06-08", ""]],
        )
        self.records = self.write(
            "records.csv",
            ["ref", "code", "leave_type", "start_date", "end_date", "status"],
            [["rota-1", "GSL003", "Annual Leave", "2026-07-06", "2026-07-12", "approved"]],
        )
        self.inputs = dict(
            sheet={"GSL003": date(2026, 6, 1)},
            overrides={"GSL005": date(2025, 12, 31)},
            opening_carry={"GSL001": 4, "GSL002": 6, "GSL004": 0, "GSL005": 0, "GSL006": 0},
            excluded={"GSL004"},
        )

    def allocation(self, employee, days, created, status="approved"):
        allocation = LeaveAllocationRequest.objects.create(
            employee_id=employee,
            leave_type_id=self.annual,
            requested_days=days,
            description="time off",
            status=status,
        )
        LeaveAllocationRequest.objects.filter(pk=allocation.pk).update(
            created_at=datetime(created.year, created.month, created.day, 9, tzinfo=NAIROBI)
        )
        return allocation

    def pending(self, employee, start, end):
        leave_request = LeaveRequest(
            employee_id=employee,
            leave_type_id=self.annual,
            start_date=start,
            end_date=end,
            description="time off",
        )
        leave_request.save()
        return leave_request

    def write(self, name, header, rows):
        path = Path(self.out_dir) / name
        with open(path, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)
        return str(path)

    def preview(self):
        """The preview in a savepoint the database itself keeps read-only."""
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL default_transaction_read_only = on")
                cursor.execute("SET LOCAL transaction_read_only = on")
            result = run_preview(
                THROUGH,
                out_dir=str(Path(self.out_dir) / "preview"),
                since=SINCE,
                decisions_path=self.decisions,
                records_path=self.records,
                today=TODAY,
                **self.inputs,
            )
            transaction.set_rollback(True)
        return result

    def apply_real_steps(self):
        call_command("leave_cleanup_setup", "--apply", stdout=StringIO())
        plans, duplicate_of, decisions = conversion.plan_all(self.decisions)
        conversion.apply_plans(plans, duplicate_of, decisions)
        hr_records.apply_plans(hr_records.plan_all(self.records))
        removals = past_pending.plan_removals(TODAY)
        past_pending.apply_removals(
            TODAY, len(removals), Path(self.out_dir) / "past-pending-mapping.csv"
        )
        real = run_rebuild(THROUGH, today=TODAY, out_dir=self.out_dir, **self.inputs)
        count = sum(plan.action == "change" for plan in real["plans"])
        run_rebuild(
            THROUGH, today=TODAY, out_dir=self.out_dir, apply=True,
            confirm_rows=count, **self.inputs,
        )
        return real

    @staticmethod
    def outcome(plans):
        return {
            plan.code: (plan.action, plan.reason, tuple(plan.flags))
            for plan in plans
        }

    def test_preview_equals_the_real_steps_row_by_row(self):
        preview = self.preview()
        predicted = {
            plan.code: plan.proposed or plan.current for plan in preview["plans"]
        }
        # Not vacuous: conversion, records, carry spent first, exemption,
        # exclusion, override and debt all show in the numbers.
        self.assertEqual(
            predicted,
            {
                "GSL001": (14, 0),  # 18 - (3 + 5 converted), 4 carry spent first
                "GSL002": (18, 6),
                "GSL003": (3, 0),  # Jun-Sep 8 - 5 recorded
                "GSL004": (12, 0),
                "GSL005": (18, 0),
                "GSL006": (-2, 0),
            },
        )
        self.annual.refresh_from_db()
        self.assertTrue(self.annual.reset)  # the type was only simulated
        self.assertEqual(
            [r.leave_request_id for r in preview["removals"]], [self.past_pending.pk]
        )
        self.assertNotIn("PENDING_EXCEEDS", self.outcome(preview["plans"])["GSL003"][2])

        real = self.apply_real_steps()
        self.assertEqual(
            LeaveRequest.objects.get(pk=self.past_pending.pk).status, "rejected"
        )
        saved = {
            row.employee_id.badge_id: (row.available_days, row.carryforward_days)
            for row in AvailableLeave.objects.filter(leave_type_id=self.annual)
        }
        self.assertEqual(saved, predicted)
        self.assertEqual(self.outcome(preview["plans"]), self.outcome(real["plans"]))

    def test_summary_and_rows_are_private_files(self):
        preview = self.preview()
        for path in (preview["csv"], preview["summary"]):
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        summary = Path(preview["summary"]).read_text()
        self.assertIn("will be:   reset=False carryforward_type='carryforward'", summary)
        self.assertIn("GSL004: 12+0, active, not on accrual", summary)
        self.assertIn("GSL001: 4 -> change", summary)
        self.assertIn("not made by steps 2-3: 1\n    Annual Leave: 1", summary)
        self.assertIn(f"ids: #{self.past_pending.pk}\n", summary)
        # GSL002's converted pending requests stay: their file decides them.
        self.assertIn("ended, made by the conversion or HR records: 1  GSL002", summary)

    def test_refuses_a_writable_connection(self):
        out = Path(self.out_dir) / "refused"
        with self.assertRaises(PreviewRefused):
            run_preview(THROUGH, out_dir=str(out), since=SINCE, today=TODAY, **self.inputs)
        sheet = self.write("sheet.csv", ["employee_code", "join_date"], [["GSL003", "2026-06-01"]])
        with self.assertRaisesMessage(CommandError, "REFUSED"):
            call_command(
                "leave_cleanup_preview", "--through", "2026-09-30", "--sheet", sheet,
                "--since", "2026-10-14T09:45:00", "--out-dir", str(out),
            )
        self.assertFalse(out.exists())

    def test_a_save_during_the_preview_is_refused(self):
        with writes_refused(), self.assertRaises(PreviewRefused):
            self.annual.save()
        self.annual.refresh_from_db()
        self.assertTrue(self.annual.reset)
