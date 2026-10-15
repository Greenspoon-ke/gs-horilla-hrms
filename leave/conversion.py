"""
One-off conversion of leave allocation requests into leave requests (HR's
2026 leave clean-up). Staff used allocation requests to ask for time off; HR
agreed to treat every one created before LEAVE_ALLOCATION_CUTOVER as the leave
request it should have been.

Rules:

- Each allocation becomes a leave request with the same status (HR can set
  another one in the decisions file, e.g. a week a manager rejected but HR's
  rota confirms was taken, with a reject reason for a rejected one): approved
  counts as days taken in the Annual Leave rebuild, requested waits for its
  manager, rejected stays rejected. Nothing is deducted here and no mail or
  notification is sent: the rebuild is the only thing that counts the days.
- HR has no real dates, so the dates are assumed: start on the day the
  allocation was created (Africa/Nairobi), moved to the next counted day, and
  end when LeaveRequest.save()'s own day count equals the allocation's days.
  HR can give real dates in the decisions file.
- Duplicates (same employee, leave type and days, created within 7 days of
  the previous one) are not converted; the earliest is kept. HR can override
  keep/drop per allocation.
- The attendance sync signal is switched off while saving: the dates are
  assumed, so they must never write or delete attendance work records.
- Markers, no migration: the leave request's description starts with
  "[Allocation #<id>] ", and the allocation is set to rejected with a reason
  ending in "(leave clean-up 2026)", which also stops the old approve and
  reject buttons acting on it. A second run changes nothing.

Background-safe: nothing here reads a request, user or session.
"""

import csv
import math
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.db import transaction
from django.db.models.signals import pre_save

from horilla.horilla_middlewares import _thread_locals
from leave.accrual import (
    NAIROBI,
    allocation_cutover,
    employee_code,
    nairobi_midnight,
    outside_repo,
)
from leave.methods import calculate_requested_days
from leave.models import AvailableLeave, LeaveAllocationRequest, LeaveRequest

MARK = "(leave clean-up 2026)"
STATUSES = ("approved", "requested", "rejected")
CONVERTED = "Converted to leave request #"
DUPLICATE_WINDOW = timedelta(days=7)
DESCRIPTION_LIMIT = 255  # the leave request form's limit
# Longest leave request the date search will build; far above any real one.
MAX_SPAN_DAYS = 366

REPORT_COLUMNS = [
    "alloc_id",
    "code",
    "employee_id",
    "leave_type",
    "original_status",
    "status",
    "days",
    "created",
    "action",
    "keep",
    "duplicate_of",
    "start",
    "end",
    "start_breakdown",
    "end_breakdown",
    "computed_days",
    "leave_request_id",
    "flags",
]
MAPPING_COLUMNS = [
    "alloc_id",
    "action",
    "leave_request_id",
    "original_status",
    "original_reject_reason",
]


class ConversionRefused(Exception):
    """The run must not go ahead. Raised before anything is written."""


def marker(alloc_id):
    return f"[Allocation #{alloc_id}] "


def day_count(leave_type, start, end, start_breakdown="full_day", end_breakdown="full_day"):
    """requested_days exactly as LeaveRequest.save() computes it."""
    leave_request = LeaveRequest(
        leave_type_id=leave_type,
        start_date=start,
        end_date=end,
        start_date_breakdown=start_breakdown,
        end_date_breakdown=end_breakdown,
    )
    leave_request.requested_days = calculate_requested_days(
        start, end, start_breakdown, end_breakdown
    )
    if leave_type.exclude_company_leave == "yes" and leave_type.exclude_holiday == "yes":
        leave_request.exclude_all_leaves()
    else:
        leave_request.exclude_leaves()
    return leave_request.requested_days


