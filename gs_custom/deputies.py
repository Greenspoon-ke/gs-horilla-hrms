"""
Department deputies: view-only leave visibility for a department.

Used by base.methods.filtersubordinates* and horilla.decorators.manager_can_enter.
"""


def is_deputy_scope(perm):
    # Only leave view permissions; change/delete/approve never widen.
    return isinstance(perm, str) and perm.startswith("leave.view_")


def deputy_department_ids(user):
    if not getattr(user, "is_authenticated", False):
        return []
    employee = getattr(user, "employee_get", None)
    if employee is None:
        return []
    # Lazy import: base.methods and horilla.decorators call into this module,
    # and both load before the app registry is ready.
    from gs_custom.models import DepartmentDeputy

    return list(
        DepartmentDeputy.objects.filter(employee=employee, is_active=True)
        .values_list("department_id", flat=True)
    )
