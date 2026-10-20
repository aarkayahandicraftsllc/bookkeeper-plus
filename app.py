"""Tally-style bookkeeping — Streamlit front end.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import calendar
import csv
import io
from datetime import date, timedelta

import pandas as pd
import streamlit as st

import accounting as acc
import db

st.set_page_config(page_title="Bookkeeper Plus", page_icon="📗", layout="wide")

# --------------------------------------------------------------------------- #
# which company
# --------------------------------------------------------------------------- #
# Chosen BEFORE anything is read, because every constant below and every query
# after it comes from whichever file this picks. The desktop app asks at launch;
# here it is a sidebar picker, since a browser tab has no launch moment and
# Streamlit re-runs this script top to bottom on each interaction.
#
# Skipped entirely when BOOKS_DB pins the file: the test suites and anyone
# pointing at a shared folder have already chosen, and a picker would override
# them.
if not db.DB_PINNED:
    _companies = db.list_companies()
    if _companies:
        _files = [c["file"] for c in _companies]
        _labels = {c["file"]: f"{c['company_name']}  ({c['file']})"
                   for c in _companies}
        _remembered = db.last_company()
        _default = (_files.index(_remembered.name)
                    if _remembered is not None and _remembered.name in _files
                    else 0)
        _picked = st.sidebar.selectbox(
            "Company", _files, index=_default,
            format_func=lambda f: _labels[f], key="company_file")
        db.use_company(db.DATA_DIR / _picked)
        st.sidebar.caption(f"{len(_companies)} "
                           f"compan{'y' if len(_companies) == 1 else 'ies'} "
                           f"in {db.DATA_DIR}")
    st.sidebar.caption(
        "New companies are created in the desktop app "
        "(Company ▸ New company).")

db.init_db()

# Backing up is a BUTTON here and not automatic, unlike the desktop app which
# takes one as it opens. Streamlit re-runs this whole script on every single
# interaction, so "on open" has no meaning in a browser tab - hooking it here
# would copy the books on every click and bury the useful copies.
if st.sidebar.button("Back up now", help="Copy these books into the backup "
                                         "folder", key="backup_now"):
    try:
        _made = db.backup_company(tag="manual")
    except Exception as _exc:                          # noqa: BLE001
        st.sidebar.error(f"Nothing was written: {_exc}")
    else:
        if _made is None:
            st.sidebar.info("Unchanged since the last backup — nothing new "
                            "was written.")
        else:
            st.sidebar.success(f"Backed up to {_made.name}")
_kept = db.backups_for()
if _kept:
    st.sidebar.caption(f"{len(_kept)} backup(s), newest {_kept[0].name}")

COMPANY = db.get_setting("company_name", "My Company")
CURRENCY = db.get_setting("currency", "₹")
FY_START_MONTH = int(db.get_setting("fy_start_month", "4"))
BOOKS_BEGIN = db.get_setting("books_begin", "")

ROW_PX = 35


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}{CURRENCY}{abs(value):,.2f}"


def amount_col(label: str) -> st.column_config.NumberColumn:
    return st.column_config.NumberColumn(label, format="%.2f")


def table_height(rows: int, visible: int | None = None) -> int:
    shown = rows if visible is None else min(rows, visible)
    return ROW_PX * (max(shown, 1) + 1) + 3


def flash(message: str, kind: str = "success") -> None:
    """A message that survives the rerun which follows an action."""
    st.session_state["flash"] = (kind, message)


def show_flash() -> None:
    queued = st.session_state.pop("flash", None)
    if queued:
        kind, message = queued
        getattr(st, kind)(message)


def nonce(name: str) -> int:
    return st.session_state.setdefault(f"nonce_{name}", 0)


def bump(name: str) -> None:
    st.session_state[f"nonce_{name}"] = nonce(name) + 1


# The financial-year arithmetic lives in accounting.py, shared with the
# desktop app: both front ends used to work it out for themselves, which is
# two places for "when does the year end" to be wrong in different ways.
FY_START, FY_END = acc.fy_bounds(date.today(), FY_START_MONTH)


def year_periods() -> dict[str, tuple[date, date]]:
    """Each financial year in the books, as a pickable period.

    The WHOLE year, opening day to closing day, even when the closing day is
    still in the future: a year named in a dropdown has to mean that year, and
    clamping the end to today quietly reports a different period.
    """
    return {f"FY {year.label}": (year.start, year.end)
            for year in acc.financial_years()}


def default_year() -> str:
    return f"FY {acc.current_financial_year().label}"


def as_at_picker(key: str, label: str = "Financial year") -> date:
    """The date an as-at report is drawn at: a year's close, or your own date.

    "The balance sheet for 2025-2026" is how the question gets asked, and
    reaching it by typing a date is needless work.
    """
    closes = {name: end for name, (_, end) in year_periods().items()}
    names = list(closes) + ["Custom date…"]
    index = names.index(default_year()) if default_year() in names else 0
    left, right = st.columns([2, 2])
    with left:
        choice = st.selectbox(label, names, index=index, key=f"{key}_year")
    if choice == "Custom date…":
        with right:
            return st.date_input("As at", value=date.today(),
                                 format="DD/MM/YYYY", key=f"{key}_asat")
    st.caption(f"As at {closes[choice].strftime('%d/%m/%Y')}")
    return closes[choice]


def sections_csv(title: str, period: str,
                 sections: list[tuple[str, "pd.DataFrame"]]) -> bytes:
    """A multi-table report as one CSV.

    A CSV holds one table, so a report with four of them has to be laid out
    rather than dumped: blank lines and headings are what let a reader tell
    where the trading account ends and the indirect one begins. The desktop
    app writes the same shape.
    """
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerows([[title], [COMPANY], [period], []])
    for heading, frame in sections:
        writer.writerow([heading])
        if frame.empty:
            writer.writerow(["(nothing to show)"])
        else:
            writer.writerow(list(frame.columns))
            for row in frame.itertuples(index=False):
                writer.writerow(["" if pd.isna(v) else v for v in row])
        writer.writerow([])
    return out.getvalue().encode("utf-8-sig")


def period_picker(key: str, allow_all: bool = True) -> tuple[date | None, date]:
    """From / to selector shared by the reports."""
    month_start = date.today().replace(day=1)
    month_end = date.today().replace(
        day=calendar.monthrange(date.today().year, date.today().month)[1])
    options = {
        **year_periods(),
        "This month": (month_start, min(month_end, date.today())),
        "Custom dates…": None,
    }
    if allow_all:
        options = {"Since books began": (None, date.today()), **options}

    names = list(options)
    index = names.index(default_year()) if default_year() in names else 0
    left, right = st.columns([2, 3])
    with left:
        choice = st.selectbox("Period", names, index=index,
                              key=f"{key}_period")
    if choice == "Custom dates…":
        with right:
            picked = st.date_input(
                "From / to", value=(FY_START, date.today()),
                format="DD/MM/YYYY", key=f"{key}_range")
        if isinstance(picked, tuple) and len(picked) == 2:
            return picked[0], picked[1]
        return FY_START, date.today()
    return options[choice]


# --------------------------------------------------------------------------- #
# voucher entry
# --------------------------------------------------------------------------- #
def voucher_editor(prefix: str = "vch", existing: dict | None = None,
                   existing_lines: list[dict] | None = None) -> None:
    """Tally's voucher screen: type, date, number, then a Dr/Cr grid."""
    n = nonce(prefix)
    k = lambda name: f"{prefix}_{name}_{n}"  # noqa: E731

    ledgers = db.get_ledgers()
    # Counting ledgers you would actually post to. The six GST tax ledgers are
    # seeded into every book, so a plain count makes a fresh install look
    # ready when it still has nothing of yours in it.
    own = ledgers[ledgers["gst_role"] == ""] if "gst_role" in ledgers.columns \
        else ledgers
    if len(own) < 2:
        st.warning(
            "You need at least two ledgers before you can post anything. "
            "Create them under **Masters**."
        )
        return
    names = list(ledgers["name"])
    id_for = dict(zip(ledgers["name"], ledgers["id"].astype(int)))
    name_for = {int(v): k2 for k2, v in id_for.items()}

    types = list(db.VOUCHER_TYPES)
    head1, head2, head3 = st.columns([2, 2, 2])
    with head1:
        default_type = (existing or {}).get("voucher_type", "Payment")
        voucher_type = st.radio(
            "Voucher type", types,
            index=types.index(default_type) if default_type in types else 0,
            key=k("type"),
        )
        st.caption(db.VOUCHER_TYPES[voucher_type])
    with head2:
        voucher_date = st.date_input(
            "Date",
            value=(date.fromisoformat(existing["voucher_date"]) if existing
                   else date.today()),
            format="DD/MM/YYYY", key=k("date"))
        number = (int(existing["number"]) if existing
                  else db.next_voucher_number(voucher_type))
        st.caption(f"Voucher No. **{number}**")
    with head3:
        narration = st.text_input(
            "Narration", value=(existing or {}).get("narration", ""),
            key=k("narration"), placeholder="what this entry is for")

    with st.expander("➕ Create a ledger or group without leaving this voucher"):
        st.caption(
            "Anything already typed into the grid is kept — creating a master "
            "only reloads the ledger list. The desktop app does this with "
            "**Alt+L**."
        )
        make = st.radio("Create", ["Ledger", "Group"], horizontal=True,
                        key=k("make"))
        group_names = list(db.get_groups()["name"])
        if make == "Ledger":
            q1, q2 = st.columns(2)
            with q1:
                quick_name = st.text_input("Ledger name", key=k("qname"),
                                           placeholder="e.g. Bank of India")
                quick_group = st.selectbox("Under group", group_names,
                                           key=k("qgroup"))
            with q2:
                quick_open = st.number_input("Opening balance", min_value=0.0,
                                             step=1000.0, format="%.2f",
                                             key=k("qopen"))
                quick_side = st.radio("Dr / Cr", db.SIDES, horizontal=True,
                                      key=k("qside"))
            chosen = db.get_groups().set_index("name")
            if quick_group in chosen.index:
                nature = str(chosen.loc[quick_group, "nature"])
                trading = bool(chosen.loc[quick_group, "affects_gross"])
                where = {
                    "asset": "the assets side of the Balance Sheet",
                    "liability": "the liabilities side of the Balance Sheet",
                    "income": "the trading account" if trading else "indirect incomes",
                    "expense": "the trading account" if trading else "indirect expenses",
                }[nature]
                st.caption(f"A {nature} account — its balance will appear in {where}.")
            if st.button("Create ledger", key=k("qcreate")):
                try:
                    db.add_ledger(quick_name, quick_group, quick_open, quick_side)
                    flash(f"Ledger {quick_name.strip()!r} created — pick it on "
                          "any line below.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        else:
            g1, g2 = st.columns(2)
            with g1:
                quick_grp = st.text_input("Group name", key=k("qgname"),
                                          placeholder="e.g. Vehicle Loan")
            with g2:
                quick_parent = st.selectbox("Under", group_names, key=k("qparent"))
            st.caption("A sub-group inherits its parent's nature, so it lands on "
                       "the same statement.")
            if st.button("Create group", key=k("qgcreate")):
                try:
                    db.add_group(quick_grp, quick_parent)
                    flash(f"Group {quick_grp.strip()!r} created under "
                          f"{quick_parent}.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

    st.markdown("**Particulars**")
    if existing_lines:
        start = pd.DataFrame([
            {"Ledger": name_for.get(int(l["ledger_id"]), ""),
             "Debit": round(float(l["debit"]), 2),
             "Credit": round(float(l["credit"]), 2)}
            for l in existing_lines
        ])
    else:
        rows = st.number_input(
            "Lines", min_value=2, max_value=30, value=2, step=1, key=k("rows"),
            help="Two lines is the usual case. Raise it for a compound voucher.")
        start = pd.DataFrame({"Ledger": [None] * int(rows),
                              "Debit": [0.0] * int(rows),
                              "Credit": [0.0] * int(rows)})

    grid = st.data_editor(
        start, hide_index=True, width="stretch", num_rows="fixed", key=k("grid"),
        column_config={
            "Ledger": st.column_config.SelectboxColumn(
                "Ledger", options=names, width="large",
                help="Which account this side of the voucher hits."),
            "Debit": st.column_config.NumberColumn("Debit", min_value=0.0,
                                                   step=100.0, format="%.2f"),
            "Credit": st.column_config.NumberColumn("Credit", min_value=0.0,
                                                    step=100.0, format="%.2f"),
        },
    )

    filled = grid[grid["Ledger"].notna()
                  & ((grid["Debit"].fillna(0) > 0) | (grid["Credit"].fillna(0) > 0))]
    debit = round(float(filled["Debit"].fillna(0).sum()), 2) if not filled.empty else 0.0
    credit = round(float(filled["Credit"].fillna(0).sum()), 2) if not filled.empty else 0.0
    gap = round(debit - credit, 2)

    t1, t2, t3 = st.columns(3)
    t1.metric("Total debit", money(debit))
    t2.metric("Total credit", money(credit))
    t3.metric("Difference", money(gap))

    payload = [{"ledger_id": id_for[row.Ledger],
                "debit": float(row.Debit or 0), "credit": float(row.Credit or 0)}
               for row in filled.itertuples()]
    balanced = abs(gap) <= 0.005 and debit > 0 and len(payload) >= 2

    if filled.empty:
        st.caption("Pick a ledger on each line, then enter the amount under "
                   "**Debit** or **Credit** — never both on one line.")
    elif balanced:
        st.success("✅ Debit equals credit.")
    else:
        st.warning(f"Debit and credit must agree. Out by {money(abs(gap))}.")

    for note in db.voucher_type_warnings(voucher_type, payload, ledgers):
        st.info(f"ℹ️ {note}")

    label = "💾 Save changes" if existing else "📗 Accept voucher"
    if st.button(label, type="primary", key=k("save"), disabled=not balanced):
        try:
            db.save_voucher(
                voucher_type=voucher_type, voucher_date=voucher_date,
                narration=narration, lines=payload,
                voucher_id=int(existing["id"]) if existing else None)
            flash(f"{voucher_type} voucher saved — {money(debit)} each side.")
            bump(prefix)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))


