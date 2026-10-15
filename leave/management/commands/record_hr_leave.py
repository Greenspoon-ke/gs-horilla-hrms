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

Remove past pending leave requests as not taken (rules in
leave/past_pending.py): rejected by default, --delete to delete them. Run it
after the records and before the rebuild:

    python manage.py record_hr_leave --past-pending
    python manage.py record_hr_leave --past-pending --apply --confirm-rows N
    python manage.py record_hr_leave --past-pending --reverse \\
        --mapping ~/hrms-local/past-pending-<stamp>-mapping.csv

Reports go to --out-dir (default ~/hrms-local, mode 600 files); paths inside
the repository are refused. Output shows refs, ids and employee codes only.
"""

from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from leave import past_pending
from leave.accrual import (
    AccrualRefused,
    _stamp,
    nairobi_today,
    prepare_out_dir,
    write_csv,
)
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
    help = (
        "Record leave HR confirms was taken, or remove past pending requests "
        "(--past-pending). Dry run unless --apply."
    )

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
        parser.add_argument(
            "--past-pending",
            action="store_true",
            help="Remove pending requests that ended before today, as not taken.",
        )
        parser.add_argument(
            "--delete",
            action="store_true",
            help="With --past-pending: delete instead of reject.",
        )
        parser.add_argument("--out-dir", default="~/hrms-local")

    def handle(self, *args, **options):
        try:
            self.check_arguments(options)
            if options["past_pending"] and options["reverse"]:
                self.reverse_past_pending(options)
            elif options["past_pending"]:
                self.remove_past_pending(options)
            elif options["reverse"]:
                self.reverse(options)
            else:
                self.record(options)
        except (ConversionRefused, AccrualRefused) as error:
            raise CommandError(f"REFUSED: {error}") from None

    @staticmethod
    def check_arguments(options):
        # One step per run, so each dry run count matches exactly one --apply.
        if options["past_pending"] and options["csv"]:
            raise ConversionRefused("--past-pending runs on its own, without --csv.")
        if options["delete"] and not options["past_pending"]:
            raise ConversionRefused("--delete only goes with --past-pending.")
        if options["delete"] and options["reverse"]:
            raise ConversionRefused("--reverse cannot be combined with --delete.")

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

    def remove_past_pending(self, options):
        write = self.stdout.write
        apply, delete = options["apply"], options["delete"]
        today = nairobi_today()
        removals = past_pending.plan_removals(today, delete=delete)
        self.check_count(options, len(removals))
        directory = prepare_out_dir(options["out_dir"])
        stamp = _stamp()
        write(
            f"{'APPLIED' if apply else 'DRY RUN (database unchanged)'}: past pending "
            f"leave requests (ended before {today}), {'delete' if delete else 'reject'}"
        )
        if apply:
            mapping = directory / f"past-pending-{stamp}-mapping.csv"
            removals = past_pending.apply_removals(
                today, options["confirm_rows"], mapping, delete=delete
            )
        report = write_csv(
            directory / f"past-pending-{stamp}-{'apply' if apply else 'dry'}.csv",
            past_pending.REPORT_COLUMNS,
            [removal.report_row() for removal in removals],
        )
        for removal in removals:
            flags = f" [{' '.join(removal.flags)}]" if removal.flags else ""
            write(
                f"  #{removal.leave_request_id} {removal.code} {removal.leave_type} "
                f"{removal.start}..{removal.end} ({removal.days:g}d): {removal.action}{flags}"
            )
        by_type = Counter(removal.leave_type for removal in removals)
        write(
            " | ".join(
                [f"{'removed' if apply else 'would remove'}: {len(removals)}"]
                + [f"{name} {by_type[name]}" for name in sorted(by_type)]
            )
        )
        if apply:
            write(f"mapping: {mapping}")
        write(f"report: {report}")

    def reverse_past_pending(self, options):
        if not options["mapping"]:
            raise ConversionRefused("--reverse needs --mapping.")
        write = self.stdout.write
        rows = past_pending.read_mapping(options["mapping"])
        pending = past_pending.plan_reverse(rows)
        self.check_count(options, len(pending))
        write(
            f"{'APPLIED' if options['apply'] else 'DRY RUN (database unchanged)'}: "
            "reverse past pending"
        )
        if options["apply"]:
            pending = past_pending.apply_reverse(rows, options["confirm_rows"])
        for leave_request_id, _reason in pending:
            write(f"  #{leave_request_id}: back to requested")
        write(f"{'reversed' if options['apply'] else 'would reverse'}: {len(pending)}")
