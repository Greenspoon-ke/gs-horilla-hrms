"""
Monthly accrual for Annual Leave.

Rules (HR decisions D1-D8 of the accrual brief, with the D4', D5' and D7'
revisions):

- Only the leave type named exactly "Annual Leave" accrues. It must grant 24
  days a year and cap carry forward at 10, or nothing runs.
- A row is on accrual when ``AvailableLeave.last_accrual_date`` is set. The
  value is always the 1st of the next month to credit; null rows are never
  touched here.
- 2 days (total_days / 12) per whole month. The joining month counts when the
  employee joined on the 1st-19th, otherwise accrual starts the next month.
  Nothing accrues before 1 January 2026. Month M is credited on the 1st of
  M+1, using the Africa/Nairobi date whatever the server's TIME_ZONE.
- When December is credited the leave year closes: up to 10 days carry
  forward, the rest lapses, and the new year starts at 0.
- The one-off rebuild sets 2026 balances to opening carry + accrued - taken,
  where taken is approved Annual Leave starting on or after 1 January 2026 and
  is spent from carry first. HR's 2026 joiners sheet is the list of 2026
  joiners, and its date replaces the system join date (D7'). Rows with Annual
  Leave assigned before 2026 are pre-2026 staff even when on the sheet. Rows
  not on the sheet wait for HR's opening carry; a null or 2026-08-05 join date
  then needs an override.
- A new Annual Leave row starts at 0 with ``last_accrual_date`` at its first
  accruing month (``prepare_new_balance``), so the daily run catches it up.

Background-safe: nothing here reads a request, user or session, so it runs
from cron. Idempotent: ``last_accrual_date`` moves in the same transaction as
the balance, so a second run credits nothing and a late run catches up.
"""

import csv
import math
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import connection, transaction
from django.db.models import Sum

from leave.models import AvailableLeave, LeaveRequest, LeaveType

ACCRUAL_LEAVE_TYPE_NAME = "Annual Leave"
REQUIRED_TOTAL_DAYS = 24
CARRY_CAP = 10
MONTHS_PER_YEAR = 12
JOIN_CUTOFF_DAY = 20
ACCRUAL_EPOCH = date(2026, 1, 1)
# 2026-08-05 is shared by 61 active staff: an import date, not a real start.
UNTRUSTED_JOIN_DATES = frozenset({date(2026, 8, 5)})
MAX_ROWS = 300
NAIROBI = ZoneInfo("Africa/Nairobi")
# Arbitrary constant; identifies this job's PostgreSQL advisory lock.
ADVISORY_LOCK_KEY = 7_302_026
# simple_history stores the change reason in a 100-character column.
MAX_REASON_LENGTH = 100
NOT_ON_SHEET_2026 = "hold: 2026 join date in system, not on HR sheet"

REPORT_COLUMNS = [
    "code",
    "action",
    "start",
    "months",
    "accrued",
    "taken",
    "opening_carry",
    "current_available",
    "current_carry",
    "proposed_available",
    "proposed_carry",
    "delta",
    "flags",
]
SNAPSHOT_COLUMNS = [
    "row_id",
    "code",
    "available_days",
    "carryforward_days",
    "total_leave_days",
    "reset_date",
    "last_accrual_date",
]


class AccrualRefused(Exception):
    """The run must not go ahead. Raised before anything is written."""


@dataclass
class RowPlan:
    """What one run would do to one balance row."""

    row_id: int
    code: str
    current: tuple
    action: str = "unchanged"  # change | unchanged | skip | refuse
    reason: str = ""
    start: date = None
    months: int = 0
    accrued: float = 0.0
    taken: float = 0.0
    opening_carry: float = 0.0
    proposed: tuple = None
    marker: date = None
    reset_date: date = None
    change_reason: str = ""
    flags: list = field(default_factory=list)

    def stop(self, action, reason):
        self.action, self.reason = action, reason
        return self

    def report_row(self):
        proposed = self.proposed or self.current
        action = f"{self.action}: {self.reason}" if self.reason else self.action
        return [
            self.code,
            action,
            self.start or "",
            self.months,
            self.accrued,
            self.taken,
            self.opening_carry,
            self.current[0],
            self.current[1],
            proposed[0],
            proposed[1],
            round(sum(proposed) - sum(self.current), 3),
            " ".join(self.flags),
        ]


def nairobi_today():
    return datetime.now(NAIROBI).date()


