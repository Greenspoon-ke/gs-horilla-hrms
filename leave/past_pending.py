"""
Past pending leave requests removed as not taken (HR's 2026 leave clean-up,
decision of 2026-10-15): every leave request still "requested" whose end date
is before the run date (Nairobi), for active staff, any leave type.

Rules:

- Default: set to rejected with reject_reason REASON. --delete removes them
  instead (QuerySet.delete; cascades to their comments and approval steps).
- Requests made by the allocation conversion or HR's records (their
  description markers) are not touched: HR decides those in their own files.
- Nothing is deducted or restored: a pending request never had days taken,
  and a request with days deducted refuses the run.
- No mail, notification or message: views, API and automations are bypassed,
  and a web request in this thread refuses the run. The attendance sync
  signal is off while saving: for any status but approved it deletes the
  leave work records on the request's dates, which may belong to real leave.
- One transaction: balances and work records of the people concerned are
  compared before and after; any difference rolls the whole run back.
- Only the status columns are written (multi-level approval steps are kept),
  and the clash counts of all requests are recounted once at the end.
- --reverse with the run's mapping file sets rejected ones back to requested
  (only those still rejected with REASON). Deleted ones need the backup.

Background-safe: nothing here reads a request, user or session.
"""

import csv
from dataclasses import dataclass, field
from datetime import date

from django.apps import apps
from django.db import transaction
from django.db.models import Q

from leave.accrual import employee_code, nairobi_today, outside_repo, write_csv
from leave.conversion import (
    ConversionRefused,
    assert_no_request,
    attendance_sync_off,
    deducted,
)
from leave.models import AvailableLeave, LeaveRequest

REASON = "Removed in leave clean-up 2026: not taken"
CHANGE_REASON = "Leave clean-up 2026: past pending, not taken"
# Prefixes of conversion.marker() and hr_records.marker(); test_past_pending
# checks they still match.
CONVERTED_PREFIX = "[Allocation #"
RECORDED_PREFIX = "[HR leave "
REPORT_COLUMNS = [
    "leave_request_id",
    "code",
    "employee_id",
    "leave_type",
    "start",
    "end",
    "days",
    "action",
    "flags",
]
MAPPING_COLUMNS = [
    "leave_request_id",
    "action",
    "code",
    "employee_id",
    "leave_type",
    "start",
    "end",
    "days",
    "original_reject_reason",
]


@dataclass
class Removal:
    """One past pending request a run would remove."""

    leave_request_id: int
    code: str
    employee_id: int
    leave_type: str
    start: date
    end: date
    days: float
    reject_reason: str
    action: str = "reject"  # reject | delete
    flags: list = field(default_factory=list)

    def report_row(self):
        return [
            self.leave_request_id,
            self.code,
            self.employee_id,
            self.leave_type,
            self.start,
            self.end,
            self.days,
            self.action,
            " ".join(self.flags),
        ]

    def mapping_row(self):
        return [
            self.leave_request_id,
            self.action,
            self.code,
            self.employee_id,
            self.leave_type,
            self.start,
            self.end,
            self.days,
            self.reject_reason,
        ]


def candidates(today):
    """The rule itself: pending, ended before today, active staff, not ours."""
    return (
        LeaveRequest._base_manager.filter(
            status="requested", end_date__lt=today, employee_id__is_active=True
        )
        .exclude(description__startswith=CONVERTED_PREFIX)
        .exclude(description__startswith=RECORDED_PREFIX)
    )


def _linked(app_label, model_name, ids):
    """Ids among ``ids`` that rows of that model point to (none if not installed)."""
    if not apps.is_installed(app_label):
        return set()
    model = apps.get_model(app_label, model_name)
    return set(
        model._base_manager.filter(leave_request_id__in=ids).values_list(
            "leave_request_id", flat=True
        )
    )


def plan_removals(today=None, delete=False):
    today = today or nairobi_today()
    requests = list(
        candidates(today)
        .select_related("employee_id", "leave_type_id")
        .order_by("end_date", "pk")
    )
    ids = [r.pk for r in requests]
    work_records = _linked("attendance", "WorkRecords", ids)
    penalties = _linked("base", "PenaltyAccounts", ids)
    removals = []
    for leave_request in requests:
        if deducted(leave_request):
            raise ConversionRefused(
                f"Pending leave request #{leave_request.pk} has days deducted; "
                "sort it out in the UI first."
            )
        removal = Removal(
            leave_request_id=leave_request.pk,
            code=employee_code(leave_request.employee_id),
            employee_id=leave_request.employee_id_id,
            leave_type=leave_request.leave_type_id.name,
            start=leave_request.start_date,
            end=leave_request.end_date,
            days=leave_request.requested_days,
            reject_reason=leave_request.reject_reason or "",
            action="delete" if delete else "reject",
        )
        if leave_request.pk in work_records:
            removal.flags.append("WORK_RECORDS")
        if leave_request.pk in penalties:
            removal.flags.append("PENALTY")
        removals.append(removal)
    if delete:
        # Deleting would take these with it (pre_delete receiver, CASCADE).
        linked = [
            r.leave_request_id
            for r in removals
            if {"WORK_RECORDS", "PENALTY"} & set(r.flags)
        ]
        if linked:
            raise ConversionRefused(
                f"--delete would also delete work records or penalties of leave "
                f"requests {linked}; reject instead (the default)."
            )
    return removals