# --------------------------------------------------------------------------- #
# header
# --------------------------------------------------------------------------- #
st.title(f"📗 {COMPANY}")
st.caption(
    f"Financial year {FY_START.strftime('%d/%m/%Y')} to {FY_END.strftime('%d/%m/%Y')}"
    + (f" · books began {date.fromisoformat(BOOKS_BEGIN).strftime('%d/%m/%Y')}"
       if BOOKS_BEGIN else "")
)
show_flash()

gap = acc.opening_difference()
if abs(gap) > acc.EPS:
    st.warning(
        f"**Difference in opening balances: {money(abs(gap))} "
        f"{'Dr' if gap > 0 else 'Cr'}.** The opening balances on your ledgers do "
        "not agree, so the trial balance will not either. It is shown as its own "
        "line on the Balance Sheet until you fix it."
    )

(tab_voucher, tab_daybook, tab_ledger, tab_trial, tab_pl, tab_bs,
 tab_masters) = st.tabs(
    ["📝 Voucher Entry", "📘 Day Book", "📑 Ledger", "⚖️ Trial Balance",
     "📈 Profit & Loss", "🏦 Balance Sheet", "⚙️ Masters"])

# --------------------------------------------------------------------------- #
with tab_voucher:
    voucher_editor()

# --------------------------------------------------------------------------- #
with tab_daybook:
    st.subheader("Day Book")
    db_from, db_to = period_picker("daybook")
    kind = st.selectbox("Voucher type", ["All"] + list(db.VOUCHER_TYPES),
                        key="daybook_type")
    book = acc.day_book(db_from, db_to)
    if kind != "All" and not book.empty:
        book = book[book["Vch Type"] == kind]

    if book.empty:
        st.info("No vouchers in this period.")
    else:
        voided_count = int(book["voided"].sum())
        st.caption(f"{len(book)} voucher(s)"
                   + (f", {voided_count} voided" if voided_count else ""))
        # The column only appears when there is something to say, so an
        # untouched book is not given an empty column to read past.
        listed = book.drop(columns=["voucher_id", "voided"])
        if voided_count:
            listed["Status"] = ["Voided" if v else "" for v in book["voided"]]
        st.download_button(
            "⬇️ Day Book CSV",
            data=listed.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"daybook_{date.today().isoformat()}.csv", mime="text/csv")
        chosen = st.dataframe(
            listed, hide_index=True, width="stretch",
            height=table_height(len(book), 20),
            on_select="rerun", selection_mode="single-row",
            column_config={
                "Date": st.column_config.DateColumn("Date", format="DD/MM/YYYY"),
                "Particulars": st.column_config.TextColumn("Particulars",
                                                           width="large"),
                "Debit": amount_col("Debit"), "Credit": amount_col("Credit"),
            })
        st.caption("`Debit ← Credit` shows which ledgers each voucher moved "
                   "value between. Select a row to open it.")

        picked = list((chosen or {}).get("selection", {}).get("rows", []))
        if picked:
            voucher_id = int(book.iloc[picked[0]]["voucher_id"])
            record = db.get_voucher(voucher_id)
            if record:
                head, lines = record
                voided = bool(head.get("voided", 0))
                st.divider()
                st.subheader(
                    f"{head['voucher_type']} voucher no. {head['number']}"
                    + ("  ·  voided" if voided else ""))

                # These sit ABOVE the editor, with the voucher's own heading.
                # Below the grid they were off-screen and went unfound.
                act1, act2, _ = st.columns([1, 1, 3])
                if voided:
                    if act1.button("↩️ Restore voucher",
                                   key=f"restore_{voucher_id}",
                                   width="stretch"):
                        db.restore_voucher(voucher_id)
                        flash(f"{head['voucher_type']} no. {head['number']} "
                              "restored — it counts in the books again.")
                        st.rerun()
                else:
                    if act1.button("🚫 Void voucher", key=f"void_{voucher_id}",
                                   width="stretch"):
                        db.void_voucher(voucher_id)
                        flash(f"{head['voucher_type']} no. {head['number']} "
                              "voided — it keeps its number but posts nothing.")
                        st.rerun()
                with act2.popover("🗑️ Delete…", width="stretch"):
                    st.warning(
                        f"Delete {head['voucher_type']} no. {head['number']} "
                        "for good? This cannot be undone, and the number is "
                        "given up — if it was not the last of its type it "
                        "leaves a permanent gap. To annul it but keep the "
                        "number, void it instead."
                    )
                    if st.button("Delete for good", key=f"del_{voucher_id}",
                                 type="primary"):
                        db.delete_voucher(voucher_id)
                        flash(f"Voucher {head['voucher_type']} no. "
                              f"{head['number']} deleted — its number has "
                              "been given up.")
                        st.rerun()

                if voided:
                    st.info(
                        "This voucher is voided. It keeps its number and stays "
                        "in the Day Book, but posts nothing to the books and is "
                        "left out of every report. It cannot be edited until "
                        "you restore it."
                    )
                else:
                    voucher_editor(f"edit{voucher_id}", head, lines)

