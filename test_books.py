"""Works a small set of books by hand and checks every report against it.

Run:  python test_books.py
Uses a throwaway database, so nothing real is touched.
"""

import os
import sys
import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP = Path(tempfile.gettempdir()) / "bkplus_test.db"
TMP.unlink(missing_ok=True)
os.environ["BOOKS_DB"] = str(TMP)
# Company files are created in BOOKS_DIR, so point it at a scratch
# folder: without this the suite would write companies into the
# user's real data/ directory.
SANDBOX = Path(tempfile.gettempdir()) / f"bkplus_sandbox_{TMP.stem}"
shutil.rmtree(SANDBOX, ignore_errors=True)
SANDBOX.mkdir(parents=True, exist_ok=True)
os.environ["BOOKS_DIR"] = str(SANDBOX)

import pandas as pd  # noqa: E402
import accounting as acc  # noqa: E402
import db  # noqa: E402

FAILURES = []


def check(label, got, want, tol=0.005):
    ok = abs(got - want) <= tol if isinstance(want, float) else got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(label)


def post(vtype, day, narration, *lines):
    """post('Payment', 5, 'rent', ('Rent', 10000, 0), ('Cash', 0, 10000))"""
    return db.save_voucher(
        voucher_type=vtype, voucher_date=date(2026, 4, day), narration=narration,
        lines=[{"ledger_id": db.ledger_id(name), "debit": dr, "credit": cr}
               for name, dr, cr in lines],
    )


db.init_db()

print("\n1) The reserved group tree matches Tally's")
groups = db.get_groups()
check("28 reserved groups seeded", len(groups), 27)
check("all marked reserved", int(groups["is_reserved"].sum()), 27)
by_name = groups.set_index("name")
check("Sundry Debtors sits under Current Assets",
      by_name.loc["Sundry Debtors", "parent_name"], "Current Assets")
check("Sundry Creditors under Current Liabilities",
      by_name.loc["Sundry Creditors", "parent_name"], "Current Liabilities")
check("Sales is income", by_name.loc["Sales Accounts", "nature"], "income")
check("Sales affects gross profit",
      int(by_name.loc["Sales Accounts", "affects_gross"]), 1)
check("Indirect Expenses does not",
      int(by_name.loc["Indirect Expenses", "affects_gross"]), 0)
check("Cash ledger created", "Cash" in set(db.get_ledgers()["name"]), True)
check("Cash is under Cash-in-hand",
      db.get_ledgers().set_index("name").loc["Cash", "group_name"], "Cash-in-hand")

print("\n2) Masters: ledgers under groups, with opening balances")
db.add_ledger("Bank of India", "Bank Accounts", 200_000, "Dr")
db.add_ledger("Capital - Owner", "Capital Account", 250_000, "Cr")
db.add_ledger("Stall Equipment", "Fixed Assets", 50_000, "Dr")
db.add_ledger("Sales", "Sales Accounts")
db.add_ledger("Purchases", "Purchase Accounts")
db.add_ledger("Freight Inward", "Direct Expenses")
db.add_ledger("Rent", "Indirect Expenses")
db.add_ledger("Interest Received", "Indirect Incomes")
db.add_ledger("Acme Traders", "Sundry Debtors")
db.add_ledger("Bharat Supplies", "Sundry Creditors")

ledgers = db.get_ledgers().set_index("name")
# The six GST tax ledgers are seeded too, so count the ones made HERE rather
# than every ledger in the book - otherwise this breaks each time the app
# seeds another master, which says nothing about what is being tested.
check("ledger count",
      len([n for n in ledgers.index
           if n not in [name for name, _role in db.GST_LEDGERS]]), 11)
check("and the tax ledgers are there as well, seeded not entered",
      len([n for n in ledgers.index
           if n in [name for name, _role in db.GST_LEDGERS]]), 6)
check("opening Dr is positive",
      float(ledgers.loc["Bank of India", "opening_signed"]), 200_000.0)
check("opening Cr is negative",
      float(ledgers.loc["Capital - Owner", "opening_signed"]), -250_000.0)
check("openings disagree by 0", acc.opening_difference(), 0.0)

for label, args in [
    ("duplicate ledger name", ("Rent", "Indirect Expenses")),
    ("unknown group", ("Nonsense", "No Such Group")),
]:
    try:
        db.add_ledger(*args)
        check(label, "accepted", "rejected")
    except ValueError:
        check(label, "rejected", "rejected")
try:
    db.add_ledger("Negative", "Fixed Assets", -5)
    check("negative opening", "accepted", "rejected")
except ValueError:
    check("negative opening", "rejected", "rejected")

print("\n3) A voucher must balance before it will post")
for label, lines in [
    ("unbalanced", [("Rent", 1000, 0), ("Cash", 0, 900)]),
    ("single line", [("Rent", 1000, 0)]),
    ("both columns on one line", [("Rent", 500, 500), ("Cash", 0, 500)]),
    ("same ledger twice", [("Rent", 500, 0), ("Rent", 0, 500)]),
    ("zero amounts", [("Rent", 0, 0), ("Cash", 0, 0)]),
]:
    try:
        post("Journal", 1, label, *lines)
        check(label, "accepted", "rejected")
    except ValueError:
        check(label, "rejected", "rejected")

print("\n4) Voucher numbering runs per type")
check("first payment is 1", db.next_voucher_number("Payment"), 1)
post("Payment", 5, "April rent", ("Rent", 30_000, 0), ("Bank of India", 0, 30_000))
check("next payment is 2", db.next_voucher_number("Payment"), 2)
check("receipts number separately", db.next_voucher_number("Receipt"), 1)
post("Payment", 6, "Freight", ("Freight Inward", 5_000, 0), ("Cash", 0, 5_000))
check("second payment numbered 2",
      int(db.get_vouchers(voucher_type="Payment")["number"].min()), 1)

print("\n5) Trading and indirect entries")
# credit sale to Acme, part received
post("Sales", 7, "Invoice 1", ("Acme Traders", 400_000, 0), ("Sales", 0, 400_000))
post("Receipt", 8, "Acme part payment",
     ("Bank of India", 250_000, 0), ("Acme Traders", 0, 250_000))
# credit purchase from Bharat, part paid
post("Purchase", 9, "Bill 77", ("Purchases", 180_000, 0), ("Bharat Supplies", 0, 180_000))
post("Payment", 10, "Bharat part payment",
     ("Bharat Supplies", 100_000, 0), ("Bank of India", 0, 100_000))
post("Receipt", 11, "Bank interest",
     ("Bank of India", 2_000, 0), ("Interest Received", 0, 2_000))
post("Contra", 12, "Cash drawn from bank",
     ("Cash", 20_000, 0), ("Bank of India", 0, 20_000))

print("\n6) Ledger balances")
bal = acc.balances().set_index("ledger")
# bank: 200,000 - 30,000 + 250,000 - 100,000 + 2,000 - 20,000
check("Bank of India", float(bal.loc["Bank of India", "closing"]), 302_000.0)
check("Cash", float(bal.loc["Cash", "closing"]), 15_000.0)
check("Acme Traders owes 150,000", float(bal.loc["Acme Traders", "closing"]), 150_000.0)
check("Bharat Supplies owed 80,000",
      float(bal.loc["Bharat Supplies", "closing"]), -80_000.0)
check("Sales is a credit balance", float(bal.loc["Sales", "closing"]), -400_000.0)
check("Rent is a debit balance", float(bal.loc["Rent", "closing"]), 30_000.0)

print("\n7) The trial balance agrees")
tb = acc.trial_balance()
check("debits equal credits", tb["difference"], 0.0, tol=0.0)
check("no opening difference", acc.opening_difference(), 0.0)
rows = tb["rows"].set_index("Particulars")
check("bank shows as a debit", float(rows.loc["Bank of India", "Debit"]), 302_000.0)
check("capital shows as a credit",
      float(rows.loc["Capital - Owner", "Credit"]), 250_000.0)
check("nil ledgers are omitted", "Stall Equipment" in rows.index, True)
grouped = acc.trial_balance(by_group=True)
check("grouped trial balance also agrees", grouped["difference"], 0.0, tol=0.0)
check("grouped totals match the detailed one", grouped["debit"], tb["debit"])

print("\n8) Profit & loss, trading then indirect")
pl = acc.profit_and_loss()
t = pl["totals"]
check("trading income", t["trading_income"], 400_000.0)
check("trading expense", t["trading_expense"], 185_000.0)   # purchases + freight
check("gross profit", t["gross_profit"], 215_000.0)
check("indirect income", t["indirect_income"], 2_000.0)
check("indirect expense", t["indirect_expense"], 30_000.0)
check("net profit", t["net_profit"], 187_000.0)
check("freight is a trading expense",
      "Freight Inward" in set(pl["trading_expense"]["Particulars"]), True)
