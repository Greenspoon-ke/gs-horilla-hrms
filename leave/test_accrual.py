"""
Tests for the Annual Leave monthly accrual (leave/accrual.py), the
creation helper used by every assignment path, and the reset job's skip.

Synthetic data only. The test runner never touches the real database.
"""

import shutil
import tempfile
from datetime import date

from django.conf import settings
from django.test import Client, TestCase
from django.urls import reverse

from employee.models import Employee, EmployeeWorkInformation
from horilla.horilla_middlewares import _thread_locals
from leave.accrual import (
    AccrualRefused,
    accrual_start,
    prepare_new_balance,
    run_daily,
    run_rebuild,
)
from leave.models import AvailableLeave, LeaveRequest, LeaveType
from leave.scheduler import leave_reset
from leave.tests import add_missing_user_column, make_employee

TODAY = date(2026, 10, 9)
THROUGH = date(2026, 9, 30)


class AccrualTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        add_missing_user_column()
        super().setUpClass()

    def setUp(self):
        _thread_locals.request = None
        self.addCleanup(setattr, _thread_locals, "request", None)
        self.out_dir = tempfile.mkdtemp(prefix="accrual-test-")
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)
        # Annual Leave as configured in production.
        self.annual = LeaveType.objects.create(
            name="Annual Leave",
            total_days=24,
            reset=True,
            reset_based="yearly",
            reset_month="1",
            reset_day="1",
            carryforward_type="carryforward expire",
            carryforward_max=10,
            carryforward_expire_in=6,
            carryforward_expire_period="month",
            carryforward_expire_date=date(2027, 3, 16),
        )

    def employee(self, code, joined, active=True):
        _, employee = make_employee(code.lower(), with_user=False)
        Employee.objects.filter(pk=employee.pk).update(
            badge_id=code, is_active=active
        )
        EmployeeWorkInformation.objects.filter(employee_id=employee).update(
            date_joining=joined
        )
        return Employee.objects.get(pk=employee.pk)

    def balance(self, employee, available=24, carry=0, assigned=None, marker=None):
        row = AvailableLeave(
            employee_id=employee,
            leave_type_id=self.annual,
            available_days=available,
            carryforward_days=carry,
            last_accrual_date=marker,
        )
        if assigned:
            row.assigned_date = assigned
        row.save()
        return row

    def approved(self, employee, start, days):
        request = LeaveRequest(
            employee_id=employee,
            leave_type_id=self.annual,
            start_date=start,
            end_date=start,
            description="test",
        )
        request.save()
        LeaveRequest.objects.filter(pk=request.pk).update(
            status="approved", requested_days=days
        )

    def values(self, row):
        row.refresh_from_db()
        return (row.available_days, row.carryforward_days, row.last_accrual_date)

    def rebuild(self, sheet, **kwargs):
        kwargs.setdefault("today", TODAY)
        kwargs.setdefault("out_dir", self.out_dir)
        return run_rebuild(THROUGH, sheet, **kwargs)

    def apply_rebuild(self, sheet, **kwargs):
        dry = self.rebuild(sheet, **kwargs)
        count = sum(plan.action == "change" for plan in dry["plans"])
        return self.rebuild(sheet, apply=True, confirm_rows=count, **kwargs)

    def daily(self, today, apply=True):
        return run_daily(apply=apply, out_dir=self.out_dir, today=today)


class JoinCutoffTests(AccrualTestCase):
    def test_joining_before_the_20th_counts_that_month(self):
        self.assertEqual(accrual_start(date(2026, 3, 1)), date(2026, 3, 1))
        self.assertEqual(accrual_start(date(2026, 3, 19)), date(2026, 3, 1))

    def test_joining_on_or_after_the_20th_starts_next_month(self):
        self.assertEqual(accrual_start(date(2026, 3, 20)), date(2026, 4, 1))
        self.assertEqual(accrual_start(date(2026, 12, 31)), date(2027, 1, 1))

    def test_nothing_accrues_before_2026(self):
        self.assertEqual(accrual_start(date(2023, 1, 15)), date(2026, 1, 1))

    def test_rebuild_applies_the_cutoff_to_the_sheet_date(self):
        early = self.balance(self.employee("GSL001", date(2026, 6, 16)))
        late = self.balance(self.employee("GSL002", date(2026, 6, 1)))
        # The sheet date wins over the system date (D7').
        self.apply_rebuild({"GSL001": date(2026, 7, 19), "GSL002": date(2026, 7, 20)})
        self.assertEqual(self.values(early), (6, 0, date(2026, 10, 1)))  # Jul-Sep
        self.assertEqual(self.values(late), (4, 0, date(2026, 10, 1)))  # Aug-Sep


