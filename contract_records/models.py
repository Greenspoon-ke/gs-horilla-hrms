"""
One row per contract in the "Greenspoon Employee Contracts" sheet.

The sheet is the source of truth; ``sync_contract_records`` copies it here
one way. This is a plain ``models.Model`` on purpose: no HorillaModel (which
reads the request for created_by) and no simple_history. Nothing here feeds
payroll, leave or the employee's joining date.

Deliberately not stored: names, emails, salary, manager, the sheet's Notes,
and the PDF link (it goes stale when the sheet regenerates a PDF).
"""

from django.db import models

from employee.models import Employee


class ContractRecord(models.Model):
    TYPE_CHOICES = [
        ("permanent", "Permanent"),
        ("fixed_1y", "Fixed-term 1 year"),
        ("fixed_6m", "Fixed-term 6 months"),
        ("fixed_3m", "Fixed-term 3 months"),
        ("casual", "Casual"),
        ("addendum", "Addendum"),
        ("other", "Other"),
    ]
    STATUS_CHOICES = [
        ("active", "Active"),
        ("generated", "Generated"),
        ("probation_confirmed", "Probation confirmed"),
        ("probation_extended", "Probation extended"),
        ("expired", "Expired"),
        ("renewed", "Renewed"),
        ("exited", "Exited"),
        ("terminated", "Terminated"),
        ("deleted", "Deleted"),
        ("other", "Other"),
    ]

    # The sheet's ContractID, trimmed and upper-cased: the idempotency key.
    external_ref = models.CharField(max_length=100, unique=True)
    # SET_NULL: deleting an employee must not delete their contract history.
    employee = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contract_records",
    )
    # The file's code, normalised, as it was when last written. Survives an
    # employee deletion or a badge edit in the HRMS.
    employee_code = models.CharField(max_length=50, db_index=True)
    contract_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    contract_type_raw = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    status_raw = models.CharField(max_length=100, blank=True)
    # Copied as the sheet has them, never recomputed.
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    # Free text exactly as in the sheet (whitespace tidied); no FK mapping yet.
    department_text = models.CharField(max_length=255, blank=True)
    role_text = models.CharField(max_length=255, blank=True)
    doc_url = models.URLField(max_length=500, blank=True)
    signed_url = models.URLField(max_length=500, blank=True)
    signed_at = models.DateTimeField(null=True, blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)
    probation_confirmed = models.BooleanField(default=False)
    # Batch that created the row: --undo-batch deletes by this, and only this.
    import_batch = models.CharField(max_length=40, db_index=True)
    # Batch and export time of the last run that changed the row.
    last_batch = models.CharField(max_length=40)
    last_export_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["employee_code", "start_date", "external_ref"]

    def __str__(self):
        return self.external_ref