check("rent is an indirect expense",
      "Rent" in set(pl["indirect_expense"]["Particulars"]), True)
check("no balance-sheet ledger leaked in",
      "Bank of India" in set(pl["indirect_expense"]["Particulars"]), False)

print("\n9) The balance sheet balances")
bs = acc.balance_sheet()
check("both sides agree", bs["totals"]["difference"], 0.0, tol=0.0)
check("no difference in opening balances",
      "Difference in opening balances" in set(bs["liabilities"]["Particulars"])
      or "Difference in opening balances" in set(bs["assets"]["Particulars"]),
      False)
liab = bs["liabilities"].set_index("Particulars")
asset = bs["assets"].set_index("Particulars")
check("capital on the liabilities side",
      float(liab.loc["Capital Account", "Amount"]), 250_000.0)
check("creditors on the liabilities side",
      float(liab.loc["Current Liabilities", "Amount"]), 80_000.0)
check("profit carried to the liabilities side",
      float(liab.loc["Profit & Loss A/c", "Amount"]), 187_000.0)
check("fixed assets", float(asset.loc["Fixed Assets", "Amount"]), 50_000.0)
# current assets: bank 302,000 + cash 15,000 + debtor 150,000
check("current assets", float(asset.loc["Current Assets", "Amount"]), 467_000.0)
check("assets total", bs["totals"]["assets"], 517_000.0)
check("liabilities total", bs["totals"]["liabilities"], 517_000.0)

print("\n10) A ledger statement reads like Tally's")
st = acc.ledger_statement(db.ledger_id("Bank of India"))
check("opening", st["opening"], 200_000.0)
check("closing", st["closing"], 302_000.0)
# 2 debits (receipt 250,000 + interest 2,000) and 3 credits
# (rent 30,000 + supplier 100,000 + contra 20,000)
check("five entries", len(st["entries"]), 5)
check("debit total", st["debit_total"], 252_000.0)
check("credit total", st["credit_total"], 150_000.0)
check("opening + debits - credits = closing",
      round(st["opening"] + st["debit_total"] - st["credit_total"], 2), st["closing"])
first = st["entries"].iloc[0]
check("a credit entry reads 'By'", str(first["Particulars"]).startswith("By "), True)
check("particulars name the other side", "Rent" in str(first["Particulars"]), True)
check("the running balance is formatted with a side",
      str(st["entries"].iloc[-1]["Balance"]), "302,000.00 Dr")

acme = acc.ledger_statement(db.ledger_id("Acme Traders"))
check("debtor closing is Dr", acme["closing"], 150_000.0)
check("a debit entry reads 'To'",
      str(acme["entries"].iloc[0]["Particulars"]).startswith("To "), True)

print("\n11) Period filters on the ledger statement")
later = acc.ledger_statement(db.ledger_id("Bank of India"),
                             date_from="2026-04-08", date_to="2026-04-30")
check("opening rolls forward to the start date", later["opening"], 170_000.0)
check("closing is unchanged", later["closing"], 302_000.0)
check("only the later entries appear", len(later["entries"]), 4)

print("\n12) The day book lists every voucher once")
book = acc.day_book()
check("eight vouchers", len(book), 8)
check("each row balances",
      float((book["Debit"] - book["Credit"]).abs().max()), 0.0)
check("newest first", str(book.iloc[0]["Date"]), "2026-04-12")
check("voucher types are named", "Contra" in set(book["Vch Type"]), True)

# The two sides get a COLUMN EACH (user asked 2026-10-20). As one
# "Rent  <-  Bank of India" cell you could not read down a column to see what
# a ledger was debited against, and no spreadsheet can filter on an arrow.
check("the sides have a column each, where Particulars was",
      list(book.columns)[:3], ["Date", "Debit account", "Credit account"])
rent = book[(book["Vch Type"] == "Payment")
            & (book["Debit account"] == "Rent")].iloc[0]
check("the debited ledger is in the debit column",
      rent["Debit account"], "Rent")
check("and the credited one in the credit column",
      rent["Credit account"], "Bank of India")
check("with no arrow left anywhere in the report",
      any("←" in str(v) for row in book.itertuples(index=False) for v in row),
      False)

narrow = acc.day_book(date_from="2026-04-09", date_to="2026-04-10")
check("the date filter works", len(narrow), 2)

print("\n13) Voucher type guidance, as warnings not blocks")
ledgers_now = db.get_ledgers(include_inactive=True)


def warn(vtype, *lines):
    return db.voucher_type_warnings(
        vtype,
        [{"ledger_id": db.ledger_id(n), "debit": dr, "credit": cr}
         for n, dr, cr in lines],
        ledgers_now,
    )


check("a payment that credits bank is fine",
      warn("Payment", ("Rent", 100, 0), ("Bank of India", 0, 100)), [])
check("a payment crediting nothing liquid is flagged",
      any("cash or bank" in n for n in
          warn("Payment", ("Rent", 100, 0), ("Acme Traders", 0, 100))), True)
check("a receipt debiting bank is fine",
      warn("Receipt", ("Bank of India", 100, 0), ("Sales", 0, 100)), [])
check("a contra between bank and cash is fine",
      warn("Contra", ("Cash", 100, 0), ("Bank of India", 0, 100)), [])
check("a contra touching a non-cash ledger is flagged",
      len(warn("Contra", ("Rent", 100, 0), ("Bank of India", 0, 100))), 1)
check("a journal touching bank is flagged",
      len(warn("Journal", ("Rent", 100, 0), ("Bank of India", 0, 100))), 1)
check("a journal that touches neither cash nor bank draws no such warning",
      any("cash" in n or "bank" in n for n in
          warn("Journal", ("Rent", 100, 0), ("Acme Traders", 0, 100))), False)
# Acme Traders is a party, so the SAME voucher is flagged for a different
# reason: 100 landing on their account against no bill.
check("though a party line with no bill reference is flagged on its own",
      any("not against any bill" in n for n in
          warn("Journal", ("Rent", 100, 0), ("Acme Traders", 0, 100))), True)

print("\n14) Opening balances that disagree are shown, not hidden")
db.add_ledger("Odd Asset", "Fixed Assets", 9_000, "Dr")
check("opening difference appears", acc.opening_difference(), 9_000.0)
tb2 = acc.trial_balance()
check("the trial balance now shows the gap", tb2["difference"], 9_000.0)
bs2 = acc.balance_sheet()
check("the balance sheet still balances", bs2["totals"]["difference"], 0.0, tol=0.0)
check("because the residual is named",
      float(bs2["liabilities"].set_index("Particulars")
            .loc["Difference in opening balances", "Amount"]), 9_000.0)
db.delete_ledger(db.ledger_id("Odd Asset"))
check("removing it clears the gap", acc.opening_difference(), 0.0)

print("\n15) Editing and deleting a voucher")
vid = db.get_vouchers(voucher_type="Payment")["id"].min()
head, lines = db.get_voucher(int(vid))
check("the voucher reads back", len(lines), 2)
db.save_voucher(voucher_id=int(vid), voucher_type="Payment",
                voucher_date=date(2026, 4, 5), narration="April rent (revised)",
                lines=[{"ledger_id": db.ledger_id("Rent"), "debit": 35_000,
                        "credit": 0},
                       {"ledger_id": db.ledger_id("Bank of India"), "debit": 0,
                        "credit": 35_000}])
check("rent after the edit",
      float(acc.balances().set_index("ledger").loc["Rent", "closing"]), 35_000.0)
check("the trial balance still agrees", acc.trial_balance()["difference"], 0.0, tol=0.0)

db.delete_voucher(int(vid))
check("one fewer voucher", len(acc.day_book()), 7)
check("rent is back to nil",
      float(acc.balances().set_index("ledger").loc["Rent", "closing"]), 0.0)
check("no orphan lines", len(db.get_lines()), 14)
check("still balanced after a delete",
      acc.trial_balance()["difference"], 0.0, tol=0.0)
check("the balance sheet still balances",
      acc.balance_sheet()["totals"]["difference"], 0.0, tol=0.0)

print("\n16) A ledger in use cannot be deleted")
check("usage is counted", db.ledger_usage(db.ledger_id("Sales")) > 0, True)
try:
    db.delete_ledger(db.ledger_id("Sales"))
    check("deleting a used ledger", "accepted", "rejected")
except ValueError:
    check("deleting a used ledger", "rejected", "rejected")
try:
    db.delete_ledger(db.ledger_id("Cash"))
    check("deleting the reserved Cash ledger", "accepted", "rejected")
