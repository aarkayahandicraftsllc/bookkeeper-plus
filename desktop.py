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


# The copy button says which voucher it would copy, because the answer depends
# on where you are: the one you are altering, or the last one of the type the
# form is set to. A button that just said "Copy" would be a guess either way.
COPY_THIS = "Copy this voucher"
COPY_LAST = "Copy last {kind}"


def money(value: float) -> str:
    symbol = db.get_setting("currency", "₹")
    sign = "-" if value < 0 else ""
    return f"{sign}{symbol}{abs(value):,.2f}"


# Every report frame carries its dates as ISO strings, because that is what
# sorts correctly and what a spreadsheet can read. Only the two DISPLAY
# boundaries - a table on screen, and the printed page - rewrite them in the
# books' own format, so a column named any of these is formatted there.
# `Due` is the Outstanding report's due date, which is a date like any other.
DATE_COLUMNS = ("Date", "Due")


def shown_dates(frame: pd.DataFrame) -> pd.DataFrame:
    """A COPY of ``frame`` with its date columns in the books' own format.

    Always a copy, even with nothing to format: callers go on to rewrite their
    money columns in place, and handing one of them the caller's own frame
    would quietly reformat the screen's data behind it.
    """
    frame = frame.copy()
    columns = [c for c in DATE_COLUMNS if c in frame.columns]
    if not columns:
        return frame
    fmt = db.date_format()              # asked once, not once per cell
    for column in columns:
        frame[column] = [db.show_date(value, fmt) for value in frame[column]]
    return frame


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
# The row's own identity, carried on its first cell. A sorted table no longer
# has screen row 3 at frame row 3, and three places used to read the data by
# POSITION: double-click-to-alter on the Day Book and the Ledger, and Delete
# ledger on Masters - the last of which would have deleted whichever ledger
# happened to land on that line. The key travels with the row instead.
ROW_KEY = int(QtCore.Qt.ItemDataRole.UserRole) + 1

# Marks an amount this screen SUGGESTED rather than one that was typed.
# Only a suggestion may be overwritten by another suggestion, which is
# what keeps the feature from ever fighting the person entering.
SUGGESTED = int(QtCore.Qt.ItemDataRole.UserRole) + 3


class _Cell(QtWidgets.QTableWidgetItem):
    """A cell that sorts on a KEY rather than on what it says.

    Clicking a header has to sort by VALUE. An amount reads "1,234.00" and a
    date reads "19/10/2026"; sorted as text, 1,234 comes before 900 and
    October before March - a sortable header that did that would be worse
    than no sorting at all.
    """

    def __init__(self, text: str, key=None):
        super().__init__(text)
        self.key = text if key is None else key

    def __lt__(self, other):
        theirs = other.key if isinstance(other, _Cell) else other.text()
        try:
            return self.key < theirs
        except TypeError:          # mixed kinds, e.g. a label among numbers
            return str(self.key) < str(theirs)


def _amount_cell(value) -> tuple[str, float | str]:
    """An amount as it is shown, and what it sorts by."""
    if pd.isna(value):
        return "", 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        # Presentation text in a money column: the ledger's Balance column
        # carries "88,000.00 Dr". Shown and sorted as written.
        return str(value), str(value)
    return ("" if abs(number) < 0.005 else f"{number:,.2f}"), number


class FrameTable(QtWidgets.QTableWidget):
    """Displays a DataFrame. Numeric columns are right-aligned and formatted.

    Click a column header to sort by it, and again to reverse it (asked for
    2026-10-20). Sorting is OURS rather than Qt's `setSortingEnabled`, which
    sorts the moment it is switched on and would have thrown away each
    report's own order - the Day Book's newest-first above all - before the
    user had clicked anything.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.setSortingEnabled(False)
        self._sort: tuple[int, QtCore.Qt.SortOrder] | None = None
        self._pinned_last = False
        header = self.horizontalHeader()
        header.setSortIndicatorShown(True)
        header.setSectionsClickable(True)
        header.sectionClicked.connect(self.sort_by)

    def sort_by(self, column: int) -> None:
        """Sort by a column; clicking the same one again reverses it."""
        if self._pinned_last or not self.rowCount():
            return
        ascending = QtCore.Qt.SortOrder.AscendingOrder
        descending = QtCore.Qt.SortOrder.DescendingOrder
        order = (descending if self._sort == (column, ascending)
                 else ascending)
        self._sort = (column, order)
        self.sortItems(column, order)
        self.horizontalHeader().setSortIndicator(column, order)

    def key_at(self, row: int) -> object:
        """What the data layer calls the row now on this line of the screen."""
        item = self.item(row, 0)
        return None if item is None else item.data(ROW_KEY)

    def load(self, frame: pd.DataFrame, numeric: tuple[str, ...] = (),
             bold_last: bool = False, keys=None,
             sortable: bool = True) -> None:
        raw = frame.reset_index(drop=True)
        frame = shown_dates(raw)
        keys = None if keys is None else list(keys)
        # Two kinds of table cannot be sorted. One has a pinned total row,
        # which would be carried off into the middle of the figures it
        # totals - the Trial Balance keeps its total in a bar below the
        # table instead, which is what lets it be sorted. The other is an
        # INDENTED one: the detailed Balance Sheet lists accounts under
        # their heading, and a sort would scatter them away from it.
        self._pinned_last = bold_last or not sortable
        self.clear()
        self.setRowCount(len(frame))
        self.setColumnCount(len(frame.columns))
        self.setHorizontalHeaderLabels([str(c) for c in frame.columns])

        for row in range(len(frame)):
            for col, column in enumerate(frame.columns):
                value = frame.iloc[row, col]
                if column in numeric:
                    text, key = _amount_cell(value)
                    item = _Cell(text, key)
                    item.setTextAlignment(QtCore.Qt.AlignRight
                                          | QtCore.Qt.AlignVCenter)
                    item.setFont(mono_font())
                elif column in DATE_COLUMNS:
                    # Sorted on the ISO underneath, which is chronological as
                    # text; what is SHOWN is the books' own format.
                    item = _Cell("" if pd.isna(value) else str(value),
                                 str(raw.iloc[row, col]))
                else:
                    text = "" if pd.isna(value) else str(value)
                    # Case-folded, or ASCII puts every capital before every
                    # lower-case letter and the order looks arbitrary.
                    item = _Cell(text, text.casefold())
                if bold_last and row == len(frame) - 1:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self.setItem(row, col, item)
            if keys is not None and self.item(row, 0) is not None:
                self.item(row, 0).setData(ROW_KEY, keys[row])

        # A sort the user chose outlives a refresh: posting a voucher should
        # not quietly put the report back in its default order.
        if self._sort is not None and not self._pinned_last and len(frame):
            self.sortItems(*self._sort)
            self.horizontalHeader().setSortIndicator(*self._sort)

        header = self.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        if len(frame.columns):
            # One column takes the slack, and it has to be the one with the
            # most to say. This used to pick the FIRST non-numeric column,
            # which on the Day Book and the Ledger is `Date` - so a fixed
            # ten-character date was handed 600px of stretch while the ledger
            # names beside it were squeezed to fit. Measured in characters,
            # which is enough to choose between columns; the actual widths
            # are still Qt's.
            def room_for(index: int) -> int:
                column = frame.columns[index]
                if column in numeric:
                    return -1               # amounts are a known width
                lengths = [len(str(v)) for v in frame[column]]
                return max(lengths + [len(str(column))])

            header.setSectionResizeMode(
                max(range(len(frame.columns)), key=room_for),
                QtWidgets.QHeaderView.Stretch)


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

    No company field is ``multiline`` any more - the address became separate
    lines - but the kind is kept, since it is part of the CompanyField
    contract and the line-sizing it needs is a trap worth not relearning.
    `test_desktop.py` exercises it directly.
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
        editor.setDisplayFormat(db.date_format().qt)
        return editor
    if field.kind == "choice":
        editor = QtWidgets.QComboBox()
        editor.setEditable(True)            # so an unlisted symbol still works
        editor.addItems(list(field.choices))
        return editor
    if field.kind == "option":
        # Closed, unlike `choice`: a date format the code cannot render is not
        # a value, so there is nothing for a typed one to mean.
        editor = QtWidgets.QComboBox()
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
    # A TWO-SIDED statement: the sections are taken in pairs and written left
    # and right on ONE sheet, which is how a balance sheet is read. Without
    # this each side became a sheet of its own and the reader had to hold one
    # in their head while looking at the other.
    side_by_side: bool = False
    # Each section's own total, by heading. Needed because the Amount column
    # cannot always simply be summed: the detailed balance sheet carries a
    # heading's total AND the accounts beneath it, so a SUM() doubles the side.
    totals: dict[str, float] = field(default_factory=dict)


    def filename(self, suffix: str) -> str:
        stem = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")
        return f"{stem}-{date.today():%Y%m%d}{suffix}"


def period_caption(bar: "PeriodBar") -> str:
    """How a report's period reads on paper."""
    start, end = bar.range()
    fmt = db.date_format()
    if start is None:
        return f"as at {db.show_date(end, fmt)}"
    return f"{db.show_date(start, fmt)} to {db.show_date(end, fmt)}"


def _for_display(section: ReportSection) -> pd.DataFrame:
    """The section with its money columns written the way the screen shows
    them - grouped thousands, two decimals, and blank rather than 0.00.

    Dates go the same way, so the printed page reads as the screen does. The
    export path deliberately does NOT come through here: a spreadsheet wants a
    number it can sum and an ISO date it can sort, not either one as text.
    """
    frame = shown_dates(section.frame)
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
        if report.side_by_side:
            _csv_sides(writer, report)
            return
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


# Column widths for the two-sided sheet: a ledger name, a figure, a gutter.
_SIDE_WIDTHS = {"A": 38, "B": 16, "C": 3, "D": 38, "E": 16}


def _cell_text(value) -> str:
    return "" if pd.isna(value) else str(value)


