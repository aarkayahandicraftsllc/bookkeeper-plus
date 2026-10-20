"""Storage for a Tally-style set of books: groups, ledgers and vouchers.

The structure follows Tally's, because the reports fall out of it:

    Group  ->  Ledger  ->  Voucher line

* **Groups** form a tree with 28 reserved groups at the top, mirroring Tally's.
  A group's ``nature`` (asset / liability / income / expense) decides which
  statement a balance lands on, and ``affects_gross`` separates the trading
  accounts (which produce gross profit) from the indirect ones.
* **Ledgers** are the accounts you actually post to. Each sits under exactly
  one group and may carry an opening balance.
* **Vouchers** are the transactions. Every voucher holds two or more lines
  whose debits and credits must agree exactly, which is enforced here rather
  than only in the form.

Everything is stored debit-positive internally: a signed balance of +500 means
500 Dr, -500 means 500 Cr. Presentation flips it back per convention.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

def _app_dir() -> Path:
    """The app's own folder — the anchor for the data folder.

    **Not** ``__file__`` when packaged. A PyInstaller onefile build unpacks
    itself into a TEMPORARY directory and deletes that directory on exit, so
    ``__file__`` points somewhere that will not exist next launch: books
    written beside it are lost the moment the app closes, silently, with the
    app having looked like it saved them. That is exactly what happened on
    2026-10-16 — a company created in the packaged app, journals entered, and
    both gone on the next launch.

    The executable's own location is the stable anchor, which also keeps the
    promise the packaging script makes: the books sit in ``data`` next to the
    exe, so the two travel together.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = _app_dir()
DATA_DIR = Path(os.environ.get("BOOKS_DIR", APP_DIR / "data"))
DB_PATH = Path(os.environ.get("BOOKS_DB", DATA_DIR / "books.db"))

# True when BOOKS_DB names the file explicitly. The company picker is then
# skipped: a caller that pinned the file - the test suites, the Streamlit app,
# anyone pointing at a shared folder - has already chosen, and asking again
# would override them.
DB_PINNED = "BOOKS_DB" in os.environ

# Each company is its OWN database file, the way Tally keeps a company per data
# folder. The alternative - one file with a company_id on every table - would
# have put a scoping clause in every query in this module and in accounting.py,
# for a feature that never needs to report across companies. Keeping them
# separate also means a company is backed up, copied or handed over by moving
# one file.
LAST_COMPANY_FILE = DATA_DIR / ".last_company"

# Filenames that live in the data folder but are NOT companies. The backup that
# save-before-overwrite leaves behind is the one that matters: without this it
# would appear in the picker as a second company with the same name.
_NOT_COMPANIES = (".backup", ".tmp", ".old")

NATURES = ("asset", "liability", "income", "expense")
SIDES = ("Dr", "Cr")

# Tally's voucher types, with the rule each one exists to express.
VOUCHER_TYPES = {
    "Payment": "Money going out of cash or bank.",
    "Receipt": "Money coming into cash or bank.",
    "Contra": "Money moving between your own cash and bank accounts.",
    "Journal": "Adjustments that touch neither cash nor bank.",
    "Sales": "An invoice raised on a customer.",
    "Purchase": "A bill received from a supplier.",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL UNIQUE,
    parent_id     INTEGER REFERENCES groups(id),
    nature        TEXT    NOT NULL CHECK (nature IN ('asset', 'liability',
                                                     'income', 'expense')),
    affects_gross INTEGER NOT NULL DEFAULT 0,
    is_reserved   INTEGER NOT NULL DEFAULT 0,
    sort_order    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ledgers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL UNIQUE,
    group_id     INTEGER NOT NULL REFERENCES groups(id),
    opening      REAL    NOT NULL DEFAULT 0 CHECK (opening >= 0),
    opening_side TEXT    NOT NULL DEFAULT 'Dr' CHECK (opening_side IN ('Dr', 'Cr')),
    notes        TEXT    NOT NULL DEFAULT '',
    is_reserved  INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1,
    bill_wise    INTEGER NOT NULL DEFAULT 0,
    -- GST. `gst_rate` is NULL for a ledger that is not taxable at all, which
    -- is NOT the same as 0: nil-rated and exempt supplies are taxable at
    -- zero per cent and have to be reported as such.
    gst_rate     REAL,
    -- Which tax ledger this is, if it is one. Named explicitly rather than
    -- matched on the ledger's title, so renaming "Output CGST" cannot quietly
    -- detach it from the arithmetic.
    gst_role     TEXT    NOT NULL DEFAULT '',
    gstin        TEXT    NOT NULL DEFAULT '',
    state_code   TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS vouchers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_type TEXT    NOT NULL,
    number       INTEGER NOT NULL,
    voucher_date TEXT    NOT NULL,
    narration    TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL,
    voided       INTEGER NOT NULL DEFAULT 0,
    voided_at    TEXT    NOT NULL DEFAULT '',
    UNIQUE (voucher_type, number)
);

CREATE TABLE IF NOT EXISTS voucher_lines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_id INTEGER NOT NULL REFERENCES vouchers(id) ON DELETE CASCADE,
    ledger_id  INTEGER NOT NULL REFERENCES ledgers(id),
    debit      REAL    NOT NULL DEFAULT 0 CHECK (debit >= 0),
    credit     REAL    NOT NULL DEFAULT 0 CHECK (credit >= 0),
    line_order INTEGER NOT NULL DEFAULT 0,
    CHECK ((debit = 0 OR credit = 0) AND debit + credit > 0)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Bill-wise receivables and payables.
--
-- A bill is NOT a row of its own: it is the pair (ledger_id, bill_ref), and
-- what is outstanding on it is DERIVED by summing these allocations, exactly
-- as a ledger balance is derived from voucher lines. There is therefore no
-- stored bill amount that can drift out of step with the vouchers.
--
-- `signed` follows the project's debit-positive convention, so the sum over a
-- reference IS its outstanding: a sales invoice debits the customer (+), a
-- receipt credits them (-), and a settled bill sums to zero.
--
-- ledger_id is carried here as well as on the line. It cannot drift, because
-- editing a voucher deletes and re-inserts its lines and these cascade with
-- them - and it keeps every outstanding query off a join.
CREATE TABLE IF NOT EXISTS bill_allocations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id     INTEGER NOT NULL REFERENCES voucher_lines(id) ON DELETE CASCADE,
    ledger_id   INTEGER NOT NULL REFERENCES ledgers(id),
    bill_ref    TEXT    NOT NULL DEFAULT '',
    kind        TEXT    NOT NULL
                CHECK (kind IN ('new', 'against', 'advance', 'on_account')),
    signed      REAL    NOT NULL,
    due_date    TEXT    NOT NULL DEFAULT '',
    alloc_order INTEGER NOT NULL DEFAULT 0,
    -- Only an on-account allocation may be nameless; everything else has to
    -- be findable by reference or it can never be settled.
    CHECK (kind = 'on_account' OR bill_ref <> '')
);

