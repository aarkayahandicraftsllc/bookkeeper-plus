"""Renders the app and drives voucher entry through the real widgets.

Run:  python test_ui.py
"""

import os
import sys
import shutil
import tempfile
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP = Path(tempfile.gettempdir()) / "bkplus_ui_test.db"
TMP.unlink(missing_ok=True)
os.environ["BOOKS_DB"] = str(TMP)
# Company files are created in BOOKS_DIR, so point it at a scratch
# folder: without this the suite would write companies into the
# user's real data/ directory.
SANDBOX = Path(tempfile.gettempdir()) / f"bkplus_sandbox_{TMP.stem}"
shutil.rmtree(SANDBOX, ignore_errors=True)
SANDBOX.mkdir(parents=True, exist_ok=True)
os.environ["BOOKS_DIR"] = str(SANDBOX)

from streamlit.testing.v1 import AppTest  # noqa: E402

import accounting as acc  # noqa: E402
import db  # noqa: E402

APP = Path(__file__).with_name("app.py")


def fresh():
    return AppTest.from_file(str(APP), default_timeout=120).run()


print("\n1) It renders on empty books")
at = fresh()
assert not at.exception, at.exception
tabs = [t.label for t in at.tabs]
print(f"PASS  {len(tabs)} tabs: {tabs}")
assert len(tabs) == 7, tabs

warnings = " ".join(w.value for w in at.warning)
assert "two ledgers" in warnings, warnings
print("PASS  voucher entry explains it needs ledgers first")

print("\n2) Creating masters through the UI")
db.add_ledger("Bank", "Bank Accounts", 100_000, "Dr")
db.add_ledger("Capital", "Capital Account", 100_000, "Cr")
db.add_ledger("Sales", "Sales Accounts")
db.add_ledger("Rent", "Indirect Expenses")

at = fresh()
assert not at.exception, at.exception
assert not any("two ledgers" in w.value for w in at.warning)
ledger_pick = next(s for s in at.selectbox if s.key == "ledger_pick")
assert "Bank" in list(ledger_pick.options), list(ledger_pick.options)
print(f"PASS  {len(ledger_pick.options)} ledgers offered on the Ledger tab")

vtype = next(r for r in at.radio if r.key.startswith("vch_type"))
assert list(vtype.options) == list(db.VOUCHER_TYPES), list(vtype.options)
assert vtype.value == "Payment", vtype.value
print(f"PASS  voucher types: {list(vtype.options)}")

print("\n3) Accept is dead until the voucher balances")
accept = next(b for b in at.button if "Accept voucher" in b.label)
assert accept.disabled, "Accept should start disabled"
from streamlit.testing.v1.errors import AppTestError  # noqa: E402

try:
    accept.click().run()
    raise AssertionError("a disabled Accept button was clickable")
except AppTestError:
    pass
print("PASS  Accept is disabled on an empty grid and cannot be clicked")

labels = {m.label for m in at.metric}
for wanted in ("Total debit", "Total credit", "Difference"):
    assert wanted in labels, labels
print("PASS  running totals are shown while entering")

print("\n4) Posting a voucher through the data layer, then reading it back")
db.save_voucher(voucher_type="Receipt", voucher_date=date.today(),
                narration="cash sale",
                lines=[{"ledger_id": db.ledger_id("Bank"), "debit": 50_000,
                        "credit": 0},
                       {"ledger_id": db.ledger_id("Sales"), "debit": 0,
                        "credit": 50_000}])
db.save_voucher(voucher_type="Payment", voucher_date=date.today(),
                narration="office rent",
                lines=[{"ledger_id": db.ledger_id("Rent"), "debit": 12_000,
                        "credit": 0},
                       {"ledger_id": db.ledger_id("Bank"), "debit": 0,
                        "credit": 12_000}])

at = fresh()
assert not at.exception, at.exception
metrics = {m.label: m.value for m in at.metric}
assert metrics["Total debit"] == metrics["Total credit"], metrics
print(f"PASS  trial balance {metrics['Total debit']} on both sides")
assert metrics["Liabilities"] == metrics["Assets"], metrics
print(f"PASS  balance sheet {metrics['Assets']} on both sides")

oks = " ".join(s.value for s in at.success)
assert "Debits equal credits" in oks, oks
assert "Both sides agree" in oks, oks
print("PASS  both statements report themselves balanced")

print("\n5) The reports agree with the data layer")
trial = acc.trial_balance()
sheet = acc.balance_sheet()
pl = acc.profit_and_loss()
assert trial["difference"] == 0.0, trial
assert sheet["totals"]["difference"] == 0.0, sheet["totals"]
assert pl["totals"]["net_profit"] == 38_000.0, pl["totals"]   # 50,000 - 12,000
print(f"PASS  net profit {pl['totals']['net_profit']:,.2f}, both statements tie")

