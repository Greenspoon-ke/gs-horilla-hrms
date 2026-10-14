"""
Data settings for HR's 2026 leave clean-up. Dry run unless --apply.

1. Annual Leave: reset=False and "no carryforward" (accrue_leave refuses to
   run otherwise). Saved through LeaveType.save, which clears the carry-forward
   expiry date, so the 20-second leave_reset job no longer touches the type.
2. Group "HR – Leave" with view/add/change leave allocation requests and
   view/change available leave.
3. --members: user ids to put in the group. The group's permissions are then
   removed from those users' direct grants (moved to the group); any other
   direct leave permission they hold is listed, not changed.

    python manage.py leave_cleanup_setup --members 4,6,92
    python manage.py leave_cleanup_setup --members 4,6,92 --apply

Safe to re-run: a second --apply changes nothing. Output shows ids only.
The dry run makes the same changes inside a transaction and rolls it back,
so it shows exactly what --apply would do.
"""

from django.contrib.auth.models import Group, Permission, User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from leave.accrual import ACCRUAL_LEAVE_TYPE_NAME, REQUIRED_CARRY_TYPE
from leave.methods import HR_LEAVE_GROUP
from leave.models import LeaveType

GROUP_PERMISSIONS = [
    "view_leaveallocationrequest",
    "add_leaveallocationrequest",
    "change_leaveallocationrequest",
    "view_availableleave",
    "change_availableleave",
]


def parse_ids(raw):
    try:
        return sorted({int(part) for part in raw.split(",") if part.strip()})
    except ValueError:
        raise CommandError("--members must be user ids, e.g. 4,6,92") from None


class Command(BaseCommand):
    help = "Leave clean-up data settings. Dry run unless --apply is given."

    def add_arguments(self, parser):
        parser.add_argument("--members", default="", help="User ids, e.g. 4,6,92")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Save changes. Without it the database is not written.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        member_ids = parse_ids(options["members"])
        types = list(LeaveType._base_manager.filter(name=ACCRUAL_LEAVE_TYPE_NAME))
        if len(types) != 1:
            raise CommandError(
                f'Expected one leave type named "{ACCRUAL_LEAVE_TYPE_NAME}", '
                f"found {len(types)}."
            )
        members = list(User.objects.filter(pk__in=member_ids, is_active=True))
        missing = sorted(set(member_ids) - {user.pk for user in members})
        if missing:
            raise CommandError(f"No active user with id(s): {missing}")
        permissions = list(
            Permission.objects.filter(
                content_type__app_label="leave", codename__in=GROUP_PERMISSIONS
            )
        )
        if len(permissions) != len(GROUP_PERMISSIONS):
            raise CommandError("A leave permission is missing; run migrate first.")

        write = self.stdout.write
        write(f"{'APPLY' if apply else 'DRY RUN (database unchanged)'}: leave clean-up")
        with transaction.atomic():
            changes = self.leave_type(types[0], write)
            changes += self.group(permissions, members, write)
            if not apply:
                transaction.set_rollback(True)
        write(f"{'changed' if apply else 'would change'}: {changes}")

    @staticmethod
    def leave_type(leave_type, write):
        def describe():
            return (
                f"reset={leave_type.reset} carry={leave_type.carryforward_type!r} "
                f"expiry={leave_type.carryforward_expire_date or '-'}"
            )

        before = describe()
        if not leave_type.reset and leave_type.carryforward_type == REQUIRED_CARRY_TYPE:
            write(f"  leave type {leave_type.pk}: already set")
            return 0
        leave_type.reset = False
        leave_type.carryforward_type = REQUIRED_CARRY_TYPE
        leave_type.save()  # also clears carryforward_expire_date
        write(f"  leave type {leave_type.pk}: {before} -> {describe()}")
        return 1

    @staticmethod
    def group(permissions, members, write):
        changes = 0
        group, created = Group.objects.get_or_create(name=HR_LEAVE_GROUP)
        if created:
            write(f"  group {group.pk}: created")
            changes += 1
        missing = [perm for perm in permissions if perm not in group.permissions.all()]
        if missing:
            group.permissions.add(*missing)
            write(f"  group {group.pk}: add {sorted(p.codename for p in missing)}")
            changes += 1
        for user in members:
            if not user.groups.filter(pk=group.pk).exists():
                user.groups.add(group)
                write(f"  user {user.pk}: added to group")
                changes += 1
            # Moved to the group: drop the same permissions granted directly.
            direct = user.user_permissions.filter(pk__in=[p.pk for p in permissions])
            if direct.exists():
                write(
                    f"  user {user.pk}: remove direct "
                    f"{sorted(direct.values_list('codename', flat=True))}"
                )
                user.user_permissions.remove(*direct)
                changes += 1
            others = user.user_permissions.filter(
                content_type__app_label="leave"
            ).values_list("codename", flat=True)
            if others:
                write(f"  user {user.pk}: keeps direct {sorted(others)} (not changed)")
        return changes
