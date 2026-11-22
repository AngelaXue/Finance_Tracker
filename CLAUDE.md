# Finance Tracker — Spec

A private, single-user personal-finance dashboard that runs only on the user's own computer (`127.0.0.1`). It
imports downloaded bank CSV statements (no bank logins, no aggregators like Plaid), then shows net worth, cash
flow, spending by category, investment growth, and an IOU ledger with other people. Categories are learned by a
local classifier trained on the user's own data. Python + Flask + SQLite, server-rendered, no build step.

This file is the full spec: constraints, data model, every business rule **and why it is that way**, conventions,
and a playbook for adapting the app to a user. `README.md` is the user-facing guide; the in-app Help page
(`templates/help.html`) is the short version of the rules. Keep all three in sync when a rule changes.

---

## 0. Rules for AI agents — read first

1. **Never read the user's financial data.** You are a cloud service; their data must not reach you. Never read,
   grep, open, print or summarize anything in `data/` (the SQLite DB, the trained classifier, demo data),
   `incoming_statements/`, `import_history/`, `legacy_data/`, or `config/account_map.json`. This includes doing it
   indirectly — e.g. `python -c "print(open(...).read())"`, `sqlite3`, pandas `read_csv` + print, or running app code
   that prints rows. `.claude/settings.json` hard-blocks the obvious tools for Claude Code; the rest is on you.
   Other agents: follow this anyway (`AGENTS.md` repeats it).
2. **When the user has a problem with their data**, work from metadata: ask them to run the `--audit` commands or
   paste error messages (those never contain amounts — see §6.13), and reproduce with *fake* data in a test. The
   demo database (`python -m src.seed_demo`, `data/demo.db`) is fake and fine to use via the app, but prefer tests.
3. **Never** bind to `0.0.0.0`, add a network call that sends transaction data anywhere, hardcode an absolute path,
   or log/print an amount or balance.
4. **Schema changes** go in `src/db.py`: update `SCHEMA` *and* add an idempotent step to `_migrate()` (it runs at
   every start) — never ask the user to alter their database by hand.
5. **Verify every change:** `python -m pytest -q` (all tests use temp DBs) and `node --check static/app.js`. Add a
   route test for every new endpoint, including its failure cases.
6. **Docs:** a rule change updates this file, `README.md` (§4 concepts) and `templates/help.html` together.

### Adapting the app to a user — a playbook

