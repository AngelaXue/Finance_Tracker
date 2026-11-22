"""Schema migrations: a database from the original single-user version
(a "parents ledger", family_transfer, owner 'parents') upgrades in place,
losing nothing."""

import sqlite3

import pytest

import src.db as db
from src import aggregates

OLD_SCHEMA = """
CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL, institution TEXT NOT NULL,
    account_type TEXT NOT NULL, currency TEXT NOT NULL, owner TEXT NOT NULL DEFAULT 'self', last4 TEXT,
    is_active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE transactions (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, date TEXT NOT NULL,
    amount REAL NOT NULL, currency TEXT NOT NULL, category TEXT, transaction_type TEXT NOT NULL,
    description TEXT, note TEXT, source TEXT NOT NULL, dedup_hash TEXT NOT NULL UNIQUE,
    manually_corrected INTEGER NOT NULL DEFAULT 0, original_category TEXT,
    parent_debt TEXT, parent_settlement TEXT);
CREATE TABLE balances (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, date TEXT NOT NULL,
    balance REAL NOT NULL, principal_contributed REAL, UNIQUE(account_id, date));
CREATE TABLE parent_ledger (id INTEGER PRIMARY KEY, date TEXT NOT NULL, description TEXT NOT NULL,
    direction TEXT NOT NULL, amount REAL NOT NULL, related_transaction_id INTEGER);
CREATE TABLE cds (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, institution TEXT NOT NULL,
    principal REAL NOT NULL, apy REAL, opened_date TEXT, maturity_date TEXT, note TEXT);
"""


@pytest.fixture
def old_db(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.executescript("""
        INSERT INTO accounts (name, institution, account_type, currency, owner)
            VALUES ('CMB Card', 'CMB', 'credit_card', 'USD', 'parents'), ('Checking', 'Bank', 'checking', 'USD', 'self');
        INSERT INTO transactions (account_id, date, amount, currency, transaction_type, description, source, dedup_hash)
            VALUES (1, '2026-01-05', -100, 'USD', 'expense', 'groceries', 'manual', 'a');
        INSERT INTO transactions (account_id, date, amount, currency, transaction_type, description, source,
                                  dedup_hash, parent_debt, parent_settlement)
            VALUES (2, '2026-01-20', -60, 'USD', 'family_transfer', 'zelle to mom', 'manual', 'b', 'paid', 'settled');
        INSERT INTO parent_ledger (date, description, direction, amount)
            VALUES ('2026-01-02', 'paid my rent', 'charge', 400), ('2026-01-03', 'cash', 'payment', 50);
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


def test_old_database_upgrades_in_place(old_db):
    db.init_db()
    db.init_db()  # idempotent
    conn = db.get_connection()
    try:
        cmb = conn.execute("SELECT owner, iou_person FROM accounts WHERE name = 'CMB Card'").fetchone()
        assert (cmb["owner"], cmb["iou_person"]) == ("other", "Parents")

        zelle = conn.execute("SELECT * FROM transactions WHERE description = 'zelle to mom'").fetchone()
        assert zelle["transaction_type"] == "personal_transfer"
        assert (zelle["iou_kind"], zelle["iou_person"], zelle["iou_settlement"]) == ("i_paid", "Parents", "settled")

        manual = conn.execute("SELECT person, direction, amount FROM ious ORDER BY date").fetchall()
        assert [tuple(r) for r in manual] == [("Parents", "they_paid", 400.0), ("Parents", "i_paid", 50.0)]
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'parent_ledger'").fetchone()

        ledger = aggregates.iou_ledgers(conn)[0]
        # 100 card spending + 400 manual − 60 repaid − 50 cash
        assert (ledger["person"], ledger["balance"]) == ("Parents", 390.0)
        assert "kind" in {c["name"] for c in conn.execute("PRAGMA table_info(cds)").fetchall()}
    finally:
        conn.close()


def test_fresh_database_has_the_current_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "fresh.db")
    db.init_db()
    conn = db.get_connection()
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    conn.close()
    assert {"iou_kind", "iou_person", "iou_settlement", "flagged_duplicate"} <= columns
    assert "parent_debt" not in columns
    assert "ious" in tables and "parent_ledger" not in tables
