"""
Tests for the leave clean-up data step (leave_cleanup_setup).

Synthetic data only. The test runner never touches the real database.
"""

from datetime import date
from io import StringIO

from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import TestCase

from horilla.horilla_middlewares import _thread_locals
from leave.methods import HR_LEAVE_GROUP
from leave.models import LeaveType
from leave.tests import add_missing_user_column, make_employee


class CleanupSetupTests(TestCase):
    @classmethod
    def setUpClass(cls):
        add_missing_user_column()
        super().setUpClass()

    def setUp(self):
        _thread_locals.request = None
        self.addCleanup(setattr, _thread_locals, "request", None)
        # Annual Leave as configured before the clean-up.
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
        self.hr_user, _ = make_employee("hr")
        self.hr_user.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="leave",
                codename__in=[
                    "view_leaveallocationrequest",
                    "add_leaveallocationrequest",
                    "delete_leaveallocationrequest",
                ],
            )
        )

    def run_setup(self, *extra):
        out = StringIO()
        call_command("leave_cleanup_setup", "--members", str(self.hr_user.pk), *extra, stdout=out)
        return out.getvalue()

    def test_dry_run_changes_nothing(self):
        output = self.run_setup()
        self.annual.refresh_from_db()
        self.assertTrue(self.annual.reset)
        self.assertFalse(Group.objects.filter(name=HR_LEAVE_GROUP).exists())
        self.assertIn("would change: 5", output)

    def test_apply_then_rerun_is_a_no_op(self):
        self.run_setup("--apply")
        self.annual.refresh_from_db()
        self.assertEqual(
            (self.annual.reset, self.annual.carryforward_type),
            (False, "no carryforward"),
        )
        self.assertIsNone(self.annual.carryforward_expire_date)
        group = Group.objects.get(name=HR_LEAVE_GROUP)
        self.assertEqual(group.permissions.count(), 5)
        self.assertTrue(self.hr_user.groups.filter(pk=group.pk).exists())
        # Moved to the group; the delete permission stays a direct grant.
        direct = set(self.hr_user.user_permissions.values_list("codename", flat=True))
        self.assertEqual(direct, {"delete_leaveallocationrequest"})
        self.assertIn("changed: 0", self.run_setup("--apply"))