-- The GST facts of a voucher that CANNOT be derived from its lines: the rate
-- charged, who the counterparty was for tax purposes, and where the supply
-- was made. The amounts are deliberately NOT stored - taxable value and tax
-- are summed from the voucher's own lines, so there is one source of truth
-- for money, the same decision as for bills.
--
-- Place of supply and the party's GSTIN ARE stored, because they are a
-- snapshot: a return filed for August must keep saying what it said then,
-- even if the customer later moves state or registers.
CREATE TABLE IF NOT EXISTS gst_details (
    voucher_id      INTEGER PRIMARY KEY
                    REFERENCES vouchers(id) ON DELETE CASCADE,
    party_ledger_id INTEGER REFERENCES ledgers(id),
    party_gstin     TEXT    NOT NULL DEFAULT '',
    place_of_supply TEXT    NOT NULL DEFAULT '',
    supply_type     TEXT    NOT NULL DEFAULT 'intra'
                    CHECK (supply_type IN ('intra', 'inter')),
    rate            REAL    NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_alloc_line ON bill_allocations(line_id);
CREATE INDEX IF NOT EXISTS idx_alloc_bill ON bill_allocations(ledger_id, bill_ref);

CREATE INDEX IF NOT EXISTS idx_lines_voucher ON voucher_lines(voucher_id);
CREATE INDEX IF NOT EXISTS idx_lines_ledger ON voucher_lines(ledger_id);
CREATE INDEX IF NOT EXISTS idx_vouchers_date ON vouchers(voucher_date);
"""

# Tally's reserved groups: (name, parent, nature, affects_gross)
# `affects_gross` marks the trading accounts that produce gross profit.
RESERVED_GROUPS = [
    ("Capital Account", None, "liability", 0),
    ("Reserves & Surplus", "Capital Account", "liability", 0),
    ("Loans (Liability)", None, "liability", 0),
    ("Bank OD A/c", "Loans (Liability)", "liability", 0),
    ("Secured Loans", "Loans (Liability)", "liability", 0),
    ("Unsecured Loans", "Loans (Liability)", "liability", 0),
    ("Current Liabilities", None, "liability", 0),
    ("Duties & Taxes", "Current Liabilities", "liability", 0),
    ("Provisions", "Current Liabilities", "liability", 0),
    ("Sundry Creditors", "Current Liabilities", "liability", 0),
    ("Fixed Assets", None, "asset", 0),
    ("Investments", None, "asset", 0),
    ("Current Assets", None, "asset", 0),
    ("Bank Accounts", "Current Assets", "asset", 0),
    ("Cash-in-hand", "Current Assets", "asset", 0),
    ("Deposits (Asset)", "Current Assets", "asset", 0),
    ("Loans & Advances (Asset)", "Current Assets", "asset", 0),
    ("Stock-in-hand", "Current Assets", "asset", 0),
    ("Sundry Debtors", "Current Assets", "asset", 0),
    ("Misc. Expenses (Asset)", None, "asset", 0),
    ("Suspense A/c", None, "liability", 0),
    ("Sales Accounts", None, "income", 1),
    ("Purchase Accounts", None, "expense", 1),
    ("Direct Expenses", None, "expense", 1),
    ("Direct Incomes", None, "income", 1),
    ("Indirect Expenses", None, "expense", 0),
    ("Indirect Incomes", None, "income", 0),
]

# the groups whose balances belong to cash or bank, used by the voucher rules
CASH_GROUPS = ("Cash-in-hand", "Bank Accounts", "Bank OD A/c")

# Tally creates these two ledgers with every new company
RESERVED_LEDGERS = [("Cash", "Cash-in-hand"), ("Profit & Loss A/c", "Primary")]

DEFAULT_SETTINGS = {
    "company_name": "My Company",
    "fy_start_month": "4",          # April, the Indian financial year
    "books_begin": "",              # filled in on first run
    "currency": "₹",
    "country": "India",
}


# --------------------------------------------------------------------------- #
# company master
# --------------------------------------------------------------------------- #
# Tally's Company Creation screen, trimmed to the fields that earn their place
# in a book-keeping-only build: who the company is, where it is, what it is
# registered as, and which year the books cover. Declared once, here in the data
# layer, because BOTH front ends render and save it - a field added to this
# tuple appears in the Qt screen and the Streamlit form with no further work,
# and both get the same validation.
@dataclass(frozen=True)
class CompanyField:
    key: str
    label: str
    # choice offers values but takes an unlisted one (a currency symbol this
    # build has never heard of); option is closed, because a date format the
    # code does not know how to render is not a value at all.
    kind: str = "text"          # text | multiline | month | date | choice |
                                # option
    hint: str = ""
    choices: tuple[str, ...] = ()
    required: bool = False
    # Streamlit lays the short fields out three to a row, which reads wrongly
    # for the address lines: stacked they are an address, side by side they are
    # three unrelated boxes. The Qt form gives every field its own row anyway,
    # so this changes nothing there.
    wide: bool = False


CURRENCY_SYMBOLS = ("₹", "$", "€", "£", "¥")


# Dates are STORED as ISO (YYYY-MM-DD) in every table and every setting,
# always. This is only how they are SHOWN on screen and typed into an editor.
# Storing a localised date would make 03/04 ambiguous in the file itself and
# would sort wrongly, which is also why the report frames keep ISO and only
# the display boundary formats them.
@dataclass(frozen=True)
class DateFormat:
    key: str            # what is stored, and what the dropdown offers
    python: str         # strftime, for captions and table cells
    qt: str             # QDateEdit.setDisplayFormat
    streamlit: str      # st.date_input and DateColumn


DATE_FORMATS: tuple[DateFormat, ...] = (
    DateFormat("DD/MM/YYYY", "%d/%m/%Y", "dd/MM/yyyy", "DD/MM/YYYY"),
    DateFormat("MM/DD/YYYY", "%m/%d/%Y", "MM/dd/yyyy", "MM/DD/YYYY"),
)
DEFAULT_DATE_FORMAT = "DD/MM/YYYY"

# Which format a set of books STARTS on when nobody has chosen one. The
# currency is the only clue the books carry about where they are kept, and it
# is the clue the user pointed at - "if the currency sign is $ the date format
# should be MM/DD/YYYY". It is a DEFAULT, not a rule: once the format has been
# chosen the two settings are independent, so changing the currency later
# cannot silently reorder every date on screen.
DATE_FORMAT_BY_CURRENCY = {"$": "MM/DD/YYYY"}


def default_date_format(currency: str) -> str:
    return DATE_FORMAT_BY_CURRENCY.get((currency or "").strip(),
                                       DEFAULT_DATE_FORMAT)

COMPANY_FIELDS: tuple[CompanyField, ...] = (
    CompanyField("company_name", "Company name", required=True),
    CompanyField("mailing_name", "Mailing name",
                 hint="How the name should print on statements. "
                      "Blank uses the company name."),
    CompanyField("address1", "Address line 1", wide=True),
    CompanyField("address2", "Address line 2", wide=True),
    CompanyField("address3", "Address line 3", wide=True),
    CompanyField("city", "City"),
    CompanyField("state", "State"),
    CompanyField("country", "Country"),
    CompanyField("pincode", "ZIP / PIN code",
                 hint="Six digits for an Indian PIN code, "
                      "or a five-digit ZIP."),
    CompanyField("phone", "Phone"),
    CompanyField("email", "E-mail"),
    CompanyField("pan", "PAN", hint="Ten characters, e.g. AAAPZ1234C."),
    CompanyField("gstin", "GSTIN",
                 hint="Fifteen characters. Must carry the same PAN."),
    CompanyField("tan", "TAN", hint="Ten characters, e.g. DELA12345B."),
    CompanyField("cin", "CIN", hint="Twenty-one characters."),
    CompanyField("fy_start_month", "Financial year begins", kind="month"),
    CompanyField("books_begin", "Books begin on", kind="date"),
    CompanyField("currency", "Currency symbol", kind="choice",
                 choices=CURRENCY_SYMBOLS),
    CompanyField("date_format", "Date format", kind="option",
                 choices=tuple(fmt.key for fmt in DATE_FORMATS),
                 hint="How dates are shown and typed. A new set of books "
                      "takes its first format from the currency - $ starts "
                      "on MM/DD/YYYY - and is its own setting after that."),
)

# Identifiers are stored upper case, because that is how they are issued and
# how they must be quoted - and because a lower-case PAN would otherwise fail
# the GSTIN cross-check for no reason the user can see.
UPPER_FIELDS = ("pan", "gstin", "tan", "cin")

# The street part of the address, in printing order. It was one multi-line box
# until 2026-10-20; _migrate() moves an old one across.
ADDRESS_LINES = ("address1", "address2", "address3")

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
# 2-digit state code, the holder's PAN, entity number, 'Z', then a check digit.
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")
TAN_RE = re.compile(r"^[A-Z]{4}[0-9]{5}[A-Z]$")
CIN_RE = re.compile(r"^[LUu][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$")
# An Indian PIN code is six digits; a US ZIP is five, or five plus four. The
# field is labelled for both, so it has to accept both - a book kept in one
# country should not reject the other country's postcode as malformed.
PINCODE_RE = re.compile(r"^(?:[0-9]{6}|[0-9]{5}(?:-[0-9]{4})?)$")


def get_company() -> dict[str, str]:
    """Every company field, with the defaults filled in."""
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    stored = {row["key"]: row["value"] for row in rows}
    values = {field.key: str(stored.get(field.key,
                                        DEFAULT_SETTINGS.get(field.key, "")) or "")
              for field in COMPANY_FIELDS}
    # Not in DEFAULT_SETTINGS because the default depends on another field.
    # Books that predate the setting - and every book does - come out on the
    # format their currency implies, which is the whole point of deriving it.
    if values["date_format"] not in {fmt.key for fmt in DATE_FORMATS}:
        values["date_format"] = default_date_format(values["currency"])
    return values


def date_format() -> DateFormat:
    """How these books show and take dates. Read at every display boundary."""
    by_key = {fmt.key: fmt for fmt in DATE_FORMATS}
    return by_key.get(get_company()["date_format"],
                      by_key[DEFAULT_DATE_FORMAT])


def show_date(value, fmt: DateFormat | None = None) -> str:
    """An ISO date - string or ``date`` - written the way these books show it.

    Anything that is not a date is passed through exactly as written, the same
    way the amount formatter passes presentation text through: a report column
    can carry a blank or a label, and a formatter that raised on those would
    take the whole report down with it.

    ``fmt`` is passed in by callers formatting a whole column, so a table of
    200 rows asks the database once rather than 200 times.
    """
    if value is None or value == "":
        return ""
    if value != value:          # NaN and pandas' NaT, without importing pandas
        return ""
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value[:10])
        except ValueError:
            return value
    if not hasattr(value, "strftime"):
        return str(value)
    return value.strftime((fmt or date_format()).python)


def validate_company(values: dict[str, str]) -> dict[str, str]:
    """Clean and check the company details. Returns what should be stored.

    Blank is always acceptable - these are details you fill in as you learn
    them, and refusing to save a half-known address would be worse than useless.
    A value that IS given has to be well formed, because a malformed GSTIN is
    worse than an absent one: it reaches a filing looking legitimate.
    """
    cleaned: dict[str, str] = {}
    for field in COMPANY_FIELDS:
        raw = values.get(field.key, "")
        text = str("" if raw is None else raw).strip()
        if field.key in UPPER_FIELDS:
            text = text.upper()
        cleaned[field.key] = text

    if not cleaned["company_name"]:
        raise ValueError("The company needs a name.")

    for key, pattern, label, shape in (
        ("pan", PAN_RE, "PAN", "five letters, four digits, one letter"),
        ("gstin", GSTIN_RE, "GSTIN",
         "two-digit state code, a PAN, entity number, Z, check digit"),
        ("tan", TAN_RE, "TAN", "four letters, five digits, one letter"),
        ("cin", CIN_RE, "CIN", "21 characters, starting L or U"),
        ("pincode", PINCODE_RE, "ZIP / PIN code",
         "six digits for a PIN code, five for a ZIP"),
    ):
        if cleaned[key] and not pattern.match(cleaned[key]):
            raise ValueError(
                f"{label} {cleaned[key]!r} is not the right shape "
                f"({shape}). Leave it blank if you do not have it yet.")

    # A GSTIN contains its holder's PAN at characters 3 to 12. If both are given
    # and they disagree, one of the two is wrong, and this is the only place it
    # will ever be caught.
    if cleaned["pan"] and cleaned["gstin"]:
        inside = cleaned["gstin"][2:12]
        if inside != cleaned["pan"]:
            raise ValueError(
                f"The GSTIN carries PAN {inside}, but the PAN field says "
                f"{cleaned['pan']}. A GSTIN always contains the holder's PAN, "
                f"so one of the two is wrong.")

    if cleaned["email"] and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
                                         cleaned["email"]):
        raise ValueError(f"{cleaned['email']!r} does not look like an e-mail "
                         f"address.")

    try:
        month = int(cleaned["fy_start_month"] or 4)
    except ValueError:
        raise ValueError("The financial year must begin in a month.") from None
    if not 1 <= month <= 12:
        raise ValueError("The financial year must begin in a month, 1 to 12.")
    cleaned["fy_start_month"] = str(month)

    if cleaned["books_begin"]:
        try:
            date.fromisoformat(cleaned["books_begin"])
        except ValueError:
            raise ValueError(
                f"{cleaned['books_begin']!r} is not a date (expected "
                f"YYYY-MM-DD).") from None

    if not cleaned["currency"]:
        cleaned["currency"] = DEFAULT_SETTINGS["currency"]

    # Derived from the currency ONLY when nothing has been chosen - which is
    # how a set of books gets its first one. After the currency default above,
    # so that a blank currency still decides a format.
    if not cleaned["date_format"]:
        cleaned["date_format"] = default_date_format(cleaned["currency"])
    elif cleaned["date_format"] not in {fmt.key for fmt in DATE_FORMATS}:
        raise ValueError(
            f"{cleaned['date_format']!r} is not a date format this build can "
            f"show. Pick one of: "
            f"{', '.join(fmt.key for fmt in DATE_FORMATS)}.")
    return cleaned


def save_company(values: dict[str, str]) -> dict[str, str]:
    """Validate, then store. Nothing is written if anything is wrong."""
    cleaned = validate_company(values)
    with connect() as conn:
        for key, value in cleaned.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
    return cleaned


def company_display_name() -> str:
    """The name to print: the mailing name if one is set, else the company."""
    company = get_company()
    return company["mailing_name"] or company["company_name"]


# --------------------------------------------------------------------------- #
# more than one company
# --------------------------------------------------------------------------- #
def company_files() -> list[Path]:
    """Every company database in the data folder, newest first.

    The folder IS the registry - there is no index file to fall out of step
    with what is on disk, and dropping a company file in (or taking one away)
    just works. Backups are filtered out by name.
    """
    if not DATA_DIR.exists():
        return []
    files = [path for path in DATA_DIR.glob("*.db")
             if not any(mark in path.name.lower() for mark in _NOT_COMPANIES)
             and not path.name.startswith(".")]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


# --------------------------------------------------------------------------- #
# backups
# --------------------------------------------------------------------------- #
# A company is one file, which is what makes a backup a copy rather than an
# export. They go in a SUBFOLDER: company_files() globs ``*.db`` without
# recursing, so nothing in here can ever be mistaken for a company no matter
# what it is called - the name-based guard above protects the older
# ``books.backup-....db`` that already sat in the data folder.
BACKUP_DIR = DATA_DIR / "backups"

# Generous, because these are tens of kilobytes rather than the 144 MB the exe
# archive has to live with. Twenty sessions back is far enough to notice that
# something went wrong days ago.
KEEP_BACKUPS = 20

_STAMP = "%Y%m%d-%H%M%S"


def backup_dir() -> Path:
    return BACKUP_DIR


def _digest(path: Path) -> str:
    """A content hash, read in chunks so a large file is not held in memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


# Written INTO each backup, recording the books it was made from. A backup is
# produced by SQLite's own backup API, which rebuilds the file page by page -
# so the copy is logically identical but NOT byte-identical to its source, and
# hashing the copy can never tell you which books it came from. The source
# hash therefore has to be carried, not recomputed.
SOURCE_KEY = "backup_source_sha256"
TAKEN_KEY = "backup_taken_at"
ORIGIN_KEY = "backup_source_file"

_NAME_TIME = re.compile(r"-(\d{8}-\d{6})")


def _backup_order(path: Path):
    """Sort key: the timestamp in the NAME first, the real mtime to break ties.

    The name is what survives being copied to a USB stick or restored from
    one, so it leads. Two backups taken in the same second cannot be told
    apart by name - and the suffix that keeps them from overwriting each other
    sorts the wrong way round, "-open-2.db" before "-open.db" - so the
    filesystem's nanosecond mtime settles it.
    """
    found = _NAME_TIME.search(path.name)
    return (found.group(1) if found else "", path.stat().st_mtime_ns)


def backups_for(path=None) -> list[Path]:
    """Backups of one company, newest first.

    Matched with a strict pattern rather than a ``stem-*`` glob: a company
    called "books" and one called "books extra" would otherwise collect each
    other's backups, and pruning would then delete the wrong company's.
    """
    source = Path(path or DB_PATH)
    if not BACKUP_DIR.exists():
        return []
    pattern = re.compile(
        rf"^{re.escape(source.stem)}-\d{{8}}-\d{{6}}(-[a-z]+)?(-\d+)?\.db$")
    found = [p for p in BACKUP_DIR.glob("*.db") if pattern.match(p.name)]
    return sorted(found, key=_backup_order, reverse=True)


def backup_source_digest(path: Path) -> str:
    """The hash of the books a backup was taken from, as recorded inside it."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return ""
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?",
                           (SOURCE_KEY,)).fetchone()
        return row[0] if row else ""
    except sqlite3.Error:
        return ""        # not one of ours, or too old to carry the hash
    finally:
        conn.close()


def backup_is_current(path=None) -> bool:
    """True when the newest backup already holds exactly these books.

    Lets a caller skip ASKING about a backup it knows would be deduped away:
    offering to save work that is already saved, then writing nothing, reads
    as a backup that failed.
    """
    source = Path(path or DB_PATH)
    if not source.exists():
        return False
    newest = backups_for(source)
    return bool(newest and backup_source_digest(newest[0]) == _digest(source))


def prune_backups(path=None, keep: int = KEEP_BACKUPS) -> list[Path]:
    """Drop all but the newest ``keep`` backups of one company.

    Each failure is swallowed on purpose. Windows will refuse to delete a file
    something else has open, and a backup folder someone is browsing must not
    be able to stop a backup from being taken - it prunes on a later run.
    """
    dropped = []
    for old in backups_for(path)[keep:]:
        try:
            old.unlink()
            dropped.append(old)
        except OSError:
            pass
    return dropped


def backup_company(path=None, tag: str = "manual") -> Path | None:
    """Copy a company's books into the backup folder. Returns the file written.

    Returns **None** when the books are byte-for-byte what the newest backup
    already holds: five sessions that entered nothing should not leave five
    identical copies and push the useful ones out. Callers must SAY so rather
    than go quiet - see `MainWindow.ask_backup_on_close`, where silence on
    this path was reported as the backup not working.

    Uses SQLite's own online backup rather than a file copy. A plain copy taken
    while a write is in flight can capture a torn database, and the copy looks
    perfectly fine right up to the day you need it.
    """
    source = Path(path or DB_PATH)
    if not source.exists():
        return None

    digest = _digest(source)
    newest = backups_for(source)
    # Against the hash the newest backup CARRIES, never against the hash of
    # the backup file itself: see SOURCE_KEY above.
    if newest and backup_source_digest(newest[0]) == digest:
        return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime(_STAMP)
    target = BACKUP_DIR / f"{source.stem}-{stamp}-{tag}.db"
    # Two backups within the same second would otherwise overwrite each other.
    spare = 2
    while target.exists():
        target = BACKUP_DIR / f"{source.stem}-{stamp}-{tag}-{spare}.db"
        spare += 1

    src = sqlite3.connect(source)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
            # Stamped into the copy, not kept in an index beside it: an index
            # is one more thing that can fall out of step with the folder, and
            # a backup that travels on its own should still be able to say
            # which books it came from and when.
            dst.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(SOURCE_KEY, digest),
                 (TAKEN_KEY, datetime.now().isoformat(timespec="seconds")),
                 (ORIGIN_KEY, source.name)])
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()

    prune_backups(source)
    return target


def peek_settings(path, keys: tuple[str, ...]) -> dict[str, str]:
    """Read settings out of a company file WITHOUT switching to it.

    Opened read-only and defensively: anything may be sitting in the data
    folder with a .db suffix, and a file we cannot read should come back empty
    rather than stop the picker from opening.
    """
    found: dict[str, str] = {}
    try:
        conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            marks = ", ".join("?" * len(keys))
            rows = conn.execute(
                f"SELECT key, value FROM settings WHERE key IN ({marks})",
                keys).fetchall()
            for row in rows:
                found[row["key"]] = row["value"] or ""
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return found


def peek_company(path: Path) -> dict[str, str]:
    """A company's headline details, for the picker."""
    wanted = ("company_name", "mailing_name", "gstin", "books_begin")
    details = {key: "" for key in wanted}
    details.update(peek_settings(path, wanted))
    details["path"] = str(path)
    details["file"] = path.name
    if not details["company_name"]:
        details["company_name"] = path.stem
    return details


def list_companies() -> list[dict[str, str]]:
    return [peek_company(path) for path in company_files()]


def company_filename(name: str) -> Path:
    """A file name for a company, derived from its name and never colliding.

    Derived rather than opaque so the data folder stays readable: you can tell
    which file is which company without opening any of them.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "company"
    candidate = DATA_DIR / f"{slug}.db"
    serial = 2
    while candidate.exists():
        candidate = DATA_DIR / f"{slug}-{serial}.db"
        serial += 1
    return candidate


def use_company(path) -> Path:
    """Point every later query at this company's file.

    ``connect()`` reads DB_PATH on each call, so reassigning it here is all
    that switching takes - no connection is held open between calls.
    """
    global DB_PATH
    DB_PATH = Path(path)
    return DB_PATH


def create_company(values: dict[str, str]) -> Path:
    """Validate, make the file, seed the masters, store the details.

    Switches to the new company on success. On failure the previous company is
    restored, so a rejected name cannot leave the app pointing at a file that
    was never set up.
    """
    cleaned = validate_company(values)          # before creating anything
    previous = DB_PATH
    path = company_filename(cleaned["company_name"])
    try:
        use_company(path)
        init_db()
        save_company(cleaned)
    except Exception:
        use_company(previous)
        path.unlink(missing_ok=True)
        raise
    remember_last_company(path)
    return path


def remember_last_company(path) -> None:
    """Note which company was opened, to preselect it next time.

    Kept in a plain file beside the data rather than in a company database -
    which company to open cannot be answered from inside one - and outside the
    *.db scan so it is never mistaken for a company.
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LAST_COMPANY_FILE.write_text(str(Path(path).name), encoding="utf-8")
    except OSError:
        pass            # a preference, not data: losing it costs one click


def startup_company() -> Path:
    """Which company to have open when the window first appears.

    The app opens on the company you used last and ASKS afterwards, rather than
    holding the window back behind a dialog. With nothing to go on it falls
    back to the default file - which is also what the app used before it knew
    about more than one company, so a fresh install behaves as it always did
    instead of demanding a company be created before anything can be seen.
    """
    remembered = last_company()
    if remembered is not None:
        return remembered
    existing = company_files()
    if existing:
        return existing[0]
    return DATA_DIR / "books.db"


def last_company() -> Path | None:
    try:
        name = LAST_COMPANY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    candidate = DATA_DIR / name
    return candidate if name and candidate.exists() else None


# --------------------------------------------------------------------------- #
# connection
# --------------------------------------------------------------------------- #
@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _split_address(text: str) -> list[str]:
    """An old multi-line address as up to three separate lines.

    Anything past the third line is folded INTO the third rather than dropped:
    the lines are a presentation choice, but the text is the user's.
    """
    lines = [line.strip() for line in str(text or "").splitlines()]
    lines = [line for line in lines if line]
    if len(lines) > len(ADDRESS_LINES):
        keep = len(ADDRESS_LINES) - 1
        lines = lines[:keep] + [", ".join(lines[keep:])]
    return lines


def _migrate(conn) -> None:
    """Add columns and move settings that later versions introduced.

    `CREATE TABLE IF NOT EXISTS` leaves an existing table exactly as it was,
    so books created before a column existed need it added in place. Each
    step must be safe to run on a database that already has it.
    """
    # The address was one multi-line box until 2026-10-20 and is now separate
    # lines. A settings key dropped out of COMPANY_FIELDS is still IN the file,
    # it just stops being read - so an address typed into the old box would
    # quietly vanish from the screen while sitting in the database. Move it.
    stored = {row["key"]: row["value"] for row in
              conn.execute("SELECT key, value FROM settings").fetchall()}
    if "address" in stored:
        for key, value in zip(ADDRESS_LINES, _split_address(stored["address"])):
            if stored.get(key, "").strip():
                continue                # already filled in; leave it alone
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value))
        # Kept, not deleted: the original wording is the only record of how the
        # user had laid the address out, and keeping it is also what makes this
        # step run exactly once.
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('address_legacy', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (stored["address"],))
        conn.execute("DELETE FROM settings WHERE key = 'address'")

    columns = {row["name"] for row in
               conn.execute("PRAGMA table_info(vouchers)").fetchall()}
    if "voided" not in columns:
        conn.execute("ALTER TABLE vouchers ADD COLUMN voided INTEGER NOT NULL"
                     " DEFAULT 0")
    if "voided_at" not in columns:
        conn.execute("ALTER TABLE vouchers ADD COLUMN voided_at TEXT NOT NULL"
                     " DEFAULT ''")

    ledger_columns = {row["name"] for row in
                      conn.execute("PRAGMA table_info(ledgers)").fetchall()}
    # GST. Each is additive and safe to skip; gst_rate is deliberately
    # NULLABLE, because "not taxable" and "taxable at 0%" are different
    # things and a nil-rated supply still has to be reported.
    for column, declaration in (("gst_rate", "REAL"),
                                ("gst_role", "TEXT NOT NULL DEFAULT ''"),
                                ("gstin", "TEXT NOT NULL DEFAULT ''"),
                                ("state_code", "TEXT NOT NULL DEFAULT ''")):
        if column not in ledger_columns:
            conn.execute(f"ALTER TABLE ledgers ADD COLUMN {column}"
                         f" {declaration}")

    if "bill_wise" not in ledger_columns:
        conn.execute("ALTER TABLE ledgers ADD COLUMN bill_wise INTEGER NOT"
                     " NULL DEFAULT 0")
        # Switched on for the parties it is actually for, ONCE, at the moment
        # the column appears - never on later runs. Doing it every time
        # init_db ran would silently undo a ledger the user had turned off.
        # Sub-groups count, so this walks down from the two reserved groups
        # rather than matching a name.
        conn.execute(
            """
            UPDATE ledgers SET bill_wise = 1 WHERE group_id IN (
                WITH RECURSIVE under(id) AS (
                    SELECT id FROM groups
                     WHERE name IN ('Sundry Debtors', 'Sundry Creditors')
                    UNION ALL
                    SELECT g.id FROM groups g JOIN under u ON g.parent_id = u.id
                )
                SELECT id FROM under
            )
            """)

    # Lines posted to a party BEFORE bills existed carry no allocations, so
    # the outstanding report would show nothing while the balance sheet showed
    # the party owing money - silent under-reporting, the worst of the
    # options. Backfilled as ON ACCOUNT, which is the truth: the money is on
    # their account and against no bill. The report then says so, and says
    # nobody can be chased for it, which is the prompt to go and allocate it.
    #
    # Keyed on its own marker rather than on the column being added, because
    # the column may already have been added by an earlier version of this
    # function that did not backfill.
    done = conn.execute("SELECT value FROM settings WHERE key = ?",
                        (BACKFILL_KEY,)).fetchone()
    if not (done and done["value"]):
        conn.execute(
            """
            INSERT INTO bill_allocations (line_id, ledger_id, bill_ref, kind,
                                          signed, due_date, alloc_order)
            SELECT vl.id, vl.ledger_id, '', 'on_account',
                   ROUND(vl.debit - vl.credit, 2), '', 0
              FROM voucher_lines vl
              JOIN ledgers l ON l.id = vl.ledger_id
             WHERE l.bill_wise = 1
               AND ROUND(vl.debit - vl.credit, 2) <> 0
               AND NOT EXISTS (SELECT 1 FROM bill_allocations ba
                                WHERE ba.line_id = vl.id)
            """)
        conn.execute("INSERT INTO settings (key, value) VALUES (?, '1')"
                     " ON CONFLICT(key) DO UPDATE SET value = '1'",
                     (BACKFILL_KEY,))


