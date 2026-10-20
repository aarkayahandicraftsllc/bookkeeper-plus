# Bookkeeper Plus — double-entry bookkeeping with receivables and GST

**This is a fork of [Bookkeeper](../Bookkeeper), taken at the `accounting-v1`
tag.** That app is the accounting core and is finished: groups, ledgers,
voucher entry, the five reports, multi-company, backups. It stays as it is.

Bookkeeper Plus is where the trading features are built on top:

* bill-wise receivables and payables, with ageing
* GST — tax ledgers, rates on sales and purchase vouchers, input/output tax
  and a GST summary
* later, possibly: inventory, cost centres

**The two are separate applications with separate books.** This one has its
own `data/` folder, started as a copy of the other's, and its own executable
(`BookkeeperPlus.exe`). Entering a voucher in one does not reach the other —
decide which app holds your real books and use that one.

> **Shared code, two copies.** `db.py` and `accounting.py` began identical to
> Bookkeeper's. Fixes to the accounting core are NOT shared automatically and
> have to be applied in both places, so keep those two files as close to the
> original as the new features allow, rather than reorganising them.

Two front ends over one set of books:

```bash
python desktop.py          # native desktop app (PySide6/Qt)
streamlit run app.py       # browser version
```

Both share `db.py` and `accounting.py` and read the same `data/books.db`, so
you can use whichever suits the moment. The accounting lives in those two
modules, not in either interface.

## Why a desktop build

Voucher entry wants a grid that reacts to every keystroke and a key that
switches voucher type without reaching for the mouse. Streamlit reruns the
whole script on each interaction, so the browser version is fine for reports
but never feels immediate enough for data entry. The desktop app is for
entry; use either for reports.

### Keyboard

| Key | Does |
| --- | --- |
| `Alt+Shift+P` `R` `C` `J` `S` `U` | Payment, Receipt, Contra, Journal, Sales, Purchase |
| `Ctrl+A` | Accept the voucher |
| `Ctrl+1` … `Ctrl+8` | Jump between screens (`Ctrl+8` is Company) |
| `Ctrl+S` | Save the company details, on that screen |
| `F3` | Switch company |
| `Alt+L` | Create a ledger from the line you are on |
| `F2` | Set the voucher date |
| `F11` | Refresh every report |
| `Esc` | Back to voucher entry |
| `Ctrl+Shift+1` … `Ctrl+Shift+5` | Colour scheme |
| `Ctrl+P` | Print the report on screen |
| `Ctrl+E` | Export it to a spreadsheet or CSV |
| `Ctrl+B` | Back up these books |
| `Ctrl+Q` | Exit (**Company ▸ Exit**) — offers a backup on the way out |
| `Ctrl+=` / `Ctrl+-` | Text bigger / smaller |

Voucher types are **Alt+Shift+initial**, not plain `Alt+initial` and not
function keys.

*Why Shift:* the menu bar owns the plain Alt letters — `&Go` `&Voucher`
`&Report` `&Company` `&Settings` — and **a menu-bar mnemonic beats a
shortcut**, so Receipt, Contra and Sales would each have opened a menu and
never switched type. That is the same silent dead key `Alt+C` was. Shift
sidesteps the menus, leaving every initial usable.

*Why `U` for Purchase:* Payment already has `P`, so Purchase takes the `U`
of p**u**rchase. No scheme can give both of them their initial.

## Text size

**Settings › Text size** — Normal, Large, Larger, Largest — or nudge it with
`Ctrl+=` and `Ctrl+-` while you are reading. Saved in the books as `text_size`,
alongside the colour scheme.

**Large is the default**, two points above the platform size. Sizes are held as
a *step* above whatever your Windows display setting gives, not as absolute
point sizes, so Normal is always exactly the platform's own size on any machine.

The layout follows the size, which took more than setting the application font.
Three kinds of thing do not follow it by themselves:

