"""Covers the UNPINNED path: more than one company, and choosing between them.

Run:  python test_multi_company.py

The other three suites set BOOKS_DB, which pins the data file and deliberately
switches the company picker off - so they exercise everything except the way
the app actually starts for a user. This one runs with BOOKS_DB unset and
BOOKS_DIR pointed at a scratch folder, which is the real launch path.

Both front ends are driven: the Qt picker and create dialog, and the Streamlit
sidebar selector, including that switching really does change which books are
being read rather than only the label at the top.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Unset, so db.DB_PINNED is False and the pickers are live. BOOKS_DIR keeps
# every company file inside a scratch folder - without it this suite would
# create companies in the user's real data/ directory.
os.environ.pop("BOOKS_DB", None)
SANDBOX = Path(tempfile.gettempdir()) / "bkplus_multi_company"
shutil.rmtree(SANDBOX, ignore_errors=True)
SANDBOX.mkdir(parents=True)
os.environ["BOOKS_DIR"] = str(SANDBOX)

from PySide6 import QtWidgets  # noqa: E402

import db  # noqa: E402
import desktop  # noqa: E402

FAILURES = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(label)


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

# Closing a window - which is also how switching company works - offers a
# backup in a MODAL dialog, and there is nobody here to click it. Answer "no"
# throughout: this suite is about which books are open, and the backup offer
# has its own checks in test_desktop.py. Without this the suite hangs.
BACKUP_ANSWERS = []
desktop.MainWindow.ask_backup_on_close = (
    lambda self: BACKUP_ANSWERS.append(self.company_file.name) or "no")

print("\n1) The app opens WITHOUT being asked to create anything")
check("the data file is not pinned", db.DB_PINNED, False)
check("no companies to begin with", db.company_files(), [])

# The point of this section: a fresh install must open a usable window, not a
# create-company dialog (the user asked for exactly this 2026-10-16).
db.use_company(db.startup_company())
check("it falls back to the default file",
      Path(db.DB_PATH).name, "books.db")
first_window = desktop.MainWindow()
first_window.show()
check("a window opened", first_window.isHidden(), False)
check("on a working set of books", bool(db.get_ledgers().shape[0]), True)
check("and that default is now the only company",
      [p.name for p in db.company_files()], ["books.db"])

picker = desktop.CompanyPicker(first_window, current=first_window.company_file)
check("the dropdown offers the company that is open",
      picker.combo.count(), 1)
check("and marks it as open", "open now" in picker.detail.text(), True)
first_window.close()

print("\n2) Creating two companies")
made = {}
for name in ("Alpha Exports", "Beta Trading"):
    dialog = desktop.NewCompanyDialog()
    dialog.editors["company_name"].setText(name)
    dialog.editors["currency"].setCurrentText("₹")
    dialog._create()
    check(f"{name} created", dialog.created is not None, True)
    made[name] = Path(dialog.created)
    # A ledger that exists ONLY here, to prove the books are separate.
    db.add_ledger(f"{name.split()[0]} Only", "Indirect Expenses")
check("each has its own file",
      len({p.name for p in made.values()}), 2)
check("the file name follows the company name",
      made["Alpha Exports"].name, "alpha-exports.db")

print("\n3) The books really are separate")
db.use_company(made["Alpha Exports"])
alpha = list(db.get_ledgers()["name"])
db.use_company(made["Beta Trading"])
beta = list(db.get_ledgers()["name"])
check("Alpha has its own ledger", "Alpha Only" in alpha, True)
check("and not Beta's", "Beta Only" in alpha, False)
check("Beta has its own ledger", "Beta Only" in beta, True)
check("and not Alpha's", "Alpha Only" in beta, False)

print("\n4) The dropdown lists and preselects")
db.use_company(made["Alpha Exports"])
db.remember_last_company(made["Alpha Exports"])
picker = desktop.CompanyPicker(current=made["Alpha Exports"])
names = [picker.combo.itemText(i).split("  ")[0]
         for i in range(picker.combo.count())]
# Three, not two: the default company the app opened on in section 1 is a real
# company and belongs in the list.
check("all three are in the dropdown", sorted(names),
      ["Alpha Exports", "Beta Trading", "My Company"])
check("it starts on the company that is open",
      Path(picker.combo.currentData()).name, made["Alpha Exports"].name)
picker.combo.setCurrentIndex(
    next(i for i in range(picker.combo.count())
         if Path(picker.combo.itemData(i)).name == made["Beta Trading"].name))
check("the detail line follows the dropdown",
      made["Beta Trading"].name in picker.detail.text(), True)
picker._open()
check("opening returns that company's file",
      Path(picker.chosen).name, made["Beta Trading"].name)

print("\n5) Appearance is per company, so the dialog inherits the open one")
import theme  # noqa: E402

db.use_company(made["Alpha Exports"])
window = desktop.MainWindow()
window.set_color_scheme("classic")
window.set_text_size("largest")
stored = db.peek_settings(
    made["Alpha Exports"], (theme.SETTING_KEY, theme.FONT_SETTING_KEY))
check("the scheme was stored in that company's file",
      stored.get(theme.SETTING_KEY), "classic")
check("and so was the text size",
      stored.get(theme.FONT_SETTING_KEY), "largest")
# The dialog now opens over a running window, so it simply inherits the
# application palette - no need to dress it from the file beforehand.
picker = desktop.CompanyPicker(window, current=made["Alpha Exports"])
check("the dialog is in the running app's scheme",
      QtWidgets.QApplication.palette().color(
          QtWidgets.QApplication.palette().ColorRole.Window).name(),
      theme.SCHEMES["classic"].window)
window.set_color_scheme("light")
window.set_text_size(theme.DEFAULT_TEXT)
window.close()

print("\n6) Choosing a company from the open window")
db.use_company(made["Beta Trading"])
window = desktop.MainWindow()
check("switching is offered when the file is not pinned",
      window._switch_action.isEnabled(), True)
check("not switching to begin with", window.switch_requested, False)
check("the window knows which company it is showing",
      window.company_file.name, made["Beta Trading"].name)

# Choosing the company already open changes nothing.
window._open_company(made["Beta Trading"])
check("reopening the same company still just switches",
      window.switch_requested, True)
window.switch_requested = False

# Cancelling after a create must leave the data layer where the window is -
# create_company switches to the new file as a side effect.
db.use_company(made["Alpha Exports"])          # as a create would have
window._stay()
check("cancelling puts the data layer back",
      Path(db.DB_PATH).name, window.company_file.name)

window._open_company(made["Alpha Exports"])
check("choosing another company sets the flag main() loops on",
      window.switch_requested, True)
check("and points the data layer at it",
      Path(db.DB_PATH).name, made["Alpha Exports"].name)
check("and remembers it for next launch",
      db.last_company().name, made["Alpha Exports"].name)

# The backup offer on a switch must be about the books being LEFT, not the
# ones being opened: _open_company points the data layer at the new file
# before closing the window, so anything reading db.DB_PATH here would offer
# to back up the wrong company.
BACKUP_ANSWERS.clear()
window.close()
check("switching offers to back up the company being left",
      BACKUP_ANSWERS, [made["Beta Trading"].name])

print("\n7) A backup in the data folder is not a company")
shutil.copyfile(made["Alpha Exports"],
                SANDBOX / "alpha-exports.backup-20261016-120000.db")
listed = [c["file"] for c in db.list_companies()]
check("the backup is not offered", any("backup" in f for f in listed), False)
check("the three real companies still are", len(listed), 3)

print("\n8) The Streamlit sidebar switches books too")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = Path(__file__).with_name("app.py")
at = AppTest.from_file(str(APP), default_timeout=180).run()
assert not at.exception, at.exception
selector = next((s for s in at.selectbox if s.key == "company_file"), None)
check("a company selector is rendered", selector is not None, True)
check("it offers every company", len(list(selector.options)), 3)


def shown(app_test):
    """Everything the page prints, to see WHICH company is on screen."""
    parts = [m.value for m in app_test.markdown if isinstance(m.value, str)]
    parts += [t.value for t in app_test.title if isinstance(t.value, str)]
    parts += [h.value for h in app_test.header if isinstance(h.value, str)]
    return " ".join(parts)


at = selector.set_value(made["Alpha Exports"].name).run()
assert not at.exception, at.exception
page = shown(at)
check("picking Alpha shows Alpha", "Alpha Exports" in page, True)
check("and not Beta", "Beta Trading" in page, False)

selector = next(s for s in at.selectbox if s.key == "company_file")
at = selector.set_value(made["Beta Trading"].name).run()
assert not at.exception, at.exception
page = shown(at)
check("picking Beta shows Beta", "Beta Trading" in page, True)
check("and not Alpha", "Alpha Exports" in page, False)

print("\n9) Nothing escaped the sandbox")
check("every company file is in the scratch folder",
      all(str(p).startswith(str(SANDBOX)) for p in db.company_files()), True)

print("\n" + ("ALL MULTI-COMPANY CHECKS PASSED" if not FAILURES
             else f"FAILURES: {FAILURES}"))
raise SystemExit(1 if FAILURES else 0)