Ask (or infer from what they've told you), then act:

| Question | What to do |
|---|---|
| Which banks/cards? | They add each as an account (Net Worth → Add Account, or `python -m src.maintenance create-account`). BofA and Chase CSVs are read by dedicated parsers; most others by auto-detection. If they paste a CSV **header row** (column names only — never data rows) for a bank that fails, write an entry for `config/csv_formats.json` (§6.10). Only write a new parser class for something truly unusual. |
| Which currency do they think in? Any foreign accounts? | Set `BASE_CURRENCY` and `RATES` in `src/fx.py` (§6.11). Add missing currencies + symbols. Remind them the shipped non-base rates are placeholders. The multi-currency UI turns itself on when an account in a second currency exists — nothing to toggle. |
| Does anyone else pay one of their accounts, or do they share costs with people? | Owner *Someone else pays it* (`owner='other'`, `iou_person`) for such accounts; explain the IOU menu (§6.8). |
| Investment accounts? | `brokerage`/`retirement` accounts with distinctive first words in their names, so a note like `roth` matches (§6.7). |
| Older history in a spreadsheet? | Help them reshape it to the templates in `static/examples/` (§6.10). They run the import; you don't open the file. |
| Recurring descriptions that are always one category/type? | Add `CATEGORY_RULES` entries in `src/categorize.py` (specific patterns above general ones). |
| Their bank's category names? | Extend `BANK_CATEGORY_MAP` in `src/parsers/base.py`. |
| A work card / expense reimbursements? | Build it from the recipe `docs/recipes/work-expenses.md` (a full design, deliberately not built in). Until then, the pass-through rule (§6.6) covers the money in their own accounts. |
| A feature request | Check `docs/recipes/` (designs for optional features), §9 (open ideas) and the reasoning in §6 first — several "obvious" features were tried and removed for good reasons (e.g. net-worth adjustments for pass-through money, FIFO repayment matching). |

---

## 1. Non-negotiable constraints

- **All file paths are relative**, derived from `src/paths.py` (`BASE_DIR = Path(__file__).resolve().parent.parent`),
  so the folder can be copied to another machine and just work.
- **No data leaves the machine.** No cloud APIs with transaction data, account numbers or balances. The classifier
  is local (scikit-learn). Pages load only Chart.js (+ the Sankey plugin) from a CDN.
- **Server binds to `127.0.0.1` only.**
- **Statement files are deleted after a successful import** (they're a liability once the data is in).
- **Account numbers are never stored in full** — `accounts.last4` at most.
- **Log content rule:** error/log/CLI output may include file names, row numbers, reasons, dates, descriptions and
  categories — **never amounts, balances or other monetary figures** (§6.13).
- **`.gitignore`** must exclude `data/`, `incoming_statements/`, `import_history/`, `config/`, spreadsheets
  (`*.xlsx`, `*.xls`), virtualenvs, `__pycache__/`, `.DS_Store`, `.env`. (`legacy_data/` is also kept from the
  original single-user version.)

## 2. Tech stack

Python 3.11+ · Flask + Jinja2 (server-rendered) · SQLite via stdlib `sqlite3` · pandas + openpyxl (history import)
· scikit-learn + joblib (classifier) · Chart.js 4 + chartjs-chart-sankey via CDN `<script>` · plain JS in
`static/app.js`. No Node/npm/build tooling anywhere.

## 3. Repository structure

```
app.py                     Flask app: page routes, JSON endpoints, request-parsing helpers.
                           `python app.py [--demo] [--port N]`; binds 127.0.0.1.
src/
  paths.py                 All file locations; FINANCE_TRACKER_DEMO=1 (set by --demo) swaps in demo ones
  db.py                    SCHEMA, get_connection, _migrate_before_schema + _migrate (idempotent, every start)
  aggregates.py            Calculations shared by pages: balances, net worth (+trend), spending,
                           investments, CDs & bonds, IOUs, transaction fetching
  sankey.py                Cash flow allocation + residual
  categorize.py            CATEGORY_RULES + local classifier (train / predict / predict_uncategorized)
  dedup.py                 dedup hash, unique_dedup_hash, find_possible_duplicate
  fx.py                    BASE_CURRENCY, RATES, SYMBOLS, to_base(), symbol()
  periods.py               Period presets
  maintenance.py           Dry-run-first CLI fixes; ACCOUNT_TYPES, OWNERS, create_account (shared with UI)
  seed_demo.py             Fake data → data/demo.db only
  parsers/
    base.py                ParserError, BaseParser, read_lines, parse_date, money_value/parse_amount,
                           apply_category_rules_to_row, BANK_CATEGORY_MAP, default_card_method, find_header_line
    bofa.py, chase.py      Dedicated adapters (detected by header)
    generic.py             GenericCSVParser: auto-detection + user formats from config/csv_formats.json
    csv_importer.py        Statement pipeline (folders, detection, signs, dedup flags, deletion), CLI
    history_importer.py    Optional one-time history import (CSV/XLSX templates), CLI
templates/                 base.html (sidebar, demo banner, MONEY_DIGITS, MONEY_SYMBOL), _macros.html
                           (period_selector), _period_selector.html, _cd_table.html, one per page,
                           help.html
static/                    app.js, style.css, examples/ (history templates, csv_formats.example.json)
tests/                     pytest; every test uses temp DBs/folders
docs/recipes/              Optional features written as designs, not code (work-expenses.md)
.claude/settings.json      Deny rules keeping Claude Code out of the user's data
```

## 4. Data model

SQLite, `data/finances.db` (demo: `data/demo.db`). Authoritative DDL with comments: `src/db.py`.

**accounts** — `id, name, institution, account_type, currency, owner, iou_person, last4, is_active`
- `account_type`: `checking | savings | credit_card | loan | brokerage | retirement | digital_wallet | cash`.
  Liabilities: `credit_card`, `loan` (balances stored as positive "amount owed").
- `currency`: a key of `fx.RATES`.
- `owner`: `self | joint | other`. `other` = someone else settles it (e.g. a card in the user's name a parent pays):
  excluded from net worth everywhere; its spending still counts in Spending/Cash Flow; feeds IOUs for `iou_person`.

**transactions** — one row per transaction.
- `date` (`YYYY-MM-DD`; every filter compares strings, so dates are always zero-padded), `amount` (signed:
  **negative = out**), `currency` (= the account's), `description`, `note`, `method` (free text payment rail:
  Card, Zelle, Cash, …), `source` (`csv_parsed | history_import | manual`).
- `transaction_type`: fixed enum, always a strict dropdown in the UI (a typo would silently break the math):
  `income | expense | internal_transfer | personal_transfer | investment_contribution` (§6.1).
- `category`: free-text label. `category_predicted=1` = filled automatically, unreviewed ("needs review", yellow);
  cleared on edit or confirm. `manually_corrected` + `original_category` log corrections (training signal).
- `dedup_hash` (UNIQUE) and `flagged_duplicate` (§6.9).
- `statement_balance`: the bank-reported balance after this row (from CSVs that have one); NULL = unknown. Cleared
  if date/account/amount is edited. Used for the Transactions page's running balance.
- `attributable_to_user`: 0 = "Not Mine" (§6.4).
- `iou_kind` (`NULL`=automatic | `they_paid` | `i_paid` | `none`), `iou_person`, `iou_settlement`
  (`NULL | pending | settled`, reminder only) — §6.8.
- `investment_account`: Investments override (`NULL`=automatic | account id | `none`) — §6.7.

**balances** — recorded balances: `account_id, date, balance, principal_contributed (legacy), note`,
`UNIQUE(account_id, date)`. Written only by: Add Account's starting balance, Set Balance (checking/savings/loan/
credit_card), the Investments snapshot form and its All-entries edits (brokerage/retirement), and the history
import. **CSV imports never write it** — balances roll forward from transactions instead (§6.2).

**ious** — manual IOU entries only (what has no transaction behind it): `date, person, description, direction
(they_paid | i_paid | starting_balance), amount (positive; a starting balance is signed), settlement`. Always in
the base currency. Transaction-derived IOUs are computed live, never copied here.

**cds** — "CDs & Bonds" memo: `account_id, institution, principal, apy, opened_date, maturity_date, note, kind`
(`CD | Treasury | Corporate bond | Municipal bond | Other` = `app.FIXED_INCOME_KINDS`). Never summed into net worth —
the money is already inside `account_id`'s balance.

**cd_interest** — interest logged by hand per CD/bond: `cd_id (ON DELETE CASCADE), date, amount (negative = a
correction), note`. A CD's **Current Balance** = principal + its interest (`aggregates.list_cds`). Deliberately *not*
the linked account's balance: for a CD inside a brokerage/retirement account that showed the whole account. Memo only.

**investment_deposits** — "Principal added" typed on Investments: new money into a brokerage/retirement account that
didn't come through a logged transaction. Counted exactly like a matched contribution.

**Migrations** (`db._migrate_before_schema`, `db._migrate`): idempotent `ALTER TABLE`s at every start. They also
upgrade databases from the original single-user version: `parent_ledger` → `ious` (person `'Parents'`,
`charge→they_paid`, `payment→i_paid`), `transactions.parent_debt/parent_settlement` → `iou_kind/iou_settlement`
(+ `iou_person='Parents'`), `family_transfer` → `personal_transfer`, owner `parents` → `other` with
`iou_person='Parents'`. Covered by `tests/test_db.py`.

## 5. Pages & endpoints

| Route | Purpose |
|---|---|
| `/` Overview | Net worth **excluding brokerage/retirement** (stable between manual updates) + trend (own period param prefix `nw_`, default All); Balances (per account; per currency + combined when `MULTI_CURRENCY`, §6.11); debts, loans & IOU balances; allocation + spending donuts; small Sankey; *Getting started* checklist until there are accounts and transactions. |
| `/net-worth` | One table per currency (base currency first, always shown; others when used; titled just "Accounts" without currency labels while single-currency), grouped Cash/Investments/Credit/Loans, 30-day change, liabilities shown negative; converted total + the rates used; "Paid by someone else" list; Set Balance; CDs & Bonds; Investments summary; Add Account (`#add-account`). No charts. |
| `/cash-flow` | 4 cards (Inflow, Living Expenses, Saved + Invested, Savings Rate) + Sankey. |
| `/spending` | Category bars (share, change vs. prior equal-length period), donut, sparklines; `?pass_through=include`. Click-through to Transactions with `period=custom&start&end&category`. No currency split (by design). |
| `/transactions` | Filter (period: This Month default / All / custom; year, month, account, category incl. Uncategorized, needs review, Mine/Not Mine, `iou=any|they_paid|i_paid`, search); inline staged edits + Save/Discard; + Add row; Delete selected; Mark Not Mine/Mine; IOU… menu + Person box; filtered + selected totals; Suggest categories. |
| `/import` | Upload statements to an account folder; pending files with per-file sign choice; Preview; Import; Remove; optional history section. |
| `/investments` | Overview panel + one collapsible panel per investment account (own period params `a<id>_period`…), charts on a linear time axis, CDs & bonds band; "need a look" list with staged account choices; Add a Snapshot; All entries (editable). |
| `/ious` | Person cards; selected person's They paid / I paid panels with Settled/Pending marks; Add an entry (incl. starting balance). `?person=`. |
| `/help` | FAQ-style reference with a browser-only search box (`initHelpSearch`: word stems, related words from `HELP_RELATED` — extend it when a concept gets new vocabulary — typo tolerance, rarer words weigh more; `/` focuses it; `?q=` pre-fills it). |

JSON endpoints (all POST unless noted): `/api/transactions/{save,delete,set-attributable,set-iou,suggest-categories}`,
`/api/accounts/{add,set-balance}`, `/api/cds/{add,update,delete}`, `/api/cds/interest/{add,delete}`, `/api/investments/{entry,assign}`,
GET `/api/investments/guesses`, `/api/investments/deposits/{update,delete}`, `/api/balances/{update,delete}`,
`/api/ious/{add,set-settlement,delete}`, `/api/import/{upload,preview,run,discard}`,
`/api/import/history/{upload,preview,run}`.

## 6. Business rules (and why)

### 6.1 Type decides the math; Category is a label

Every calculation reads `transaction_type`, never `category` (one exception: pass-through, §6.6).
- `internal_transfer` — between the user's own accounts. Excluded from Cash Flow, Spending, needs-review.
  **Both legs** of a card payoff need it: the card side is usually caught by `CATEGORY_RULES` ("payment thank you"),
  the checking side is worded inconsistently and easy to miss — which double-counts purchases.
  `maintenance.retag_card_payoff_checking_legs` fixes it by amount + nearby-date matching against tagged card
  inflows (self/joint cards only), not description guessing.
- `personal_transfer` — between the user and another person, not income or spending (repaying, being repaid,
  pass-through). Treated exactly like `internal_transfer` in every calculation (`aggregates.TRANSFER_TYPES`), kept
  separate so it stays distinguishable. A repayment typed `expense` would double-count the original purchase;
  `maintenance.retag_personal_transfers` fixes those (only `expense` rows, by exact description and/or note substring).
- `investment_contribution` — feeds Investments and the Sankey's Investing output.

### 6.2 Account balances (`aggregates.latest_balance`)

- `cash`, `digital_wallet` (`COMPUTED_BALANCE_TYPES`): always the running sum of their transactions; never take a
  `balances` row (Add Account refuses a starting balance for them).
- `brokerage`, `retirement` (`INVESTMENT_TYPES`, no roll-forward): §6.7.
- Everything else: the latest `balances` anchor on/before `as_of` **rolled forward** by the account's transactions
  dated after it. Without this, a balance would freeze at the last anchor, since imports never write anchors.
- No anchor at all: treat as starting at 0 → sum of all transactions (negated for `credit_card`/`loan`, owed is
  positive), `source="computed"`, shown with a grey "from transactions" tag. Returns `None` if nothing is logged as of
  that date (no fake $0 points). Set Balance adds a real anchor when the sum is wrong.

### 6.3 Net worth & trend

- Sum of balances of active `self`/`joint` accounts, converted with `fx.to_base`, liabilities subtracted. `other`
  accounts are excluded entirely.
- **No adjustment for personal_transfer money parked in an account.** An earlier version subtracted the cumulative
  sum of such transactions; that treats every one as half of a pair that nets to zero, which is true for
  pass-through but false for one-way repayments, so the error grew without bound. Real account balances are simplest
  and most trustworthy; held money shows as a temporary bump.
- `net_worth_trend`: one point per week for spans ≤ ~13 months, per month-end beyond; **every account evaluated at the
  same `as_of`** per point (with roll-forward), so mismatched snapshot dates can't make a transfer look like a spike.
  `maintenance list-balance-dates` (dates only) diagnoses a remaining spike.

### 6.4 Not Mine (`attributable_to_user = 0`)

A real purchase/income that wasn't the user's money. Excluded from Cash Flow income and living expenses
(symmetrically) and from automatic IOUs; **still counted in Spending** (`spending_by_category` deliberately ignores
the flag) — "was this my money" and "what was it" are different questions. Set via explicit Mark Not Mine / Mark Mine
buttons on selected rows (set, not toggle, so mixed selections can't go wrong). Per row, not per portion: a partly
kept deposit is split into two rows.

### 6.5 Cash flow / Sankey (`src/sankey.py`) — must be a real calculation

For a period, all amounts converted to the base currency:
1. `hard_outputs = living_expenses + investing + paid_by_others`, where `living_expenses` = `expense` rows with
   `attributable_to_user=1`; `investing` = `investment_contribution` rows; `paid_by_others` = manual `ious` rows with
   `direction='they_paid'` (someone paid for something of the user's directly — consumed this period, now owed;
   transaction-derived IOUs are already in living_expenses).
2. `income` = `income` rows with `attributable_to_user=1`, grouped by category (`Other Income` if blank).
3. `residual = income − hard_outputs`. If ≥ 0: an output **Cash Savings** = residual. If < 0: an extra input
   **From Savings** = −residual. Both sides always balance.
4. Transfers never participate. Link tooltips show amount and % of `income_total` (one denominator for every link).
Savings rate = (Investing + Cash Savings) / income.

### 6.6 Spending & pass-through

`expense` rows only, grouped by category ("Uncategorized" if NULL), Not Mine included. Rows whose category normalizes
to "passthrough" (case, spaces, hyphens ignored — `_PASS_THROUGH_SQL`) are excluded unless `pass_through=include`;
Overview's donut uses the same default. This is the **one place a category changes the math** — deliberate, because
work reimbursements often arrive typed `expense`/`income`. Cash Flow doesn't read the category, so it still counts
them unless they're marked Not Mine — counted, they inflate income and lower the savings rate. So a pass-through row
is "finished" only with both: Category Pass-Through **and** Not Mine. `aggregates.PASS_THROUGH_TODO_SQL` finds the
half-finished ones (income/expense, Pass-Through, still Mine): a yellow banner on Transactions plus the
`review=pass_through` filter ("Pass-through to finish"). It never guesses from descriptions — the original version
also matched "amex"/"reimb" for one user's corporate card; that's user-specific. Possible future: a dedicated
type/flag so Spending and Cash Flow agree without reading a label.

### 6.7 Investments

- **Matching contributions** (`investment_contributions`): rows on a *non*-investment account with Type
  `investment_contribution` (or Category starting "invest", surfaced only to be fixed — never counted unless the Type
  is right). Target account, in order: `transactions.investment_account` override (id, or `none` = not an
  investment) → the **note** naming an investment account (full name or first word, case-insensitive) → the
  **description**, same way. 0 or >1 matches → listed on Investments ("need a look") with a staged dropdown. Rows on
  the investment account itself are ignored, so logging both legs can't double count.
- **Principal** = matched contributions + `investment_deposits` (+ a legacy `balances.principal_contributed` overrides
  from its date). The user enters money *added*, never a running total.
- **Value** = latest snapshot (`balances` row) + inflows dated after it at face value (deposits certainly arrived;
  market moves wait for the next snapshot). Never rolled forward from the account's own transactions — that would
  silently understate gains/losses. Principal added that matches a matched contribution (same account/amount,
  ±5 days) is refused with 409 `needs_confirm` until confirmed.
- Charts: one point per snapshot/deposit date within the range plus range start/end (once data exists); linear time
  axis; Principal stepped, Value straight segments; CDs & bonds band from opened→maturity (`fixed_income_trend`).
- Deliberately **no** holdings, prices or "top movers" — that's the brokerage's job.

### 6.8 IOUs (`aggregates.iou_status`, `iou_ledgers`)

Per person, `balance` = **what the user owes them** (negative = they owe the user), in the base currency.
- `they_paid` (money/value from them to the user) adds; `i_paid` subtracts.
- One rule, shared by the IOUs page and the Transactions IOU column:
  1. A manual override (`iou_kind`) wins; person = `transactions.iou_person`, else the account's `iou_person`.
     `none` opts out.
  2. Otherwise, on an `owner='other'` account: `expense`/`income` rows with `attributable_to_user=1` count for the
     account's `iou_person` — negative amount → `they_paid`, a refund → `i_paid`. Transfers on that account (them
     paying the card) are not the user's repayment.
  3. Nothing else is inferred. In particular a `personal_transfer` could be a loan, a repayment or pass-through, so
     the user marks it. (The original version auto-counted pass-through arrivals as owed; that only held for one
     user's family situation and was dropped.)
- Manual entries (`ious`) for what has no transaction. A person's latest `starting_balance` replaces all of their
  entries on/before its date (same day: the starting balance sorts last). Starting balances are signed.
- Settled/Pending marks are reminders only; never auto-applied. Automatic FIFO matching of repayments to debts was
  rejected: repayments are often ballpark figures and pass-through money is often repaid out of order.
- IOUs never change net worth. Set via `/api/transactions/set-iou` (`value` they_paid/i_paid requires `person`;
  `none`; `auto` clears).

### 6.9 Duplicates (`src/dedup.py`)

`dedup_hash = sha256(account_id|date|amount|normalized_description)` (lowercase, punctuation/extra whitespace
stripped). The CSV pipeline and manual add/edit **never drop or block** a duplicate-looking row: `unique_dedup_hash`
returns a disambiguated hash for a repeat, and `find_possible_duplicate` catches same account/date/amount with a
different description. Either sets `flagged_duplicate=1` (pink) + a note marker; the user deletes or confirms (select
+ Save), and editing clears it. Why: two genuinely different transactions can share every field; silently skipping
risks losing a real one. Accepted tradeoff: re-importing the same file adds a flagged second copy. The history import
instead uses `INSERT OR IGNORE` on the plain hash, so it's safely re-runnable. Save responses include `newly_flagged`
so the UI can offer Continue / Discard / Cancel immediately.

### 6.10 Importing

**Statements** (`csv_importer.py`):
- `incoming_statements/<account folder>/<any>.csv`; folder = `account_folder_name(account.name)` (filesystem-unsafe
  characters → `-`), created for every active account on demand (`ensure_account_folders`: Import page, Add Account,
  `create-account`). Files elsewhere, or in a folder matching no account, are errors. **Never creates accounts.**
- Layout detection by header: `KNOWN_PARSERS` (Chase CC, Chase checking, BofA checking, BofA CC) → custom formats
  from `config/csv_formats.json` → `GenericCSVParser.from_lines` (auto) → else a loud error.
- Generic mapping: date column (prefers transaction date over posting date), amount **or** debit+credit
  (`amount = |credit| − |debit|`), optional description/balance/category/check number. Date format = first of
  `DATE_FORMATS` that parses *every* date in the file (month-first wins ties). Summary rows ("Beginning balance…")
  without an amount are skipped. Custom formats: keys `name, date, description, amount | debit+credit, balance,
  category, check_number, date_format, negate, decimal_comma, match` (match = extra required header names).
- **Signs:** `sign` per file = `auto | asis | flip`. `auto` flips only an *auto-detected, single-amount-column* file
  on a `credit_card`/`loan` account when most amounts are positive; checking/savings, dedicated adapters and custom
  formats are never auto-flipped. After a flip, income/expense types are re-derived from the sign.
- Rows take the **account's currency**; card rows without a method get `default_card_method` (Card, or
  Venmo/Zelle/PayPal named in the description). Category: `CATEGORY_RULES` first, then the bank's category via
  `BANK_CATEGORY_MAP` (flagged `category_predicted`; raw label kept in the note).
- `run(apply, signs, keep_files, sample_size)`: everything inside one DB transaction; `apply=False` rolls back (a
  real dry run — counts include within-batch duplicates). On apply: commit, run `predict_uncategorized` (best
  effort), delete files that imported cleanly. A file with any parse error imports nothing and stays.
- Bank adapters: BofA checking/savings (`Date,Description,Amount,Running Bal.`; skips the "Beginning balance" row;
  running balance → `statement_balance`), BofA card (`Posted Date,…,Payee,…,Amount`; negative = purchase), Chase
  checking (`Details,Posting Date,Description,Amount,Type,Balance,Check or Slip#`; Type → note; check number →
  method Check), Chase card (`Transaction Date,Post Date,Description,Category,Type,Amount,Memo`; Chase category →
  suggestion). New adapter = one file implementing `BaseParser` (`name`, `REQUIRED_HEADERS`, `parse`) + an entry in
  `KNOWN_PARSERS`; nothing else changes.

**History** (`history_importer.py`, optional, one-time): `.csv`/`.xlsx` in `import_history/`. Transactions
(`Date, Account, Amount` required; Description, Category, Type, Method, Note, Balance), Accounts (`Name, Institution,
Type`; Currency, Owner, Paid By — creates missing accounts), Balances (`Date, Account, Balance`). Order: accounts →
transactions → balances. Blank Type → by sign, then rules. Last `Balance` per account per month → a `balances` row.
`INSERT OR IGNORE` (re-runnable). Bad rows are skipped and reported (unlike statements — one bad row in years of
history shouldn't block the rest). Audit prints distinct category labels with counts (audit them for near-duplicates
before importing; they become training data). Web import deletes the files only after a run with zero problems.

### 6.11 Currencies (`src/fx.py`)

**Single-currency by default, multi-currency automatically.** `aggregates.currencies_in_use` (active accounts' currencies
+ base) → the `MULTI_CURRENCY` template flag (`app._multi_currency`, injected on every page). While it's false (most
users), templates hide everything currency-specific: per-currency table titles and "(USD)" suffixes, Overview shows
**Balances** (per account) instead of **Balances by Currency**, no currency code after Transactions amounts, no
"converted to …" notes, and Add Account's currency picker sits behind an "Other currency?" link. Once any active
account uses another currency, all of it appears with no setting. New currency-specific UI must be wrapped in
`{% if MULTI_CURRENCY %}` the same way.

Show every account in its own currency; never blend within a table. Convert only to combine (Net Worth total, Cash
Flow, Spending, IOUs, balances-by-currency's combined line) via `to_base(amount, currency)` with
`RATES[c]` = value of 1 unit in `BASE_CURRENCY`. Rates are manual (not a live API — this is a personal tool).
Only currencies in `RATES` can be chosen. Changing `BASE_CURRENCY` requires re-expressing all rates. Templates use
`fmt_money(value, symbol=None)` (None = base symbol) and `currency_symbol(code)`; JS uses `MONEY_SYMBOL`.
Investments assume the base currency (open idea: convert non-base investment accounts).

### 6.12 Categorization (`src/categorize.py`)

1. `CATEGORY_RULES` — `(substring, category|None, type|None)`, case-insensitive, first match wins (specific above
   general: "roth ira" above "transfer"). A rule that sets only the type needs no review; one that sets a category is
   flagged.
2. Bank category mapping (CSV import only).
3. `MultinomialNB(fit_prior=False)` on TF-IDF of descriptions, trained on all categorized rows (corrections
   included); `predict_category` returns `None` below `MIN_CONFIDENCE` (0.3). Without `fit_prior=False` and the
   threshold, out-of-vocabulary descriptions silently get the most frequent training category (observed bug:
   everything predicted as "Dining"). "I don't know" must stay a valid outcome.
`predict_uncategorized` retrains, then fills NULL categories on non-transfer rows (flagged). Model at
`paths.MODEL_PATH` (private data — gitignored).

### 6.13 Parser errors & log content

A parser hitting an unexpected format fails loudly with `ParserError(filename, row, reason, date?, description?,
category?)` — never silently skips or inserts malformed data. `ParserError` can't carry amounts by construction.
This "never print money" rule applies to every log line, CLI output and exception message in the codebase.
CLI `--audit` output is counts, dates, names and reasons only. (The Import page may show sample amounts in the
user's own browser; that's local display, not logging.)

### 6.14 Demo mode

`python app.py --demo` sets `FINANCE_TRACKER_DEMO=1` before `src.paths` is imported, which switches `DB_PATH`
(`data/demo.db`), `MODEL_PATH` and `INCOMING_DIR` (`data/demo_incoming`). The demo DB is seeded on first run
(`seed_demo.seed_demo_database`, deterministic, relative to today). A banner shows on every page. `seed_demo` only
ever writes the demo DB.

## 7. Code conventions (reuse these)

**Server (`app.py`)**
- `_money(value)` parses every amount from the UI ("1,234.50", "$1,234.50"); never `float()` a request value.
- `_iso_date` → padded `YYYY-MM-DD` or None (via `_pad_date`); `_optional_date`; `_clean_note`; `_parse_apy`.
- `_fail(message, conn=None, status=400)` is the one JSON error response. (Only Investments' 409 `needs_confirm` is
  hand-built.)
- **Small editors save all or nothing**: validate every edit, then write (`/api/cds/update`, `/api/balances/update`,
  `/api/investments/deposits/update`, `/api/investments/assign`). The two All-entries editors share
  `_parse_entry_edits` + `_apply_updates` (table/column names only ever from code). Exception:
  `/api/transactions/save` saves valid rows and returns the rest in `skipped` with reasons.
- Uploaded filenames go through `secure_filename`; file paths from the page through `_pending_file_path` (refuses
  anything outside its base folder).
- Account groups: `aggregates.INVESTMENT_TYPES`, `COMPUTED_BALANCE_TYPES`, `LIABILITY_TYPES`, `TRANSFER_TYPES`;
  `SET_BALANCE_TYPES`, `TRANSACTION_TYPES`, `FIXED_INCOME_KINDS` in app.py; `ACCOUNT_TYPES`, `OWNERS` in maintenance.py.
- Templates: `fmt_money(value, symbol=None)` for display, `fmt_num(value)` for table cells / editable boxes.

**Browser (`static/app.js`)**
- `postJSON(url, body)` for every save. Staged editing (highlight `cd-edited`, explicit Save changes / Discard) via
  `trackStagedEdits`; Transactions has its own richer version. Immediate actions (Mark buttons, IOU…) reload after a
  confirm if edits are pending.
- `x.toLocaleString(undefined, MONEY_DIGITS)` for money; `MONEY_SYMBOL` for the base symbol; `localISODate()` for
  today (never `toISOString()` — UTC shifts the date in the evening).
- (i) tooltips: `<span class="info-tip" tabindex="0" data-tip="...">i</span>` (`info-tip-wide` for long text;
  `&#10;` = line break). `initInfoTips` shows them as one floating box on `<body>`, clamped inside the window, so
  they're never under the sidebar or clipped by a scroll box; the CSS `::after` version is only the no-JS fallback.
- localStorage only for per-browser conveniences (folded panels), wrapped in try/catch; pages work without it.
- One `init…()` per page feature, called from the `DOMContentLoaded` handler at the bottom.

**Tests**: `python -m pytest -q`. Route tests use the `client` fixture, which points `db.DB_PATH`,
`csv_importer.INCOMING_DIR`/`CSV_FORMATS_PATH`, `history_importer.HISTORY_DIR` and `categorize.MODEL_PATH` at
`tmp_path` — any new path a route touches must be patched there too. Module-level path constants are read at call
time so monkeypatching works; keep it that way.

## 8. Design notes

- Pastel categorical palette (`PASTEL_COLORS`), with red/green reserved for gain/loss (`STATUS_*`). Every chart also
  has text labels/legends.
- Net Worth page has no charts (the trend lives on Overview). Help is static content, FAQ style: General topics, then
  one section per page in sidebar order; sub-topics are h3s with stable anchors (e.g. `#type-vs-category`,
  `#duplicates`, `#ious`); `initHelpPage` opens the section containing a linked anchor.

## 9. Open ideas (discussed, not built)

- Pass-through: a dedicated type or flag instead of the category-based Spending exception (§6.6).
- IOUs: a live "selected total" on the panels; projected payoff from repayment cadence; a per-person currency.
- Transactions: Method using the custom suggestion dropdown (so Tab works like Account/Category).
- Net Worth: rename/deactivate an account from the UI (today: `UPDATE accounts SET name = …` is safe — everything
  references `account_id` — but the account's statements folder keeps the old name until renamed too).
- CDs & bonds: an estimated current value from the rate (display-only today; Current Balance is principal + interest
  logged by hand).
- Work card / reimbursements: fully designed in `docs/recipes/work-expenses.md`, not built.
- Investments: currency conversion for a non-base-currency investment account.
- Import: OFX/QFX files (many banks offer them alongside CSV); remembering a per-account sign choice.
- Categories: an in-app editor for `CATEGORY_RULES`.

## 10. History

The app began as one person's tracker (with a "Parents Ledger", a fixed Excel-workbook importer and two hardcoded
banks) and was generalized: the ledger became multi-person IOUs, `family_transfer` became `personal_transfer`, owner
`parents` became `other` + `iou_person`, the workbook importer became the template-based history import, and
statements moved to per-account folders with format detection. `_migrate` upgrades databases from that version in
place.
