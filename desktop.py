"""Tally-style bookkeeping — native desktop app (PySide6/Qt).

    python desktop.py

This is a second front end over the same books. ``db.py`` and ``accounting.py``
are shared with the Streamlit version untouched, and both read the same
``data/books.db``, so you can use whichever suits the moment.

Why a desktop build exists at all: voucher entry wants a grid that responds to
every keystroke without a round trip, and a key that switches voucher type
without reaching for the mouse. Keyboard map:

    Alt+Shift+P/R/C/J/S/U  Payment, Receipt, Contra, Journal, Sales, Purchase
    Alt+L             create a ledger from the line you are on
    Ctrl+A            accept the voucher
    Ctrl+1..8         jump between screens (8 = Company details)
    Ctrl+S            save the company details, on that screen
    F2                set the voucher date
    Esc               back to voucher entry
    Ctrl+Shift+1..5   colour scheme (also under Settings > Colour scheme)
    Ctrl+= / Ctrl+-   text bigger / smaller (Settings > Text size)

Colours and text sizes live in ``theme.py``, never inline here: both are
switchable at runtime, so a hardcoded ``#666`` would be unreadable the moment
someone picks a dark scheme, and a hardcoded point size would ignore the text
setting. Build fonts with ``mono_font()`` / ``heading()``, not ``QFont(...)``.
"""

from __future__ import annotations

import calendar
import csv
import html
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from PySide6 import QtCore, QtGui, QtWidgets

import accounting as acc
import db
import theme

MONO = "Consolas, 'Courier New', monospace"

# Alt+Shift+<initial> picks the voucher type.
#
# Alt+SHIFT, not plain Alt, because the menu bar owns the plain Alt letters
# (&Go &Voucher &Report &Company &Settings) and a menu-bar mnemonic beats a
# shortcut - Receipt, Contra and Sales would all have opened a menu instead,
# the same silent dead key that Alt+C was. Adding Shift sidesteps the menus
# entirely, so every initial stays available.
#
# Purchase is the one exception to "the initial": Payment has P, so Purchase
# takes the U of p-U-rchase.
VOUCHER_KEYS = {
    "Alt+Shift+P": "Payment", "Alt+Shift+R": "Receipt",
    "Alt+Shift+C": "Contra", "Alt+Shift+J": "Journal",
    "Alt+Shift+S": "Sales", "Alt+Shift+U": "Purchase",
}


def money(value: float) -> str:
    symbol = db.get_setting("currency", "₹")
    sign = "-" if value < 0 else ""
    return f"{sign}{symbol}{abs(value):,.2f}"


def to_qdate(value) -> QtCore.QDate:
    if isinstance(value, str):
        value = date.fromisoformat(value)
    return QtCore.QDate(value.year, value.month, value.day)


def from_qdate(value: QtCore.QDate) -> date:
    return date(value.year(), value.month(), value.day())


def parse_amount(text: str) -> float:
    """Read an amount typed with or without separators. Blank means zero."""
    cleaned = (text or "").strip().replace(",", "")
    if not cleaned:
        return 0.0
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------- #
# a read-only table built from a DataFrame
# --------------------------------------------------------------------------- #
class FrameTable(QtWidgets.QTableWidget):
    """Displays a DataFrame. Numeric columns are right-aligned and formatted."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.setSortingEnabled(False)

    def load(self, frame: pd.DataFrame, numeric: tuple[str, ...] = (),
             bold_last: bool = False) -> None:
        frame = frame.reset_index(drop=True)
        self.clear()
        self.setRowCount(len(frame))
        self.setColumnCount(len(frame.columns))
        self.setHorizontalHeaderLabels([str(c) for c in frame.columns])

        for row in range(len(frame)):
            for col, column in enumerate(frame.columns):
                raw = frame.iloc[row, col]
                if column in numeric:
                    number = 0.0 if pd.isna(raw) else float(raw)
                    text = "" if abs(number) < 0.005 else f"{number:,.2f}"
                    item = QtWidgets.QTableWidgetItem(text)
                    item.setTextAlignment(QtCore.Qt.AlignRight
                                          | QtCore.Qt.AlignVCenter)
                    item.setFont(mono_font())
                else:
                    item = QtWidgets.QTableWidgetItem(
                        "" if pd.isna(raw) else str(raw))
                if bold_last and row == len(frame) - 1:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self.setItem(row, col, item)

        header = self.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        if len(frame.columns):
            widest = max(range(len(frame.columns)),
                         key=lambda i: 1 if frame.columns[i] in numeric else 2)
            header.setSectionResizeMode(widest, QtWidgets.QHeaderView.Stretch)


def mono_font() -> QtGui.QFont:
    """A monospaced font AT THE CURRENT TEXT SIZE.

    ``QFont(MONO)`` does not inherit the application font's size - it comes
    back at a system default of 12pt - so building one without passing a size
    leaves the amount columns ignoring the text-size setting, and looking
    smaller than their surroundings once the body text grows.
    """
    return QtGui.QFont(MONO, theme.point_size())


def apply_mono(widget) -> None:
    """Give a widget the monospaced font and mark it, so a later text-size
    change can find it again."""
    widget.setFont(mono_font())
    widget.setProperty("mono", True)


def fit_lines(widget) -> None:
    """Size a multi-line box in LINES of the current font, not pixels.

    Marked with a ``lines`` property so a text-size change can re-measure it;
    a pixel height would clip the text at the larger sizes.

    Measured against ``theme.point_size()`` rather than ``widget.font()``:
    ``app.setFont()`` propagates through a POSTED event, so a widget asked
    straight afterwards still reports its old font and the box would be
    measured with stale metrics - which is exactly what left it unchanged.
    """
    rows = int(widget.property("lines") or 3)
    font = widget.font()
    font.setPointSize(theme.point_size())
    metrics = QtGui.QFontMetrics(font)
    widget.setMinimumHeight(metrics.lineSpacing() * rows + 12)


def editor_for(field) -> QtWidgets.QWidget:
    """The right editor for a ``db.CompanyField``.

    Shared by the Company screen and the create-company dialog, so the two
    present the same field the same way.
    """
    if field.kind == "multiline":
        editor = QtWidgets.QPlainTextEdit()
        editor.setPlaceholderText(field.hint)
        editor.setProperty("lines", 3)
        fit_lines(editor)
        return editor
    if field.kind == "month":
        editor = QtWidgets.QComboBox()
        for month in range(1, 13):
            editor.addItem(calendar.month_name[month], month)
        return editor
    if field.kind == "date":
        editor = QtWidgets.QDateEdit()
        editor.setCalendarPopup(True)
        editor.setDisplayFormat("dd/MM/yyyy")
        return editor
    if field.kind == "choice":
        editor = QtWidgets.QComboBox()
        editor.setEditable(True)            # so an unlisted symbol still works
        editor.addItems(list(field.choices))
        return editor
    editor = QtWidgets.QLineEdit()
    editor.setPlaceholderText(field.hint)
    return editor


def flowing(label: QtWidgets.QLabel) -> QtWidgets.QLabel:
    """A label that WRAPS rather than pinning the window open.

    An unwrapped label's minimum width is the width of its entire text, and a
    window can never be narrower than its widest unwrapped label. The report
    captions carry long sentences and formatted figures, so left unwrapped they
    forced the window wider at every step of text size - one of them was
    demanding 1770px on its own. Use this for anything whose text is set at
    refresh and could run long.
    """
    label.setWordWrap(True)
    return label


def heading(text: str, size: int | None = None) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    font = label.font()
    font.setPointSize(size if size else theme.heading_point_size())
    font.setBold(True)
    label.setFont(font)
    # Marked so a text-size change can resize it: the point size is set here,
    # once, which means it would otherwise never follow the application font.
    label.setProperty("heading", True)
    return label


def hint_text(label: QtWidgets.QLabel) -> QtWidgets.QLabel:
    """The voucher-type description: one centred line, a step above body text.

    Deliberately NOT flowing(): it is a short sentence meant to be read in one
    glance, so it is kept on a single line. That is only safe because it is
    short - the longest is 53 characters, which asks for far less width than
    the voucher grid beside it, so it cannot become the thing pinning the
    window open. Check `test_desktop.py`'s width ceiling before lengthening
    any of `db.VOUCHER_TYPES`.
    """
    label.setWordWrap(False)
    label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    font = label.font()
    font.setPointSize(theme.hint_point_size())
    label.setFont(font)
    # Marked so a text-size change resizes it, the same reason headings are:
    # a point size set once here would never follow the application font.
    label.setProperty("hint", True)
    return label


def muted(label: QtWidgets.QLabel) -> QtWidgets.QLabel:
    """Guidance text, coloured by the active scheme.

    Styled by object name rather than inline, because these labels are set up
    once in a constructor and never written to again - the stylesheet repaints
    them when the scheme changes, with nothing to re-run.
    """
    label.setObjectName("muted")
    return label


def tone(label: QtWidgets.QLabel, token: str) -> None:
    """Colour a status label by MEANING - good, bad, warn, muted.

    The token is also stored on the widget, so ``MainWindow._repaint_tones``
    can recolour it after a scheme change without recomputing the report that
    produced the message.
    """
    label.setProperty("tone", token)
    label.setStyleSheet(theme.fg(token))


# --------------------------------------------------------------------------- #
# taking a report off the screen: print, PDF, spreadsheet
# --------------------------------------------------------------------------- #
# A report nobody can get out of the window is half a report - it has to reach
# an auditor, a bank or an email. Each screen describes itself as a Report and
# the three things below (preview, print, export) work off that description
# alone, so a new report gets all of them by writing one method.
@dataclass
class ReportSection:
    """One table in a report, with the columns that hold money."""

    heading: str
    frame: pd.DataFrame
    numeric: tuple[str, ...] = ()


@dataclass
class Report:
    """A whole report, as it would be handed to someone."""

    title: str
    period: str                       # "as at 31/03/2027", "01/04 to 31/03"
    sections: list[ReportSection]
    notes: list[str] = field(default_factory=list)

    def filename(self, suffix: str) -> str:
        stem = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")
        return f"{stem}-{date.today():%Y%m%d}{suffix}"


def period_caption(bar: "PeriodBar") -> str:
    """How a report's period reads on paper."""
    start, end = bar.range()
    if start is None:
        return f"as at {end:%d/%m/%Y}"
    return f"{start:%d/%m/%Y} to {end:%d/%m/%Y}"


def _for_display(section: ReportSection) -> pd.DataFrame:
    """The section with its money columns written the way the screen shows
    them - grouped thousands, two decimals, and blank rather than 0.00."""
    frame = section.frame.copy()
    for column in section.numeric:
        if column not in frame.columns:
            continue
        shown = []
        for value in frame[column]:
            if pd.isna(value):
                shown.append("")
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                # Already presentation text, not a number: the ledger's
                # Balance column carries Tally's "88,000.00 Dr". It belongs in
                # `numeric` so it is right-aligned with the other figures, and
                # it is passed through exactly as written.
                shown.append(str(value))
                continue
            shown.append("" if abs(number) < 0.005 else f"{number:,.2f}")
        frame[column] = shown
    return frame


def report_html(report: Report, company: str | None = None) -> str:
    """The report as a printable page.

    Figures are FORMATTED here, unlike the spreadsheet export which keeps them
    as numbers: this is the copy somebody reads, and the other is the copy
    somebody computes with.
    """
    who = company if company is not None else db.company_display_name()
    # Qt's rich-text engine understands only a subset of CSS, and cell padding
    # is not in it: styled with `td { padding }` alone the columns print
    # touching each other, so "Capital Account" and "100,000.00" ran together.
    # Spacing comes from the table's cellpadding ATTRIBUTE instead.
    # Colours are stated outright for the same reason they are not taken from
    # the scheme: this goes on white paper whatever the screen looks like, and
    # a dark scheme's pale text would print nearly invisible.
    parts = [
        "<html><head><meta charset='utf-8'><style>",
        "body { font-family: sans-serif; color: #000000; }",
        "h1 { font-size: 15pt; margin: 0 0 2pt 0; color: #000000; }",
        "h2 { font-size: 11pt; margin: 12pt 0 3pt 0; color: #000000; }",
        ".who { font-size: 11pt; font-weight: bold; }",
        ".period { margin-bottom: 8pt; }",
        ".note { font-size: 9pt; margin-top: 8pt; }",
        "</style></head><body>",
        f"<div class='who'>{html.escape(who)}</div>",
        f"<h1>{html.escape(report.title)}</h1>",
        f"<div class='period'>{html.escape(report.period)}</div>",
    ]
    for section in report.sections:
        if section.heading:
            parts.append(f"<h2>{html.escape(section.heading)}</h2>")
        frame = _for_display(section)
        if frame.empty:
            parts.append("<p><i>Nothing to show.</i></p>")
            continue
        # Built by hand rather than with to_html, which gives no hook for
        # per-column alignment - and a column of figures that is not
        # right-aligned is the difference between a report and a dump.
        right = {column for column in section.numeric
                 if column in frame.columns}

        def cell(tag: str, column, value) -> str:
            side = "right" if column in right else "left"
            shown = "" if pd.isna(value) else html.escape(str(value))
            return f"<{tag} style='text-align: {side}'>{shown}</{tag}>"

        head = "".join(cell("th", c, c) for c in frame.columns)
        body = "".join(
            "<tr>" + "".join(cell("td", c, v)
                             for c, v in zip(frame.columns, row)) + "</tr>"
            for row in frame.itertuples(index=False))
        parts.append(
            "<table width='100%' cellpadding='4' cellspacing='0' border='1'"
            f" style='border-collapse: collapse'><tr>{head}</tr>"
            f"{body}</table>")
    for note in report.notes:
        if note:
            parts.append(f"<div class='note'>{html.escape(note)}</div>")
    parts.append("</body></html>")
    return "\n".join(parts)