except ValueError:
    check("deleting the reserved Cash ledger", "rejected", "rejected")

print("\n17) Custom sub-groups inherit their parent's nature")
db.add_group("Vehicle Loan", "Secured Loans")
check("nature inherited",
      db.get_groups().set_index("name").loc["Vehicle Loan", "nature"], "liability")
db.add_ledger("Car Loan", "Vehicle Loan", 300_000, "Cr")
check("it lands on the liabilities side",
      float(acc.balance_sheet()["liabilities"].set_index("Particulars")
            .loc["Loans (Liability)", "Amount"]), 300_000.0)
check("and the sheet still balances",
      acc.balance_sheet()["totals"]["difference"], 0.0, tol=0.0)
try:
    db.delete_group("Vehicle Loan")
    check("deleting a group with ledgers", "accepted", "rejected")
except ValueError:
    check("deleting a group with ledgers", "rejected", "rejected")
try:
    db.delete_group("Fixed Assets")
    check("deleting a reserved group", "accepted", "rejected")
except ValueError:
    check("deleting a reserved group", "rejected", "rejected")

print("\nCompany master")
GOOD = {
    "company_name": "Aarkaya Handicrafts LLC",
    "mailing_name": "",
    "address1": "12 Mall Road",
    "address2": "Connaught Place",
    "address3": "",
    "city": "New Delhi",
    "state": "Delhi",
    "country": "India",
    "pincode": "110001",
    "phone": "011-5550123",
    "email": "books@aarkaya.example",
    "pan": "aaapz1234c",            # lower case on purpose
    "gstin": "09AAAPZ1234C1Z5",
    "tan": "dela12345b",
    "cin": "U12345DL2020PTC123456",
    "fy_start_month": "4",
    "books_begin": "2026-04-01",
    "currency": "₹",
}
stored = db.save_company(GOOD)
check("company details round-trip",
      db.get_company()["company_name"], GOOD["company_name"])
check("identifiers are stored upper case", stored["pan"], "AAAPZ1234C")
check("TAN upper-cased too", stored["tan"], "DELA12345B")
check("each address line is its own field",
      [db.get_company()[key] for key in db.ADDRESS_LINES],
      ["12 Mall Road", "Connaught Place", ""])
check("the city is kept apart from the lines",
      db.get_company()["city"], "New Delhi")
check("mailing name falls back to the company name",
      db.company_display_name(), GOOD["company_name"])
db.save_company(dict(GOOD, mailing_name="Aarkaya Handicrafts"))
check("a mailing name is preferred when set",
      db.company_display_name(), "Aarkaya Handicrafts")

# Blank is always allowed: these are details you fill in as you learn them.
bare = db.validate_company({"company_name": "X"})
check("optional fields may be blank", bare["pan"], "")
check("a blank currency falls back to the default",
      bare["currency"], db.DEFAULT_SETTINGS["currency"])
check("a blank financial year falls back to April",
      bare["fy_start_month"], "4")

# The field is labelled ZIP / PIN, so it has to take either shape.
for shape, value in (("an Indian PIN code", "110001"),
                     ("a US ZIP", "45202"),
                     ("a ZIP+4", "45202-1234")):
    check(f"accepts {shape}",
          db.validate_company(dict(GOOD, pincode=value))["pincode"], value)

# A value that IS given has to be well formed - a malformed GSTIN is worse than
# an absent one, because it reaches a filing looking legitimate.
REJECT = {
    "no name": dict(GOOD, company_name="   "),
    "malformed PAN": dict(GOOD, pan="ABC123", gstin=""),
    "malformed GSTIN": dict(GOOD, gstin="09AAAPZ1234C1X5"),
    "GSTIN not carrying the PAN": dict(GOOD, pan="BBBPZ1234C"),
    "malformed TAN": dict(GOOD, tan="XX1"),
    "malformed CIN": dict(GOOD, cin="NOPE"),
    "a four-digit postcode": dict(GOOD, pincode="1100"),
    "a seven-digit postcode": dict(GOOD, pincode="1100012"),
    "not an e-mail": dict(GOOD, email="books-at-aarkaya"),
    "month 13": dict(GOOD, fy_start_month="13"),
    "date the wrong way round": dict(GOOD, books_begin="01/04/2026"),
}
for label, values in REJECT.items():
    try:
        db.validate_company(values)
        check(f"rejects {label}", "accepted", "rejected")
    except ValueError:
        check(f"rejects {label}", "rejected", "rejected")

# Nothing is written when validation fails, or a half-valid save would land.
before = db.get_company()
try:
    db.save_company(dict(GOOD, pan="RUBBISH", state="Karnataka"))
except ValueError:
    pass
check("a rejected save writes nothing", db.get_company(), before)

print("\nWhere the data folder lives when packaged")
# The bug this guards (2026-10-16): APP_DIR came from __file__, and a
# PyInstaller onefile build unpacks into a TEMPORARY directory it deletes on
# exit - so the packaged app wrote its books somewhere that ceased to exist
# when it closed. A company created in the exe, with journals entered, was
# gone on the next launch. Invisible from source, where __file__ is correct,
# so it needs a test that pretends to be frozen.
_real_frozen = getattr(sys, "frozen", None)
_real_executable = sys.executable
try:
    sys.frozen = True
    sys.executable = str(Path(tempfile.gettempdir()) / "somewhere"
                         / "BookkeeperPlus.exe")
    frozen_dir = db._app_dir()
    check("packaged, the anchor is the executable's folder",
          frozen_dir, Path(sys.executable).resolve().parent)
    check("and NOT the unpacked module directory",
          frozen_dir == Path(db.__file__).resolve().parent, False)
finally:
    sys.executable = _real_executable
    if _real_frozen is None:
        del sys.frozen
    else:
        sys.frozen = _real_frozen
check("from source, the anchor is the module's folder",
      db._app_dir(), Path(db.__file__).resolve().parent)

print("\nMore than one company")
# Each company is its own file, so the test is whether the books really are
# separate - a ledger in one must not be visible in the other. Both are created
# inside BOOKS_DIR; the database BOOKS_DB pins for this suite sits outside it
# and is deliberately not a listed company.
pinned = db.DB_PATH
first = db.create_company({"company_name": "First Company"})
shutil.copyfile(first, SANDBOX / "books.backup-20261014-204341.db")

second = db.create_company({"company_name": "Second Company"})
check("creating a company switches to it", db.DB_PATH, second)
check("the new company has its own name",
      db.get_company()["company_name"], "Second Company")
db.add_ledger("Only In Second", "Indirect Expenses")

db.use_company(first)
check("the first company is unchanged",
      db.get_company()["company_name"], "First Company")
check("a ledger created in one is not in the other",
      "Only In Second" in list(db.get_ledgers()["name"]), False)
db.use_company(second)
check("and it IS in its own company",
      "Only In Second" in list(db.get_ledgers()["name"]), True)

names = [c["company_name"] for c in db.list_companies()]
check("both companies are listed", sorted(set(names)),
      ["First Company", "Second Company"])
check("a backup file is not listed as a company",
      any("backup" in c["file"] for c in db.list_companies()), False)

# Two companies may legitimately share a name; the files must not collide.
third = db.create_company({"company_name": "Second Company"})
check("a repeated name gets its own file", third != second, True)
check("and it is readable", db.get_company()["company_name"], "Second Company")

db.remember_last_company(first)
check("the last company opened is remembered",
      db.last_company().name, Path(first).name)

before = sorted(p.name for p in db.company_files())
try:
    db.create_company({"company_name": "   "})
    check("a company with no name is refused", "accepted", "rejected")
except ValueError:
    check("a company with no name is refused", "rejected", "rejected")
check("and no file was left behind",
      sorted(p.name for p in db.company_files()), before)

check("peeking reads the name without opening it",
      db.peek_company(first)["company_name"], "First Company")
db.use_company(pinned)

print("\nBackups")

import sqlite3 as _sqlite3  # noqa: E402  (only this section needs it)

before = db.backups_for()
first_backup = db.backup_company(tag="manual")
check("a backup is written", first_backup is not None and first_backup.exists(),
      True)
check("it is named for the company it came from",
      first_backup.name.startswith(Path(db.DB_PATH).stem + "-"), True)
check("it lands in the backup folder, not beside the books",
      first_backup.parent, db.backup_dir())
check("and it is listed for that company",
      first_backup in db.backups_for(), True)

