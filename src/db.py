import sqlite3

from src import paths

BASE_DIR = paths.BASE_DIR
DB_PATH = paths.DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    institution TEXT NOT NULL,
    account_type TEXT NOT NULL,         -- see ACCOUNT_TYPES in maintenance.py
    currency TEXT NOT NULL,             -- a code from fx.RATES
    owner TEXT NOT NULL DEFAULT 'self', -- 'self' | 'joint' | 'other'. 'other'
                                         -- = someone else settles this
                                         -- account (e.g. a card a family
                                         -- member pays off): left out of
                                         -- net worth entirely, but its
                                         -- spending still shows in Spending/
                                         -- Cash Flow, and counts toward what
                                         -- you owe `iou_person` on the IOUs
                                         -- page.
    iou_person TEXT,                    -- owner='other' only: who pays it
    last4 TEXT,                         -- optional, never the full number
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    date TEXT NOT NULL,                 -- 'YYYY-MM-DD'
    amount REAL NOT NULL,               -- positive = money in, negative = out
    currency TEXT NOT NULL,
    category TEXT,                      -- free-text label ('Groceries', ...)
    transaction_type TEXT NOT NULL,     -- what the row COUNTS AS — see
                                         -- TRANSACTION_TYPES in app.py:
                                         -- income | expense |
                                         -- internal_transfer (between your
                                         -- own accounts) | personal_transfer
                                         -- (to/from other people, not income
                                         -- or spending) |
                                         -- investment_contribution
    description TEXT,
    note TEXT,
    method TEXT,                        -- payment rail, free text (Card,
                                         -- Zelle, Cash, ...)
    source TEXT NOT NULL,               -- 'csv_parsed' | 'history_import'
                                         -- | 'manual'
    dedup_hash TEXT NOT NULL UNIQUE,    -- see src/dedup.py
    manually_corrected INTEGER NOT NULL DEFAULT 0,
    original_category TEXT,             -- the guess a correction replaced
    category_predicted INTEGER NOT NULL DEFAULT 0, -- 1 = filled in
                                         -- automatically and not reviewed
                                         -- yet ("needs review", yellow)
    statement_balance REAL,             -- the account's balance AFTER this
                                         -- row, as the bank's statement
                                         -- recorded it (checking/savings
                                         -- CSVs). NULL = not known; the
                                         -- running balance is summed
                                         -- instead. Cleared if the row's
                                         -- date/account/amount is edited,
                                         -- since it no longer matches.
    attributable_to_user INTEGER NOT NULL DEFAULT 1,
                                         -- 0 = "Not Mine": a real purchase
                                         -- (or income) that wasn't your own
                                         -- money. Left out of Cash Flow's
                                         -- income vs. spending, and out of
                                         -- automatic IOUs; still counted on
                                         -- Spending (it answers "was this my
                                         -- money", not "what was it").
    iou_kind TEXT,                      -- IOUs override: NULL = automatic
                                         -- (see aggregates.iou_status),
                                         -- 'they_paid' = money/value from
                                         -- iou_person to you (you owe them
                                         -- more, or they paid you back),
                                         -- 'i_paid' = from you to them,
                                         -- 'none' = not an IOU.
    iou_person TEXT,                    -- who, for a 'they_paid'/'i_paid'
                                         -- override (NULL = the account's
                                         -- own iou_person)
    iou_settlement TEXT,                -- NULL | 'pending' | 'settled':
                                         -- a reminder mark on the IOUs
                                         -- page, never changes a balance
    investment_account TEXT,            -- Investments override for an
                                         -- investment row: NULL = automatic
                                         -- (matched by note, then
                                         -- description), an account id, or
                                         -- 'none' = not an investment. Never
                                         -- changes transaction_type.
    flagged_duplicate INTEGER NOT NULL DEFAULT 0
                                         -- 1 = matched an existing row's
                                         -- account/date/amount when added
                                         -- (see src/dedup.py). Kept, never
                                         -- dropped — two real transactions
                                         -- can share every field — and
                                         -- highlighted for review.
);

CREATE TABLE IF NOT EXISTS balances (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    date TEXT NOT NULL,
    balance REAL NOT NULL,              -- liabilities: amount owed, positive
    principal_contributed REAL,         -- legacy: a typed running principal
                                         -- total (investments only)
    note TEXT,
    UNIQUE(account_id, date)
);

-- IOUs entered by hand — only what has NO transaction behind it (someone
-- paid for something of yours directly, cash lent, ...). IOUs that come
-- from transactions are worked out live (aggregates.iou_entries), never
-- copied in here. Per person, the balance is "what you owe them":
-- they_paid adds, i_paid subtracts (negative = they owe you), and
-- 'starting_balance' says "as of this date the balance was exactly
-- `amount`" (signed), replacing everything for that person on or before it.
CREATE TABLE IF NOT EXISTS ious (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    person TEXT NOT NULL,
    description TEXT NOT NULL,
    direction TEXT NOT NULL,            -- 'they_paid' | 'i_paid' | 'starting_balance'
    amount REAL NOT NULL,               -- positive, except a starting balance
    settlement TEXT                     -- same reminder mark as
                                         -- transactions.iou_settlement
);

