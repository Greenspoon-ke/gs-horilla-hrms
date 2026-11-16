import calendar
import contextlib
from datetime import datetime, timedelta

from django.apps import apps
from django.db.models import Q
from django.utils.translation import gettext as _

from employee.models import Employee
from horilla.methods import get_horilla_model_class


def calculate_requested_days(
    start_date, end_date, start_date_breakdown, end_date_breakdown
):
    if start_date == end_date:
        return (
            1
            if start_date_breakdown == "full_day" and end_date_breakdown == "full_day"
            else 0.5
        )

    # Count full days between the two dates, excluding start and end
    middle_days = (end_date - start_date).days - 1

    # Count start and end days
    start_day_value = 1 if start_date_breakdown == "full_day" else 0.5
    end_day_value = 1 if end_date_breakdown == "full_day" else 0.5

    return middle_days + start_day_value + end_day_value


def holiday_dates_list(holidays):
    """
    :return: This function returns a list of all holiday dates.
    """
    holiday_dates = []
    for holiday in holidays:
        holiday_start_date = holiday.start_date
        holiday_end_date = holiday.end_date or holiday_start_date
        holiday_dates.extend(
            holiday_start_date + timedelta(i)
            for i in range((holiday_end_date - holiday_start_date).days + 1)
        )
    return holiday_dates


def company_leave_dates_list(company_leaves, start_date):
    """
    :return: This function returns a list of all company leave dates
    """
    company_leave_dates = set()
    year = start_date.year
    for company_leave in company_leaves:
        based_on_week = company_leave.based_on_week
        based_on_week_day = company_leave.based_on_week_day

        for month in range(1, 13):
            month_calendar = calendar.monthcalendar(year, month)

            if based_on_week is not None:
                # Set Sunday as the first day of the week
                calendar.setfirstweekday(6)
                try:
                    week_days = [
                        day for day in month_calendar[int(based_on_week)] if day != 0
                    ]
                    for day in week_days:
                        date = datetime(year, month, day)
                        if date.weekday() == int(based_on_week_day):
                            company_leave_dates.add(date.date())
                except IndexError:
                    pass
            else:
                # Set Monday as the first day of the week
                calendar.setfirstweekday(0)
                for week in month_calendar:
                    if week[int(based_on_week_day)] != 0:
                        date = datetime(year, month, week[int(based_on_week_day)])
                        company_leave_dates.add(date.date())

    return list(company_leave_dates)


def get_leave_day_attendance(employee, comp_id=None):
    """
    This function returns a queryset of attendance on leave dates
    """
    Attendance = get_horilla_model_class(app_label="attendance", model="attendance")
    from leave.models import CompensatoryLeaveRequest

    attendances_to_exclude = Attendance.objects.none()  # Empty queryset to start with
    # Check for compensatory leave requests that are not rejected and not the current one
    if (
        CompensatoryLeaveRequest.objects.filter(employee_id=employee)
        .exclude(Q(id=comp_id) | Q(status="rejected"))
        .exists()
    ):
        comp_leave_reqs = CompensatoryLeaveRequest.objects.filter(
            employee_id=employee
        ).exclude(Q(id=comp_id) | Q(status="rejected"))
        for req in comp_leave_reqs:
            attendances_to_exclude |= req.attendance_id.all()
    # Filter holiday attendance excluding the attendances in attendances_to_exclude
    holiday_attendance = Attendance.objects.filter(
        is_holiday=True, employee_id=employee, attendance_validated=True
    ).exclude(id__in=attendances_to_exclude.values_list("id", flat=True))
    return holiday_attendance


def attendance_days(employee, attendances):
    """
    This function returns count of workrecord from the attendance
    """
    attendance_days = 0
    if apps.is_installed("attendance"):
        from attendance.models import WorkRecords

        for attendance in attendances:
            if WorkRecords.objects.filter(
                employee_id=employee, date=attendance.attendance_date
            ).exists():
                work_record_type = (
                    WorkRecords.objects.filter(
                        employee_id=employee, date=attendance.attendance_date
                    )
                    .first()
                    .work_record_type
                )
                if work_record_type == "HDP":
                    attendance_days += 0.5
                elif work_record_type == "FDP":
                    attendance_days += 1
    return attendance_days