def init_db() -> None:
    """Create the schema and the reserved masters. Safe to call repeatedly."""
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)

        if conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0:
            ids: dict[str, int] = {}
            for order, (name, parent, nature, gross) in enumerate(RESERVED_GROUPS):
                cur = conn.execute(
                    "INSERT INTO groups (name, parent_id, nature, affects_gross,"
                    " is_reserved, sort_order) VALUES (?, ?, ?, ?, 1, ?)",
                    (name, ids.get(parent), nature, gross, order),
                )
                ids[name] = int(cur.lastrowid)

        if conn.execute("SELECT COUNT(*) FROM ledgers").fetchone()[0] == 0:
            cash_group = conn.execute(
                "SELECT id FROM groups WHERE name = 'Cash-in-hand'"
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO ledgers (name, group_id, is_reserved) VALUES (?, ?, 1)",
                ("Cash", cash_group),
            )

        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT DO NOTHING",
                (key, value),
            )
        if not conn.execute(
            "SELECT value FROM settings WHERE key = 'books_begin'"
        ).fetchone()["value"]:
            today = datetime.now().date()
            start = today.replace(month=4, day=1)
            if today.month < 4:
                start = start.replace(year=today.year - 1)
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = 'books_begin'",
                (start.isoformat(),),
            )

    # After the block above has committed, because it opens its own
    # connection: nesting connect() inside a write transaction would wait on
    # a lock the outer call is still holding.
    seed_gst_ledgers()


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def get_setting(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


# --------------------------------------------------------------------------- #
# groups
# --------------------------------------------------------------------------- #
def get_groups() -> pd.DataFrame:
    with connect() as conn:
        return pd.read_sql_query(
            "SELECT g.*, p.name AS parent_name FROM groups g"
            " LEFT JOIN groups p ON p.id = g.parent_id"
            " ORDER BY g.sort_order, g.name", conn
        )


def group_id(name: str) -> int:
    with connect() as conn:
        row = conn.execute("SELECT id FROM groups WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise ValueError(f"There is no group called {name!r}.")
    return int(row["id"])


def add_group(name: str, parent: str, nature: str | None = None) -> int:
    """Create a sub-group. Nature is inherited from the parent unless given."""
    name = name.strip()
    if not name:
        raise ValueError("A group needs a name.")
    groups = get_groups().set_index("name")
    if name in groups.index:
        raise ValueError(f"There is already a group called {name!r}.")
    if parent not in groups.index:
        raise ValueError(f"There is no group called {parent!r}.")
    parent_row = groups.loc[parent]
    nature = nature or str(parent_row["nature"])
    if nature not in NATURES:
        raise ValueError(f"Unknown nature {nature!r}")
    with connect() as conn:
        nxt = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM groups"
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO groups (name, parent_id, nature, affects_gross,"
            " is_reserved, sort_order) VALUES (?, ?, ?, ?, 0, ?)",
            (name, int(parent_row["id"]), nature,
             int(parent_row["affects_gross"]), nxt),
        )
        return int(cur.lastrowid)


def delete_group(name: str) -> None:
    groups = get_groups().set_index("name")
    if name not in groups.index:
        raise ValueError(f"There is no group called {name!r}.")
    if int(groups.loc[name, "is_reserved"]):
        raise ValueError(f"{name!r} is one of the reserved groups.")
    gid = int(groups.loc[name, "id"])
    with connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM ledgers WHERE group_id = ?",
                        (gid,)).fetchone()[0]:
            raise ValueError(f"{name!r} still has ledgers under it.")
        if conn.execute("SELECT COUNT(*) FROM groups WHERE parent_id = ?",
                        (gid,)).fetchone()[0]:
            raise ValueError(f"{name!r} still has sub-groups under it.")
        conn.execute("DELETE FROM groups WHERE id = ?", (gid,))


def group_lineage(groups: pd.DataFrame | None = None) -> dict[int, list[str]]:
    """Each group's ancestry, root first — used to roll report subtotals up."""
    groups = get_groups() if groups is None else groups
    by_id = {int(r.id): r for r in groups.itertuples()}
    lineage: dict[int, list[str]] = {}
    for gid in by_id:
        chain, cursor = [], gid
        seen = set()
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            row = by_id.get(cursor)
            if row is None:
                break
            chain.append(str(row.name))
            cursor = int(row.parent_id) if not pd.isna(row.parent_id) else None
        lineage[gid] = list(reversed(chain))
    return lineage


def primary_group(group_name: str, lineage: dict[int, list[str]] | None = None,
                  groups: pd.DataFrame | None = None) -> str:
    """The reserved top-level group a given group ultimately sits under."""
    groups = get_groups() if groups is None else groups
    lineage = group_lineage(groups) if lineage is None else lineage
    match = groups[groups["name"] == group_name]
    if match.empty:
        return group_name
    return lineage[int(match.iloc[0]["id"])][0]


# --------------------------------------------------------------------------- #
# ledgers
# --------------------------------------------------------------------------- #
def get_ledgers(include_inactive: bool = False) -> pd.DataFrame:
    sql = (
        "SELECT l.*, g.name AS group_name, g.nature, g.affects_gross"
        " FROM ledgers l JOIN groups g ON g.id = l.group_id"
    )
    if not include_inactive:
        sql += " WHERE l.active = 1"
    sql += " ORDER BY l.name"
    with connect() as conn:
        frame = pd.read_sql_query(sql, conn)
    if not frame.empty:
        frame["opening_signed"] = frame.apply(
            lambda r: float(r["opening"]) * (1 if r["opening_side"] == "Dr" else -1),
            axis=1,
        ).round(2)
    return frame


def ledger_id(name: str) -> int:
    with connect() as conn:
        row = conn.execute("SELECT id FROM ledgers WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise ValueError(f"There is no ledger called {name!r}.")
    return int(row["id"])


BILL_WISE_GROUPS = ("Sundry Debtors", "Sundry Creditors")
# Marks that party lines posted before bills existed have been accounted for.
BACKFILL_KEY = "bills_backfilled"


def defaults_to_bill_wise(group: str) -> bool:
    """Whether a ledger under this group should keep bills by default.

    The two party groups, and anything the user has put beneath them. Asked
    of the GROUP rather than hardcoded per ledger, so a new customer comes
    out bill-wise without anyone remembering to tick a box.
    """
    lineage = group_lineage()
    gid = group_id(group)
    return any(name in BILL_WISE_GROUPS for name in lineage.get(gid, []))


def add_ledger(name: str, group: str, opening: float = 0.0,
               opening_side: str = "Dr", notes: str = "",
               bill_wise: bool | None = None, gst_rate: float | None = None,
               gstin: str = "", state_code: str = "") -> int:
    name = name.strip()
    if not name:
        raise ValueError("A ledger needs a name.")
    if opening_side not in SIDES:
        raise ValueError("Opening side must be 'Dr' or 'Cr'.")
    if float(opening) < 0:
        raise ValueError("Enter the opening balance as a positive amount and "
                         "choose Dr or Cr.")
    gid = group_id(group)
    if bill_wise is None:
        bill_wise = defaults_to_bill_wise(group)
    with connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM ledgers WHERE name = ?",
                        (name,)).fetchone()[0]:
            raise ValueError(f"There is already a ledger called {name!r}.")
        cur = conn.execute(
            "INSERT INTO ledgers (name, group_id, opening, opening_side, notes,"
            " bill_wise, gst_rate, gstin, state_code)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, gid, round(float(opening), 2), opening_side, notes.strip(),
             1 if bill_wise else 0,
             None if gst_rate is None else round(float(gst_rate), 2),
             gstin, state_code),
        )
        return int(cur.lastrowid)