# --------------------------------------------------------------------------- #
with tab_ledger:
    st.subheader("Ledger")
    ledgers = db.get_ledgers(include_inactive=True)
    if ledgers.empty:
        st.info("No ledgers yet.")
    else:
        pick1, pick2 = st.columns([2, 3])
        with pick1:
            chosen_ledger = st.selectbox("Ledger", list(ledgers["name"]),
                                         key="ledger_pick")
        with pick2:
            lg_from, lg_to = period_picker("ledger")

        statement = acc.ledger_statement(
            int(ledgers.set_index("name").loc[chosen_ledger, "id"]), lg_from, lg_to)

        st.caption(f"**{statement['ledger']}** · under {statement['group']}")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Opening", acc.fmt_side(statement["opening"]))
        m2.metric("Debit", money(statement["debit_total"]))
        m3.metric("Credit", money(statement["credit_total"]))
        m4.metric("Closing", acc.fmt_side(statement["closing"]))

        entries = statement["entries"]
        if entries.empty:
            st.info("No entries against this ledger in the period.")
        else:
            st.download_button(
                "⬇️ Ledger CSV",
                data=entries.drop(columns=["voucher_id"]).to_csv(index=False)
                         .encode("utf-8-sig"),
                file_name=f"ledger_{chosen_ledger}_{date.today().isoformat()}.csv",
                mime="text/csv")
            st.dataframe(
                entries.drop(columns=["voucher_id"]), hide_index=True,
                width="stretch", height=table_height(len(entries), 20),
                column_config={
                    "Date": st.column_config.DateColumn("Date", format="DD/MM/YYYY"),
                    "Particulars": st.column_config.TextColumn("Particulars",
                                                               width="large"),
                    "Debit": amount_col("Debit"), "Credit": amount_col("Credit"),
                })
            st.caption("**To** is a debit in this ledger, **By** a credit — the "
                       "name is the other side of the voucher.")

