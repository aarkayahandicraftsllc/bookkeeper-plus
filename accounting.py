"""Reports: balances, ledger statements, trial balance, P&L and balance sheet.

Everything works from one idea: a ledger's **closing balance** is its opening
balance plus every voucher line against it, held debit-positive. A positive
closing is a debit balance, a negative one a credit balance.

Because every voucher balances, the transactions always net to zero, so:

    sum of all closing balances  ==  sum of all opening balances

If that total is not zero, the opening balances themselves disagree. Rather
than hide it, the trial balance and the balance sheet both show the residual as
**Difference in opening balances**, which is what Tally does.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import NamedTuple

import pandas as pd

import db

EPS = 0.005

# the primary groups that make up each side of the balance sheet, in the order
# Tally prints them
LIABILITY_SIDE = ["Capital Account", "Loans (Liability)", "Current Liabilities",
                  "Suspense A/c"]
ASSET_SIDE = ["Fixed Assets", "Investments", "Current Assets",
              "Misc. Expenses (Asset)"]


# --------------------------------------------------------------------------- #
# financial years
# --------------------------------------------------------------------------- #
# A financial year is not the calendar year: in India it runs April to March,
# and the month it opens on is a company setting, so every one of these is
# derived from ``fy_start_month`` rather than assumed. Shared by both front
# ends, which each had their own partial copy of this arithmetic before.
class FinancialYear(NamedTuple):
    label: str
    start: date
    end: date

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


def fy_start_month() -> int:
    """The month the financial year opens on, 1-12. April unless told
    otherwise, and anything out of range falls back to April rather than
    producing a year that cannot exist."""
    try:
        month = int(db.get_setting("fy_start_month", "4"))
    except ValueError:
        return 4
    return month if 1 <= month <= 12 else 4


def fy_bounds(day: date, start_month: int | None = None) -> tuple[date, date]:
    """First and last day of the financial year containing ``day``."""
    if start_month is None:
        start_month = fy_start_month()
    start_year = day.year if day.month >= start_month else day.year - 1
    start = date(start_year, start_month, 1)
    # A year starting in January ends that same December; any other start
    # month ends in the month BEFORE it, the following calendar year.
    end_month = 12 if start_month == 1 else start_month - 1
    end_year = start_year if start_month == 1 else start_year + 1
    end = date(end_year, end_month,
               calendar.monthrange(end_year, end_month)[1])
    return start, end


def fy_label(start: date, start_month: int | None = None) -> str:
    """'2025-2026' - or just '2026' for books that run the calendar year,
    because '2026-2026' would be nonsense."""
    if start_month is None:
        start_month = fy_start_month()
    if start_month == 1:
        return str(start.year)
    return f"{start.year}-{start.year + 1}"


def financial_years() -> list[FinancialYear]:
    """Every financial year these books touch, earliest first.

    Derived from the data - the voucher dates and the books-begin date - so
    the list grows by itself as vouchers are entered and there is nothing to
    maintain. The year containing TODAY is always included even in empty
    books, because that is the one you are about to post into.

    Opening balances carry no date of their own, which is what
    ``books_begin`` is for: without it, books whose vouchers all fall in the
    current year would offer no way to look at the year the openings came
    from.
    """
    today = date.today()
    month = fy_start_month()
    # Today's year, and the one before it. The previous year is offered even
    # with nothing posted in it: comparing against last year is routine, and a
    # balance sheet as at its close is meaningful from the opening balances
    # alone. Earlier years than that have to be earned by the data.
    marks = [today, fy_bounds(today, month)[0] - timedelta(days=1)]

    begins = db.get_setting("books_begin", "")
    if begins:
        try:
            marks.append(date.fromisoformat(begins))
        except ValueError:
            pass

    vouchers = db.get_vouchers()
    if not vouchers.empty:
        dates = pd.to_datetime(vouchers["voucher_date"], errors="coerce")
        dates = dates.dropna()
        if not dates.empty:
            marks += [dates.min().date(), dates.max().date()]

    first = min(fy_bounds(mark, month)[0] for mark in marks)
    last = max(fy_bounds(mark, month)[0] for mark in marks)

    years, start = [], first
    while start <= last:
        begin, end = fy_bounds(start, month)
        years.append(FinancialYear(fy_label(begin, month), begin, end))
        # Step by a year from the START of the year, never by 365 days: a leap
        # year would otherwise drift the month and eventually skip one.
        start = date(begin.year + 1, begin.month, 1)
    return years


def current_financial_year() -> FinancialYear:
    """The financial year containing today."""
    today = date.today()
    for year in financial_years():
        if year.contains(today):
            return year
    begin, end = fy_bounds(today)
    return FinancialYear(fy_label(begin), begin, end)


def side_of(amount: float) -> str:
    return "Dr" if amount >= 0 else "Cr"


def fmt_side(amount: float) -> str:
    """'13,744.00 Dr' — how a balance reads in Tally."""
    return f"{abs(amount):,.2f} {side_of(amount)}"


# --------------------------------------------------------------------------- #
# balances
# --------------------------------------------------------------------------- #
def balances(as_at=None, ledgers: pd.DataFrame | None = None,
             lines: pd.DataFrame | None = None) -> pd.DataFrame:
    """Opening, movement and closing per ledger, as at a date."""
    ledgers = db.get_ledgers(include_inactive=True) if ledgers is None else ledgers
    lines = db.get_lines(date_to=as_at) if lines is None else lines

    frame = ledgers[["id", "name", "group_name", "nature", "affects_gross",
                     "group_id", "opening_signed"]].copy()
    frame = frame.rename(columns={"id": "ledger_id", "name": "ledger",
                                  "opening_signed": "opening"})
    if lines.empty:
        frame["debit"] = 0.0
        frame["credit"] = 0.0
    else:
        totals = lines.groupby("ledger_id")[["debit", "credit"]].sum()
        frame = frame.join(totals, on="ledger_id")
        frame[["debit", "credit"]] = frame[["debit", "credit"]].fillna(0.0)
    frame["movement"] = (frame["debit"] - frame["credit"]).round(2)
    frame["closing"] = (frame["opening"] + frame["movement"]).round(2)
    return frame.reset_index(drop=True)


def opening_difference(as_at=None, ledgers: pd.DataFrame | None = None) -> float:
    """How far the opening balances are from agreeing. Zero in healthy books."""
    ledgers = db.get_ledgers(include_inactive=True) if ledgers is None else ledgers
    if ledgers.empty:
        return 0.0
    return round(float(ledgers["opening_signed"].sum()), 2)


# --------------------------------------------------------------------------- #
# ledger statement
# --------------------------------------------------------------------------- #
def ledger_statement(ledger_id: int, date_from=None, date_to=None) -> dict:
    """One account's entries in date order, with a running balance.

    Particulars name the other side of each voucher, prefixed the way Tally
    does it: a debit in this ledger reads 'To …', a credit reads 'By …'.
    """
    ledgers = db.get_ledgers(include_inactive=True).set_index("id")
    if int(ledger_id) not in ledgers.index:
        raise ValueError("No such ledger.")
    row = ledgers.loc[int(ledger_id)]

    everything = db.get_lines(date_to=date_to)
    mine = everything[everything["ledger_id"] == int(ledger_id)]

    before = mine
    if date_from is not None:
        before = mine[mine["voucher_date"] < str(date_from)]
        mine = mine[mine["voucher_date"] >= str(date_from)]
    else:
        before = mine.iloc[0:0]

    opening = round(float(row["opening_signed"])
                    + float(before["signed"].sum() if not before.empty else 0.0), 2)

    # the other ledgers in each of my vouchers, for the particulars column
    contra: dict[int, str] = {}
    if not mine.empty:
        others = everything[everything["voucher_id"].isin(set(mine["voucher_id"]))]
        others = others[others["ledger_id"] != int(ledger_id)]
        for voucher, group in others.groupby("voucher_id"):
            names = list(dict.fromkeys(group["ledger_name"]))
            contra[int(voucher)] = (names[0] if len(names) == 1
                                    else f"{names[0]} and {len(names) - 1} more")

    records, running = [], opening
    for line in mine.itertuples():
        running = round(running + float(line.signed), 2)
        other = contra.get(int(line.voucher_id), "(as per details)")
        records.append({
            "Date": line.voucher_date,
            "Particulars": ("To " if line.debit else "By ") + other,
            "Vch Type": line.voucher_type,
            "Vch No": int(line.number),
            "Debit": round(float(line.debit), 2),
            "Credit": round(float(line.credit), 2),
            "Balance": fmt_side(running),
            "voucher_id": int(line.voucher_id),
        })

    entries = pd.DataFrame(records, columns=[
        "Date", "Particulars", "Vch Type", "Vch No", "Debit", "Credit",
        "Balance", "voucher_id"])
    return {
        "ledger": str(row["name"]),
        "group": str(row["group_name"]),
        "opening": opening,
        "closing": round(running, 2),
        "debit_total": round(float(entries["Debit"].sum()), 2) if not entries.empty else 0.0,
        "credit_total": round(float(entries["Credit"].sum()), 2) if not entries.empty else 0.0,
        "entries": entries,
    }


# --------------------------------------------------------------------------- #
# day book
# --------------------------------------------------------------------------- #
def day_book(date_from=None, date_to=None) -> pd.DataFrame:
    """Every voucher in the period, one row per voucher, Tally's Day Book."""
    # Voided vouchers are asked for deliberately: they belong in the Day Book,
    # holding their number, even though they post nothing.
    lines = db.get_lines(date_from=date_from, date_to=date_to,
                         include_voided=True)
    # The two sides get a column each. They were one "A / B  <-  C" cell until
    # 2026-10-20, which read as a sentence rather than as a table: you could
    # not see down a column to find what a ledger was debited against, and an
    # arrow in the middle of text is nothing a spreadsheet can filter on.
    # Named "... account" because "Debit" and "Credit" here are the AMOUNTS.
    columns = ["Date", "Debit account", "Credit account", "Vch Type", "Vch No",
               "Debit", "Credit", "voided", "voucher_id"]
    if lines.empty:
        return pd.DataFrame(columns=columns)

    records = []
    for voucher, group in lines.groupby("voucher_id", sort=False):
        debits = group[group["debit"] > 0]["ledger_name"].tolist()
        credits = group[group["credit"] > 0]["ledger_name"].tolist()
        head = group.iloc[0]
        voided = bool(head.get("voided", 0))
        records.append({
            "Date": head["voucher_date"],
            # A voucher may touch several ledgers on a side, so a cell can
            # still hold a list - but only ever of one side's ledgers.
            "Debit account": " / ".join(debits),
            "Credit account": " / ".join(credits),
            "Vch Type": head["voucher_type"],
            "Vch No": int(head["number"]),
            # Shown as nil, not as what it used to be - a voided voucher moves
            # no value, so a Day Book that summed its old amounts would
            # disagree with every other report.
            "Debit": 0.0 if voided else round(float(group["debit"].sum()), 2),
            "Credit": 0.0 if voided else round(float(group["credit"].sum()), 2),
            "voided": voided,
            "voucher_id": int(voucher),
        })
    frame = pd.DataFrame(records, columns=columns)
    return frame.sort_values(["Date", "Vch No"], ascending=[False, False]) \
                .reset_index(drop=True)