def update_ledger(ledger: int, name: str, group: str, opening: float,
                  opening_side: str, notes: str = "", active: bool = True,
                  bill_wise: bool | None = None,
                  gst_rate: float | None = None, gstin: str | None = None,
                  state_code: str | None = None) -> None:
    if opening_side not in SIDES:
        raise ValueError("Opening side must be 'Dr' or 'Cr'.")
    if float(opening) < 0:
        raise ValueError("Enter the opening balance as a positive amount.")
    with connect() as conn:
        if bill_wise is None:
            # Left as it is when the caller does not say. Defaulting from the
            # group here would re-tick a ledger the user had turned off every
            # time anything else about it was edited.
            row = conn.execute("SELECT bill_wise FROM ledgers WHERE id = ?",
                               (int(ledger),)).fetchone()
            bill_wise = bool(row and row["bill_wise"])
        row = conn.execute("SELECT gst_rate, gstin, state_code FROM ledgers"
                           " WHERE id = ?", (int(ledger),)).fetchone()
        conn.execute(
            "UPDATE ledgers SET name = ?, group_id = ?, opening = ?,"
            " opening_side = ?, notes = ?, active = ?, bill_wise = ?,"
            " gst_rate = ?, gstin = ?, state_code = ? WHERE id = ?",
            (name.strip(), group_id(group), round(float(opening), 2), opening_side,
             notes.strip(), 1 if active else 0, 1 if bill_wise else 0,
             # None means "leave it", not "clear it" - the same convention as
             # bill_wise above, so a caller that knows nothing about GST
             # cannot wipe a rate by saving a ledger.
             (row["gst_rate"] if row else None) if gst_rate is None
             else round(float(gst_rate), 2),
             (row["gstin"] if row else "") if gstin is None else gstin.strip().upper(),
             (row["state_code"] if row else "") if state_code is None
             else state_code.strip(),
             int(ledger)),
        )