def month_start(day):
    return day.replace(day=1)


def months_between(start, end):
    """Whole months from the month of ``start`` up to, not including, ``end``'s."""
    return max((end.year - start.year) * 12 + end.month - start.month, 0)


def accrual_start(joined):
    """First month that earns leave for someone who joined on ``joined``."""
    first = month_start(joined)
    if joined.day >= JOIN_CUTOFF_DAY:
        first += relativedelta(months=1)
    return max(first, ACCRUAL_EPOCH)


def is_accrual_type(leave_type):
    return leave_type is not None and leave_type.name == ACCRUAL_LEAVE_TYPE_NAME


def prepare_new_balance(row):
    """
    Call on every new AvailableLeave before save() or bulk_create().

    An Annual Leave row starts at 0 days with ``last_accrual_date`` at its
    first accruing month, so the daily run credits the months already earned
    (and refuses more than 12, for a person to check). Without a join date the
    assignment date is used. Rows of other leave types are returned unchanged.
    """
    if not is_accrual_type(row.leave_type_id):
        return row
    joined = join_date(row.employee_id) or row.assigned_date
    if isinstance(joined, datetime):  # assigned_date defaults to timezone.now
        joined = joined.astimezone(NAIROBI).date()
    row.available_days = 0
    row.carryforward_days = 0
    row.last_accrual_date = accrual_start(joined)
    # bulk_create skips save(): set reset and expiry dates and the total here.
    row.pre_save_processing()
    return row


def accrual_leave_type():
    """The one leave type that accrues, after checking HR's fixed settings."""
    types = list(LeaveType._base_manager.filter(name=ACCRUAL_LEAVE_TYPE_NAME))
    if len(types) != 1:
        raise AccrualRefused(
            f'Expected one leave type named "{ACCRUAL_LEAVE_TYPE_NAME}", '
            f"found {len(types)}."
        )
    leave_type = types[0]
    if leave_type.total_days != REQUIRED_TOTAL_DAYS:
        raise AccrualRefused(
            f"total_days is {leave_type.total_days}, expected {REQUIRED_TOTAL_DAYS}."
        )
    # "carryforward expire" is accepted (D5'): its expiry branch is disabled
    # for this type in leave_reset, so the carry never expires.
    if leave_type.carryforward_type not in ("carryforward", "carryforward expire"):
        raise AccrualRefused(
            f"carryforward_type is {leave_type.carryforward_type!r}."
        )
    if leave_type.carryforward_max != CARRY_CAP:
        raise AccrualRefused(
            f"carryforward_max is {leave_type.carryforward_max}, expected {CARRY_CAP}."
        )
    row_count = AvailableLeave._base_manager.filter(leave_type_id=leave_type).count()
    if row_count > MAX_ROWS:
        raise AccrualRefused(
            f"{row_count} balance rows exceed the safety limit of {MAX_ROWS}."
        )
    return leave_type


def employee_code(employee):
    return (employee.badge_id or "").strip() or f"id:{employee.pk}"


def join_date(employee):
    try:
        return employee.employee_work_info.date_joining
    except ObjectDoesNotExist:
        return None


def _requested_days(employee_id, leave_type, status):
    total = LeaveRequest._base_manager.filter(
        employee_id=employee_id,
        leave_type_id=leave_type,
        status=status,
        start_date__gte=ACCRUAL_EPOCH,
    ).aggregate(total=Sum("requested_days"))["total"]
    return round(total or 0, 3)


def _current(row):
    return (round(row.available_days, 3), round(row.carryforward_days, 3))


