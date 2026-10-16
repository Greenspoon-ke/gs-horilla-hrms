"""
Read-only preview of HR's 2026 leave clean-up against a live database
(leave_cleanup_preview). Without saving anything it computes what the
production steps would do, in their order:

1. leave_cleanup_setup: Annual Leave's settings as they will be, set on an
   in-memory copy (leave.accrual.set_accrual_type); the row is not changed,
   so this works against the unchanged production type.
2. convert_leave_allocations with HR's decisions (leave.conversion.plan_all).
3. record_hr_leave with HR's records (leave.hr_records.plan_all).
3b. record_hr_leave --past-pending: pending requests that ended before today
   rejected as not taken (leave.past_pending.plan_removals).
4. accrue_leave --rebuild with the opening carry, overrides and exclusions
   (leave.accrual.plan_rebuild_row), counting the leave requests steps 2-3b
   would create, change or remove as if they were saved.

Two guards keep it from writing: it refuses to start unless the connection is
read-only (PGOPTIONS='-c default_transaction_read_only=on'), and any model
save or delete while it runs raises. Output is two files, mode 600, outside
the repository: a per-row CSV and a summary with counts and employee codes.
"""

import os
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

from django.db import connection
from django.db.models.signals import m2m_changed, pre_delete, pre_save

from base.models import Department
from employee.models import Employee
from leave import conversion, hr_records, past_pending
from leave.accrual import (
    ACCRUAL_EPOCH,
    ACCRUAL_LEAVE_TYPE_NAME,
    EXCLUDED,
    UNTRUSTED_JOIN_DATES,
    _candidate_rows,
    _stamp,
    carry_rules,
    check_accrual_type,
    check_rebuild_args,
    department_name,
    employee_code,
    find_accrual_type,
    join_date,
    nairobi_today,
    plan_rebuild_row,
    prepare_out_dir,
    set_accrual_type,
    write_csv,
)
from leave.models import LeaveAllocationRequest, LeaveRequest

COUNTED = ("approved", "requested")
CSV_COLUMNS = [
    "code",
    "action",
    "exempt_team",
    "current_available",
    "current_carry",
    "earned",
    "granted",
    "taken_existing",
    "taken_converted_recorded",
    "opening_carry",
    "new_available",
    "new_carry",
    "change",
    "flags",
]


class PreviewRefused(Exception):
    """The preview must not run, or tried to write."""


def assert_read_only():
    if connection.vendor != "postgresql":
        raise PreviewRefused("The preview needs PostgreSQL.")
    with connection.cursor() as cursor:
        cursor.execute("SHOW default_transaction_read_only")
        default = cursor.fetchone()[0]
        cursor.execute("SHOW transaction_read_only")
        current = cursor.fetchone()[0]
    if (default, current) != ("on", "on"):
        raise PreviewRefused(
            "The database connection is writable. Run with "
            "PGOPTIONS='-c default_transaction_read_only=on'."
        )


@contextmanager
def writes_refused():
    """
    Backstop to the read-only connection: a save or delete here is a bug, so
    raise before it reaches the database (other pre_save receivers may run
    first, but the read-only connection refuses their writes too).
    """

    def refuse(sender, **kwargs):
        raise PreviewRefused(f"The preview tried to write {sender.__name__}.")

    signals = (pre_save, pre_delete, m2m_changed)
    for signal in signals:
        signal.connect(refuse, weak=False, dispatch_uid="leave_cleanup_preview")
    try:
        yield
    finally:
        for signal in signals:
            signal.disconnect(dispatch_uid="leave_cleanup_preview")


@dataclass(frozen=True)
class Planned:
    """A leave request as it will be after steps 2-3 (saved or not)."""

    employee_pk: int
    code: str
    active: bool
    leave_type: str
    status: str
    start: date
    end: date
    days: float


def _changed_ids(conversion_plans, record_plans):
    """Saved requests steps 2-3 would change or delete."""
    return {
        plan.leave_request_id
        for plan in [*conversion_plans, *record_plans]
        if plan.leave_request_id and plan.action in ("update", "unconvert")
    }


