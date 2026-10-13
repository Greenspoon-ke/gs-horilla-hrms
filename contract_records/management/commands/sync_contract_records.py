"""
Copy the contracts sheet export into ContractRecord (rules in
contract_records/sync.py). Needs CONTRACT_NAME_SALT in the environment.

    python manage.py sync_contract_records --csv ~/hrms-local/contracts.csv
    python manage.py sync_contract_records --csv ... --apply
    python manage.py sync_contract_records --csv ... --report-joining
    python manage.py sync_contract_records --undo-batch BATCH [--apply]

Dry run unless --apply. Files must be outside the repository. Output is
counts, the file's SHA-256 and the batch id; the review file (mode 600) holds
line numbers, employee codes and reason codes only.

--undo-batch deletes only rows that batch created. To restore rows it
updated, re-run the previous export with --force (it is older, so the stale
guard refuses it otherwise); keep every export, mode 600, outside the repo.
"""

import sys
import time

from apscheduler.schedulers.base import BaseScheduler
from django.core.management.base import BaseCommand, CommandError

from contract_records.sync import (
    BUCKETS,
    SyncRefused,
    run_joining_report,
    run_sync,
    run_undo,
)

SYNC_ONLY = ("accept_file", "report_joining", "force")


def stop_in_process_schedulers():
    """
    Every app's scheduler.py starts an APScheduler when Django loads, unless
    the command name is on that app's guard list; this command is on none.
    Stop them in this process only (the web workers are untouched), so the
    20-second leave_reset and 25-second disciplinary jobs cannot run while
    this command does. Returns how many were stopped.
    """
    stopped = 0
    for name, module in list(sys.modules.items()):
        if not name.endswith(".scheduler") or name.startswith(
            ("apscheduler", "django_apscheduler")
        ):
            continue
        scheduler = getattr(module, "scheduler", None)
        if isinstance(scheduler, BaseScheduler) and scheduler.running:
            scheduler.shutdown(wait=True)
            stopped += 1
    return stopped


class Command(BaseCommand):
    help = (
        "Load the contracts sheet export into ContractRecord. Dry run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--csv", help="The export file, outside the repository.")
        parser.add_argument(
            "--apply", action="store_true", help="Write. Without it nothing is saved."
        )
        parser.add_argument(
            "--review-out", help="Review file path (default ~/hrms-local/...)."
        )
        parser.add_argument(
            "--accept-file",
            help="CSV with an employee_code column: codes HR confirmed after review.",
        )
        parser.add_argument("--undo-batch", help="Delete the rows this batch created.")
        parser.add_argument(
            "--report-joining",
            action="store_true",
            help="Read-only: earliest contract start against date_joining.",
        )
        parser.add_argument(
            "--min-ratio",
            type=float,
            default=0.8,
            help="Refuse a file with fewer rows than this share of stored records.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Skip the stale-file and partial-export guards only.",
        )

    def handle(self, *args, **options):
        started = time.monotonic()
        stopped = stop_in_process_schedulers()
        try:
            if options["undo_batch"]:
                if options["csv"] or any(options[name] for name in SYNC_ONLY):
                    raise CommandError("--undo-batch takes only --apply.")
                self.report_undo(run_undo(options["undo_batch"], options["apply"]))
            elif not options["csv"]:
                raise CommandError("Give --csv PATH, or --undo-batch ID.")
            elif options["report_joining"]:
                if options["apply"]:
                    raise CommandError("--report-joining is read-only; drop --apply.")
                self.report_joining(
                    run_joining_report(
                        options["csv"], options["review_out"], options["accept_file"]
                    )
                )
            else:
                result = run_sync(
                    options["csv"],
                    apply=options["apply"],
                    review_out=options["review_out"],
                    accept_path=options["accept_file"],
                    min_ratio=options["min_ratio"],
                    force=options["force"],
                )
                self.report_sync(result)
                if not result["identity_ok"]:
                    raise CommandError("Row counts do not add up to rows read.")
        except SyncRefused as error:
            raise CommandError(f"REFUSED: {error}") from None
        self.stdout.write(
            f"schedulers stopped in this process: {stopped} | "
            f"seconds: {time.monotonic() - started:.1f}"
        )

    def counts(self, title, counter):
        if counter:
            self.stdout.write(f"{title}:")
            for key, value in sorted(counter.items()):
                self.stdout.write(f"  {key}: {value}")

    def report_sync(self, result):
        write = self.stdout.write
        buckets = result["buckets"]
        write("APPLIED" if result["apply"] else "DRY RUN (rolled back, nothing saved)")
        write(f"file sha256: {result['sha256']}")
        write(
            f"file rows: {result['file_rows']} | exported at: {result['exported_at']}"
        )
        write(f"batch: {result['batch']}")
        write(f"rows_read: {buckets['rows_read']}")
        for name in BUCKETS:
            write(f"  {name}: {buckets[name]}")
        write(
            f"identity (rows_read = sum of buckets): {'OK' if result['identity_ok'] else 'FAILED'}"
        )
        self.counts("rejected and review reasons", result["reasons"])
        self.counts("row flags", result["flags"])
        write(f"accept-file codes not used: {result['accepted_codes_unused']}")
        write(
            f"codes whose rows carry different names: {result['codes_with_differing_names']}"
        )
        write(
            f"name hashes found under more than one code: {result['names_on_several_codes']}"
        )
        self.counts("stored data, as given (not corrected)", result["quality"])
        self.counts("reconciliation", result["reconciliation"])
        write(f"review file: {result['review_file']}")

    def report_joining(self, result):
        write = self.stdout.write
        write("JOINING REPORT (read-only; date_joining is never written)")
        write(f"file sha256: {result['sha256']} | file rows: {result['file_rows']}")
        write(f"matched employees with a start date: {result['matched_employees']}")
        for key in ("equal", "hrms_later", "hrms_earlier", "hrms_empty"):
            write(f"  {key}: {result['counts'][key]}")
        write(f"review file: {result['review_file']}")

    def report_undo(self, result):
        verb = "deleted" if result["apply"] else "would delete (dry run)"
        self.stdout.write(f"undo batch {result['batch']}: {verb} {result['deleted']}")