def dates_from(leave_type, start, days):
    """
    (start, end, start_breakdown, end_breakdown, flags) for ``days`` days from
    ``start``: the start moves to the next day that counts, the end is the
    first counted day where save()'s count reaches the days; x.5 ends on a
    first-half day. Refuses if the count cannot be matched exactly.
    """
    flags = []
    first = start
    while day_count(leave_type, start, start) <= 0:
        start += timedelta(days=1)
        if (start - first).days > MAX_SPAN_DAYS:
            raise ConversionRefused(f"No counted day after {first}.")
    if start != first:
        flags.append("START_MOVED")
    whole = math.ceil(days)
    end = start
    while not (
        day_count(leave_type, start, end) >= whole and day_count(leave_type, end, end) > 0
    ):
        end += timedelta(days=1)
        if (end - start).days > MAX_SPAN_DAYS:
            raise ConversionRefused(f"Cannot fit {days:g} days from {start}.")
    start_breakdown = end_breakdown = "full_day"
    if days != whole:
        end_breakdown = "first_half"
        if start == end:
            start_breakdown = "first_half"
    counted = day_count(leave_type, start, end, start_breakdown, end_breakdown)
    if counted != days:
        raise ConversionRefused(
            f"Assumed dates {start}..{end} count {counted:g} days, not {days:g}."
        )
    if start.year != end.year:
        # company_leave_dates() only covers the start year: weekends in the
        # next year would count. Flag it for HR rather than guess.
        flags.append("CROSSES_YEAR")
    return start, end, start_breakdown, end_breakdown, flags


def created_day(allocation):
    if allocation.created_at is None:
        raise ConversionRefused(f"Allocation {allocation.pk} has no creation time.")
    return allocation.created_at.astimezone(NAIROBI).date()


def original_state(allocation):
    """
    (status, reject_reason) before this clean-up touched the allocation:
    the current values, or the latest history row without the marker.
    """
    if MARK not in (allocation.reject_reason or ""):
        return allocation.status, allocation.reject_reason or ""
    for record in allocation.history_set.order_by("-history_date", "-history_id"):
        if MARK not in (record.reject_reason or ""):
            return record.status, record.reject_reason or ""
    raise ConversionRefused(f"Allocation {allocation.pk}: no history before the clean-up.")


def converted_request(allocation):
    """
    The leave request made from this allocation: found by its description
    marker, or by the id written into the allocation's reject reason (in case
    someone edited the description since).
    """
    found = (
        LeaveRequest._base_manager.filter(description__startswith=marker(allocation.pk))
        .order_by("pk")
        .first()
    )
    if found is None and MARK in (allocation.reject_reason or ""):
        recorded = re.search(re.escape(CONVERTED) + r"(\d+)", allocation.reject_reason)
        if recorded:
            found = LeaveRequest._base_manager.filter(
                pk=int(recorded.group(1)), employee_id=allocation.employee_id
            ).first()
    return found


def candidates():
    """Allocations created before the cut-over (Nairobi midnight), oldest first."""
    cutover = nairobi_midnight(allocation_cutover())
    return list(
        LeaveAllocationRequest._base_manager.filter(created_at__lt=cutover)
        .select_related("employee_id", "leave_type_id", "created_by")
        .order_by("created_at", "pk")
    )


def duplicates(allocations):
    """{alloc_id: kept_id} for repeats of the same employee, type and days."""
    groups = {}
    for allocation in allocations:
        key = (allocation.employee_id_id, allocation.leave_type_id_id, allocation.requested_days)
        groups.setdefault(key, []).append(allocation)
    result = {}
    for group in groups.values():
        kept, previous = group[0], group[0]
        for allocation in group[1:]:
            if allocation.created_at - previous.created_at <= DUPLICATE_WINDOW:
                result[allocation.pk] = kept.pk
            else:
                kept = allocation
            previous = allocation
    return result


def parse_day(value, name, line):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        raise ConversionRefused(f"decisions line {line}: {name} must be YYYY-MM-DD") from None


def read_decisions(raw_path, known_ids):
    """
    HR's CSV: alloc_id,keep_drop,start_date,end_date[,status][,reject_reason]
    (blank = default). status (approved, requested or rejected) replaces the
    allocation's own; reject_reason only goes with status rejected.
    """
    if not raw_path:
        return {}
    path = outside_repo(raw_path)
    if not path.is_file():
        raise ConversionRefused(f"File not found: {raw_path}")
    decisions = {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = {(name or "").strip().lower() for name in reader.fieldnames or []}
        if "alloc_id" not in columns:
            raise ConversionRefused(f"{path.name} needs an alloc_id column")
        for line, raw in enumerate(reader, start=2):
            record = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k}
            try:
                alloc_id = int(record["alloc_id"])
            except ValueError:
                raise ConversionRefused(f"decisions line {line}: bad alloc_id") from None
            if alloc_id not in known_ids:
                raise ConversionRefused(
                    f"decisions line {line}: allocation {alloc_id} is not converted "
                    "(unknown, or created on/after the cut-over)"
                )
            if alloc_id in decisions:
                raise ConversionRefused(f"decisions line {line}: duplicate {alloc_id}")
            keep_drop = record.get("keep_drop", "").lower()
            if keep_drop not in ("", "keep", "drop"):
                raise ConversionRefused(f"decisions line {line}: keep_drop is keep or drop")
            start = parse_day(record.get("start_date", ""), "start_date", line)
            end = parse_day(record.get("end_date", ""), "end_date", line)
            if end and not start:
                raise ConversionRefused(f"decisions line {line}: end_date needs start_date")
            if start and end and end < start:
                raise ConversionRefused(f"decisions line {line}: end_date before start_date")
            status = record.get("status", "").lower()
            if status not in ("", *STATUSES):
                raise ConversionRefused(
                    f"decisions line {line}: status is one of {', '.join(STATUSES)}"
                )
            reject_reason = record.get("reject_reason", "")
            if reject_reason and status != "rejected":
                raise ConversionRefused(
                    f"decisions line {line}: reject_reason needs status rejected"
                )
            if len(reject_reason) > DESCRIPTION_LIMIT:
                raise ConversionRefused(
                    f"decisions line {line}: reject_reason is over {DESCRIPTION_LIMIT} characters"
                )
            decisions[alloc_id] = {
                "keep_drop": keep_drop,
                "start": start,
                "end": end,
                "status": status,
                "reject_reason": reject_reason,
            }
    return decisions


