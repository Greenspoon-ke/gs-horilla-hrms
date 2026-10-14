"""
Record leave HR confirms was taken but that is missing from the system (rules
in leave/hr_records.py). Dry run first, then apply with the dry run's count:

    python manage.py record_hr_leave --csv ~/hrms-local/hr-leave-records.csv
    python manage.py record_hr_leave --csv ... --apply --confirm-rows N

CSV columns: ref,code,leave_type,start_date,end_date,status. Run
accrue_leave --rebuild afterwards: it counts the approved ones.

Undo the requests a run created, with the mapping file it wrote:

    python manage.py record_hr_leave --reverse --mapping ~/hrms-local/hr-leave-<stamp>-mapping.csv
    python manage.py record_hr_leave --reverse --mapping ... --apply --confirm-rows N

Reports go to --out-dir (default ~/hrms-local, mode 600 files); paths inside
the repository are refused. Output shows refs, ids and employee codes only.
"""

from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from leave.accrual import AccrualRefused, _stamp, prepare_out_dir, write_csv
from leave.conversion import ConversionRefused
from leave.hr_records import (
    MAPPING_COLUMNS,
    REPORT_COLUMNS,
    apply_plans,
    apply_reverse,
    plan_all,
    plan_reverse,
    read_mapping,
)


class Command(BaseCommand):
    help = "Record leave HR confirms was taken. Dry run unless --apply."

    def add_arguments(self, parser):
        parser.add_argument("--csv", help="HR's leave records CSV (outside the repo)")
        parser.add_argument("--apply", action="store_true", help="Save changes.")
        parser.add_argument(
            "--confirm-rows",
            type=int,
            help="With --apply: must equal the dry run's number of rows to change.",
        )
        parser.add_argument("--reverse", action="store_true", help="Undo a run.")
        parser.add_argument("--mapping", help="With --reverse: the mapping CSV to undo.")
        parser.add_argument("--out-dir", default="~/hrms-local")

    def handle(self, *args, **options):
        try:
            if options["reverse"]:
                self.reverse(options)
            else:
                self.record(options)
        except (ConversionRefused, AccrualRefused) as error:
            raise CommandError(f"REFUSED: {error}") from None

    @staticmethod
    def check_count(options, count):
        if options["apply"] and options["confirm_rows"] != count:
            raise ConversionRefused(
                f"--confirm-rows must equal the {count} rows this run would change "
                "(see the dry run). Nothing written."
            )

    def record(self, options):
        if not options["csv"]:
            raise ConversionRefused("--csv is required.")
        write = self.stdout.write
        apply = options["apply"]
        plans = plan_all(options["csv"])
        changes = [plan for plan in plans if plan.action != "none"]
        self.check_count(options, len(changes))
        directory = prepare_out_dir(options["out_dir"])
        stamp = _stamp()
        write(f"{'APPLIED' if apply else 'DRY RUN (database unchanged)'}: HR leave records")
        stale = []
        if apply:
            done, stale = apply_plans(changes)
            mapping = write_csv(
                directory / f"hr-leave-{stamp}-mapping.csv",
                MAPPING_COLUMNS,
                [[plan.ref, plan.action, plan.leave_request_id] for plan in done],
            )
        report = write_csv(
            directory / f"hr-leave-{stamp}-{'apply' if apply else 'dry'}.csv",
            REPORT_COLUMNS,
            [plan.report_row() for plan in plans],
        )
        for plan in plans:
            flags = f" [{' '.join(plan.flags)}]" if plan.flags else ""
            write(
                f"  {plan.ref} {plan.code} {plan.leave_type.name} {plan.status} "
                f"{plan.start}..{plan.end} ({plan.computed_days:g}d): {plan.action}{flags}"
            )
        counts = Counter(plan.action for plan in plans)
        write(
            " | ".join(
                [f"records: {len(plans)}"]
                + [f"{action} {counts[action]}" for action in sorted(counts)]
            )
        )
        if apply:
            write(f"saved {len(changes) - len(stale)}, changed since planning (not saved) {len(stale)}")
            if stale:
                write(f"  not saved: {stale}")
            write(f"mapping: {mapping}")
        write(f"report: {report}")

    def reverse(self, options):
        if not options["mapping"]:
            raise ConversionRefused("--reverse needs --mapping.")
        write = self.stdout.write
        pending = plan_reverse(read_mapping(options["mapping"]))
        self.check_count(options, len(pending))
        write(f"{'APPLIED' if options['apply'] else 'DRY RUN (database unchanged)'}: reverse")
        for ref, leave_request_id in pending:
            write(f"  {ref}: delete leave request #{leave_request_id}")
        if options["apply"]:
            apply_reverse(pending)
        write(f"{'reversed' if options['apply'] else 'would reverse'}: {len(pending)}")