def _document(report: Report) -> QtGui.QTextDocument:
    doc = QtGui.QTextDocument()
    doc.setHtml(report_html(report))
    return doc


def preview_report(report: Report, parent=None) -> None:
    """Show the report as it will print, with printing available from there.

    A preview rather than a bare print dialog: these are wide tables, and
    whether one fits the page is the first thing anybody wants to know.
    """
    from PySide6 import QtPrintSupport

    printer = QtPrintSupport.QPrinter(
        QtPrintSupport.QPrinter.PrinterMode.HighResolution)
    printer.setDocName(report.filename(""))
    dialog = QtPrintSupport.QPrintPreviewDialog(printer, parent)
    dialog.setWindowTitle(f"{report.title} — print preview")
    dialog.paintRequested.connect(lambda target: _document(report).print_(target))
    dialog.resize(900, 700)
    dialog.exec()
    dialog.deleteLater()


def downloads_dir() -> Path:
    """Where a browser would put a file - which is where people look for one.

    Asked of the platform rather than assumed, because the folder is localised
    and can be moved; the guess and then the home folder are only there so a
    machine that reports nothing still gets somewhere writable.
    """
    location = QtCore.QStandardPaths.writableLocation(
        QtCore.QStandardPaths.StandardLocation.DownloadLocation)
    if location and Path(location).is_dir():
        return Path(location)
    guess = Path.home() / "Downloads"
    return guess if guess.is_dir() else Path.home()


def export_and_open(report: Report, parent=None) -> Path | None:
    """Write the report, then open it in whatever handles the file.

    Opening is most of the point: an export you then have to go and find is a
    job in itself, and the file is offered to Downloads precisely because that
    is where you would look for it.

    Separate from ``export_report`` so that writing a file and launching an
    application stay apart - the tests, and anything scripted, want the first
    without the second.
    """
    try:
        written = export_report(report, parent)
    except Exception as exc:                          # noqa: BLE001
        QtWidgets.QMessageBox.warning(
            parent, "Could not export",
            f"Nothing was written:\n\n{exc}\n\nIf the file is already open in "
            f"Excel, close it and try again.")
        return None
    if written is None:
        return None

    opened = QtGui.QDesktopServices.openUrl(
        QtCore.QUrl.fromLocalFile(str(written)))
    window = parent.window() if parent is not None else None
    if isinstance(window, QtWidgets.QMainWindow):
        # Said either way: a file written but not opened still has to be
        # findable, so the path goes up even when launching failed.
        window.statusBar().showMessage(
            f"Exported to {written}" if opened else
            f"Exported to {written} — could not open it automatically", 10000)
    return written


def export_report(report: Report, parent=None,
                  path: str | None = None) -> Path | None:
    """Write the report to a spreadsheet or a CSV. Returns the file written.

    Figures stay NUMBERS here rather than formatted text, so the file can be
    summed and pivoted on arrival - a column of "1,234.00" strings is a
    spreadsheet you have to repair before you can use it.
    """
    if path is None:
        suggested = str(downloads_dir() / report.filename(".xlsx"))
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            parent, f"Export {report.title}", suggested,
            "Excel workbook (*.xlsx);;CSV file (*.csv)")
        if not path:
            return None
    target = Path(path)
    if target.suffix.lower() == ".csv":
        _export_csv(report, target)
    else:
        _export_excel(report, target)
    return target


def _export_csv(report: Report, target: Path) -> None:
    """One flat file: the sections stacked, each under its own heading.

    A CSV holds one table, so a report with four of them has to be laid out
    rather than simply dumped - blank lines and headings are what let a reader
    tell where the trading account ends and the indirect one begins.
    """
    lines = [[report.title], [db.company_display_name()], [report.period], []]
    with open(target, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerows(lines)
        for section in report.sections:
            if section.heading:
                writer.writerow([section.heading])
            if section.frame.empty:
                writer.writerow(["(nothing to show)"])
            else:
                writer.writerow(list(section.frame.columns))
                for row in section.frame.itertuples(index=False):
                    writer.writerow(["" if pd.isna(v) else v for v in row])
            writer.writerow([])
        for note in report.notes:
            if note:
                writer.writerow([note])


def _export_excel(report: Report, target: Path) -> None:
    """A sheet per section, each headed with who, what and when.

    Sheet names are trimmed to Excel's 31 characters and stripped of the
    characters it refuses, because a long section heading would otherwise make
    the whole write fail at the last step.
    """
    with pd.ExcelWriter(target, engine="openpyxl") as writer:
        for index, section in enumerate(report.sections):
            name = re.sub(r"[\[\]:*?/\\]", " ",
                          section.heading or report.title)[:31].strip()
            name = name or f"Sheet{index + 1}"
            frame = section.frame
            frame.to_excel(writer, sheet_name=name, index=False, startrow=4)
            sheet = writer.sheets[name]
            sheet["A1"] = db.company_display_name()
            sheet["A2"] = report.title
            sheet["A3"] = report.period
            if section.heading:
                sheet["A4"] = section.heading
            # Enough width to read a ledger name or a formatted figure.
            for column, header in enumerate(frame.columns, start=1):
                width = max(len(str(header)) + 2,
                            *(len(str(v)) + 2 for v in frame[header])) \
                    if len(frame) else len(str(header)) + 2
                sheet.column_dimensions[
                    sheet.cell(row=5, column=column).column_letter].width = \
                    min(40, max(10, width))
            if report.notes:
                start = 6 + len(frame)
                for offset, note in enumerate(report.notes):
                    sheet.cell(row=start + offset, column=1, value=note)


class ReportActions(QtWidgets.QWidget):
    """The Print and Export buttons, for a screen that can describe itself.

    Takes a callable rather than a Report: the report is whatever is on screen
    at the moment the button is pressed, and capturing it at build time would
    export the empty one the screen started with.
    """

    def __init__(self, source, parent=None):
        super().__init__(parent)
        self.source = source
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        self.print_button = QtWidgets.QPushButton("Print…  (Ctrl+P)")
        self.print_button.setToolTip("Preview and print, or save as PDF from "
                                     "the printer list")
        self.print_button.clicked.connect(self.print_report)
        self.export_button = QtWidgets.QPushButton("Export…  (Ctrl+E)")
        self.export_button.setToolTip("Write this report to a spreadsheet "
                                      "or a CSV file")
        self.export_button.clicked.connect(self.export)
        row.addWidget(self.print_button)
        row.addWidget(self.export_button)

    def print_report(self) -> None:
        preview_report(self.source(), self)

    def export(self) -> None:
        export_and_open(self.source(), self)


def totals_bar(*pairs: tuple[str, str]) -> QtWidgets.QWidget:
    """A right-aligned row of label/value pairs, for report footers."""
    box = QtWidgets.QWidget()
    layout = QtWidgets.QHBoxLayout(box)
    layout.setContentsMargins(0, 4, 0, 4)
    layout.addStretch(1)
    for label, value in pairs:
        cell = QtWidgets.QLabel(f"{label}  <b>{value}</b>")
        apply_mono(cell)
        layout.addWidget(cell)
        layout.addSpacing(18)
    return box


# --------------------------------------------------------------------------- #
# create-master dialogs, reachable mid-voucher without losing the voucher
# --------------------------------------------------------------------------- #
class GroupDialog(QtWidgets.QDialog):
    """Create a sub-group without leaving whatever you were doing."""

    def __init__(self, parent=None, under: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Create Group")
        self.created: str | None = None

        form = QtWidgets.QFormLayout(self)
        self.name = QtWidgets.QLineEdit()
        self.name.setPlaceholderText("e.g. Vehicle Loan")
        form.addRow("Name", self.name)

        self.parent_box = QtWidgets.QComboBox()
        self.parent_box.setEditable(True)
        self.parent_box.completer().setFilterMode(QtCore.Qt.MatchContains)
        names = list(db.get_groups()["name"])
        self.parent_box.addItems(names)
        if under in names:
            self.parent_box.setCurrentText(under)
        form.addRow("Under", self.parent_box)

        note = QtWidgets.QLabel(
            "A sub-group inherits its parent's nature, so it lands on the same "
            "statement.")
        note.setWordWrap(True)
        muted(note)
        form.addRow(note)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._create)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.name.setFocus()

    def _create(self) -> None:
        try:
            db.add_group(self.name.text(), self.parent_box.currentText())
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "Cannot create group", str(exc))
            return
        self.created = self.name.text().strip()
        self.accept()


class LedgerDialog(QtWidgets.QDialog):
    """Create a ledger mid-voucher, including its group if that is missing too."""

    def __init__(self, parent=None, name: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Create Ledger")
        self.created: str | None = None

        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.name = QtWidgets.QLineEdit(name)
        self.name.setPlaceholderText("e.g. Bank of India")
        form.addRow("Name", self.name)

        # built before _load_groups(), which describes the chosen group into it
        self.note = QtWidgets.QLabel()
        self.note.setWordWrap(True)
        muted(self.note)

        under_row = QtWidgets.QHBoxLayout()
        self.group_box = QtWidgets.QComboBox()
        self.group_box.setEditable(True)
        self.group_box.completer().setFilterMode(QtCore.Qt.MatchContains)
        self._load_groups()
        under_row.addWidget(self.group_box, 1)
        new_group = QtWidgets.QPushButton("New group…")
        new_group.clicked.connect(self._new_group)
        under_row.addWidget(new_group)
        under_holder = QtWidgets.QWidget()
        under_holder.setLayout(under_row)
        form.addRow("Under", under_holder)

        opening_row = QtWidgets.QHBoxLayout()
        self.opening = QtWidgets.QDoubleSpinBox()
        self.opening.setRange(0, 1e12)
        self.opening.setDecimals(2)
        self.opening.setGroupSeparatorShown(True)
        self.side = QtWidgets.QComboBox()
        self.side.addItems(list(db.SIDES))
        opening_row.addWidget(self.opening, 1)
        opening_row.addWidget(self.side)
        opening_holder = QtWidgets.QWidget()
        opening_holder.setLayout(opening_row)
        form.addRow("Opening balance", opening_holder)
        layout.addLayout(form)

        layout.addWidget(self.note)
        self.group_box.currentTextChanged.connect(self._describe)
        self._describe()

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._create)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        (self.group_box if name else self.name).setFocus()

    def _load_groups(self, select: str | None = None) -> None:
        current = select or self.group_box.currentText()
        names = list(db.get_groups()["name"])
        self.group_box.blockSignals(True)
        self.group_box.clear()
        self.group_box.addItems(names)
        if current in names:
            self.group_box.setCurrentText(current)
        self.group_box.blockSignals(False)
        self._describe()

    def _new_group(self) -> None:
        dialog = GroupDialog(self, under=self.group_box.currentText())
        if dialog.exec() and dialog.created:
            self._load_groups(select=dialog.created)

    def _describe(self) -> None:
        """Say in plain words where this ledger's balance will show up."""
        groups = db.get_groups().set_index("name")
        chosen = self.group_box.currentText()
        if chosen not in groups.index:
            self.note.setText("")
            return
        nature = str(groups.loc[chosen, "nature"])
        trading = bool(groups.loc[chosen, "affects_gross"])
        where = {
            "asset": "the assets side of the Balance Sheet",
            "liability": "the liabilities side of the Balance Sheet",
            "income": ("the trading account" if trading
                       else "the indirect incomes"),
            "expense": ("the trading account" if trading
                        else "the indirect expenses"),
        }[nature]
        self.note.setText(f"A {nature} account — its balance will appear in {where}.")

    def _create(self) -> None:
        try:
            db.add_ledger(self.name.text(), self.group_box.currentText(),
                          self.opening.value(), self.side.currentText())
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "Cannot create ledger", str(exc))
            return
        self.created = self.name.text().strip()
        self.accept()