def _planned(conversion_plans, record_plans):
    """Requests steps 2-3 would create or change, as they will be saved."""
    for plan in conversion_plans:
        if plan.action in ("create", "update"):
            yield Planned(
                plan.employee_id,
                plan.code,
                "EMP_INACTIVE" not in plan.flags,
                plan.leave_type,
                plan.status,
                plan.start,
                plan.end,
                plan.computed_days,
            )
    for plan in record_plans:
        if plan.action in ("create", "update"):
            yield Planned(
                plan.employee.pk,
                plan.code,
                plan.employee.is_active,
                plan.leave_type.name,
                plan.status,
                plan.start,
                plan.end,
                plan.computed_days,
            )


def _saved(leave_request):
    return Planned(
        leave_request.employee_id_id,
        employee_code(leave_request.employee_id),
        leave_request.employee_id.is_active,
        leave_request.leave_type_id.name,
        leave_request.status,
        leave_request.start_date,
        leave_request.end_date,
        leave_request.requested_days,
    )


def unsaved_requests(conversion_plans, record_plans, removals=()):
    """
    {employee pk: {status: days}}: how the rebuild's counts of approved and
    requested Annual Leave from 2026 (accrual._requested_days) move once steps
    2-3b are saved. Replaced, deleted or removed requests are taken off first.
    """
    deltas = defaultdict(lambda: defaultdict(float))

    def add(request, sign):
        if (
            request.leave_type == ACCRUAL_LEAVE_TYPE_NAME
            and request.status in COUNTED
            and request.start >= ACCRUAL_EPOCH
        ):
            deltas[request.employee_pk][request.status] += sign * request.days

    for old in LeaveRequest._base_manager.filter(
        pk__in=_changed_ids(conversion_plans, record_plans)
    ).select_related("employee_id", "leave_type_id"):
        add(_saved(old), -1)
    for new in _planned(conversion_plans, record_plans):
        add(new, 1)
    # Step 3b never touches a request steps 2-3 made or changed (markers), so
    # nothing here is taken off twice.
    for removal in removals:
        add(
            Planned(
                removal.employee_id, removal.code, True, removal.leave_type,
                "requested", removal.start, removal.end, removal.days,
            ),
            -1,
        )
    return {pk: dict(days) for pk, days in deltas.items()}


def requests_after(queryset, conversion_plans, record_plans):
    """The queryset's requests plus the planned ones, as they will be."""
    saved = queryset.exclude(
        pk__in=_changed_ids(conversion_plans, record_plans)
    ).select_related("employee_id", "leave_type_id")
    return [_saved(r) for r in saved] + list(_planned(conversion_plans, record_plans))


def describe_type(leave_type):
    return (
        f"reset={leave_type.reset} carryforward_type={leave_type.carryforward_type!r} "
        f"carryforward_max={leave_type.carryforward_max} "
        f"expiry={leave_type.carryforward_expire_date or '-'}"
    )


def run_preview(
    through,
    sheet,
    out_dir,
    since,
    overrides=None,
    opening_carry=None,
    excluded=None,
    decisions_path=None,
    records_path=None,
    negative="record",
    today=None,
):
    """Compute the whole clean-up in memory; write only the two output files."""
    assert_read_only()
    today = today or nairobi_today()
    check_rebuild_args(through, sheet, negative, today)
    overrides, opening_carry = overrides or {}, opening_carry or {}
    excluded = frozenset(excluded or ())
    with writes_refused():
        result = _compute(
            through, sheet, overrides, opening_carry, excluded,
            decisions_path, records_path, negative, today, since,
        )
    directory = prepare_out_dir(out_dir)
    stamp = _stamp()
    result["csv"] = write_csv(
        directory / f"leave-cleanup-preview-{stamp}-rows.csv",
        CSV_COLUMNS,
        result.pop("csv_rows"),
    )
    result["summary"] = _write_text(
        directory / f"leave-cleanup-preview-{stamp}-summary.txt", result["lines"]
    )
    return result


def _write_text(path, lines):
    # Same rules as accrual.write_csv: never overwrite, owner-only.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