MONTH_LABEL = "%b %Y"


def monthly_summary(ledger_id: int, date_from=None, date_to=None) -> dict:
    """One account month by month: what moved, and where it stood after.

    Asked for 2026-10-21 - "i would like to see balance for each month and
    then if i click on that month it should show activity for that specific
    month only". Reading a closing balance tells you where an account ended
    up; reading it month by month is how you find the month that explains it.

    EVERY month in the period is listed, including the quiet ones. A month
    with nothing in it is an answer to "when did this stop moving?", and a
    table that skips it makes the reader count dates to notice.

    Each row carries `start` and `end`, the month's own bounds, so clicking
    one can ask for exactly that month without re-deriving which days it
    covers - a month is not thirty days and February is not always the same
    length.
    """
    ledgers = db.get_ledgers(include_inactive=True).set_index("id")
    if int(ledger_id) not in ledgers.index:
        raise ValueError("No such ledger.")

    statement = ledger_statement(ledger_id, date_from, date_to)
    entries = statement["entries"]

    end = date_to or date.today()
    if date_from is not None:
        first = date(date_from.year, date_from.month, 1)
    elif not entries.empty:
        earliest = date.fromisoformat(str(entries["Date"].min()))
        first = date(earliest.year, earliest.month, 1)
    else:
        # No start and nothing in the account: the month being looked at is
        # the only one worth showing.
        first = date(end.year, end.month, 1)

    months, cursor = [], first
    while cursor <= end:
        last = date(cursor.year, cursor.month,
                    calendar.monthrange(cursor.year, cursor.month)[1])
        months.append((cursor, min(last, end)))
        cursor = date(cursor.year + (cursor.month == 12),
                      1 if cursor.month == 12 else cursor.month + 1, 1)

    rows, running = [], statement["opening"]
    for start, finish in months:
        if entries.empty:
            debit = credit = 0.0
        else:
            within = entries[(entries["Date"] >= start.isoformat())
                             & (entries["Date"] <= finish.isoformat())]
            debit = round(float(within["Debit"].sum()), 2)
            credit = round(float(within["Credit"].sum()), 2)
        running = round(running + debit - credit, 2)
        rows.append({
            "Month": start.strftime(MONTH_LABEL),
            "Debit": debit,
            "Credit": credit,
            # The side is spelled out, as it is on the ledger itself: a bare
            # negative number does not say "credit" to anybody reading a
            # statement.
            "Closing": fmt_side(running),
            "start": start,
            "end": finish,
        })

    frame = pd.DataFrame(rows, columns=["Month", "Debit", "Credit", "Closing",
                                        "start", "end"])
    return {"ledger": statement["ledger"], "group": statement["group"],
            "opening": statement["opening"], "closing": statement["closing"],
            "debit_total": statement["debit_total"],
            "credit_total": statement["credit_total"],
            "months": frame}