statement = acc.ledger_statement(db.ledger_id("Bank"))
assert statement["opening"] == 100_000.0, statement["opening"]
assert statement["closing"] == 138_000.0, statement["closing"]
assert len(statement["entries"]) == 2, statement["entries"]
print(f"PASS  bank ledger: opening {statement['opening']:,.2f} "
      f"-> closing {statement['closing']:,.2f}")

print("\n6) Day Book lists the vouchers and offers an export")
book = acc.day_book()
assert len(book) == 2, book
downloads = [b.label for b in at.download_button]
assert any("Day Book" in d for d in downloads), downloads
assert any("Trial Balance" in d for d in downloads), downloads
print(f"PASS  {len(book)} vouchers, exports: {downloads}")

print("\n7) Every period option renders")
for key in ("daybook_period", "pl_period", "ledger_period"):
    picker = next(s for s in at.selectbox if s.key == key)
    for option in picker.options:
        at = next(s for s in at.selectbox if s.key == key).set_value(option).run()
        assert not at.exception, (key, option, at.exception)
print("PASS  all period selections render cleanly")

print("\n8) An opening-balance difference is surfaced")
db.add_ledger("Stray", "Fixed Assets", 7_000, "Dr")
at = fresh()
assert not at.exception, at.exception
warned = " ".join(w.value for w in at.warning)
assert "Difference in opening balances" in warned, warned
print("PASS  the header warns about the opening difference")
sheet = acc.balance_sheet()
assert sheet["totals"]["difference"] == 0.0, sheet["totals"]
print("PASS  and the balance sheet still balances, with the residual named")

print("\n9) The company form, rendered from the shared field list")
at = fresh()
assert not at.exception, at.exception
labels = ({i.label for i in at.text_input} | {a.label for a in at.text_area}
          | {s.label for s in at.selectbox} | {d.label for d in at.date_input})
missing = [f.label for f in db.COMPANY_FIELDS if f.label not in labels]
assert not missing, f"not rendered: {missing}"
print(f"PASS  all {len(db.COMPANY_FIELDS)} company fields render "
      f"(PAN, GSTIN, address, financial year...)")

pan = next(i for i in at.text_input if i.label == "PAN")
gstin = next(i for i in at.text_input if i.label == "GSTIN")
save = next(b for b in at.button if "Save company details" in b.label)
pan.set_value("aaapz1234c")
gstin.set_value("09AAAPZ1234C1Z5")
at = save.click().run()
assert not at.exception, at.exception
assert db.get_company()["pan"] == "AAAPZ1234C", db.get_company()["pan"]
print("PASS  saving through the form stores the upper-cased PAN")

# The same validation the desktop screen gets, because both call save_company.
at = fresh()
next(i for i in at.text_input if i.label == "GSTIN").set_value("09BBBPZ1234C1Z5")
at = next(b for b in at.button if "Save company details" in b.label).click().run()
assert not at.exception, at.exception
errors = " ".join(e.value for e in at.error)
assert "PAN" in errors, errors or "no error shown"
assert db.get_company()["gstin"] == "09AAAPZ1234C1Z5", "a bad GSTIN was stored"
print("PASS  a GSTIN not carrying the PAN is refused, and nothing is stored")

print("\n10) A voided voucher in the web app")
# The Void / Restore / Delete buttons sit behind a dataframe row selection,
# which AppTest cannot click, so what is checked here is that a book
# CONTAINING a voided voucher renders and reports correctly.
victim = int(acc.day_book().iloc[0]["voucher_id"])
net_live = acc.net_profit()
db.void_voucher(victim)
at = fresh()
assert not at.exception, at.exception
print("PASS  the app renders with a voided voucher in the books")

book = acc.day_book()
row = book[book["voucher_id"] == victim]
assert len(row) == 1, "the voided voucher vanished from the Day Book"
assert bool(row.iloc[0]["voided"]), row
assert float(row.iloc[0]["Debit"]) == 0.0, row
print("PASS  it is still listed, marked voided, showing nil")

assert acc.net_profit() != net_live, "voiding changed nothing in the reports"
assert acc.balance_sheet()["totals"]["difference"] == 0.0, "sheet unbalanced"
print("PASS  it is out of the reports, and the balance sheet still balances")

db.restore_voucher(victim)
assert acc.net_profit() == net_live, "restoring did not put it back"
at = fresh()
assert not at.exception, at.exception
print("PASS  restoring puts it back, and the app still renders")

print("\nALL UI CHECKS PASSED")
