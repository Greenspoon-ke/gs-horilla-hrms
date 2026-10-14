"""
Leave that HR confirms was taken but that is not in the system at all (no
leave request, no allocation), recorded as leave requests during the 2026
leave clean-up, e.g. weeks on HR's leave rota.

Same safety as the allocation conversion (leave/conversion.py): nothing is
deducted (the Annual Leave rebuild counts approved requests), no mail or
notification, the attendance sync signal is off while saving (these are past
dates; attendance and payroll for them are already settled), and clean() is
never called.

Input CSV (outside the repository), one row per leave:
    ref,code,leave_type,start_date,end_date,status
ref is HR's own stable key (e.g. rota-17); the request's description starts
with "[HR leave <ref>] ", so a re-run updates that request instead of adding
another. status is approved, requested or rejected.

Background-safe: nothing here reads a request, user or session.
"""

import csv
import re
from dataclasses import dataclass, field
from datetime import date

from django.db import transaction

from employee.models import Employee
from leave.accrual import employee_code, outside_repo
from leave.conversion import (
    STATUSES,
    ConversionRefused,
    assert_no_request,
    attendance_sync_off,
    day_count,
    deducted,
)
from leave.models import AvailableLeave, LeaveRequest, LeaveType

REF_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
DESCRIPTION = "Recorded by HR (leave clean-up 2026)"
REPORT_COLUMNS = [
    "ref",
    "code",
    "employee_id",
    "leave_type",
    "status",
    "start",
    "end",
    "computed_days",
    "action",
    "leave_request_id",
    "flags",
]
MAPPING_COLUMNS = ["ref", "action", "leave_request_id"]


def marker(ref):
    return f"[HR leave {ref}] "


def existing_request(ref):
    return (
        LeaveRequest._base_manager.filter(description__startswith=marker(ref))
        .order_by("pk")
        .first()
    )


@dataclass
class RecordPlan:
    """What one run would do for one line of HR's file."""

    ref: str
    code: str
    employee: Employee
    leave_type: LeaveType
    status: str
    start: date
    end: date
    computed_days: float = None
    leave_request_id: int = None
    action: str = "none"  # create | update | none
    flags: list = field(default_factory=list)

    def key(self):
        return (self.action, self.status, self.start, self.end)

    def report_row(self):
        return [
            self.ref,
            self.code,
            self.employee.pk,
            self.leave_type.name,
            self.status,
            self.start,
            self.end,
            self.computed_days,
            self.action,
            self.leave_request_id or "",
            " ".join(self.flags),
        ]


def _employees_by_code():
    by_code = {}
    for employee in Employee._base_manager.all():
        by_code.setdefault(employee_code(employee).upper(), []).append(employee)
    return by_code


def read_records(raw_path):
    """Validate every line before anything is planned; a bad line refuses."""
    path = outside_repo(raw_path)
    if not path.is_file():
        raise ConversionRefused(f"File not found: {raw_path}")
    by_code = _employees_by_code()
    records, refs = [], set()
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = {(name or "").strip().lower() for name in reader.fieldnames or []}
        needed = {"ref", "code", "leave_type", "start_date", "end_date", "status"}
        if needed - columns:
            raise ConversionRefused(f"{path.name} needs columns: {', '.join(sorted(needed))}")
        for line, raw in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k}
            ref = row["ref"]
            if not REF_PATTERN.match(ref):
                raise ConversionRefused(f"line {line}: ref must be letters, digits, - or _")
            if ref in refs:
                raise ConversionRefused(f"line {line}: duplicate ref {ref}")
            refs.add(ref)
            matches = by_code.get(row["code"].upper(), [])
            if len(matches) != 1:
                raise ConversionRefused(
                    f"line {line}: code {row['code']} matches {len(matches)} employees"
                )
            leave_types = list(LeaveType._base_manager.filter(name=row["leave_type"]))
            if len(leave_types) != 1:
                raise ConversionRefused(f"line {line}: unknown leave type {row['leave_type']!r}")
            try:
                start = date.fromisoformat(row["start_date"])
                end = date.fromisoformat(row["end_date"])
            except ValueError:
                raise ConversionRefused(f"line {line}: dates must be YYYY-MM-DD") from None
            if end < start:
                raise ConversionRefused(f"line {line}: end_date before start_date")
            status = row["status"].lower()
            if status not in STATUSES:
                raise ConversionRefused(f"line {line}: status is one of {', '.join(STATUSES)}")
            records.append((ref, matches[0], leave_types[0], start, end, status))
    return records


