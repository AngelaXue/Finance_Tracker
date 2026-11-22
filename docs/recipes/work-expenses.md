# Recipe: Work Expenses (a work card + reimbursement tracker)

> **Status: designed, not built.** Not every user has a work card, so this ships as a recipe instead of code. It is
> distilled from a working implementation in the app's original single-user version (an Amex corporate card),
> including the mistakes that were made and reverted. An AI agent can build it from this file; a human can read it to
> decide whether they want it.
>
> Before building, read `CLAUDE.md` (§0 rules, §6 business rules, §7 conventions). Everything below follows them.

## 1. In one paragraph

Some people pay for work things — on a **company card in their name**, or on their **own card** — and get paid back
by their employer. They need to know: what haven't I submitted yet, what am I still waiting for, did I pay each card
bill, and how much of this ended up costing me. The **Work Expenses** page tracks exactly that, in a separate table
that never touches the user's own finances (Spending, Cash Flow, Net Worth, Transactions).

## 2. First, ask the user (it changes the build)

| Question | If yes | If no |
|---|---|---|
| Is it a **company card** (a bill you pay yourself, or the company pays)? | Build the full recipe: charges + statements + paid. | Their own card: skip statements/paid (§5.4–5.5); work expenses are rows on their normal card in `transactions`. A light version is enough — see §9. |
| Can they **download the card's activity as CSV**? | Import (§6). | Manual add form only. |
| Statement **closing day** and how many days later it's **due**? | Store per card (`work_cards.closing_day`, `due_days`). | Default: closing day = none → group by calendar month. |
| Does the company ever **pay the card directly**? | Keep the `reimbursement` type (§4). | Can hide it from the menus. |
| Do they ever get **partial** reimbursements? | Keep `reimbursed_amount`. | Still keep it — it costs nothing. |
| More than one work card? | `work_cards` table (§3). | Same table, one row. |

## 3. Data model

New tables, added to `SCHEMA` in `src/db.py` (`CREATE TABLE IF NOT EXISTS`, so `_migrate` needs nothing until a
later column change):

```sql
-- One per work card. Not an `accounts` row on purpose: accounts feed net worth.
CREATE TABLE IF NOT EXISTS work_cards (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,          -- "Work Visa"
    closing_day INTEGER,                -- 1-28; NULL = statements are calendar months
    due_days INTEGER NOT NULL DEFAULT 25, -- due this many days after closing
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS work_charges (
    id INTEGER PRIMARY KEY,
    card_id INTEGER NOT NULL REFERENCES work_cards(id),
    date TEXT NOT NULL,                 -- YYYY-MM-DD
    description TEXT,
    amount REAL NOT NULL,               -- THIS TABLE's sign: positive = adds to the bill,
                                         -- negative = money onto the card (see §4)
    kind TEXT NOT NULL DEFAULT 'charge',-- what the row is (KINDS)
    status TEXT NOT NULL DEFAULT 'to_submit', -- reimbursement process (STATUSES), work charges only
    reimbursed_amount REAL,             -- NULL = automatic (full once 'reimbursed', else 0); typed = partial
    paid INTEGER NOT NULL DEFAULT 0,    -- marked BY HAND; expense kinds only (§5.5)
    statement_close TEXT,               -- NULL = automatic statement (§5.4); else picked by hand
    note TEXT,
    source TEXT NOT NULL,               -- 'csv_parsed' | 'manual'
    dedup_hash TEXT NOT NULL UNIQUE,
    flagged_duplicate INTEGER NOT NULL DEFAULT 0
);

-- Only what the user typed about a statement; totals are always computed.
CREATE TABLE IF NOT EXISTS work_statements (
    card_id INTEGER NOT NULL REFERENCES work_cards(id),
    close_date TEXT NOT NULL,
    note TEXT,
    out_of_pocket REAL,                 -- NULL = automatic (§5.6)
    PRIMARY KEY (card_id, close_date)
);
```

**Why a separate table instead of `transactions` with a flag?** These are the employer's expenses. In
`transactions` every page would need an exclusion, and one forgotten `WHERE` silently puts the company's spending
into the user's Spending, Cash Flow and net worth. A separate table makes leaking impossible by construction.

## 4. Two labels per row: Type and Status (keep them separate)

The original version had one combined column ("To submit / Reimbursed / Refunded / Personal…"). The user found it
confusing, so it was split into two:

**Type (`kind`)**, what the row *is*:

| key | label | sign | notes |
|---|---|---|---|
| `charge` | Work expense | + | The only type with a Status. |
| `personal` | Personal expense | + | The user's own purchase on the work card: they pay it, nothing to claim. |
| `credit` | Refund / credit | − | A merchant refund. |
| `payment` | Your payment | − | The user paying the card. |
| `reimbursement` | Reimbursed to card | − | The company paying the card directly. |

