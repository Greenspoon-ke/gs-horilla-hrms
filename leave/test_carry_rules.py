"""
Tests for the carry-over rules: exempt teams' year-end cap, the optional
30 June expiry, booking against exempt carry, API approvals spending carry
first, and the reset job's connection cleanup.

Synthetic data only. The test runner never touches the real database.
"""

from datetime import date
from unittest import mock

from django.test import override_settings

from base.models import Department
from employee.models import EmployeeWorkInformation
from horilla_api.api_views.leave.views import (
    LeaveRequestApproveAPIView,
    LeaveRequestBulkApproveDeleteAPIview,
    spend_carry_first,
)
from leave import scheduler
from leave.accrual import AccrualRefused, booking_carry_cap, carry_rules
from leave.models import LeaveRequest
from leave.test_accrual import AccrualTestCase

TEAMS = ["Packing", "Private Label"]


class CarryRulesTestCase(AccrualTestCase):
    def setUp(self):
        super().setUp()
        # Department.save passes its arguments on to clean(), so objects.create
        # (which passes force_insert) fails; a plain save() works.
        self.packing = Department(department="Packing").save()
        Department(department="Private Label").save()
        self.finance = Department(department="Finance").save()

    def in_department(self, employee, department):
        EmployeeWorkInformation.objects.filter(employee_id=employee).update(
            department_id=department
        )
        return employee

    def year_end_row(self, code, department, available):
        employee = self.in_department(
            self.employee(code, date(2026, 1, 5)), department
        )
        return self.balance(employee, available, 0, marker=date(2026, 12, 1))


class ExemptTeamCapTests(CarryRulesTestCase):
    def test_off_by_default_everyone_is_capped_at_10(self):
        row = self.year_end_row("GSL001", self.packing, 15)
        self.daily(date(2027, 1, 2))
        self.assertEqual(self.values(row)[1], 10)

    @override_settings(LEAVE_CARRY_EXEMPT_DEPARTMENTS=TEAMS)
    def test_exempt_team_keeps_every_day_other_teams_keep_10(self):
        packer = self.year_end_row("GSL001", self.packing, 15)
        finance = self.year_end_row("GSL002", self.finance, 15)
        self.daily(date(2027, 1, 2))
        self.assertEqual(self.values(packer), (0, 17, date(2027, 1, 1)))
        self.assertEqual(self.values(finance), (0, 10, date(2027, 1, 1)))

    @override_settings(
        LEAVE_CARRY_EXEMPT_DEPARTMENTS=TEAMS, LEAVE_CARRY_EXEMPT_CAP="15"
    )
    def test_exempt_team_with_a_higher_cap(self):
        row = self.year_end_row("GSL001", self.packing, 18)
        self.daily(date(2027, 1, 2))
        self.assertEqual(self.values(row)[1], 15)

    @override_settings(LEAVE_CARRY_EXEMPT_DEPARTMENTS=TEAMS)
    def test_exempt_carry_above_10_is_not_refused_next_run(self):
        employee = self.in_department(
            self.employee("GSL001", date(2026, 1, 5)), self.packing
        )
        row = self.balance(employee, 0, 14, marker=date(2027, 1, 1))
        result = self.daily(date(2027, 2, 1))
        self.assertEqual(result["plans"][0].action, "change")
        self.assertEqual(self.values(row), (2, 14, date(2027, 2, 1)))

    def test_settings_mistakes_refuse_the_run(self):
        self.year_end_row("GSL001", self.packing, 15)
        for settings in [
            {"LEAVE_CARRY_EXEMPT_DEPARTMENTS": ["Packng"]},
            {"LEAVE_CARRY_EXEMPT_CAP": "five"},
            {"LEAVE_CARRY_EXEMPT_CAP": "5"},
        ]:
            with self.subTest(settings=settings), override_settings(**settings):
                with self.assertRaises(AccrualRefused):
                    carry_rules()
                with self.assertRaises(AccrualRefused):
                    self.daily(date(2027, 1, 2))


class June30ExpiryTests(CarryRulesTestCase):
    def june_row(self, department):
        employee = self.in_department(
            self.employee("GSL001", date(2025, 3, 3)), department
        )
        return self.balance(employee, 3, 4, marker=date(2026, 6, 1))

    def test_off_by_default_carry_stays(self):
        row = self.june_row(self.finance)
        self.daily(date(2026, 7, 2))
        self.assertEqual(self.values(row), (5, 4, date(2026, 7, 1)))

    @override_settings(LEAVE_CARRY_EXPIRES_30_JUNE=True)
    def test_unused_carry_lapses_when_june_is_credited(self):
        row = self.june_row(self.finance)
        result = self.daily(date(2026, 7, 2))
        self.assertEqual(self.values(row), (5, 0, date(2026, 7, 1)))
        self.assertIn("CARRY_EXPIRED", result["plans"][0].flags)
        latest = row.history_set.order_by("-history_date").first()
        self.assertIn("carry 4 expired 30 Jun", latest.history_change_reason)

    @override_settings(LEAVE_CARRY_EXPIRES_30_JUNE=True)
    def test_nothing_lapses_before_june_is_credited(self):
        row = self.june_row(self.finance)
        self.daily(date(2026, 6, 30))
        self.assertEqual(self.values(row), (3, 4, date(2026, 6, 1)))

    @override_settings(
        LEAVE_CARRY_EXPIRES_30_JUNE=True,
        LEAVE_CARRY_EXEMPT_DEPARTMENTS=TEAMS,
        LEAVE_CARRY_EXEMPT_SKIP_EXPIRY=True,
    )
    def test_exempt_team_can_skip_the_expiry(self):
        row = self.june_row(self.packing)
        self.daily(date(2026, 7, 2))
        self.assertEqual(self.values(row), (5, 4, date(2026, 7, 1)))