def day_book_lines(date_from=None, date_to=None) -> pd.DataFrame:
    """Every voucher in the period, one row per LINE rather than per voucher.

    For taking the day book away with you (user asked 2026-10-21: "while
    exporting daybook not able to see individual amounts for multiline
    entries"). `day_book` puts a voucher on one row, which means a compound
    voucher's accounts arrive as "Rent / Electricity / Phone" against a
    single total - the figure that belongs to each one is nowhere on the
    page.

    The voucher's date, type and number are REPEATED on each of its lines
    rather than left blank under the first. That is what makes the export
    worth having: every row stands on its own, so the file can be sorted by
    account, filtered to one voucher type, or pivoted, none of which works
    if half the rows only make sense in the context of the row above.

    Adding the Debit column up still gives the same total as `day_book`'s,
    since a voucher's total is exactly the sum of its lines - there is no
    double counting here of the sort the detailed balance sheet has to warn
    about.
    """
    lines = db.get_lines(date_from=date_from, date_to=date_to,
                         include_voided=True)
    columns = ["Date", "Vch Type", "Vch No", "Account", "Debit", "Credit",
               "Narration", "voided", "voucher_id"]
    if lines.empty:
        return pd.DataFrame(columns=columns)

    records = []
    for row in lines.sort_values(["voucher_id", "line_order"]).itertuples():
        voided = bool(getattr(row, "voided", 0))
        records.append({
            "Date": row.voucher_date,
            "Vch Type": row.voucher_type,
            "Vch No": int(row.number),
            "Account": row.ledger_name,
            # Nil for a voided voucher, exactly as `day_book` shows it: it
            # moves no value, and a day book that summed its old amounts
            # would disagree with every other report.
            "Debit": 0.0 if voided else round(float(row.debit), 2),
            "Credit": 0.0 if voided else round(float(row.credit), 2),
            "Narration": row.narration,
            "voided": voided,
            "voucher_id": int(row.voucher_id),
        })
    frame = pd.DataFrame(records, columns=columns)
    # Newest voucher first, as the Day Book reads - but a voucher's own lines
    # keep the order they were entered in, which is the order they balance in.
    return frame.sort_values(["Date", "Vch No"], ascending=[False, False],
                             kind="stable").reset_index(drop=True)