* **Fonts built by hand.** `QFont(family)` does **not** inherit the application
  font's size — it comes back at a system default of 12pt. That left the
  monospaced amount columns pinned at 12pt, so they would have looked *smaller*
  than their own labels once the body text grew. Build them with `mono_font()`
  and headings with `heading()`, never `QFont(...)` directly.
* **Widgets whose point size was set once in a constructor** — the screen
  headings and the mono labels. They are marked with a `heading` / `mono`
  property when built, and `set_text_size` finds them again and re-fonts them.
* **The window.** It grows with the text, clamped to the display, so a larger
  font does not push the Accept button under the taskbar.

**Long labels must wrap.** A window can never be narrower than its widest
*unwrapped* label, so one long sentence silently sets a floor for the whole
app — the Balance Sheet note was demanding 1770px, wider than a 1366px laptop
screen, and grew with every step. Report captions go through `flowing()`, which
wraps them. `test_desktop.py` guards the floor: no screen may demand more than
1400px at the largest size, and nothing may be clipped.

## Colour schemes

**Settings › Colour scheme**, in the desktop app. Five choices:

| Scheme | Looks like |
| --- | --- |
| **Light** | The default. Dark text on white, on the platform's own widget style — no banding. |
| **Dark** | Dark grey ground, light text, with the status colours lifted so they stay readable. |
| **Bookkeeper Classic** | The DOS-era accounting screen: deep blue `#000080`, cream text, cyan selection bar. |
| **Bookkeeper Light** | The modern take: light body, a blue-grey band behind the headers, a yellow highlight row. |
| **Follow Windows** | Matches the OS light/dark setting. |

The two **Bookkeeper** schemes are the accounting-screen look in its two eras —
the inky DOS blue, and the light one. Plain **Light** is neither; it is just the
platform's own appearance.

Renamed scheme keys are honoured on the way in — `color_scheme` saved as
`tally` still opens **Bookkeeper Classic**, and `tallyprime` opens
**Bookkeeper Light** — so renaming a scheme never silently resets someone's
choice. The map is `theme.ALIASES`.

The choice is saved in the books (`color_scheme` in the `settings` table), so it
survives a restart and travels with the data file. Switching repaints
immediately — there is nothing to restart.

Two things worth knowing if you add a scheme of your own in `theme.py`:

* **Nothing hardcodes a colour outside `theme.py`.** Four status colours carry
  meaning — a balanced voucher is green, out-of-balance red, a Tally-convention
  warning amber, guidance muted — and each is a *token* looked up on the active
  scheme. Previously they were hex literals, which is fine on white and
  illegible on anything else.
* **`test_desktop.py` measures contrast.** Every token in every scheme is
  checked against both backgrounds it can appear on and must clear the WCAG AA
  ratio of 4.5:1, so a new scheme cannot ship with an unreadable status line.
  The tightest in the current set is 5.22:1.

Light stays on the platform's native widget style, so it is identical to how the
app has always looked; the other schemes use Qt's Fusion style, because the
native Windows style paints from the OS theme and ignores the palette — a dark
scheme on it would keep white table headers and buttons.

The Streamlit version is unaffected; it keeps its browser default.

## More than one company

**The app opens first, then asks.** It comes up on the company you used last,
and a **Select company** dialog appears over the running window with a
dropdown of the companies it found. Keep the one you are in (Cancel), pick
another, or press **New company…**. Inside the app, **F3** (Company ▸ Switch
company) brings the same dropdown back.

Nothing is ever created on the way in. A fresh install opens the default
`books.db` — exactly as the app behaved before it knew about more than one
company — rather than demanding a company be created before you can see
anything. Creating one is always a button you press.

**Each company is its own database file** in `data/`, named after the company:

```
data/aarkaya-handicrafts-llc.db
data/shubh-traders.db
data/books.db
```

That is how Tally keeps a company per data folder, and it is why the accounting
code did not have to change: no query needed a company filter. It also means a
company is backed up, copied or handed to someone by moving one file. The
trade-off is no cross-company reporting — nothing in the app reports across
companies anyway.

