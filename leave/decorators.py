"""
decorator functions for leave
"""

from django.contrib import messages
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.utils.translation import gettext_lazy as _

from leave.methods import can_act_on, can_use_allocations
from leave.models import LeaveGeneralSetting

from .models import LeaveAllocationRequest

decorator_with_arguments = (
    lambda decorator: lambda *args, **kwargs: lambda func: decorator(
        func, *args, **kwargs
    )
)


def no_permission(request):
    """
    The app's usual "no permission" response (message, then back to the
    previous page), sent with status 403 so a refused call is visible as one.
    """
    messages.info(request, _("You dont have permission."))
    if "HTTP_HX_REQUEST" in request.META:
        return render(request, "decorator_404.html", status=403)
    previous_url = request.META.get("HTTP_REFERER", "/")
    script = f'<script>window.location.href = "{previous_url}"</script>'
    return HttpResponse(script, status=403)


def allocation_access_required(function):
    """Allocation pages: HR and reporting managers only (HR decision 4)."""

    def check_permission(request, *args, **kwargs):
        if can_use_allocations(request.user):
            return function(request, *args, **kwargs)
        return no_permission(request)

    return check_permission


@decorator_with_arguments
def leave_allocation_change_permission(function=None, *args, **kwargs):
    def check_permission(
        request,
        req_id=None,
        *args,
        **kwargs,
    ):
        """
        This method is used to check the employee can change a leave allocation request or not
        """
        leave_allocation_request = LeaveAllocationRequest.objects.get(id=req_id)
        # HR or this employee's own manager; staff no longer edit allocations.
        if can_act_on(
            request.user,
            leave_allocation_request,
            "leave.change_leaveallocationrequest",
        ):
            return function(request, *args, req_id=req_id, **kwargs)
        return no_permission(request)

    return check_permission


@decorator_with_arguments
def leave_allocation_delete_permission(function=None, *args, **kwargs):
    def check_permission(
        request,
        req_id=None,
        *args,
        **kwargs,
    ):
        """
        This method is used to check the employee can delete a leave allocation request or not
        """
        try:
            leave_allocation_request = LeaveAllocationRequest.objects.get(id=req_id)
            if can_act_on(
                request.user,
                leave_allocation_request,
                "leave.delete_leaveallocationrequest",
            ):
                return function(request, *args, req_id=req_id, **kwargs)
            return no_permission(request)
        except (LeaveAllocationRequest.DoesNotExist, OverflowError, ValueError):
            messages.error(request, _("Leave allocation request not found"))
            return redirect("/leave/leave-allocation-request-view/")

    return check_permission


@decorator_with_arguments
def leave_allocation_reject_permission(function=None, *args, **kwargs):
    def check_permission(
        request,
        req_id=None,
        *args,
        **kwargs,
    ):
        """
        This method is used to check the employee can approve or reject a leave
        allocation request or not
        """
        try:
            leave_allocation_request = LeaveAllocationRequest.objects.get(id=req_id)
            # change_ (not delete_): the permission the approve/reject buttons
            # and the API already use.
            if can_act_on(
                request.user,
                leave_allocation_request,
                "leave.change_leaveallocationrequest",
            ):
                return function(request, *args, req_id=req_id, **kwargs)
            return no_permission(request)
        except (LeaveAllocationRequest.DoesNotExist, OverflowError, ValueError):
            messages.error(request, _("Leave allocation request not found"))
            return redirect("/leave/leave-allocation-request-view/")

    return check_permission


@decorator_with_arguments
def is_compensatory_leave_enabled(func=None, *args, **kwargs):
    def function(request, *args, **kwargs):
        """
        This function check whether the compensatory leave feature is enabled
        """
        if (
            LeaveGeneralSetting.objects.exists()
            and LeaveGeneralSetting.objects.all().first().compensatory_leave
        ):
            return func(request, *args, **kwargs)
        messages.info(request, _("Sorry,Compensatory leave is not enabled."))
        previous_url = request.META.get("HTTP_REFERER", "/")
        script = f'<script>window.location.href = "{previous_url}"</script>'
        return HttpResponse(script)

    return function