class DailyAccrualTests(AccrualTestCase):
    def test_catches_up_every_missed_month(self):
        row = self.balance(self.employee("GSL001", date(2026, 5, 4)), 3, 0)
        AvailableLeave.objects.filter(pk=row.pk).update(
            last_accrual_date=date(2026, 6, 1)
        )
        self.daily(TODAY)
        # June to September: 4 months of 2 days.
        self.assertEqual(self.values(row), (11, 0, date(2026, 10, 1)))

    def test_year_end_carries_at_most_10(self):
        row = self.balance(
            self.employee("GSL001", date(2026, 1, 5)), 15, 0, marker=date(2026, 12, 1)
        )
        self.daily(date(2027, 1, 3))
        row.refresh_from_db()
        # December's 2 days are credited first: 17 left, 10 carried, 7 lapse.
        self.assertEqual((row.available_days, row.carryforward_days), (0, 10))
        self.assertEqual(row.last_accrual_date, date(2027, 1, 1))
        self.assertEqual(row.reset_date, date(2028, 1, 1))

    def test_year_end_carries_a_balance_below_the_cap_in_full(self):
        row = self.balance(
            self.employee("GSL001", date(2026, 1, 5)), 4, 0, marker=date(2026, 12, 1)
        )
        self.daily(date(2027, 2, 1))  # December and January
        self.assertEqual(self.values(row), (2, 6, date(2027, 2, 1)))

    def test_negative_balance_is_recorded_at_year_end(self):
        row = self.balance(
            self.employee("GSL001", date(2026, 1, 5)), -5, 0, marker=date(2026, 12, 1)
        )
        result = self.daily(date(2027, 1, 2))
        self.assertEqual(self.values(row), (-3, 0, date(2027, 1, 1)))
        self.assertIn("NEGATIVE", result["plans"][0].flags)

    def test_second_run_credits_nothing(self):
        row = self.balance(
            self.employee("GSL001", date(2026, 5, 4)), 0, 0, marker=date(2026, 9, 1)
        )
        self.daily(TODAY)
        history = row.history_set.count()
        result = self.daily(TODAY)
        self.assertEqual(self.values(row), (2, 0, date(2026, 10, 1)))
        self.assertEqual(result["saved"], [])
        self.assertEqual(row.history_set.count(), history)

    def test_dry_run_writes_nothing(self):
        row = self.balance(
            self.employee("GSL001", date(2026, 5, 4)), 0, 0, marker=date(2026, 9, 1)
        )
        result = self.daily(TODAY, apply=False)
        self.assertEqual(result["plans"][0].action, "change")
        self.assertEqual(self.values(row), (0, 0, date(2026, 9, 1)))

    def test_change_reason_is_written_to_history(self):
        row = self.balance(
            self.employee("GSL001", date(2026, 5, 4)), 0, 0, marker=date(2026, 9, 1)
        )
        self.daily(TODAY)
        latest = row.history_set.order_by("-history_date").first()
        self.assertEqual(
            latest.history_change_reason, "Monthly accrual: 1 month(s) to Sep 2026"
        )

    def test_rows_off_accrual_are_untouched(self):
        row = self.balance(self.employee("GSL001", date(2026, 5, 4)), 24, 3)
        result = self.daily(date(2027, 1, 2))
        self.assertEqual(result["plans"], [])
        self.assertEqual(self.values(row), (24, 3, None))

    def test_refuses_rows_it_should_not_guess_about(self):
        employee = self.employee("GSL001", date(2026, 5, 4))
        not_first = self.balance(employee, 0, 0, marker=date(2026, 9, 15))
        too_many = self.balance(
            self.employee("GSL002", date(2025, 1, 6)), 0, 0, marker=date(2025, 9, 1)
        )
        over_cap = self.balance(
            self.employee("GSL003", date(2026, 1, 6)), 0, 12, marker=date(2026, 9, 1)
        )
        result = self.daily(TODAY)
        self.assertEqual(
            sorted(plan.action for plan in result["plans"]), ["refuse"] * 3
        )
        self.assertEqual(self.values(not_first), (0, 0, date(2026, 9, 15)))
        self.assertEqual(self.values(too_many), (0, 0, date(2025, 9, 1)))
        self.assertEqual(self.values(over_cap), (0, 12, date(2026, 9, 1)))