**The folder is the list.** There is no index file to fall out of step with
what is on disk, so dropping a company file into `data/` makes it appear and
taking one out makes it vanish. Files with `backup`, `tmp` or `old` in the name
are skipped, or the backup beside your books would show up as a second company
with the same name.

A few details worth knowing:

* Creating a company asks only for **name, financial year, books-begin date and
  currency** — enough to enter a voucher. Address, PAN and GSTIN are filled in
  afterwards on the Company screen, rather than facing you with eighteen fields
  before you can do anything.
* **Two companies may share a name**; the second file gets a `-2` suffix.
* **Switching closes and reopens the window** rather than rebuilding eight
  screens against a different database. If a voucher is part-entered you are
  warned first, since leaving discards it. Choosing the company you are already
  in does nothing at all.
* Because the dialog opens **over a running window**, it simply inherits that
  company's colours and text size — those settings live inside each company's
  file, and a dialog shown *before* the window would have appeared in the
  wrong ones.
* The window remembers **which file it was opened with** (`company_file`), and
  the dialog preselects from that rather than from `db.DB_PATH` — creating a
  company switches the data layer as a side effect, so the two can differ for
  a moment, and a cancel puts it back.
* **Appearance and company details are per company.** Each file carries its own
  `color_scheme`, `text_size`, currency and financial year.
* `BOOKS_DB` **pins the file and switches the picker off** — the test suites
  and anyone pointing at a shared folder have already chosen. `BOOKS_DIR` moves
  the whole company folder.
* The browser version has a **Company selector in the sidebar** instead, since
  a browser tab has no launch moment. It creates nothing; use the desktop app
  for that.

## Backups

Copies of the books live in **`data/backups/`**, named for the company they
came from and when they were taken:

```
data/backups/books-20261018-150337-close.db
data/backups/books-20261018-145823-manual.db
```

**The desktop app offers one when you close it**, so the copy holds the work
you just did: *Back up and close* / *Close without backing up* / *Cancel*,
with backing up as the default so Enter on the way out is the safe answer.
Cancel calls the close off altogether — including a company switch (`F3`),
which gets there by closing the window, so cancelling means staying on these
books.

**When nothing has changed** since the newest backup it says so — *"These
books are already backed up: the copy taken at 10:56 is identical…"* — with
*Close* and *Cancel*. There is always a dialog.

> An earlier version **skipped** the dialog in that case, reasoning that a Yes
> which then wrote nothing (identical books are deduped away) would read as a
> failed backup. That was wrong: closing in silence reads exactly the same
> way, and was reported as the backup not working at all. Say what is true
> instead — there is a copy, it matches, nothing needs writing.

> **What this does not cover.** A close-time backup needs a clean exit. It does
> **not** run if the app crashes, is killed from Task Manager, or the machine
> loses power — so a session that ends badly ends with no backup. That is the
> trade for having the copy contain the current session's work, and it is why
> `Ctrl+B` still matters before anything you are unsure about. Earlier versions
> backed up on *open* instead, which always ran but could never hold the
> session you were in.

**Company ▸ Back up now** (`Ctrl+B`) takes one on demand — for before you do
something you are unsure about. **Company ▸ Open backup folder** opens it in
Explorer. Streamlit gets the button but no automatic copy: it re-runs the whole
script on every interaction and a browser tab has no reliable close, so there
is no moment to hook.

The last **20** per company are kept (`db.KEEP_BACKUPS`) and older ones pruned.
These are tens of kilobytes, unlike the 144 MB the exe archive has to ration.
A prune that cannot delete a file gives up on that file rather than failing —
Windows will refuse to delete what something else has open, and a backup folder
you happen to be browsing must not be able to stop a backup being taken.

Three things about how it works that are not obvious:

* **It uses SQLite's own online backup, not a file copy.** A copy taken while a
  write is in flight can capture a torn database, and it looks perfectly fine
  right up to the day you need it. `test_books.py` runs `PRAGMA
  integrity_check` on a backup and compares its voucher count with the live
  books, because "a file appeared" is not the same as "a backup exists".