# --------------------------------------------------------------------------- #
def trial_balance(as_at=None, by_group: bool = False,
                  include_zero: bool = False) -> dict:
    """Closing balances with debit and credit columns that must agree.

    Accounts standing at nil are LEFT OUT unless `include_zero` is asked for
    (user asked 2026-10-20 for the choice). Out by default because a trial
    balance is read to find what the figures are, and a page of blank lines
    is in the way of that; in, when the question is whether an account exists
    at all or has been used this year.

    The totals cannot move either way - nil adds nothing to a side - which is
    what makes the toggle safe to flip while reading.
    """
    frame = balances(as_at)
    if not include_zero:
        frame = frame[frame["closing"].abs() > EPS]

    if by_group:
        grouped = frame.groupby("group_name", as_index=False)["closing"].sum()
        grouped["closing"] = grouped["closing"].round(2)
        rows = grouped.rename(columns={"group_name": "Particulars"})
    else:
        rows = frame[["ledger", "group_name", "closing"]].rename(
            columns={"ledger": "Particulars", "group_name": "Under"})

    # Said twice on purpose: grouping can net two ledgers off against each
    # other, so a group can come to nil out of balances that are not.
    if not include_zero:
        rows = rows[rows["closing"].abs() > EPS]
    rows = rows.copy()
    rows["Debit"] = rows["closing"].map(lambda v: round(v, 2) if v > 0 else 0.0)
    rows["Credit"] = rows["closing"].map(lambda v: round(-v, 2) if v < 0 else 0.0)
    rows = rows.drop(columns=["closing"]).reset_index(drop=True)

    debit = round(float(rows["Debit"].sum()), 2) if not rows.empty else 0.0
    credit = round(float(rows["Credit"].sum()), 2) if not rows.empty else 0.0
    difference = round(debit - credit, 2)
    return {"rows": rows, "debit": debit, "credit": credit,
            "difference": difference}