class RebuildTests(AccrualTestCase):
    def test_balance_is_accrued_minus_taken(self):
        employee = self.employee("GSL001", date(2026, 6, 1))
        row = self.balance(employee)
        self.approved(employee, date(2026, 8, 10), 3)
        self.apply_rebuild({"GSL001": date(2026, 6, 1)})
        self.assertEqual(self.values(row), (5, 0, date(2026, 10, 1)))  # 8 - 3

    def test_overdraw_is_recorded_as_negative(self):
        employee = self.employee("GSL001", date(2026, 6, 1))
        row = self.balance(employee)
        self.approved(employee, date(2026, 6, 10), 9)
        result = self.apply_rebuild({"GSL001": date(2026, 6, 1)})
        self.assertEqual(self.values(row), (-1, 0, date(2026, 10, 1)))
        self.assertIn("NEGATIVE", result["plans"][0].flags)

    def test_held_rows_are_untouched(self):
        joiner = self.balance(self.employee("GSL001", date(2026, 6, 1)))
        # Assigned before 2026: pre-2026 staff even though on the sheet.
        renewed = self.balance(
            self.employee("GSL002", date(2026, 1, 1)), 24, 4, date(2025, 12, 18)
        )
        not_on_sheet = self.balance(self.employee("GSL003", date(2026, 8, 5)), 24, 0)
        pre_2026 = self.balance(self.employee("GSL004", date(2021, 3, 1)), 20, 6)
        no_date = self.balance(self.employee("GSL005", None), 24, 10)
        inactive = self.balance(
            self.employee("GSL006", date(2026, 6, 1), active=False), 24, 0
        )
        held = [renewed, not_on_sheet, pre_2026, no_date, inactive]
        before = [(self.values(row), row.history_set.count()) for row in held]

        sheet = {"GSL001": date(2026, 6, 1), "GSL002": date(2026, 1, 1)}
        sheet["GSL006"] = date(2026, 6, 1)
        result = self.apply_rebuild(sheet)

        after = [(self.values(row), row.history_set.count()) for row in held]
        self.assertEqual(after, before)
        self.assertEqual(self.values(joiner), (8, 0, date(2026, 10, 1)))
        self.assertEqual(result["missing_from_sheet"], ["GSL003"])
        self.assertEqual(result["saved"], ["GSL001"])

    def test_second_rebuild_changes_nothing(self):
        row = self.balance(self.employee("GSL001", date(2026, 6, 1)))
        sheet = {"GSL001": date(2026, 6, 1)}
        self.apply_rebuild(sheet)
        history = row.history_set.count()
        result = self.rebuild(sheet)
        self.assertEqual([plan.action for plan in result["plans"]], ["unchanged"])
        self.assertEqual(row.history_set.count(), history)

    def test_rebuild_then_daily_does_not_double_count(self):
        row = self.balance(self.employee("GSL001", date(2026, 6, 1)))
        self.apply_rebuild({"GSL001": date(2026, 6, 1)})
        self.daily(TODAY)
        self.assertEqual(self.values(row), (8, 0, date(2026, 10, 1)))
        self.daily(date(2026, 11, 1))  # October
        self.assertEqual(self.values(row), (10, 0, date(2026, 11, 1)))

    def test_refusals_write_nothing(self):
        row = self.balance(self.employee("GSL001", date(2026, 6, 1)))
        sheet = {"GSL001": date(2026, 6, 1)}
        cases = [
            dict(through=date(2026, 9, 29)),  # not a month end
            dict(through=date(2026, 10, 31)),  # month not finished
            dict(sheet={}),  # no sheet
            dict(apply=True),  # no --confirm-rows
            dict(apply=True, confirm_rows=5),  # wrong count
            dict(out_dir=str(settings.BASE_DIR / "tmp-accrual")),  # in the repo
        ]
        for case in cases:
            with self.subTest(case=case):
                kwargs = {"today": TODAY, "out_dir": self.out_dir, **case}
                with self.assertRaises(AccrualRefused):
                    run_rebuild(
                        kwargs.pop("through", THROUGH),
                        kwargs.pop("sheet", sheet),
                        **kwargs,
                    )
        self.assertEqual(self.values(row), (24, 0, None))
        self.assertFalse((settings.BASE_DIR / "tmp-accrual").exists())

    def test_refuses_when_annual_leave_settings_change(self):
        self.balance(self.employee("GSL001", date(2026, 6, 1)))
        for field, value in [("total_days", 21), ("carryforward_max", 5)]:
            with self.subTest(field=field):
                LeaveType.objects.filter(pk=self.annual.pk).update(**{field: value})
                with self.assertRaises(AccrualRefused):
                    self.rebuild({"GSL001": date(2026, 6, 1)})
                with self.assertRaises(AccrualRefused):
                    self.daily(TODAY)
                LeaveType.objects.filter(pk=self.annual.pk).update(
                    total_days=24, carryforward_max=10
                )


