"""
One-way copy of the contracts sheet export into ContractRecord.

The export file (written by the Apps Script) is UTF-8, comma-separated, with
exactly the columns in ``HEADER``. It carries no names, emails, salary,
manager or notes. ``NameTokens`` holds salted hashes of the employee's name
words so a wrong employee code can be caught without the file holding names:

    normalise to Unicode NFC, lower-case, replace every run of characters that are not letters
    or digits with one space, split on spaces, keep words of 2+ characters,
    and for each word take the first 12 hex characters of
    sha256(salt + word), joined with single spaces.

The salt comes from CONTRACT_NAME_SALT here and from Script Properties on the
sheet side; it is never in the repository or the file.

Every data row lands in exactly one bucket: created, updated, unchanged,
skipped_deleted_never_synced, rejected (file problem HR must fix) or review
(cannot be matched to an employee safely). Output is counts and employee
codes only.

Background-safe: nothing here reads a request, user, session or
``_thread_locals``. Employees are read with ``objects.entire()``, which skips
the request-based company filter.
"""

import csv
import hashlib
import os
import re
import unicodedata
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from django.apps import apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import connection, transaction
from django.db.models import Max

from contract_records.models import ContractRecord
from employee.models import Employee, EmployeeWorkInformation

HEADER = [
    "ContractID",
    "EmployeeCode",
    "NameTokens",
    "ContractType",
    "StartDate",
    "EndDate",
    "Status",
    "Department",
    "Role",
    "DocURL",
    "SignedURL",
    "SignedAt",
    "ProbationConfirmed",
    "GeneratedAt",
    "ExportedAt",
]
# A column outside HEADER whose name contains one of these is refused as
# personal data. NameTokens is allowed because it is in HEADER.
PII_WORDS = ("name", "mail", "salary", "wage", "pay", "note", "manager", "phone")
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
TOKEN = re.compile(r"[0-9a-f]{12}")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
SALT_ENV = "CONTRACT_NAME_SALT"
# Greenspoon employee codes are this prefix followed by digits.
CODE_PREFIX = "GSL"

TYPE_LABELS = {
    "permanent employment": "permanent",
    "permanent": "permanent",
    "fixed-term 1 year": "fixed_1y",
    "fixed-term employment": "fixed_1y",  # the old 1-year label
    "fixed-term 6 months": "fixed_6m",
    "6 month fixed-term": "fixed_6m",
    "fixed-term 3 months": "fixed_3m",
    "casual": "casual",
    "addendum": "addendum",
}
STATUS_LABELS = {
    "active": "active",
    "generated": "generated",
    "probation confirmed": "probation_confirmed",
    "probation extended": "probation_extended",
    "expired": "expired",
    "renewed": "renewed",
    "exited": "exited",
    "terminated": "terminated",
    "deleted": "deleted",
}
ACTIVE_STATUSES = frozenset({"active", "probation_confirmed", "probation_extended"})
TERM_MONTHS = {"fixed_1y": 12, "fixed_6m": 6, "fixed_3m": 3}
LINK_HOSTS = frozenset({"docs.google.com", "drive.google.com"})
REF_MAX = ContractRecord._meta.get_field("external_ref").max_length
CODE_MAX = ContractRecord._meta.get_field("employee_code").max_length
LABEL_MAX = ContractRecord._meta.get_field("status_raw").max_length
TEXT_MAX = ContractRecord._meta.get_field("department_text").max_length
URL_MAX = ContractRecord._meta.get_field("doc_url").max_length
EARLIEST_DATE = date(2000, 1, 1)
NAIROBI = ZoneInfo("Africa/Nairobi")
# Arbitrary constant, distinct from leave.accrual's 7_302_026.
ADVISORY_LOCK_KEY = 7_302_027

REVIEW_COLUMNS = ["line", "employee_code", "bucket", "reason"]
BUCKETS = (
    "created",
    "updated",
    "unchanged",
    "skipped_deleted_never_synced",
    "rejected",
    "review",
)


class SyncRefused(Exception):
    """The whole run is refused before (or without) writing anything."""