# --------------------------------------------------------------------------- #
# profit & loss
# --------------------------------------------------------------------------- #
def profit_and_loss(date_from=None, date_to=None) -> dict:
    """Trading account then the indirect accounts, as Tally lays it out.

    With ``date_from`` unset the report runs from the start of the books and
    includes any opening balances sitting on nominal ledgers. Given a start
    date it reports only that period's movement, which is what you want for a
    month or a quarter.
    """
    ledgers = db.get_ledgers(include_inactive=True)
    lines = db.get_lines(date_from=date_from, date_to=date_to)

    frame = ledgers[["id", "name", "group_name", "nature", "affects_gross",
                     "opening_signed"]].copy()
    if lines.empty:
        frame["movement"] = 0.0
    else:
        movement = lines.groupby("ledger_id")["signed"].sum()
        frame["movement"] = frame["id"].map(movement).fillna(0.0)
    include_opening = date_from is None
    frame["amount"] = (
        frame["movement"] + (frame["opening_signed"] if include_opening else 0.0)
    ).round(2)

    nominal = frame[frame["nature"].isin(["income", "expense"])]

    def section(nature: str, gross: bool) -> pd.DataFrame:
        part = nominal[(nominal["nature"] == nature)
                       & (nominal["affects_gross"].astype(bool) == gross)]
        # income sits credit-side, so flip it to read as a positive figure
        sign = -1.0 if nature == "income" else 1.0
        out = pd.DataFrame({
            "Particulars": part["name"],
            "Under": part["group_name"],
            "Amount": (part["amount"] * sign).round(2),
        })
        out = out[out["Amount"].abs() > EPS]
        return out.sort_values("Amount", ascending=False).reset_index(drop=True)

    trading_income = section("income", True)
    trading_expense = section("expense", True)
    indirect_income = section("income", False)
    indirect_expense = section("expense", False)

    def total(part: pd.DataFrame) -> float:
        return round(float(part["Amount"].sum()), 2) if not part.empty else 0.0

    gross = round(total(trading_income) - total(trading_expense), 2)
    net = round(gross + total(indirect_income) - total(indirect_expense), 2)

    return {
        "trading_income": trading_income,
        "trading_expense": trading_expense,
        "indirect_income": indirect_income,
        "indirect_expense": indirect_expense,
        "totals": {
            "trading_income": total(trading_income),
            "trading_expense": total(trading_expense),
            "gross_profit": gross,
            "indirect_income": total(indirect_income),
            "indirect_expense": total(indirect_expense),
            "net_profit": net,
        },
    }


def net_profit(as_at=None) -> float:
    """Profit from the start of the books to a date — what the balance sheet needs."""
    return profit_and_loss(None, as_at)["totals"]["net_profit"]


# --------------------------------------------------------------------------- #
# balance sheet
# --------------------------------------------------------------------------- #
DETAIL_INDENT = "    "


def balance_sheet(as_at=None, detailed: bool = False) -> dict:
    """Both sides as at a date, balanced, with the residual named honestly.

    `detailed` lists the ACCOUNTS under each heading as well as the heading's
    own total (asked for 2026-10-20: "i need balance sheet in detailed as well
    as summary format so I can see accounts under each header"). The headings
    and the totals are identical either way - detail is added beneath them,
    never instead of them, so the two formats can be compared line for line.

    NOTE for anyone summing the Amount column: in detailed form it holds a
    heading's total AND the accounts that make it up, so the column adds up to
    twice the side. The totals in `totals` are the ones to use, and they are
    computed from the headings alone.
    """
    frame = balances(as_at)
    groups = db.get_groups()
    lineage = db.group_lineage(groups)

    primary_of = {}
    for row in groups.itertuples():
        chain = lineage.get(int(row.id), [str(row.name)])
        primary_of[str(row.name)] = chain[0] if chain else str(row.name)
    frame["primary"] = frame["group_name"].map(primary_of).fillna(frame["group_name"])

    # nominal accounts are closed into Profit & Loss A/c, not listed here
    real = frame[~frame["nature"].isin(["income", "expense"])]
    by_primary = real.groupby("primary")["closing"].sum().round(2).to_dict()

    liabilities = [[name, round(-by_primary.get(name, 0.0), 2)]
                   for name in LIABILITY_SIDE if abs(by_primary.get(name, 0.0)) > EPS]
    assets = [[name, round(by_primary.get(name, 0.0), 2)]
              for name in ASSET_SIDE if abs(by_primary.get(name, 0.0)) > EPS]

    # anything under a group outside the two standard lists
    for name, amount in by_primary.items():
        if name in LIABILITY_SIDE or name in ASSET_SIDE or abs(amount) <= EPS:
            continue
        if amount < 0:
            liabilities.append([name, round(-amount, 2)])
        else:
            assets.append([name, round(amount, 2)])

    profit = net_profit(as_at)
    if abs(profit) > EPS:
        liabilities.append(["Profit & Loss A/c", profit])

    liability_total = round(sum(amount for _, amount in liabilities), 2)
    asset_total = round(sum(amount for _, amount in assets), 2)

    # the residual is the opening-balance disagreement; show it, do not bury it
    residual = round(asset_total - liability_total, 2)
    if abs(residual) > EPS:
        if residual > 0:
            liabilities.append(["Difference in opening balances", residual])
            liability_total = round(liability_total + residual, 2)
        else:
            assets.append(["Difference in opening balances", round(-residual, 2)])
            asset_total = round(asset_total - residual, 2)

    if detailed:
        # Added AFTER both totals are settled, so the headings still decide
        # what the side comes to and the two formats cannot disagree.
        liabilities = _with_accounts(liabilities, real, sign=-1)
        assets = _with_accounts(assets, real, sign=1)

    return {
        "liabilities": pd.DataFrame(liabilities, columns=["Particulars", "Amount"]),
        "assets": pd.DataFrame(assets, columns=["Particulars", "Amount"]),
        "detailed": detailed,
        "totals": {
            "liabilities": liability_total,
            "assets": asset_total,
            "difference": round(asset_total - liability_total, 2),
            "net_profit": profit,
        },
    }