# --------------------------------------------------------------------------- #
# voucher entry
# --------------------------------------------------------------------------- #
class VoucherScreen(QtWidgets.QWidget):
    """The screen you live in: type, date, number, then a Dr/Cr grid."""

    posted = QtCore.Signal()
    masters_changed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.editing_id: int | None = None
        self.editing_voided = False
        layout = QtWidgets.QVBoxLayout(self)

        self.title = heading("Voucher Entry")
        layout.addWidget(self.title)

        # --- header row ---
        head = QtWidgets.QHBoxLayout()
        self.type_box = QtWidgets.QComboBox()
        self.type_box.addItems(list(db.VOUCHER_TYPES))
        self.type_box.currentTextChanged.connect(self._type_changed)
        head.addWidget(QtWidgets.QLabel("Type"))
        head.addWidget(self.type_box)

        self.date_edit = QtWidgets.QDateEdit(to_qdate(date.today()))
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("dd/MM/yyyy")
        head.addWidget(QtWidgets.QLabel("Date"))
        head.addWidget(self.date_edit)

        self.number_label = QtWidgets.QLabel()
        apply_mono(self.number_label)
        head.addWidget(self.number_label)
        head.addStretch(1)
        layout.addLayout(head)

        # Its own row, so it can be centred across the screen rather than
        # sitting squeezed at the right-hand end of the header controls.
        self.hint = hint_text(QtWidgets.QLabel())
        muted(self.hint)
        layout.addWidget(self.hint)

        narration_row = QtWidgets.QHBoxLayout()
        narration_row.addWidget(QtWidgets.QLabel("Narration"))
        self.narration = QtWidgets.QLineEdit()
        self.narration.setPlaceholderText("what this entry is for")
        narration_row.addWidget(self.narration)
        layout.addLayout(narration_row)

        # --- grid ---
        self.grid = QtWidgets.QTableWidget(0, 3)
        self.grid.setHorizontalHeaderLabels(["Particulars", "Debit", "Credit"])
        self.grid.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.Stretch)
        self.grid.verticalHeader().setVisible(False)
        self.grid.itemChanged.connect(self._recalculate)
        layout.addWidget(self.grid, 1)

        buttons = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton("Add line")
        add.clicked.connect(lambda: self._add_row())
        remove = QtWidgets.QPushButton("Remove line")
        remove.clicked.connect(self._remove_row)
        create = QtWidgets.QPushButton("Create ledger  (Alt+L)")
        create.setToolTip("Create a ledger without leaving this voucher — "
                          "whatever you have typed into the line is kept")
        create.clicked.connect(self.create_ledger)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addWidget(create)
        buttons.addStretch(1)
        self.dr_label = QtWidgets.QLabel()
        self.cr_label = QtWidgets.QLabel()
        self.gap_label = QtWidgets.QLabel()
        for widget in (self.dr_label, self.cr_label, self.gap_label):
            apply_mono(widget)
            buttons.addWidget(widget)
            buttons.addSpacing(14)
        layout.addLayout(buttons)

        self.notice = QtWidgets.QLabel()
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)

        self.accept_button = QtWidgets.QPushButton("Accept  (Ctrl+A)")
        self.accept_button.setDefault(True)
        self.accept_button.clicked.connect(self.accept_voucher)
        bottom = QtWidgets.QHBoxLayout()
        # The two destructive actions sit at the far LEFT, a whole stretch away
        # from Accept, so neither is ever the button next to the one you meant.
        self.delete_button = QtWidgets.QPushButton("Delete voucher")
        self.delete_button.clicked.connect(self.delete_voucher)
        self.delete_button.setVisible(False)
        bottom.addWidget(self.delete_button)
        self.void_button = QtWidgets.QPushButton("Void voucher")
        self.void_button.clicked.connect(self.void_voucher)
        self.void_button.setVisible(False)
        bottom.addWidget(self.void_button)
        self.restore_button = QtWidgets.QPushButton("Restore voucher")
        self.restore_button.clicked.connect(self.restore_voucher)
        self.restore_button.setVisible(False)
        bottom.addWidget(self.restore_button)
        bottom.addStretch(1)
        self.cancel_button = QtWidgets.QPushButton("Cancel edit")
        self.cancel_button.clicked.connect(self.reset)
        self.cancel_button.setVisible(False)
        bottom.addWidget(self.cancel_button)
        bottom.addWidget(self.accept_button)
        layout.addLayout(bottom)

        # Alt+L, not Alt+C: the menu bar holds "&Company", and a menu-bar
        # mnemonic WINS over a widget shortcut, so Alt+C opened that menu and
        # this never fired. Any new Alt shortcut must dodge the menu letters
        # (G, V, R, C, S) for the same reason.
        shortcut = QtGui.QShortcut(QtGui.QKeySequence("Alt+L"), self)
        shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        shortcut.activated.connect(self.create_ledger)

        self.reset()

    # --- grid plumbing ------------------------------------------------- #
    def _ledger_combo(self) -> QtWidgets.QComboBox:
        combo = QtWidgets.QComboBox()
        combo.setEditable(True)
        combo.addItem("")
        combo.addItems(self._ledger_names)
        combo.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        completer = combo.completer()
        completer.setFilterMode(QtCore.Qt.MatchContains)
        completer.setCompletionMode(QtWidgets.QCompleter.PopupCompletion)
        combo.currentTextChanged.connect(self._recalculate)
        return combo

    def _add_row(self, ledger: str = "", debit: float = 0.0,
                 credit: float = 0.0) -> None:
        row = self.grid.rowCount()
        self.grid.insertRow(row)
        combo = self._ledger_combo()
        if ledger:
            combo.setCurrentText(ledger)
        self.grid.setCellWidget(row, 0, combo)
        for col, value in ((1, debit), (2, credit)):
            item = QtWidgets.QTableWidgetItem("" if not value else f"{value:,.2f}")
            item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            item.setFont(mono_font())
            self.grid.setItem(row, col, item)
        self._recalculate()

    def _remove_row(self) -> None:
        if self.grid.rowCount() > 2:
            self.grid.removeRow(self.grid.currentRow()
                                if self.grid.currentRow() >= 0
                                else self.grid.rowCount() - 1)
            self._recalculate()

    def row_values(self) -> list[dict]:
        rows = []
        for row in range(self.grid.rowCount()):
            combo = self.grid.cellWidget(row, 0)
            name = combo.currentText().strip() if combo else ""
            debit = parse_amount(self.grid.item(row, 1).text()
                                 if self.grid.item(row, 1) else "")
            credit = parse_amount(self.grid.item(row, 2).text()
                                  if self.grid.item(row, 2) else "")
            if not name or (not debit and not credit):
                continue
            if name not in self._ledger_ids:
                continue
            rows.append({"ledger_id": self._ledger_ids[name],
                         "debit": debit, "credit": credit})
        return rows

    def _recalculate(self, *_args) -> None:
        if getattr(self, "_loading", False):
            return
        rows = self.row_values()
        debit = round(sum(r["debit"] for r in rows), 2)
        credit = round(sum(r["credit"] for r in rows), 2)
        gap = round(debit - credit, 2)

        self.dr_label.setText(f"Dr {debit:,.2f}")
        self.cr_label.setText(f"Cr {credit:,.2f}")
        self.gap_label.setText(f"Difference {gap:,.2f}")
        tone(self.gap_label,
             "good" if abs(gap) <= 0.005 and debit > 0 else "bad")

        balanced = abs(gap) <= 0.005 and debit > 0 and len(rows) >= 2
        distinct = len({r["ledger_id"] for r in rows}) >= 2
        # This runs on every keystroke, so the voided freeze has to be applied
        # HERE as well as on load - otherwise typing in the grid hands Accept
        # back and the data layer would then refuse the save.
        self.accept_button.setEnabled(balanced and distinct
                                      and not self.editing_voided)

        notes = db.voucher_type_warnings(
            self.type_box.currentText(), rows, self._ledgers)
        if self.editing_voided:
            self.notice.setText(
                "This voucher is voided. It keeps its number but posts nothing "
                "to the books, so it is left out of every report. Restore it "
                "to make it count again, or delete it to give up its number.")
            tone(self.notice, "warn")
        elif not rows:
            self.notice.setText(
                "Pick a ledger on each line, then put the amount under Debit or "
                "Credit — never both on one line.")
            tone(self.notice, "muted")
        elif not balanced or not distinct:
            self.notice.setText("Debit and credit must agree, across at least "
                                "two different ledgers.")
            tone(self.notice, "bad")
        elif notes:
            self.notice.setText("ℹ️  " + "  ".join(notes))
            tone(self.notice, "warn")
        else:
            self.notice.setText("Debit equals credit — ready to accept.")
            tone(self.notice, "good")

        # keep a spare blank line at the bottom, the way Tally does
        last = self.grid.rowCount() - 1
        combo = self.grid.cellWidget(last, 0)
        if combo and combo.currentText().strip() and self.grid.rowCount() < 30:
            self._add_row()

    def _type_changed(self, name: str) -> None:
        if self.editing_id is None:
            self.number_label.setText(
                f"No. {db.next_voucher_number(name)}")
        self.hint.setText(db.VOUCHER_TYPES.get(name, ""))
        self._recalculate()

    # --- actions ------------------------------------------------------- #
    def create_ledger(self) -> None:
        """Alt+L: make the ledger here, then use it on this line.

        Whatever has been typed into the current cell is carried into the
        dialog as the name, so a half-typed account is not wasted.
        """
        row = self.grid.currentRow()
        if row < 0:
            row = self._first_empty_row()
        combo = self.grid.cellWidget(row, 0) if row >= 0 else None
        typed = combo.currentText().strip() if combo else ""
        if typed in self._ledger_ids:
            typed = ""   # already a real ledger, so start the dialog empty

        dialog = LedgerDialog(self, name=typed)
        if not dialog.exec() or not dialog.created:
            return

        self._add_ledger_name(dialog.created)
        if combo is not None:
            self._loading = True
            combo.setCurrentText(dialog.created)
            self._loading = False
        self._recalculate()
        self.masters_changed.emit()
        window = self.window()
        if isinstance(window, QtWidgets.QMainWindow):
            window.statusBar().showMessage(
                f"Ledger '{dialog.created}' created and selected.", 6000)

    def _first_empty_row(self) -> int:
        for row in range(self.grid.rowCount()):
            combo = self.grid.cellWidget(row, 0)
            if combo and not combo.currentText().strip():
                return row
        return self.grid.rowCount() - 1

    def _add_ledger_name(self, name: str) -> None:
        """Offer a newly created ledger on every line, without losing entries."""
        self.reload_ledgers()
        self._loading = True
        for row in range(self.grid.rowCount()):
            combo = self.grid.cellWidget(row, 0)
            if combo and combo.findText(name) < 0:
                combo.addItem(name)
        self._loading = False

    def reload_ledgers(self) -> None:
        self._ledgers = db.get_ledgers()
        self._ledger_names = list(self._ledgers["name"])
        self._ledger_ids = dict(zip(self._ledgers["name"],
                                    self._ledgers["id"].astype(int)))

    def reset(self) -> None:
        self._loading = True
        self.reload_ledgers()
        self.editing_id = None
        self.editing_voided = False
        self.title.setText("Voucher Entry")
        self.cancel_button.setVisible(False)
        self.delete_button.setVisible(False)
        self.void_button.setVisible(False)
        self.restore_button.setVisible(False)
        self.accept_button.setEnabled(True)
        self.narration.clear()
        self.date_edit.setDate(to_qdate(date.today()))
        self.grid.setRowCount(0)
        self._loading = False
        self._add_row()
        self._add_row()
        self._type_changed(self.type_box.currentText())

    def load_voucher(self, voucher_id: int) -> None:
        record = db.get_voucher(voucher_id)
        if not record:
            return
        head, lines = record
        self._loading = True
        self.reload_ledgers()
        self.editing_id = int(head["id"])
        self.editing_voided = bool(head.get("voided", 0))
        self.title.setText(
            (f"Voided {head['voucher_type']} voucher no. {head['number']}"
             if self.editing_voided else
             f"Altering {head['voucher_type']} voucher no. {head['number']}"))
        self.cancel_button.setVisible(True)
        self.delete_button.setVisible(True)
        # A voided voucher offers Restore instead of Void, and cannot be
        # accepted - it is a record of what was annulled, not a draft.
        self.void_button.setVisible(not self.editing_voided)
        self.restore_button.setVisible(self.editing_voided)
        self.accept_button.setEnabled(not self.editing_voided)
        self.type_box.setCurrentText(str(head["voucher_type"]))
        self.date_edit.setDate(to_qdate(str(head["voucher_date"])))
        self.narration.setText(str(head["narration"]))
        self.number_label.setText(f"No. {head['number']}")
        self.grid.setRowCount(0)
        self._loading = False
        for line in lines:
            self._add_row(str(line["ledger_name"]), float(line["debit"]),
                          float(line["credit"]))
        self._recalculate()

    def _voucher_label(self) -> str:
        head = self.title.text()
        return head.split(" ", 1)[1] if " " in head else "this voucher"

    def _finish(self, message: str) -> None:
        """Leave the editor and tell the rest of the app the books moved."""
        self.reset()
        self.posted.emit()
        window = self.window()
        if isinstance(window, QtWidgets.QMainWindow):
            window.statusBar().showMessage(message, 6000)

    def void_voucher(self) -> None:
        if self.editing_id is None:
            return
        confirm = QtWidgets.QMessageBox.question(
            self, "Void this voucher?",
            f"Void {self._voucher_label()}?\n\n"
            "It keeps its number and stays in the Day Book, but posts nothing "
            "to the books and drops out of every report. You can restore it "
            "later.",
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No)
        if confirm != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            db.void_voucher(self.editing_id)
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "Cannot void", str(exc))
            return
        self._finish("Voucher voided — it keeps its number but posts nothing.")

    def restore_voucher(self) -> None:
        if self.editing_id is None:
            return
        try:
            db.restore_voucher(self.editing_id)
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "Cannot restore", str(exc))
            return
        self._finish("Voucher restored — it counts in the books again.")

    def _ask_delete(self) -> str:
        """Put the three ways out of a delete, and report which was chosen.

        Kept apart from `delete_voucher` so the decision can be driven in a
        test: `clickedButton()` is set by a real click and cannot be faked.
        Returns "delete", "void" or "keep".
        """
        # Deleting is the one action with nothing behind it, so it says so and
        # defaults to keeping. Void is offered again right here, because most
        # of the time it is what was actually wanted.
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Delete this voucher?")
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setText(f"Delete {self._voucher_label()} for good?")
        box.setInformativeText(
            "This cannot be undone. Voucher number "
            f"{self.number_label.text().removeprefix('No. ')} is given up — if "
            "it was not the last of its type, it leaves a permanent gap in the "
            "series.\n\nIf you only want to annul it, void it instead — that "
            "keeps the number and can be undone.")
        delete = box.addButton("Delete for good",
                               QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        void = box.addButton("Void instead",
                             QtWidgets.QMessageBox.ButtonRole.ActionRole)
        keep = box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(keep)
        box.exec()
        clicked = box.clickedButton()
        return "delete" if clicked is delete else \
               "void" if clicked is void else "keep"

    def delete_voucher(self) -> None:
        if self.editing_id is None:
            return
        choice = self._ask_delete()
        if choice == "void":
            self.void_voucher()
            return
        if choice != "delete":
            return
        db.delete_voucher(self.editing_id)
        self._finish("Voucher deleted — its number has been given up.")

    def accept_voucher(self) -> None:
        rows = self.row_values()
        try:
            db.save_voucher(
                voucher_type=self.type_box.currentText(),
                voucher_date=from_qdate(self.date_edit.date()),
                narration=self.narration.text(),
                lines=rows,
                voucher_id=self.editing_id,
            )
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "Cannot accept", str(exc))
            return
        total = sum(r["debit"] for r in rows)
        was_editing = self.editing_id is not None
        self.reset()
        self.posted.emit()
        window = self.window()
        if isinstance(window, QtWidgets.QMainWindow):
            window.statusBar().showMessage(
                ("Voucher altered — " if was_editing else "Voucher accepted — ")
                + money(total), 6000)