`EXPENSE_KINDS = (charge, personal)`, `PAYMENT_KINDS = (payment, reimbursement)`.

**Status**, which applies to work expenses only and tracks the company's process:
`to_submit` (every new charge starts here) → `submitted` → `reimbursed` | `rejected`, plus `not_applicable`
(nothing to claim, e.g. the merchant refunded it). Statuses an expense app might show, such as "approved" or
"reported", count as `submitted`; don't add them as statuses.

Rules:
- Both are strict dropdowns. Show each label's meaning in an (i) tooltip (`KIND_HELP`, `STATUS_HELP` dicts; the
  app's `info-tip` convention, CLAUDE.md §7).
- Retyping is **all or nothing and sign-checked**: an expense type needs a positive amount, the others a negative
  one. A mixed selection that doesn't fit changes nothing and says why.
- Setting a Status on a non-`charge` row is silently skipped (only charges have one).

## 5. Business rules (and why)

### 5.1 Sign convention
Card exports usually show charges as **positive** (the reverse of bank accounts). This table keeps "positive = adds
to the bill" because every number on the page reads that way. Convert at import (§6), never per page.

### 5.2 Never touches the user's own finances
Nothing on this page reads or writes `transactions`, `accounts` or `balances`. The money that *does* cross into
their own accounts is the reimbursement deposit and, for a company card they pay, the card payment. Those stay
ordinary transactions and follow the app's **pass-through** rule (CLAUDE.md §6.6): Category `Pass-Through` + Mark Not
Mine. The Transactions page's "Pass-through to finish" reminder already catches half-finished ones. **Do not** try
to link deposits to charges automatically (see §8).

### 5.3 Reimbursed and out of pocket per row
- `reimbursed(row)` = `reimbursed_amount` if typed, else the full amount when `status == 'reimbursed'`, else 0.
- `not_reimbursed(row)`, the part that's on the user: a `personal` row counts in full; a `charge` counts
  `amount − reimbursed` (floored at 0), except `not_applicable`, which counts 0; any other type counts 0.

### 5.4 Statements
- With a `closing_day`: a date on or before the closing day belongs to that month's statement; a later date belongs to
  next month's. Due = close + `due_days`. Clamp the closing day to 28, or handle short months explicitly. Without one,
  statements are calendar months.
- **Name a statement by the month it closes** (how card issuers name them), and always show its due date next to it.
- **Which statement a row is on** (`auto_statement`):
  - expenses and credits: the cycle they're dated in;
  - payments and reimbursements-to-card: **the bill they paid**, which is the statement that most recently closed. A
    payment on Oct 1 pays the September statement.
  - The user can override any row ("Move to statement"); show a `*` on moved rows. Payments made early, before their
    bill closed, need this.

### 5.5 "Paid" is marked by hand, never derived
Mark paid / Mark unpaid on selected rows, or **Mark statement paid** for every expense on a statement. Paid rows
fade.

> **Why:** the first version applied payments and credits to the oldest unpaid charges automatically, the way issuers
> do. With even one payment missing from the imports it confidently put money on the wrong bill (a November credit
> "paid" a statement that was due in October). A "where things stand" panel and a "how each bill was paid" list came
> with it; both confused more than they helped and were removed. The user knows what they paid.

### 5.6 No bill or payment totals per statement
Money on a card isn't tied to a statement (a refund for next month's charge lowers this month's payment), so a "bill
total vs. paid total" looks exact while being wrong. It was built, produced a misleading result in real use, and was
removed. Instead each statement has:
- **Out of pocket**: blank = automatic (sum of `not_reimbursed` over its expenses); typed when reality differs.
- **A note**, to explain why.

### 5.7 Duplicates: flagged, never dropped
Same as the rest of the app (CLAUDE.md §6.9). Reuse `src/dedup.py`: generalize `unique_dedup_hash` with a `table`
parameter restricted to a whitelist (`("transactions", "work_charges")`; table names never come from a request),
and hash with `card_id` in place of `account_id`. Flag an exact repeat, or the same card + date + amount.

## 6. Import

Keep it out of `csv_importer.run`, whose job is account statements. Instead, the page has its own **Import CSV** box:
upload → reuse `csv_importer.detect_parser(lines, filename)` / `GenericCSVParser` to read date, description and amount → then:
1. **Sign:** most card exports are positive = charge. Offer `auto | asis | flip` like the Import page; `auto` =
   flip to "positive = charge" if most rows are negative.
2. **Kind guess** (`guess_kind`): positive → `charge`; negative with "payment" / "thank you" in the description →
   `payment`; other negative → `credit`. A company paying the card can't be told apart from the text, so the user
   retypes it as `reimbursement`.
3. Insert with flag-not-skip duplicate handling; delete the uploaded file after a clean import (constraint: statement
   files are a liability). A parse error imports nothing (`ParserError`, never amounts in messages — CLAUDE.md §6.13).

