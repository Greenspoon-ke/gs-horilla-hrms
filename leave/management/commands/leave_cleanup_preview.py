"""
Read-only preview of the whole 2026 leave clean-up (rules in leave/preview.py):
the Annual Leave type settings, allocation conversion, HR leave records, past
pending requests removed as not taken, and the rebuild, computed in memory
against the current database. Nothing is
saved; it refuses to run on a writable connection.

    PGOPTIONS='-c default_transaction_read_only=on' \\
    python manage.py leave_cleanup_preview --through 2026-09-30 \\
        --sheet ~/hrms-local/joiners_2026_sheet_dates.csv \\
        --opening-carry ~/hrms-local/opening-carry.csv \\
        --overrides ~/hrms-local/overrides.csv \\
        --exclude ~/hrms-local/exclusions.csv \\
        --decisions ~/hrms-local/allocation-decisions.csv \\
        --records ~/hrms-local/hr-leave-records.csv \\
        --since 2026-10-14T09:45:13+03:00 \\
        --out-dir ~/hrms-local/preview-<stamp>

The input files are the ones the production commands take. --since is when
the data the inputs were prepared from was taken (anything created later is
listed). Output (mode 600, outside the repository): a per-row CSV and a
summary with counts and employee codes.
"""

from datetime import date, datetime

from django.core.management.base import BaseCommand, CommandError

from leave.accrual import (
    NAIROBI,
    AccrualRefused,
    parse_carry,
    parse_reason,
    parse_sheet_date,
    parse_start,
    read_code_file,
)
from leave.conversion import ConversionRefused
from leave.preview import PreviewRefused, run_preview


def parse_since(value):
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=NAIROBI)


class Command(BaseCommand):
    help = "Read-only preview of the leave clean-up. Never saves anything."

    def add_arguments(self, parser):
        parser.add_argument("--through", type=date.fromisoformat, required=True)
        parser.add_argument("--sheet", required=True, help="employee_code,join_date")
        parser.add_argument("--opening-carry", help="code,opening_carry")
        parser.add_argument("--overrides", help="code,start_date")
        parser.add_argument("--exclude", help="code,reason")
        parser.add_argument("--decisions", help="Allocation decisions CSV")
        parser.add_argument("--records", help="HR leave records CSV")
        parser.add_argument(
            "--since",
            type=parse_since,
            required=True,
            help="When the input data was taken (ISO; Nairobi if no offset).",
        )
        parser.add_argument(
            "--negative", choices=["record", "zero"], default="record"
        )
        parser.add_argument("--out-dir", required=True)

    def handle(self, *args, **options):
        try:
            result = run_preview(
                options["through"],
                self.read(options["sheet"], "join_date", parse_sheet_date),
                options["out_dir"],
                options["since"],
                overrides=self.read(options["overrides"], "start_date", parse_start),
                opening_carry=self.read(
                    options["opening_carry"], "opening_carry", parse_carry
                ),
                excluded=self.read(options["exclude"], "reason", parse_reason),
                decisions_path=options["decisions"],
                records_path=options["records"],
                negative=options["negative"],
            )
        except (AccrualRefused, ConversionRefused, PreviewRefused) as error:
            raise CommandError(f"REFUSED: {error}") from None
        self.stdout.write("\n".join(result["lines"]))
        self.stdout.write(f"rows: {result['csv']}")
        self.stdout.write(f"summary: {result['summary']}")

    @staticmethod
    def read(path, column, parse):
        return read_code_file(path, column, parse) if path else {}