# The point of a backup is that it opens. A file copy taken mid-write passes
# every check except this one.
conn = _sqlite3.connect(f"file:{first_backup}?mode=ro", uri=True)
check("the backup passes SQLite's own integrity check",
      conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
check("it holds the same vouchers as the live books",
      conn.execute("SELECT COUNT(*) FROM vouchers").fetchone()[0],
      len(db.get_vouchers()))
conn.close()

# Opening the app repeatedly without entering anything must not push the
# useful copies out with identical ones.
check("an unchanged book is not copied again",
      db.backup_company(tag="close"), None)
check("so the count is unchanged", len(db.backups_for()), len(before) + 1)

post("Journal", 20, "something to change the books",
     ("Cash", 1.0, 0), ("Sales", 0, 1.0))
second = db.backup_company(tag="close")
check("a changed book IS copied again", second is not None, True)
check("and the newest is the one just written", db.backups_for()[0], second)

# The dedup above only works because each backup carries the hash of the books
# it came from. SQLite's backup rebuilds the file page by page, so the copy is
# logically identical but not byte-identical and its own hash says nothing.
check("the backup records the hash of the books it came from",
      db.backup_source_digest(second), db._digest(db.DB_PATH))
check("that is NOT the hash of the backup file itself",
      db.backup_source_digest(second) == db._digest(second), False)
conn = _sqlite3.connect(f"file:{second}?mode=ro", uri=True)
stamped = dict(conn.execute("SELECT key, value FROM settings").fetchall())
conn.close()
check("and says which file and when", stamped.get(db.ORIGIN_KEY),
      Path(db.DB_PATH).name)
check("the time it was taken is recorded",
      stamped.get(db.TAKEN_KEY, "").startswith(str(date.today())), True)
check("a file that is not one of ours is handled, not crashed on",
      db.backup_source_digest(db.DATA_DIR / "nothing-here.db"), "")

# A backup must never be mistaken for a company. The folder guard is what
# does it here - the name-based guard only covers the older flat files.
check("the backup folder is not listed as a company",
      any("backups" in c["file"] for c in db.list_companies()), False)
check("nor are the files inside it",
      [c for c in db.list_companies()
       if Path(c["file"]).stem.startswith(Path(db.DB_PATH).stem + "-")], [])

# Pruning keeps the newest and must not reach into another company's.
other = db.create_company({"company_name": "Backup Neighbour"})
db.backup_company(other, tag="manual")
mine = db.DATA_DIR / (Path(db.DB_PATH).stem + ".db")
db.use_company(pinned)
for _ in range(4):
    post("Journal", 21, "more", ("Cash", 1.0, 0), ("Sales", 0, 1.0))
    db.backup_company(tag="close")
kept = db.prune_backups(keep=2)
check("pruning drops the older copies", len(db.backups_for()), 2)
check("it reports what it dropped", len(kept) > 0, True)
check("the neighbour's backup is untouched", len(db.backups_for(other)), 1)
check("and the newest copy survived pruning",
      db.backups_for()[0].exists(), True)

print("\nFinancial years")

# The arithmetic, worked by hand. An April year running 2026-27 covers
# 01/04/2026 to 31/03/2027, and 31/03/2026 belongs to the year BEFORE it.
db.set_setting("fy_start_month", "4")
check("an April year opens on 1 April",
      acc.fy_bounds(date(2026, 7, 15)), (date(2026, 4, 1), date(2027, 3, 31)))
check("a date in March belongs to the year that opened last April",
      acc.fy_bounds(date(2026, 3, 31)), (date(2025, 4, 1), date(2026, 3, 31)))
check("the first day is in its own year",
      acc.fy_bounds(date(2026, 4, 1)), (date(2026, 4, 1), date(2027, 3, 31)))
check("and the last day too",
      acc.fy_bounds(date(2027, 3, 31)), (date(2026, 4, 1), date(2027, 3, 31)))
check("an April year is labelled across two calendar years",
      acc.fy_label(date(2026, 4, 1)), "2026-2027")

# Books that run the calendar year are a different shape, not a special case
# of the same one: the year ends in December of its OWN year, and
# "2026-2026" would be nonsense as a label.
db.set_setting("fy_start_month", "1")
check("a January year is the calendar year",
      acc.fy_bounds(date(2026, 7, 15)), (date(2026, 1, 1), date(2026, 12, 31)))
check("and is labelled with the one year", acc.fy_label(date(2026, 1, 1)),
      "2026")

# A leap day must not shift the year's boundaries.
check("February 29 falls in the year containing it",
      acc.fy_bounds(date(2028, 2, 29)), (date(2028, 1, 1), date(2028, 12, 31)))

# Garbage in the setting must not stop the reports opening.
db.set_setting("fy_start_month", "13")
check("an impossible start month falls back to April",
      acc.fy_start_month(), 4)
db.set_setting("fy_start_month", "not a month")
check("so does a start month that is not a number",
      acc.fy_start_month(), 4)

db.set_setting("fy_start_month", "4")
years = acc.financial_years()
labels = [y.label for y in years]
# These books hold vouchers dated April 2026 and begin 01/04/2026, so the
# 2026-2027 year is earned by the data; 2025-2026 is offered anyway, because
# comparing with last year is routine.
check("the year holding the vouchers is listed", "2026-2027" in labels, True)
check("last year is offered even with nothing posted in it",
      "2025-2026" in labels, True)
check("the years are in order, earliest first", labels, sorted(labels))
check("every year starts the day after the last one ends",
      all(years[i + 1].start - years[i].end == timedelta(days=1)
          for i in range(len(years) - 1)), True)
check("no year is listed twice", len(labels), len(set(labels)))
check("today is inside one of them",
      any(y.contains(date.today()) for y in years), True)
check("and that is the one reported as current",
      acc.current_financial_year().contains(date.today()), True)

# A voucher dated years back must pull its year into the list, or there would
# be no way to report on it.
db.save_voucher(
    voucher_type="Journal", voucher_date=date(2023, 5, 10),
    narration="An old entry",
    lines=[{"ledger_id": db.ledger_id("Cash"), "debit": 10.0, "credit": 0.0},
           {"ledger_id": db.ledger_id("Sales"), "debit": 0.0, "credit": 10.0}])
labels = [y.label for y in acc.financial_years()]
check("an old voucher brings its year into the list",
      "2023-2024" in labels, True)
check("and the years between it and now are unbroken",
      labels, ["2023-2024", "2024-2025", "2025-2026", "2026-2027"])

print("\n18) Voiding a voucher: it stops counting but keeps its number")
# A fresh pair of ledgers, so these checks do not have to reason about
# everything posted above.
db.add_ledger("Void Test Expense", "Indirect Expenses")
db.add_ledger("Void Test Bank", "Bank Accounts")
before_gap = acc.trial_balance()["difference"]
net_before = acc.net_profit()
vid = db.save_voucher(
    voucher_type="Payment", voucher_date=date(2026, 4, 20),
    narration="To be voided",
    lines=[{"ledger_id": db.ledger_id("Void Test Expense"), "debit": 7_500,
            "credit": 0},
           {"ledger_id": db.ledger_id("Void Test Bank"), "debit": 0,
            "credit": 7_500}])
numbered = int(db.get_voucher(vid)[0]["number"])
balances = acc.balances().set_index("ledger")
check("the voucher posts while it is live",
      float(balances.loc["Void Test Expense", "closing"]), 7_500.0)
next_before = db.next_voucher_number("Payment")

db.void_voucher(vid)
check("it reads back as voided", db.is_voided(vid), True)
balances = acc.balances().set_index("ledger")
check("the expense is back to nil",
      float(balances.loc["Void Test Expense", "closing"]), 0.0)
check("and so is the bank",
      float(balances.loc["Void Test Bank", "closing"]), 0.0)
check("it is gone from the lines the reports read",
      vid in set(db.get_lines()["voucher_id"]), False)
check("but the lines are still THERE, not erased",
      vid in set(db.get_lines(include_voided=True)["voucher_id"]), True)
check("the trial balance is undisturbed",
      acc.trial_balance()["difference"], before_gap, tol=0.0)
check("the balance sheet still balances",
      acc.balance_sheet()["totals"]["difference"], 0.0, tol=0.0)
check("it is off the face of the P&L",
      "Void Test Expense" in
      set(acc.profit_and_loss()["indirect_expense"]["Particulars"]), False)
check("and the net profit is exactly what it was before the voucher",
      acc.net_profit(), net_before, tol=0.0)

# The point of voiding rather than deleting: the number stays spent.
check("its number is NOT handed to the next voucher",
      db.next_voucher_number("Payment"), next_before)
book = acc.day_book()
row = book[book["voucher_id"] == vid]
check("it is still listed in the Day Book", len(row), 1)
check("holding its number", int(row.iloc[0]["Vch No"]), numbered)
check("marked as voided", bool(row.iloc[0]["voided"]), True)
check("and showing nil, not what it used to be",
      float(row.iloc[0]["Debit"]), 0.0)
check("the Day Book's own total ignores it",
      float(book["Debit"].sum()), float(book[~book["voided"]]["Debit"].sum()))

print("\n19) A voided voucher is frozen until it is restored")
try:
    db.save_voucher(voucher_id=vid, voucher_type="Payment",
                    voucher_date=date(2026, 4, 20), narration="sneaky edit",
                    lines=[{"ledger_id": db.ledger_id("Void Test Expense"),
                            "debit": 1, "credit": 0},
                           {"ledger_id": db.ledger_id("Void Test Bank"),
                            "debit": 0, "credit": 1}])
    check("editing a voided voucher is refused", "allowed", "refused")
except ValueError as exc:
    check("editing a voided voucher is refused", "refused", "refused")
    check("and says to restore it first", "restore" in str(exc).lower(), True)
check("so its lines are untouched",
      float(db.get_lines(include_voided=True)
            .query("voucher_id == @vid")["debit"].sum()), 7_500.0)

db.restore_voucher(vid)
check("restoring brings it back", db.is_voided(vid), False)
check("with its amounts intact",
      float(acc.balances().set_index("ledger")
            .loc["Void Test Expense", "closing"]), 7_500.0)
check("and the books still balance",
      acc.trial_balance()["difference"], before_gap, tol=0.0)

print("\n20) An older book gains what it was made without")
# Books created before voiding existed have no `voided` column, and
# CREATE TABLE IF NOT EXISTS will not add one. Simulate that exactly.
legacy = Path(tempfile.gettempdir()) / "bkplus_legacy.db"
legacy.unlink(missing_ok=True)
import sqlite3  # noqa: E402
from contextlib import closing  # noqa: E402

# `with sqlite3.connect(...)` opens a TRANSACTION, it does not close the
# connection - so without closing() the file stays locked and Windows
# refuses to delete it at the end.
def legacy_sql(*statements):
    with closing(sqlite3.connect(legacy)) as old, old:
        return [old.execute(sql).fetchall() for sql in statements]

with closing(sqlite3.connect(legacy)) as old, old:
    old.executescript(db.SCHEMA)
legacy_sql("ALTER TABLE vouchers DROP COLUMN voided",
           "ALTER TABLE vouchers DROP COLUMN voided_at",
           "INSERT INTO vouchers (voucher_type, number, voucher_date,"
           " narration, created_at) VALUES ('Journal', 1, '2026-04-01',"
           " 'legacy', '2026-04-01')",
           # ...and it holds its address the old way, in one multi-line box.
           "INSERT INTO settings (key, value) VALUES"
           " ('company_name', 'Old Books'),"
           " ('address', '12 Mall Road\nConnaught Place\nNew Delhi')")
columns = {r[1] for r in legacy_sql("PRAGMA table_info(vouchers)")[0]}
check("the legacy book really lacks it", "voided" in columns, False)

kept = db.DB_PATH
db.use_company(legacy)
db.init_db()
columns, rows = legacy_sql("PRAGMA table_info(vouchers)",
                           "SELECT voided FROM vouchers")
columns = {r[1] for r in columns}
check("opening it adds the column", "voided" in columns, True)
check("and every existing voucher is live, not voided",
      [r[0] for r in rows], [0])
# A settings key dropped from COMPANY_FIELDS stays in the FILE; it just stops
# being read. So the old address has to be moved, or it is invisible but
# present - which looks exactly like the app lost it.
settings = dict(legacy_sql("SELECT key, value FROM settings")[0])
check("the old address was split across the lines",
      [settings.get(key, "") for key in db.ADDRESS_LINES],
      ["12 Mall Road", "Connaught Place", "New Delhi"])
check("so the Company screen shows it",
      db.get_company()["address1"], "12 Mall Road")
check("the old key is gone", "address" in settings, False)
check("but its text is kept", settings.get("address_legacy", ""),
      "12 Mall Road\nConnaught Place\nNew Delhi")

check("init_db can be run again safely", db.init_db(), None)
# The move must not run twice: clearing a line then reopening must not refill
# it from the legacy text, or an edit could never be saved.
db.save_company(dict(db.get_company(), address2=""))
db.init_db()
check("and does not undo a later edit", db.get_company()["address2"], "")

# Anything past the third line is folded in, never dropped.
check("a four-line address keeps every word",
      db._split_address("A\nB\nC\nD"), ["A", "B", "C, D"])
check("blank lines are not lines", db._split_address("A\n\n \nB"), ["A", "B"])
db.use_company(kept)
legacy.unlink(missing_ok=True)

print("\n21) Bill-wise receivables and payables")
from datetime import timedelta as _td

check("a debtor keeps bills by default",
      db.defaults_to_bill_wise("Sundry Debtors"), True)
check("so does a creditor",
      db.defaults_to_bill_wise("Sundry Creditors"), True)
check("a bank account does not", db.defaults_to_bill_wise("Bank Accounts"), False)
db.add_group("Export Customers", "Sundry Debtors")
check("and so does a SUB-group of a party group, not only the group itself",
      db.defaults_to_bill_wise("Export Customers"), True)

db.add_ledger("Acme Ltd", "Sundry Debtors")
db.add_ledger("Supplier Co", "Sundry Creditors")
check("a new ledger under a party group comes out bill-wise",
      bool(db.get_ledgers().set_index("name").loc["Acme Ltd", "bill_wise"]), True)
check("but Cash does not",
      bool(db.get_ledgers().set_index("name").loc["Cash", "bill_wise"]), False)

TODAY = date.today()


def invoice(ref, amount, days_ago, credit_days):
    when = TODAY - _td(days=days_ago)
    return db.save_voucher(
        voucher_type="Sales", voucher_date=when, narration=ref,
        lines=[{"ledger_id": db.ledger_id("Acme Ltd"), "debit": amount,
                "credit": 0,
                "bills": [{"kind": "new", "bill_ref": ref, "signed": amount,
                           "due_date": (when + _td(days=credit_days)).isoformat()}]},
               {"ledger_id": db.ledger_id("Sales"), "debit": 0,
                "credit": amount}])


sales_before = len(db.get_vouchers(voucher_type="Sales"))
loose_before = acc.outstanding()["totals"]["on_account"]
gap_before = acc.trial_balance()["difference"]


def refused(what, **kwargs):
    """Save must be rejected AND write nothing. Returns the message."""
    try:
        db.save_voucher(**kwargs)
        check(what, "allowed", "refused")
        return ""
    except ValueError as exc:
        check(what, "refused", "refused")
        return str(exc)


# --- the hard rule: a bill-wise line must be fully allocated ------------- #
# Unallocated money is ALLOWED - demanding otherwise would be a bookkeeping
# practice enforced as arithmetic, and it broke every voucher already posted
# to a party. The remainder is materialised as an explicit on-account
# allocation, so nothing is unaccounted for and the editor can show it.
loose = db.save_voucher(
    voucher_type="Sales", voucher_date=TODAY, narration="no refs",
    lines=[{"ledger_id": db.ledger_id("Acme Ltd"), "debit": 1000, "credit": 0},
           {"ledger_id": db.ledger_id("Sales"), "debit": 0, "credit": 1000}])
kept = db.get_voucher(loose)[1][0]["bills"]
check("a bill-wise line with nothing allocated still saves", len(kept), 1)
check("with the whole amount explicitly ON ACCOUNT", kept[0]["kind"],
      "on_account")
check("and it warns rather than refusing",
      any("not against any bill" in n for n in db.voucher_type_warnings(
          "Sales",
          [{"ledger_id": db.ledger_id("Acme Ltd"), "debit": 1000, "credit": 0}],
          db.get_ledgers())), True)

part = db.save_voucher(
    voucher_type="Sales", voucher_date=TODAY, narration="short",
    lines=[{"ledger_id": db.ledger_id("Acme Ltd"), "debit": 1000, "credit": 0,
            "bills": [{"kind": "new", "bill_ref": "PART", "signed": 600}]},
           {"ledger_id": db.ledger_id("Sales"), "debit": 0, "credit": 1000}])
kept = db.get_voucher(part)[1][0]["bills"]
check("allocating LESS than the line saves too", len(kept), 2)
check("the named bill keeping its own amount",
      float(kept[0]["signed"]), 600.0)
check("and the shortfall landing on account", float(kept[1]["signed"]), 400.0)
db.delete_voucher(loose)
db.delete_voucher(part)

msg = refused(
    "allocating MORE than the line is refused",
    voucher_type="Sales", voucher_date=TODAY, narration="over",
    lines=[{"ledger_id": db.ledger_id("Acme Ltd"), "debit": 1000, "credit": 0,
            "bills": [{"kind": "new", "bill_ref": "OVER", "signed": 1400}]},
           {"ledger_id": db.ledger_id("Sales"), "debit": 0, "credit": 1000}])
check("saying by how much", "400.00 more than" in msg, True)

msg = refused(
    "an allocation running the opposite way to its line is refused",
    voucher_type="Sales", voucher_date=TODAY, narration="backwards",
    lines=[{"ledger_id": db.ledger_id("Acme Ltd"), "debit": 1000, "credit": 0,
            "bills": [{"kind": "new", "bill_ref": "BACK", "signed": -1000}]},
           {"ledger_id": db.ledger_id("Sales"), "debit": 0, "credit": 1000}])
check("because a receipt must never increase what is owed",
      "opposite way" in msg, True)

msg = refused(
    "a bill reference on a ledger that does not keep bills is refused",
    voucher_type="Receipt", voucher_date=TODAY, narration="stray ref",
    lines=[{"ledger_id": db.ledger_id("Cash"), "debit": 500, "credit": 0,
            "bills": [{"kind": "new", "bill_ref": "NOPE", "signed": 500}]},
           {"ledger_id": db.ledger_id("Sales"), "debit": 0, "credit": 500}])
check("so a reference cannot sit unseen on a cash line",
      "does not keep bills" in msg, True)

msg = refused(
    "an allocation with no reference is refused",
    voucher_type="Sales", voucher_date=TODAY, narration="nameless",
    lines=[{"ledger_id": db.ledger_id("Acme Ltd"), "debit": 900, "credit": 0,
            "bills": [{"kind": "new", "bill_ref": "", "signed": 900}]},
           {"ledger_id": db.ledger_id("Sales"), "debit": 0, "credit": 900}])
check("since it could never be settled afterwards",
      "needs a bill reference" in msg, True)
check("and not one of those refusals wrote a voucher",
      len(db.get_vouchers(voucher_type="Sales")), sales_before)

# --- now a real set of bills -------------------------------------------- #
invoice("INV-1", 100_000, 100, 30)        # long overdue
invoice("INV-2", 50_000, 45, 30)          # overdue
invoice("INV-3", 25_000, 10, 60)          # not due yet
db.save_voucher(
    voucher_type="Receipt", voucher_date=TODAY, narration="cheque",
    lines=[{"ledger_id": db.ledger_id("Cash"), "debit": 90_000, "credit": 0},
           {"ledger_id": db.ledger_id("Acme Ltd"), "debit": 0, "credit": 90_000,
            "bills": [{"kind": "against", "bill_ref": "INV-1", "signed": -40_000},
                      {"kind": "against", "bill_ref": "INV-2", "signed": -50_000}]}])
when = TODAY - _td(days=70)
db.save_voucher(
    voucher_type="Purchase", voucher_date=when, narration="BILL-9",
    lines=[{"ledger_id": db.ledger_id("Purchases"), "debit": 30_000, "credit": 0},
           {"ledger_id": db.ledger_id("Supplier Co"), "debit": 0,
            "credit": 30_000,
            "bills": [{"kind": "new", "bill_ref": "BILL-9", "signed": -30_000,
                       "due_date": (when + _td(days=15)).isoformat()}]}])

out = acc.outstanding()
by_ref = out["bills"].set_index("Ref")
check("a part-paid bill shows what is LEFT, not what it was",
      float(by_ref.loc["INV-1", "Pending"]), 60_000.0)
check("while still reporting the amount it was raised for",
      float(by_ref.loc["INV-1", "Amount"]), 100_000.0)
check("a bill settled in full stops being a bill",
      "INV-2" in by_ref.index, False)
check("receivables total", out["totals"]["receivable"], 85_000.0)
check("payables total", out["totals"]["payable"], 30_000.0)

# THE check that makes bill-wise trustworthy. If these ever disagree the
# bills are fiction: a party would owe one amount on the balance sheet and a
# different amount on the outstanding report.
def reconciles(party, result=None):
    """A party's ledger balance, less their bills and on-account money.

    Must be exactly zero. If it is not, the bills are fiction: the party
    would owe one amount on the balance sheet and another on the outstanding
    report, and nobody could tell which was right.
    """
    result = result or acc.outstanding()
    balance = float(acc.balances().set_index("ledger").loc[party, "closing"])
    pending = float(result["bills"].query("Party == @party")["Signed"].sum())
    loose = float(result["on_account"].query("Party == @party")["Pending"].sum())
    return round(balance - pending - loose, 2)


for party in ("Acme Ltd", "Supplier Co"):
    check(f"{party}: bills and on-account money reconcile to the ledger",
          reconciles(party, out), 0.0, tol=0.0)

# --- ageing, on both bases ---------------------------------------------- #
check("a bill not yet due is not overdue at all",
      str(by_ref.loc["INV-3", "Bucket"]), "Not due")
check("and its age is negative, being days still to run",
      int(by_ref.loc["INV-3", "Age"]) < 0, True)
check("aged by due date, INV-1 is 70 days late",
      int(by_ref.loc["INV-1", "Age"]), 70)
older = acc.outstanding(age_by="bill date")["bills"].set_index("Ref")
check("aged by bill date it is 100 days old instead",
      int(older.loc["INV-1", "Age"]), 100)
check("which puts it in a different bucket",
      str(older.loc["INV-1", "Bucket"]), "Over 90 days")
check("and INV-3 is merely recent rather than 'not due'",
      str(older.loc["INV-3", "Bucket"]), "0-30 days")
check("the basis changes how bills are GROUPED, never what is owed",
      acc.outstanding(age_by="bill date")["totals"]["receivable"],
      out["totals"]["receivable"], tol=0.0)
try:
    acc.outstanding(age_by="whenever")
    check("an unknown ageing basis is refused", "allowed", "refused")
except ValueError:
    check("an unknown ageing basis is refused", "refused", "refused")

check("the ageing summary adds up to the receivables total",
      round(float(out["ageing"]["Receivable"].sum()), 2),
      out["totals"]["receivable"], tol=0.0)
check("and to the payables total",
      round(float(out["ageing"]["Payable"].sum()), 2),
      out["totals"]["payable"], tol=0.0)

# --- a bill on a voided voucher is not money anyone owes ----------------- #
vid = int(db.get_vouchers(voucher_type="Sales")["id"].max())
ref_voided = db.get_voucher(vid)[1][0]["bills"][0]["bill_ref"]
db.void_voucher(vid)
check("voiding an invoice takes its bill off the outstanding report",
      ref_voided in set(acc.outstanding()["bills"]["Ref"]), False)
check("and the party still reconciles to the ledger",
      reconciles("Acme Ltd"), 0.0, tol=0.0)
db.restore_voucher(vid)
check("restoring brings the bill back",
      ref_voided in set(acc.outstanding()["bills"]["Ref"]), True)

# --- allocations survive a round trip through the editor ----------------- #
receipt = int(db.get_vouchers(voucher_type="Receipt")["id"].max())
head, lines = db.get_voucher(receipt)
party_line = next(line for line in lines if line["bills"])
check("a saved voucher reads its allocations back", len(party_line["bills"]), 2)
check("under the same key save_voucher takes",
      sorted(b["bill_ref"] for b in party_line["bills"]), ["INV-1", "INV-2"])
db.save_voucher(voucher_id=receipt, voucher_type=head["voucher_type"],
                voucher_date=head["voucher_date"],
                narration="cheque (re-saved)", lines=lines)
check("and re-saving it unchanged leaves the bills exactly as they were",
      float(acc.outstanding()["bills"].set_index("Ref").loc["INV-1", "Pending"]),
      60_000.0)
check("with no duplicate allocations left behind",
      len(db.get_voucher(receipt)[1][1]["bills"]), 2)

# --- money on account is not a bill ------------------------------------- #
db.save_voucher(
    voucher_type="Receipt", voucher_date=TODAY, narration="unidentified",
    lines=[{"ledger_id": db.ledger_id("Cash"), "debit": 5_000, "credit": 0},
           {"ledger_id": db.ledger_id("Acme Ltd"), "debit": 0, "credit": 5_000,
            "bills": [{"kind": "on_account", "bill_ref": "", "signed": -5_000}]}])
after = acc.outstanding()
check("on-account money raises no bill", len(after["bills"]), len(out["bills"]))
check("but it is reported rather than lost",
      round(after["totals"]["on_account"] - loose_before, 2), -5_000.0)
check("and the party reconciles with it included",
      reconciles("Acme Ltd", after), 0.0, tol=0.0)
check("and the trial balance is untouched by any of this",
      acc.trial_balance()["difference"], gap_before, tol=0.0)

# --- the picker's view -------------------------------------------------- #
picker = acc.open_bills(db.ledger_id("Acme Ltd"))
check("the picker offers only that party's bills",
      set(picker["Party"]), {"Acme Ltd"})
check("oldest first, the order they are usually settled in",
      list(picker["Date"]), sorted(picker["Date"]))

print("\n22) Party lines posted before bills existed are accounted for")
# Books written by an earlier version have party lines with NO allocations at
# all. Left alone, the outstanding report would show nothing while the
# balance sheet showed the party owing money - under-reporting in silence.
# Simulated by throwing the allocations away and clearing the marker.
with db.connect() as conn:
    conn.execute("DELETE FROM bill_allocations")
    conn.execute("DELETE FROM settings WHERE key = ?", (db.BACKFILL_KEY,))
check("with the allocations gone, the party has no bills at all",
      len(acc.outstanding()["bills"]), 0)
check("so the report and the ledger disagree - the thing to avoid",
      reconciles("Acme Ltd") != 0.0, True)

db.init_db()
check("opening the books backfills them", db.get_setting(db.BACKFILL_KEY), "1")
check("as on-account money, which is what it is",
      set(acc.outstanding()["on_account"]["Party"]) >= {"Acme Ltd"}, True)
check("and the party reconciles to the ledger again",
      reconciles("Acme Ltd"), 0.0, tol=0.0)
check("no bills are invented out of it", len(acc.outstanding()["bills"]), 0)

before = len(db.get_allocations())
db.init_db()
check("opening again backfills nothing a second time",
      len(db.get_allocations()), before)

print("\n23) GST: which tax applies, how much, and what it ties to")

check("a GSTIN opens with its state code",
      db.state_code_of("29AAAPZ1234C1Z5"), "29")
check("named", db.state_name("29"), "Karnataka")
check("rubbish is not a state code", db.state_code_of("hello"), "")
check("nor is a code that does not exist", db.state_code_of("99AAAPZ1234C1Z5"), "")

db.save_company({**db.get_company(), "company_name": "GST Probe",
                 "pan": "AAAPZ1234C", "gstin": "29AAAPZ1234C1Z5",
                 "state": "Kerala"})
# The GSTIN wins: a typed state name can disagree with it, and the tax
# follows the GSTIN.
check("our own state comes from our GSTIN, not the typed State field",
      db.company_state_code(), "29")

check("a supply inside our state is intra", db.supply_type("29"), "intra")
check("one to another state is inter", db.supply_type("27"), "inter")
check("an unknown place of supply is treated as intra, like a counter sale",
      db.supply_type(""), "intra")

# --- the split ---------------------------------------------------------- #
check("intra-state halves the rate into CGST and SGST",
      db.split_tax(10_000, 18.0, "intra"),
      {"cgst": 900.0, "sgst": 900.0, "igst": 0.0})
check("interstate puts the whole rate on IGST",
      db.split_tax(20_000, 18.0, "inter"),
      {"cgst": 0.0, "sgst": 0.0, "igst": 3600.0})
odd = db.split_tax(1_050, 18.0, "intra")
check("an amount that does not halve cleanly still adds back exactly",
      round(odd["cgst"] + odd["sgst"], 2), 189.0)
check("with the odd paisa on the second half, not lost",
      [odd["cgst"], odd["sgst"]], [94.5, 94.5])
third = db.split_tax(100.0, 5.0, "intra")
check("and a genuinely odd total splits without losing a paisa",
      round(third["cgst"] + third["sgst"], 2), 5.0)
check("nil-rated is a real rate, not an absence",
      db.split_tax(5_000, 0.0, "intra"),
      {"cgst": 0.0, "sgst": 0.0, "igst": 0.0})

# --- the tax ledgers ---------------------------------------------------- #
roles = db.gst_ledger_ids()
check("all six tax ledgers are seeded", sorted(roles), sorted(db.GST_ROLES))
ledgers = db.get_ledgers(include_inactive=True).set_index("name")
check("under Duties & Taxes",
      str(ledgers.loc["Output CGST", "group_name"]), "Duties & Taxes")
check("and reserved, so the report cannot lose them",
      bool(ledgers.loc["Output CGST", "is_reserved"]), True)
db.seed_gst_ledgers()
check("seeding again creates nothing", db.gst_ledger_ids(), roles)

check("a taxable ledger carries a rate",
      db.add_ledger("Sales 18%", "Sales Accounts", gst_rate=18.0) > 0, True)
db.add_ledger("Purchases 5%", "Purchase Accounts", gst_rate=5.0)
db.add_ledger("Exempt Sales", "Sales Accounts", gst_rate=0.0)
rates = db.get_ledgers().set_index("name")
check("0% is stored as a rate", float(rates.loc["Exempt Sales", "gst_rate"]), 0.0)
# The distinction the nullable column exists for.
check("while a ledger that is not taxable at all has no rate",
      rates.loc["Cash", "gst_rate"] is None
      or pd.isna(rates.loc["Cash", "gst_rate"]), True)

db.add_ledger("Local Co", "Sundry Debtors", gstin="29BBBPZ1234C1Z5",
              state_code="29")
db.add_ledger("Far Co", "Sundry Debtors", gstin="27CCCPZ1234C1Z5",
              state_code="27")
db.add_ledger("A Supplier", "Sundry Creditors", gstin="29DDDPZ1234C1Z5",
              state_code="29")


def gst_sale(party, taxable, rate, pos, ref, when=date(2026, 8, 10)):
    kind = db.supply_type(pos)
    tax = db.split_tax(taxable, rate, kind)
    total = round(taxable + tax["cgst"] + tax["sgst"] + tax["igst"], 2)
    lines = [{"ledger_id": db.ledger_id(party), "debit": total, "credit": 0,
              "bills": [{"kind": "new", "bill_ref": ref, "signed": total}]},
             {"ledger_id": db.ledger_id("Sales 18%"), "debit": 0,
              "credit": taxable}]
    for key, role in (("cgst", "output_cgst"), ("sgst", "output_sgst"),
                      ("igst", "output_igst")):
        if tax[key]:
            lines.append({"ledger_id": roles[role], "debit": 0,
                          "credit": tax[key]})
    return db.save_voucher(
        voucher_type="Sales", voucher_date=when, narration=ref, lines=lines,
        gst={"party_ledger_id": db.ledger_id(party),
             "party_gstin": str(db.get_ledgers().set_index("name")
                                .loc[party, "gstin"]),
             "place_of_supply": pos, "supply_type": kind, "rate": rate})


local = gst_sale("Local Co", 10_000, 18.0, "29", "GINV-1")
far = gst_sale("Far Co", 20_000, 18.0, "27", "GINV-2")
gst_sale("Local Co", 1_050, 18.0, "29", "GINV-3")

tax = db.split_tax(40_000, 5.0, "intra")
total = round(40_000 + tax["cgst"] + tax["sgst"], 2)
db.save_voucher(
    voucher_type="Purchase", voucher_date=date(2026, 8, 12), narration="GBILL-7",
    lines=[{"ledger_id": db.ledger_id("Purchases 5%"), "debit": 40_000,
            "credit": 0},
           {"ledger_id": roles["input_cgst"], "debit": tax["cgst"], "credit": 0},
           {"ledger_id": roles["input_sgst"], "debit": tax["sgst"], "credit": 0},
           {"ledger_id": db.ledger_id("A Supplier"), "debit": 0, "credit": total,
            "bills": [{"kind": "new", "bill_ref": "GBILL-7", "signed": -total}]}],
    gst={"party_ledger_id": db.ledger_id("A Supplier"),
         "party_gstin": "29DDDPZ1234C1Z5", "place_of_supply": "29",
         "supply_type": "intra", "rate": 5.0})

summary = db.get_setting  # keep the name short below
s = acc.gst_summary()
check("output tax across the three sales", s["totals"]["output_tax"], 5_589.0)
check("input tax on the purchase", s["totals"]["input_tax"], 2_000.0)
check("so this much is payable", s["totals"]["net"], 3_589.0)
check("outward taxable value", s["totals"]["outward_taxable"], 31_050.0)

# THE check: the summary is drawn from the lines, so it must equal the tax
# LEDGERS. If these disagree, one of the two is lying about the return.
held = acc.balances().set_index("ledger")


def ledger_tax(prefix):
    return round(sum(abs(float(held.loc[f"{prefix} {t}", "closing"]))
                     for t in ("CGST", "SGST", "IGST")), 2)


check("output tax equals the Output tax ledgers",
      s["totals"]["output_tax"], ledger_tax("Output"), tol=0.0)
check("input tax equals the Input tax ledgers",
      s["totals"]["input_tax"], ledger_tax("Input"), tol=0.0)
check("and the books still balance", acc.trial_balance()["difference"],
      gap_before, tol=0.0)

by_supply = s["by_supply"].set_index("Supply")
check("interstate sales are reported apart from local ones",
      float(by_supply.loc["Interstate", "Taxable"]), 20_000.0)
check("as is the tax on them",
      float(by_supply.loc["Interstate", "Tax"]), 3_600.0)
check("the rate breakdown only lists rates actually used",
      list(s["outward"]["Rate"]), ["18%"])
check("and the inward side separately", list(s["inward"]["Rate"]), ["5%"])

# --- the register -------------------------------------------------------- #
# Indexed by the voucher numbers actually assigned - earlier sections have
# already posted Sales vouchers, so these do not start at 1.
num_local = int(db.get_voucher(local)[0]["number"])
num_far = int(db.get_voucher(far)[0]["number"])
reg = acc.gst_register(outward=True).set_index("Vch No")
check("every sale is listed invoice by invoice", len(reg), 3)
check("with the place of supply spelled out",
      str(reg.loc[num_far, "Place of supply"]), "Maharashtra")
check("and named as interstate", str(reg.loc[num_far, "Supply"]), "Interstate")
check("the total is taxable plus tax",
      float(reg.loc[num_local, "Total"]), 11_800.0)
check("purchases are not on the outward register",
      "Purchase" in set(reg["Vch Type"]), False)
check("but they are on the inward one",
      set(acc.gst_register(outward=False)["Vch Type"]), {"Purchase"})

# Place of supply and the counterparty's GSTIN are a SNAPSHOT: a return
# filed for August must keep saying what it said then.
db.update_ledger(db.ledger_id("Far Co"), "Far Co", "Sundry Debtors", 0.0, "Dr",
                 gstin="19EEEPZ1234C1Z5", state_code="19")
again = acc.gst_register(outward=True).set_index("Vch No")
check("moving the customer does NOT rewrite a filed invoice",
      str(again.loc[num_far, "GSTIN"]), "27CCCPZ1234C1Z5")
check("nor where the supply was made",
      str(again.loc[num_far, "Place of supply"]), "Maharashtra")

# The amounts, by contrast, are derived - so a correction reports itself.
head, lines = db.get_voucher(local)
for line in lines:
    if line["ledger_name"] == "Sales 18%":
        line["credit"] = 12_000.0
    if line["ledger_name"] == "Local Co":
        line["debit"] = 14_160.0
        line["bills"] = [{"kind": "new", "bill_ref": "GINV-1",
                          "signed": 14_160.0}]
    if line["ledger_name"] in ("Output CGST", "Output SGST"):
        line["credit"] = 1_080.0
db.save_voucher(voucher_id=local, voucher_type="Sales",
                voucher_date=head["voucher_date"], narration="GINV-1 revised",
                lines=lines)
check("correcting a voucher re-reports its GST with nothing to re-run",
      acc.gst_summary()["totals"]["output_tax"], 5_949.0)
check("and the summary still equals the tax ledgers",
      acc.gst_summary()["totals"]["output_tax"],
      round(sum(abs(float(acc.balances().set_index("ledger")
                          .loc[f"Output {t}", "closing"]))
                for t in ("CGST", "SGST", "IGST")), 2), tol=0.0)

# --- a voided invoice is not a supply ----------------------------------- #
before_void = acc.gst_summary()["totals"]["output_tax"]
db.void_voucher(far)
check("voiding an invoice takes it off the GST return",
      far in set(acc.gst_register(outward=True)["voucher_id"]), False)
check("and its tax out of the output total",
      acc.gst_summary()["totals"]["output_tax"],
      round(before_void - 3_600.0, 2))
db.restore_voucher(far)
check("restoring puts it back",
      acc.gst_summary()["totals"]["output_tax"], before_void)

# --- taking the tax off a voucher must take it off the return ----------- #
head, lines = db.get_voucher(local)
kept = [line for line in lines
        if line["ledger_name"] not in ("Output CGST", "Output SGST")]
for line in kept:
    if line["ledger_name"] == "Local Co":
        line["debit"] = 12_000.0
        line["bills"] = [{"kind": "new", "bill_ref": "GINV-1",
                          "signed": 12_000.0}]
db.save_voucher(voucher_id=local, voucher_type="Sales",
                voucher_date=head["voucher_date"], narration="GINV-1 no tax",
                lines=kept)
check("a voucher stripped of its tax lines leaves the GST return",
      db.get_gst_details(local), None)
check("rather than sitting on it with no tax",
      local in set(acc.gst_register(outward=True)["voucher_id"]), False)

print("\n24) How dates are shown, and what is stored underneath")
# The ask (2026-10-20): "if the currency sign is $ the date format should be
# MM/DD/YYYY and if its Rupees ... DD/MM/YYYY or provide a seperate line for
# selecting date format". Built as a SETTING whose first value comes from the
# currency - so both halves hold, without the two being chained forever.
db.save_company(dict(GOOD, currency="₹", date_format=""))
check("rupee books start on DD/MM/YYYY",
      db.date_format().key, "DD/MM/YYYY")
db.save_company(dict(GOOD, currency="$", date_format=""))
check("dollar books start on MM/DD/YYYY",
      db.date_format().key, "MM/DD/YYYY")
check("and the derived value is SAVED, not re-derived every read",
      db.get_setting("date_format"), "MM/DD/YYYY")

# The point of it being its own field: a chosen format is not overruled.
db.save_company(dict(GOOD, currency="$", date_format="DD/MM/YYYY"))
check("a chosen format outranks the currency",
      db.date_format().key, "DD/MM/YYYY")
db.save_company(dict(GOOD, currency="€", date_format="DD/MM/YYYY"))
check("and changing the currency does not reorder the dates",
      db.date_format().key, "DD/MM/YYYY")

try:
    db.validate_company(dict(GOOD, date_format="YY.MM.DD"))
    check("a format the app cannot render is refused", "accepted", "rejected")
except ValueError as exc:
    check("a format the app cannot render is refused", "rejected", "rejected")
    check("and it lists the ones that work", "MM/DD/YYYY" in str(exc), True)

for name, fmt in (("DD/MM/YYYY", "DD/MM/YYYY"), ("MM/DD/YYYY", "MM/DD/YYYY")):
    db.save_company(dict(GOOD, date_format=fmt))
    check(f"an ISO string shown as {name}",
          db.show_date("2026-03-04"), "04/03/2026" if fmt[0] == "D"
          else "03/04/2026")
    check(f"a date object shown as {name}",
          db.show_date(date(2026, 3, 4)), "04/03/2026" if fmt[0] == "D"
          else "03/04/2026")
check("a blank is blank, not a crash", db.show_date(""), "")
check("and so is a missing value", db.show_date(None), "")
check("something that is not a date is passed through as written",
      db.show_date("Opening Balance"), "Opening Balance")

# THE INVARIANT: only the display changes. Nothing localised is ever stored,
# or the file would be ambiguous and the Date column would sort wrongly - and
# here the bills' own dates ride on the same rule.
db.save_company(dict(GOOD, date_format="MM/DD/YYYY"))
vid = post("Journal", 7, "stored ISO", ("Rent", 10, 0), ("Cash", 0, 10))
check("the voucher date is stored ISO whatever is displayed",
      db.get_voucher(vid)[0]["voucher_date"], "2026-04-07")
check("so the report frame carries ISO too",
      str(acc.day_book(date(2026, 4, 7), date(2026, 4, 7))["Date"].iloc[0]),
      "2026-04-07")


def _iso_or_blank(value) -> bool:
    """A cell that is an ISO date, or empty - a bill may have no due date."""
    text = str(value or "")
    if not text:
        return True
    try:
        date.fromisoformat(text)
        return True
    except ValueError:
        return False


# Both of the Outstanding report's date columns ride on the same rule, and
# `Due` is the one that would be missed: it is only a date on some rows.
_bills = acc.outstanding()["bills"]
check("the Outstanding report has its two date columns",
      [column for column in ("Date", "Due") if column in _bills.columns],
      ["Date", "Due"])
check("and carries ISO in both of them",
      [column for column in ("Date", "Due") if column in _bills.columns
       and not all(_iso_or_blank(v) for v in _bills[column])], [])
db.save_company(dict(GOOD, date_format="DD/MM/YYYY"))

print("\n" + ("ALL CHECKS PASSED" if not FAILURES else f"FAILURES: {FAILURES}"))
raise SystemExit(1 if FAILURES else 0)
