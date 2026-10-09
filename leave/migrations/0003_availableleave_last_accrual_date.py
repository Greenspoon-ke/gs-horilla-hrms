from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("leave", "0002_alter_leaveallocationrequest_attachment_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="availableleave",
            name="last_accrual_date",
            field=models.DateField(
                blank=True, null=True, verbose_name="Last Accrual Date"
            ),
        ),
        # simple_history mirrors every AvailableLeave field onto its history
        # table; without this column every balance save would fail.
        migrations.AddField(
            model_name="historicalavailableleave",
            name="last_accrual_date",
            field=models.DateField(
                blank=True, null=True, verbose_name="Last Accrual Date"
            ),
        ),
    ]