* **Each backup carries the hash of the books it came from**, stamped into its
  own `settings` table as `backup_source_sha256`, alongside which file and when.
  This is what lets an unchanged book be skipped instead of copied again —
  opening the app five times without entering anything leaves one copy, not
  five, so the useful ones are not pushed out. It has to be *carried* rather
  than recomputed: the backup API rebuilds the file page by page, so the copy
  is logically identical but **not byte-identical** to its source and hashing
  the copy tells you nothing about where it came from.
* **They sit in a subfolder so they can never be mistaken for a company.**
  `company_files()` globs `*.db` without recursing, so the folder is invisible
  to the picker whatever the files are called. The name-based guard
  (`_NOT_COMPANIES`) still covers the older flat `books.backup-....db`.

**What this does not protect against.** The backups are on the same disk as the
books. They cover a mistake, a bad import or a corrupted file; they do **not**
cover the drive failing, the laptop being lost, or the folder being deleted. For
that, `data/` needs to be copied somewhere else — another drive, or a synced
folder. Nothing in the app does that for you.

## Company details

**Ctrl+8**, or Masters → Company in the browser version. Eighteen fields:

| | |
| --- | --- |
| Identity | Company name (required), Mailing name |
| Address | Address line 1, 2 and 3, City, State, Country, ZIP / PIN code |
| Contact | Phone, E-mail |
| Registrations | PAN, GSTIN, TAN, CIN |
| Books | Financial year begins, Books begin on, Currency symbol |

The address is **separate lines**, not one box, so it can be printed and
addressed field by field. It *was* one multi-line box: books written before
2026-10-20 have their old address split across the lines when they are next
opened, and the original text kept under `address_legacy` in case the split
read wrongly. **ZIP / PIN code** takes a six-digit Indian PIN or a five-digit
ZIP (with or without the +4).

### Date format

**Date format** picks between `DD/MM/YYYY` and `MM/DD/YYYY`, and governs every
date the app shows or takes: the voucher date, a bill's due date, the From/To
editors on the reports, the Date and Due columns on the Outstanding report,
the period on a printed page.

A set of books with no format chosen takes its first one **from the currency**
— `$` starts on `MM/DD/YYYY`, anything else on `DD/MM/YYYY`. That is only the
default. The two are separate settings once chosen, deliberately: changing the
currency must not silently reorder dates that are already on screen, and there
is no reading of a figure that makes `03/04` safe to reinterpret.

**Dates are stored as ISO (`YYYY-MM-DD`) regardless** — in every table, in
every setting, and in the frames the reports are built from. Only two places
format them: the table on screen, and the printed page. **A spreadsheet export
keeps the ISO date**, so it still sorts and filters on arrival — the same split
the money columns make, and for the same reason. This matters more here than
in Bookkeeper: ageing is arithmetic on those dates.

`db.date_format()` and `db.show_date()` are the only two functions that know
any of this; nothing else may hardcode a format.

**Blank is always allowed** except the name — these are details you fill in as
you learn them, and refusing to save a half-known address would be worse than
useless. But a registration that *is* filled in has to be well formed, because
a malformed GSTIN is worse than an absent one: it reaches a filing looking
legitimate. PAN, GSTIN, TAN and CIN are stored upper case, and the form shows
them back that way so you can see it happened.

The sharpest check is the cross-reference: **a GSTIN contains its holder's PAN**
at characters 3–12, so if both fields are filled and they disagree, one of them
is wrong — and this is the only place that would ever be noticed.

```
The GSTIN carries PAN BBBPZ1234C, but the PAN field says AAAPZ1234C.
A GSTIN always contains the holder's PAN, so one of the two is wrong.
```

A rejected save **writes nothing and keeps your typing** — losing a form to one
bad character is worse than the bad character.

The field list lives in `db.COMPANY_FIELDS`, and both front ends render from it
and save through `db.save_company`. Adding a field to that tuple puts it on the
Qt screen and in the Streamlit form, validated the same way, with nothing
further to write.

## Creating masters mid-voucher