# --------------------------------------------------------------------------- #
with tab_trial:
    st.subheader("Trial Balance")
    as_at = as_at_picker("tb")
    grouped = st.toggle("Summarise by group", key="tb_group")
    trial = acc.trial_balance(as_at, by_group=grouped)

    k1, k2, k3 = st.columns(3)
    k1.metric("Total debit", money(trial["debit"]))
    k2.metric("Total credit", money(trial["credit"]))
    k3.metric("Difference", money(trial["difference"]))

    if abs(trial["difference"]) <= acc.EPS:
        st.success("✅ Debits equal credits.")
    else:
        st.warning(
            f"Out by {money(abs(trial['difference']))} — this is the difference "
            "in opening balances, not a problem with your vouchers. Every "
            "voucher is forced to balance when it is saved."
        )

    if trial["rows"].empty:
        st.info("Nothing posted up to this date.")
    else:
        total_row = pd.DataFrame([{
            "Particulars": "Total", "Under": "",
            "Debit": trial["debit"], "Credit": trial["credit"]}])
        shown = pd.concat([trial["rows"], total_row], ignore_index=True)
        if "Under" not in trial["rows"].columns:
            shown = shown.drop(columns=["Under"])
        st.dataframe(shown, hide_index=True, width="stretch",
                     height=table_height(len(shown), None),
                     column_config={
                         "Particulars": st.column_config.TextColumn(
                             "Particulars", width="large"),
                         "Debit": amount_col("Debit"),
                         "Credit": amount_col("Credit")})
        st.download_button(
            "⬇️ Trial Balance CSV",
            data=shown.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"trial_balance_{as_at}.csv", mime="text/csv")

