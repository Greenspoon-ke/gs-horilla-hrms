from django.contrib import admin

from .models import DepartmentDeputy


@admin.register(DepartmentDeputy)
class DepartmentDeputyAdmin(admin.ModelAdmin):
    list_display = ("employee", "department", "is_active")
    list_filter = ("department", "is_active")