# --------------------------------------------------------------------------- #
# report screens
# --------------------------------------------------------------------------- #
class PeriodDialog(QtWidgets.QDialog):
    """Asks which dates a custom period runs between.

    Choosing "Custom dates" used to do nothing at all: the dropdown changed
    and the editors were left alone, so there was no sign the choice had been
    registered and - worse - if "Since books began" had been chosen before it,
    the From editor was still disabled and no start date could be set at all.

    An as-at report is asked for ONE date, not two. A balance sheet is drawn
    at a moment and its balances run from the beginning of the books whatever
    you put in a start field, so offering one would be offering a control that
    does nothing.
    """

    def __init__(self, start: date, end: date, with_from: bool = True,
                 parent=None):
        super().__init__(parent)
        self.with_from = with_from
        self.setWindowTitle("Custom dates" if with_from else "As at")
        layout = QtWidgets.QVBoxLayout(self)

        form = QtWidgets.QFormLayout()
        self.from_edit = QtWidgets.QDateEdit(to_qdate(start))
        self.to_edit = QtWidgets.QDateEdit(to_qdate(end))
        for editor in (self.from_edit, self.to_edit):
            editor.setCalendarPopup(True)
            editor.setDisplayFormat("dd/MM/yyyy")
        if with_from:
            form.addRow("From", self.from_edit)
            form.addRow("To", self.to_edit)
        else:
            form.addRow("As at", self.to_edit)
        layout.addLayout(form)

        self.note = muted(flowing(QtWidgets.QLabel(
            "Any dates you like - a quarter, a month, part of a year."
            if with_from else
            "Balances are shown as they stood at the end of this day.")))
        layout.addWidget(self.note)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        start, end = self.dates()
        if start is not None and start > end:
            QtWidgets.QMessageBox.warning(
                self, "Dates the wrong way round",
                "The From date is after the To date. A period cannot end "
                "before it starts.")
            return
        self.accept()

    def dates(self) -> tuple[date | None, date]:
        return (from_qdate(self.from_edit.date()) if self.with_from else None,
                from_qdate(self.to_edit.date()))


class PeriodBar(QtWidgets.QWidget):
    """Which period a report covers: pick a financial year, or set dates.

    The year dropdown is the quick path - "the Balance Sheet for 2025-2026" is
    how the question is actually asked, and reaching it by typing two dates is
    needless work. The dates stay, because a period that is not a whole year
    (a quarter, a month, up to yesterday) still has to be reachable.

    The two cannot be allowed to disagree: editing a date by hand moves the
    dropdown to **Custom dates**, because leaving it reading "FY 2026-2027"
    over a period that is no longer that year would be a lie about what you
    are looking at.
    """

    changed = QtCore.Signal()

    SINCE = "Since books began"
    CUSTOM = "Custom dates…"
    # Marks the since-books-began entry. The years carry a FinancialYear and
    # Custom carries None, so the three are told apart by what they hold
    # rather than by their position in a list that changes with the data.
    ALL = "all"

    def __init__(self, with_from: bool = True, parent=None):
        super().__init__(parent)
        self.with_from = with_from
        # Set while the bar updates its own dates, so that the handlers which
        # watch for YOUR edits do not fire for ours - without it, choosing a
        # year would immediately flip the dropdown to Custom.
        self._quiet = False
        # Which entry was in force before the current one, so that cancelling
        # the custom-dates dialog can put the dropdown back.
        self._last_index = 0
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)

        # A trial balance and a balance sheet are drawn AT A MOMENT - their
        # balances run from the beginning of the books however the period is
        # set - so they are labelled for what they are. Left as "Period ... To"
        # they read as half a control, a To with the From missing, next to the
        # "From ... To" the period reports show.
        row.addWidget(QtWidgets.QLabel("Period" if with_from else "Year"))
        self.year_box = QtWidgets.QComboBox()
        self.year_box.setToolTip(
            "The financial year to report on. The years come from the books, "
            "and the month one opens on is set on the Company screen."
            if with_from else
            "The financial year to report at the close of. The years come "
            "from the books, and the month one opens on is set on the "
            "Company screen.")
        self.year_box.currentIndexChanged.connect(self._year_chosen)
        row.addWidget(self.year_box)

        if with_from:
            row.addWidget(QtWidgets.QLabel("From"))
            self.from_edit = QtWidgets.QDateEdit()
            self.from_edit.setCalendarPopup(True)
            self.from_edit.setDisplayFormat("dd/MM/yyyy")
            self.from_edit.dateChanged.connect(self._date_typed)
            row.addWidget(self.from_edit)

        row.addWidget(QtWidgets.QLabel("To" if with_from else "As at"))
        self.to_edit = QtWidgets.QDateEdit(to_qdate(date.today()))
        self.to_edit.setCalendarPopup(True)
        self.to_edit.setDisplayFormat("dd/MM/yyyy")
        self.to_edit.dateChanged.connect(self._date_typed)
        row.addWidget(self.to_edit)

        row.addStretch(1)

        self.reload_years()

    # --- the year list ---------------------------------------------------- #
    def reload_years(self) -> None:
        """(Re)fill the dropdown from the books, keeping the year selected.

        Called again whenever the books change under it - another company
        opened, or the financial-year start month edited on the Company
        screen - because both change what the years ARE. Matching on the
        label rather than the index means a year that still exists stays
        selected even though the list around it has moved.
        """
        wanted = self.year_box.currentText()
        years = acc.financial_years()

        self._quiet = True
        self.year_box.clear()
        # Since-books-began lives in this dropdown rather than beside it as a
        # tickbox: it is one of the periods you can choose, and a row holding
        # both was 1468px wide at the largest text size - wider than the
        # 1400px ceiling the whole app has to fit inside.
        if self.with_from:
            self.year_box.addItem(self.SINCE, self.ALL)
        for year in years:
            self.year_box.addItem(f"FY {year.label}", year)
        self.year_box.addItem(self.CUSTOM, None)

        index = self.year_box.findText(wanted) if wanted else -1
        if index < 0:
            current = acc.current_financial_year()
            index = max(0, self.year_box.findText(f"FY {current.label}"))
        self.year_box.setCurrentIndex(index)
        self._last_index = index
        self._quiet = False
        self._apply(self.year_box.currentData())

    def _year_chosen(self, index: int) -> None:
        if self._quiet:
            return
        if self.year_box.currentData() is None:
            # Custom: ask for the dates. _last_index is left alone so that
            # cancelling can put the dropdown back where it was.
            self._ask_custom()
            return
        self._last_index = index
        self._apply(self.year_box.currentData())

    def _apply(self, chosen) -> None:
        """Put the chosen period's dates into the editors, then report it.

        ``None`` is Custom, which means leave the dates exactly as they are -
        the point of Custom is that you set them.
        """
        if chosen is None:
            # Custom. Reached on a reload as well as a click, so it must not
            # prompt - that is _ask_custom's job, called from the click only.
            self.changed.emit()
            return
        self._quiet = True
        if chosen == self.ALL:
            # No start date at all, so the From editor has nothing to say.
            if self.with_from:
                self.from_edit.setEnabled(False)
            self.to_edit.setDate(to_qdate(date.today()))
        else:
            if self.with_from:
                self.from_edit.setEnabled(True)
                self.from_edit.setDate(to_qdate(chosen.start))
            # The WHOLE fiscal year, opening day to closing day - including a
            # closing day still in the future for the year in progress. A year
            # in the dropdown means that year, and clamping the end to today
            # quietly gave you a different period from the one named.
            self.to_edit.setDate(to_qdate(chosen.end))
        self._quiet = False
        self.changed.emit()

    def _ask_custom(self) -> None:
        """Ask for the dates, having been asked for a custom period."""
        start = (from_qdate(self.from_edit.date()) if self.with_from
                 else acc.current_financial_year().start)
        dialog = PeriodDialog(start, from_qdate(self.to_edit.date()),
                              self.with_from, self)
        # Parented for its position and palette, but released afterwards:
        # without this one lingers on the bar for every custom period chosen.
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            # Nothing changed, so the dropdown must not read as though it did.
            self._quiet = True
            self.year_box.setCurrentIndex(self._last_index)
            self._quiet = False
            return
        picked_start, picked_end = dialog.dates()
        self._quiet = True
        if self.with_from and picked_start is not None:
            # Re-enabled explicitly: coming here from since-books-began leaves
            # it disabled, which is what made custom dates unusable.
            self.from_edit.setEnabled(True)
            self.from_edit.setDate(to_qdate(picked_start))
        self.to_edit.setDate(to_qdate(picked_end))
        self._quiet = False
        self._last_index = self.year_box.currentIndex()
        self.changed.emit()

    def _date_typed(self, *_) -> None:
        """A date changed by hand: the period is no longer a whole year."""
        if self._quiet:
            return
        self._mark_custom()
        self.changed.emit()

    def _mark_custom(self) -> None:
        index = self.year_box.findText(self.CUSTOM)
        if index >= 0 and self.year_box.currentIndex() != index:
            self._quiet = True
            self.year_box.setCurrentIndex(index)
            self._last_index = index
            self._quiet = False
        # Typing a date is the one way into a custom period that does not go
        # through the dialog, so the From editor is re-enabled here too.
        if self.with_from:
            self.from_edit.setEnabled(True)

    def selected_year(self):
        """The chosen financial year, or None for custom or all dates."""
        chosen = self.year_box.currentData()
        return chosen if isinstance(chosen, acc.FinancialYear) else None

    def range(self) -> tuple[date | None, date]:
        """The period, as ``accounting`` wants it: None means no start date."""
        if self.year_box.currentData() == self.ALL or not self.with_from:
            return None, from_qdate(self.to_edit.date())
        return from_qdate(self.from_edit.date()), from_qdate(self.to_edit.date())


class DayBookScreen(QtWidgets.QWidget):
    open_voucher = QtCore.Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(heading("Day Book"))
        self.period = PeriodBar()
        self.period.changed.connect(self.refresh)
        layout.addWidget(self.period)

        filters = QtWidgets.QHBoxLayout()
        filters.addWidget(QtWidgets.QLabel("Type"))
        self.type_filter = QtWidgets.QComboBox()
        self.type_filter.addItems(["All"] + list(db.VOUCHER_TYPES))
        self.type_filter.currentTextChanged.connect(self.refresh)
        filters.addWidget(self.type_filter)
        filters.addStretch(1)
        self.count = flowing(QtWidgets.QLabel())
        filters.addWidget(self.count)
        layout.addLayout(filters)

        self.table = FrameTable()
        self.table.doubleClicked.connect(self._open)
        layout.addWidget(self.table, 1)
        layout.addWidget(QtWidgets.QLabel(
            "Double-click a voucher to alter it."))
        layout.addWidget(ReportActions(self.report))
        self._frame = pd.DataFrame()
        self._shown = pd.DataFrame()

    def refresh(self) -> None:
        start, end = self.period.range()
        frame = acc.day_book(start, end)
        kind = self.type_filter.currentText()
        if kind != "All" and not frame.empty:
            frame = frame[frame["Vch Type"] == kind]
        self._frame = frame.reset_index(drop=True)
        voided = int(self._frame["voided"].sum()) if not frame.empty else 0
        self.count.setText(f"{len(self._frame)} voucher(s)"
                           + (f", {voided} voided" if voided else ""))
        if frame.empty:
            shown = pd.DataFrame(columns=["Date", "Particulars", "Vch Type",
                                          "Vch No", "Debit", "Credit"])
        else:
            shown = self._frame.drop(columns=["voucher_id", "voided"])
            # The column only appears when there is something to say, so an
            # untouched book is not given an empty column to read past.
            if voided:
                shown.insert(len(shown.columns), "Status",
                             ["Voided" if v else ""
                              for v in self._frame["voided"]])
        # Kept for print and export: what is on SCREEN, without the voucher
        # id the table hides and no reader of a report wants to see.
        self._shown = shown
        self.table.load(shown, numeric=("Debit", "Credit"))

    def report(self) -> Report:
        kind = self.type_filter.currentText()
        return Report(
            title="Day Book",
            period=period_caption(self.period)
            + ("" if kind == "All" else f"  ·  {kind} vouchers only"),
            sections=[ReportSection("", self._shown, ("Debit", "Credit"))],
            notes=[self.count.text()])

    def _open(self, index) -> None:
        if 0 <= index.row() < len(self._frame):
            self.open_voucher.emit(int(self._frame.iloc[index.row()]["voucher_id"]))