# --------------------------------------------------------------------------- #
with tab_pl:
    st.subheader("Profit & Loss")
    pl_from, pl_to = period_picker("pl")
    report = acc.profit_and_loss(pl_from, pl_to)
    totals = report["totals"]

    st.caption(
        ("Since the books began" if pl_from is None
         else f"{pl_from.strftime('%d/%m/%Y')} to {pl_to.strftime('%d/%m/%Y')}")
        + ". The trading accounts give gross profit; the indirect accounts "
          "take it to net profit."
    )

    g1, g2, g3 = st.columns(3)
    g1.metric("Gross profit" if totals["gross_profit"] >= 0 else "Gross loss",
              money(abs(totals["gross_profit"])))
    g2.metric("Indirect expenses", money(totals["indirect_expense"]))
    g3.metric("Net profit" if totals["net_profit"] >= 0 else "Net loss",
              money(abs(totals["net_profit"])))

    def section(title: str, frame: pd.DataFrame, total: float) -> None:
        st.markdown(f"**{title}** — {money(total)}")
        if frame.empty:
            st.caption("Nothing here.")
        else:
            st.dataframe(frame, hide_index=True, width="stretch",
                         height=table_height(len(frame), None),
                         column_config={"Amount": amount_col("Amount")})

    st.markdown("### Trading account")
    left, right = st.columns(2)
    with left:
        section("Purchases and direct expenses", report["trading_expense"],
                totals["trading_expense"])
    with right:
        section("Sales and direct incomes", report["trading_income"],
                totals["trading_income"])
    st.markdown(
        f"#### {'Gross profit' if totals['gross_profit'] >= 0 else 'Gross loss'}: "
        f"{money(abs(totals['gross_profit']))}")

    st.divider()
    st.markdown("### Profit & loss account")
    left, right = st.columns(2)
    with left:
        section("Indirect expenses", report["indirect_expense"],
                totals["indirect_expense"])
    with right:
        section("Indirect incomes", report["indirect_income"],
                totals["indirect_income"])
    st.markdown(
        f"#### {'Net profit' if totals['net_profit'] >= 0 else 'Net loss'}: "
        f"{money(abs(totals['net_profit']))}")
    st.download_button(
        "⬇️ Profit & Loss CSV",
        data=sections_csv(
            "Profit & Loss",
            ("since the books began to "
             f"{pl_to.strftime('%d/%m/%Y')}" if pl_from is None else
             f"{pl_from.strftime('%d/%m/%Y')} to "
             f"{pl_to.strftime('%d/%m/%Y')}"),
            [("Trading account - purchases and direct expenses",
              report["trading_expense"]),
             ("Trading account - sales and direct incomes",
              report["trading_income"]),
             ("Indirect expenses", report["indirect_expense"]),
             ("Indirect incomes", report["indirect_income"])]),
        file_name=f"profit_and_loss_{date.today().isoformat()}.csv",
        mime="text/csv", key="pl_csv")