@dataclass
class Plan:
    """What one run would do to one allocation."""

    alloc_id: int
    code: str
    employee_id: int
    leave_type: str
    original_status: str
    original_reason: str
    status: str
    days: float
    created: date
    keep: bool
    duplicate_of: int = None
    start: date = None
    end: date = None
    start_breakdown: str = "full_day"
    end_breakdown: str = "full_day"
    computed_days: float = None
    leave_request_id: int = None
    reject_reason: str = ""  # the leave request's, not the allocation's
    action: str = "none"  # create | update | mark | unconvert | drop | none
    flags: list = field(default_factory=list)

    def key(self):
        return (
            self.action,
            self.status,
            self.reject_reason,
            self.start,
            self.end,
            self.start_breakdown,
            self.end_breakdown,
        )

    def report_row(self):
        return [
            self.alloc_id,
            self.code,
            self.employee_id,
            self.leave_type,
            self.original_status,
            self.status,
            self.days,
            self.created,
            self.action,
            "keep" if self.keep else "drop",
            self.duplicate_of or "",
            self.start or "",
            self.end or "",
            self.start_breakdown,
            self.end_breakdown,
            "" if self.computed_days is None else self.computed_days,
            self.leave_request_id or "",
            " ".join(self.flags),
        ]


def deducted(leave_request):
    """
    A converted pending request approved later through the normal view has
    had days deducted; deleting or re-dating it here would lose that.
    """
    return bool(
        leave_request.approved_available_days or leave_request.approved_carryforward_days
    )


