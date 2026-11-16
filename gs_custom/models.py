"""
Greenspoon-only models, kept apart from upstream Horilla apps.
"""

from django.db import models


class DepartmentDeputy(models.Model):
    """
    An employee who can view leave data for a whole department, the way a
    reporting manager sees their team. Viewing only: approving and editing
    stay with real managers and HR (see gs_custom.deputies).

    Plain models.Model rather than HorillaModel so it stays out of
    horilla_audit and its company/created_by plumbing.
    """

    employee = models.ForeignKey(
        "employee.Employee",
        on_delete=models.CASCADE,
        related_name="deputy_departments",
    )
    department = models.ForeignKey(
        "base.Department",
        on_delete=models.CASCADE,
        related_name="deputies",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("employee", "department")

    def __str__(self):
        return f"{self.employee} → {self.department}"
