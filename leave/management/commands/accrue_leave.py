"""
Monthly Annual Leave accrual (rules in leave/accrual.py).

Daily mode credits every whole month that is due on rows already on accrual
and closes the leave year after December. Run it once a day from cron:

    python manage.py accrue_leave            # dry run: prints, saves nothing
    python manage.py accrue_leave --apply

One-off rebuild: sets 2026 balances from the rules and puts the rows on
accrual. Dry run first, then apply with the dry run's row count:

    python manage.py accrue_leave --rebuild --through 2026-09-30 \
        --sheet ~/hrms-local/joiners_2026_sheet_dates.csv \
        [--overrides ~/hrms-local/overrides.csv] \
        [--opening-carry ~/hrms-local/opening_carry.csv] \
        [--negative record|zero]
    python manage.py accrue_leave --rebuild --through 2026-09-30 ... \
        --apply --confirm-rows N

The sheet is HR's list of 2026 joiners (employee_code,join_date): a row on it
is enrolled from that date unless its Annual Leave was assigned before 2026;
a row not on it is pre-2026 staff and waits for opening carry.
overrides.csv columns: code,start_date (YYYY-MM-DD, replaces the join date).
opening_carry.csv columns: code,opening_carry.

Reports and snapshots go to --out-dir (default ~/hrms-local, mode 600 files);
paths inside the repository are refused. Output shows employee codes and
counts only. Deliberately NOT registered with the in-process scheduler: every
web worker would run its own copy.
"""

from collections import Counter
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from leave.accrual import (
    AccrualRefused,
    parse_carry,
    parse_sheet_date,
    parse_start,
    read_code_file,
    run_daily,
    run_rebuild,
)

REBUILD_ONLY = ("through", "sheet", "overrides", "opening_carry", "confirm_rows")


class Command(BaseCommand):
    help = "Annual Leave monthly accrual. Dry run unless --apply is given."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Save changes. Without it the database is not written.",
        )
        parser.add_argument(
            "--rebuild", action="store_true", help="One-off rebuild of 2026."
        )
        parser.add_argument(
            "--through",
            type=date.fromisoformat,
            help="Rebuild: last day of the last month to credit (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--sheet", help="Rebuild: HR's 2026 joiners, employee_code,join_date."
        )
        parser.add_argument("--overrides", help="Rebuild: CSV of code,start_date.")
        parser.add_argument(
            "--opening-carry", help="Rebuild: CSV of code,opening_carry."
        )
        parser.add_argument(
            "--negative",
            choices=["record", "zero"],
            default="record",
            help="Rebuild: keep a negative balance (record) or set it to 0.",
        )
        parser.add_argument(
            "--confirm-rows",
            type=int,
            help="Rebuild --apply: the dry run's 'would change' count.",
        )
        parser.add_argument(
            "--out-dir",
            default="~/hrms-local",
            help="Folder for reports and snapshots, outside the repository.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        try:
            if options["rebuild"]:
                if not options["through"]:
                    raise CommandError("--rebuild needs --through YYYY-MM-DD.")
                if not options["sheet"]:
                    raise CommandError("--rebuild needs --sheet (HR's joiners).")
                result = run_rebuild(
                    options["through"],
                    self.read(options["sheet"], "join_date", parse_sheet_date),
                    apply=apply,
                    out_dir=options["out_dir"],
                    overrides=self.read(
                        options["overrides"], "start_date", parse_start
                    ),
                    opening_carry=self.read(
                        options["opening_carry"], "opening_carry", parse_carry
                    ),
                    negative=options["negative"],
                    confirm_rows=options["confirm_rows"],
                )
            else:
                if any(options[name] for name in REBUILD_ONLY):
                    raise CommandError(
                        "--through, --sheet, --overrides, --opening-carry and "
                        "--confirm-rows need --rebuild."
                    )
                result = run_daily(apply=apply, out_dir=options["out_dir"])
        except AccrualRefused as error:
            raise CommandError(f"REFUSED: {error}") from None
        self.report(result, apply)

    @staticmethod
    def read(path, column, parse):
        return read_code_file(path, column, parse) if path else {}

    def report(self, result, apply):
        write = self.stdout.write
        mode = "APPLIED" if apply else "DRY RUN (database unchanged)"
        write(f"{mode}: {result['mode']} on {result['today']} (Africa/Nairobi)")
        plans = result["plans"]
        for plan in plans:
            if plan.action == "change":
                flags = f" [{' '.join(plan.flags)}]" if plan.flags else ""
                write(
                    f"  {plan.code}: months={plan.months} "
                    f"{plan.current[0]:g}+{plan.current[1]:g} -> "
                    f"{plan.proposed[0]:g}+{plan.proposed[1]:g}{flags}"
                )
            elif plan.action == "refuse":
                write(f"  REFUSED {plan.code}: {plan.reason}")
        counts = Counter(plan.action for plan in plans)
        write(
            f"rows: {len(plans)} | {'changed' if apply else 'would change'} "
            f"{counts['change']} | unchanged {counts['unchanged']} | "
            f"skipped {counts['skip']} | refused {counts['refuse']}"
        )
        skipped = Counter(plan.reason for plan in plans if plan.action == "skip")
        for reason, count in sorted(skipped.items()):
            write(f"  skipped ({reason}): {count}")
        flagged = Counter(
            flag for plan in plans if plan.action == "change" for flag in plan.flags
        )
        if flagged:
            write("flags: " + ", ".join(f"{k} {v}" for k, v in sorted(flagged.items())))
        if result.get("missing_from_sheet"):
            write(
                "2026 join date in system but not on HR sheet (held): "
                + ", ".join(result["missing_from_sheet"])
            )
        if result.get("unmatched_codes"):
            write(
                "codes in input files with no Annual Leave row: "
                + ", ".join(result["unmatched_codes"])
            )
        if apply:
            write(
                f"saved {len(result['saved'])}, changed since planning "
                f"(not saved) {len(result['stale'])}"
            )
            if result["stale"]:
                write(f"  not saved: {', '.join(result['stale'])}")
        for key in ("report", "snapshot"):
            if result.get(key):
                write(f"{key}: {result[key]}")