class LedgerScreen(QtWidgets.QWidget):
    open_voucher = QtCore.Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(heading("Ledger"))

        picker = QtWidgets.QHBoxLayout()
        picker.addWidget(QtWidgets.QLabel("Account"))
        self.ledger_box = QtWidgets.QComboBox()
        self.ledger_box.setEditable(True)
        self.ledger_box.completer().setFilterMode(QtCore.Qt.MatchContains)
        self.ledger_box.currentTextChanged.connect(self.refresh)
        picker.addWidget(self.ledger_box, 1)
        layout.addLayout(picker)

        self.period = PeriodBar()
        self.period.changed.connect(self.refresh)
        layout.addWidget(self.period)

        self.caption = flowing(QtWidgets.QLabel())
        layout.addWidget(self.caption)
        self.table = FrameTable()
        self.table.doubleClicked.connect(self._open)
        layout.addWidget(self.table, 1)
        self.footer = QtWidgets.QWidget()
        self.footer_slot = QtWidgets.QVBoxLayout()
        self.footer_slot.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.footer_slot)
        layout.addWidget(ReportActions(self.report))
        self._frame = pd.DataFrame()
        self._shown = pd.DataFrame()
        self._summary = ""

    def reload_ledgers(self) -> None:
        current = self.ledger_box.currentText()
        names = list(db.get_ledgers(include_inactive=True)["name"])
        self.ledger_box.blockSignals(True)
        self.ledger_box.clear()
        self.ledger_box.addItems(names)
        if current in names:
            self.ledger_box.setCurrentText(current)
        self.ledger_box.blockSignals(False)

    def refresh(self) -> None:
        name = self.ledger_box.currentText()
        ledgers = db.get_ledgers(include_inactive=True).set_index("name")
        if name not in ledgers.index:
            self.caption.setText("Pick an account.")
            self.table.load(pd.DataFrame())
            return
        start, end = self.period.range()
        statement = acc.ledger_statement(int(ledgers.loc[name, "id"]), start, end)
        self.caption.setText(
            f"<b>{statement['ledger']}</b> &nbsp; under {statement['group']}"
            f" &nbsp;·&nbsp; Opening {acc.fmt_side(statement['opening'])}"
            f" &nbsp;·&nbsp; Closing <b>{acc.fmt_side(statement['closing'])}</b>")
        self._frame = statement["entries"].reset_index(drop=True)
        shown = self._frame.drop(columns=["voucher_id"]) if not self._frame.empty \
            else pd.DataFrame(columns=["Date", "Particulars", "Vch Type",
                                       "Vch No", "Debit", "Credit", "Balance"])
        self._shown = shown
        # The screen says this in the caption as HTML and in the totals bar;
        # on paper it has to be one plain line, and it is the whole point of a
        # ledger statement - opening, what moved, closing.
        self._summary = (
            f"{statement['ledger']}, under {statement['group']}"
            f"  ·  Opening {acc.fmt_side(statement['opening'])}"
            f"  ·  Debit {statement['debit_total']:,.2f}"
            f"  ·  Credit {statement['credit_total']:,.2f}"
            f"  ·  Closing {acc.fmt_side(statement['closing'])}")
        self.table.load(shown, numeric=("Debit", "Credit"))

        while self.footer_slot.count():
            item = self.footer_slot.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.footer_slot.addWidget(totals_bar(
            ("Debit", f"{statement['debit_total']:,.2f}"),
            ("Credit", f"{statement['credit_total']:,.2f}"),
            ("Closing", acc.fmt_side(statement["closing"]))))

    def report(self) -> Report:
        name = self.ledger_box.currentText() or "Ledger"
        return Report(
            title=f"Ledger - {name}",
            period=period_caption(self.period),
            sections=[ReportSection("", self._shown,
                                    ("Debit", "Credit", "Balance"))],
            notes=[self._summary])

    def _open(self, index) -> None:
        if 0 <= index.row() < len(self._frame):
            self.open_voucher.emit(int(self._frame.iloc[index.row()]["voucher_id"]))


