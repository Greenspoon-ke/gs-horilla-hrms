from django.contrib import admin

from contract_records.models import ContractRecord


@admin.register(ContractRecord)
class ContractRecordAdmin(admin.ModelAdmin):
    """
    View-only. The sheet is the source of truth and the sync command is the
    only writer, so nobody may add, change or delete rows here, superusers
    included. Viewing still needs the view_contractrecord permission.
    """

    list_display = (
        "external_ref",
        "employee_code",
        "contract_type",
        "status",
        "start_date",
        "end_date",
    )
    list_filter = ("status", "contract_type")
    search_fields = ("employee_code", "external_ref")
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