# --------------------------------------------------------------------------- #
# bill-wise receivables and payables
# --------------------------------------------------------------------------- #
# How long a bill has been outstanding, in days, bucketed. Open-ended at the
# top: "over 90" is one answer, and splitting it further says little that
# "very late" does not.
AGE_BUCKETS = ((0, 30), (31, 60), (61, 90), (91, None))
AGE_BY = ("due date", "bill date")


def age_bucket(days: int) -> str:
    """The label a number of days falls in. Anything not yet due is 'Not due'."""
    if days < 0:
        return "Not due"
    for low, high in AGE_BUCKETS:
        if high is None or days <= high:
            return f"{low}-{high} days" if high else f"Over {low - 1} days"
    return "Not due"


def bucket_labels() -> list[str]:
    return ["Not due"] + [age_bucket(low) for low, _ in AGE_BUCKETS]


def outstanding(as_at=None, age_by: str = "due date",
                ledger: int | None = None) -> dict:
    """Open bills per party, with how overdue each one is.

    A bill is the pair (party, reference) and its balance is the SUM of its
    allocations - there is no stored bill amount. So a reference settled in
    full sums to zero and simply stops appearing, with no status to update
    and nothing to go stale.

    `age_by` decides what the clock runs from: the due date, which answers
    "how late is this?", or the bill's own date, which answers "how long have
    we been carrying it?". Bills with no due date always age from their own
    date, so choosing due date never silently drops them.
    """
    if age_by not in AGE_BY:
        raise ValueError(f"Age bills by {' or '.join(AGE_BY)}, not {age_by!r}.")
    today = date.today() if as_at is None else (
        as_at if isinstance(as_at, date) else date.fromisoformat(str(as_at)))

    frame = db.get_allocations(as_at=as_at)
    # `Pending` is the magnitude, which is what a report shows; `Signed` keeps
    # the direction, which is what reconciling against the ledger needs. A
    # party can owe on bills while holding a credit on account, and adding
    # magnitudes would then double-count instead of netting.
    columns = ["Party", "Ref", "Date", "Due", "Amount", "Pending", "Signed",
               "Age", "Bucket", "nature", "ledger_id"]
    if ledger is not None and not frame.empty:
        frame = frame[frame["ledger_id"] == int(ledger)]
    if frame.empty:
        return {"bills": pd.DataFrame(columns=columns),
                "on_account": pd.DataFrame(columns=["Party", "Pending",
                                                    "nature", "ledger_id"]),
                "ageing": pd.DataFrame(columns=["Bucket", "Receivable",
                                                "Payable"]),
                "totals": {"receivable": 0.0, "payable": 0.0,
                           "on_account": 0.0},
                "age_by": age_by, "as_at": today}

    # On-account money is not a bill: it is deliberately unattached, so it
    # has nothing to be overdue against. Carried in the totals, not the list.
    on_account = frame[frame["kind"] == "on_account"]
    frame = frame[frame["kind"] != "on_account"]

    rows = []
    for (lid, ref), group in frame.groupby(["ledger_id", "bill_ref"],
                                           sort=False):
        pending = round(float(group["signed"].sum()), 2)
        if abs(pending) <= EPS:
            continue                      # settled in full, so not a bill now
        opened = group[group["kind"].isin(["new", "advance"])]
        first = (opened if not opened.empty else group).iloc[0]
        billed = opened["signed"].sum() if not opened.empty else pending
        bill_date = date.fromisoformat(str(first["voucher_date"]))
        due_raw = str(first["due_date"] or "")
        due = date.fromisoformat(due_raw) if due_raw else None
        # No due date means there is nothing to be late against, so those age
        # from the bill date whichever basis is chosen.
        from_date = due if (age_by == "due date" and due) else bill_date
        rows.append({
            "Party": first["ledger_name"],
            "Ref": ref,
            "Date": bill_date.isoformat(),
            "Due": due.isoformat() if due else "",
            "Amount": round(abs(float(billed)), 2),
            "Pending": round(abs(pending), 2),
            "Signed": pending,
            "Age": (today - from_date).days,
            "Bucket": age_bucket((today - from_date).days),
            "nature": first["nature"],
            "ledger_id": int(lid),
        })

    bills = pd.DataFrame(rows, columns=columns)
    if not bills.empty:
        bills = bills.sort_values(["Age", "Party", "Ref"],
                                 ascending=[False, True, True]) \
                     .reset_index(drop=True)

    # An asset-natured party owes us; a liability-natured one is owed. Taken
    # from the group's nature rather than the sign, so a customer in credit
    # still reports under receivables, where you would look for them.
    def side(frame_in, nature):
        part = frame_in[frame_in["nature"] == nature]
        return round(float(part["Pending"].sum()), 2) if not part.empty else 0.0

    ageing = []
    for label in bucket_labels():
        part = bills[bills["Bucket"] == label] if not bills.empty else bills
        ageing.append({"Bucket": label,
                       "Receivable": side(part, "asset") if not part.empty else 0.0,
                       "Payable": side(part, "liability") if not part.empty else 0.0})
    ageing = pd.DataFrame(ageing, columns=["Bucket", "Receivable", "Payable"])
    ageing = ageing[(ageing["Receivable"].abs() > EPS)
                    | (ageing["Payable"].abs() > EPS)].reset_index(drop=True)

    # On-account money, per party. Reported beside the bills rather than
    # folded into them: it is real money on the account that simply is not
    # against anything, and a party's ledger balance only reconciles once it
    # is counted. Seeing it is also the prompt to go and allocate it.
    unattached = 0.0
    loose_columns = ["Party", "Pending", "nature", "ledger_id"]
    loose = pd.DataFrame(columns=loose_columns)
    if not on_account.empty:
        unattached = round(float(on_account["signed"].sum()), 2)
        grouped = on_account.groupby(["ledger_id", "ledger_name", "nature"],
                                     as_index=False)["signed"].sum()
        grouped = grouped[grouped["signed"].abs() > EPS]
        loose = pd.DataFrame({
            "Party": grouped["ledger_name"],
            "Pending": grouped["signed"].round(2),
            "nature": grouped["nature"],
            "ledger_id": grouped["ledger_id"].astype(int),
        }, columns=loose_columns).reset_index(drop=True)

    return {
        "bills": bills,
        "on_account": loose,
        "ageing": ageing,
        "totals": {
            "receivable": side(bills, "asset") if not bills.empty else 0.0,
            "payable": side(bills, "liability") if not bills.empty else 0.0,
            "on_account": unattached,
        },
        "age_by": age_by,
        "as_at": today,
    }