def _compute(
    through, sheet, overrides, opening_carry, excluded,
    decisions_path, records_path, negative, today, since,
):
    leave_type = find_accrual_type()
    type_before = describe_type(leave_type)
    set_accrual_type(leave_type)  # in memory only (step 1)
    check_accrual_type(leave_type)
    rules = carry_rules()

    conversion_plans, _duplicates, _decisions = conversion.plan_all(decisions_path)
    record_plans = hr_records.plan_all(records_path) if records_path else []
    removals = past_pending.plan_removals(today)
    unsaved = unsaved_requests(conversion_plans, record_plans, removals)

    rows = _candidate_rows(leave_type, enrolled_only=False)
    plans = [
        plan_rebuild_row(
            row, leave_type, through, sheet, overrides, opening_carry, negative,
            rules, excluded, unsaved,
        )
        for row in rows
    ]
    rows_by_id = {row.pk: row for row in rows}
    employees = {row.pk: row.employee_id for row in rows}

    csv_rows, by_code = [], {}
    for plan in plans:
        employee = employees[plan.row_id]
        added = unsaved.get(employee.pk, {}).get("approved", 0)
        computed = plan.action in ("change", "unchanged")
        new = plan.proposed or plan.current
        by_code[plan.code.upper()] = plan
        csv_rows.append(
            [
                plan.code,
                f"{plan.action}: {plan.reason}" if plan.reason else plan.action,
                "yes" if rules.is_exempt(employee) else "",
                plan.current[0],
                plan.current[1],
                plan.accrued if computed else "",
                plan.allocated if computed else "",
                round(plan.taken - added, 3) if computed else "",
                round(added, 3) if computed else "",
                plan.opening_carry if computed else "",
                new[0],
                new[1],
                round(sum(new) - sum(plan.current), 3),
                " ".join(plan.flags),
            ]
        )

    lines = _summary(
        through=through,
        today=today,
        since=since,
        rules=rules,
        type_before=type_before,
        type_after=describe_type(leave_type),
        conversion_plans=conversion_plans,
        record_plans=record_plans,
        removals=removals,
        plans=plans,
        rows_by_id=rows_by_id,
        by_code=by_code,
        sheet=sheet,
        overrides=overrides,
        opening_carry=opening_carry,
        excluded=excluded,
    )
    return {
        "plans": plans,
        "conversion": conversion_plans,
        "records": record_plans,
        "removals": removals,
        "unsaved": unsaved,
        "csv_rows": csv_rows,
        "lines": lines,
    }


def _codes(codes):
    counted = Counter(codes)
    return ", ".join(
        f"{code} x{n}" if n > 1 else code for code, n in sorted(counted.items())
    ) or "-"