def filter_conditional_leave_request(request):
    """
    Filters and returns LeaveRequest objects that have been conditionally approved by the previous sequence of approvals.
    """
    approval_manager = Employee.objects.filter(employee_user_id=request.user).first()
    leave_request_ids = []
    if apps.is_installed("leave"):
        from leave.models import LeaveRequest, LeaveRequestConditionApproval

        multiple_approval_requests = LeaveRequestConditionApproval.objects.filter(
            manager_id=approval_manager
        )
    else:
        multiple_approval_requests = None
    for instance in multiple_approval_requests:
        if instance.sequence > 1:
            pre_sequence = instance.sequence - 1
            leave_request_id = instance.leave_request_id
            instance = LeaveRequestConditionApproval.objects.filter(
                leave_request_id=leave_request_id, sequence=pre_sequence
            ).first()
            if instance and instance.is_approved:
                leave_request_ids.append(instance.leave_request_id.id)
        else:
            leave_request_ids.append(instance.leave_request_id.id)
    return LeaveRequest.objects.filter(pk__in=leave_request_ids)


# Group that holds HR's leave-allocation permissions. Its members are told
# when a manager grants days directly (HR decision Q7).
HR_LEAVE_GROUP = "HR – Leave"


def can_use_allocations(user):
    """
    The allocation page is for HR (the view permission) and reporting
    managers only; staff take time off through leave requests.
    """
    from base.templatetags.basefilters import is_reportingmanager

    return user.has_perm("leave.view_leaveallocationrequest") or is_reportingmanager(
        user
    )


def can_act_on(user, instance, perm):
    """
    Object-level check for one leave request or allocation (audit S1): the
    permission holder, this employee's own reporting manager, or one of this
    leave request's conditional approvers. Being a manager of someone else is
    not enough.
    """
    from base.methods import check_manager

    if user.has_perm(perm):
        return True
    employee = getattr(user, "employee_get", None)
    if employee is None:
        return False
    if check_manager(employee, instance):
        return True
    multiple_approvals = getattr(instance, "multiple_approvals", None)
    if multiple_approvals is None:
        return False
    conditions = multiple_approvals()
    return bool(conditions) and employee in conditions["managers"]


def chain_step(user, leave_request):
    """
    The Multiple Approvals step `user` may approve now on `leave_request`.

    Returns (step, is_last, refusal). With a refusal, the user may not approve
    yet and nothing should change. Steps go in sequence order and the request
    is final only once every step is approved. Upstream finalised whenever the
    last person in an unordered list clicked, so approver 2 could approve alone,
    and anyone outside the chain (HR, the reporting manager) hit a 500.
    """
    from leave.models import LeaveRequestConditionApproval

    steps = list(
        LeaveRequestConditionApproval.objects.filter(
            leave_request_id=leave_request
        ).order_by("sequence")
    )
    employee = getattr(user, "employee_get", None)
    my_steps = [
        s for s in steps if employee is not None and s.manager_id_id == employee.pk
    ]
    mine = next((s for s in my_steps if not s.is_approved), None)
    if mine is None:
        if my_steps:
            return None, False, _("You have already approved this leave request.")
        pending = ", ".join(str(s.manager_id) for s in steps if not s.is_approved)
        return None, False, _("This leave request is waiting for: {}.").format(pending)
    earlier = [s for s in steps if s.sequence < mine.sequence and not s.is_approved]
    if earlier:
        return (
            None,
            False,
            _("{} has to approve this leave request first.").format(
                earlier[0].manager_id
            ),
        )
    is_last = all(s.is_approved for s in steps if s.pk != mine.pk)
    return mine, is_last, None


def hide_pending_chains(request, queryset):
    """
    Leave lists hide requests still going through a Multiple Approvals chain,
    so a reporting manager outside the chain does not act on them; the
    approver whose turn it is gets them back via filter_conditional_leave_request.
    HR (leave.view_leaverequest) and department deputies keep seeing them, as
    approving and rejecting are guarded by chain_step and chain_rejecter_step.
    Only requested ones are hidden; upstream hid rejected and approved ones for
    good because their other steps stay unmarked.
    """
    from gs_custom.deputies import deputy_department_ids
    from leave.models import LeaveRequest, LeaveRequestConditionApproval

    if request.user.has_perm("leave.view_leaverequest"):
        return queryset
    pending = LeaveRequestConditionApproval.objects.filter(
        is_approved=False, is_rejected=False, leave_request_id__status="requested"
    )
    dept_ids = deputy_department_ids(request.user)
    if dept_ids:
        pending = pending.exclude(
            leave_request_id__employee_id__employee_work_info__department_id__in=dept_ids
        )
    return (
        LeaveRequest.objects.filter(id__in=queryset.values("id"))
        .exclude(id__in=pending.values("leave_request_id"))
        .distinct()
    )