def open_bills(ledger: int, as_at=None) -> pd.DataFrame:
    """That party's unsettled bills - what the allocation picker offers.

    Oldest first, because that is the order they are usually settled in and
    the order "apply to the oldest" needs.
    """
    result = outstanding(as_at=as_at, ledger=int(ledger))
    bills = result["bills"]
    if bills.empty:
        return bills
    return bills.sort_values("Date").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# GST
# --------------------------------------------------------------------------- #
# Which voucher types make an outward supply (tax you collect and owe) and
# which an inward one (tax you have paid and can set off).
OUTWARD = ("Sales",)
INWARD = ("Purchase",)


def gst_summary(date_from=None, date_to=None) -> dict:
    """Output tax, input tax, and what is left to pay.

    Every figure is summed from the VOUCHER LINES, with `gst_details` saying
    only what the lines cannot - the rate charged, the place of supply, the
    counterparty's GSTIN. So a voucher corrected afterwards reports its new
    numbers with nothing to re-run, and this can never disagree with the
    Duties & Taxes ledgers it is drawn from.
    """
    frame = db.get_gst_vouchers(date_from=date_from, date_to=date_to)
    rate_columns = ["Rate", "Taxable", "CGST", "SGST", "IGST", "Tax"]
    blank = pd.DataFrame(columns=rate_columns)
    empty = {
        "outward": blank.copy(), "inward": blank.copy(),
        "totals": {"output_tax": 0.0, "input_tax": 0.0, "net": 0.0,
                   "outward_taxable": 0.0, "inward_taxable": 0.0},
        "by_supply": pd.DataFrame(columns=["Supply", "Taxable", "Tax"]),
    }
    if frame.empty:
        return empty

    frame = frame.copy()
    frame["tax"] = (frame["cgst"] + frame["sgst"] + frame["igst"]).round(2)

    def by_rate(part: pd.DataFrame) -> pd.DataFrame:
        if part.empty:
            return blank.copy()
        grouped = part.groupby("rate", as_index=False)[
            ["taxable", "cgst", "sgst", "igst", "tax"]].sum().round(2)
        grouped = grouped.sort_values("rate").reset_index(drop=True)
        return pd.DataFrame({
            "Rate": grouped["rate"].map(lambda r: f"{r:g}%"),
            "Taxable": grouped["taxable"], "CGST": grouped["cgst"],
            "SGST": grouped["sgst"], "IGST": grouped["igst"],
            "Tax": grouped["tax"]}, columns=rate_columns)

    out = frame[frame["voucher_type"].isin(OUTWARD)]
    inp = frame[frame["voucher_type"].isin(INWARD)]
    output_tax = round(float(out["tax"].sum()), 2) if not out.empty else 0.0
    input_tax = round(float(inp["tax"].sum()), 2) if not inp.empty else 0.0

    supply = pd.DataFrame(columns=["Supply", "Taxable", "Tax"])
    if not out.empty:
        grouped = out.groupby("supply_type", as_index=False)[
            ["taxable", "tax"]].sum().round(2)
        supply = pd.DataFrame({
            "Supply": grouped["supply_type"].map(
                {"intra": "Within the state", "inter": "Interstate"}),
            "Taxable": grouped["taxable"], "Tax": grouped["tax"]},
            columns=["Supply", "Taxable", "Tax"]).reset_index(drop=True)

    return {
        "outward": by_rate(out),
        "inward": by_rate(inp),
        "by_supply": supply,
        "totals": {
            "output_tax": output_tax,
            "input_tax": input_tax,
            # Positive is payable, negative is credit carried forward. Named
            # rather than shown as a bare signed figure, because "you owe
            # 4,000" and "you are owed 4,000" are not a sign apart in use.
            "net": round(output_tax - input_tax, 2),
            "outward_taxable": round(float(out["taxable"].sum()), 2)
            if not out.empty else 0.0,
            "inward_taxable": round(float(inp["taxable"].sum()), 2)
            if not inp.empty else 0.0,
        },
    }