def _summary(**s):
    rules, plans, by_code = s["rules"], s["plans"], s["by_code"]
    rows_by_id = s["rows_by_id"]
    employees = {row_id: row.employee_id for row_id, row in rows_by_id.items()}
    conversion_plans, record_plans = s["conversion_plans"], s["record_plans"]
    out = []
    add = out.append

    def section(title):
        add("")
        add(f"== {title}")

    add("Leave clean-up preview (read-only; nothing was saved)")
    add(f"today (Nairobi): {s['today']}  through: {s['through']}  since: {s['since']}")
    exempt_cap = "none" if rules.exempt_cap == float("inf") else f"{rules.exempt_cap:g}"
    add(
        f"carry rules: cap {rules.cap:g}; exempt cap {exempt_cap}; "
        f"30 June expiry {'on' if rules.expires_30_june else 'off'}; "
        f"exempt teams skip it {'yes' if rules.exempt_skip_expiry else 'no'}"
    )

    section("Step 1: Annual Leave type (simulated, not saved)")
    add(f"now:       {s['type_before']}")
    add(f"will be:   {s['type_after']}")
    add("exempt departments (configured name -> departments, active staff):")
    active_by_department = Counter(
        department_name(e)
        for e in Employee._base_manager.filter(is_active=True).select_related(
            "employee_work_info__department_id"
        )
    )
    departments = list(Department._base_manager.all())
    for name in sorted(rules.exempt_departments):
        matched = [d for d in departments if (d.department or "").strip().casefold() == name]
        add(
            f"  {name}: "
            + ", ".join(f"#{d.pk} {d.department.strip()!r}" for d in matched)
            + f"; {active_by_department[name]} active"
        )

    section("Step 2: allocation conversion")
    add("actions: " + ", ".join(
        f"{k} {v}" for k, v in sorted(Counter(p.action for p in conversion_plans).items())
    ))
    kept = [p for p in conversion_plans if p.action in ("create", "update")]
    add("created/updated by status: " + ", ".join(
        f"{k} {v}" for k, v in sorted(Counter(p.status for p in kept).items())
    ))
    add("flags: " + ", ".join(
        f"{k} {v}"
        for k, v in sorted(Counter(f for p in conversion_plans for f in p.flags).items())
    ))

    section("Step 3: HR leave records")
    add("actions: " + (", ".join(
        f"{k} {v}" for k, v in sorted(Counter(p.action for p in record_plans).items())
    ) or "no records file"))

    removals = s["removals"]
    section("Step 3b: past pending requests removed as not taken (rejected)")
    add(
        f"pending, ended before {s['today']}, active staff, not made by steps 2-3: "
        f"{len(removals)}"
    )
    for leave_type, n in sorted(Counter(r.leave_type for r in removals).items()):
        add(f"    {leave_type}: {n}")
    add("flags: " + (", ".join(
        f"{k} {v}" for k, v in sorted(Counter(f for r in removals for f in r.flags).items())
    ) or "-"))
    add(f"    {_codes(r.code for r in removals)}")
    add("    ids: " + (", ".join(f"#{r.leave_request_id}" for r in removals) or "-"))

    section("Step 4: Annual Leave rebuild")
    counts = Counter(p.action for p in plans)
    add(
        f"rows {len(plans)} | change {counts['change']} | unchanged "
        f"{counts['unchanged']} | skipped {counts['skip']} | refused {counts['refuse']}"
    )
    for reason, n in sorted(Counter(p.reason for p in plans if p.action == "skip").items()):
        add(f"  skipped ({reason}): {n}")
    for p in plans:
        if p.action == "refuse":
            add(f"  REFUSED {p.code}: {p.reason}")
    changed = [p for p in plans if p.action == "change"]
    deltas = [round(sum(p.proposed) - sum(p.current), 3) for p in changed]
    add(
        f"days: total change {round(sum(deltas), 3):g}; rows down "
        f"{sum(d < 0 for d in deltas)} ({round(sum(d for d in deltas if d < 0), 3):g}); "
        f"rows up {sum(d > 0 for d in deltas)} ({round(sum(d for d in deltas if d > 0), 3):g})"
    )
    known = {p.code.upper() for p in plans}
    unmatched = (
        set(s["sheet"]) | set(s["overrides"]) | set(s["opening_carry"]) | s["excluded"]
    ) - known
    add(f"codes in input files with no Annual Leave row: {_codes(unmatched)}")

    section("Edge cases")
    computed = [p for p in plans if p.action in ("change", "unchanged")]

    def flagged(flag):
        return [p.code for p in computed if flag in p.flags]

    def item(title, codes):
        add(f"- {title}: {len(codes)}")
        add(f"    {_codes(codes)}")

    item("negative after the rebuild", flagged("NEGATIVE"))
    item("pending exceeds balance (engine flag; includes negative rows)",
         flagged("PENDING_EXCEEDS"))

    pending = [
        r for r in requests_after(
            LeaveRequest._base_manager.filter(status="requested").exclude(
                pk__in=[r.leave_request_id for r in removals]
            ),
            conversion_plans, record_plans,
        )
        if r.status == "requested" and r.start < s["today"] and r.active
    ]
    # Step 3b removes every other one, so what is left either has not ended
    # or was made by steps 2-3 (HR decides those in their own files).
    running = [r for r in pending if r.end is None or r.end >= s["today"]]
    ended = [r for r in pending if r not in running]
    add(f"- pending requests started before today, left after step 3b "
        f"(active staff, all leave types): {len(pending)}")
    add(f"    not ended yet (or no end date): {len(running)}  {_codes(r.code for r in running)}")
    add(f"    ended, made by the conversion or HR records: {len(ended)}  "
        f"{_codes(r.code for r in ended)}")

    straddling = [
        r for r in requests_after(
            LeaveRequest._base_manager.filter(
                start_date__lt=ACCRUAL_EPOCH, end_date__gte=ACCRUAL_EPOCH
            ),
            conversion_plans, record_plans,
        )
        if r.leave_type == ACCRUAL_LEAVE_TYPE_NAME and r.status in COUNTED
        and r.start < ACCRUAL_EPOCH <= r.end and r.active
    ]
    item("Annual Leave straddling 31 Dec 2025 (not counted as 2026 taken)",
         [r.code for r in straddling])
    crosses = [p.code for p in conversion_plans if "CROSSES_YEAR" in p.flags]
    item("converted requests crossing into 2027 (CROSSES_YEAR)", crosses)

    excluded_rows = [p for p in plans if p.reason == EXCLUDED]
    add(f"- excluded by HR (balance untouched; HR archives them in the UI): "
        f"{len(excluded_rows)}")
    for p in excluded_rows:
        employee = employees[p.row_id]
        # The daily run still credits an excluded row that is on accrual.
        row_note = (
            "on daily accrual" if rows_by_id[p.row_id].last_accrual_date
            else "not on accrual"
        )
        add(
            f"    {p.code}: {p.current[0]:g}+{p.current[1]:g}, "
            f"{'active' if employee.is_active else 'inactive'}, {row_note}"
        )

    restored = sorted((c, v) for c, v in s["opening_carry"].items() if v > 0)
    add(f"- restored opening carry: {len(restored)} ({sum(v for _, v in restored):g} days)")
    for code, value in restored:
        plan = by_code.get(code)
        if plan is None:
            add(f"    {code}: {value:g} (no Annual Leave row)")
            continue
        exempt = rules.is_exempt(employees[plan.row_id])
        expiry = "no expiry (exempt team)" if exempt and rules.exempt_skip_expiry else (
            "lapses 30 Jun 2027 if unused" if rules.expires_30_june else "no expiry"
        )
        new = plan.proposed or plan.current
        add(
            f"    {code}: {value:g} -> {plan.action}, new {new[0]:g}+{new[1]:g} "
            f"(carry spent first); {expiry}"
        )

    exempt_rows = [
        p for p in plans
        if employees[p.row_id].is_active and rules.is_exempt(employees[p.row_id])
    ]
    add(f"- exempt-team members with an Annual Leave row (active): {len(exempt_rows)}")
    for department, n in sorted(
        Counter(department_name(employees[p.row_id]) for p in exempt_rows).items()
    ):
        add(f"    {department}: {n}")
    add(f"    {_codes(p.code for p in exempt_rows)}")

    used = flagged("JOIN_FROM_OVERRIDE")
    item("join-date overrides used", used)
    unused = sorted(set(s["overrides"]) - {c.upper() for c in used})
    item("overrides not used (row skipped, excluded or missing)", unused)

    untrusted = []
    for p in plans:
        employee = employees[p.row_id]
        if employee.is_active and join_date(employee) in UNTRUSTED_JOIN_DATES:
            key = p.code.upper()
            how = (
                "override" if "JOIN_FROM_OVERRIDE" in p.flags
                else "sheet" if key in s["sheet"]
                else p.reason or p.action
            )
            untrusted.append(f"{p.code} ({how})")
    add(f"- rows on the untrusted join date(s) "
        f"{', '.join(str(d) for d in sorted(UNTRUSTED_JOIN_DATES))}: {len(untrusted)}")
    for how, n in sorted(Counter(u.split(" (", 1)[1][:-1] for u in untrusted).items()):
        add(f"    resolved by {how}: {n}")
    add(f"    {', '.join(sorted(untrusted)) or '-'}")

    odd = [
        f"#{p.alloc_id} {p.code} {'/'.join(f for f in p.flags if f in ('EMP_INACTIVE', 'NO_BALANCE_ROW'))} ({p.action})"
        for p in conversion_plans
        if {"EMP_INACTIVE", "NO_BALANCE_ROW"} & set(p.flags)
    ]
    add(f"- allocations whose employee is inactive or has no balance row: {len(odd)}")
    for line in odd:
        add(f"    {line}")

    since = s["since"]
    new_requests = LeaveRequest._base_manager.filter(created_at__gte=since).select_related(
        "employee_id"
    )
    new_allocations = LeaveAllocationRequest._base_manager.filter(
        created_at__gte=since
    ).select_related("employee_id")
    # Employee has no creation time; its login's date_joined is the closest.
    new_employees = Employee._base_manager.filter(employee_user_id__date_joined__gte=since)
    item(f"leave requests created since {since}",
         [employee_code(r.employee_id) for r in new_requests])
    item(f"leave allocations created since {since}",
         [employee_code(a.employee_id) for a in new_allocations])
    item(f"employees created since {since} (login date_joined; staff without a "
         "login not covered)", [employee_code(e) for e in new_employees])
    return out