def plan_allocation(allocation, duplicate_of, decision):
    """The single place that decides what happens to one allocation."""
    if not allocation.requested_days or allocation.requested_days < 0:
        raise ConversionRefused(f"Allocation {allocation.pk} has no requested days.")
    status, reason = original_state(allocation)
    employee = allocation.employee_id
    leave_type = allocation.leave_type_id
    keep = duplicate_of is None
    if decision and decision["keep_drop"]:
        keep = decision["keep_drop"] == "keep"
    plan = Plan(
        alloc_id=allocation.pk,
        code=employee_code(employee),
        employee_id=employee.pk,
        leave_type=leave_type.name,
        original_status=status,
        original_reason=reason,
        status=status,
        days=allocation.requested_days,
        created=created_day(allocation),
        keep=keep,
        duplicate_of=duplicate_of,
    )
    if duplicate_of is not None:
        plan.flags.append("DUPLICATE")
    if decision and (decision["keep_drop"] or decision["start"] or decision["status"]):
        plan.flags.append("HR_DECISION")
    if not employee.is_active:
        plan.flags.append("EMP_INACTIVE")
    if not AvailableLeave._base_manager.filter(
        employee_id=employee, leave_type_id=leave_type
    ).exists():
        plan.flags.append("NO_BALANCE_ROW")
    if status == "requested":
        plan.flags.append("PENDING")

    existing = converted_request(allocation)
    plan.leave_request_id = existing.pk if existing else None
    # Like the dates: HR's decision wins, else keep what an earlier run set.
    if decision and decision["status"]:
        plan.status = decision["status"]
    elif existing:
        plan.status = existing.status
    if decision and decision["reject_reason"]:
        plan.reject_reason = decision["reject_reason"]
    elif existing:
        plan.reject_reason = existing.reject_reason or ""
    reason_now = allocation.reject_reason or ""
    marked_converted = CONVERTED in reason_now and MARK in reason_now
    marked_dropped = MARK in reason_now and not marked_converted

    if not keep:
        if existing and deducted(existing):
            raise ConversionRefused(
                f"Allocation {allocation.pk}: leave request #{existing.pk} was approved "
                "with days deducted since; reject it in the UI instead."
            )
        if existing:
            plan.action = "unconvert"
        elif not marked_dropped or allocation.status != "rejected":
            plan.action = "drop"
        return plan

    if decision and decision["start"]:
        if decision["end"]:
            dates = (decision["start"], decision["end"], "full_day", "full_day", [])
        else:
            dates = dates_from(leave_type, decision["start"], plan.days)
    elif existing:
        # Keep what is there (HR may have corrected it on an earlier run).
        dates = (
            existing.start_date,
            existing.end_date,
            existing.start_date_breakdown,
            existing.end_date_breakdown,
            [],
        )
    else:
        dates = dates_from(leave_type, plan.created, plan.days)
    (
        plan.start,
        plan.end,
        plan.start_breakdown,
        plan.end_breakdown,
        date_flags,
    ) = dates
    plan.flags.extend(date_flags)
    plan.computed_days = day_count(
        leave_type, plan.start, plan.end, plan.start_breakdown, plan.end_breakdown
    )
    if plan.computed_days != plan.days:
        # Only HR's own dates can get here: assumed dates are exact.
        plan.flags.append("DAYS_DIFFER")
    overlapping = (
        LeaveRequest._base_manager.filter(
            employee_id=employee, start_date__lte=plan.end, end_date__gte=plan.start
        )
        .exclude(status__in=["rejected", "cancelled"])
        .exclude(pk=plan.leave_request_id)
    )
    if overlapping.exists():
        plan.flags.append("OVERLAPS_LEAVE")

    if not existing:
        plan.action = "create"
    elif (
        existing.status,
        existing.reject_reason or "",
        existing.start_date,
        existing.end_date,
        existing.start_date_breakdown,
        existing.end_date_breakdown,
    ) != (
        plan.status,
        plan.reject_reason,
        plan.start,
        plan.end,
        plan.start_breakdown,
        plan.end_breakdown,
    ):
        plan.action = "update"
    elif not (marked_converted and allocation.status == "rejected"):
        plan.action = "mark"
    if plan.action == "update" and deducted(existing):
        raise ConversionRefused(
            f"Allocation {allocation.pk}: leave request #{existing.pk} was approved "
            "with days deducted since; change it in the UI instead."
        )
    return plan


def plan_all(decisions_path=None):
    allocations = candidates()
    duplicate_of = duplicates(allocations)
    decisions = read_decisions(decisions_path, {a.pk for a in allocations})
    return [
        plan_allocation(a, duplicate_of.get(a.pk), decisions.get(a.pk))
        for a in allocations
    ], duplicate_of, decisions


@contextmanager
def attendance_sync_off():
    """
    leave.signals.leaverequest_pre_save writes (approved) or deletes (any other
    status) attendance work records for every date of a leave request. The
    dates here are assumed, so that must not happen; switch it off and back on.
    """
    from leave import signals

    receiver = getattr(signals, "leaverequest_pre_save", None)
    if receiver is None:  # the attendance app is not installed
        yield
        return
    if not pre_save.disconnect(receiver, sender=LeaveRequest):
        raise ConversionRefused("Could not switch off the attendance sync signal.")
    try:
        yield
    finally:
        pre_save.connect(receiver, sender=LeaveRequest)


def assert_no_request():
    """A web request in this thread would let save() and automations act as a user."""
    if getattr(_thread_locals, "request", None) is not None:
        raise ConversionRefused("Run from a management command, not a web request.")


def _mark(allocation, reason):
    allocation.status = "rejected"
    allocation.reject_reason = reason[:DESCRIPTION_LIMIT]
    allocation.skip_history = False
    allocation._change_reason = "Leave clean-up 2026"
    allocation.save()


def _save_request(leave_request, plan):
    leave_request.status = plan.status
    leave_request.reject_reason = plan.reject_reason
    leave_request.start_date = plan.start
    leave_request.end_date = plan.end
    leave_request.start_date_breakdown = plan.start_breakdown
    leave_request.end_date_breakdown = plan.end_breakdown
    leave_request._change_reason = f"Leave clean-up 2026: allocation #{plan.alloc_id}"
    leave_request.save()  # never clean(): no balance check, no deduction
    if leave_request.requested_days != plan.computed_days:
        raise ConversionRefused(
            f"Allocation {plan.alloc_id}: saved {leave_request.requested_days:g} days, "
            f"planned {plan.computed_days:g}."
        )