class RowRejected(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------- matching


def normalise_code(raw):
    """
    Trim, upper-case, drop spaces and the hyphen after the prefix, and strip
    leading zeros when the rest is all digits: "<prefix>-0NN", "<prefix> 0NN"
    and "<prefix>0NN" all become "<prefix>NN". Other shapes keep theirs.
    """
    code = "".join((raw or "").split()).upper()
    if code.startswith(CODE_PREFIX + "-"):
        code = CODE_PREFIX + code[len(CODE_PREFIX) + 1 :]
    digits = re.fullmatch(re.escape(CODE_PREFIX) + r"0*(\d+)", code)
    return f"{CODE_PREFIX}{digits.group(1)}" if digits else code


def name_tokens(name, salt):
    # NFC first, so "é" typed as e + accent hashes like the single character.
    text = unicodedata.normalize("NFC", name or "").lower()
    words = re.sub(r"[\W_]+", " ", text).split()
    return frozenset(
        hashlib.sha256((salt + word).encode("utf-8")).hexdigest()[:12]
        for word in words
        if len(word) >= 2
    )


def names_agree(first, second):
    # Two shared words, or one when either side has a one-word name.
    needed = min(2, len(first), len(second))
    return needed > 0 and len(first & second) >= needed


class EmployeeIndex:
    """Every employee, active and archived, loaded once (about 270 rows)."""

    def __init__(self, salt):
        self.exact = defaultdict(list)
        self.normal = defaultdict(list)
        self.tokens = {}
        employees = Employee.objects.entire().only(
            "id", "badge_id", "employee_first_name", "employee_last_name"
        )
        for employee in employees:
            badge = (employee.badge_id or "").strip()
            if not badge:
                continue
            self.exact[badge.upper()].append(employee)
            self.normal[normalise_code(badge)].append(employee)
            self.tokens[employee.pk] = name_tokens(
                f"{employee.employee_first_name} {employee.employee_last_name or ''}",
                salt,
            )

    def find(self, raw_code):
        """(employee, None) or (None, review reason). Exact code first."""
        if not (raw_code or "").strip():
            return None, "BLANK_CODE"
        for found in (
            self.exact.get(raw_code.strip().upper(), []),
            self.normal.get(normalise_code(raw_code), []),
        ):
            if len(found) == 1:
                return found[0], None
            if len(found) > 1:
                return None, "CODE_AMBIGUOUS"
        return None, "CODE_NOT_FOUND"


# ---------------------------------------------------------------- the file


def name_salt():
    salt = os.environ.get(SALT_ENV, "")
    if len(salt) < 16:
        raise SyncRefused(f"Set {SALT_ENV} (16+ characters) to check NameTokens.")
    return salt


def outside_repo(raw_path):
    path = Path(raw_path).expanduser().resolve()
    base = Path(settings.BASE_DIR).resolve()
    # The repository is public: no exports or review files inside it.
    if path == base or base in path.parents:
        raise SyncRefused(f"Refusing a path inside the repository: {raw_path}")
    return path


def existing_file(raw_path):
    path = outside_repo(raw_path)
    if not path.is_file():
        raise SyncRefused(f"File not found: {raw_path}")
    return path


def tidy(value):
    """Trim, collapse all whitespace (incl. non-breaking), undo Excel guard."""
    text = " ".join((value or "").split())
    # The export prefixes ' to cells starting = + - @ so Excel will not run
    # them as formulas; store the sheet's own text.
    if len(text) > 1 and text[0] == "'" and text[1] in "=+-@":
        text = text[1:]
    return text


def parse_timestamp(value):
    text = tidy(value)
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        raise RowRejected("BAD_TIMESTAMP") from None
    if "T" not in text or stamp.tzinfo is None:
        raise RowRejected("BAD_TIMESTAMP")
    return stamp


def _check_header(header):
    if len(header) == 1 and (";" in header[0] or "\t" in header[0]):
        raise SyncRefused(
            "The file is not comma-separated (re-saved by Excel?). "
            "Use the export as downloaded."
        )
    personal = [
        column
        for column in header
        if column not in HEADER and any(word in column.lower() for word in PII_WORDS)
    ]
    if personal:
        raise SyncRefused(
            f"Refusing a file with personal-data columns: {', '.join(personal)}"
        )
    if header != HEADER:
        extra = [column for column in header if column not in HEADER]
        missing = [column for column in HEADER if column not in header]
        raise SyncRefused(
            "Header must be exactly: "
            + ", ".join(HEADER)
            + (f". Unexpected: {', '.join(extra)}" if extra else "")
            + (f". Missing: {', '.join(missing)}" if missing else "")
        )


def _rows(path):
    """Yield (line, {column: cell}) one row at a time; never the whole file."""
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = [column.strip() for column in next(reader, None) or []]
        if not header:
            raise SyncRefused("The file is empty.")
        _check_header(header)
        for cells in reader:
            if not cells:
                continue
            if len(cells) != len(HEADER):
                raise SyncRefused(
                    f"Line {reader.line_num}: {len(cells)} cells, "
                    f"expected {len(HEADER)}."
                )
            yield reader.line_num, dict(zip(HEADER, cells))


def inspect_file(path):
    """
    Read the whole file once before anything is written, so a bad file is
    refused whole and never half-applied. Collects what the second pass and
    the guards need.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    rows = 0
    exported = set()
    tokens_by_code = defaultdict(set)
    codes_by_tokens = defaultdict(set)
    try:
        for line, row in _rows(path):
            if [cell.strip() for cell in row.values()] == HEADER:
                raise SyncRefused(f"Line {line}: the header row is repeated.")
            if any(EMAIL.search(cell) for cell in row.values()):
                raise SyncRefused(f"Line {line}: a cell looks like an email address.")
            words = row["NameTokens"].split()
            if not all(TOKEN.fullmatch(word) for word in words):
                # Not hashes: the export may have leaked real names.
                raise SyncRefused(
                    f"Line {line}: NameTokens must be 12-character hex hashes."
                )
            code = normalise_code(row["EmployeeCode"])
            if code and words:
                tokens_by_code[code].add(frozenset(words))
                codes_by_tokens[frozenset(words)].add(code)
            exported.add(row["ExportedAt"].strip())
            rows += 1
    except UnicodeDecodeError:
        raise SyncRefused("The file is not UTF-8.") from None
    except csv.Error as error:
        raise SyncRefused(f"The file is not valid CSV: {error}") from None
    exported_at = None
    if rows:
        if len(exported) != 1:
            raise SyncRefused("ExportedAt must be the same on every row.")
        try:
            exported_at = parse_timestamp(exported.pop())
        except RowRejected:
            exported_at = None
        if exported_at is None:
            raise SyncRefused("ExportedAt must be an ISO timestamp with a timezone.")
    conflicting = {
        code
        for code, sets in tokens_by_code.items()
        if any(not names_agree(a, b) for a in sets for b in sets)
    }
    return {
        "sha256": digest.hexdigest(),
        "rows": rows,
        "exported_at": exported_at,
        "codes_with_differing_names": conflicting,
        "names_on_several_codes": sum(
            1 for codes in codes_by_tokens.values() if len(codes) > 1
        ),
    }


def read_accept_file(raw_path):
    """HR-confirmed codes: a CSV with an employee_code column."""
    path = existing_file(raw_path)
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = [(name or "").strip().lower() for name in reader.fieldnames or []]
        if "employee_code" not in columns:
            raise SyncRefused(f"{path.name} needs an employee_code column.")
        reader.fieldnames = columns
        return {
            normalise_code(row["employee_code"])
            for row in reader
            if (row["employee_code"] or "").strip()
        }


# ---------------------------------------------------------------- one row


def _date(value):
    text = tidy(value)
    if not text:
        return None, None
    if ISO_DATE.fullmatch(text):
        try:
            return date.fromisoformat(text), None
        except ValueError:
            raise RowRejected("BAD_DATE") from None
    # Digits in a non-ISO date (dd/mm/yyyy, an Excel serial, "1st Jan 2026")
    # mean a real date in the wrong shape: refuse rather than guess.
    if any(char.isdigit() for char in text):
        raise RowRejected("BAD_DATE_FORMAT")
    # Words only ("Commence date not stated"): keep the row, no date.
    return None, "DATE_TEXT_STORED_NULL"


def _link(value):
    text = tidy(value)
    if not text:
        return "", None
    try:
        URLValidator(schemes=["https"])(text)
        host = urlsplit(text).hostname
    except (ValidationError, ValueError):
        return "", "LINK_REJECTED"
    if host not in LINK_HOSTS or len(text) > URL_MAX:
        return "", "LINK_REJECTED"
    return text, None


def _bool(value):
    text = tidy(value).lower()
    if text in ("true", "yes", "y", "1"):
        return True
    if text in ("false", "no", "n", "0", ""):
        return False
    raise RowRejected("BAD_BOOLEAN")


def parse_fields(row):
    """Stored fields (no employee yet) and parse-time flags, or RowRejected."""
    type_raw = tidy(row["ContractType"])
    status_raw = tidy(row["Status"])
    department = tidy(row["Department"])
    role = tidy(row["Role"])
    if len(type_raw) > LABEL_MAX or len(status_raw) > LABEL_MAX:
        raise RowRejected("TOO_LONG")
    # Refuse rather than silently truncate.
    if len(department) > TEXT_MAX or len(role) > TEXT_MAX:
        raise RowRejected("TOO_LONG")
    start, flag_start = _date(row["StartDate"])
    end, flag_end = _date(row["EndDate"])
    doc_url, flag_doc = _link(row["DocURL"])
    signed_url, flag_signed = _link(row["SignedURL"])
    flags = [f for f in (flag_start, flag_end, flag_doc, flag_signed) if f]
    fields = {
        "contract_type": TYPE_LABELS.get(type_raw.lower(), "other"),
        "contract_type_raw": type_raw,
        "status": STATUS_LABELS.get(status_raw.lower(), "other"),
        "status_raw": status_raw,
        "start_date": start,
        "end_date": end,
        "department_text": department,
        "role_text": role,
        "doc_url": doc_url,
        "signed_url": signed_url,
        "signed_at": parse_timestamp(row["SignedAt"]),
        "generated_at": parse_timestamp(row["GeneratedAt"]),
        "probation_confirmed": _bool(row["ProbationConfirmed"]),
    }
    return fields, flags


def _save(existing, ref, fields, batch, exported_at):
    """Create, update or leave one record, in its own transaction."""
    with transaction.atomic():
        if existing is None:
            ContractRecord.objects.create(
                external_ref=ref,
                import_batch=batch,
                last_batch=batch,
                last_export_at=exported_at,
                **fields,
            )
            return "created"
        changed = [
            name for name, value in fields.items() if getattr(existing, name) != value
        ]
        if not changed:
            return "unchanged"
        for name in changed:
            setattr(existing, name, fields[name])
        existing.last_batch = batch
        existing.last_export_at = exported_at
        existing.save(
            update_fields=[*changed, "last_batch", "last_export_at", "updated_at"]
        )
        return "updated"


# ---------------------------------------------------------------- the run


def nairobi_today():
    return datetime.now(NAIROBI).date()


def _stamp():
    return datetime.now(NAIROBI).strftime("%Y%m%d-%H%M%S-%f")


@contextmanager
def advisory_lock():
    """One run at a time, across processes sharing the database."""
    if connection.vendor != "postgresql":
        raise SyncRefused("Requires PostgreSQL (advisory lock).")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [ADVISORY_LOCK_KEY])
        acquired = cursor.fetchone()[0]
    if not acquired:
        raise SyncRefused("Another contract-records run holds the lock.")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [ADVISORY_LOCK_KEY])


def review_target(raw_path, prefix):
    if raw_path:
        path = outside_repo(raw_path)
    else:
        path = outside_repo(
            Path("~/hrms-local").expanduser() / f"{prefix}-{_stamp()}.csv"
        )
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        raise SyncRefused(f"Review file already exists: {path}")
    return path


def _safe_cell(value):
    # Codes come from the sheet; HR opens this file in Excel, so a code
    # starting = + - @ must not run as a formula.
    text = str(value)
    return f"'{text}" if text[:1] in ("=", "+", "-", "@") else text


def write_csv(path, header, rows):
    # O_EXCL: never overwrite an earlier file. 0o600: owner only.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows([_safe_cell(cell) for cell in row] for row in rows)
    return path


def _guards(info, min_ratio, force):
    """Stale file and partial export. --force skips both, nothing else."""
    if force:
        return
    newest = ContractRecord.objects.aggregate(newest=Max("last_export_at"))["newest"]
    if newest and info["exported_at"] and info["exported_at"] < newest:
        raise SyncRefused(
            "Stale file: exported before the newest export already stored. "
            "Use --force only to restore an older export on purpose."
        )
    stored = ContractRecord.objects.count()
    if stored and info["rows"] < min_ratio * stored:
        raise SyncRefused(
            f"Partial export: {info['rows']} rows for {stored} stored records "
            f"(below {min_ratio:.0%}). Use --force if this is intended."
        )


def run_sync(
    csv_path,
    apply=False,
    review_out=None,
    accept_path=None,
    min_ratio=0.8,
    force=False,
    today=None,
):
    """Dry run unless ``apply``: a dry run does the same work and rolls back."""
    if not 0 < min_ratio <= 1:
        raise SyncRefused("--min-ratio must be above 0 and at most 1.")
    path = existing_file(csv_path)
    salt = name_salt()
    info = inspect_file(path)
    accepted = read_accept_file(accept_path) if accept_path else set()
    review_path = review_target(review_out, "contract-review")
    batch = f"{datetime.now(NAIROBI):%Y%m%d-%H%M%S}-{info['sha256'][:8]}"
    today = today or nairobi_today()
    with advisory_lock():
        _guards(info, min_ratio, force)
        if apply:
            result = _process(path, info, salt, accepted, batch, today)
        else:
            with transaction.atomic():
                result = _process(path, info, salt, accepted, batch, today)
                transaction.set_rollback(True)
    result.update(
        apply=apply,
        batch=batch,
        sha256=info["sha256"],
        file_rows=info["rows"],
        exported_at=info["exported_at"],
        review_file=write_csv(review_path, REVIEW_COLUMNS, result.pop("review_rows")),
    )
    return result


def _process(path, info, salt, accepted, batch, today):
    index = EmployeeIndex(salt)
    buckets = Counter({name: 0 for name in BUCKETS})
    reasons = Counter()
    flags = Counter()
    review_rows = []
    seen = set()
    accepted_used = set()
    for line, row in _rows(path):
        buckets["rows_read"] += 1
        ref = tidy(row["ContractID"]).upper()
        code = normalise_code(row["EmployeeCode"])
        try:
            if not ref:
                # The sheet's batch patch appends rows with no ContractID.
                # Never invent one: HR fixes the sheet.
                raise RowRejected("BLANK_CONTRACT_ID")
            if ref in seen:
                raise RowRejected("DUPLICATE_CONTRACT_ID")
            seen.add(ref)
            if len(ref) > REF_MAX or len(code) > CODE_MAX:
                raise RowRejected("TOO_LONG")
            status_raw = tidy(row["Status"])
            # A Deleted row is handled on its status alone, so a bad date or
            # link elsewhere in it cannot block the delete.
            deleted = status_raw.lower() == "deleted"
            if not deleted:
                fields, row_flags = parse_fields(row)
        except RowRejected as rejected:
            buckets["rejected"] += 1
            reasons[f"rejected:{rejected.reason}"] += 1
            review_rows.append([line, code, "rejected", rejected.reason])
            continue

        existing = ContractRecord.objects.filter(external_ref=ref).first()
        if deleted:
            if existing is None:
                buckets["skipped_deleted_never_synced"] += 1
                continue
            # Never hard-delete: mark it, whatever else the row says.
            outcome = _save(
                existing,
                ref,
                {"status": "deleted", "status_raw": status_raw},
                batch,
                info["exported_at"],
            )
            buckets[outcome] += 1
            continue

        employee, reason = index.find(row["EmployeeCode"])
        if employee is not None:
            file_tokens = frozenset(row["NameTokens"].split())
            if not names_agree(file_tokens, index.tokens[employee.pk]):
                # One code with rows for different people cannot be accepted:
                # that would attach someone else's contract. HR fixes the sheet.
                differing = code in info["codes_with_differing_names"]
                if code in accepted and not differing:
                    flags["ACCEPTED_BY_HR"] += 1
                    accepted_used.add(code)
                elif not file_tokens:
                    reason = "NO_NAME_TOKENS"
                else:
                    reason = "CODE_NAMES_DIFFER" if differing else "NAME_MISMATCH"
        if reason:
            buckets["review"] += 1
            reasons[f"review:{reason}"] += 1
            review_rows.append([line, code, "review", reason])
            continue

        if existing and existing.employee_id and existing.employee_id != employee.pk:
            flags["EMPLOYEE_CHANGED"] += 1
        fields.update(employee_id=employee.pk, employee_code=code)
        outcome = _save(existing, ref, fields, batch, info["exported_at"])
        buckets[outcome] += 1
        for flag in row_flags:
            flags[flag] += 1
            if flag == "DATE_TEXT_STORED_NULL":
                review_rows.append([line, code, outcome, flag])

    total = sum(buckets[name] for name in BUCKETS)
    return {
        "buckets": buckets,
        "identity_ok": total == buckets["rows_read"],
        "reasons": reasons,
        "flags": flags,
        "accepted_codes_unused": len(accepted - accepted_used),
        "codes_with_differing_names": len(info["codes_with_differing_names"]),
        "names_on_several_codes": info["names_on_several_codes"],
        "quality": data_quality(today),
        "reconciliation": reconcile(seen),
        "review_rows": review_rows,
    }


# ---------------------------------------------------------------- reports


def _live_records():
    return list(
        ContractRecord.objects.exclude(status="deleted").values(
            "employee_id",
            "employee_code",
            "contract_type",
            "status",
            "start_date",
            "end_date",
            "signed_url",
            "signed_at",
            "external_ref",
        )
    )


def data_quality(today):
    """Counts of sheet data stored as given but worth HR's attention."""
    counts = Counter()
    groups = Counter()
    far_future = today + timedelta(days=3650)
    for rec in _live_records():
        start, end, kind = rec["start_date"], rec["end_date"], rec["contract_type"]
        groups[(rec["employee_code"], kind, start, end)] += 1
        if kind == "permanent" and end:
            counts["permanent_with_end_date"] += 1
        if kind in TERM_MONTHS and not end:
            counts["fixed_term_without_end_date"] += 1
        if kind in TERM_MONTHS and start and end and end > start:
            # Compared with start + term - 1 day. Never recomputed or stored.
            expected = (
                start + relativedelta(months=TERM_MONTHS[kind]) - timedelta(days=1)
            )
            off_by = abs((end - expected).days)
            if off_by > 31:
                # e.g. a "6 month" label over 12 months.
                counts["term_length_not_matching_label"] += 1
            elif off_by:
                # The generator's setMonth month-end overflow and its
                # "+1 year" without the import script's "- 1 day".
                counts["term_end_off_by_1_to_31_days"] += 1
        if start and end and end < start:
            counts["end_before_start"] += 1
        if start and end and end == start:
            counts["end_equals_start"] += 1
        if any(d and not EARLIEST_DATE <= d <= far_future for d in (start, end)):
            counts["implausible_date"] += 1
        if rec["status"] in ACTIVE_STATUSES and end and end < today:
            counts["active_but_end_date_past"] += 1
        if bool(rec["signed_url"]) != bool(rec["signed_at"]):
            counts["signed_link_and_date_mismatch"] += 1
        if kind == "other":
            counts["type_other"] += 1
        if rec["status"] == "other":
            counts["status_other"] += 1
    counts["duplicate_looking_groups"] = sum(1 for n in groups.values() if n > 1)
    return counts


def reconcile(file_refs):
    """The "did it work" numbers. Counts only; nothing is changed."""
    records = [r for r in _live_records() if r["employee_id"]]
    by_employee = defaultdict(list)
    for rec in records:
        by_employee[rec["employee_id"]].append(rec)
    employees = dict(Employee.objects.entire().values_list("id", "is_active"))
    badges = dict(Employee.objects.entire().values_list("id", "badge_id"))
    counts = Counter()
    counts["active_employees_without_record"] = sum(
        1 for pk, active in employees.items() if active and pk not in by_employee
    )
    for pk, recs in by_employee.items():
        contracts = [r for r in recs if r["contract_type"] != "addendum"]
        if contracts:
            latest = max(
                contracts,
                key=lambda r: (r["start_date"] or date.min, r["external_ref"]),
            )
            if employees.get(pk) and latest["status"] in ("exited", "terminated"):
                counts["active_employees_latest_exited_or_terminated"] += 1
            if employees.get(pk) is False and latest["status"] in ACTIVE_STATUSES:
                counts["archived_employees_latest_active"] += 1
        spans = sorted(
            (r["start_date"], r["end_date"] or date.max)
            for r in contracts
            if r["status"] in ACTIVE_STATUSES and r["start_date"]
        )
        if any(later[0] <= earlier[1] for earlier, later in zip(spans, spans[1:])):
            counts["employees_with_overlapping_active_contracts"] += 1
        if any(normalise_code(badges.get(pk)) != r["employee_code"] for r in recs):
            counts["employees_whose_code_changed_since_sync"] += 1
    counts["records_missing_from_file"] = ContractRecord.objects.exclude(
        external_ref__in=file_refs
    ).count()
    counts["records_without_employee"] = ContractRecord.objects.filter(
        employee__isnull=True
    ).count()
    if apps.is_installed("payroll"):
        from payroll.models.models import Contract

        # entire(): the default manager would read the request's company.
        with_payroll = set(
            Contract.objects.entire().values_list("employee_id_id", flat=True)
        )
        counts["employees_with_payroll_contract_but_no_record"] = len(
            with_payroll - set(by_employee)
        )
        counts["employees_with_record_but_no_payroll_contract"] = len(
            set(by_employee) - with_payroll
        )
    return counts


def run_joining_report(csv_path, review_out=None, accept_path=None):
    """
    Read-only: earliest contract start per matched employee against
    date_joining. Never writes date_joining or anything else.
    """
    path = existing_file(csv_path)
    salt = name_salt()
    info = inspect_file(path)
    accepted = read_accept_file(accept_path) if accept_path else set()
    review_path = review_target(review_out, "contract-joining")
    index = EmployeeIndex(salt)
    earliest = {}
    codes = {}
    with transaction.atomic():
        for _line, row in _rows(path):
            try:
                fields, _flags = parse_fields(row)
            except RowRejected:
                continue
            # Addenda change a contract; deleted rows never happened.
            if fields["contract_type"] == "addendum" or fields["status"] == "deleted":
                continue
            if not fields["start_date"]:
                continue
            employee, reason = index.find(row["EmployeeCode"])
            if reason:
                continue
            code = normalise_code(row["EmployeeCode"])
            tokens = frozenset(row["NameTokens"].split())
            if not names_agree(tokens, index.tokens[employee.pk]) and (
                code not in accepted or code in info["codes_with_differing_names"]
            ):
                continue
            current = earliest.get(employee.pk)
            if current is None or fields["start_date"] < current:
                earliest[employee.pk] = fields["start_date"]
            codes[employee.pk] = code
        joined = dict(
            EmployeeWorkInformation.objects.entire()
            .filter(employee_id__in=list(earliest))
            .values_list("employee_id", "date_joining")
        )
        transaction.set_rollback(True)
    counts = Counter()
    rows = []
    for pk, start in sorted(earliest.items(), key=lambda item: codes[item[0]]):
        joining = joined.get(pk)
        if joining is None:
            result = "hrms_empty"
        elif joining == start:
            result = "equal"
        elif joining > start:
            result = "hrms_later"
        else:
            result = "hrms_earlier"
        counts[result] += 1
        if result != "equal":
            rows.append([codes[pk], result])
    return {
        "matched_employees": len(earliest),
        "counts": counts,
        "sha256": info["sha256"],
        "file_rows": info["rows"],
        "review_file": write_csv(review_path, ["employee_code", "result"], rows),
    }


def run_undo(batch, apply=False):
    """Delete only rows CREATED by ``batch``. Dry run unless ``apply``."""
    if not (batch or "").strip():
        raise SyncRefused("--undo-batch needs a batch id.")
    with advisory_lock():
        records = ContractRecord.objects.filter(import_batch=batch.strip())
        count = records.count()
        if apply and count:
            with transaction.atomic():
                records.delete()
    return {"batch": batch.strip(), "apply": apply, "deleted": count}