## 7. The page (`/work-expenses`)

- **Four cards** (always over all rows, never the filtered view): **Next payment** (the oldest statement with unpaid
  expenses: due date, red if past), **To submit** (total + count), **Awaiting reimbursement** (`submitted`), and
  **Out of pocket** (summed over statements marked paid).
- **Statement picker:** All (default) or one statement (`?statement=<close>`). A month grid with a dot per month that
  has rows (open / paid / overdue) scales better than one chip per statement, which was tried and dropped. Picking one
  shows a **statement panel**: Paid pill + Mark statement paid, Out of pocket (blank = automatic), note, Save.
- **Filters:** type, status, card (if several).
- **One table**, newest first, in a fixed-height scroll box with a sticky header: Date, Description, Amount,
  Statement (+ due / paid), Type pill, Status pill, Reimbursed (editable; blank = automatic), Note (auto-growing
  textarea — reuse `autoGrow`). Live selected total.
- **Actions:**
  - Immediate, on checked rows: Set type, Set status, Mark paid / unpaid, Move to statement (or back to automatic),
    Not a duplicate, Delete.
  - Staged, with Save / Discard: Reimbursed and Note, saved all or nothing; saving clears the duplicate flag.
- **Add a row** form: date, amount typed positive (sign set from the type), type, description. Mainly for payments a
  CSV hasn't brought in yet.
- Sidebar entry only when at least one `work_cards` row exists, or behind a setting, so users without a work card
  never see it.

### Endpoints (all POST, JSON; `_fail` for errors; route tests for each, including failures)
| Endpoint | Body | Notes |
|---|---|---|
| `/api/work/cards/add` | `{name, closing_day?, due_days?}` | closing_day 1–28 or blank |
| `/api/work/mark` | `{ids, field, value}` | field ∈ `status` (charges only), `kind` (all-or-nothing + sign check), `paid` (bool, expenses only), `statement` (a valid close date or `auto`), `duplicate` (`false` only) |
| `/api/work/update` | `{edits: [{id, reimbursed_amount?, note?}]}` | all or nothing; blank reimbursed = automatic |
| `/api/work/statement` | `{card_id, close, note?, out_of_pocket?, paid?}` | `paid` marks every expense on it |
| `/api/work/add` | `{card_id, date, amount, kind, description?}` | amount must be non-zero |
| `/api/work/delete` | `{ids}` | |
| `/api/work/import/{upload,preview,run}` | | as §6 |

Code lives in `src/work_expenses.py` (rules, `page_data(conn, …)`), `templates/work_expenses.html`,
`initWorkExpensesPage()` in `static/app.js`.

## 8. Pitfalls already hit (don't repeat them)

1. **Deriving "paid" from payments** led to wrong bills (§5.5).
2. **Per-statement bill/payment totals** looked exact but were wrong (§5.6).
3. **One mixed Type/Status column** was confusing (§4).
4. **Auto-linking reimbursement deposits** in the user's bank to charges: amounts get batched, split, net of
   corrections and arrive weeks later. Keep the two worlds separate; the user marks statuses.
5. **Storing these in `transactions`** risks leaking employer spending into the user's numbers (§3).
6. **Guessing user-specific merchants** ("amex", a company name) in shared code: put patterns in a per-user place,
   never in the general rules.
7. **One chip per statement** in the picker doesn't scale past a year (§7).

## 9. Light version (own card, no company card)

If work expenses go on the user's **own** card, skip the new tables and build:
- a `transactions.work_status` column (`NULL | to_submit | submitted | reimbursed | rejected`) via `_migrate`;
- a "Work…" menu on Transactions like the IOU menu, and a filter;
- a small summary (to submit / awaiting) on the page or Overview.

The purchase and the reimbursement deposit both get Category Pass-Through + Not Mine, so the existing rules keep them
out of Spending and Cash Flow.

## 10. Tests to write (temp DBs only, like the rest of `tests/`)

- `statement_dates` across month and year boundaries; a closing day of 28; no closing day (calendar months).
- `auto_statement`: charge on the closing day vs. one day after; payment → previous statement; override wins.
- `reimbursed` / `not_reimbursed` for each type and status, including partial and `not_applicable`.
- Import: sign auto-flip, kind guess, duplicate flagged (not skipped), a parse error imports nothing.
- Each endpoint: happy path and every rejection (bad ids, wrong sign for a type, invalid close date, zero amount),
  all-or-nothing leaves the DB unchanged.
- Isolation: after adding work charges, Spending, Cash Flow, Net Worth and Transactions are unchanged.
- Docs: add the page to `CLAUDE.md` §5, a section in `README.md` §5, a Help section, and extend `HELP_RELATED` in
  `app.js` (e.g. `"work employer company business job card corporate"`).
