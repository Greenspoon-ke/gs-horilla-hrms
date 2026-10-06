"""
Set leave balances from a CSV file, with a reason recorded for every change.

Columns (header row required): email, leave_type, available_days,
carryforward_days, reason.

Values are absolute: they replace the current balance, they are not added to
it. Without --apply this is a DRY RUN that prints old -> new per row and saves
nothing. Re-running the same file changes nothing (idempotent).

Each changed balance gets a history row carrying the reason (max 100 chars) and
no user, since this runs outside a web request. Rows with a missing reason, an
unknown employee or leave type, bad numbers, no existing balance, or a
duplicate target are rejected and reported; the rest of the file still runs.

Output identifies people by employee id and file line number only, never by
name or email. Keep the CSV outside the repository (for example in
~/hrms-local/); paths inside the repository are refused.

    python manage.py adjust_leave_balances --csv ~/hrms-local/balances.csv
    python manage.py adjust_leave_balances --csv ~/hrms-local/balances.csv --apply
"""

import csv
import math
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from employee.models import Employee
from leave.models import AvailableLeave, LeaveType

REQUIRED_COLUMNS = {
    "email",
    "leave_type",
    "available_days",
    "carryforward_days",
    "reason",
}
# simple_history stores the change reason in a 100-character column.
MAX_REASON_LENGTH = 100


def _parse_days(value, name):
    """Parse a non-negative, finite number or raise ValueError with a message."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} is not a number") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be zero or more")
    return round(number, 3)


class Command(BaseCommand):
    help = "Set leave balances from a CSV file. Dry run unless --apply is given."

    def add_arguments(self, parser):
        parser.add_argument("--csv", required=True, help="Path to the CSV file.")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Save the changes. Without this flag nothing is written.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        path = self.checked_path(options["csv"])
        self.stdout.write("APPLYING" if apply else "DRY RUN (nothing saved)")

        changed = unchanged = 0
        rejected = []
        seen = set()
        # utf-8-sig tolerates the BOM that Excel adds to CSV exports.
        with open(path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            columns = {(name or "").strip().lower() for name in reader.fieldnames or []}
            missing = REQUIRED_COLUMNS - columns
            if missing:
                raise CommandError(f"Missing column(s): {', '.join(sorted(missing))}")
            for line, raw in enumerate(reader, start=2):
                # A key of None holds cells beyond the header; ignore them.
                record = {
                    key.strip().lower(): (value or "").strip()
                    for key, value in raw.items()
                    if key is not None
                }
                try:
                    outcome = self.process_row(record, seen, apply)
                except ValueError as error:
                    rejected.append(line)
                    self.stdout.write(f"rejected line={line}: {error}")
                    continue
                if outcome is None:
                    unchanged += 1
                    continue
                changed += 1
                self.stdout.write(f"line={line} {outcome}")

        self.stdout.write(
            f"rows changed: {changed}, unchanged: {unchanged}, "
            f"rejected: {len(rejected)}"
        )

    def checked_path(self, raw_path):
        path = Path(raw_path).expanduser().resolve()
        base = Path(settings.BASE_DIR).resolve()
        # The repository is public; a CSV of emails and balances must not live in it.
        if path == base or base in path.parents:
            raise CommandError(
                "Refusing a CSV inside the repository. Keep it outside, "
                "for example in ~/hrms-local/."
            )
        if not path.is_file():
            raise CommandError("CSV file not found.")
        return path

    def process_row(self, record, seen, apply):
        """
        Validate one row and, with apply, save it. Returns a description when
        the balance differs from the file, None when it already matches.
        Raises ValueError to reject the row.
        """
        reason = record["reason"]
        if not reason:
            raise ValueError("reason is required")
        if len(reason) > MAX_REASON_LENGTH:
            raise ValueError(f"reason is longer than {MAX_REASON_LENGTH} characters")
        available = _parse_days(record["available_days"], "available_days")
        carryforward = _parse_days(record["carryforward_days"], "carryforward_days")

        employees = Employee.objects.filter(email__iexact=record["email"])
        if len(employees) != 1:
            raise ValueError("unknown employee")
        leave_types = LeaveType.objects.filter(name__iexact=record["leave_type"])
        if len(leave_types) != 1:
            raise ValueError("unknown or ambiguous leave type")
        employee, leave_type = employees[0], leave_types[0]

        key = (employee.pk, leave_type.pk)
        if key in seen:
            raise ValueError("duplicate employee and leave type in this file")
        seen.add(key)

        with transaction.atomic():
            # _base_manager skips HorillaCompanyManager's company filtering and
            # distinct() handling, which FOR UPDATE does not allow.
            rows = AvailableLeave._base_manager.filter(
                employee_id=employee, leave_type_id=leave_type
            )
            if apply:
                rows = rows.select_for_update()
            row = rows.first()
            if row is None:
                raise ValueError("employee has no balance for this leave type")
            current = (row.available_days, row.carryforward_days)
            if current == (available, carryforward):
                return None
            description = (
                f"employee={employee.pk} row={row.pk} "
                f"available {row.available_days} -> {available} "
                f"carryforward {row.carryforward_days} -> {carryforward}"
            )
            if apply:
                row.available_days = available
                row.carryforward_days = carryforward
                row._change_reason = reason
                # save() recomputes total_leave_days.
                row.save()
            return description