def _export_excel_sides(report: Report, target: Path) -> None:
    """A two-sided statement on ONE sheet, laid out the way it is read.

    Asked for 2026-10-20: "balancesheet export format need to be more
    profession not in 2 seperate sheets, it should be in one sheet". So the
    sections are taken in pairs, left beside right, under one title block,
    each side ruled off and totalled.

    Figures stay NUMBERS with a format applied, never formatted text - a
    column of "1,234.00" strings is a spreadsheet you repair before you can
    use it, and that principle does not change because the layout got
    smarter.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    book = Workbook()
    sheet = book.active
    sheet.title = (re.sub(r"[\[\]:*?/\\]", " ", report.title)[:31].strip()
                   or "Report")

    rule = Side(style="thin", color="FF999999")
    heavy = Side(style="medium", color="FF333333")
    band = PatternFill("solid", fgColor="FFF2F2F2")
    money = "#,##0.00"
    right = Alignment(horizontal="right")

    for column, width in _SIDE_WIDTHS.items():
        sheet.column_dimensions[column].width = width

    # --- who, what, when, centred over the whole statement ---
    for row, (text, font) in enumerate((
            (db.company_display_name(), Font(bold=True, size=14)),
            (report.title, Font(bold=True, size=12)),
            (report.period, Font(italic=True, color="FF666666"))), start=1):
        sheet.merge_cells(start_row=row, start_column=1,
                          end_row=row, end_column=5)
        cell = sheet.cell(row=row, column=1, value=text)
        cell.font = font
        cell.alignment = Alignment(horizontal="center")

    at = 5
    for first in range(0, len(report.sections), 2):
        pair = report.sections[first:first + 2]
        left = pair[0]
        other = pair[1] if len(pair) > 1 else None

        for column, section in ((1, left), (4, other)):
            if section is None:
                continue
            head = sheet.cell(row=at, column=column,
                              value=section.heading or "Particulars")
            amount = sheet.cell(row=at, column=column + 1, value="Amount")
            for cell in (head, amount):
                cell.font = Font(bold=True)
                cell.fill = band
                cell.border = Border(bottom=heavy)
            amount.alignment = right

        rows = max(len(left.frame),
                   0 if other is None else len(other.frame))
        for offset in range(rows):
            for column, section in ((1, left), (4, other)):
                if section is None or offset >= len(section.frame):
                    continue
                particular = _cell_text(section.frame.iloc[offset, 0])
                value = section.frame.iloc[offset, 1]
                # The detailed balance sheet indents its accounts with spaces.
                # Excel has a real indent, which survives a column resize and
                # does not read as a typo.
                deep = particular.startswith(acc.DETAIL_INDENT)
                name = sheet.cell(row=at + 1 + offset, column=column,
                                  value=particular.strip())
                if deep:
                    name.alignment = Alignment(indent=2)
                else:
                    name.font = Font(bold=True) if rows and any(
                        _cell_text(v).startswith(acc.DETAIL_INDENT)
                        for v in section.frame.iloc[:, 0]) else Font()
                figure = sheet.cell(row=at + 1 + offset, column=column + 1,
                                    value=None if pd.isna(value) else float(value))
                figure.number_format = money

        total_row = at + 1 + rows
        for column, section in ((1, left), (4, other)):
            if section is None:
                continue
            label = sheet.cell(row=total_row, column=column, value="Total")
            figure = sheet.cell(row=total_row, column=column + 1,
                                value=report.totals.get(section.heading))
            for cell in (label, figure):
                cell.font = Font(bold=True)
                cell.border = Border(top=rule, bottom=heavy)
            figure.number_format = money
        at = total_row + 2

    for note in report.notes:
        if not note:
            continue
        sheet.merge_cells(start_row=at, start_column=1,
                          end_row=at, end_column=5)
        cell = sheet.cell(row=at, column=1, value=note)
        cell.font = Font(italic=True, size=9, color="FF666666")
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        at += 1

    sheet.sheet_view.showGridLines = False
    book.save(target)


def _csv_sides(writer, report: Report) -> None:
    """The two-sided layout again, so both exports read as one statement."""
    for first in range(0, len(report.sections), 2):
        pair = report.sections[first:first + 2]
        left = pair[0]
        other = pair[1] if len(pair) > 1 else None
        writer.writerow([left.heading or "Particulars", "Amount", "",
                         (other.heading if other else ""),
                         ("Amount" if other else "")])
        rows = max(len(left.frame), 0 if other is None else len(other.frame))
        for offset in range(rows):
            row = []
            for section in (left, other):
                if section is None or offset >= len(section.frame):
                    row += ["", ""]
                else:
                    value = section.frame.iloc[offset, 1]
                    row += [_cell_text(section.frame.iloc[offset, 0]),
                            "" if pd.isna(value) else value]
                if section is left:
                    row.append("")
            writer.writerow(row)
        writer.writerow(["Total", report.totals.get(left.heading), "",
                         "Total" if other else "",
                         report.totals.get(other.heading) if other else ""])
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
    if report.side_by_side:
        _export_excel_sides(report, target)
        return
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
class BillsDialog(QtWidgets.QDialog):
    """Attach one voucher line to bills: open a new one, or settle open ones.

    The party's open bills are listed with what is left on each, so settling
    is a matter of typing into the row rather than remembering a reference.
    Anything left unallocated is reported here and saved on account, not
    silently dropped - which is what the data layer does with it too.
    """

    def __init__(self, parent, *, party: str, ledger_id: int, amount: float,
                 existing: list[dict] | None = None,
                 suggested_ref: str = "", as_at=None):
        super().__init__(parent)
        self.setWindowTitle(f"Bills — {party}")
        self.ledger_id = int(ledger_id)
        # Signed, so the sign of every allocation follows the line it is on.
        self.amount = round(float(amount), 2)
        self.allocations: list[dict] = []

        layout = QtWidgets.QVBoxLayout(self)
        side = "Dr" if self.amount > 0 else "Cr"
        layout.addWidget(flowing(QtWidgets.QLabel(
            f"<b>{party}</b> &nbsp; {money(abs(self.amount))} {side} on this "
            f"line.")))

        self._open = acc.open_bills(self.ledger_id, as_at=as_at)
        # Only bills running the OTHER way can be settled by this line: a
        # receipt reduces what is owed. One running the same way would be
        # this line adding to a bill it is not part of.
        if not self._open.empty:
            self._open = self._open[
                self._open["Signed"] * self.amount < 0].reset_index(drop=True)

        self.table = QtWidgets.QTableWidget(len(self._open), 4)
        self.table.setHorizontalHeaderLabels(["Reference", "Due", "Pending",
                                              "Settle now"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        prefilled = {str(b.get("bill_ref")): b for b in (existing or [])
                     if b.get("kind") == "against"}
        for row in range(len(self._open)):
            bill = self._open.iloc[row]
            for col, text in ((0, str(bill["Ref"])),
                              (1, str(bill["Due"]) or "—"),
                              (2, f"{float(bill['Pending']):,.2f}")):
                item = QtWidgets.QTableWidgetItem(text)
                item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled
                              | QtCore.Qt.ItemFlag.ItemIsSelectable)
                if col == 2:
                    item.setTextAlignment(QtCore.Qt.AlignRight
                                          | QtCore.Qt.AlignVCenter)
                    item.setFont(mono_font())
                self.table.setItem(row, col, item)
            was = prefilled.get(str(bill["Ref"]))
            settle = QtWidgets.QTableWidgetItem(
                f"{abs(float(was['signed'])):,.2f}" if was else "")
            settle.setTextAlignment(QtCore.Qt.AlignRight
                                    | QtCore.Qt.AlignVCenter)
            settle.setFont(mono_font())
            self.table.setItem(row, 3, settle)
        self.table.itemChanged.connect(self._retotal)
        if len(self._open):
            layout.addWidget(QtWidgets.QLabel("Open bills — type what this "
                                              "line settles against each:"))
            layout.addWidget(self.table, 1)
        else:
            self.table.setVisible(False)
            layout.addWidget(flowing(QtWidgets.QLabel(
                "This party has no open bills that this line could settle.")))

        new_box = QtWidgets.QGroupBox("Open a new bill")
        form = QtWidgets.QFormLayout(new_box)
        was_new = next((b for b in (existing or [])
                        if b.get("kind") in ("new", "advance")), None)
        self.new_ref = QtWidgets.QLineEdit(
            str(was_new["bill_ref"]) if was_new else suggested_ref)
        self.new_ref.setPlaceholderText("invoice or bill number")
        self.new_ref.textChanged.connect(self._retotal)
        form.addRow("Reference", self.new_ref)
        self.new_due = QtWidgets.QDateEdit()
        self.new_due.setCalendarPopup(True)
        self.new_due.setDisplayFormat(db.date_format().qt)
        self.new_due.setSpecialValueText("none")
        self.new_due.setMinimumDate(QtCore.QDate(1900, 1, 1))
        if was_new and was_new.get("due_date"):
            self.new_due.setDate(to_qdate(str(was_new["due_date"])))
        else:
            self.new_due.setDate(self.new_due.minimumDate())
        form.addRow("Due", self.new_due)
        self.new_amount = QtWidgets.QLineEdit(
            f"{abs(float(was_new['signed'])):,.2f}" if was_new else "")
        self.new_amount.setPlaceholderText("leave blank to use whatever is left")
        self.new_amount.setFont(mono_font())
        self.new_amount.textChanged.connect(self._retotal)
        form.addRow("Amount", self.new_amount)
        layout.addWidget(new_box)

        self.summary = flowing(QtWidgets.QLabel())
        layout.addWidget(self.summary)

        buttons = QtWidgets.QHBoxLayout()
        oldest = QtWidgets.QPushButton("Apply to oldest")
        oldest.setToolTip("Fill the open bills from the oldest down, until "
                          "this line is used up")
        oldest.clicked.connect(self._apply_to_oldest)
        oldest.setEnabled(bool(len(self._open)))
        buttons.addWidget(oldest)
        clear = QtWidgets.QPushButton("Clear")
        clear.clicked.connect(self._clear)
        buttons.addWidget(clear)
        buttons.addStretch(1)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.ok = QtWidgets.QPushButton("Done")
        self.ok.setDefault(True)
        self.ok.clicked.connect(self._accept)
        buttons.addWidget(self.ok)
        layout.addLayout(buttons)

        self._retotal()

    # --- the running total ------------------------------------------------ #
    def _settled(self) -> float:
        total = 0.0
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 3)
            total = round(total + parse_amount(item.text() if item else ""), 2)
        return total

    def _new_amount(self) -> float:
        return parse_amount(self.new_amount.text())

    def _retotal(self, *_args) -> None:
        settled = self._settled()
        named = self._new_amount()
        # A reference with no amount means "whatever is left", which is the
        # common case: one invoice, one line, the whole amount.
        implied = (round(abs(self.amount) - settled, 2)
                   if self.new_ref.text().strip() and not named else 0.0)
        used = round(settled + named + max(implied, 0.0), 2)
        left = round(abs(self.amount) - used, 2)

        if used > abs(self.amount) + 0.005:
            self.summary.setText(
                f"Allocated {money(used)}, which is "
                f"{money(used - abs(self.amount))} more than the line.")
            tone(self.summary, "bad")
            self.ok.setEnabled(False)
            return
        self.ok.setEnabled(True)
        if abs(left) <= 0.005:
            self.summary.setText(f"All {money(abs(self.amount))} allocated.")
            tone(self.summary, "good")
        else:
            self.summary.setText(
                f"{money(left)} of {money(abs(self.amount))} is not against "
                f"any bill, and will sit on account.")
            tone(self.summary, "warn")

    # --- actions ---------------------------------------------------------- #
    def _apply_to_oldest(self) -> None:
        left = abs(self.amount) - self._new_amount()
        for row in range(self.table.rowCount()):
            pending = abs(float(self._open.iloc[row]["Pending"]))
            take = round(min(pending, max(left, 0.0)), 2)
            self.table.item(row, 3).setText(f"{take:,.2f}" if take else "")
            left = round(left - take, 2)
        self._retotal()

    def _clear(self) -> None:
        for row in range(self.table.rowCount()):
            self.table.item(row, 3).setText("")
        self.new_ref.clear()
        self.new_amount.clear()
        self._retotal()

    def _accept(self) -> None:
        sign = 1.0 if self.amount > 0 else -1.0
        out: list[dict] = []
        for row in range(self.table.rowCount()):
            take = parse_amount(self.table.item(row, 3).text()
                                if self.table.item(row, 3) else "")
            if not take:
                continue
            pending = abs(float(self._open.iloc[row]["Pending"]))
            if take > pending + 0.005:
                QtWidgets.QMessageBox.warning(
                    self, "More than the bill",
                    f"{money(take)} is more than the {money(pending)} left on "
                    f"{self._open.iloc[row]['Ref']}.")
                return
            out.append({"kind": "against",
                        "bill_ref": str(self._open.iloc[row]["Ref"]),
                        "signed": round(sign * take, 2), "due_date": ""})

        ref = self.new_ref.text().strip()
        if ref:
            named = self._new_amount()
            if not named:
                named = round(abs(self.amount) - self._settled(), 2)
            if named > 0.005:
                due = self.new_due.date()
                out.append({
                    "kind": "new", "bill_ref": ref,
                    "signed": round(sign * named, 2),
                    "due_date": ("" if due == self.new_due.minimumDate()
                                 else from_qdate(due).isoformat())})
        elif self._new_amount():
            QtWidgets.QMessageBox.warning(
                self, "No reference",
                "Give the new bill a reference, or it could never be settled "
                "later.")
            return

        # The remainder is deliberately NOT added here. The data layer
        # materialises it as an on-account allocation, so there is one place
        # that decides what unallocated money becomes.
        self.allocations = out
        self.accept()


class GstDialog(QtWidgets.QDialog):
    """Work out the GST on a voucher and say what lines it needs.

    Asks only what cannot be worked out: the rate and the place of supply.
    Whether that means CGST+SGST or IGST follows from the place of supply
    against our own state, so it is shown rather than asked.

    The dialog computes; `VoucherScreen.apply_gst` puts the lines in the
    grid. Keeping those apart means the arithmetic can be tested without
    driving a grid, and the grid stays the one place lines are written.
    """

    def __init__(self, parent, *, taxable: float, voucher_type: str,
                 party: str = "", party_gstin: str = "",
                 suggested_rate: float = 18.0,
                 place_of_supply: str = ""):
        super().__init__(parent)
        self.setWindowTitle("GST")
        self.taxable = round(float(taxable), 2)
        self.voucher_type = voucher_type
        self.party = party
        self.result_gst: dict | None = None

        layout = QtWidgets.QVBoxLayout(self)
        ours = db.company_state_code()
        layout.addWidget(flowing(QtWidgets.QLabel(
            f"Taxable value <b>{money(abs(self.taxable))}</b>"
            + (f" &nbsp;·&nbsp; {party}" if party else "")
            + (f" &nbsp;·&nbsp; we are in {db.state_name(ours)}" if ours
               else " &nbsp;·&nbsp; <b>our GSTIN is not set</b>, so every "
                    "supply will look local"))))

        form = QtWidgets.QFormLayout()
        self.rate = QtWidgets.QComboBox()
        self.rate.setEditable(True)
        self.rate.addItems([f"{r:g}" for r in db.GST_SLABS])
        self.rate.setCurrentText(f"{float(suggested_rate):g}")
        self.rate.currentTextChanged.connect(self._recompute)
        form.addRow("Rate %", self.rate)

        self.place = QtWidgets.QComboBox()
        self.place.addItem("Unregistered or unknown", "")
        for code, name in sorted(db.STATE_CODES.items(),
                                 key=lambda kv: kv[1]):
            self.place.addItem(f"{name} ({code})", code)
        wanted = place_of_supply or db.state_code_of(party_gstin) or ours
        if wanted:
            found = self.place.findData(wanted)
            if found >= 0:
                self.place.setCurrentIndex(found)
        self.place.currentTextChanged.connect(self._recompute)
        form.addRow("Place of supply", self.place)

        self.gstin = QtWidgets.QLineEdit(party_gstin)
        self.gstin.setPlaceholderText("the party's GSTIN, if they have one")
        self.gstin.textChanged.connect(self._gstin_typed)
        form.addRow("Party GSTIN", self.gstin)
        layout.addLayout(form)

        self.breakdown = QtWidgets.QLabel()
        apply_mono(self.breakdown)
        layout.addWidget(self.breakdown)
        self.note = flowing(QtWidgets.QLabel())
        layout.addWidget(self.note)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.ok = QtWidgets.QPushButton("Apply")
        self.ok.setDefault(True)
        self.ok.clicked.connect(self._accept)
        buttons.addWidget(self.ok)
        layout.addLayout(buttons)

        self._recompute()

    def _gstin_typed(self, text: str) -> None:
        """A GSTIN names its own state, so typing one sets the place."""
        code = db.state_code_of(text)
        if code:
            found = self.place.findData(code)
            if found >= 0 and self.place.currentData() != code:
                self.place.setCurrentIndex(found)
        self._recompute()

    def chosen_rate(self) -> float:
        return parse_amount(self.rate.currentText())

    def computed(self) -> dict:
        place = self.place.currentData() or ""
        kind = db.supply_type(place)
        tax = db.split_tax(abs(self.taxable), self.chosen_rate(), kind)
        return {"supply_type": kind, "place_of_supply": place, **tax,
                "rate": self.chosen_rate(),
                "total": round(sum(tax.values()), 2)}

    def _recompute(self, *_args) -> None:
        got = self.computed()
        if got["supply_type"] == "inter":
            self.breakdown.setText(
                f"IGST {money(got['igst'])}   "
                f"Invoice {money(abs(self.taxable) + got['total'])}")
            self.note.setText(
                "Interstate, so the whole rate goes on IGST.")
        else:
            self.breakdown.setText(
                f"CGST {money(got['cgst'])}   SGST {money(got['sgst'])}   "
                f"Invoice {money(abs(self.taxable) + got['total'])}")
            self.note.setText(
                "Within our state, so the rate is split between CGST and "
                "SGST.")
        tone(self.note, "muted")
        if not self.taxable:
            self.note.setText(
                "Nothing to tax yet — put the amount on the sales or purchase "
                "line first.")
            tone(self.note, "warn")
        self.ok.setEnabled(bool(self.taxable))

    def _accept(self) -> None:
        got = self.computed()
        self.result_gst = {
            "rate": got["rate"], "supply_type": got["supply_type"],
            "place_of_supply": got["place_of_supply"],
            "party_gstin": self.gstin.text().strip().upper(),
            "cgst": got["cgst"], "sgst": got["sgst"], "igst": got["igst"],
        }
        self.accept()


class VoucherScreen(QtWidgets.QWidget):
    """The screen you live in: type, date, number, then a Dr/Cr grid."""

    posted = QtCore.Signal()
    # Raised only when an EXISTING voucher was saved, so the window can go
    # back to the report it was opened from. A new voucher deliberately
    # does not: there you are entering a run of them.
    altered = QtCore.Signal()
    masters_changed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.editing_id: int | None = None
        self.editing_voided = False
        # Set before the grid exists, because writing a suggestion into it
        # raises itemChanged, which lands straight back in _amount_typed.
        self._loading = False
        self._suggesting = False
        # The GST facts of the voucher being entered: what the lines cannot
        # say. None until Alt+T has been through.
        self._gst: dict | None = None
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
        self.date_edit.setDisplayFormat(db.date_format().qt)
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
        self.grid.itemChanged.connect(self._amount_typed)
        # Moving between rows changes whether the Bills button means anything,
        # and nothing else would notice: the cells themselves have not changed.
        self.grid.currentCellChanged.connect(
            lambda *_a: self._recalculate())
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
        self.bills_button = QtWidgets.QPushButton("Bills…  (Alt+B)")
        self.bills_button.setToolTip(
            "Attach this line to a bill — open a new one, or settle open "
            "ones. Only for ledgers that keep bills.")
        self.bills_button.clicked.connect(self.edit_bills)
        self.gst_button = QtWidgets.QPushButton("GST…  (Alt+T)")
        self.gst_button.setToolTip(
            "Work out the GST on this voucher and put the tax lines on it. "
            "Sales and Purchase vouchers only.")
        self.gst_button.clicked.connect(self.edit_gst)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addWidget(create)
        buttons.addWidget(self.bills_button)
        buttons.addWidget(self.gst_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        # The totals get a row of their own. Sharing the button row, they put
        # Voucher Entry's minimum width at 1572px once a fourth button was
        # added - past the 1400px ceiling the whole app has to fit inside,
        # and wider than a 1366px laptop screen at the largest text size.
        totals_row = QtWidgets.QHBoxLayout()
        totals_row.addStretch(1)
        self.dr_label = QtWidgets.QLabel()
        self.cr_label = QtWidgets.QLabel()
        self.gap_label = QtWidgets.QLabel()
        for widget in (self.dr_label, self.cr_label, self.gap_label):
            apply_mono(widget)
            totals_row.addWidget(widget)
            totals_row.addSpacing(14)
        layout.addLayout(totals_row)

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
        # Not destructive, but it DOES replace whatever is in the grid, so it
        # keeps away from Accept with the rest of the voucher-level actions.
        self.copy_button = QtWidgets.QPushButton(
            COPY_LAST.format(kind="voucher"))
        self.copy_button.clicked.connect(self.copy_voucher)
        bottom.addWidget(self.copy_button)
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

        bills_key = QtGui.QShortcut(QtGui.QKeySequence("Alt+B"), self)
        bills_key.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        bills_key.activated.connect(self.edit_bills)

        gst_key = QtGui.QShortcut(QtGui.QKeySequence("Alt+T"), self)
        gst_key.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        gst_key.activated.connect(self.edit_gst)

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
        # The row's bill allocations live on its combo, so they travel with
        # the row when one is inserted or removed above it.
        combo._bills = []
        combo.currentTextChanged.connect(self._recalculate)
        # A bill reference belongs to a PARTY. Point the row at somebody else
        # and the old party's references would be saved against the new one.
        combo.currentTextChanged.connect(
            lambda _text, c=combo: self._ledger_swapped(c))
        return combo

    def _ledger_swapped(self, combo) -> None:
        if getattr(self, "_loading", False):
            return
        if getattr(combo, "_bills", []):
            combo._bills = []

    def _add_row(self, ledger: str = "", debit: float = 0.0,
                 credit: float = 0.0, bills: list[dict] | None = None) -> None:
        row = self.grid.rowCount()
        self.grid.insertRow(row)
        combo = self._ledger_combo()
        if ledger:
            combo.setCurrentText(ledger)
        # AFTER the name is set, because setting it fires the handler that
        # discards allocations when the row is pointed at another party.
        combo._bills = list(bills or [])
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
            # The allocations hang off the row's own combo widget, so they
            # follow the row when one is inserted or removed above it. Keyed
            # on the row NUMBER they would attach to the wrong line the
            # moment a row was deleted.
            rows.append({"ledger_id": self._ledger_ids[name],
                         "debit": debit, "credit": credit,
                         "bills": list(getattr(combo, "_bills", []))})
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

        self.bills_button.setEnabled(
            not self.editing_voided
            and self._bill_wise_row(self.grid.currentRow()) is not None)
        self.gst_button.setEnabled(
            not self.editing_voided
            and self.type_box.currentText() in (acc.OUTWARD + acc.INWARD))

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

    def _amount_typed(self, item) -> None:
        """Offer the balancing amount on the next line.

        Asked for 2026-10-20: "as soon as i enter the amount in first line as
        Debit/Credit it should be copied in next line unless i change it to
        make it a multiline journal". So the line below is offered whatever
        would make the voucher balance - for the ordinary two-line voucher
        that is the amount just typed, on the other side; for a journal being
        built up it is the REMAINDER, so changing the suggestion to 400 of
        1,000 leaves 600 offered on the line after.

        It never argues. A cell is only ever written if it is empty or holds
        an earlier suggestion of ours; anything typed by hand is left alone.
        """
        if self._loading or self._suggesting or item is None:
            return
        if item.column() not in (1, 2):
            return
        # Typed over: this figure is the user's now, not a suggestion.
        # Guarded, because setData raises itemChanged in its own right - and
        # unguarded this method re-entered, the inner pass wrote the
        # remainder, and the outer pass then saw a balanced voucher and
        # withdrew the suggestion it had just made.
        self._suggesting = True
        try:
            item.setData(SUGGESTED, None)
        finally:
            self._suggesting = False
        self._suggest_balance(item.row() + 1)

    def _suggest_balance(self, row: int) -> None:
        if row < 0 or row >= 30:
            return
        # Read from the CELLS, not from row_values(), which skips a line
        # whose ledger is not chosen yet. The amount is often typed before
        # the ledger, and a suggestion that appeared only sometimes would be
        # harder to trust than one that always does.
        gap = 0.0
        for index in range(self.grid.rowCount()):
            debit = self.grid.item(index, 1)
            credit = self.grid.item(index, 2)
            gap += parse_amount(debit.text() if debit else "")
            gap -= parse_amount(credit.text() if credit else "")
        gap = round(gap, 2)

        self._suggesting = True
        try:
            # The spare line below is added when a LEDGER is filled in, not an
            # amount, so on a two-line voucher there is nowhere to put the
            # remainder yet. A gap means another line is needed regardless, so
            # it is added - but only then, never on a voucher that balances.
            if row >= self.grid.rowCount():
                if abs(gap) <= 0.005:
                    return
                self._add_row()
            self._write_suggestion(row, gap)
        finally:
            self._suggesting = False
        self._recalculate()

    def _write_suggestion(self, row: int, gap: float) -> None:
        cells = [self.grid.item(row, 1), self.grid.item(row, 2)]
        if any(cell is None for cell in cells):
            return
        # Both of the line's amount cells have to be ours to write into, or a
        # figure someone entered would be moved or erased.
        if any(cell.text().strip() and cell.data(SUGGESTED) is None
               for cell in cells):
            return

        wanted = ["", ""]
        if gap > 0.005:                       # debits lead: offer a credit
            wanted[1] = f"{gap:,.2f}"
        elif gap < -0.005:
            wanted[0] = f"{-gap:,.2f}"
        # gap of nil leaves both blank, clearing a suggestion that has been
        # made good some other way - a stale one would be read as a figure.

        for cell, text in zip(cells, wanted):
            if cell.text() == text:
                continue
            cell.setText(text)
            cell.setData(SUGGESTED, True if text else None)

    def _type_changed(self, name: str) -> None:
        if self.editing_id is None:
            self.number_label.setText(
                f"No. {db.next_voucher_number(name)}")
        self.hint.setText(db.VOUCHER_TYPES.get(name, ""))
        # Both the label and whether it is enabled depend on the type, and
        # every path that changes the type or resets the form comes through
        # here - so this is the one place it needs saying.
        self._update_copy_button()
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
        self._gst = None
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

    def _update_copy_button(self) -> None:
        """Say which voucher the button would copy, and whether there is one."""
        if self.editing_id is not None:
            self.copy_button.setText(COPY_THIS)
            self.copy_button.setEnabled(True)
            self.copy_button.setToolTip(
                "Start a new voucher with these same lines, dated today. "
                "Nothing is saved until you accept it.")
            return
        kind = self.type_box.currentText()
        self.copy_button.setText(COPY_LAST.format(kind=kind.lower()))
        available = db.last_voucher_id(kind) is not None
        self.copy_button.setEnabled(available)
        self.copy_button.setToolTip(
            f"Fill this form from the last {kind.lower()} voucher, dated "
            f"today - for an entry that repeats. Nothing is saved until you "
            f"accept it." if available else
            f"There is no {kind.lower()} voucher in these books yet to copy.")

    def copy_voucher(self) -> None:
        """Start a NEW voucher with the lines of an existing one.

        Asked for 2026-10-20, for recurring entries: "I need a button there to
        create a copy the same voucher again to avoid typing everything again"
        and "a button in voucher entry for copy last voucher". Those are two
        ways into the same thing, so it is one button that copies whichever
        voucher is in front of you - the one being altered, or the last one of
        the type the form is set to.

        It is a DRAFT. Nothing is written until Accept, the number is the next
        one for the type, and the date is today - which is the whole point of
        a recurring entry, and the one field you would otherwise always have
        to change.
        """
        kind = self.type_box.currentText()
        source = (self.editing_id if self.editing_id is not None
                  else db.last_voucher_id(kind))
        if source is None:
            QtWidgets.QMessageBox.information(
                self, "Nothing to copy",
                f"There is no {kind.lower()} voucher in these books yet.")
            return
        record = db.get_voucher(int(source))
        if not record:
            return
        head, lines = record

        # Replacing a part-typed voucher without asking would lose the typing,
        # the same reason switching company warns first.
        if self.editing_id is None and self.row_values():
            if QtWidgets.QMessageBox.question(
                    self, "Replace what you have typed?",
                    f"This form already has entries. Replace them with a copy "
                    f"of {head['voucher_type'].lower()} voucher no. "
                    f"{head['number']}?") != \
                    QtWidgets.QMessageBox.StandardButton.Yes:
                return

        self.reset()                      # a copy is a new voucher, not an edit
        self._loading = True
        self.type_box.setCurrentText(str(head["voucher_type"]))
        self.narration.setText(str(head["narration"]))
        self._loading = False
        # Bills are deliberately NOT carried over: repeating an invoice
        # means a NEW bill, not a second posting against the old one.
        # The GST rate and place of supply DO come, because a recurring
        # invoice is taxed the same way as the one it repeats.
        self._gst = dict(db.get_gst_details(int(source)) or {})
        self._fill_lines(lines, bills=False)
        self._update_copy_button()
        window = self.window()
        if isinstance(window, QtWidgets.QMainWindow):
            window.statusBar().showMessage(
                f"Copied {head['voucher_type'].lower()} voucher no. "
                f"{head['number']}. Check the date, then accept.", 8000)

    def _fill_lines(self, lines, bills: bool = True) -> None:
        """Replace the grid with these lines, plus the one spare at the end.

        THE ORDER MATTERS. `_recalculate` keeps a spare blank line under a
        filled last row and `_add_row` ends by calling it, so the flag has to
        stay on for the whole loop: cleared before it, as `load_voucher` did
        until 2026-10-20, every loaded line appended a blank one after itself
        and the grid came out line, blank, line, blank.

        On-account allocations are never re-loaded: the data layer puts them
        back from whatever is left unallocated, so carrying them in would
        count them twice. `bills=False` drops the bill allocations entirely,
        which is what COPYING a voucher wants - a repeat of an invoice is a
        new bill, not another posting against the old one.
        """
        self._loading = True
        self.grid.setRowCount(0)
        for line in lines:
            self._add_row(str(line["ledger_name"]), float(line["debit"]),
                          float(line["credit"]),
                          [b for b in line.get("bills", [])
                           if b.get("kind") != "on_account"] if bills else [])
        self._loading = False
        self._recalculate()

    def load_voucher(self, voucher_id: int) -> None:
        record = db.get_voucher(voucher_id)
        if not record:
            return
        head, lines = record
        self._loading = True
        self.reload_ledgers()
        self.editing_id = int(head["id"])
        self.editing_voided = bool(head.get("voided", 0))
        # So that re-accepting an invoice keeps its rate and place of supply
        # rather than silently dropping it off the return.
        self._gst = db.get_gst_details(int(head["id"]))
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
        self._update_copy_button()
        self._fill_lines(lines)

    def _ledger_row(self, name: str):
        """That ledger's master row, or None."""
        lid = self._ledger_ids.get(name)
        if lid is None or self._ledgers.empty:
            return None
        found = self._ledgers[self._ledgers["id"].astype(int) == int(lid)]
        return None if found.empty else found.iloc[0]

    def _taxable_rows(self) -> list[int]:
        """Grid rows on a ledger that carries a GST rate.

        A rate of 0 counts: nil-rated and exempt supplies are taxable at
        zero and still belong on the return, which is why the column is
        nullable rather than defaulting to 0.
        """
        rows = []
        for row in range(self.grid.rowCount()):
            combo = self.grid.cellWidget(row, 0)
            master = self._ledger_row(combo.currentText().strip()
                                      if combo else "")
            if master is None or str(master.get("gst_role") or ""):
                continue
            rate = master.get("gst_rate")
            if rate is not None and not pd.isna(rate):
                rows.append(row)
        return rows

    def _row_amount(self, row: int) -> float:
        debit = parse_amount(self.grid.item(row, 1).text()
                             if self.grid.item(row, 1) else "")
        credit = parse_amount(self.grid.item(row, 2).text()
                              if self.grid.item(row, 2) else "")
        return round(debit - credit, 2)

    def _set_row_amount(self, row: int, signed: float) -> None:
        self._loading = True
        for col, value in ((1, signed if signed > 0 else 0.0),
                           (2, -signed if signed < 0 else 0.0)):
            item = self.grid.item(row, col)
            if item is None:
                item = QtWidgets.QTableWidgetItem()
                item.setTextAlignment(QtCore.Qt.AlignRight
                                      | QtCore.Qt.AlignVCenter)
                item.setFont(mono_font())
                self.grid.setItem(row, col, item)
            item.setText(f"{value:,.2f}" if value else "")
        self._loading = False

    def _tax_rows(self) -> list[int]:
        rows = []
        for row in range(self.grid.rowCount()):
            combo = self.grid.cellWidget(row, 0)
            master = self._ledger_row(combo.currentText().strip()
                                      if combo else "")
            if master is not None and str(master.get("gst_role") or ""):
                rows.append(row)
        return rows

    def edit_gst(self) -> None:
        """Alt+T: work out the GST and put the tax lines on the voucher."""
        if self.type_box.currentText() not in (acc.OUTWARD + acc.INWARD):
            QtWidgets.QMessageBox.information(
                self, "Not a taxable voucher",
                "GST goes on a Sales or Purchase voucher. Use the voucher "
                "type for the supply and the tax follows.")
            return
        taxable_rows = self._taxable_rows()
        if not taxable_rows:
            QtWidgets.QMessageBox.information(
                self, "Nothing taxable on this voucher",
                "Put the amount on a sales or purchase ledger that carries a "
                "GST rate. The rate is set on the ledger, on the Masters "
                "screen.")
            return
        taxable = round(sum(self._row_amount(r) for r in taxable_rows), 2)

        # The party is the line running the OTHER way - the one the invoice
        # total lands on.
        party_rows = [r for r in range(self.grid.rowCount())
                      if r not in taxable_rows and r not in self._tax_rows()
                      and self._row_amount(r)
                      and (self._row_amount(r) > 0) != (taxable > 0)]
        party_row = party_rows[0] if party_rows else None
        party_master = (self._ledger_row(
            self.grid.cellWidget(party_row, 0).currentText().strip())
            if party_row is not None else None)

        rates = {float(self._ledger_row(
            self.grid.cellWidget(r, 0).currentText().strip())["gst_rate"])
            for r in taxable_rows}
        existing = self._gst or {}
        dialog = GstDialog(
            self, taxable=taxable, voucher_type=self.type_box.currentText(),
            party=str(party_master["name"]) if party_master is not None else "",
            party_gstin=str(existing.get("party_gstin")
                            or (party_master.get("gstin")
                                if party_master is not None else "") or ""),
            suggested_rate=float(existing.get("rate") or
                                 (sorted(rates)[-1] if rates else 18.0)),
            place_of_supply=str(existing.get("place_of_supply")
                                or (party_master.get("state_code")
                                    if party_master is not None else "") or ""))
        if len(rates) > 1:
            QtWidgets.QMessageBox.information(
                self, "More than one rate",
                "The taxable lines on this voucher carry different rates, and "
                "GST is worked out at ONE rate per voucher. The highest is "
                "offered; split the invoice if that is wrong.")
        if not dialog.exec() or dialog.result_gst is None:
            return
        got = dialog.result_gst

        # Out with the old tax lines before the new, so changing the rate
        # replaces them rather than stacking another set underneath.
        for row in sorted(self._tax_rows(), reverse=True):
            self.grid.removeRow(row)

        roles = db.gst_ledger_ids()
        prefix = ("output" if self.type_box.currentText() in acc.OUTWARD
                  else "input")
        by_id = self._ledgers.set_index("id")
        side = 1.0 if taxable > 0 else -1.0
        tax_total = 0.0
        for part in ("cgst", "sgst", "igst"):
            amount = round(float(got.get(part) or 0), 2)
            if not amount:
                continue
            lid = roles.get(f"{prefix}_{part}")
            if lid is None or lid not in by_id.index:
                continue
            # Tax sits on the SAME side as the supply; the party carries the
            # whole invoice on the other.
            self._add_row(str(by_id.loc[lid, "name"]),
                          amount if side > 0 else 0.0,
                          amount if side < 0 else 0.0)
            tax_total = round(tax_total + amount, 2)

        if party_row is not None:
            self._set_row_amount(party_row, -(taxable + side * tax_total))
            # The party line's bill allocations were for the old total.
            combo = self.grid.cellWidget(party_row, 0)
            if getattr(combo, "_bills", []):
                combo._bills = []

        self._gst = {
            "rate": got["rate"], "supply_type": got["supply_type"],
            "place_of_supply": got["place_of_supply"],
            "party_gstin": got["party_gstin"],
            "party_ledger_id": (int(party_master["id"])
                                if party_master is not None else None),
        }
        self._recalculate()

    def _bill_wise_row(self, row: int) -> tuple[int, str] | None:
        """The ledger on that row, if it keeps bills. Otherwise None."""
        if row < 0 or row >= self.grid.rowCount():
            return None
        combo = self.grid.cellWidget(row, 0)
        name = combo.currentText().strip() if combo else ""
        lid = self._ledger_ids.get(name)
        if lid is None or self._ledgers.empty:
            return None
        found = self._ledgers[self._ledgers["id"].astype(int) == int(lid)]
        if found.empty or not bool(found.iloc[0].get("bill_wise", 0)):
            return None
        return int(lid), name

    def edit_bills(self) -> None:
        """Alt+B: allocate the current line to bills."""
        row = self.grid.currentRow()
        found = self._bill_wise_row(row)
        if found is None:
            QtWidgets.QMessageBox.information(
                self, "No bills here",
                "Put the cursor on a line whose ledger keeps bills — a "
                "customer or supplier. Bill-wise is a tick on the ledger, on "
                "the Masters screen.")
            return
        lid, name = found
        combo = self.grid.cellWidget(row, 0)
        debit = parse_amount(self.grid.item(row, 1).text()
                             if self.grid.item(row, 1) else "")
        credit = parse_amount(self.grid.item(row, 2).text()
                              if self.grid.item(row, 2) else "")
        amount = round(debit - credit, 2)
        if not amount:
            QtWidgets.QMessageBox.information(
                self, "No amount yet",
                "Put the amount on the line first — the bills have to add up "
                "to something.")
            return

        dialog = BillsDialog(
            self, party=name, ledger_id=lid, amount=amount,
            existing=list(getattr(combo, "_bills", [])),
            # A line that INCREASES what the party owes is raising a bill, so
            # the voucher's own number is the obvious reference to offer.
            suggested_ref=(self.number_label.text().removeprefix("No. ").strip()
                           if not getattr(combo, "_bills", []) else ""),
            as_at=from_qdate(self.date_edit.date()))
        if not dialog.exec():
            return
        combo._bills = dialog.allocations
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
                gst=self._gst,
            )
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "Cannot accept", str(exc))
            return
        total = sum(r["debit"] for r in rows)
        was_editing = self.editing_id is not None
        self.reset()
        self.posted.emit()
        if was_editing:
            self.altered.emit()
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
            editor.setDisplayFormat(db.date_format().qt)
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
    TODAY = "Today"
    CUSTOM = "Custom dates…"
    # Marks the since-books-began entry. The years carry a FinancialYear and
    # Custom carries None, so the three are told apart by what they hold
    # rather than by their position in a list that changes with the data.
    ALL = "all"
    NOW = "now"

    def __init__(self, with_from: bool = True, today_first: bool = False,
                 parent=None):
        super().__init__(parent)
        self.with_from = with_from
        # Opt-in, and only Outstanding asks for it. An ageing report drawn at
        # the END of the financial year - which is where the year dropdown
        # lands by default, and a date still in the future - shows every bill
        # as months overdue and a bill not yet due as long past it. The other
        # as-at reports keep opening on the current year, which is what was
        # asked for them: a year named in a dropdown means that whole year.
        self.today_first = today_first
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
            self.from_edit.setDisplayFormat(db.date_format().qt)
            self.from_edit.dateChanged.connect(self._date_typed)
            row.addWidget(self.from_edit)

        row.addWidget(QtWidgets.QLabel("To" if with_from else "As at"))
        self.to_edit = QtWidgets.QDateEdit(to_qdate(date.today()))
        self.to_edit.setCalendarPopup(True)
        self.to_edit.setDisplayFormat(db.date_format().qt)
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
        if self.today_first:
            self.year_box.addItem(self.TODAY, self.NOW)
        if self.with_from:
            self.year_box.addItem(self.SINCE, self.ALL)
        for year in years:
            self.year_box.addItem(f"FY {year.label}", year)
        self.year_box.addItem(self.CUSTOM, None)

        index = self.year_box.findText(wanted) if wanted else -1
        if index < 0 and self.today_first:
            index = self.year_box.findText(self.TODAY)
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
        if chosen == self.NOW:
            if self.with_from:
                self.from_edit.setEnabled(False)
            self.to_edit.setDate(to_qdate(date.today()))
        elif chosen == self.ALL:
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
            shown = pd.DataFrame(columns=["Date", "Debit account",
                                          "Credit account", "Vch Type",
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
        self.table.load(shown, numeric=("Debit", "Credit"),
                        keys=self._frame["voucher_id"] if not frame.empty
                        else [])

    def report(self) -> Report:
        kind = self.type_filter.currentText()
        return Report(
            title="Day Book",
            period=period_caption(self.period)
            + ("" if kind == "All" else f"  ·  {kind} vouchers only"),
            sections=[ReportSection("", self._shown, ("Debit", "Credit"))],
            notes=[self.count.text()])

    def _open(self, index) -> None:
        # Read from the row, not from its position: once the table is sorted,
        # screen row 3 is not frame row 3, and this would open whichever
        # voucher happened to land on the line that was clicked.
        key = self.table.key_at(index.row())
        if key is not None:
            self.open_voucher.emit(int(key))


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
        self.table.load(shown, numeric=("Debit", "Credit"),
                        keys=self._frame["voucher_id"]
                        if not self._frame.empty else [])

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
        # Read from the row, not from its position: once the table is sorted,
        # screen row 3 is not frame row 3, and this would open whichever
        # voucher happened to land on the line that was clicked.
        key = self.table.key_at(index.row())
        if key is not None:
            self.open_voucher.emit(int(key))


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
        # The total used to be the last ROW of the table, bold. It moved out
        # here so the table can be sorted by clicking a header: a total row
        # sorts like any other and would be carried off into the middle of
        # the figures it totals. The Ledger has had this bar all along, and
        # the printed report still carries the Total as a row.
        self.footer_slot = QtWidgets.QVBoxLayout()
        self.footer_slot.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.footer_slot)
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
            # The screen shows the rows alone, so they can be sorted; the
            # REPORT keeps the Total as its last row, because a printed trial
            # balance without its total is not a trial balance.
            self._shown = pd.concat([rows, pd.DataFrame([total])],
                                    ignore_index=True)
            self.table.load(rows, numeric=("Debit", "Credit"))

        while self.footer_slot.count():
            item = self.footer_slot.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.footer_slot.addWidget(totals_bar(
            ("Total debit", f"{trial['debit']:,.2f}"),
            ("Total credit", f"{trial['credit']:,.2f}")))

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
        # Summary is the default: a balance sheet is read at the heading level
        # first, and the accounts are what you open up when a heading looks
        # wrong. Same shape as the Trial Balance's "Summarise by group".
        self.detailed = QtWidgets.QCheckBox(
            "Show the accounts under each heading")
        self.detailed.toggled.connect(self.refresh)
        layout.addWidget(self.detailed)
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
        self._totals: dict[str, float] = {}
        self._lines: list[str] = []

    def report(self) -> Report:
        blank = pd.DataFrame(columns=["Particulars", "Amount"])
        return Report(
            title="Balance Sheet",
            period=period_caption(self.period)
            + ("  ·  accounts shown under each heading"
               if self.detailed.isChecked() else ""),
            sections=[
                ReportSection("Liabilities",
                              self._sides.get("liabilities", blank),
                              ("Amount",)),
                ReportSection("Assets", self._sides.get("assets", blank),
                              ("Amount",))],
            notes=self._lines + [self._note],
            # Read left and right on one sheet, and told its own totals -
            # the detailed form's Amount column cannot be summed.
            side_by_side=True,
            totals={"Liabilities": self._totals.get("liabilities", 0.0),
                    "Assets": self._totals.get("assets", 0.0)})

    def refresh(self) -> None:
        _, as_at = self.period.range()
        detailed = self.detailed.isChecked()
        sheet = acc.balance_sheet(as_at, detailed=detailed)
        totals = sheet["totals"]
        self.liability_label.setText(
            f"<b>Liabilities</b> &nbsp; {money(totals['liabilities'])}")
        self.asset_label.setText(f"<b>Assets</b> &nbsp; {money(totals['assets'])}")
        self.liabilities.load(sheet["liabilities"], numeric=("Amount",),
                              sortable=not detailed)
        self.assets.load(sheet["assets"], numeric=("Amount",),
                         sortable=not detailed)
        self._sides = {"liabilities": sheet["liabilities"],
                       "assets": sheet["assets"]}
        self._totals = dict(totals)
        self._lines = [
            f"Liabilities {money(totals['liabilities'])}"
            f"  ·  Assets {money(totals['assets'])}"]
        if detailed:
            # Says so on the page and in the export, where someone WILL put a
            # SUM() down the Amount column and get twice the side.
            self._lines.append(
                "Each heading is followed by the accounts that make it up, so "
                "the Amount column holds both; the side totals above are the "
                "ones to read.")
        if abs(totals["difference"]) <= acc.EPS:
            self.status.setText("Both sides agree.")
            tone(self.status, "good")
        else:
            self.status.setText("The two sides do not agree — that should be "
                                "impossible, please report it.")
            tone(self.status, "bad")


class OutstandingScreen(QtWidgets.QWidget):
    """Who owes what, how overdue it is, and anything sitting unallocated."""

    open_voucher = QtCore.Signal(int)

    # Shown on screen and in the printed report; the rest of what
    # acc.outstanding() returns is for reconciling, not for reading.
    BILL_COLUMNS = ["Party", "Ref", "Date", "Due", "Amount", "Pending",
                    "Age", "Bucket"]

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(heading("Outstanding"))

        # Opens on TODAY, not on the financial year's closing day: ageing is
        # measured from the date the report is drawn at, and the year's end is
        # still in the future for the year in progress.
        self.period = PeriodBar(with_from=False, today_first=True)
        self.period.changed.connect(self.refresh)
        layout.addWidget(self.period)

        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("Age by"))
        self.age_by = QtWidgets.QComboBox()
        self.age_by.addItems(list(acc.AGE_BY))
        self.age_by.setToolTip(
            "Due date answers 'how late is this?'. Bill date answers 'how "
            "long have we carried it?'. Bills with no due date age from "
            "their own date either way.")
        self.age_by.currentTextChanged.connect(self.refresh)
        controls.addWidget(self.age_by)
        controls.addSpacing(18)
        self.show_side = QtWidgets.QComboBox()
        self.show_side.addItems(["Receivable and payable", "Receivable only",
                                 "Payable only"])
        self.show_side.currentTextChanged.connect(self.refresh)
        controls.addWidget(self.show_side)
        controls.addStretch(1)
        layout.addLayout(controls)

        # Its own row. Sharing the controls row, this one unwrapped mono label
        # was asking for 760px by itself and put the whole screen's minimum at
        # 1429px, past the 1400px ceiling - the tables are not the problem,
        # they scroll. An unwrapped label is almost always what pins a screen
        # open; see flowing().
        totals_row = QtWidgets.QHBoxLayout()
        totals_row.addStretch(1)
        self.totals = QtWidgets.QLabel()
        apply_mono(self.totals)
        totals_row.addWidget(self.totals)
        layout.addLayout(totals_row)

        self.status = flowing(QtWidgets.QLabel())
        layout.addWidget(self.status)

        self.table = FrameTable()
        self.table.doubleClicked.connect(self._open)
        layout.addWidget(self.table, 2)

        layout.addWidget(flowing(QtWidgets.QLabel(
            "Double-click a bill to open the voucher that raised it.")))

        self.ageing_label = QtWidgets.QLabel("Ageing")
        layout.addWidget(self.ageing_label)
        self.ageing = FrameTable()
        layout.addWidget(self.ageing, 1)

        self.loose_label = QtWidgets.QLabel("On account, against no bill")
        layout.addWidget(self.loose_label)
        self.loose = FrameTable()
        layout.addWidget(self.loose, 1)

        layout.addWidget(ReportActions(self.report))
        self._bills = pd.DataFrame()
        self._shown = pd.DataFrame()
        self._ageing = pd.DataFrame()
        self._loose = pd.DataFrame()

    def report(self) -> Report:
        sections = [ReportSection("Bills outstanding", self._shown,
                                  ("Amount", "Pending"))]
        if not self._ageing.empty:
            sections.append(ReportSection("Ageing", self._ageing,
                                          ("Receivable", "Payable")))
        if not self._loose.empty:
            sections.append(ReportSection("On account, against no bill",
                                          self._loose, ("Pending",)))
        return Report(
            title="Outstanding",
            period=period_caption(self.period)
            + f"  ·  aged by {self.age_by.currentText()}"
            + ("" if self.show_side.currentIndex() == 0
               else f"  ·  {self.show_side.currentText().lower()}"),
            sections=sections,
            # The totals and the warning live in LABELS, not in any table, so
            # a printed report without them loses the figures that were wanted.
            notes=[self.totals.text(), self.status.text()])

    def refresh(self) -> None:
        _, as_at = self.period.range()
        result = acc.outstanding(as_at=as_at, age_by=self.age_by.currentText())
        bills, loose = result["bills"], result["on_account"]

        keep = {"Receivable only": "asset", "Payable only": "liability"}.get(
            self.show_side.currentText())
        if keep:
            bills = bills[bills["nature"] == keep] if not bills.empty else bills
            loose = loose[loose["nature"] == keep] if not loose.empty else loose

        self._bills = bills.reset_index(drop=True)
        self._shown = (self._bills[self.BILL_COLUMNS] if not bills.empty
                       else pd.DataFrame(columns=self.BILL_COLUMNS))
        self.table.load(self._shown, numeric=("Amount", "Pending"),
                        keys=(list(zip(self._bills["ledger_id"],
                                       self._bills["Ref"]))
                              if not self._bills.empty else []))

        self._ageing = (result["ageing"] if not result["ageing"].empty
                        else pd.DataFrame(columns=["Bucket", "Receivable",
                                                   "Payable"]))
        self.ageing.load(self._ageing, numeric=("Receivable", "Payable"))
        self.ageing_label.setVisible(not self._ageing.empty)
        self.ageing.setVisible(not self._ageing.empty)

        self._loose = (loose[["Party", "Pending"]].assign(
            Pending=loose["Pending"].abs().round(2)).reset_index(drop=True)
            if not loose.empty else pd.DataFrame(columns=["Party", "Pending"]))
        self.loose.load(self._loose, numeric=("Pending",))
        self.loose_label.setVisible(not self._loose.empty)
        self.loose.setVisible(not self._loose.empty)

        totals = result["totals"]
        self.totals.setText(
            f"Receivable {money(totals['receivable'])}"
            f"   Payable {money(totals['payable'])}")

        overdue = (self._bills[self._bills["Age"] > 0]
                   if not self._bills.empty else self._bills)
        if self._shown.empty and self._loose.empty:
            self.status.setText(
                "No bills outstanding. A bill appears here once a voucher on "
                "a bill-wise ledger opens a reference — see the Bills button "
                "on Voucher Entry.")
            tone(self.status, "muted")
        elif not overdue.empty:
            worst = overdue.iloc[0]
            self.status.setText(
                f"{len(overdue)} bill(s) overdue, the oldest "
                f"{int(worst['Age'])} days — {worst['Party']} "
                f"{worst['Ref']} for {money(float(worst['Pending']))}.")
            tone(self.status, "bad")
        else:
            self.status.setText("Nothing is overdue.")
            tone(self.status, "good")

        if not self._loose.empty:
            self.status.setText(
                self.status.text()
                + f"  {money(float(self._loose['Pending'].sum()))} is on "
                f"account against no bill, so nobody can be chased for it.")
            tone(self.status, "warn")

    def _open(self, index) -> None:
        key = self.table.key_at(index.row())
        if key is not None:
            ref = {"ledger_id": key[0], "Ref": key[1]}
            found = db.get_allocations()
            found = found[(found["ledger_id"] == int(ref["ledger_id"]))
                          & (found["bill_ref"] == ref["Ref"])
                          & (found["kind"].isin(["new", "advance"]))]
            if not found.empty:
                self.open_voucher.emit(int(found.iloc[0]["voucher_id"]))