def set_ledger_gst(ledger: int, *, rate: float | None, gstin: str = "",
                   state_code: str = "") -> None:
    """Set a ledger's GST details, unconditionally - including clearing them.

    Separate from `update_ledger` because the two need `None` to mean
    opposite things. There, None means "leave this alone", which is what
    stops a caller that knows nothing about GST from wiping a rate while
    saving a name. Here it means "not taxable", which is a value in its own
    right and has to be settable - otherwise a rate put on by mistake could
    never be taken off.
    """
    with connect() as conn:
        if conn.execute("SELECT id FROM ledgers WHERE id = ?",
                        (int(ledger),)).fetchone() is None:
            raise ValueError("That ledger no longer exists.")
        conn.execute(
            "UPDATE ledgers SET gst_rate = ?, gstin = ?, state_code = ?"
            " WHERE id = ?",
            (None if rate is None else round(float(rate), 2),
             (gstin or "").strip().upper(), (state_code or "").strip(),
             int(ledger)))


def ledger_usage(ledger: int) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM voucher_lines WHERE ledger_id = ?", (int(ledger),)
        ).fetchone()[0]


def delete_ledger(ledger: int) -> None:
    if ledger_usage(ledger):
        raise ValueError("That ledger appears in vouchers — it cannot be deleted.")
    with connect() as conn:
        row = conn.execute("SELECT is_reserved, name FROM ledgers WHERE id = ?",
                           (int(ledger),)).fetchone()
        if row and int(row["is_reserved"]):
            raise ValueError(f"{row['name']} is a reserved ledger.")
        conn.execute("DELETE FROM ledgers WHERE id = ?", (int(ledger),))


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# GST
# --------------------------------------------------------------------------- #
# The state codes a GSTIN opens with. Held here because the first two
# characters of a GSTIN ARE the state code, which is what decides whether a
# supply is taxed as CGST+SGST or as IGST - so this is arithmetic, not a
# dropdown's worth of decoration.
STATE_CODES = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana", "07": "Delhi",
    "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam",
    "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra & Nagar Haveli and Daman & Diu", "27": "Maharashtra",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
    "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman & Nicobar Islands",
    "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory",
}