class BookingCapTests(CarryRulesTestCase):
    def accrual_row(self, department, carry):
        employee = self.in_department(
            self.employee("GSL001", date(2025, 3, 3)), department
        )
        return self.balance(employee, 2, carry, marker=date(2026, 10, 1))

    def test_type_cap_by_default(self):
        self.assertEqual(booking_carry_cap(self.accrual_row(self.packing, 14)), 10)

    @override_settings(LEAVE_CARRY_EXEMPT_DEPARTMENTS=TEAMS)
    def test_exempt_team_on_accrual_can_book_all_its_carry(self):
        row = self.accrual_row(self.packing, 14)
        self.assertGreaterEqual(booking_carry_cap(row), 14)

    @override_settings(LEAVE_CARRY_EXEMPT_DEPARTMENTS=TEAMS)
    def test_other_teams_and_rows_off_accrual_keep_the_type_cap(self):
        self.assertEqual(booking_carry_cap(self.accrual_row(self.finance, 4)), 10)
        employee = self.in_department(
            self.employee("GSL002", date(2025, 3, 3)), self.packing
        )
        self.assertEqual(booking_carry_cap(self.balance(employee, 24, 3)), 10)


class ApiCarryFirstTests(CarryRulesTestCase):
    def request_for(self, row, days):
        leave_request = LeaveRequest(
            employee_id=row.employee_id,
            leave_type_id=self.annual,
            start_date=date(2026, 11, 2),
            end_date=date(2026, 11, 2),
            description="test",
        )
        leave_request.requested_days = days
        return leave_request

    def test_carried_days_are_spent_first(self):
        row = self.balance(self.employee("GSL001", date(2025, 3, 3)), 10, 3)
        leave_request = self.request_for(row, 5)
        spend_carry_first(leave_request, row)
        row.refresh_from_db()
        self.assertEqual((row.available_days, row.carryforward_days), (8, 0))
        self.assertEqual(
            (
                leave_request.approved_carryforward_days,
                leave_request.approved_available_days,
            ),
            (3, 2),
        )

    def test_request_within_carry_leaves_this_years_days(self):
        row = self.balance(self.employee("GSL001", date(2025, 3, 3)), 10, 3)
        spend_carry_first(self.request_for(row, 2), row)
        row.refresh_from_db()
        self.assertEqual((row.available_days, row.carryforward_days), (10, 1))

    def test_single_and_bulk_api_approvals_use_it(self):
        views = (LeaveRequestApproveAPIView, LeaveRequestBulkApproveDeleteAPIview)
        for number, view in enumerate(views, start=1):
            with self.subTest(view=view.__name__):
                row = self.balance(
                    self.employee(f"GSL00{number}", date(2025, 3, 3)), 10, 3
                )
                view().leave_approve_calculation(self.request_for(row, 4), row)
                row.refresh_from_db()
                self.assertEqual((row.available_days, row.carryforward_days), (9, 0))


class ResetJobConnectionTests(AccrualTestCase):
    def test_stale_connections_are_dropped_before_and_after_each_run(self):
        with mock.patch.object(scheduler, "close_stale_connections") as cleanup:
            scheduler.leave_reset()
        self.assertEqual(cleanup.call_count, 2)

    def test_cleanup_still_runs_when_the_run_fails(self):
        with mock.patch.object(
            scheduler, "close_stale_connections"
        ) as cleanup, mock.patch.object(
            scheduler, "_reset_balances", side_effect=RuntimeError("db gone")
        ):
            with self.assertRaises(RuntimeError):
                scheduler.leave_reset()
        self.assertEqual(cleanup.call_count, 2)

    def test_a_connection_inside_a_transaction_is_left_alone(self):
        # Tests run inside a transaction: closing it would break everything.
        scheduler.close_stale_connections()
        self.assertTrue(LeaveRequest.objects.count() >= 0)

    def test_a_connection_outside_a_transaction_is_checked(self):
        idle = mock.Mock(in_atomic_block=False)
        busy = mock.Mock(in_atomic_block=True)
        with mock.patch.object(scheduler, "connections") as handler:
            handler.all.return_value = [idle, busy]
            scheduler.close_stale_connections()
        idle.close_if_unusable_or_obsolete.assert_called_once_with()
        busy.close_if_unusable_or_obsolete.assert_not_called()