You rarely know every account before you start entering. Press **Alt+L** (or
the *Create ledger* button) on the voucher screen and the ledger is created
there and then, dropped into the line you were on, and usable immediately.

> The shortcut is **Alt+L, not Alt+C**. Two reasons: the menu bar holds
> `&Company`, and a menu-bar mnemonic beats a widget shortcut, so Alt+C only
> ever opened that menu — the shortcut never fired. Alt+C is also the key
> Tally uses, and this app deliberately does not copy its shortcuts. Any new
> Alt shortcut has to dodge the menu letters G, V, R, C and S.

Two details that make it worth using rather than breaking off:

* **Whatever you had typed into the cell becomes the name**, so a half-typed
  account is not wasted.
* The dialog **says where the balance will land** — "a liability account, its
  balance will appear on the liabilities side of the Balance Sheet" — because
  the group you pick is the decision that matters, and it is easy to get wrong.

If the group you need does not exist either, **New group…** inside that dialog
creates it without losing the ledger you were part-way through.

The Streamlit version has the same thing as an expander above the grid.

## Structure

It follows Tally's, because the reports fall out of it:

```
Group  →  Ledger  →  Voucher line
```

* **Groups** form a tree, with the 27 reserved groups seeded as Tally has them
  — Capital Account, Sundry Debtors under Current Assets, Sundry Creditors
  under Current Liabilities, Duties & Taxes, Direct and Indirect Expenses, and
  so on. A group's **nature** (asset / liability / income / expense) decides
  which statement a balance lands on. A separate **trading** flag marks the
  accounts that produce gross profit.
* **Ledgers** are what you post to. Each sits under exactly one group and may
  carry an opening balance, entered as a positive amount plus Dr or Cr.
* **Vouchers** hold two or more lines whose debits and credits must agree.

Add your own sub-groups under any reserved group when you want finer
subtotals; a sub-group inherits its parent's nature.

Balances are held **debit-positive** internally: `+500` is 500 Dr, `-500` is
500 Cr. Presentation flips it back.

## Screens