def plan_rebuild_row(
    row, leave_type, through, sheet, overrides, opening_carry, negative
):
    """Absolute 2026 balance for one row, credited through the month of ``through``."""
    employee = row.employee_id
    code = employee_code(employee)
    plan = RowPlan(row.pk, code, _current(row))
    if not (employee.is_active and row.is_active):
        return plan.stop("skip", "inactive")
    marker = month_start(through) + relativedelta(months=1)
    if row.last_accrual_date and row.last_accrual_date > marker:
        # Re-running an old rebuild would undo months the daily run credited.
        return plan.stop("skip", "already accrued past --through")

    key = code.upper()
    system_joined = join_date(employee)
    joined = overrides.get(key)
    if joined is not None:
        plan.flags.append("JOIN_FROM_OVERRIDE")
    elif key in sheet:
        # HR's sheet is the list of 2026 joiners and its date wins (D7').
        joined = sheet[key]
        if joined != system_joined:
            plan.flags.append("JOIN_FROM_SHEET")
    elif key not in opening_carry:
        # Not a 2026 joiner by HR's sheet, so pre-2026 staff (D7').
        if system_joined and system_joined >= ACCRUAL_EPOCH:
            return plan.stop("skip", NOT_ON_SHEET_2026)
        return plan.stop("skip", "wait: not on HR sheet, needs opening carry")
    else:
        joined = system_joined
        if joined is None:
            return plan.stop("skip", "hold: no join date")
        if joined in UNTRUSTED_JOIN_DATES:
            return plan.stop("skip", f"hold: untrusted join date {joined}")

    # Assigned before 2026 means pre-2026 staff whatever the current join date
    # says (some 2025 staff had their join date overwritten with a 2026 one).
    pre_epoch = joined < ACCRUAL_EPOCH or row.assigned_date < ACCRUAL_EPOCH
    opening = opening_carry.get(key)
    if opening is None:
        if pre_epoch:
            return plan.stop("skip", "wait: needs opening carry from HR")
        opening = 0.0
    if opening > CARRY_CAP:
        return plan.stop("refuse", f"opening carry {opening} above cap {CARRY_CAP}")

    plan.start = accrual_start(joined)
    plan.months = months_between(plan.start, marker)
    plan.accrued = round(plan.months * leave_type.total_days / MONTHS_PER_YEAR, 3)
    if plan.accrued > leave_type.total_days:
        return plan.stop("refuse", "accrued above one year's entitlement")
    plan.taken = _requested_days(employee.pk, leave_type, "approved")
    plan.opening_carry = opening

    from_carry = min(opening, plan.taken)  # carry is spent first (approve view)
    carry = round(opening - from_carry, 3)
    available = round(plan.accrued - (plan.taken - from_carry), 3)
    if available < 0:
        plan.flags.append("NEGATIVE")
        if negative == "zero":
            available = 0.0
    if _requested_days(employee.pk, leave_type, "requested") > available + carry:
        plan.flags.append("PENDING_EXCEEDS")

    plan.proposed = (available, carry)
    # A future joiner must not be credited for months before they start.
    plan.marker = max(marker, plan.start)
    plan.change_reason = (
        f"Accrual rebuild to {through.isoformat()}: {plan.months} months, "
        f"{plan.taken:g} taken, opening carry {opening:g}"
    )[:MAX_REASON_LENGTH]
    if plan.proposed == plan.current and row.last_accrual_date == plan.marker:
        return plan  # unchanged: no save, so no history or audit rows
    plan.action = "change"
    return plan


def plan_daily_row(row, leave_type, today):
    """Credit every whole month due up to ``today`` and close any finished year."""
    employee = row.employee_id
    plan = RowPlan(row.pk, employee_code(employee), _current(row))
    if not (employee.is_active and row.is_active):
        return plan.stop("skip", "inactive")
    last = row.last_accrual_date
    if last.day != 1:
        return plan.stop("refuse", f"last_accrual_date {last} is not a 1st")
    plan.months = months_between(last, month_start(today))
    if plan.months == 0:
        return plan
    # More than a year due means the marker was edited or the job was off for
    # a year; a person should look before a year's leave is granted at once.
    if plan.months > MONTHS_PER_YEAR:
        return plan.stop("refuse", f"{plan.months} months due")
    if row.carryforward_days > CARRY_CAP:
        return plan.stop(
            "refuse", f"carry {row.carryforward_days} above cap {CARRY_CAP}"
        )

    rate = leave_type.total_days / MONTHS_PER_YEAR
    available, carry = row.available_days, row.carryforward_days
    closed = []
    for _ in range(plan.months):
        available += rate
        last += relativedelta(months=1)
        if last.month == 1:  # December was just credited: close the year
            balance = available + carry
            if balance >= 0:
                carry, available = min(CARRY_CAP, balance), 0.0
            else:
                # D8 "record": the debt stays visible in available_days;
                # a negative carry would be zeroed by pre_save_processing.
                carry, available = 0.0, balance
                plan.flags.append("NEGATIVE")
            closed.append(str(last.year - 1))
            plan.reset_date = date(last.year + 1, 1, 1)
    plan.accrued = round(plan.months * rate, 3)
    plan.proposed = (round(available, 3), round(carry, 3))
    plan.marker = last
    credited_to = last - relativedelta(months=1)
    reason = f"Monthly accrual: {plan.months} month(s) to {credited_to:%b %Y}"
    if closed:
        reason += f"; {', '.join(closed)} closed, carry {plan.proposed[1]:g}"
    plan.change_reason = reason[:MAX_REASON_LENGTH]
    plan.action = "change"
    return plan