-- CDs & bonds (`kind`). A memo layer: the money always sits inside some
-- real account (account_id) and is counted once through that account's
-- balance — principal/apy/dates here are never added into Net Worth.
CREATE TABLE IF NOT EXISTS cds (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    institution TEXT NOT NULL,
    principal REAL NOT NULL,
    apy REAL,                           -- display only, never used to estimate
    opened_date TEXT,
    maturity_date TEXT,
    note TEXT,
    kind TEXT NOT NULL DEFAULT 'CD'     -- app.FIXED_INCOME_KINDS
);

-- Interest earned on one CD/bond, logged by hand. Its current balance =
-- principal + the sum of these. Memo only, like cds itself: the money is
-- already in the holding account's balance.
CREATE TABLE IF NOT EXISTS cd_interest (
    id INTEGER PRIMARY KEY,
    cd_id INTEGER NOT NULL REFERENCES cds(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    amount REAL NOT NULL,               -- usually positive; negative = a correction
    note TEXT
);

-- "Principal added" typed on the Investments page: new money into a
-- brokerage/retirement account that didn't come through a logged
-- transaction. Counted exactly like a matched investment_contribution.
CREATE TABLE IF NOT EXISTS investment_deposits (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    date TEXT NOT NULL,
    amount REAL NOT NULL,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account_id);
CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category);
CREATE INDEX IF NOT EXISTS idx_balances_account_date ON balances(account_id, date);
CREATE INDEX IF NOT EXISTS idx_cds_account ON cds(account_id);
"""


def get_connection():
    # Git doesn't track empty dirs, so a fresh clone won't have data/ yet.
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _migrate_before_schema(conn):
    """Renames that have to happen before SCHEMA's CREATE TABLE IF NOT
    EXISTS would create the new, empty table instead. The app began as a
    one-person tracker with a "parents ledger"; that became the general
    IOUs table, with the person filled in as 'Parents'."""
    if _table_exists(conn, "parent_ledger") and not _table_exists(conn, "ious"):
        conn.execute("ALTER TABLE parent_ledger RENAME TO ious")
        conn.execute("ALTER TABLE ious ADD COLUMN person TEXT NOT NULL DEFAULT 'Parents'")
        conn.execute("UPDATE ious SET direction = 'they_paid' WHERE direction = 'charge'")
        conn.execute("UPDATE ious SET direction = 'i_paid' WHERE direction = 'payment'")


def _migrate(conn):
    """CREATE TABLE IF NOT EXISTS won't add new columns to an existing
    table, so a column added to the schema after the first init needs an
    explicit, idempotent ALTER TABLE here. Runs at every app start."""
    existing = _columns(conn, "transactions")
    for column, ddl in (
        ("method", "TEXT"),
        ("category_predicted", "INTEGER NOT NULL DEFAULT 0"),
        ("statement_balance", "REAL"),
        ("attributable_to_user", "INTEGER NOT NULL DEFAULT 1"),
        ("flagged_duplicate", "INTEGER NOT NULL DEFAULT 0"),
        ("investment_account", "TEXT"),
    ):
        if column not in existing:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {column} {ddl}")

    # parents ledger → IOUs (see _migrate_before_schema)
    if "parent_debt" in existing and "iou_kind" not in existing:
        conn.execute("ALTER TABLE transactions RENAME COLUMN parent_debt TO iou_kind")
        conn.execute("UPDATE transactions SET iou_kind = 'they_paid' WHERE iou_kind = 'owe'")
        conn.execute("UPDATE transactions SET iou_kind = 'i_paid' WHERE iou_kind = 'paid'")
        conn.execute("ALTER TABLE transactions ADD COLUMN iou_person TEXT")
        conn.execute("UPDATE transactions SET iou_person = 'Parents' WHERE iou_kind IN ('they_paid', 'i_paid')")
    if "parent_settlement" in existing and "iou_settlement" not in existing:
        conn.execute("ALTER TABLE transactions RENAME COLUMN parent_settlement TO iou_settlement")
    existing = _columns(conn, "transactions")
    for column in ("iou_kind", "iou_person", "iou_settlement"):
        if column not in existing:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {column} TEXT")
    conn.execute("UPDATE transactions SET transaction_type = 'personal_transfer' "
                 "WHERE transaction_type = 'family_transfer'")

    if "iou_person" not in _columns(conn, "accounts"):
        conn.execute("ALTER TABLE accounts ADD COLUMN iou_person TEXT")
    conn.execute("UPDATE accounts SET owner = 'other', iou_person = COALESCE(iou_person, 'Parents') "
                 "WHERE owner = 'parents'")

    if "note" not in _columns(conn, "balances"):
        conn.execute("ALTER TABLE balances ADD COLUMN note TEXT")
    if "kind" not in _columns(conn, "cds"):
        conn.execute("ALTER TABLE cds ADD COLUMN kind TEXT NOT NULL DEFAULT 'CD'")
    if "settlement" not in _columns(conn, "ious"):
        conn.execute("ALTER TABLE ious ADD COLUMN settlement TEXT")


def init_db():
    conn = get_connection()
    try:
        _migrate_before_schema(conn)
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