# The common slabs. Not a closed set - any rate can be typed - because the
# slabs change by notification and an app that refused a new one would be
# wrong the day after it moved.
GST_SLABS = (0.0, 0.1, 0.25, 1.0, 1.5, 3.0, 5.0, 6.0, 7.5, 12.0, 18.0, 28.0)

# The six tax ledgers, seeded under Duties & Taxes. `gst_role` is what the
# arithmetic looks them up by, so these names are only what you read.
GST_LEDGERS = (
    ("Output CGST", "output_cgst"),
    ("Output SGST", "output_sgst"),
    ("Output IGST", "output_igst"),
    ("Input CGST", "input_cgst"),
    ("Input SGST", "input_sgst"),
    ("Input IGST", "input_igst"),
)
GST_ROLES = tuple(role for _name, role in GST_LEDGERS)
OUTPUT_ROLES = tuple(r for r in GST_ROLES if r.startswith("output"))
INPUT_ROLES = tuple(r for r in GST_ROLES if r.startswith("input"))
GST_SEEDED_KEY = "gst_ledgers_seeded"


def state_code_of(gstin: str) -> str:
    """The state a GSTIN belongs to, or '' if it does not look like one."""
    gstin = (gstin or "").strip().upper()
    return gstin[:2] if len(gstin) >= 2 and gstin[:2] in STATE_CODES else ""


def state_name(code: str) -> str:
    return STATE_CODES.get((code or "").strip(), "")


def company_state_code() -> str:
    """Our own state, from our GSTIN, falling back to the State field.

    The GSTIN is preferred because it cannot disagree with itself: a typed
    state name can say Kerala while the GSTIN says 33 (Tamil Nadu), and the
    GSTIN is the one the tax follows.
    """
    company = get_company()
    from_gstin = state_code_of(company.get("gstin", ""))
    if from_gstin:
        return from_gstin
    typed = (company.get("state") or "").strip().lower()
    for code, name in STATE_CODES.items():
        if name.lower() == typed:
            return code
    return ""


def seed_gst_ledgers() -> None:
    """Create the six tax ledgers, once.

    Reserved, so they cannot be deleted out from under the GST report, and
    marked by `gst_role` so the report finds them however they are renamed.
    """
    if get_setting(GST_SEEDED_KEY):
        return
    with connect() as conn:
        group = conn.execute(
            "SELECT id FROM groups WHERE name = 'Duties & Taxes'").fetchone()
        if group is None:
            return
        for name, role in GST_LEDGERS:
            existing = conn.execute(
                "SELECT id FROM ledgers WHERE name = ?", (name,)).fetchone()
            if existing:
                conn.execute("UPDATE ledgers SET gst_role = ? WHERE id = ?",
                             (role, existing["id"]))
                continue
            conn.execute(
                "INSERT INTO ledgers (name, group_id, is_reserved, gst_role)"
                " VALUES (?, ?, 1, ?)", (name, group["id"], role))
        conn.execute("INSERT INTO settings (key, value) VALUES (?, '1')"
                     " ON CONFLICT(key) DO UPDATE SET value = '1'",
                     (GST_SEEDED_KEY,))


def gst_ledger_ids() -> dict[str, int]:
    """Tax ledger id by role — how every GST figure is found."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, gst_role FROM ledgers WHERE gst_role <> ''").fetchall()
    return {row["gst_role"]: int(row["id"]) for row in rows}


def supply_type(place_of_supply: str, our_state: str | None = None) -> str:
    """'intra' when the supply stays in our state, otherwise 'inter'.

    This single comparison is the whole of the CGST/SGST-versus-IGST
    decision, which is why it is derived rather than asked on every voucher.
    An unknown place of supply is treated as intra-state: that is what a
    counter sale to an unregistered walk-in customer is.
    """
    ours = (our_state if our_state is not None else company_state_code()).strip()
    theirs = (place_of_supply or "").strip()
    if not theirs or not ours:
        return "intra"
    return "intra" if theirs == ours else "inter"


def split_tax(taxable: float, rate: float, kind: str) -> dict[str, float]:
    """The tax on an amount, split the way the supply requires.

    Intra-state splits the rate down the middle into CGST and SGST; inter
    state puts the whole of it on IGST. Each half is rounded on its own and
    the second takes the remainder, so the two always add back to the total
    - halving 18% of 1,050 gives 94.50 twice, but a rate that does not
    halve cleanly would otherwise lose or gain a paisa against the invoice.
    """
    taxable = round(float(taxable), 2)
    total = round(taxable * float(rate) / 100.0, 2)
    if kind == "inter":
        return {"cgst": 0.0, "sgst": 0.0, "igst": total}
    half = round(total / 2, 2)
    return {"cgst": half, "sgst": round(total - half, 2), "igst": 0.0}


def save_gst_details(voucher_id: int, *, party_ledger_id: int | None,
                     party_gstin: str, place_of_supply: str,
                     supply: str, rate: float) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO gst_details (voucher_id, party_ledger_id,"
            " party_gstin, place_of_supply, supply_type, rate)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(voucher_id) DO UPDATE SET"
            " party_ledger_id = excluded.party_ledger_id,"
            " party_gstin = excluded.party_gstin,"
            " place_of_supply = excluded.place_of_supply,"
            " supply_type = excluded.supply_type, rate = excluded.rate",
            (int(voucher_id), party_ledger_id,
             (party_gstin or "").strip().upper(),
             (place_of_supply or "").strip(), supply, round(float(rate), 2)))


def clear_gst_details(voucher_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM gst_details WHERE voucher_id = ?",
                     (int(voucher_id),))


def get_gst_details(voucher_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM gst_details WHERE voucher_id = ?",
                           (int(voucher_id),)).fetchone()
    return dict(row) if row else None


def get_gst_vouchers(date_from=None, date_to=None,
                     include_voided: bool = False) -> pd.DataFrame:
    """Every voucher carrying GST, with its taxable value and tax.

    The amounts are SUMMED FROM THE LINES rather than read from
    `gst_details`, which holds only what the lines cannot say. So a voucher
    edited afterwards reports its new figures with nothing to re-save, and
    the GST report can never disagree with the ledgers it came from.
    """
    sql = (
        "SELECT v.id AS voucher_id, v.voucher_type, v.number, v.voucher_date,"
        " v.narration, v.voided, g.party_ledger_id, g.party_gstin,"
        " g.place_of_supply, g.supply_type, g.rate,"
        " p.name AS party,"
        " COALESCE(SUM(CASE WHEN l.gst_role = '' AND l.gst_rate IS NOT NULL"
        "      THEN ABS(vl.debit - vl.credit) END), 0) AS taxable,"
        " COALESCE(SUM(CASE WHEN l.gst_role IN ('output_cgst', 'input_cgst')"
        "      THEN ABS(vl.debit - vl.credit) END), 0) AS cgst,"
        " COALESCE(SUM(CASE WHEN l.gst_role IN ('output_sgst', 'input_sgst')"
        "      THEN ABS(vl.debit - vl.credit) END), 0) AS sgst,"
        " COALESCE(SUM(CASE WHEN l.gst_role IN ('output_igst', 'input_igst')"
        "      THEN ABS(vl.debit - vl.credit) END), 0) AS igst"
        " FROM gst_details g"
        " JOIN vouchers v ON v.id = g.voucher_id"
        " LEFT JOIN ledgers p ON p.id = g.party_ledger_id"
        " JOIN voucher_lines vl ON vl.voucher_id = v.id"
        " JOIN ledgers l ON l.id = vl.ledger_id"
        " WHERE 1 = 1"
    )
    params: list = []
    if not include_voided:
        sql += " AND v.voided = 0"
    if date_from is not None:
        sql += " AND v.voucher_date >= ?"
        params.append(str(date_from))
    if date_to is not None:
        sql += " AND v.voucher_date <= ?"
        params.append(str(date_to))
    sql += " GROUP BY v.id ORDER BY v.voucher_date, v.id"
    with connect() as conn:
        return pd.read_sql_query(sql, conn, params=params)


# vouchers
# --------------------------------------------------------------------------- #
def next_voucher_number(voucher_type: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(number), 0) + 1 FROM vouchers WHERE voucher_type = ?",
            (voucher_type,),
        ).fetchone()
    return int(row[0])


def last_voucher_id(voucher_type: str) -> int | None:
    """The most recently entered voucher of a type, for copying it forward.

    Ordered by NUMBER, not date: "the last one" here means the last one
    entered, which is what a recurring entry gets copied from - a voucher
    back-dated into last month is not the one you want to repeat.

    Voided ones are skipped. A voided voucher is a record of something that
    did not happen, so it is not what "the last one" means when the whole
    point is to do it again. Copying a voided voucher you are LOOKING at
    stays possible, because that is an explicit choice.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM vouchers WHERE voucher_type = ? AND voided = 0"
            " ORDER BY number DESC LIMIT 1",
            (voucher_type,),
        ).fetchone()
    return int(row["id"]) if row else None