def _candidate_rows(leave_type, enrolled_only):
    rows = AvailableLeave._base_manager.filter(leave_type_id=leave_type)
    if enrolled_only:
        rows = rows.filter(last_accrual_date__isnull=False)
    return list(rows.select_related("employee_id").order_by("pk"))


def _apply_plans(plans, replan):
    """Save each planned change under a row lock, re-checking it first."""
    saved, stale = [], []
    for plan in plans:
        with transaction.atomic():
            # _base_manager skips HorillaCompanyManager's distinct() handling,
            # which FOR UPDATE does not allow; of=self keeps the lock on the row.
            row = (
                AvailableLeave._base_manager.select_related("employee_id")
                .select_for_update(of=("self",))
                .get(pk=plan.row_id)
            )
            fresh = replan(row)
            expected = ("change", plan.proposed, plan.marker)
            if (fresh.action, fresh.proposed, fresh.marker) != expected:
                stale.append(plan.code)  # changed since the plan was made
                continue
            row.available_days, row.carryforward_days = fresh.proposed
            row.last_accrual_date = fresh.marker
            if fresh.reset_date:
                row.reset_date = fresh.reset_date
            row._change_reason = fresh.change_reason
            row.save()  # recomputes total_leave_days, clamped at 0 (D8)
            saved.append(plan.code)
    return saved, stale


@contextmanager
def advisory_lock():
    """One writer at a time, across processes and servers sharing the database."""
    if connection.vendor != "postgresql":
        raise AccrualRefused("Writing requires PostgreSQL (advisory lock).")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [ADVISORY_LOCK_KEY])
        acquired = cursor.fetchone()[0]
    if not acquired:
        raise AccrualRefused("Another accrual run holds the lock. Nothing written.")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [ADVISORY_LOCK_KEY])


def outside_repo(raw_path):
    path = Path(raw_path).expanduser().resolve()
    base = Path(settings.BASE_DIR).resolve()
    # The repository is public: no balances, codes or snapshots inside it.
    if path == base or base in path.parents:
        raise AccrualRefused(f"Refusing a path inside the repository: {raw_path}")
    return path