def _state(spans):
    """Balances and work records of the people and dates concerned."""
    employee_ids = {employee_id for employee_id, _start, _end, _pk in spans}
    balances = list(
        AvailableLeave._base_manager.filter(employee_id__in=employee_ids)
        .order_by("pk")
        .values_list("pk", "available_days", "carryforward_days", "total_leave_days")
    )
    if not apps.is_installed("attendance") or not spans:
        return balances, []
    WorkRecords = apps.get_model("attendance", "WorkRecords")
    on_dates = Q()
    for employee_id, start, end, pk in spans:
        on_dates |= Q(employee_id=employee_id, date__range=(start, end))
        on_dates |= Q(leave_request_id=pk)
    records = list(
        WorkRecords._base_manager.filter(on_dates)
        .order_by("pk")
        .values_list(
            "pk", "work_record_type", "is_leave_record", "leave_request_id", "day_percentage"
        )
    )
    return balances, records


def _check_unchanged(before, spans):
    if _state(spans) != before:
        raise ConversionRefused(
            "Balances or attendance work records changed while removing; "
            "everything was rolled back."
        )


def _set_status(leave_request, status, reject_reason, change_reason):
    """
    Write only the status columns, through the parent save so the history
    row and signals still happen. LeaveRequest.save() would recount every
    request's clashes on each call (7 minutes for 44 on a production copy,
    with every open request locked meanwhile) and, back to requested, restart
    multi-level approval. Clashes are recounted once by _recount_clashes.
    """
    leave_request.status = status
    leave_request.reject_reason = reject_reason
    leave_request._change_reason = change_reason
    fields = ["status", "reject_reason"]
    if status == "rejected":
        leave_request.leave_clashes_count = 0  # as LeaveRequest.save() does
        fields.append("leave_clashes_count")
    super(LeaveRequest, leave_request).save(update_fields=fields)


def _recount_clashes():
    # update_leave_clashes_count recounts every open request except the one
    # it is called on; an unsaved instance (no id) leaves none out.
    LeaveRequest().update_leave_clashes_count()


def _lock(queryset):
    # Lock the requests first, then plan from them: a manager acting in the
    # UI meanwhile waits, and the count checked is the one that is saved.
    list(queryset.select_for_update(of=("self",)).values_list("pk", flat=True))


def apply_removals(today, confirm_rows, mapping_path, delete=False):
    assert_no_request()
    with transaction.atomic():
        _lock(candidates(today))
        removals = plan_removals(today, delete=delete)
        if len(removals) != confirm_rows:
            raise ConversionRefused(
                f"{len(removals)} past pending requests now, not {confirm_rows}; "
                "run the dry run again. Nothing written."
            )
        # Written before any change, so a committed run always has its undo
        # file; if the run rolls back, a reverse with it finds nothing to do.
        write_csv(mapping_path, MAPPING_COLUMNS, [r.mapping_row() for r in removals])
        spans = [(r.employee_id, r.start, r.end, r.leave_request_id) for r in removals]
        before = _state(spans)
        ids = [r.leave_request_id for r in removals]
        with attendance_sync_off():
            if delete:
                LeaveRequest._base_manager.filter(pk__in=ids).delete()
            else:
                for leave_request in LeaveRequest._base_manager.filter(pk__in=ids):
                    _set_status(leave_request, "rejected", REASON, CHANGE_REASON)
        _check_unchanged(before, spans)
        _recount_clashes()
    return removals


def read_mapping(raw_path):
    path = outside_repo(raw_path)
    if not path.is_file():
        raise ConversionRefused(f"File not found: {raw_path}")
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or set(MAPPING_COLUMNS) - set(rows[0]):
        raise ConversionRefused(f"{path.name} is not a past pending mapping file")
    return rows


def plan_reverse(rows):
    """[(leave request id, original reject reason)] still rejected by a run."""
    deleted = [row["leave_request_id"] for row in rows if row["action"] != "reject"]
    if deleted:
        raise ConversionRefused(
            f"That run deleted {len(deleted)} requests; they can only come back "
            "from the backup taken before it."
        )
    pending = []
    for row in rows:
        found = LeaveRequest._base_manager.filter(
            pk=int(row["leave_request_id"]), status="rejected", reject_reason=REASON
        ).first()
        if found is not None:
            pending.append((found.pk, row["original_reject_reason"]))
    return pending


def apply_reverse(rows, confirm_rows):
    assert_no_request()
    ids = [int(row["leave_request_id"]) for row in rows]
    with transaction.atomic():
        _lock(LeaveRequest._base_manager.filter(pk__in=ids))
        pending = plan_reverse(rows)
        if len(pending) != confirm_rows:
            raise ConversionRefused(
                f"{len(pending)} requests to restore now, not {confirm_rows}; "
                "run the dry run again. Nothing written."
            )
        restored = list(LeaveRequest._base_manager.filter(pk__in=[pk for pk, _ in pending]))
        spans = [(r.employee_id_id, r.start_date, r.end_date, r.pk) for r in restored]
        before = _state(spans)
        reasons = dict(pending)
        with attendance_sync_off():
            for leave_request in restored:
                _set_status(
                    leave_request,
                    "requested",
                    reasons[leave_request.pk],
                    "Leave clean-up 2026: past pending restored",
                )
        _check_unchanged(before, spans)
        _recount_clashes()
    return pending
