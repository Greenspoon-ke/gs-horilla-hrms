"""
Convert leave allocation requests made before LEAVE_ALLOCATION_CUTOVER into
leave requests (rules in leave/conversion.py). Dry run first, then apply with
the dry run's row count:

    python manage.py convert_leave_allocations \
        [--decisions ~/hrms-local/allocation-decisions.csv]
    python manage.py convert_leave_allocations [...] --apply --confirm-rows N

decisions.csv columns: alloc_id,keep_drop,start_date,end_date. keep_drop is
keep, drop or blank (default: duplicates dropped); blank dates mean assumed
dates. Run accrue_leave --rebuild afterwards: it counts the approved ones.

Undo with the mapping file an --apply run wrote:

    python manage.py convert_leave_allocations --reverse \
        --mapping ~/hrms-local/allocation-conversion-<stamp>-mapping.csv
    python manage.py convert_leave_allocations --reverse --mapping ... \
        --apply --confirm-rows N

Reports go to --out-dir (default ~/hrms-local, mode 600 files); paths inside
the repository are refused. Output shows ids and employee codes only.
"""

from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from leave.accrual import (
    AccrualRefused,
    _stamp,
    allocation_cutover,
    prepare_out_dir,
    write_csv,
)
from leave.conversion import (
    MAPPING_COLUMNS,
    REPORT_COLUMNS,
    ConversionRefused,
    apply_plans,
    apply_reverse,
    plan_all,
    plan_reverse,
    read_mapping,
)


class Command(BaseCommand):
    help = "Convert pre-cut-over leave allocations into leave requests. Dry run unless --apply."

    def add_arguments(self, parser):
        parser.add_argument("--decisions", help="HR's decisions CSV (outside the repo)")
        parser.add_argument("--apply", action="store_true", help="Save changes.")
        parser.add_argument(
            "--confirm-rows",
            type=int,
            help="With --apply: must equal the dry run's number of rows to change.",
        )
        parser.add_argument("--reverse", action="store_true", help="Undo a conversion.")
        parser.add_argument("--mapping", help="With --reverse: the mapping CSV to undo.")
        parser.add_argument("--out-dir", default="~/hrms-local")

    def handle(self, *args, **options):
        try:
            if options["reverse"]:
                self.reverse(options)
            else:
                self.convert(options)
        except (ConversionRefused, AccrualRefused) as error:
            raise CommandError(f"REFUSED: {error}") from None

    @staticmethod
    def check_count(options, count):
        if options["apply"] and options["confirm_rows"] != count:
            raise ConversionRefused(
                f"--confirm-rows must equal the {count} rows this run would change "
                "(see the dry run). Nothing written."
            )

    def convert(self, options):
        if options["mapping"]:
            raise ConversionRefused("--mapping is only for --reverse.")
        write = self.stdout.write
        apply = options["apply"]
        plans, duplicate_of, decisions = plan_all(options["decisions"])
        changes = [plan for plan in plans if plan.action != "none"]
        self.check_count(options, len(changes))
        directory = prepare_out_dir(options["out_dir"])
        stamp = _stamp()
        write(
            f"{'APPLIED' if apply else 'DRY RUN (database unchanged)'}: allocations "
            f"created before {allocation_cutover()} (Africa/Nairobi)"
        )
        stale = []
        if apply:
            done, stale = apply_plans(changes, duplicate_of, decisions)
            mapping = write_csv(
                directory / f"allocation-conversion-{stamp}-mapping.csv",
                MAPPING_COLUMNS,
                [
                    [
                        plan.alloc_id,
                        plan.action,
                        plan.leave_request_id or "",
                        plan.original_status,
                        plan.original_reason,
                    ]
                    for plan in done
                ],
            )
        report = write_csv(
            directory / f"allocation-conversion-{stamp}-{'apply' if apply else 'dry'}.csv",
            REPORT_COLUMNS,
            [plan.report_row() for plan in plans],
        )
        for plan in changes:
            flags = f" [{' '.join(plan.flags)}]" if plan.flags else ""
            dates = f" {plan.start}..{plan.end}" if plan.start else ""
            write(
                f"  #{plan.alloc_id} {plan.code} {plan.leave_type} {plan.days:g}d "
                f"{plan.original_status}: {plan.action}{dates}{flags}"
            )
        counts = Counter(plan.action for plan in plans)
        write(
            " | ".join(
                [f"allocations: {len(plans)}"]
                + [f"{action} {counts[action]}" for action in sorted(counts)]
            )
        )
        flagged = Counter(flag for plan in plans for flag in plan.flags)
        if flagged:
            write("flags: " + ", ".join(f"{k} {v}" for k, v in sorted(flagged.items())))
        if apply:
            write(f"saved {len(changes) - len(stale)}, changed since planning (not saved) {len(stale)}")
            if stale:
                write(f"  not saved: {stale}")
            write(f"mapping: {mapping}")
        write(f"report: {report}")

    def reverse(self, options):
        if not options["mapping"]:
            raise ConversionRefused("--reverse needs --mapping.")
        if options["decisions"]:
            raise ConversionRefused("--decisions is not used with --reverse.")
        write = self.stdout.write
        pending = plan_reverse(read_mapping(options["mapping"]))
        self.check_count(options, len(pending))
        write(f"{'APPLIED' if options['apply'] else 'DRY RUN (database unchanged)'}: reverse")
        for alloc_id, status, _reason, leave_request_id in pending:
            removed = f", delete leave request #{leave_request_id}" if leave_request_id else ""
            write(f"  #{alloc_id}: restore status {status}{removed}")
        if options["apply"]:
            apply_reverse(pending)
        write(f"{'reversed' if options['apply'] else 'would reverse'}: {len(pending)}")