def next_chain_approver(leave_request):
    """The employee whose approval step is next, or None when all are done."""
    from leave.models import LeaveRequestConditionApproval

    step = (
        LeaveRequestConditionApproval.objects.filter(
            leave_request_id=leave_request, is_approved=False
        )
        .order_by("sequence")
        .first()
    )
    return step.manager_id if step else None


def chain_rejecter_step(user, leave_request):
    """
    The step a non-superuser rejects through on a request with a Multiple
    Approvals chain: any approver in the chain may reject the whole request,
    in any order. None means the user is not in the chain and may not reject
    it (upstream raised IndexError, a 500, for HR and the reporting manager).
    """
    from leave.models import LeaveRequestConditionApproval

    employee = getattr(user, "employee_get", None)
    if employee is None:
        return None
    return (
        LeaveRequestConditionApproval.objects.filter(
            leave_request_id=leave_request, manager_id=employee
        )
        .order_by("sequence")
        .first()
    )


def can_grant_directly(user, employee):
    """
    A manager's or HR's allocation for someone else applies at once; nobody
    grants days to themselves without a second person approving.
    """
    from base.methods import check_manager

    me = getattr(user, "employee_get", None)
    if me is None or me == employee:
        return False
    return user.has_perm("leave.add_leaveallocationrequest") or check_manager(
        me, employee
    )


def approve_allocation(allocation):
    """
    Grant an allocation's days: the single place this happens (web approve,
    API approve, and direct grants on create). Adds requested_days to the
    employee's balance, creating the row if missing, and marks it approved.
    Returns False and changes nothing unless the allocation is still requested,
    so a double click or a retried request cannot grant twice.
    """
    from django.db import transaction

    from leave.accrual import prepare_new_balance
    from leave.models import AvailableLeave, LeaveAllocationRequest

    with transaction.atomic():
        # _base_manager: the company manager's distinct() cannot be locked.
        allocation = LeaveAllocationRequest._base_manager.select_for_update().get(
            pk=allocation.pk
        )
        if allocation.status != "requested":
            return False
        available_leave = AvailableLeave.objects.filter(
            employee_id=allocation.employee_id,
            leave_type_id=allocation.leave_type_id,
        ).first() or prepare_new_balance(
            AvailableLeave(
                employee_id=allocation.employee_id,
                leave_type_id=allocation.leave_type_id,
            )
        )
        available_leave.available_days += allocation.requested_days
        available_leave.save()
        allocation.status = "approved"
        allocation.save()
    return True


def revoke_allocation(allocation):
    """
    Take an approved allocation's days back off the balance. No floor at 0:
    a balance already in debt must stay correct (HR decision 3).
    """
    from leave.models import AvailableLeave

    available_leave = AvailableLeave.objects.filter(
        leave_type_id=allocation.leave_type_id,
        employee_id=allocation.employee_id,
    ).first()
    if available_leave is not None:
        available_leave.available_days -= allocation.requested_days
        available_leave.save()


def notify_direct_grant(actor, allocation):
    """Tell the employee, and HR (HR decision Q7), that days were granted."""
    from django.contrib.auth.models import User
    from django.urls import reverse

    from notifications.signals import notify

    redirect = reverse("leave-allocation-request-view") + f"?id={allocation.id}"
    with contextlib.suppress(Exception):
        notify.send(
            actor,
            recipient=allocation.employee_id.employee_user_id,
            verb="Your leave allocation request has been approved",
            verb_ar="تمت الموافقة على طلب تخصيص إجازتك",
            verb_de="Ihr Antrag auf Urlaubszuweisung wurde genehmigt",
            verb_es="Se ha aprobado su solicitud de asignación de vacaciones",
            verb_fr="Votre demande d'allocation de congé a été approuvée",
            icon="people-circle",
            redirect=redirect,
        )
    hr_users = User.objects.filter(groups__name=HR_LEAVE_GROUP, is_active=True)
    hr_users = hr_users.exclude(pk=getattr(actor.employee_user_id, "pk", None))
    if hr_users.exists():
        with contextlib.suppress(Exception):
            notify.send(
                actor,
                recipient=list(hr_users),
                verb=f"{allocation.requested_days:g} day(s) of "
                f"{allocation.leave_type_id} granted to {allocation.employee_id}.",
                icon="people-circle",
                redirect=redirect,
            )