class TrialBalanceScreen(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(heading("Trial Balance"))
        self.period = PeriodBar(with_from=False)
        self.period.changed.connect(self.refresh)
        layout.addWidget(self.period)
        self.by_group = QtWidgets.QCheckBox("Summarise by group")
        self.by_group.toggled.connect(self.refresh)
        layout.addWidget(self.by_group)
        self.status = flowing(QtWidgets.QLabel())
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = FrameTable()
        layout.addWidget(self.table, 1)
        layout.addWidget(ReportActions(self.report))
        self._shown = pd.DataFrame()

    def report(self) -> Report:
        return Report(
            title="Trial Balance",
            period=period_caption(self.period)
            + ("  ·  summarised by group" if self.by_group.isChecked() else ""),
            sections=[ReportSection("", self._shown, ("Debit", "Credit"))],
            notes=[self.status.text()])

    def refresh(self) -> None:
        _, as_at = self.period.range()
        trial = acc.trial_balance(as_at, by_group=self.by_group.isChecked())
        rows = trial["rows"]
        if rows.empty:
            self._shown = pd.DataFrame(
                columns=["Particulars", "Debit", "Credit"])
            self.table.load(self._shown, numeric=("Debit", "Credit"))
        else:
            total = {"Particulars": "Total", "Debit": trial["debit"],
                     "Credit": trial["credit"]}
            if "Under" in rows.columns:
                total["Under"] = ""
            shown = pd.concat([rows, pd.DataFrame([total])], ignore_index=True)
            self._shown = shown
            self.table.load(shown, numeric=("Debit", "Credit"), bold_last=True)

        if abs(trial["difference"]) <= acc.EPS:
            self.status.setText("Debits equal credits.")
            tone(self.status, "good")
        else:
            self.status.setText(
                f"Out by {money(abs(trial['difference']))} — this is the "
                "difference in opening balances, not a problem with any "
                "voucher. Every voucher is forced to balance when accepted.")
            tone(self.status, "warn")


class ProfitLossScreen(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(heading("Profit & Loss"))
        self.period = PeriodBar()
        # Opens on the financial year, not on since-books-began as it used to:
        # a profit and loss account is a statement FOR a year, and with a year
        # dropdown on the screen the period should agree with what it says.
        # Since-books-began is still one choice away in the same dropdown.
        self.period.changed.connect(self.refresh)
        layout.addWidget(self.period)

        self.trading_label = flowing(QtWidgets.QLabel())
        layout.addWidget(self.trading_label)
        trading = QtWidgets.QHBoxLayout()
        self.trading_expense = FrameTable()
        self.trading_income = FrameTable()
        trading.addWidget(self.trading_expense)
        trading.addWidget(self.trading_income)
        layout.addLayout(trading, 1)
        self.gross_label = flowing(QtWidgets.QLabel())
        layout.addWidget(self.gross_label)

        self.pl_label = flowing(QtWidgets.QLabel())
        layout.addWidget(self.pl_label)
        indirect = QtWidgets.QHBoxLayout()
        self.indirect_expense = FrameTable()
        self.indirect_income = FrameTable()
        indirect.addWidget(self.indirect_expense)
        indirect.addWidget(self.indirect_income)
        layout.addLayout(indirect, 1)
        self.net_label = flowing(QtWidgets.QLabel())
        layout.addWidget(self.net_label)
        layout.addWidget(ReportActions(self.report))
        self._parts: dict[str, pd.DataFrame] = {}
        self._lines: list[str] = []

    def report(self) -> Report:
        """Four tables, in the order the statement is read.

        The side labels carry the totals and the profit figures, which are the
        numbers anybody actually wants - so they go on as notes rather than
        being left behind on the screen.
        """
        blank = pd.DataFrame(columns=["Particulars", "Under", "Amount"])
        order = (("Trading account - purchases and direct expenses",
                  "trading_expense"),
                 ("Trading account - sales and direct incomes",
                  "trading_income"),
                 ("Indirect expenses", "indirect_expense"),
                 ("Indirect incomes", "indirect_income"))
        return Report(
            title="Profit & Loss",
            period=period_caption(self.period),
            sections=[ReportSection(label,
                                    self._parts.get(key, blank), ("Amount",))
                      for label, key in order],
            notes=self._lines)

    def refresh(self) -> None:
        start, end = self.period.range()
        report = acc.profit_and_loss(start, end)
        totals = report["totals"]
        blank = pd.DataFrame(columns=["Particulars", "Under", "Amount"])

        self.trading_label.setText(
            f"<b>Trading account</b> &nbsp; purchases and direct expenses "
            f"{money(totals['trading_expense'])} &nbsp;|&nbsp; sales and direct "
            f"incomes {money(totals['trading_income'])}")
        self.trading_expense.load(
            report["trading_expense"] if not report["trading_expense"].empty else blank,
            numeric=("Amount",))
        self.trading_income.load(
            report["trading_income"] if not report["trading_income"].empty else blank,
            numeric=("Amount",))
        gross = totals["gross_profit"]
        self.gross_label.setText(
            f"<b>{'Gross profit' if gross >= 0 else 'Gross loss'}: "
            f"{money(abs(gross))}</b>")

        self.pl_label.setText(
            f"<b>Profit &amp; loss account</b> &nbsp; indirect expenses "
            f"{money(totals['indirect_expense'])} &nbsp;|&nbsp; indirect incomes "
            f"{money(totals['indirect_income'])}")
        self.indirect_expense.load(
            report["indirect_expense"] if not report["indirect_expense"].empty
            else blank, numeric=("Amount",))
        self.indirect_income.load(
            report["indirect_income"] if not report["indirect_income"].empty
            else blank, numeric=("Amount",))
        net = totals["net_profit"]
        self.net_label.setText(
            f"<b>{'Net profit' if net >= 0 else 'Net loss'}: {money(abs(net))}</b>")

        self._parts = {key: report[key] for key in
                       ("trading_expense", "trading_income",
                        "indirect_expense", "indirect_income")}
        self._lines = [
            f"Purchases and direct expenses {money(totals['trading_expense'])}"
            f"  ·  Sales and direct incomes {money(totals['trading_income'])}",
            f"{'Gross profit' if gross >= 0 else 'Gross loss'}"
            f" {money(abs(gross))}",
            f"Indirect expenses {money(totals['indirect_expense'])}"
            f"  ·  Indirect incomes {money(totals['indirect_income'])}",
            f"{'Net profit' if net >= 0 else 'Net loss'} {money(abs(net))}"]


class BalanceSheetScreen(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(heading("Balance Sheet"))
        self.period = PeriodBar(with_from=False)
        self.period.changed.connect(self.refresh)
        layout.addWidget(self.period)
        self.status = flowing(QtWidgets.QLabel())
        layout.addWidget(self.status)

        sides = QtWidgets.QHBoxLayout()
        left = QtWidgets.QVBoxLayout()
        self.liability_label = flowing(QtWidgets.QLabel())
        self.liabilities = FrameTable()
        left.addWidget(self.liability_label)
        left.addWidget(self.liabilities)
        right = QtWidgets.QVBoxLayout()
        self.asset_label = flowing(QtWidgets.QLabel())
        self.assets = FrameTable()
        right.addWidget(self.asset_label)
        right.addWidget(self.assets)
        sides.addLayout(left)
        sides.addLayout(right)
        layout.addLayout(sides, 1)
        # Wrapped, and muted like every other piece of guidance. Unwrapped, a
        # sentence this long sets the window's MINIMUM width to the width of
        # the sentence - it was forcing 1770px, so the window could not be made
        # narrower than that and grew worse with every step of text size.
        note = QtWidgets.QLabel(
            "Profit & Loss A/c carries the net profit from the start of the "
            "books to this date. Nominal ledgers are closed into it.")
        note.setWordWrap(True)
        layout.addWidget(muted(note))
        layout.addWidget(ReportActions(self.report))
        self._note = note.text()
        self._sides: dict[str, pd.DataFrame] = {}
        self._lines: list[str] = []

    def report(self) -> Report:
        blank = pd.DataFrame(columns=["Particulars", "Amount"])
        return Report(
            title="Balance Sheet",
            period=period_caption(self.period),
            sections=[
                ReportSection("Liabilities",
                              self._sides.get("liabilities", blank),
                              ("Amount",)),
                ReportSection("Assets", self._sides.get("assets", blank),
                              ("Amount",))],
            notes=self._lines + [self._note])

    def refresh(self) -> None:
        _, as_at = self.period.range()
        sheet = acc.balance_sheet(as_at)
        totals = sheet["totals"]
        self.liability_label.setText(
            f"<b>Liabilities</b> &nbsp; {money(totals['liabilities'])}")
        self.asset_label.setText(f"<b>Assets</b> &nbsp; {money(totals['assets'])}")
        self.liabilities.load(sheet["liabilities"], numeric=("Amount",))
        self.assets.load(sheet["assets"], numeric=("Amount",))
        self._sides = {"liabilities": sheet["liabilities"],
                       "assets": sheet["assets"]}
        self._lines = [
            f"Liabilities {money(totals['liabilities'])}"
            f"  ·  Assets {money(totals['assets'])}"]
        if abs(totals["difference"]) <= acc.EPS:
            self.status.setText("Both sides agree.")
            tone(self.status, "good")
        else:
            self.status.setText("The two sides do not agree — that should be "
                                "impossible, please report it.")
            tone(self.status, "bad")


class MastersScreen(QtWidgets.QWidget):
    changed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(heading("Masters"))

        tabs = QtWidgets.QTabWidget()
        layout.addWidget(tabs, 1)

        # --- ledgers ---
        ledger_page = QtWidgets.QWidget()
        ledger_layout = QtWidgets.QVBoxLayout(ledger_page)
        self.ledger_table = FrameTable()
        ledger_layout.addWidget(self.ledger_table, 1)

        form = QtWidgets.QGridLayout()
        self.led_name = QtWidgets.QLineEdit()
        self.led_name.setPlaceholderText("e.g. Bank of India")
        self.led_group = QtWidgets.QComboBox()
        self.led_group.setEditable(True)
        self.led_group.completer().setFilterMode(QtCore.Qt.MatchContains)
        self.led_open = QtWidgets.QDoubleSpinBox()
        self.led_open.setRange(0, 1e12)
        self.led_open.setDecimals(2)
        self.led_open.setGroupSeparatorShown(True)
        self.led_side = QtWidgets.QComboBox()
        self.led_side.addItems(list(db.SIDES))
        form.addWidget(QtWidgets.QLabel("Name"), 0, 0)
        form.addWidget(self.led_name, 0, 1)
        form.addWidget(QtWidgets.QLabel("Under"), 0, 2)
        form.addWidget(self.led_group, 0, 3)
        form.addWidget(QtWidgets.QLabel("Opening"), 1, 0)
        form.addWidget(self.led_open, 1, 1)
        form.addWidget(QtWidgets.QLabel("Dr / Cr"), 1, 2)
        form.addWidget(self.led_side, 1, 3)
        ledger_layout.addLayout(form)

        buttons = QtWidgets.QHBoxLayout()
        create = QtWidgets.QPushButton("Create ledger")
        create.clicked.connect(self.create_ledger)
        delete = QtWidgets.QPushButton("Delete selected")
        delete.clicked.connect(self.delete_ledger)
        buttons.addWidget(create)
        buttons.addWidget(delete)
        buttons.addStretch(1)
        ledger_layout.addLayout(buttons)
        tabs.addTab(ledger_page, "Ledgers")

        # --- groups ---
        group_page = QtWidgets.QWidget()
        group_layout = QtWidgets.QVBoxLayout(group_page)
        self.group_table = FrameTable()
        group_layout.addWidget(self.group_table, 1)
        group_form = QtWidgets.QHBoxLayout()
        self.grp_name = QtWidgets.QLineEdit()
        self.grp_name.setPlaceholderText("e.g. Vehicle Loan")
        self.grp_parent = QtWidgets.QComboBox()
        self.grp_parent.setEditable(True)
        self.grp_parent.completer().setFilterMode(QtCore.Qt.MatchContains)
        group_form.addWidget(QtWidgets.QLabel("Name"))
        group_form.addWidget(self.grp_name, 1)
        group_form.addWidget(QtWidgets.QLabel("Under"))
        group_form.addWidget(self.grp_parent, 1)
        add_group = QtWidgets.QPushButton("Create group")
        add_group.clicked.connect(self.create_group)
        group_form.addWidget(add_group)
        group_layout.addLayout(group_form)
        tabs.addTab(group_page, "Groups")

        self.refresh()

    def refresh(self) -> None:
        ledgers = db.get_ledgers(include_inactive=True)
        balances = acc.balances().set_index("ledger_id")
        view = pd.DataFrame({
            "Ledger": ledgers["name"],
            "Under": ledgers["group_name"],
            "Opening": ledgers["opening"],
            "Dr/Cr": ledgers["opening_side"],
            "Closing": ledgers["id"].astype(int).map(
                lambda i: acc.fmt_side(float(balances.loc[i, "closing"]))
                if i in balances.index else "—"),
        })
        self.ledger_table.load(view, numeric=("Opening",))
        self._ledger_names = list(ledgers["name"])

        groups = db.get_groups()
        self.group_table.load(pd.DataFrame({
            "Group": groups["name"],
            "Under": groups["parent_name"].fillna("—"),
            "Nature": groups["nature"],
            "Trading": groups["affects_gross"].map({1: "yes", 0: ""}),
            "Reserved": groups["is_reserved"].map({1: "yes", 0: ""}),
        }))
        names = list(groups["name"])
        for box in (self.led_group, self.grp_parent):
            current = box.currentText()
            box.blockSignals(True)
            box.clear()
            box.addItems(names)
            if current in names:
                box.setCurrentText(current)
            box.blockSignals(False)

    def _warn(self, exc: Exception) -> None:
        QtWidgets.QMessageBox.warning(self, "Cannot do that", str(exc))

    def create_ledger(self) -> None:
        try:
            db.add_ledger(self.led_name.text(), self.led_group.currentText(),
                          self.led_open.value(), self.led_side.currentText())
        except ValueError as exc:
            self._warn(exc)
            return
        self.led_name.clear()
        self.led_open.setValue(0)
        self.refresh()
        self.changed.emit()

    def delete_ledger(self) -> None:
        row = self.ledger_table.currentRow()
        if row < 0 or row >= len(self._ledger_names):
            return
        name = self._ledger_names[row]
        try:
            db.delete_ledger(db.ledger_id(name))
        except ValueError as exc:
            self._warn(exc)
            return
        self.refresh()
        self.changed.emit()

    def create_group(self) -> None:
        try:
            db.add_group(self.grp_name.text(), self.grp_parent.currentText())
        except ValueError as exc:
            self._warn(exc)
            return
        self.grp_name.clear()
        self.refresh()
        self.changed.emit()


# --------------------------------------------------------------------------- #
# choosing a company at launch
# --------------------------------------------------------------------------- #
# Only the fields needed to open a usable set of books. The rest - address,
# PAN, GSTIN - are filled in afterwards on the Company screen, so creating a
# company is not a fifteen-field form before you can enter a single voucher.
NEW_COMPANY_FIELDS = ("company_name", "fy_start_month", "books_begin",
                      "currency")


class NewCompanyDialog(QtWidgets.QDialog):
    """Create a company, which means creating its database file."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create a company")
        self.created: Path | None = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(muted(flowing(QtWidgets.QLabel(
            "Each company is a separate set of books, in its own file in the "
            "data folder. Address, PAN and GSTIN can be filled in afterwards "
            "on the Company screen."))))

        form = QtWidgets.QFormLayout()
        by_key = {field.key: field for field in db.COMPANY_FIELDS}
        self.editors: dict[str, QtWidgets.QWidget] = {}
        defaults = db.DEFAULT_SETTINGS
        for key in NEW_COMPANY_FIELDS:
            field = by_key[key]
            editor = editor_for(field)
            if key == "fy_start_month":
                editor.setCurrentIndex(int(defaults.get(key, 4)) - 1)
            elif key == "books_begin":
                editor.setDate(to_qdate(date.today()))
            elif key == "currency":
                editor.setCurrentText(defaults.get(key, "₹"))
            self.editors[key] = editor
            form.addRow(field.label + (" *" if field.required else ""), editor)
        layout.addLayout(form)

        self.status = flowing(QtWidgets.QLabel())
        layout.addWidget(self.status)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Create")
        buttons.accepted.connect(self._create)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.editors["company_name"].setFocus()

    def values(self) -> dict[str, str]:
        out = {}
        for key, editor in self.editors.items():
            if isinstance(editor, QtWidgets.QComboBox):
                out[key] = (str(editor.currentData())
                            if editor.currentData() is not None
                            else editor.currentText())
            elif isinstance(editor, QtWidgets.QDateEdit):
                out[key] = from_qdate(editor.date()).isoformat()
            else:
                out[key] = editor.text()
        return out

    def _create(self) -> None:
        try:
            self.created = db.create_company(self.values())
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))
            tone(self.status, "bad")
            return
        self.accept()


class CompanyPicker(QtWidgets.QDialog):
    """Which set of books to work in — a dropdown, over the open window.

    Shown just after the app appears, and again on F3. It opens OVER a running
    window rather than in front of it, so the app is up and usable first and
    the company already open is what the dropdown starts on.

    It never creates anything by itself: *New company…* is a button you press.
    """

    def __init__(self, parent=None, current=None):
        super().__init__(parent)
        self.setWindowTitle("Select company")
        self.chosen: Path | None = None
        # Which company to start the dropdown on. Passed in rather than read
        # from db.DB_PATH, because creating a company switches that - and the
        # dropdown should show the books the window is displaying.
        self.current = Path(current) if current else Path(db.DB_PATH)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(heading("Select company"))
        self.note = muted(flowing(QtWidgets.QLabel()))
        layout.addWidget(self.note)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Company"))
        self.combo = QtWidgets.QComboBox()
        self.combo.currentIndexChanged.connect(self._describe)
        row.addWidget(self.combo, 1)
        layout.addLayout(row)

        self.detail = muted(flowing(QtWidgets.QLabel()))
        layout.addWidget(self.detail)
        layout.addStretch(1)

        buttons = QtWidgets.QHBoxLayout()
        new = QtWidgets.QPushButton("New company…")
        new.setToolTip("Create another set of books")
        new.clicked.connect(self.create)
        buttons.addWidget(new)
        buttons.addStretch(1)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.setToolTip("Stay in the company that is already open")
        cancel.clicked.connect(self.reject)
        self.open_button = QtWidgets.QPushButton("Open")
        self.open_button.setDefault(True)
        self.open_button.clicked.connect(self._open)
        buttons.addWidget(cancel)
        buttons.addWidget(self.open_button)
        layout.addLayout(buttons)

        self.reload()
        self.resize(520, 240)

    def reload(self) -> None:
        companies = db.list_companies()
        self.combo.blockSignals(True)
        self.combo.clear()
        select = 0
        for index, company in enumerate(companies):
            self.combo.addItem(
                f"{company['company_name']}   ({company['file']})",
                company["path"])
            if company["file"] == self.current.name:
                select = index          # the company the window is showing
        self.combo.blockSignals(False)
        if companies:
            self.combo.setCurrentIndex(select)
            self.note.setText(
                f"{len(companies)} "
                f"compan{'y' if len(companies) == 1 else 'ies'} in "
                f"{db.DATA_DIR}.")
        else:
            self.note.setText(
                f"No companies in {db.DATA_DIR} yet. "
                f"Use New company to add one.")
        self.open_button.setEnabled(bool(companies))
        self._describe()
        self.combo.setFocus()

    def _describe(self) -> None:
        """Say what is actually in the company the dropdown is showing."""
        path = self.selected()
        if path is None:
            self.detail.clear()
            return
        details = db.peek_company(path)
        changed = datetime.fromtimestamp(path.stat().st_mtime)
        parts = [f"{details['file']}",
                 f"last changed {changed.strftime('%d/%m/%Y %H:%M')}"]
        if details["gstin"]:
            parts.append(f"GSTIN {details['gstin']}")
        if path.name == self.current.name:
            parts.append("open now")
        self.detail.setText(" · ".join(parts))

    def selected(self) -> Path | None:
        data = self.combo.currentData()
        return Path(data) if data else None

    def _open(self) -> None:
        path = self.selected()
        if path is None:
            return
        self.chosen = path
        self.accept()

    def create(self) -> None:
        """Only ever reached by pressing the button."""
        dialog = NewCompanyDialog(self)
        if dialog.exec() and dialog.created is not None:
            self.chosen = dialog.created
            self.accept()


# --------------------------------------------------------------------------- #
# company master
# --------------------------------------------------------------------------- #
class CompanyScreen(QtWidgets.QWidget):
    """Who the books belong to: name, address, registrations, the year.

    The form is BUILT FROM ``db.COMPANY_FIELDS`` rather than written out, so a
    field added to that tuple appears here and in the Streamlit form with
    nothing further to write, and both save through the same validation.
    """

    changed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(heading("Company"))
        layout.addWidget(muted(flowing(QtWidgets.QLabel(
            "Printed on statements and quoted on returns. Everything but the "
            "name can be left blank until you have it — but a PAN, GSTIN, TAN "
            "or CIN that IS filled in has to be the right shape, because a "
            "malformed one is worse than an absent one."))))

        # Fifteen rows at the largest text size is taller than the window, so
        # the form scrolls instead of having its last fields cut off.
        host = QtWidgets.QWidget()
        self.form = QtWidgets.QFormLayout(host)
        self.form.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        self.editors: dict[str, QtWidgets.QWidget] = {}
        for field in db.COMPANY_FIELDS:
            editor = editor_for(field)
            if field.hint:
                editor.setToolTip(field.hint)
            self.editors[field.key] = editor
            label = QtWidgets.QLabel(
                f"{field.label} *" if field.required else field.label)
            self.form.addRow(label, editor)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setWidget(host)
        layout.addWidget(scroll, 1)

        self.status = flowing(QtWidgets.QLabel())
        layout.addWidget(self.status)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        revert = QtWidgets.QPushButton("Revert")
        revert.setToolTip("Discard edits and reload what is saved")
        revert.clicked.connect(self.refresh)
        self.save_button = QtWidgets.QPushButton("Save  (Ctrl+S)")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save)
        buttons.addWidget(revert)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

        action = QtGui.QAction("Save company", self)
        action.setShortcut(QtGui.QKeySequence("Ctrl+S"))
        action.triggered.connect(self.save)
        self.addAction(action)

        self.refresh()

    def refresh(self) -> None:
        """Load what is saved, discarding any edits.

        Called when you switch TO this screen, and by Revert - but deliberately
        NOT by refresh_all(), which would throw away half-typed details the
        moment a voucher was posted elsewhere. The voucher screen is left out
        of refresh_all() for the same reason: both hold input, not a report.
        """
        values = db.get_company()
        for field in db.COMPANY_FIELDS:
            editor = self.editors[field.key]
            value = values.get(field.key, "")
            if field.kind == "multiline":
                editor.setPlainText(value)
                fit_lines(editor)
            elif field.kind == "month":
                editor.setCurrentIndex(max(0, int(value or 4) - 1))
            elif field.kind == "date":
                editor.setDate(to_qdate(value) if value
                               else to_qdate(date.today()))
            elif field.kind == "choice":
                editor.setCurrentText(value)
            else:
                editor.setText(value)
        self.status.clear()

    def values(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for field in db.COMPANY_FIELDS:
            editor = self.editors[field.key]
            if field.kind == "multiline":
                out[field.key] = editor.toPlainText()
            elif field.kind == "month":
                out[field.key] = str(editor.currentData())
            elif field.kind == "date":
                out[field.key] = from_qdate(editor.date()).isoformat()
            elif field.kind == "choice":
                out[field.key] = editor.currentText()
            else:
                out[field.key] = editor.text()
        return out

    def save(self) -> bool:
        """Save, or explain why not. Rejected input is LEFT IN THE FORM, so a
        single bad character does not cost the rest of the typing."""
        try:
            saved = db.save_company(self.values())
        except ValueError as exc:
            self.status.setText(str(exc))
            tone(self.status, "bad")
            return False
        # Re-show the cleaned values: identifiers come back upper-cased, which
        # the user should see rather than wonder about.
        for key in db.UPPER_FIELDS:
            self.editors[key].setText(saved[key])
        self.status.setText(f"Saved. {saved['company_name']}"
                            + (f" · GSTIN {saved['gstin']}" if saved["gstin"]
                               else ""))
        tone(self.status, "good")
        self.changed.emit()
        return True


# --------------------------------------------------------------------------- #
# main window
# --------------------------------------------------------------------------- #
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        db.init_db()
        # Set by "Switch company"; main() reads it after the window closes and
        # comes back round to open the chosen company.
        self.switch_requested = False
        # The company THIS window is showing. Compared against rather than
        # db.DB_PATH, because creating a company switches the data layer as a
        # side effect - so db.DB_PATH can already have moved on while these
        # screens still show the old books.
        self.company_file = Path(db.DB_PATH)

        # Before any widget is built, so nothing is created under the wrong
        # palette or at the wrong size. Read from the books, so both choices
        # survive a restart and are the same whichever machine opens this file.
        app = QtWidgets.QApplication.instance()
        if app is not None:
            theme.remember_native_style(app)
            theme.remember_base_font(app)
            theme.apply(app, db.get_setting(theme.SETTING_KEY, theme.DEFAULT))
            theme.apply_font(
                app, db.get_setting(theme.FONT_SETTING_KEY, theme.DEFAULT_TEXT))

        self._set_title()
        # The window grows with the text. At a larger size the same 1180x760
        # would simply push the lower half of a screen out of view, which is
        # the opposite of making anything easier to read.
        self.resize(self._sized_to_text())

        self.stack = QtWidgets.QStackedWidget()
        self.setCentralWidget(self.stack)

        self.voucher = VoucherScreen()
        self.daybook = DayBookScreen()
        self.ledger = LedgerScreen()
        self.trial = TrialBalanceScreen()
        self.profit = ProfitLossScreen()
        self.sheet = BalanceSheetScreen()
        self.masters = MastersScreen()
        self.company = CompanyScreen()

        self.screens = [
            ("Voucher Entry", self.voucher),
            ("Day Book", self.daybook),
            ("Ledger", self.ledger),
            ("Trial Balance", self.trial),
            ("Profit && Loss", self.profit),
            ("Balance Sheet", self.sheet),
            ("Masters", self.masters),
            ("Company", self.company),
        ]
        for _, widget in self.screens:
            self.stack.addWidget(widget)

        self.voucher.posted.connect(self.refresh_all)
        self.voucher.masters_changed.connect(self._masters_changed)
        self.masters.changed.connect(self.refresh_all)
        self.daybook.open_voucher.connect(self._alter)
        self.ledger.open_voucher.connect(self._alter)
        # The company name is in the title bar and its currency symbol is in
        # every figure, so saving it has to reach the whole window.
        self.company.changed.connect(self._company_changed)

        self._build_menu()
        self.statusBar().showMessage(
            "Alt+Shift+P/R/C/J/S/U switch voucher type · Ctrl+A accepts")
        self.refresh_all()
        self.show_screen(0)

    def _build_menu(self) -> None:
        bar = self.menuBar()
        go = bar.addMenu("&Go")
        for index, (label, _) in enumerate(self.screens):
            action = QtGui.QAction(label.replace("&&", "&"), self)
            action.setShortcut(QtGui.QKeySequence(f"Ctrl+{index + 1}"))
            action.triggered.connect(lambda _=False, i=index: self.show_screen(i))
            go.addAction(action)

        voucher_menu = bar.addMenu("&Voucher")
        for key, name in VOUCHER_KEYS.items():
            action = QtGui.QAction(f"{name}\t{key}", self)
            action.setShortcut(QtGui.QKeySequence(key))
            action.triggered.connect(
                lambda _=False, n=name: self._set_voucher_type(n))
            voucher_menu.addAction(action)
        voucher_menu.addSeparator()
        accept = QtGui.QAction("Accept voucher", self)
        accept.setShortcut(QtGui.QKeySequence("Ctrl+A"))
        accept.triggered.connect(self._accept)
        voucher_menu.addAction(accept)
        create_master = QtGui.QAction("Create ledger	Alt+L", self)
        create_master.triggered.connect(
            lambda: (self.show_screen(0), self.voucher.create_ledger()))
        voucher_menu.addAction(create_master)
        set_date = QtGui.QAction("Set date", self)
        set_date.setShortcut(QtGui.QKeySequence("F2"))
        set_date.triggered.connect(
            lambda: (self.show_screen(0), self.voucher.date_edit.setFocus()))
        voucher_menu.addAction(set_date)

        escape = QtGui.QAction("Back to voucher entry", self)
        escape.setShortcut(QtGui.QKeySequence("Esc"))
        escape.triggered.connect(lambda: self.show_screen(0))
        self.addAction(escape)

        refresh = QtGui.QAction("Refresh", self)
        refresh.setShortcut(QtGui.QKeySequence("F11"))
        refresh.triggered.connect(self.refresh_all)
        self.addAction(refresh)

        report_menu = bar.addMenu("&Report")
        print_action = QtGui.QAction("Print…", self)
        print_action.setShortcut(QtGui.QKeySequence("Ctrl+P"))
        print_action.triggered.connect(self.print_current)
        report_menu.addAction(print_action)
        export_action = QtGui.QAction("Export…", self)
        export_action.setShortcut(QtGui.QKeySequence("Ctrl+E"))
        export_action.triggered.connect(self.export_current)
        report_menu.addAction(export_action)

        company_menu = bar.addMenu("&Company")
        details = QtGui.QAction("Company details", self)
        details.triggered.connect(
            lambda: self.show_screen(len(self.screens) - 1))
        company_menu.addAction(details)
        company_menu.addSeparator()
        switch = self._switch_action = QtGui.QAction("Switch company…\tF3", self)
        switch.setShortcut(QtGui.QKeySequence("F3"))
        switch.setEnabled(not db.DB_PINNED)
        switch.setToolTip("Close these books and choose another company"
                          if not db.DB_PINNED else
                          "BOOKS_DB pins the data file for this session")
        switch.triggered.connect(self.switch_company)
        company_menu.addAction(switch)
        new_company = QtGui.QAction("New company…", self)
        new_company.setEnabled(not db.DB_PINNED)
        new_company.triggered.connect(self.new_company)
        company_menu.addAction(new_company)

        company_menu.addSeparator()
        backup = QtGui.QAction("Back up now\tCtrl+B", self)
        backup.setShortcut(QtGui.QKeySequence("Ctrl+B"))
        backup.setToolTip("Copy these books into the backup folder now. You "
                          "are also offered one every time the app closes.")
        backup.triggered.connect(self.backup_now)
        company_menu.addAction(backup)
        open_folder = QtGui.QAction("Open backup folder", self)
        open_folder.triggered.connect(self.open_backup_folder)
        company_menu.addAction(open_folder)

        company_menu.addSeparator()
        quit_action = QtGui.QAction("Exit\tCtrl+Q", self)
        quit_action.setShortcut(QtGui.QKeySequence("Ctrl+Q"))
        quit_action.setToolTip("Close Bookkeeper Plus. You will be offered a "
                               "backup on the way out.")
        # self.close(), NEVER app.quit(): quit tears down the event loop
        # without closing the window, so closeEvent never runs and the backup
        # offer - the whole point of closing cleanly - is skipped in silence.
        quit_action.triggered.connect(self.close)
        company_menu.addAction(quit_action)

        settings_menu = bar.addMenu("&Settings")

        size_menu = settings_menu.addMenu("&Text size")
        saved_text = theme.resolve_text(
            db.get_setting(theme.FONT_SETTING_KEY, theme.DEFAULT_TEXT))
        self._text_group = QtGui.QActionGroup(self)
        self._text_group.setExclusive(True)
        self._text_actions = {}
        for key in theme.TEXT_ORDER:
            action = QtGui.QAction(theme.TEXT_LABELS[key], self, checkable=True)
            action.setChecked(key == saved_text)
            action.triggered.connect(
                lambda _=False, k=key: self.set_text_size(k))
            self._text_group.addAction(action)
            size_menu.addAction(action)
            self._text_actions[key] = action

        # The familiar zoom keys, so you can nudge it while reading rather
        # than going back to the menu.
        size_menu.addSeparator()
        for label, shortcut, delta in (("Bigger", "Ctrl+=", 1),
                                       ("Smaller", "Ctrl+-", -1)):
            action = QtGui.QAction(label, self)
            action.setShortcut(QtGui.QKeySequence(shortcut))
            action.triggered.connect(
                lambda _=False, d=delta: self.step_text_size(d))
            size_menu.addAction(action)
            self.addAction(action)
        # Ctrl++ as well: on many layouts that is what Ctrl and the plus key
        # actually sends, and only Ctrl+= is bound above.
        plus = QtGui.QAction("Bigger", self)
        plus.setShortcut(QtGui.QKeySequence("Ctrl++"))
        plus.triggered.connect(lambda: self.step_text_size(1))
        self.addAction(plus)

        settings_menu.addSeparator()
        scheme_menu = settings_menu.addMenu("&Colour scheme")
        saved = db.get_setting(theme.SETTING_KEY, theme.DEFAULT)
        self._scheme_group = QtGui.QActionGroup(self)
        self._scheme_group.setExclusive(True)
        self._scheme_actions = {}
        for position, key in enumerate(theme.ORDER, start=1):
            action = QtGui.QAction(theme.LABELS[key], self, checkable=True)
            action.setChecked(key == saved)
            action.setShortcut(QtGui.QKeySequence(f"Ctrl+Shift+{position}"))
            action.triggered.connect(
                lambda _=False, k=key: self.set_color_scheme(k))
            self._scheme_group.addAction(action)
            scheme_menu.addAction(action)
            self._scheme_actions[key] = action

    def set_color_scheme(self, key: str) -> None:
        """Switch scheme, save it, and repaint what the stylesheet cannot.

        The muted labels follow the stylesheet on their own. The status lines
        do not - they were given a colour when a report last ran - so their
        stored tone is re-applied here rather than recomputing every report.
        """
        app = QtWidgets.QApplication.instance()
        if app is not None:
            theme.apply(app, key)
        db.set_setting(theme.SETTING_KEY, key)
        if key in self._scheme_actions:
            self._scheme_actions[key].setChecked(True)
        self._repaint_tones()
        scheme = theme.active()
        label = theme.LABELS.get(key, scheme.label)
        if key == theme.AUTO:
            label = f"{label} — {scheme.label} right now"
        self.statusBar().showMessage(f"Colour scheme: {label}")

    def _repaint_tones(self) -> None:
        for label in self.findChildren(QtWidgets.QLabel):
            token = label.property("tone")
            if token:
                label.setStyleSheet(theme.fg(token))

    # --- text size ------------------------------------------------------- #
    BASE_WIDTH = 1180
    BASE_HEIGHT = 760

    def _sized_to_text(self) -> QtCore.QSize:
        """The window size for the active text size.

        Grows with the text, but never past the display: at the largest step
        the scaled size came to 1967x1267, which is taller than a 1080p screen
        and would have put the Accept button under the taskbar. Clamped to the
        available desktop, then raised to whatever the layout genuinely needs.
        """
        scale = theme.text_scale()
        size = QtCore.QSize(round(self.BASE_WIDTH * scale),
                            round(self.BASE_HEIGHT * scale))
        screen = self.screen() or QtGui.QGuiApplication.primaryScreen()
        if screen is not None:
            size = size.boundedTo(screen.availableGeometry().size())
        return size.expandedTo(self.minimumSizeHint())

    def step_text_size(self, delta: int) -> None:
        """Move one step up or down the list, stopping at the ends."""
        order = list(theme.TEXT_ORDER)
        index = order.index(theme.active_text()) + delta
        index = max(0, min(len(order) - 1, index))
        self.set_text_size(order[index])

    def set_text_size(self, key: str) -> None:
        """Resize the text, then make the layout follow it.

        Setting the application font is only half the job. Three kinds of thing
        do not follow it on their own and are fixed up here:

        * widgets whose point size was set once in a constructor - the screen
          headings, the voucher-type description and the monospaced amount
          labels, marked with the "heading", "hint" and "mono" properties when
          they were built;
        * table rows and columns, whose measured heights and widths were taken
          against the old metrics;
        * the window itself, which would otherwise keep a size chosen for
          smaller text and push the bottom of the screen out of view.
        """
        app = QtWidgets.QApplication.instance()
        if app is None:
            return
        theme.apply_font(app, key)
        key = theme.active_text()
        db.set_setting(theme.FONT_SETTING_KEY, key)
        if key in self._text_actions:
            self._text_actions[key].setChecked(True)

        for widget in self.findChildren(QtWidgets.QWidget):
            if widget.property("heading"):
                font = widget.font()
                font.setPointSize(theme.heading_point_size())
                widget.setFont(font)
            elif widget.property("hint"):
                font = widget.font()
                font.setPointSize(theme.hint_point_size())
                widget.setFont(font)
            elif widget.property("mono"):
                widget.setFont(mono_font())
            elif widget.property("lines"):
                fit_lines(widget)

        # Rebuilds every table against the new metrics, which re-fonts the
        # amount items and re-measures the columns.
        self.refresh_all()
        for table in self.findChildren(QtWidgets.QTableWidget):
            table.resizeRowsToContents()

        if not (self.isMaximized() or self.isFullScreen()):
            self.resize(self._sized_to_text())
        self.statusBar().showMessage(
            f"Text size: {theme.TEXT_LABELS[key]} ({theme.point_size()}pt)")

    def _set_title(self) -> None:
        self.setWindowTitle(f"{db.company_display_name()} — Bookkeeper Plus")

    def ask_company(self) -> None:
        """Offer the company dropdown over the open window.

        This is what runs just after launch, and on F3. Choosing the company
        already open, or cancelling, simply leaves you where you are - only a
        DIFFERENT company closes and reopens the window.
        """
        if db.DB_PINNED:
            return
        picker = CompanyPicker(self, current=self.company_file)
        if not picker.exec() or picker.chosen is None:
            self._stay()
            return
        chosen = Path(picker.chosen)
        if chosen.name == self.company_file.name:
            self._stay()
            self.statusBar().showMessage(
                f"Staying in {db.company_display_name()}")
            return
        self._open_company(chosen)

    def switch_company(self) -> None:
        """F3: the same dropdown, asked for deliberately."""
        self.ask_company()

    def new_company(self) -> None:
        dialog = NewCompanyDialog(self)
        if not dialog.exec() or dialog.created is None:
            self._stay()
            return
        self._open_company(Path(dialog.created))

    def _stay(self) -> None:
        """Point the data layer back at the books this window is showing.

        Needed because creating a company switches to it: if the user then
        cancels, the window would be showing one company and every query would
        be answering from another.
        """
        db.use_company(self.company_file)

    def _open_company(self, path: Path) -> None:
        """Leave these books and reopen the window on another company.

        Closing and reopening is deliberate: rebuilding eight screens in place
        against a different database is a lot of state to get right, and a
        voucher part-entered against the OLD company is exactly the kind of
        thing that would survive it.
        """
        if not self._confirm_leaving("Switch company"):
            self._stay()
            return
        db.use_company(path)
        db.remember_last_company(path)
        self.switch_requested = True
        self.close()

    def part_entered_rows(self) -> list[int]:
        """Voucher rows with a ledger chosen but not yet accepted.

        Separate from the confirmation below so it can be tested without a
        modal dialog blocking the run.
        """
        return [row for row in range(self.voucher.grid.rowCount())
                if self.voucher.grid.cellWidget(row, 0)
                and self.voucher.grid.cellWidget(row, 0).currentText().strip()]

    def _confirm_leaving(self, what: str) -> bool:
        """Warn if a voucher is part-entered, since leaving discards it."""
        if not self.part_entered_rows():
            return True
        answer = QtWidgets.QMessageBox.question(
            self, what,
            f"A voucher is part-entered on the Voucher Entry screen. "
            f"{what} and it will be discarded.\n\nContinue?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        return answer == QtWidgets.QMessageBox.Yes

    # --- print and export the report on screen ---------------------------- #
    def current_report(self) -> Report | None:
        """What the screen in front of you would print, if it is a report.

        Voucher Entry, Masters and Company are not reports - they are input -
        so Ctrl+P on them says so rather than printing something arbitrary.
        """
        screen = self.screens[self.stack.currentIndex()][1]
        source = getattr(screen, "report", None)
        return source() if callable(source) else None

    def _not_a_report(self) -> None:
        self.statusBar().showMessage(
            "This screen is not a report - open Day Book, Ledger, Trial "
            "Balance, Profit & Loss or Balance Sheet first.", 6000)

    def print_current(self) -> None:
        report = self.current_report()
        if report is None:
            self._not_a_report()
            return
        preview_report(report, self)

    def export_current(self) -> None:
        report = self.current_report()
        if report is None:
            self._not_a_report()
            return
        export_and_open(report, self)

    # --- backups ---------------------------------------------------------- #
    def ask_backup_on_close(self) -> str:
        """Offer a backup on the way out. Returns "yes", "no" or "cancel".

        Asked at CLOSING time (user asked 2026-10-19) rather than taken
        silently at opening, so the copy holds the work just done instead of
        the state before it. The cost of that choice, stated here because it
        is the reason the manual Ctrl+B still matters: a close-time backup
        needs a clean exit, so a crash, a kill or a power cut leaves this
        session with no backup at all.

        Switching company comes through here too, since that closes the
        window - and it is the same "leaving these books" moment, so it gets
        the same offer. Only the wording differs: a dialog that says
        "closing" while you are switching is describing the wrong thing.

        Kept apart from `closeEvent` so the decision can be driven in a test.
        """
        leaving = "switching" if self.switch_requested else "closing"

        # Already safe: say so rather than SKIPPING the dialog. An earlier
        # version returned "no" silently here, on the reasoning that a Yes
        # which then wrote nothing would read as a failed backup. But closing
        # with no dialog at all reads exactly the same way, and worse, like
        # the feature is broken - which is how it was reported. Say what is
        # true instead: there is a copy, it matches, nothing needs writing.
        if db.backup_is_current(self.company_file):
            when = db.peek_settings(
                db.backups_for(self.company_file)[0], (db.TAKEN_KEY,)
            ).get(db.TAKEN_KEY, "")
            box = QtWidgets.QMessageBox(self)
            box.setWindowTitle("Already backed up")
            box.setIcon(QtWidgets.QMessageBox.Icon.Information)
            box.setText("These books are already backed up.")
            box.setInformativeText(
                (f"The copy taken at {when.replace('T', ' ')} is identical to "
                 if when else "The newest backup is identical to ")
                + f"{self.company_file.name} as it stands, so there is "
                f"nothing new to save.\n\nBackups are in {db.backup_dir()}.")
            close = box.addButton(
                "Switch" if self.switch_requested else "Close",
                QtWidgets.QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(close)
            box.exec()
            return "no" if box.clickedButton() is close else "cancel"

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle(f"Back up before {leaving}?")
        box.setIcon(QtWidgets.QMessageBox.Icon.Question)
        box.setText(f"Do you want to back up these books before {leaving}?")
        box.setInformativeText(
            f"This saves a copy of {self.company_file.name} as it stands now, "
            f"including everything entered this session.\n\n"
            f"{db.KEEP_BACKUPS} backups are kept, in {db.backup_dir()}.")
        yes = box.addButton(f"Back up and {'switch' if self.switch_requested else 'close'}",
                            QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        no = box.addButton(
            f"{'Switch' if self.switch_requested else 'Close'} without "
            f"backing up", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        # Backing up is the default, so Enter on the way out is the safe one.
        box.setDefaultButton(yes)
        box.exec()
        clicked = box.clickedButton()
        return "yes" if clicked is yes else "no" if clicked is no else "cancel"

    def closeEvent(self, event) -> None:
        """Offer the backup, and let Cancel call the whole thing off."""
        choice = self.ask_backup_on_close()
        if choice == "cancel":
            # Also cancels a company switch, which closes this window to get
            # there: staying means staying on THESE books.
            self.switch_requested = False
            self._stay()
            event.ignore()
            return
        if choice == "yes":
            try:
                made = db.backup_company(self.company_file, tag="close")
            except Exception as exc:                  # noqa: BLE001
                # LOUD on failure - a backup you believe is being taken and is
                # not is the exact shape of the data loss this project has
                # already had once, and it stayed invisible because nothing
                # ever said otherwise. Asked again rather than closing over it.
                again = QtWidgets.QMessageBox.warning(
                    self, "Could not back up",
                    f"These books were NOT backed up:\n\n{exc}\n\nYour books "
                    f"on disk are fine. Close anyway?",
                    QtWidgets.QMessageBox.StandardButton.Close
                    | QtWidgets.QMessageBox.StandardButton.Cancel,
                    QtWidgets.QMessageBox.StandardButton.Cancel)
                if again != QtWidgets.QMessageBox.StandardButton.Close:
                    self.switch_requested = False
                    self._stay()
                    event.ignore()
                    return
            else:
                if made is None:
                    self.statusBar().showMessage(
                        "Already backed up - nothing had changed.", 4000)
        super().closeEvent(event)

    def backup_now(self) -> None:
        """Company > Back up now, for before you do something you are unsure
        about."""
        try:
            made = db.backup_company(self.company_file, tag="manual")
        except Exception as exc:                      # noqa: BLE001
            QtWidgets.QMessageBox.warning(
                self, "Could not back up", f"Nothing was written:\n\n{exc}")
            return
        kept = len(db.backups_for(self.company_file))
        if made is None:
            # Not a failure, and saying "done" would be a lie - so say which.
            QtWidgets.QMessageBox.information(
                self, "Already backed up",
                "These books have not changed since the last backup, so "
                f"nothing new was written.\n\n{kept} backup(s) kept in:\n"
                f"{db.backup_dir()}")
            return
        self.statusBar().showMessage(f"Backed up to {made.name}", 6000)
        QtWidgets.QMessageBox.information(
            self, "Backed up",
            f"{db.company_display_name()} copied to:\n\n{made}\n\n"
            f"{kept} backup(s) kept; the oldest are dropped beyond "
            f"{db.KEEP_BACKUPS}.")

    def open_backup_folder(self) -> None:
        folder = db.backup_dir()
        folder.mkdir(parents=True, exist_ok=True)
        QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(folder)))

    def _company_changed(self) -> None:
        """Company details were saved: the title and every figure can change,
        since the currency symbol and the financial year both live there."""
        self._set_title()
        self.voucher.reload_ledgers()
        self.ledger.reload_ledgers()
        # The financial-year start month is on that screen, and it decides
        # what the years ARE - so the dropdowns have to be rebuilt, not just
        # the reports re-run.
        for _, widget in self.screens:
            bar = getattr(widget, "period", None)
            if isinstance(bar, PeriodBar):
                bar.reload_years()
        for _, widget in self.screens:
            if widget is not self.company and hasattr(widget, "refresh"):
                widget.refresh()

    def _masters_changed(self) -> None:
        """A master was created mid-voucher: refresh everything but the grid."""
        self.ledger.reload_ledgers()
        self.masters.refresh()
        for screen in (self.daybook, self.trial, self.profit, self.sheet):
            screen.refresh()

    def _set_voucher_type(self, name: str) -> None:
        self.show_screen(0)
        self.voucher.type_box.setCurrentText(name)

    def _accept(self) -> None:
        if self.stack.currentIndex() == 0 and self.voucher.accept_button.isEnabled():
            self.voucher.accept_voucher()

    def _alter(self, voucher_id: int) -> None:
        self.voucher.load_voucher(int(voucher_id))
        self.show_screen(0)

    def show_screen(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        widget = self.screens[index][1]
        if hasattr(widget, "reload_ledgers"):
            widget.reload_ledgers()
        if hasattr(widget, "refresh"):
            widget.refresh()

    def refresh_all(self) -> None:
        self.voucher.reload_ledgers()
        self.ledger.reload_ledgers()
        # The voucher and company screens hold INPUT, not a report. Refreshing
        # them here would discard a part-entered voucher or half-typed company
        # details the moment anything else in the app changed.
        for _, widget in self.screens:
            if widget not in (self.voucher, self.company) \
                    and hasattr(widget, "refresh"):
                widget.refresh()

        gap = acc.opening_difference()
        if abs(gap) > acc.EPS:
            self.statusBar().showMessage(
                f"Difference in opening balances: {money(abs(gap))} "
                f"{'Dr' if gap > 0 else 'Cr'} — shown as its own line on the "
                "Balance Sheet until the opening balances agree.")


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Bookkeeper Plus")
    theme.remember_native_style(app)
    theme.remember_base_font(app)

    # The app OPENS FIRST, on the company used last, and asks afterwards (user
    # asked 2026-10-16). Nothing is created on the way in: a fresh install
    # falls back to the default file, exactly as before this app knew about
    # more than one company, so starting up never demands a company be made.
    if not db.DB_PINNED:
        db.use_company(db.startup_company())

    ask_on_open = not db.DB_PINNED

    # One pass per company opened. Switching closes the window and comes back
    # here, which is far safer than rebuilding every screen in place against a
    # different database.
    while True:
        window = MainWindow()
        # No backup on the way IN any more (user asked 2026-10-19) - the window
        # offers one on the way OUT instead, via closeEvent, so the copy holds
        # the work just done. Switching company closes the window to come back
        # round this loop, so it gets the same offer.
        window.show()
        if ask_on_open:
            # After the window is up and painted, not before it.
            QtCore.QTimer.singleShot(0, window.ask_company)
        app.exec()
        if db.DB_PINNED or not window.switch_requested:
            return 0
        # A company was chosen; open it without asking all over again.
        ask_on_open = False


if __name__ == "__main__":
    raise SystemExit(main())