def plan_record(ref, employee, leave_type, start, end, status):
    plan = RecordPlan(
        ref=ref,
        code=employee_code(employee),
        employee=employee,
        leave_type=leave_type,
        status=status,
        start=start,
        end=end,
    )
    plan.computed_days = day_count(leave_type, start, end)
    if not employee.is_active:
        plan.flags.append("EMP_INACTIVE")
    if not AvailableLeave._base_manager.filter(
        employee_id=employee, leave_type_id=leave_type
    ).exists():
        plan.flags.append("NO_BALANCE_ROW")
    existing = existing_request(ref)
    plan.leave_request_id = existing.pk if existing else None
    overlapping = (
        LeaveRequest._base_manager.filter(
            employee_id=employee, start_date__lte=end, end_date__gte=start
        )
        .exclude(status__in=["rejected", "cancelled"])
        .exclude(pk=plan.leave_request_id)
    )
    if overlapping.exists():
        plan.flags.append("OVERLAPS_LEAVE")
    if existing is None:
        plan.action = "create"
        return plan
    if (existing.employee_id_id, existing.leave_type_id_id) != (employee.pk, leave_type.pk):
        raise ConversionRefused(
            f"ref {ref}: leave request #{existing.pk} is for another employee or "
            "leave type; use a new ref."
        )
    if (existing.status, existing.start_date, existing.end_date) != (status, start, end):
        if deducted(existing):
            raise ConversionRefused(
                f"ref {ref}: leave request #{existing.pk} was approved with days "
                "deducted since; change it in the UI instead."
            )
        plan.action = "update"
    return plan


def plan_all(raw_path):
    return [plan_record(*record) for record in read_records(raw_path)]


def _save(leave_request, plan):
    leave_request.status = plan.status
    leave_request.start_date = plan.start
    leave_request.end_date = plan.end
    leave_request._change_reason = f"Leave clean-up 2026: HR leave {plan.ref}"
    leave_request.save()  # never clean(): no balance check, no deduction
    if leave_request.requested_days != plan.computed_days:
        raise ConversionRefused(
            f"ref {plan.ref}: saved {leave_request.requested_days:g} days, "
            f"planned {plan.computed_days:g}."
        )


def apply_plans(plans):
    """Each line in its own transaction, re-planned first."""
    assert_no_request()
    done, stale = [], []
    with attendance_sync_off():
        for plan in plans:
            if plan.action == "none":
                continue
            with transaction.atomic():
                # Serialise on the employee row so two runs cannot both create.
                Employee._base_manager.select_for_update().get(pk=plan.employee.pk)
                fresh = plan_record(
                    plan.ref, plan.employee, plan.leave_type, plan.start, plan.end, plan.status
                )
                if fresh.key() != plan.key():
                    stale.append(plan.ref)
                    continue
                if fresh.action == "create":
                    leave_request = LeaveRequest(
                        employee_id=plan.employee,
                        leave_type_id=plan.leave_type,
                        description=marker(plan.ref) + DESCRIPTION,
                    )
                else:
                    leave_request = LeaveRequest._base_manager.get(pk=fresh.leave_request_id)
                _save(leave_request, fresh)
                plan.leave_request_id = leave_request.pk
                done.append(plan)
    return done, stale


def read_mapping(raw_path):
    path = outside_repo(raw_path)
    if not path.is_file():
        raise ConversionRefused(f"File not found: {raw_path}")
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or set(MAPPING_COLUMNS) - set(rows[0]):
        raise ConversionRefused(f"{path.name} is not an HR leave mapping file")
    return rows


def plan_reverse(rows):
    """Leave requests created by a run that still exist: [(ref, id)]."""
    pending = []
    for row in rows:
        if row["action"] != "create":
            continue  # an update is undone by re-running with the old file
        found = LeaveRequest._base_manager.filter(
            pk=int(row["leave_request_id"]), description__startswith=marker(row["ref"])
        ).first()
        if found is None:
            continue
        if deducted(found):
            raise ConversionRefused(
                f"ref {row['ref']}: leave request #{found.pk} was approved with days "
                "deducted since; reject it in the UI first."
            )
        pending.append((row["ref"], found.pk))
    return pending


def apply_reverse(pending):
    assert_no_request()
    with attendance_sync_off():
        for _ref, leave_request_id in pending:
            # QuerySet.delete: LeaveRequest.delete() refuses approved requests.
            LeaveRequest._base_manager.filter(pk=leave_request_id).delete()