def _execute(allocation, plan):
    reason = plan.original_reason
    suffix = f" | was: {reason}" if reason else ""
    if plan.action == "create":
        creator = getattr(allocation.created_by, "employee_get", None)
        leave_request = LeaveRequest(
            employee_id=allocation.employee_id,
            leave_type_id=allocation.leave_type_id,
            description=(marker(plan.alloc_id) + allocation.description)[
                :DESCRIPTION_LIMIT
            ],
            requested_date=allocation.requested_date,
            attachment=allocation.attachment.name or None,
            created_by=creator,
        )
        _save_request(leave_request, plan)
        plan.leave_request_id = leave_request.pk
        _mark(allocation, f"{CONVERTED}{leave_request.pk} {MARK}{suffix}")
    elif plan.action == "update":
        _save_request(LeaveRequest._base_manager.get(pk=plan.leave_request_id), plan)
    elif plan.action == "mark":
        _mark(allocation, f"{CONVERTED}{plan.leave_request_id} {MARK}{suffix}")
    elif plan.action in ("drop", "unconvert"):
        if plan.action == "unconvert":
            # QuerySet.delete: LeaveRequest.delete() refuses approved requests.
            LeaveRequest._base_manager.filter(pk=plan.leave_request_id).delete()
        what = (
            f"Duplicate of #{plan.duplicate_of}"
            if plan.duplicate_of
            else "Not converted: HR decision"
        )
        _mark(allocation, f"{what} {MARK}{suffix}")


def apply_plans(plans, duplicate_of, decisions):
    """Each allocation in its own transaction, re-planned under a row lock."""
    assert_no_request()
    done, stale = [], []
    with attendance_sync_off():
        for plan in plans:
            if plan.action == "none":
                continue
            with transaction.atomic():
                allocation = (
                    LeaveAllocationRequest._base_manager.select_for_update(of=("self",))
                    .select_related("employee_id", "leave_type_id", "created_by")
                    .get(pk=plan.alloc_id)
                )
                fresh = plan_allocation(
                    allocation,
                    duplicate_of.get(plan.alloc_id),
                    decisions.get(plan.alloc_id),
                )
                if fresh.key() != plan.key():
                    stale.append(plan.alloc_id)  # changed since the plan was made
                    continue
                _execute(allocation, fresh)
                plan.leave_request_id = fresh.leave_request_id
                done.append(plan)
    return done, stale


def read_mapping(raw_path):
    path = outside_repo(raw_path)
    if not path.is_file():
        raise ConversionRefused(f"File not found: {raw_path}")
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or set(MAPPING_COLUMNS) - set(rows[0]):
        raise ConversionRefused(f"{path.name} is not a conversion mapping file")
    return rows


def plan_reverse(rows):
    """[(alloc_id, status, reason, leave_request_id or None)] still to undo."""
    pending = []
    for row in rows:
        alloc_id = int(row["alloc_id"])
        allocation = LeaveAllocationRequest._base_manager.filter(pk=alloc_id).first()
        if allocation is None:
            raise ConversionRefused(f"Allocation {alloc_id} no longer exists.")
        existing = converted_request(allocation)
        if existing and deducted(existing):
            raise ConversionRefused(
                f"Allocation {alloc_id}: leave request #{existing.pk} was approved "
                "with days deducted since; reject it in the UI first."
            )
        if existing or MARK in (allocation.reject_reason or ""):
            pending.append(
                (
                    alloc_id,
                    row["original_status"],
                    row["original_reject_reason"],
                    existing.pk if existing else None,
                )
            )
    return pending


def apply_reverse(pending):
    assert_no_request()
    with attendance_sync_off():
        for alloc_id, status, reason, leave_request_id in pending:
            with transaction.atomic():
                allocation = LeaveAllocationRequest._base_manager.select_for_update(
                    of=("self",)
                ).get(pk=alloc_id)
                if leave_request_id:
                    LeaveRequest._base_manager.filter(pk=leave_request_id).delete()
                allocation.status = status
                allocation.reject_reason = reason
                allocation.skip_history = False
                allocation._change_reason = "Leave clean-up 2026 reversed"
                allocation.save()