class GstScreen(QtWidgets.QWidget):
    """What GST was charged, what was paid, and what is left to hand over."""

    open_voucher = QtCore.Signal(int)

    SUMMARY = "Summary"
    OUTWARD = "Outward supplies"
    INWARD = "Inward supplies"

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(heading("GST"))

        # A period, not an as-at: GST is returned for a month or a quarter.
        self.period = PeriodBar()
        self.period.changed.connect(self.refresh)
        layout.addWidget(self.period)

        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("Show"))
        self.view = QtWidgets.QComboBox()
        self.view.addItems([self.SUMMARY, self.OUTWARD, self.INWARD])
        self.view.setToolTip(
            "Summary is what you owe. The two registers list it invoice by "
            "invoice, the shape a return asks for.")
        self.view.currentTextChanged.connect(self.refresh)
        controls.addWidget(self.view)
        controls.addStretch(1)
        layout.addLayout(controls)

        # Its own row: an unwrapped mono label is what pins a screen open.
        totals_row = QtWidgets.QHBoxLayout()
        totals_row.addStretch(1)
        self.totals = QtWidgets.QLabel()
        apply_mono(self.totals)
        totals_row.addWidget(self.totals)
        layout.addLayout(totals_row)

        self.status = flowing(QtWidgets.QLabel())
        layout.addWidget(self.status)

        self.first_label = QtWidgets.QLabel()
        layout.addWidget(self.first_label)
        self.first = FrameTable()
        self.first.doubleClicked.connect(self._open)
        layout.addWidget(self.first, 2)

        self.second_label = QtWidgets.QLabel()
        layout.addWidget(self.second_label)
        self.second = FrameTable()
        layout.addWidget(self.second, 1)

        self.third_label = QtWidgets.QLabel()
        layout.addWidget(self.third_label)
        self.third = FrameTable()
        layout.addWidget(self.third, 1)

        layout.addWidget(ReportActions(self.report))
        self._sections: list[ReportSection] = []
        self._register = pd.DataFrame()

    def report(self) -> Report:
        return Report(
            title=f"GST — {self.view.currentText()}",
            period=period_caption(self.period),
            sections=self._sections,
            # The totals and the warning are LABELS, so a printed report
            # without them loses the figure that was wanted.
            notes=[self.totals.text(), self.status.text()])

    def _show(self, sections: list[tuple[str, pd.DataFrame, tuple]],
              keys=None) -> None:
        self._sections = [ReportSection(h, f, n) for h, f, n in sections]
        for index, ((label, table), part) in enumerate(zip(
                ((self.first_label, self.first),
                 (self.second_label, self.second),
                 (self.third_label, self.third)),
                sections + [("", pd.DataFrame(), ())] * 3)):
            heading_text, frame, numeric = part
            label.setText(heading_text)
            label.setVisible(bool(heading_text) and not frame.empty)
            table.setVisible(bool(heading_text) and not frame.empty)
            if not frame.empty:
                table.load(frame, numeric=numeric,
                           keys=keys if index == 0 else None)

    def refresh(self) -> None:
        start, end = self.period.range()
        summary = acc.gst_summary(start, end)
        totals = summary["totals"]
        self.totals.setText(
            f"Output {money(totals['output_tax'])}"
            f"   Input {money(totals['input_tax'])}"
            f"   {'Payable' if totals['net'] >= 0 else 'Credit'}"
            f" {money(abs(totals['net']))}")

        view = self.view.currentText()
        if view == self.SUMMARY:
            self._register = pd.DataFrame()
            money_cols = ("Taxable", "CGST", "SGST", "IGST", "Tax")
            self._show([
                ("Outward supplies, by rate", summary["outward"], money_cols),
                ("Inward supplies, by rate", summary["inward"], money_cols),
                ("Outward supplies, by where they went",
                 summary["by_supply"], ("Taxable", "Tax")),
            ])
        else:
            self._register = acc.gst_register(start, end,
                                              outward=view == self.OUTWARD)
            shown = (self._register.drop(columns=["voucher_id"])
                     if not self._register.empty else self._register)
            self._show([(view, shown,
                         ("Taxable", "CGST", "SGST", "IGST", "Total"))],
                       keys=(list(self._register["voucher_id"])
                             if not self._register.empty else None))

        if not totals["output_tax"] and not totals["input_tax"]:
            self.status.setText(
                "No GST on any voucher in this period. Put a rate on your "
                "sales and purchase ledgers, then use GST (Alt+T) on a Sales "
                "or Purchase voucher.")
            tone(self.status, "muted")
        elif totals["net"] >= 0:
            self.status.setText(
                f"{money(totals['net'])} of GST is payable — "
                f"{money(totals['output_tax'])} collected on sales, less "
                f"{money(totals['input_tax'])} already paid on purchases.")
            tone(self.status, "warn")
        else:
            self.status.setText(
                f"{money(abs(totals['net']))} of input credit is left over, "
                f"having paid more GST on purchases than was collected on "
                f"sales. It carries forward.")
            tone(self.status, "good")

        if not db.company_state_code():
            self.status.setText(
                self.status.text()
                + "  Our own GSTIN is not set on the Company screen, so every "
                  "supply is being treated as local and no IGST will ever be "
                  "worked out.")
            tone(self.status, "bad")

    def _open(self, index) -> None:
        key = self.first.key_at(index.row())
        if key is not None:
            self.open_voucher.emit(int(key))


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

        # GST. A plain spin box could not express the difference that
        # matters: BLANK means the ledger is not taxable, 0 means it is
        # taxable at nil rate - and a nil-rated supply still goes on the
        # return.
        self.led_rate = QtWidgets.QLineEdit()
        self.led_rate.setPlaceholderText("blank if not taxable; 0 for nil "
                                         "rated")
        self.led_gstin = QtWidgets.QLineEdit()
        self.led_gstin.setPlaceholderText("a customer's or supplier's GSTIN")
        self.led_state = QtWidgets.QComboBox()
        self.led_state.addItem("From the GSTIN", "")
        for code, name in sorted(db.STATE_CODES.items(),
                                 key=lambda kv: kv[1]):
            self.led_state.addItem(f"{name} ({code})", code)
        form.addWidget(QtWidgets.QLabel("GST rate %"), 2, 0)
        form.addWidget(self.led_rate, 2, 1)
        form.addWidget(QtWidgets.QLabel("GSTIN"), 2, 2)
        form.addWidget(self.led_gstin, 2, 3)
        form.addWidget(QtWidgets.QLabel("State"), 3, 0)
        form.addWidget(self.led_state, 3, 1)
        ledger_layout.addLayout(form)

        buttons = QtWidgets.QHBoxLayout()
        # The form above does double duty: it creates, and when a ledger is
        # picked for editing it fills with that ledger and this button saves
        # it instead. Same shape as altering a voucher.
        self.led_save = QtWidgets.QPushButton("Create ledger")
        self.led_save.clicked.connect(self.save_ledger)
        create = self.led_save
        self.led_edit = QtWidgets.QPushButton("Edit selected")
        self.led_edit.clicked.connect(self.edit_ledger)
        self.led_cancel = QtWidgets.QPushButton("Cancel edit")
        self.led_cancel.clicked.connect(self.stop_editing_ledger)
        self.led_cancel.setVisible(False)
        delete = QtWidgets.QPushButton("Delete selected")
        delete.clicked.connect(self.delete_ledger)
        buttons.addWidget(self.led_edit)
        buttons.addWidget(self.led_cancel)
        # The only way to change an EXISTING ledger from this screen. Without
        # it a rate could only ever be set on a brand new ledger, which is no
        # use to books that already have their customers in them.
        apply_gst = QtWidgets.QPushButton("Set GST on selected")
        apply_gst.setToolTip("Put the GST rate, GSTIN and state above onto "
                             "the ledger selected in the list")
        apply_gst.clicked.connect(self.apply_gst_to_selected)
        buttons.addWidget(create)
        buttons.addWidget(apply_gst)
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
        self.grp_save = QtWidgets.QPushButton("Create group")
        self.grp_save.clicked.connect(self.save_group)
        group_form.addWidget(self.grp_save)
        self.grp_edit = QtWidgets.QPushButton("Edit selected")
        self.grp_edit.clicked.connect(self.edit_group)
        group_form.addWidget(self.grp_edit)
        self.grp_cancel = QtWidgets.QPushButton("Cancel edit")
        self.grp_cancel.clicked.connect(self.stop_editing_group)
        self.grp_cancel.setVisible(False)
        group_form.addWidget(self.grp_cancel)
        group_layout.addLayout(group_form)
        tabs.addTab(group_page, "Groups")

        self.led_hint = muted(flowing(QtWidgets.QLabel()))
        ledger_layout.addWidget(self.led_hint)
        self.grp_hint = muted(flowing(QtWidgets.QLabel(
            "A group's nature follows the group it sits under. The 27 "
            "reserved groups cannot be renamed or moved - the reports are "
            "built on them by name - so put a sub-group under one instead.")))
        group_layout.addWidget(self.grp_hint)
        # The master being edited, by name, or None while creating.
        self._editing_ledger: str | None = None
        self._editing_active = True
        self._editing_group: str | None = None
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
        self.ledger_table.load(view, numeric=("Opening",),
                               keys=ledgers["name"])
        self._ledger_names = list(ledgers["name"])

        groups = db.get_groups()
        self.group_table.load(pd.DataFrame({
            "Group": groups["name"],
            "Under": groups["parent_name"].fillna("—"),
            "Nature": groups["nature"],
            "Trading": groups["affects_gross"].map({1: "yes", 0: ""}),
            "Reserved": groups["is_reserved"].map({1: "yes", 0: ""}),
        }), keys=groups["name"])
        self._group_names = list(groups["name"])
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

    def _typed_rate(self) -> float | None:
        """The rate box as a number, or None when it is blank."""
        text = self.led_rate.text().strip().rstrip("%").strip()
        if not text:
            return None
        try:
            return round(float(text), 2)
        except ValueError:
            raise ValueError(f"{self.led_rate.text()!r} is not a GST rate. "
                             f"Leave it blank if the ledger is not taxable.")

    def _selected(self, table, known) -> str | None:
        """The name on the highlighted line, or None."""
        row = table.currentRow()
        name = table.key_at(row) if row >= 0 else None
        return str(name) if name and name in known else None

    def edit_ledger(self) -> None:
        """Fill the form from the picked ledger, so the form saves it."""
        name = self._selected(self.ledger_table, self._ledger_names)
        if name is None:
            self._warn(ValueError("Pick a ledger in the list first."))
            return
        row = db.get_ledgers(include_inactive=True).set_index("name").loc[name]
        self._editing_ledger = name
        # Kept rather than defaulted: `update_ledger` takes active=True, so a
        # ledger switched off in the browser version would be switched back on
        # by saving it here, which nothing on this screen asked for.
        self._editing_active = bool(row.get("active", 1))
        self.led_name.setText(name)
        self.led_group.setCurrentText(str(row["group_name"]))
        self.led_open.setValue(float(row["opening"]))
        self.led_side.setCurrentText(str(row["opening_side"]))
        # The GST boxes are filled too, so what the form shows is what saving
        # it will write - including a rate taken back off.
        rate = row.get("gst_rate")
        self.led_rate.setText("" if rate is None or pd.isna(rate)
                              else f"{float(rate):g}")
        self.led_gstin.setText(str(row.get("gstin") or ""))
        code = str(row.get("state_code") or "")
        self.led_state.setCurrentIndex(
            max(0, self.led_state.findData(code)) if code else 0)
        self._ledger_mode()
        self.led_name.setFocus()

    def stop_editing_ledger(self) -> None:
        self._editing_ledger = None
        self._editing_active = True
        self.led_name.clear()
        self.led_open.setValue(0)
        self.led_rate.clear()
        self.led_gstin.clear()
        self.led_state.setCurrentIndex(0)
        self._ledger_mode()

    def _ledger_mode(self) -> None:
        editing = self._editing_ledger is not None
        self.led_save.setText("Save changes" if editing else "Create ledger")
        self.led_cancel.setVisible(editing)
        self.led_edit.setEnabled(not editing)
        self.led_hint.setText(
            f"Editing {self._editing_ledger} — its vouchers and bills are "
            f"untouched, they follow the ledger." if editing else "")

    def save_ledger(self) -> None:
        """Create, or save the ledger being edited - whichever the form is."""
        try:
            if self._editing_ledger is None:
                db.add_ledger(self.led_name.text(),
                              self.led_group.currentText(),
                              self.led_open.value(),
                              self.led_side.currentText(),
                              gst_rate=self._typed_rate(),
                              gstin=self.led_gstin.text().strip().upper(),
                              state_code=self.led_state.currentData() or "")
            else:
                ledger = db.ledger_id(self._editing_ledger)
                db.update_ledger(ledger, self.led_name.text(),
                                 self.led_group.currentText(),
                                 self.led_open.value(),
                                 self.led_side.currentText(),
                                 active=self._editing_active)
                # Through set_ledger_gst, not update_ledger: there None means
                # "leave it alone", and clearing the rate box has to be able
                # to mean "not taxable".
                db.set_ledger_gst(
                    ledger, rate=self._typed_rate(),
                    gstin=self.led_gstin.text().strip().upper(),
                    state_code=self.led_state.currentData() or "")
        except ValueError as exc:
            self._warn(exc)
            return
        self.stop_editing_ledger()
        self.refresh()
        self.changed.emit()

    def apply_gst_to_selected(self) -> None:
        """Put the GST boxes onto the ledger picked in the list."""
        row = self.ledger_table.currentRow()
        name = self.ledger_table.key_at(row) if row >= 0 else None
        # By POSITION this read self._ledger_names[row], which after a sort
        # named a different ledger than the highlighted one - and wrote GST
        # settings onto it.
        if not name or name not in self._ledger_names:
            self._warn(ValueError("Pick a ledger in the list first."))
            return
        try:
            rate = self._typed_rate()
        except ValueError as exc:
            self._warn(exc)
            return
        try:
            # set_ledger_gst, not update_ledger: there None means "leave
            # alone", so a blank rate box could never CLEAR a rate.
            db.set_ledger_gst(
                db.ledger_id(name), rate=rate,
                gstin=self.led_gstin.text().strip().upper(),
                state_code=self.led_state.currentData() or "")
        except ValueError as exc:
            self._warn(exc)
            return
        self.refresh()
        self.changed.emit()
        # Restored AFTER the emit, not before: changed is wired to the
        # window's refresh_all(), which rebuilds this table again and would
        # wipe a selection put back any earlier. Without this, setting GST on
        # a second ledger needs re-picking it, and the click before that
        # quietly acts on nothing.
        if name in self._ledger_names:
            self.ledger_table.setCurrentCell(self._ledger_names.index(name), 0)
        window = self.window()
        if isinstance(window, QtWidgets.QMainWindow):
            window.statusBar().showMessage(
                f"GST details set on {name}"
                + (f" — taxable at {rate:g}%" if rate is not None
                   else " — not taxable"), 6000)

    def delete_ledger(self) -> None:
        row = self.ledger_table.currentRow()
        if row < 0:
            return
        name = self.ledger_table.key_at(row)
        # By POSITION this deleted whichever ledger had been sorted onto the
        # highlighted line, rather than the one shown as highlighted.
        if not name or name not in self._ledger_names:
            return
        try:
            db.delete_ledger(db.ledger_id(name))
        except ValueError as exc:
            self._warn(exc)
            return
        self.refresh()
        self.changed.emit()

    # --- groups ---------------------------------------------------------- #
    def edit_group(self) -> None:
        name = self._selected(self.group_table, self._group_names)
        if name is None:
            self._warn(ValueError("Pick a group in the list first."))
            return
        row = db.get_groups().set_index("name").loc[name]
        if int(row["is_reserved"]):
            self._warn(ValueError(
                f"{name} is one of the reserved groups. The reports are built "
                f"on these by name, so they cannot be renamed or moved - "
                f"create a sub-group under it instead."))
            return
        self._editing_group = name
        self.grp_name.setText(name)
        parent = row.get("parent_name")
        self.grp_parent.setCurrentText("" if pd.isna(parent) else str(parent))
        self._group_mode()
        self.grp_name.setFocus()

    def stop_editing_group(self) -> None:
        self._editing_group = None
        self.grp_name.clear()
        self._group_mode()

    def _group_mode(self) -> None:
        editing = self._editing_group is not None
        self.grp_save.setText("Save changes" if editing else "Create group")
        self.grp_cancel.setVisible(editing)
        self.grp_edit.setEnabled(not editing)

    def save_group(self) -> None:
        try:
            if self._editing_group is None:
                db.add_group(self.grp_name.text(), self.grp_parent.currentText())
            else:
                db.update_group(self._editing_group, self.grp_name.text(),
                                self.grp_parent.currentText())
        except ValueError as exc:
            self._warn(exc)
            return
        self.stop_editing_group()
        self.refresh()
        self.changed.emit()


