# Finance Tracker

**A private personal-finance dashboard that runs entirely on your own computer.** Download CSV statements from
your banks' websites, drop them in, and it shows your net worth, where your money goes each month, spending by
category, how your investments are growing, and who owes whom among family and friends. There are no bank
logins, no cloud services and no subscriptions: your data lives in one file on your machine, and categories are
learned locally from your own corrections. It's a small Python app that opens in your browser — try it with
fake data in five minutes (`python app.py --demo`).

> **Using an AI coding agent?** Point it at [`CLAUDE.md`](CLAUDE.md) (or [`AGENTS.md`](AGENTS.md)) — the full spec,
> written so an agent can understand the rules and adapt the app to you. It also explains which folders an agent
> must never read (your financial data).

---

## Contents

1. [Quick start](#1-quick-start)
2. [Your first hour — a walkthrough](#2-your-first-hour--a-walkthrough)
3. [The monthly routine](#3-the-monthly-routine)
4. [How it works — the concepts](#4-how-it-works--the-concepts)
5. [The pages](#5-the-pages)
6. [Importing in detail](#6-importing-in-detail)
7. [Customizing it (for you or your AI agent)](#7-customizing-it-for-you-or-your-ai-agent)
8. [Terminal commands](#8-terminal-commands)
9. [Privacy & safety](#9-privacy--safety)
10. [Troubleshooting](#10-troubleshooting)
11. [Project layout & development](#11-project-layout--development)

---

## 1. Quick start

You need **Python 3.11 or newer** (`python3 --version` to check). Then, in a terminal, from this folder:

```bash
python3 -m venv .venv                 # one-time: a private Python environment for this app
source .venv/bin/activate             # Windows: .venv\Scripts\activate
pip install -r requirements.txt       # one-time: Flask, pandas, scikit-learn, ...
```

**Look around with fake data first:**

```bash
python app.py --demo
```

Open **http://127.0.0.1:5000** in your browser. Everything you see is invented, stored separately in
`data/demo.db`; a yellow "Demo data" banner reminds you. Stop the app with `Ctrl+C`.

**Use it for real:**

```bash
python app.py
```

Same address. It starts empty, with a **Getting started** checklist on the Overview page.

Every later session is just: open a terminal in this folder, `source .venv/bin/activate`, `python app.py`.
The terminal sitting there without returning to a prompt is normal — that means it's serving. (On a Mac, if the
page says "Access denied", see [Troubleshooting](#10-troubleshooting).)

---

## 2. Your first hour — a walkthrough

### Step 1 — Add your accounts (Net Worth page → Add Account)

Add every account you want to track: checking, savings, credit cards, loans, investment accounts, Venmo/PayPal,
cash. For each, give:

| Field | What to enter |
|---|---|
| **Name** | Anything you'll recognize — "Chase Checking", "Amex Gold". It's also the name of the account's statements folder. |
| **Institution** | The bank or company. |
| **Type** | `checking`, `savings`, `credit_card`, `loan`, `brokerage`, `retirement`, `digital_wallet` (Venmo, PayPal, …) or `cash`. |
| **Currency** | The account's own currency. |
| **Owner** | *Mine*, *Joint*, or *Someone else pays it* (e.g. a card in your name that a parent pays off — see [IOUs](#iou-ledger)). |
| **Starting balance** (optional) | What the account held on a given date. Use this when the account had money in it before the statements you'll import begin. For a credit card or loan, enter what you owe as a positive number. Skip it for investment accounts (they get values on the Investments page) and for cash/wallets (log what's in them as a first transaction). |

### Step 2 — Download statements from each bank

On each bank's website, open the account, find **Download**, **Export** or **Statements**, choose **CSV** (sometimes
"Spreadsheet" or "Excel CSV") and a date range — the last few months is a good start. Banks usually allow 1–2
years back. PDFs won't work; CSV does.

### Step 3 — Import them (Import page)

1. Pick the account the files belong to, choose the files, **Add files**.
2. Click **Preview**. For each file you'll see how many rows it has, the date range, possible duplicates, and its
   first few rows. **Check the signs:** money going out (purchases, bills) should be negative (red). If a card's
   purchases show as positive, set that file's *Amount signs* to **Flip all signs** and preview again.
3. Click **Import**. Rows are added, categories are suggested, and each file that went in cleanly is deleted
   (statements are sensitive; once the data is in, the file isn't needed).

If a file can't be read, it stays in place with the reason shown — see [Importing in detail](#6-importing-in-detail).

**Have years of history in a spreadsheet?** That can come in once, too: open *Bring in older history* on the
Import page and follow the template ([details](#bringing-in-older-history)).

### Step 4 — Review on the Transactions page

Filter to **All**, then work through:

- **Yellow rows** — a category was filled in automatically. Fix it if wrong; tick the row and **Save changes** to
  confirm it if right. Each correction teaches the categorizer.
- **Rows with no category** — type one. Categories are free text ("Groceries", "Rent", "Kids"); suggestions appear
  as you type.
- **Type** — the most important column. Make sure transfers between your own accounts (checking → savings, paying
  your card) are `internal_transfer` on *both* sides; otherwise money gets counted as spending twice. See
  [Type vs. Category](#type-vs-category).
- **Pink rows** — possible duplicates (same account, date and amount as another row). Delete the extra one, or
  tick and Save to keep both.

### Step 5 — Look at the results

- **Cash Flow** — income vs. spending vs. investing for the month, and your savings rate.
- **Spending** — where it went, by category, compared with the previous period.
- **Net Worth** — every account's balance. If one marked *from transactions* doesn't match your bank, use **Set
  Balance** with what the bank shows today.
- **Investments** — add each account's current value from its latest statement (**Add a Snapshot**).

---

## 3. The monthly routine

About 10 minutes once a month:

1. Download the newest CSV from each bank (overlapping the previous download a little is fine — overlaps are
   flagged, never lost).
2. **Import page** → add files → Preview → Import.
3. **Transactions** → review yellow and pink rows; check the Type of transfers.
4. **Investments** → add each account's current value from its statement.
5. Glance at **Cash Flow** and **Spending**.

---

## 4. How it works — the concepts

These rules are what make the numbers trustworthy. The in-app **Help** page has the same material, shorter.

### Amounts and signs

Every transaction has a signed amount: **negative = money out of the account, positive = money in.** A $50
purchase on a credit card is `-50`; a payment to that card is `+`. A credit card's or loan's *balance*, though, is
stored and entered as a positive "amount owed", and shown negative (red) on Net Worth.

### Type vs. Category

Every transaction has both, and they do different jobs:

- **Type** decides what a row *counts as*. It's a fixed list:

  | Type | Meaning | Counts in |
  |---|---|---|
  | `income` | Money you earned or received that's yours | Cash Flow (Inflow) |
  | `expense` | Money you spent | Cash Flow (Living Expenses), Spending |
  | `internal_transfer` | Between your own accounts — checking → savings, paying your own card, ATM → your Cash account | Nothing (it's not income or spending) |
  | `personal_transfer` | Between you and another person, but not income or spending — paying someone back, being paid back, money you're passing along | Nothing (track it as an [IOU](#iou-ledger) if you like) |
  | `investment_contribution` | Money moved into an investment account | Cash Flow (Investing), Investments |

- **Category** is a free-text label for your own use (filtering, the Spending breakdown). Changing a category never
  changes what a row counts toward — with one deliberate exception, [Pass-Through](#spending-and-pass-through).

**Transfers have two sides.** Moving $500 from checking to savings is a `-500` row on checking *and* a `+500` row on
savings, both `internal_transfer`. Paying your credit card is a `-` row on checking and a `+` row on the card, both
`internal_transfer`. The card side is usually tagged automatically (it says "Payment Thank You"); the checking side
is worded differently by every bank and is the one to check. If it's left as `expense`, your purchases are counted
twice — once when you made them, again when you paid the card. (`python -m src.maintenance retag-card-payoffs`
fixes these in bulk.)

### "Not Mine"

For a real purchase (or real income) that wasn't your own money — e.g. something a relative bought for themselves
on a card in your name. Tick it on Transactions and click **Mark Not Mine**. It stays in Spending's categories
(it's still "Dining on this card"), but is left out of Cash Flow's income vs. spending, and out of automatic IOUs.
Different from `personal_transfer`, which is money moving with no purchase at all.

### How each account's balance is worked out

The app never asks you to type balances every month. Instead:

| Account | Balance = |
|---|---|
| Checking, savings, credit card, loan | The most recent **recorded balance** (a starting balance, a *Set Balance* entry, or one from a history import) **plus every transaction dated after it**. With no recorded balance at all, the sum of all its transactions (shown with a grey *from transactions* tag). |
| Cash, digital wallet | Always the sum of all its transactions. |
| Brokerage, retirement | The most recent **current value** you entered on Investments, plus deposits since (at face value). Market moves show up when you enter the next value. |

**Net worth** is the sum of those balances (liabilities subtracted), converted to the
[base currency](#currencies). Accounts *someone else pays* are left out entirely. Overview's headline figure
also leaves out investment accounts, so it doesn't jump with the market; the Net Worth page includes everything.
The trend line evaluates every account on the same date at each point, so a transfer between accounts never
shows up as a spike.

### Cash flow: income and outputs always balance

For any period:

```
outputs  = Living Expenses (expense rows, not Not Mine)
         + Investing (investment_contribution rows)
         + Paid by Others (manual IOU entries where someone paid for something of yours)
income   = income rows (not Not Mine)
residual = income − outputs
```

If `residual ≥ 0`, it appears as a **Cash Savings** output. If it's negative, it appears as a **From Savings**
*input* — you dipped into savings. Either way, both sides of the Sankey diagram are equal. Transfers never appear.
**Savings rate** = (Investing + Cash Savings) ÷ income. Everything is converted to the base currency first.

### Spending and pass-through

Spending counts only `expense` rows, grouped by Category, with each category's change from the same-length
previous period. Rows whose category is **Pass-Through** (any spelling) are hidden by default — money that came in
and went straight back out, like a work reimbursement — with a switch to include them. This is the one place a
category name changes the math.

Cash Flow doesn't read the category, so a pass-through row also needs **Mark Not Mine**; otherwise it counts as your
own income and spending (inflating income, lowering the savings rate). Until that's done, Transactions shows a yellow
reminder and its review filter offers **Pass-through to finish**.

### Investments: principal, value, growth

For each brokerage/retirement account:

- **Principal** = all the money you put in: every matched deposit from Transactions + any **Principal added** you
  typed on the Investments page.
- **Current value** = your last typed-in value (from a statement) + deposits since.
- **Growth** = value − principal.

**Deposits find their account by themselves.** On the bank account the money left, give the row Type
`investment_contribution` and name the investment account in its **note** — its full name or just its first word,
any capitalization (`roth` → "Roth IRA"). If the note doesn't say, the description is checked the same way
("Transfer to Roth IRA"). Anything that can't be matched is listed at the top of the Investments page to sort out.
Tip: give investment accounts distinctive first words.

### IOU ledger

The **IOUs** page keeps one running balance per person — a parent, a roommate, a friend — of **what you owe them**
(negative means they owe you). Entries have one of two directions:

- **They paid** — money or value from them to you (they paid for something of yours, lent you money, or paid you
  back). Increases what you owe them.
- **I paid** — from you to them (you paid them back, lent them money, paid for something of theirs). Decreases it.

Entries come from three places:

1. **Automatically:** every purchase on an account whose owner is *Someone else pays it* counts as *They paid* for
   that person (unless marked Not Mine). Typical case: a card in your name that a parent pays off.
2. **Marked by you:** on Transactions, tick rows, type the person's name in the *Person* box and choose
   *They paid* / *I paid* from the **IOU…** menu. Nothing else is guessed — a transfer to a friend could be a
   loan, a repayment or a gift.
3. **Manual entries** on the IOUs page, for things with no transaction (cash, or someone paid a bill directly).
   A **starting balance** says "as of this date the balance was exactly X" and replaces older entries for that
   person.

*Settled / Pending* marks are reminders only and never change a balance. IOUs never change net worth.

**Example — splitting a bill:** you pay an $80 internet bill, half of it a roommate's. Edit the row to $40
`expense` (your half) and add a $40 `personal_transfer` row marked *I paid* → roommate. When they Venmo you $40,
mark that row *They paid* → roommate (Type `personal_transfer`). Their balance returns to zero, and only your $40
counts as your spending.

### Categorization

Three layers fill in categories, in order, always on your own machine:

1. **Rules** — fixed text patterns in `src/categorize.py` (`CATEGORY_RULES`), e.g. "payment thank you" →
   `internal_transfer`. Add your own.
2. **Your bank's category**, when the CSV has one (e.g. Chase's "Food & Drink" → Dining), via a mapping in
   `src/parsers/base.py`.
3. **A local classifier** (scikit-learn Naive Bayes on the description) trained only on your own categorized rows.
   It only guesses when reasonably confident — "I don't know" is a valid answer — and gets better as you
   categorize.

Anything filled in automatically is highlighted yellow ("needs review") until you edit or confirm it. No
transaction data is ever sent to any online service, AI or otherwise.

### Duplicates are flagged, never dropped

When an imported, typed or edited row matches an existing row's account, date and amount, it's **kept and
highlighted pink** rather than silently skipped — two genuinely different transactions can share every field (two
bus rides, same day, same fare), and the data alone can't tell. You decide: delete it, or confirm it. Consequence:
importing the very same file twice adds a flagged second copy of everything.

### Currencies

**It's automatic.** While all your accounts are in the base currency, the app hides everything currency-related: no
currency codes, no per-currency tables, no exchange rates. Overview shows plain **Balances**. As soon as any account
uses a second currency (Net Worth → Add Account → **Other currency?**), the pages switch on their own: Overview shows
**Balances by Currency**, Net Worth gets one table per currency plus the rates used, and amounts carry their currency
code. Nothing to configure.

Each account has its own currency, and its balances are always shown in it — Net Worth has one table per currency.
Totals that must combine currencies (the Net Worth figure, Cash Flow, Spending, IOUs) are converted to the **base
currency** (USD by default) with fixed rates in `src/fx.py`. Rates are edited by hand, never fetched; the ones
shipped are rough placeholders — update them. See [Customizing](#7-customizing-it-for-you-or-your-ai-agent) to
change the base currency or add one.

---

## 5. The pages

| Page | What it's for |
|---|---|
| **Overview** | Net worth (excluding investments) and its trend; account balances (by currency, once you have more than one); debts, loans and IOU balances; where income went; top spending categories; a small Sankey. Shows a *Getting started* checklist until you have data. |
| **Net Worth** | Your accounts grouped Cash / Investments / Credit / Loans, with 30-day changes (one table per currency, plus the converted total, once you use more than one); accounts someone else pays; **Set Balance**; the **CDs & Bonds** memo table (each one's balance = principal + interest you log with **Add interest**); **Add Account**. |
| **Cash Flow** | Inflow, Living Expenses, Saved + Invested, Savings Rate, and the Sankey diagram for a period. |
| **Spending** | Category bars with share and change vs. the previous period, a donut, per-category trend lines. Click a category to see its transactions. |
| **Transactions** | Every transaction: filter, search, edit inline (then Save), add rows by hand, Mark Not Mine, the IOU menu, filtered and selected totals, duplicate and needs-review highlighting, **Suggest categories**. |
| **Import** | Add statement files to an account, Preview, Import; optional older-history import. |
| **Investments** | Value, principal and growth per investment account with charts; deposits that need sorting out; **Add a Snapshot**; all entries, editable. |
| **IOUs** | One card per person with their balance; their *They paid* / *I paid* entries side by side; manual entries and starting balances. |
| **Help** | The rules, FAQ style, in the app, with a search box (typos and related words welcome; press `/`). |

Most pages have a period selector (This Month by default, 3/6 months, a year, or a custom month range).

---

## 6. Importing in detail

### Where statement files go

`incoming_statements/` holds **one folder per account, named after the account** — created automatically when you
add an account. A file's folder says which account it belongs to, so file names don't matter. The Import page's
upload just saves into the right folder; you can also drop files there yourself. A file outside an account folder,
or in a folder that doesn't match an account, is reported and left alone — the importer never creates accounts on
its own (a typo would otherwise create a junk duplicate).

### Which files can be read

The header row decides how a file is read:

1. **Dedicated readers** — Bank of America (checking/savings and credit card) and Chase (checking/savings and
   credit card).
2. **Your own formats** from `config/csv_formats.json` (below).
3. **Automatic detection** — any file with a date column (*Date*, *Transaction Date*, *Posted Date*, …), and either
   an amount column (*Amount*, …) or a pair of debit/credit columns (*Debit*/*Credit*, *Withdrawals*/*Deposits*,
   *Money out*/*Money in*, …). A description column (*Description*, *Payee*, *Merchant*, *Name*, …), a running
   balance and the bank's own category are picked up when present. Date formats are worked out from the file (if any
   day is above 12, month-first vs. day-first settles itself; otherwise month-first is assumed). Leading summary
   lines before the header are skipped.

A file that can't be read **fails loudly** with the file name, row number and reason — never a silent skip or a
half-guessed row. Error messages never include amounts or balances.

### Signs

Money out must come in negative. Dedicated readers know their bank's convention. For automatically detected files
on a **credit card or loan** with a single amount column, *Auto* flips the signs when most amounts are positive
(card exports from some banks list purchases as positive). Checking/savings files are never flipped automatically.
You can override per file on the Import page (*As in file* / *Flip all signs*), or with `--sign` on the command line.

### Describing a layout once (`config/csv_formats.json`)

For a bank the detection gets wrong — day-first dates, decimal commas, purchases as positive — describe it once.
Create `config/csv_formats.json` containing a JSON list of formats; start from
[`static/examples/csv_formats.example.json`](static/examples/csv_formats.example.json) and **copy only the entries
you need**:

```json
[
  {
    "name": "My Credit Union",
    "date": "Posting Date",
    "description": "Memo",
    "amount": "Amount",
    "date_format": "%d/%m/%Y",
    "negate": true,
    "match": ["Member Number"]
  }
]
```

| Key | Meaning |
|---|---|
| `name` | Shown on the Import page. |
| `date`, `description` | Column names (exactly as in the file's header). |
| `amount` — or `debit` + `credit` | One signed amount column, or separate out/in columns. |
| `balance`, `category`, `check_number` | Optional columns: running balance after each row, the bank's category, a check number (sets Method to "Check"). |
| `date_format` | Optional [strptime format](https://docs.python.org/3/library/datetime.html#format-codes), e.g. `%d.%m.%Y`. |
| `negate` | `true` to flip every sign. |
| `decimal_comma` | `true` for amounts like `1.234,56`. |
| `match` | Other column names that must also be present, so the format applies only to that bank's files rather than to every file with a Date/Description/Amount header. |

A format applies to a file whose header row contains every column it names (plus `match`). Custom formats are
checked before automatic detection.

### Bringing in older history

Optional, one time: if you've kept records in a spreadsheet (or can export them from another app), reshape them
into the templates and import them from the Import page (*Bring in older history*) or the terminal. It's safe to
re-run — rows already present are skipped — and rows with a problem are skipped and listed rather than blocking
everything.

| File / sheet | Columns (required **bold**) |
|---|---|
| Transactions — [template](static/examples/history_transactions.csv) | **Date**, **Account**, **Amount** (negative = out), Description, Category, Type, Method, Note, Balance (the account's balance after that row, if you tracked it) |
| Accounts (optional) — [template](static/examples/history_accounts.csv) | **Name**, **Institution**, **Type**, Currency, Owner (`self` / `joint` / `other`), Paid By |
| Balances (optional) — [template](static/examples/history_balances.csv) | **Date**, **Account**, **Balance** |

As CSV files, a name containing "accounts" or "balances" marks that kind; anything else is transactions. As one
workbook (.xlsx), use sheets named *Transactions*, *Accounts* and *Balances*. A blank Type is worked out from the
sign and the description rules. The last Balance of each month also becomes a recorded balance, so Net Worth
history is right. Before importing, Preview lists every distinct category with counts — tidy near-duplicates
("Groceries" vs. "grocery") first, since they become the categorizer's training data. Files are deleted after an
import with no problems.

---

## 7. Customizing it (for you or your AI agent)

Everything is plain Python, Jinja templates and one stylesheet — no build step. Common changes:

| I want to… | Change |
|---|---|
| Teach it that a phrase always means a category or type | `CATEGORY_RULES` in `src/categorize.py` (first match wins; put specific patterns above general ones). |
| Map my bank's categories to mine | `BANK_CATEGORY_MAP` in `src/parsers/base.py`. |
| Read a bank's CSV that isn't recognized | `config/csv_formats.json` ([above](#describing-a-layout-once-configcsv_formatsjson)); or, for something unusual, a new parser class in `src/parsers/` added to `KNOWN_PARSERS` in `csv_importer.py`. |
| Update exchange rates, add a currency | `RATES` (and `SYMBOLS`) in `src/fx.py`. |
| Report totals in EUR (or another currency) | Set `BASE_CURRENCY` in `src/fx.py`, give it rate `1.0`, and re-express the other rates relative to it. |
| Use a different port | `python app.py --port 5001` (or `PORT=5001`). |
| Change the categories offered as suggestions | Nothing to configure — suggestions come from categories you've used. |
| Change colors | `PASTEL_COLORS` / `STATUS_*` in `app.py` and the matching variables at the top of `static/style.css`. |
| Change the date-range presets | `src/periods.py` and `templates/_period_selector.html`. |
| Track a work card / expense reimbursements | Not built in — not everyone needs it. [`docs/recipes/work-expenses.md`](docs/recipes/work-expenses.md) is a ready-made design an AI agent can build from. |

**Recipes** (`docs/recipes/`) are optional features written up as designs instead of code: the rules, data model,
pitfalls and tests, so an agent can add one when a user actually needs it.

The deeper rules — and the reasons behind them, which matter before changing any calculation — are in
[`CLAUDE.md`](CLAUDE.md). Run the tests after any change ([below](#11-project-layout--development)).

---

## 8. Terminal commands

Everything here can also be done in the browser except the maintenance fixes. Run from this folder with the
virtual environment active.

**Imports**

```bash
python -m src.parsers.csv_importer --audit       # dry run: what would be imported (counts and errors only)
python -m src.parsers.csv_importer --import      # import; add --keep-files to keep the CSVs, --sign flip|asis to override signs
python -m src.parsers.history_importer --audit   # dry run of import_history/ (counts, problems, category labels)
python -m src.parsers.history_importer --import
```

**Maintenance** (`python -m src.maintenance <command>`) — each is a **dry run by default** that prints only a row
count, never transaction details; add `--apply` to change anything.

| Command | Does |
|---|---|
| `create-account "Name" --institution X --account-type savings [--currency EUR] [--owner other --person Mom]` | Same as Add Account. |
| `set-owner "Name" other --person "Mom"` (or `self` / `joint`) | Change who an account belongs to. |
| `retag-transfers "Account" --note-contains "Alex"` (and/or `--description "Repay"`) | Re-type repayments that were logged as `expense` → `personal_transfer`. Only `expense` rows are touched. |
| `retag-card-payoffs "Checking"` (`--date-window-days N`) | Tag checking-side card payoffs `internal_transfer` by matching amount + nearby date with the card's tagged payment. |
| `list-balance-dates [--start D] [--end D]` | Read-only: each account's recorded-balance dates (never values) — for diagnosing a Net Worth trend jump. |

**Other**

```bash
python -m src.seed_demo                 # rebuild the demo database (never touches your real data)
python -c "from src.db import get_connection; from src.categorize import train_classifier; c = get_connection(); print(train_classifier(c)); c.close()"   # retrain the categorizer now
```

To start over completely: stop the app and delete `data/finances.db` and `data/classifier.joblib`.

---

## 9. Privacy & safety

- **Nothing leaves your computer.** No bank connections, no aggregators, no analytics, no cloud AI. The only
  network requests the pages make are for the Chart.js charting libraries (from a CDN); no data is sent with them.
- **Local only:** the server listens on `127.0.0.1`, so no other device can reach it.
- **Your data is one file**, `data/finances.db` (SQLite). Back it up by copying it; move the whole folder to a new
  computer and it just works (all paths are relative).
- **Statements are deleted after import.** Account numbers are never stored (only an optional last 4 digits).
- **Logs and error messages never contain amounts or balances** — safe to paste when asking for help.
- **Git:** `data/`, `incoming_statements/`, `import_history/`, `config/`, spreadsheets, virtual environments and
  `.env` are all in `.gitignore`. Only code and docs are committed.
- **AI coding agents:** an AI agent is itself a cloud service, so it must never read your financial files.
  [`.claude/settings.json`](.claude/settings.json) blocks Claude Code from reading `data/`,
  `incoming_statements/` and `import_history/`, and from shell commands that print files (`cat`, `sqlite3`, …).
  Other agents don't enforce that file — [`AGENTS.md`](AGENTS.md) states the same rules for them. Error output and
  the `--audit` summaries are designed to be safe to share with an agent.

---

## 10. Troubleshooting

**The page says "Access to 127.0.0.1 was denied" (Mac).** The app isn't actually running — check the terminal for an
error. macOS's AirPlay Receiver also listens on port 5000, so with the app down the browser reaches that instead.
Start the app again, or use another port: `python app.py --port 5001`.

**"Address already in use".** An earlier copy is still running. Stop it with `Ctrl+C` in its terminal, or find it:
`lsof -nP -iTCP:5000 -sTCP:LISTEN` then `kill <PID>` (ignore a `ControlCenter` line — that's AirPlay). Or use
`--port 5001`.

**A statement wasn't imported.** The Import page shows why next to the file. Typical causes: it's not in an
account's folder; the layout isn't recognized (describe it in `config/csv_formats.json`); a date or amount in some
row isn't readable (the message gives the row number). Files with problems are never partly imported.

**Purchases show as income.** That file's signs are reversed — re-import it with *Flip all signs* (delete the wrong
rows first: filter Transactions to that account and period, tick all, Delete selected), and consider a custom
format with `"negate": true` so it's permanent.

**Spending looks too high.** Usually a transfer typed as `expense`: a credit card payoff on the checking side, a
transfer to savings, or a repayment to a person. Fix the Type (or run `retag-card-payoffs`). Also check for pink
duplicate rows.

**An account's balance is wrong.** On Net Worth, use **Set Balance** with the bank's balance for a date (today is
easiest). Transactions after that date are added on top.

**Nothing is categorized automatically.** The classifier learns from your own categorized rows; with only a handful
it won't guess. Categorize a few dozen rows by hand, then click **Suggest categories** on Transactions.

**Styles look stale after an update.** Hard-refresh the browser (`Cmd+Shift+R` / `Ctrl+Shift+R`).

**After pulling a new version.** Just restart the app: new database columns are added automatically at startup,
and older databases are upgraded in place.

---

## 11. Project layout & development

```
app.py                       Flask routes + JSON endpoints (python app.py [--demo] [--port N])
requirements.txt
CLAUDE.md / AGENTS.md        Full spec for AI agents (and humans who want the details)
src/
  paths.py                   Every file location (and demo-mode switching)
  db.py                      Schema, connection, automatic migrations
  aggregates.py              The calculations behind every page (balances, net worth, spending, investments, IOUs)
  sankey.py                  Cash flow allocation (income vs. outputs, residual)
  categorize.py              Rules + local classifier
  dedup.py                   Duplicate detection
  fx.py                      Base currency, exchange rates, symbols
  periods.py                 Date-range presets
  maintenance.py             Dry-run-first terminal fixes
  seed_demo.py               Fake data for demo mode
  parsers/
    base.py                  Parser interface + shared helpers (dates, amounts, bank category map)
    bofa.py, chase.py        Dedicated bank readers
    generic.py               Auto-detecting + user-configurable CSV reader
    csv_importer.py          Statement pipeline: folders, detection, signs, duplicates, CLI
    history_importer.py      Optional one-time history import, CLI
templates/                   One Jinja template per page + shared pieces
static/                      app.js, style.css, examples/ (import templates, csv_formats example)
tests/                       pytest suite — runs against temporary databases, never your data
docs/recipes/                Optional features as designs for an AI agent to build (e.g. work expenses)
data/                        Your database + classifier (created on first run; gitignored)
incoming_statements/         One folder per account for CSVs to import (gitignored)
import_history/              Optional history files to import (gitignored)
config/                      Your csv_formats.json, if any (gitignored)
```

**Stack:** Python 3.11+, Flask + Jinja2 (server-rendered), SQLite, pandas, openpyxl, scikit-learn, Chart.js from a
CDN. No Node/npm, no build step.

**Tests:**

```bash
python -m pytest -q
node --check static/app.js      # optional JavaScript syntax check
```

Tests use temporary databases and folders; they never touch your data.