def prepare_out_dir(raw_path):
    path = outside_repo(raw_path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def write_csv(path, header, rows):
    # O_EXCL: never overwrite an earlier report or snapshot. 0o600: owner only.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def write_snapshot(out_dir, leave_type, stamp):
    """Current values of every row of the type, written before any change."""
    rows = (
        AvailableLeave._base_manager.filter(leave_type_id=leave_type)
        .select_related("employee_id")
        .order_by("pk")
    )
    return write_csv(
        out_dir / f"accrual-snapshot-{stamp}.csv",
        SNAPSHOT_COLUMNS,
        [
            [
                row.pk,
                employee_code(row.employee_id),
                row.available_days,
                row.carryforward_days,
                row.total_leave_days,
                row.reset_date or "",
                row.last_accrual_date or "",
            ]
            for row in rows
        ],
    )


def read_code_file(raw_path, value_column, parse):
    """
    Read a "code,<value_column>" CSV into {CODE: value}; a bad line refuses.
    The code column may also be called employee_code, as on HR's sheet.
    """
    path = outside_repo(raw_path)
    if not path.is_file():
        raise AccrualRefused(f"File not found: {raw_path}")
    values = {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = {(name or "").strip().lower() for name in reader.fieldnames or []}
        code_column = "code" if "code" in columns else "employee_code"
        if not {code_column, value_column} <= columns:
            raise AccrualRefused(f"{path.name} needs columns: code, {value_column}")
        for line, raw in enumerate(reader, start=2):
            record = {
                (key or "").strip().lower(): (value or "").strip()
                for key, value in raw.items()
                if key
            }
            code = record[code_column].upper()
            if not code:
                raise AccrualRefused(f"{path.name} line {line}: empty code")
            if code in values:
                raise AccrualRefused(f"{path.name} line {line}: duplicate {code}")
            try:
                values[code] = parse(record[value_column])
            except ValueError as error:
                raise AccrualRefused(f"{path.name} line {line}: {error}") from None
    return values


def parse_carry(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("opening_carry must be zero or more")
    return round(number, 3)


def parse_start(value):
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("start_date must be YYYY-MM-DD") from None


def parse_sheet_date(value):
    try:
        joined = date.fromisoformat(value)
    except ValueError:
        raise ValueError("join_date must be YYYY-MM-DD") from None
    if joined.year != ACCRUAL_EPOCH.year:
        raise ValueError(f"join_date {joined} is not in {ACCRUAL_EPOCH.year}")
    return joined


def _stamp():
    # Microseconds: two runs in the same second must not collide (O_EXCL).
    return datetime.now(NAIROBI).strftime("%Y%m%d-%H%M%S-%f")


def run_daily(apply=False, out_dir="~/hrms-local", today=None):
    """Credit due months on every enrolled row. Dry run unless ``apply``."""
    today = today or nairobi_today()
    leave_type = accrual_leave_type()
    plans = [
        plan_daily_row(row, leave_type, today)
        for row in _candidate_rows(leave_type, enrolled_only=True)
    ]
    changes = [plan for plan in plans if plan.action == "change"]
    result = {
        "mode": "daily",
        "today": today,
        "plans": plans,
        "saved": [],
        "stale": [],
        "snapshot": None,
    }
    if apply and changes:
        directory = prepare_out_dir(out_dir)
        with advisory_lock():
            result["snapshot"] = write_snapshot(directory, leave_type, _stamp())
            result["saved"], result["stale"] = _apply_plans(
                changes, lambda row: plan_daily_row(row, leave_type, today)
            )
    return result


def run_rebuild(
    through,
    sheet,
    apply=False,
    out_dir="~/hrms-local",
    overrides=None,
    opening_carry=None,
    negative="record",
    confirm_rows=None,
    today=None,
):
    """One-off: set 2026 balances from the rules, credited through ``through``."""
    today = today or nairobi_today()
    if through != month_start(through) + relativedelta(months=1, days=-1):
        raise AccrualRefused("--through must be the last day of a month.")
    if through.year != ACCRUAL_EPOCH.year:
        raise AccrualRefused(f"--through must be in {ACCRUAL_EPOCH.year}.")
    if month_start(through) >= month_start(today):
        raise AccrualRefused("--through must be a finished month (Nairobi date).")
    if negative not in ("record", "zero"):
        raise AccrualRefused("--negative must be record or zero.")
    if not sheet:
        raise AccrualRefused("The HR joiners sheet is required (D7').")
    overrides, opening_carry = overrides or {}, opening_carry or {}

    leave_type = accrual_leave_type()
    rows = _candidate_rows(leave_type, enrolled_only=False)
    known = {employee_code(row.employee_id).upper() for row in rows}

    def replan(row):
        return plan_rebuild_row(
            row, leave_type, through, sheet, overrides, opening_carry, negative
        )

    plans = [replan(row) for row in rows]
    changes = [plan for plan in plans if plan.action == "change"]
    if apply and confirm_rows != len(changes):
        raise AccrualRefused(
            f"--confirm-rows must equal the {len(changes)} rows this run would "
            "change (see the dry run). Nothing written."
        )
    stamp = _stamp()
    directory = prepare_out_dir(out_dir)
    result = {
        "mode": "rebuild",
        "today": today,
        "plans": plans,
        "saved": [],
        "stale": [],
        "snapshot": None,
        "unmatched_codes": sorted(
            (set(sheet) | set(overrides) | set(opening_carry)) - known
        ),
        "missing_from_sheet": [
            plan.code for plan in plans if plan.reason == NOT_ON_SHEET_2026
        ],
    }

    def write_report(kind):
        return write_csv(
            directory / f"accrual-rebuild-{stamp}-{kind}.csv",
            REPORT_COLUMNS,
            [plan.report_row() for plan in plans],
        )

    if not apply:
        result["report"] = write_report("dry")
        return result
    # Report, snapshot and writes only once this run holds the lock, so a
    # refused run leaves no "apply" files behind.
    with advisory_lock():
        result["report"] = write_report("apply")
        result["snapshot"] = write_snapshot(directory, leave_type, stamp)
        result["saved"], result["stale"] = _apply_plans(changes, replan)
    return result