| Screen | What it does |
| --- | --- |
| **Voucher Entry** | Type, date, auto number, narration, then a Dr/Cr grid with ledger autocomplete and running totals. Accept is disabled until the two sides agree. |
| **Day Book** | Every voucher in the period, newest first. Double-click to alter one. |
| **Ledger** | One account: opening balance, every entry with **To**/**By** particulars naming the other side, a running balance, closing. |
| **Trial Balance** | Closing balances as Dr and Cr, by ledger or summarised by group. |
| **Profit & Loss** | Trading account → gross profit, then the indirect accounts → net profit. |
| **Balance Sheet** | Liabilities and assets side by side, with net profit carried to Profit & Loss A/c. |
| **Masters** | Create and alter ledgers and sub-groups. |
| **Company** | Who the books belong to: name, address, PAN, GSTIN, TAN, CIN, financial year, currency. |

## Choosing the period

Every report carries a **Period** dropdown listing the financial years the
books touch — `FY 2025-2026`, `FY 2026-2027` — so "the balance sheet for
2025-2026" is one choice rather than two typed dates. The From/To editors stay
beside it for a period that is not a whole year.

A year means the **whole** year, opening day to closing day — including a
closing day still in the future for the year in progress. A year named in a
dropdown has to mean that year; clamping the end to today, as this first did,
quietly reports a different period from the one on screen.

The list comes from the **data** — voucher dates and the books-begin date — so
it grows by itself and there is nothing to maintain. The year before the
current one is offered even with nothing posted in it, because comparing with
last year is routine and a prior-year balance sheet is meaningful from the
opening balances alone. Earlier years than that have to be earned by a voucher
dated in them.

A financial year is **not** assumed to start in April. It follows
`fy_start_month` on the Company screen, and books that run the calendar year
are a different shape rather than a special case: the year ends in December of
its own year and is labelled `2026`, since `2026-2026` would be nonsense.

**Custom dates…** asks for them. A period report is asked for a From and a To;
an as-at report is asked for one date, because a balance sheet is drawn at a
moment and its balances run from the beginning of the books whatever you put in
a start field — offering one would be offering a control that does nothing. The
dialog refuses a period that ends before it starts, and cancelling leaves both
the dates and the dropdown exactly as they were.

Choosing it used to do nothing at all: the dropdown changed, the editors were
left alone, and if **Since books began** had been chosen first the From editor
was still disabled, so a custom period could never be given a start date.
Typing in the date editors is the other way in, and it re-enables From for the
same reason.

Editing a date by hand also moves the dropdown to **Custom dates…**. Leaving it
reading "FY 2026-2027" over a period that is no longer that year would be a
lie about what you are looking at — and the one thing a report must not do is
misdescribe its own period. Period reports (Day Book, Ledger, P&L) also carry
**Since books began** in the same dropdown rather than beside it as a tickbox:
it is one of the periods you can choose, and a row holding both was 1468px
wide at the largest text size, over the 1400px ceiling.

The arithmetic is in `accounting.py` — `fy_bounds`, `fy_label`,
`financial_years`, `current_financial_year` — and **shared by both front
ends**, which each used to work it out for themselves. That is two places for
"when does the year end" to be wrong in different ways. `test_books.py` works
the boundaries by hand: April and January years, the first and last day of a
year, a leap day, an unbroken run of years with no gaps or repeats, and a
start month that is garbage falling back to April rather than stopping the
reports from opening.

## Printing and exporting

Every report screen carries **Print…** (`Ctrl+P`) and **Export…** (`Ctrl+E`),
also under the **Report** menu. A report nobody can get out of the window is
half a report — it has to reach an auditor, a bank, or an email.

**Print** opens a **preview** rather than going straight to a print dialog:
these are wide tables, and whether one fits the page is the first thing anybody
wants to know. Saving as PDF is the *Microsoft Print to PDF* entry in the
printer list, so there is no separate PDF command to keep working.

**Export** writes an **Excel workbook** (a sheet per table, each headed with the
company, the report and the period) or a **CSV** if you name the file `.csv`.
Both are offered in the save dialog, which **opens on your Downloads folder**
with a name already filled in — and the file is **opened** once it is written.
Opening it is most of the point: an export you then have to go and find is a
job in itself, and Downloads is where you would look.

The folder is asked of the platform rather than assumed, since it is localised
and can be moved. Writing the file and launching an application are kept apart
(`export_report` and `export_and_open`), because anything scripted wants the
first without the second. If the file cannot be opened — no application
associated with it — the path is still reported, since a file written but not
opened has to remain findable.

Two deliberate differences between the two outputs:

* **The printed page is formatted; the spreadsheet keeps numbers.** Figures
  print as `1,234.00`, grouped and right-aligned, because that copy is read by
  a person. The spreadsheet gets the raw number, because that copy is summed
  and pivoted — a column of `"1,234.00"` *strings* is a file you have to repair
  before you can use it.
* **A CSV holds one table, so a report with four of them is laid out, not
  dumped.** Blank lines and headings are what let a reader tell where the
  trading account ends and the indirect one begins. The Streamlit download
  writes the same shape.

A screen describes itself by returning a `Report` — a title, a period, a list
of `ReportSection` tables, and the notes that carry the totals. Preview, print,
Excel and CSV all work off that description alone, so a new report gets all
four by writing one method. The notes matter more than they look: the gross and
net profit, the closing balance, the opening-balance difference all live in
screen labels rather than in any table, and a printed report without them would
be missing the figures somebody actually wanted.

`Ctrl+P` on Voucher Entry, Masters or Company says so in the status bar rather
than printing something arbitrary — those screens are input, not reports.

Streamlit has a **⬇️ CSV** download on each of its five reports.

## The rules it enforces, and the ones it only advises

**Hard:** a voucher must balance, needs at least two lines, across at least two
different ledgers, each line on one side only. This is checked in `db.py`, not
just in the form, so it holds however the call arrives.

**Advisory:** the cash and bank conventions per voucher type — a Payment should
credit cash or bank, a Receipt should debit it, a Contra should be between two
of them, a Journal should touch neither. You get told, not blocked, because
real books occasionally need the exception.

## GST

Set a **GST rate** on each sales and purchase ledger (Masters ▸ GST rate %), and
a **GSTIN** on each customer and supplier. Then on a Sales or Purchase voucher,
**GST… (Alt+T)** works out the tax and writes the tax lines onto the voucher.
After that it is an ordinary voucher — which is why every existing report
already understands it.

**Whether it is CGST+SGST or IGST is derived, not asked.** The first two
characters of a GSTIN *are* the state code, so the place of supply against our
own state is the whole decision. Our own state comes from our GSTIN rather than
the typed State field, because the two can disagree and the tax follows the
GSTIN. A place of supply left as *unregistered or unknown* counts as
intra-state, which is what a counter sale is.

Intra-state halves the rate. Each half is rounded on its own and the second
takes the remainder, so 18% of 1,050 is 94.50 twice rather than losing a paisa
against the invoice.

The **GST screen** shows a summary — output tax, input tax and the net payable,
broken down by rate and by where the supply went — or either register, invoice
by invoice, in the shape an outward-supplies return asks for. All of it prints
and exports.

**A blank rate is not a zero rate.** Blank means the ledger is not taxable at
all; `0` means it is taxable at nil rate, and a nil-rated supply still belongs
on the return. That is why `ledgers.gst_rate` is nullable, and why
`db.set_ledger_gst` exists separately from `db.update_ledger`: there `None`
means "leave this alone", which is what stops a caller that knows nothing about
GST from wiping a rate while saving a name.

Six tax ledgers are seeded under **Duties & Taxes** and marked with a
`gst_role`, so the report finds them however they are renamed and cannot lose
them to a delete.

**What is stored and what is worked out.** `gst_details` holds only what the
lines cannot say: the rate, the counterparty's GSTIN, the place of supply.
Taxable value and tax are summed from the voucher's own lines, so a correction
reports itself with nothing to re-run and the return can never disagree with
the Duties & Taxes ledgers. The GSTIN and place of supply *are* stored,
precisely because they are a snapshot — a return filed for August has to keep
saying what it said then, even after the customer moves state or registers.

> **Known limit: one rate per voucher.** GST is worked out at a single rate for
> the whole voucher. If its taxable lines carry different rates you are told,
> and offered the highest; split the invoice if that is wrong. Mixed-rate
> invoices need tax per line, which needs line-level GST records.

> **This is not a filing tool.** It tells you what you owe and lists the
> supplies behind it. There is no GSTR upload, no e-invoicing, no reverse
> charge, and no composition scheme.

## Voiding a voucher, and deleting one

Two different things, and the difference is the voucher **number**.

**Void** annuls a voucher but keeps it. It holds its number, stays listed in
the Day Book marked `Voided`, and keeps its lines — but it posts nothing, so
it drops out of every balance, the trial balance, both statements and the
ledger. Use it when an entry should not count but its number must stay spent:
that is what keeps an invoice series unbroken, which matters because a gap in
a sales invoice series is not allowed under GST. A voided voucher is **frozen**
— it cannot be edited, because it is a record of what was annulled rather than
a draft. **Restore** brings it back unchanged.

**Delete** removes the voucher outright and gives up its number. If it was the
last of its type the number is handed to the next voucher; otherwise it leaves
a permanent gap. Nothing is kept and it cannot be undone, so the confirmation
says so and offers to void instead.

Both live on the voucher itself: in the desktop app, open a voucher from the
Day Book or a ledger and the buttons are at the bottom left, a deliberate
distance from Accept. In the browser version, select the voucher's row in the
Day Book and they sit beside its heading.

How this stays correct across every report: the whole accounting layer reads
voucher lines through the single `db.get_lines()`, which leaves voided
vouchers out unless asked. So no report code knows that voiding exists. The
Day Book is the one caller that passes `include_voided=True`, on purpose,
and shows those rows as nil.

Books made before this feature existed gain the column when they are next
opened — `db._migrate()` adds it, because `CREATE TABLE IF NOT EXISTS` leaves
an existing table alone. That is also where a settings key that has *moved*
gets moved, such as the old single-box address.

## Difference in opening balances

Because every voucher balances, the sum of all closing balances always equals
the sum of all **opening** balances. So if your openings disagree, the trial
balance will too — and that is not a problem with any voucher.

Rather than hide it, the residual is shown as **Difference in opening
balances**: as a gap on the trial balance, and as its own line on the balance
sheet so that statement still balances. That is what Tally does.

## Packaging a desktop executable

```bash
pip install -r requirements-desktop.txt pyinstaller
python build_exe.py
```

Produces **`Bookkeeper.exe` in this folder, beside `data/`** — copy the two
together anywhere and double-click, no Python needed on that machine.

**The books live in a `data` folder next to the executable.** That is why the
exe is built here rather than into `dist/`: here, the packaged app and
`python desktop.py` read the *same* books. Built into `dist/` they had a set
each, and vouchers entered through one were missing from the other. Set
`BOOKS_DIR` to override — a OneDrive or shared folder, say.

> **This was a real data-loss bug, fixed 2026-10-16.** The data folder was
> anchored on `__file__`, and a PyInstaller *onefile* build unpacks itself into
> a temporary directory that is **deleted when the app exits** — so the
> packaged app wrote its books somewhere that ceased to exist the moment it
> closed, having looked throughout as though it had saved. A company created in
> the exe, with journals entered, was gone on the next launch and was not
> recoverable. `db._app_dir()` now anchors on `sys.executable` when
> `sys.frozen` is set. It is invisible from source, where `__file__` is
> correct, so `test_books.py` pretends to be frozen and asserts the anchor
> follows the executable.
>
> For the same reason `build_exe.py` no longer wipes its output folder — that
> folder holds real books.

## Tests

```bash
python test_books.py                               # the accounting
python test_ui.py                                  # the Streamlit front end
QT_QPA_PLATFORM=offscreen python test_desktop.py   # the desktop front end
python test_multi_company.py                       # choosing a company
```

The first three set `BOOKS_DB`, which pins the data file and switches the
company picker off — so they cover everything *except* the way the app actually
starts. `test_multi_company.py` runs unpinned, with `BOOKS_DIR` on a scratch
folder, and drives the real launch path in both front ends: creating companies,
that their books are genuinely separate, the picker's listing and preselection,
switching, and that a backup file is not mistaken for a company.

All four point `BOOKS_DIR` at a temporary folder, so no suite can write a
company into your real `data/` directory.

`test_books.py` works a full set of books by hand — opening balances, a credit
sale part received, a credit purchase part paid, a contra, bank interest — and
asserts the trial balance and balance sheet differences are exactly `0.0` with
no tolerance, including after edits, deletes, voids and custom sub-groups. For
voiding it checks both halves of the bargain: that the voucher leaves every
report, and that its number is *not* handed on to the next one. It also builds
a database without the `voided` column and opens it, to prove an older book is
migrated rather than broken.

`test_desktop.py` drives the real Qt widgets with the offscreen platform, so
the desktop build is tested rather than only eyeballed: function keys, the
disabled Accept button, the balance check, the voucher-type advice, posting,
altering from the Day Book, voiding and restoring, the three ways out of the
delete confirmation, and creating masters.

What a screen LOOKS like is the one thing these cannot assert. `screenshot.py`
renders the real screens to PNGs in `shots/`, so a change to any of them can be
compared against the build before it:

```bash
python screenshot.py                        # every scheme, three screens
python screenshot.py --scheme dark --tag after
python screenshot.py --screens 0 3          # Voucher Entry, Trial Balance
```

It copies your books into a scratch folder and pins `BOOKS_DB` to the copy, so
taking a screenshot cannot write to `data/`, and it never shows the window. It
runs on the *native* platform rather than offscreen on purpose: Light keeps the
native widget style, and offscreen would render it as Fusion — that is, not the
thing being judged.
