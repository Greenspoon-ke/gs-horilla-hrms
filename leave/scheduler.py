import calendar
import datetime as dt
import sys
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from dateutil.relativedelta import relativedelta
from django.db import connections


def close_stale_connections():
    """
    Background threads never get Django's per-request connection cleanup, so a
    connection the database dropped (e.g. PostgreSQL restarted by automatic
    updates) would fail every later run. Same as close_old_connections, except
    that a connection inside a transaction is never touched.
    """
    for connection in connections.all(initialized_only=True):
        if not connection.in_atomic_block:
            connection.close_if_unusable_or_obsolete()


def leave_reset():
    """Runs every 20 s in a background thread of each web process."""
    close_stale_connections()
    try:
        _reset_balances()
    finally:
        close_stale_connections()


def _reset_balances():
    # Imported here: this module loads from leave/__init__.py, before models.
    from leave.accrual import ACCRUAL_LEAVE_TYPE_NAME
    from leave.models import LeaveType

    today = datetime.now()
    today_date = today.date()
    leave_types = LeaveType.objects.filter(reset=True)
    # Looping through filtered leave types with reset is true
    for leave_type in leave_types:
        # Annual Leave carry forward never expires (HR decision D5'). Its type
        # is "carryforward expire", and the expiry below zeroes the carry and
        # refills 24 days, so it is switched off for that type only.
        expiry_enabled = leave_type.name != ACCRUAL_LEAVE_TYPE_NAME
        # Looping through all available leaves
        available_leaves = leave_type.employee_available_leave.all()

        for available_leave in available_leaves:
            # Rows on monthly accrual are owned by the accrue_leave command;
            # resetting them here would grant the full year up front.
            if available_leave.last_accrual_date is not None:
                continue
            reset_date = available_leave.reset_date
            expired_date = available_leave.expired_date
            if reset_date == today_date:
                available_leave.update_carryforward()
                # new_reset_date = available_leave.set_reset_date(assigned_date=today_date,available_leave = available_leave)
                new_reset_date = available_leave.set_reset_date(
                    assigned_date=today_date, available_leave=available_leave
                )
                available_leave.reset_date = new_reset_date
                available_leave.save()
            if expiry_enabled and expired_date and expired_date <= today_date:
                new_expired_date = available_leave.set_expired_date(
                    available_leave=available_leave, assigned_date=today_date
                )
                available_leave.expired_date = new_expired_date
                available_leave.save()

        if (
            expiry_enabled
            and leave_type.carryforward_expire_date
            and leave_type.carryforward_expire_date <= today_date
        ):
            leave_type.carryforward_expire_date = leave_type.set_expired_date(
                today_date
            )
            leave_type.save()


if not any(
    cmd in sys.argv
    # "test": the 20s job would otherwise mutate rows while tests run.
    # "accrue_leave": keep that command the only writer of leave balances
    # while it runs.
    for cmd in [
        "makemigrations",
        "migrate",
        "compilemessages",
        "flush",
        "shell",
        "test",
        "accrue_leave",
    ]
):
    """
    Initializes and starts background tasks using APScheduler when the server is running.
    """
    scheduler = BackgroundScheduler()
    scheduler.add_job(leave_reset, "interval", seconds=20)

    scheduler.start()