# --------------------------------------------------------------------------- #
with tab_bs:
    st.subheader("Balance Sheet")
    bs_as_at = as_at_picker("bs")
    sheet = acc.balance_sheet(bs_as_at)

    b1, b2, b3 = st.columns(3)
    b1.metric("Liabilities", money(sheet["totals"]["liabilities"]))
    b2.metric("Assets", money(sheet["totals"]["assets"]))
    b3.metric("Difference", money(sheet["totals"]["difference"]))

    if abs(sheet["totals"]["difference"]) <= acc.EPS:
        st.success("✅ Both sides agree.")
    else:
        st.error("The two sides do not agree — that should be impossible, "
                 "please report it.")

    st.download_button(
        "⬇️ Balance Sheet CSV",
        data=sections_csv("Balance Sheet", f"as at {bs_as_at}",
                          [("Liabilities", sheet["liabilities"]),
                           ("Assets", sheet["assets"])]),
        file_name=f"balance_sheet_{bs_as_at}.csv", mime="text/csv",
        key="bs_csv")

    left, right = st.columns(2)
    with left:
        st.markdown(f"**Liabilities** — {money(sheet['totals']['liabilities'])}")
        st.dataframe(sheet["liabilities"], hide_index=True, width="stretch",
                     height=table_height(len(sheet["liabilities"]), None),
                     column_config={"Amount": amount_col("Amount")})
    with right:
        st.markdown(f"**Assets** — {money(sheet['totals']['assets'])}")
        st.dataframe(sheet["assets"], hide_index=True, width="stretch",
                     height=table_height(len(sheet["assets"]), None),
                     column_config={"Amount": amount_col("Amount")})
    st.caption(
        "Profit & Loss A/c carries the net profit from the start of the books to "
        "this date. Nominal ledgers are closed into it rather than listed here."
    )