# --------------------------------------------------------------------------- #
# choosing a company at launch
# --------------------------------------------------------------------------- #
# Only the fields needed to open a usable set of books. The rest - address,
# PAN, GSTIN - are filled in afterwards on the Company screen, so creating a
# company is not an eighteen-field form before you can enter a single voucher.
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
                 f"last changed {db.show_date(changed.date())}"
                 f" {changed.strftime('%H:%M')}"]
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

        # Eighteen rows at the largest text size is taller than the window, so
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
            elif field.kind in ("choice", "option"):
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
            elif field.kind in ("choice", "option"):
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
        self.outstanding = OutstandingScreen()
        self.gst = GstScreen()
        self.masters = MastersScreen()
        self.company = CompanyScreen()

        self.screens = [
            ("Voucher Entry", self.voucher),
            ("Day Book", self.daybook),
            ("Ledger", self.ledger),
            ("Trial Balance", self.trial),
            ("Profit && Loss", self.profit),
            ("Balance Sheet", self.sheet),
            ("Outstanding", self.outstanding),
            ("GST", self.gst),
            ("Masters", self.masters),
            ("Company", self.company),
        ]
        for _, widget in self.screens:
            self.stack.addWidget(widget)

        self._altered_from: int | None = None
        self.voucher.posted.connect(self.refresh_all)
        self.voucher.altered.connect(self._back_from_alter)
        self.voucher.masters_changed.connect(self._masters_changed)
        self.masters.changed.connect(self.refresh_all)
        self.daybook.open_voucher.connect(self._alter)
        self.ledger.open_voucher.connect(self._alter)
        self.outstanding.open_voucher.connect(self._alter)
        self.gst.open_voucher.connect(self._alter)
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
        # A QDateEdit's display format is set ONCE, when it is built, so every
        # editor on every other screen would go on showing the old order after
        # the date format was changed. The tables re-read it by themselves,
        # because Table.load formats dates as it fills them.
        shown = db.date_format().qt
        for editor in self.findChildren(QtWidgets.QDateEdit):
            editor.setDisplayFormat(shown)
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
        # Remembered so that saving the alteration returns to the report it
        # was opened from (user asked 2026-10-20): you were reading the Day
        # Book and went to fix one line, not starting a new voucher.
        self._altered_from = self.stack.currentIndex()
        self.voucher.load_voucher(int(voucher_id))
        self.show_screen(0)

    def _back_from_alter(self) -> None:
        """Go back to the report the altered voucher was opened from."""
        index, self._altered_from = self._altered_from, None
        if index is not None and index != 0:
            self.show_screen(index)

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