def gst_register(date_from=None, date_to=None,
                 outward: bool = True) -> pd.DataFrame:
    """Invoice-by-invoice GST, the shape an outward-supplies return wants.

    Place of supply and the counterparty's GSTIN come from `gst_details`
    because they are a SNAPSHOT: a return filed for August has to keep
    saying what it said then, even after the customer moves state or
    registers. The money comes from the lines.
    """
    frame = db.get_gst_vouchers(date_from=date_from, date_to=date_to)
    columns = ["Date", "Vch Type", "Vch No", "Party", "GSTIN", "Place of supply",
               "Supply", "Rate", "Taxable", "CGST", "SGST", "IGST", "Total",
               "voucher_id"]
    kinds = OUTWARD if outward else INWARD
    if not frame.empty:
        frame = frame[frame["voucher_type"].isin(kinds)]
    if frame.empty:
        return pd.DataFrame(columns=columns)

    rows = []
    for row in frame.itertuples():
        tax = round(row.cgst + row.sgst + row.igst, 2)
        rows.append({
            "Date": str(row.voucher_date),
            "Vch Type": row.voucher_type,
            "Vch No": int(row.number),
            "Party": row.party or "",
            # Blank is meaningful: an unregistered customer has no GSTIN, and
            # those supplies are reported separately on a real return.
            "GSTIN": row.party_gstin or "",
            "Place of supply": (db.state_name(row.place_of_supply)
                                or row.place_of_supply or ""),
            "Supply": "Interstate" if row.supply_type == "inter"
                      else "Within the state",
            "Rate": f"{row.rate:g}%",
            "Taxable": round(float(row.taxable), 2),
            "CGST": round(float(row.cgst), 2),
            "SGST": round(float(row.sgst), 2),
            "IGST": round(float(row.igst), 2),
            "Total": round(float(row.taxable) + tax, 2),
            "voucher_id": int(row.voucher_id),
        })
    return pd.DataFrame(rows, columns=columns)


def _with_accounts(rows: list[list], real: pd.DataFrame,
                   sign: int) -> list[list]:
    """Each heading followed by the accounts that make it up, indented.

    Only real headings gain detail. "Profit & Loss A/c" and "Difference in
    opening balances" are figures this statement works out for itself, not
    groups with ledgers under them, so they stay single lines - looking them
    up would find nothing and print an empty heading.
    """
    out: list[list] = []
    for name, amount in rows:
        out.append([name, amount])
        under = real[real["primary"] == name]
        if under.empty:
            continue
        for row in under.sort_values("ledger").itertuples():
            value = round(sign * float(row.closing), 2)
            if abs(value) <= EPS:
                continue
            out.append([f"{DETAIL_INDENT}{row.ledger}", value])
    return out