# --------------------------------------------------------------------------- #
with tab_masters:
    st.subheader("Company")
    st.caption(
        "Printed on statements and quoted on returns. Everything but the name "
        "can be left blank until you have it — but a PAN, GSTIN, TAN or CIN "
        "that *is* filled in has to be the right shape."
    )
    # Rendered from db.COMPANY_FIELDS, the same tuple the desktop screen reads,
    # so a field added there appears in both front ends and is validated once.
    saved_company = db.get_company()
    entered: dict[str, str] = {}
    columns = st.columns(3)
    for position, field in enumerate(db.COMPANY_FIELDS):
        current = saved_company.get(field.key, "")
        if field.kind == "multiline":
            entered[field.key] = st.text_area(
                field.label, value=current, help=field.hint or None)
            continue
        slot = columns[position % 3]
        with slot:
            if field.kind == "month":
                entered[field.key] = str(st.selectbox(
                    field.label, list(range(1, 13)),
                    index=max(0, int(current or 4) - 1),
                    format_func=lambda m: calendar.month_name[m],
                    help=field.hint or None))
            elif field.kind == "date":
                entered[field.key] = st.date_input(
                    field.label,
                    value=date.fromisoformat(current) if current else FY_START,
                    format="DD/MM/YYYY").isoformat()
            elif field.kind == "choice":
                choices = list(field.choices)
                entered[field.key] = st.selectbox(
                    field.label, choices,
                    index=choices.index(current) if current in choices else 0,
                    help=field.hint or None)
            else:
                entered[field.key] = st.text_input(
                    field.label, value=current, help=field.hint or None)
    if st.button("Save company details"):
        try:
            db.save_company(entered)
        except ValueError as exc:
            st.error(str(exc))
        else:
            flash("Company details saved.")
            st.rerun()

    st.divider()
    st.subheader("Ledgers")
    st.caption(
        "Every ledger sits under one group, and the group decides which report "
        "the balance lands on. Enter opening balances as a positive amount and "
        "pick Dr or Cr."
    )
    all_ledgers = db.get_ledgers(include_inactive=True)
    groups = db.get_groups()
    group_names = list(groups["name"])

    balances_now = acc.balances().set_index("ledger_id")
    view = all_ledgers[["id", "name", "group_name", "opening", "opening_side",
                        "is_reserved", "active"]].copy()
    view["closing"] = view["id"].map(balances_now["closing"]).fillna(0.0)
    view["Closing"] = view["closing"].map(acc.fmt_side)
    st.dataframe(
        view.drop(columns=["closing", "is_reserved"]).rename(columns={
            "id": "ID", "name": "Ledger", "group_name": "Under",
            "opening": "Opening", "opening_side": "Dr/Cr", "active": "Active"}),
        hide_index=True, width="stretch", height=table_height(len(view), 15),
        column_config={"Opening": amount_col("Opening")})

    with st.expander("➕ Create a ledger"):
        n1, n2 = st.columns(2)
        with n1:
            led_name = st.text_input("Name", key="new_led_name",
                                     placeholder="e.g. Bank of India")
            led_group = st.selectbox("Under group", group_names, key="new_led_group")
        with n2:
            led_open = st.number_input("Opening balance", min_value=0.0, step=1000.0,
                                       format="%.2f", key="new_led_open")
            led_side = st.radio("Dr / Cr", db.SIDES, horizontal=True,
                                key="new_led_side")
        if st.button("Create ledger", type="primary"):
            try:
                db.add_ledger(led_name, led_group, led_open, led_side)
                flash(f"Ledger {led_name.strip()!r} created under {led_group}.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

    with st.expander("✏️ Alter or remove a ledger"):
        if all_ledgers.empty:
            st.caption("No ledgers yet.")
        else:
            target = st.selectbox("Ledger", list(all_ledgers["name"]),
                                  key="alter_led")
            row = all_ledgers.set_index("name").loc[target]
            a1, a2 = st.columns(2)
            with a1:
                alt_name = st.text_input("Name", value=target, key="alt_name")
                alt_group = st.selectbox(
                    "Under group", group_names,
                    index=group_names.index(str(row["group_name"])),
                    key="alt_group")
            with a2:
                alt_open = st.number_input(
                    "Opening balance", min_value=0.0, step=1000.0, format="%.2f",
                    value=float(row["opening"]), key="alt_open")
                alt_side = st.radio(
                    "Dr / Cr", db.SIDES,
                    index=db.SIDES.index(str(row["opening_side"])),
                    horizontal=True, key="alt_side")
            alt_active = st.checkbox("Active", value=bool(row["active"]),
                                     key="alt_active")
            if st.button("Save ledger"):
                try:
                    db.update_ledger(int(row["id"]), alt_name, alt_group, alt_open,
                                     alt_side, str(row["notes"]), alt_active)
                    flash(f"Ledger {alt_name.strip()!r} updated.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

            used = db.ledger_usage(int(row["id"]))
            if used:
                st.info(f"{target} appears in {used} voucher line(s), so it "
                        "cannot be deleted. Untick **Active** to hide it.")
            elif int(row["is_reserved"]):
                st.info(f"{target} is a reserved ledger.")
            elif st.button("🗑️ Delete ledger"):
                try:
                    db.delete_ledger(int(row["id"]))
                    flash(f"Ledger {target!r} deleted.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

    st.divider()
    st.subheader("Groups")
    st.caption(
        "The 27 reserved groups come from Tally and cannot be removed. Add your "
        "own sub-groups under them when you want finer subtotals — a sub-group "
        "inherits its parent's nature."
    )
    st.dataframe(
        groups[["name", "parent_name", "nature", "affects_gross", "is_reserved"]]
        .rename(columns={"name": "Group", "parent_name": "Under",
                         "nature": "Nature", "affects_gross": "Trading",
                         "is_reserved": "Reserved"}),
        hide_index=True, width="stretch", height=table_height(len(groups), 12))

    with st.expander("➕ Create a sub-group"):
        g1, g2 = st.columns(2)
        with g1:
            grp_name = st.text_input("Name", key="new_grp_name",
                                     placeholder="e.g. Vehicle Loan")
        with g2:
            grp_parent = st.selectbox("Under", group_names, key="new_grp_parent")
        if st.button("Create group", type="primary"):
            try:
                db.add_group(grp_name, grp_parent)
                flash(f"Group {grp_name.strip()!r} created under {grp_parent}.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        custom = groups[groups["is_reserved"] == 0]
        if not custom.empty:
            kill = st.selectbox("Remove group", [None] + list(custom["name"]),
                                key="rm_grp")
            if kill and st.button("🗑️ Remove group"):
                try:
                    db.delete_group(kill)
                    flash(f"Group {kill!r} removed.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

    st.divider()
    st.caption(f"Data file: `{db.DB_PATH}`")