class NewBalanceTests(AccrualTestCase):
    def test_new_annual_leave_row_starts_at_zero_on_accrual(self):
        employee = self.employee("GSL001", date(2026, 7, 21))
        row = prepare_new_balance(
            AvailableLeave(
                employee_id=employee, leave_type_id=self.annual, available_days=24
            )
        )
        row.save()
        self.assertEqual(self.values(row), (0, 0, date(2026, 8, 1)))
        # The daily run then credits August and September.
        self.daily(TODAY)
        self.assertEqual(self.values(row), (4, 0, date(2026, 10, 1)))

    def test_without_a_join_date_the_assignment_date_is_used(self):
        employee = self.employee("GSL001", None)
        row = AvailableLeave(employee_id=employee, leave_type_id=self.annual)
        row.assigned_date = date(2026, 9, 25)
        prepare_new_balance(row)
        self.assertEqual(row.last_accrual_date, date(2026, 10, 1))

    def test_other_leave_types_are_unchanged(self):
        sick = LeaveType.objects.create(name="Sick Leave", total_days=14)
        employee = self.employee("GSL001", date(2026, 7, 1))
        row = prepare_new_balance(
            AvailableLeave(employee_id=employee, leave_type_id=sick, available_days=14)
        )
        self.assertEqual((row.available_days, row.last_accrual_date), (14, None))

    def test_bulk_assign_view_uses_the_helper(self):
        sick = LeaveType.objects.create(name="Sick Leave", total_days=14)
        employee = self.employee("GSL001", date(2026, 7, 1))
        admin, _ = make_employee("admin")
        admin.is_superuser = True
        admin.save()
        client = Client()
        client.force_login(admin)
        client.post(
            reverse("assign"),
            {"leave_type_id": [self.annual.pk, sick.pk], "employee_id": [employee.pk]},
            HTTP_HX_REQUEST="true",
        )
        annual = AvailableLeave.objects.get(
            employee_id=employee, leave_type_id=self.annual
        )
        sick_row = AvailableLeave.objects.get(employee_id=employee, leave_type_id=sick)
        self.assertEqual(
            (annual.available_days, annual.total_leave_days, annual.last_accrual_date),
            (0, 0, date(2026, 7, 1)),
        )
        self.assertEqual(
            (sick_row.available_days, sick_row.last_accrual_date), (14, None)
        )


class ResetJobTests(AccrualTestCase):
    def test_reset_and_expiry_skip_accrual_rows(self):
        today = date.today()
        row = self.balance(
            self.employee("GSL001", date(2026, 6, 1)), 8, 3, marker=date(2026, 10, 1)
        )
        AvailableLeave.objects.filter(pk=row.pk).update(
            reset_date=today, expired_date=today
        )
        leave_reset()
        self.assertEqual(self.values(row), (8, 3, date(2026, 10, 1)))

    def test_annual_leave_carry_does_not_expire_for_rows_off_accrual(self):
        today = date.today()
        row = self.balance(self.employee("GSL001", date(2021, 6, 1)), 20, 6)
        AvailableLeave.objects.filter(pk=row.pk).update(expired_date=today)
        leave_reset()
        self.assertEqual(self.values(row), (20, 6, None))