def _validate_lines(lines: list[dict], ledgers: pd.DataFrame) -> list[dict]:
    known = set(ledgers["id"].astype(int))
    cleaned = []
    for order, line in enumerate(lines):
        if line.get("ledger_id") in (None, "") and not (
                line.get("debit") or line.get("credit")):
            continue  # a blank row in the grid
        try:
            lid = int(line["ledger_id"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Every line needs a ledger.") from None
        if lid not in known:
            raise ValueError("A line points at a ledger that does not exist.")
        debit = round(float(line.get("debit") or 0), 2)
        credit = round(float(line.get("credit") or 0), 2)
        if debit < 0 or credit < 0:
            raise ValueError("Use the other column instead of a negative amount.")
        if debit and credit:
            raise ValueError("A line is either a debit or a credit, not both.")
        if not debit and not credit:
            raise ValueError("Every line needs an amount.")
        cleaned.append({"ledger_id": lid, "debit": debit, "credit": credit,
                        "line_order": order,
                        "bills": line.get("bills") or []})

    if len(cleaned) < 2:
        raise ValueError("A voucher needs at least two lines.")
    if len({line["ledger_id"] for line in cleaned}) < 2:
        raise ValueError("A voucher needs at least two different ledgers.")
    debits = round(sum(line["debit"] for line in cleaned), 2)
    credits = round(sum(line["credit"] for line in cleaned), 2)
    if abs(debits - credits) > 0.005:
        raise ValueError(
            f"Debit {debits:,.2f} and credit {credits:,.2f} must agree — "
            f"the difference is {abs(debits - credits):,.2f}."
        )
    _validate_bills(cleaned, ledgers)
    return cleaned


ALLOCATION_KINDS = ("new", "against", "advance", "on_account")


def _validate_bills(cleaned: list[dict], ledgers: pd.DataFrame) -> None:
    """Check the bill allocations on every bill-wise line, and complete them.

    What is REFUSED is only what is arithmetically wrong: allocating more
    than the line holds, or allocating the opposite way to it. Allocating
    LESS is allowed, and the remainder becomes an explicit `on_account`
    allocation.

    That split follows the rule this project already uses - a voucher must
    balance because the arithmetic demands it, while the cash/bank
    conventions only warn because real books need exceptions. Demanding full
    allocation would have been a third thing: a bookkeeping PRACTICE enforced
    as arithmetic. It also instantly broke every voucher already posted to a
    party ledger, including simply re-saving one, which is how it was found.

    The remainder is MATERIALISED rather than left implicit, so the database
    says what it means: this money is on this party's account and attached to
    no bill. It also keeps the reconciliation simple - a party's ledger
    balance is the sum of their pending bills PLUS their on-account money,
    with nothing unaccounted for.

    Lines on ledgers that do not keep bills must carry no allocations at all,
    so a stray reference on a cash line cannot sit in the data unseen.
    """
    bill_wise = {}
    names = {}
    if not ledgers.empty:
        for row in ledgers.itertuples():
            bill_wise[int(row.id)] = bool(getattr(row, "bill_wise", 0))
            names[int(row.id)] = row.name

    for line in cleaned:
        lid = line["ledger_id"]
        bills = line["bills"]
        who = names.get(lid, "that ledger")
        if not bill_wise.get(lid):
            if bills:
                raise ValueError(
                    f"{who} does not keep bills, so its line cannot carry "
                    f"bill references. Turn on bill-wise for the ledger, or "
                    f"remove them.")
            continue

        amount = round(line["debit"] - line["credit"], 2)
        total = 0.0
        for bill in bills:
            kind = str(bill.get("kind") or "").strip()
            if kind not in ALLOCATION_KINDS:
                raise ValueError(f"{kind!r} is not a kind of bill allocation.")
            ref = str(bill.get("bill_ref") or "").strip()
            if kind != "on_account" and not ref:
                raise ValueError(
                    f"A {kind} allocation on {who} needs a bill reference, "
                    f"or it could never be settled later.")
            signed = round(float(bill.get("signed") or 0), 2)
            if not signed:
                raise ValueError(
                    f"A bill allocation on {who} has no amount.")
            # Allocating the opposite way to the line would mean a receipt
            # that increased what was owed.
            if (signed > 0) != (amount > 0):
                raise ValueError(
                    f"A bill allocation on {who} runs the opposite way to "
                    f"the line it is on.")
            total = round(total + signed, 2)

        over = abs(total) - abs(amount)
        if over > 0.005:
            raise ValueError(
                f"The bills on {who} come to {abs(total):,.2f}, which is "
                f"{over:,.2f} more than the {abs(amount):,.2f} on the line.")
        remainder = round(amount - total, 2)
        if abs(remainder) > 0.005:
            bills.append({"kind": "on_account", "bill_ref": "",
                          "signed": remainder, "due_date": ""})


def voucher_type_warnings(voucher_type: str, lines: list[dict],
                          ledgers: pd.DataFrame) -> list[str]:
    """Tally-style guidance: which side a voucher type expects cash or bank on.

    Returned as warnings rather than enforced, because real books occasionally
    need the exception — but you should see it before you accept the voucher.
    """
    if not lines:
        return []
    by_id = ledgers.set_index("id")
    groups = get_groups()
    lineage = group_lineage(groups)

    def is_cash(ledger_id_value: int) -> bool:
        if int(ledger_id_value) not in by_id.index:
            return False
        chain = lineage.get(int(by_id.loc[int(ledger_id_value), "group_id"]), [])
        own = str(by_id.loc[int(ledger_id_value), "group_name"])
        return any(name in CASH_GROUPS for name in chain + [own])

    cash_debits = [l for l in lines if l["debit"] and is_cash(l["ledger_id"])]
    cash_credits = [l for l in lines if l["credit"] and is_cash(l["ledger_id"])]
    any_cash = cash_debits or cash_credits
    notes = []

    if voucher_type == "Payment" and not cash_credits:
        notes.append("A payment normally credits a cash or bank ledger — the "
                     "money has to leave something.")
    if voucher_type == "Receipt" and not cash_debits:
        notes.append("A receipt normally debits a cash or bank ledger — the "
                     "money has to arrive somewhere.")
    if voucher_type == "Contra" and not (cash_debits and cash_credits):
        notes.append("A contra moves money between your own cash and bank "
                     "accounts, so both sides should be cash or bank.")
    if voucher_type == "Journal" and any_cash:
        notes.append("A journal is for adjustments that touch neither cash nor "
                     "bank. Use Payment, Receipt or Contra for money movements.")

    # Money on a party's account but attached to no bill. Allowed - and saved
    # as an explicit on-account allocation - but it will not appear on the
    # outstanding report as anything anyone can chase, so say so now.
    for line in lines:
        lid = line.get("ledger_id")
        if lid is None or lid not in by_id.index:
            continue
        if not bool(by_id.loc[lid].get("bill_wise", 0)):
            continue
        amount = round(float(line.get("debit") or 0)
                       - float(line.get("credit") or 0), 2)
        allocated = round(sum(float(b.get("signed") or 0)
                              for b in (line.get("bills") or [])
                              if b.get("kind") != "on_account"), 2)
        loose = round(amount - allocated, 2)
        if abs(loose) > 0.005:
            notes.append(
                f"{abs(loose):,.2f} on {by_id.loc[lid]['name']} is not "
                f"against any bill, so it will sit on account rather than on "
                f"the outstanding report.")
    return notes


def save_voucher(*, voucher_type: str, voucher_date, narration: str,
                 lines: list[dict], voucher_id: int | None = None,
                 number: int | None = None, gst: dict | None = None) -> int:
    """Post a voucher. Refuses anything that does not balance."""
    if voucher_type not in VOUCHER_TYPES:
        raise ValueError(f"Unknown voucher type {voucher_type!r}")
    if voucher_id is not None and is_voided(voucher_id):
        # A voided voucher is a record of what was annulled. Editing one would
        # quietly rewrite history that the books have already set aside.
        raise ValueError(
            "This voucher is voided and cannot be altered. Restore it first "
            "if you want to change it.")
    cleaned = _validate_lines(lines, get_ledgers(include_inactive=True))
    date_str = (voucher_date.isoformat() if hasattr(voucher_date, "isoformat")
                else str(voucher_date))

    with connect() as conn:
        if voucher_id is None:
            assigned = number or next_voucher_number(voucher_type)
            cur = conn.execute(
                "INSERT INTO vouchers (voucher_type, number, voucher_date,"
                " narration, created_at) VALUES (?, ?, ?, ?, ?)",
                (voucher_type, int(assigned), date_str, narration.strip(), _now()),
            )
            voucher_id = int(cur.lastrowid)
        else:
            conn.execute(
                "UPDATE vouchers SET voucher_type = ?, voucher_date = ?,"
                " narration = ? WHERE id = ?",
                (voucher_type, date_str, narration.strip(), int(voucher_id)),
            )
            conn.execute("DELETE FROM voucher_lines WHERE voucher_id = ?",
                         (int(voucher_id),))
        # One at a time rather than executemany, because each line's bill
        # allocations need its rowid. Deleting the lines above took their old
        # allocations with them, by cascade.
        for line in cleaned:
            cur = conn.execute(
                "INSERT INTO voucher_lines (voucher_id, ledger_id, debit,"
                " credit, line_order) VALUES (?, ?, ?, ?, ?)",
                (voucher_id, line["ledger_id"], line["debit"], line["credit"],
                 line["line_order"]),
            )
            line_id = int(cur.lastrowid)
            for order, bill in enumerate(line["bills"]):
                kind = str(bill["kind"]).strip()
                conn.execute(
                    "INSERT INTO bill_allocations (line_id, ledger_id,"
                    " bill_ref, kind, signed, due_date, alloc_order)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (line_id, line["ledger_id"],
                     str(bill.get("bill_ref") or "").strip(), kind,
                     round(float(bill["signed"]), 2),
                     # A due date belongs to the bill, so it is recorded on
                     # the allocation that OPENS one and nowhere else.
                     str(bill.get("due_date") or "")
                     if kind in ("new", "advance") else "",
                     order),
                )
    _store_gst(int(voucher_id), gst)
    return int(voucher_id)


def _store_gst(voucher_id: int, gst: dict | None) -> None:
    """Record the GST facts of a voucher, or clear ones left behind.

    `gst=None` means "say nothing about GST", which on a NEW voucher means
    there is none. On an edited one it means leave what is there - EXCEPT
    when the voucher no longer has a single tax line, in which case the
    details are stale and go. Without that, taking the tax off a voucher
    would leave it still listed on the GST return.
    """
    if gst:
        save_gst_details(
            voucher_id,
            party_ledger_id=gst.get("party_ledger_id"),
            party_gstin=gst.get("party_gstin", ""),
            place_of_supply=gst.get("place_of_supply", ""),
            supply=gst.get("supply_type")
            or supply_type(gst.get("place_of_supply", "")),
            rate=gst.get("rate", 0.0))
        return
    with connect() as conn:
        taxed = conn.execute(
            "SELECT COUNT(*) FROM voucher_lines vl"
            " JOIN ledgers l ON l.id = vl.ledger_id"
            " WHERE vl.voucher_id = ? AND l.gst_role <> ''",
            (voucher_id,)).fetchone()[0]
    if not taxed:
        clear_gst_details(voucher_id)


def get_vouchers(date_from=None, date_to=None,
                 voucher_type: str | None = None) -> pd.DataFrame:
    sql = "SELECT * FROM vouchers WHERE 1 = 1"
    params: list = []
    if date_from is not None:
        sql += " AND voucher_date >= ?"
        params.append(str(date_from))
    if date_to is not None:
        sql += " AND voucher_date <= ?"
        params.append(str(date_to))
    if voucher_type:
        sql += " AND voucher_type = ?"
        params.append(voucher_type)
    sql += " ORDER BY voucher_date DESC, id DESC"
    with connect() as conn:
        return pd.read_sql_query(sql, conn, params=params)


def get_lines(date_from=None, date_to=None,
              include_voided: bool = False) -> pd.DataFrame:
    """Every voucher line in the period, joined to its voucher and ledger.

    Voided vouchers are LEFT OUT by default. This is the one place the whole
    accounting layer reads lines from, so excluding them here is what keeps
    them out of every balance, the trial balance, both statements and the
    ledger without a word of report code knowing about voiding. The Day Book
    passes `include_voided=True` on purpose: a voided voucher must stay
    visible, holding its number, which is the entire point of voiding it.
    """
    sql = (
        "SELECT vl.id, vl.voucher_id, vl.ledger_id, vl.debit, vl.credit,"
        " vl.line_order, v.voucher_type, v.number, v.voucher_date, v.narration,"
        " v.voided, l.name AS ledger_name, g.name AS group_name, g.nature,"
        " g.affects_gross, l.group_id"
        " FROM voucher_lines vl"
        " JOIN vouchers v ON v.id = vl.voucher_id"
        " JOIN ledgers l ON l.id = vl.ledger_id"
        " JOIN groups g ON g.id = l.group_id WHERE 1 = 1"
    )
    params: list = []
    if not include_voided:
        sql += " AND v.voided = 0"
    if date_from is not None:
        sql += " AND v.voucher_date >= ?"
        params.append(str(date_from))
    if date_to is not None:
        sql += " AND v.voucher_date <= ?"
        params.append(str(date_to))
    sql += " ORDER BY v.voucher_date, v.id, vl.line_order"
    with connect() as conn:
        frame = pd.read_sql_query(sql, conn, params=params)
    if not frame.empty:
        frame["signed"] = (frame["debit"] - frame["credit"]).round(2)
    return frame


def get_voucher(voucher_id: int) -> tuple[dict, list[dict]] | None:
    with connect() as conn:
        head = conn.execute("SELECT * FROM vouchers WHERE id = ?",
                            (int(voucher_id),)).fetchone()
        if head is None:
            return None
        lines = conn.execute(
            "SELECT vl.id, vl.ledger_id, vl.debit, vl.credit,"
            " l.name AS ledger_name, l.bill_wise"
            " FROM voucher_lines vl JOIN ledgers l ON l.id = vl.ledger_id"
            " WHERE vl.voucher_id = ? ORDER BY vl.line_order",
            (int(voucher_id),),
        ).fetchall()
        allocations = conn.execute(
            "SELECT line_id, bill_ref, kind, signed, due_date"
            " FROM bill_allocations WHERE line_id IN ("
            "   SELECT id FROM voucher_lines WHERE voucher_id = ?)"
            " ORDER BY line_id, alloc_order",
            (int(voucher_id),),
        ).fetchall()
    by_line: dict[int, list[dict]] = {}
    for row in allocations:
        by_line.setdefault(int(row["line_id"]), []).append(dict(row))
    out = []
    for line in lines:
        line = dict(line)
        # Read back under the same key save_voucher takes, so a voucher can
        # be loaded, changed and saved again without the caller translating.
        line["bills"] = by_line.get(int(line["id"]), [])
        out.append(line)
    return dict(head), out


def delete_voucher(voucher_id: int) -> None:
    """Remove a voucher outright. Its number is given up and leaves a gap."""
    with connect() as conn:
        conn.execute("DELETE FROM voucher_lines WHERE voucher_id = ?",
                     (int(voucher_id),))
        conn.execute("DELETE FROM vouchers WHERE id = ?", (int(voucher_id),))


def get_allocations(as_at=None, include_voided: bool = False) -> pd.DataFrame:
    """Every bill allocation, with the voucher and party it belongs to.

    Voided vouchers are left out by default for the same reason they are left
    out of `get_lines`: a voided invoice is not money anyone owes. This is the
    single place the bill reports read from, so that holds everywhere at once.
    """
    sql = (
        "SELECT ba.id, ba.line_id, ba.ledger_id, ba.bill_ref, ba.kind,"
        " ba.signed, ba.due_date, ba.alloc_order,"
        " l.name AS ledger_name, g.name AS group_name, g.nature,"
        " v.id AS voucher_id, v.voucher_type, v.number, v.voucher_date,"
        " v.narration, v.voided"
        " FROM bill_allocations ba"
        " JOIN voucher_lines vl ON vl.id = ba.line_id"
        " JOIN vouchers v ON v.id = vl.voucher_id"
        " JOIN ledgers l ON l.id = ba.ledger_id"
        " JOIN groups g ON g.id = l.group_id WHERE 1 = 1"
    )
    params: list = []
    if not include_voided:
        sql += " AND v.voided = 0"
    if as_at is not None:
        sql += " AND v.voucher_date <= ?"
        params.append(str(as_at))
    sql += " ORDER BY v.voucher_date, v.id, ba.alloc_order"
    with connect() as conn:
        return pd.read_sql_query(sql, conn, params=params)


def is_voided(voucher_id: int) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT voided FROM vouchers WHERE id = ?",
                           (int(voucher_id),)).fetchone()
    return bool(row and row["voided"])


def void_voucher(voucher_id: int) -> None:
    """Annul a voucher while KEEPING its number and its lines.

    The voucher stops affecting the books the moment this is set, because
    `get_lines()` leaves voided vouchers out. Nothing is erased: the lines
    stay so the Day Book can still show what the voucher was, and the number
    stays occupied so the series has no gap. `restore_voucher` undoes it.
    """
    with connect() as conn:
        if conn.execute("SELECT id FROM vouchers WHERE id = ?",
                        (int(voucher_id),)).fetchone() is None:
            raise ValueError("That voucher no longer exists.")
        conn.execute("UPDATE vouchers SET voided = 1, voided_at = ?"
                     " WHERE id = ?", (_now(), int(voucher_id)))


def restore_voucher(voucher_id: int) -> None:
    """Bring a voided voucher back into the books, unchanged."""
    with connect() as conn:
        if conn.execute("SELECT id FROM vouchers WHERE id = ?",
                        (int(voucher_id),)).fetchone() is None:
            raise ValueError("That voucher no longer exists.")
        conn.execute("UPDATE vouchers SET voided = 0, voided_at = ''"
                     " WHERE id = ?", (int(voucher_id),))
