import sqlite3

import pytest

from src.db import SCHEMA
from src.fx import RATES
from src.sankey import compute_sankey


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO accounts (id, name, institution, account_type, currency, owner) "
        "VALUES (1, 'Checking', 'Bank', 'checking', 'USD', 'self')"
    )
    yield connection
    connection.close()


def _insert_txn(conn, date, amount, transaction_type, category=None, description="txn", attributable_to_user=1, currency="USD"):
    dedup_hash = f"{date}-{amount}-{description}-{transaction_type}-{currency}"
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, category, "
        "transaction_type, description, source, dedup_hash, attributable_to_user) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, 'manual', ?, ?)",
        (date, amount, currency, category, transaction_type, description, dedup_hash, attributable_to_user),
    )


def test_positive_residual_produces_cash_savings(conn):
    _insert_txn(conn, "2026-01-05", 3000, "income", category="Salary")
    _insert_txn(conn, "2026-01-10", -1500, "expense", category="Rent")
    _insert_txn(conn, "2026-01-15", -500, "investment_contribution", category="Investing")
    conn.commit()

    result = compute_sankey(conn, "2026-01-01", "2026-01-31")

    assert result["income_total"] == 3000
    assert result["hard_outputs"] == 2000
    assert result["residual"] == 1000
    dest_labels = {d["label"]: d["amount"] for d in result["destinations"]}
    assert dest_labels["Cash Savings"] == 1000
    assert not any(s["label"] == "From Savings" for s in result["sources"])


def test_negative_residual_produces_from_savings(conn):
    _insert_txn(conn, "2026-01-05", 1000, "income", category="Salary")
    _insert_txn(conn, "2026-01-10", -2500, "expense", category="Rent")
    conn.commit()

    result = compute_sankey(conn, "2026-01-01", "2026-01-31")

    assert result["residual"] == -1500
    source_labels = {s["label"]: s["amount"] for s in result["sources"]}
    assert source_labels["From Savings"] == 1500
    assert not any(d["label"] == "Cash Savings" for d in result["destinations"])
    # Sides balance by construction.
    total_sources = sum(s["amount"] for s in result["sources"])
    total_destinations = sum(d["amount"] for d in result["destinations"])
    assert round(total_sources, 2) == round(total_destinations, 2)


def test_internal_transfer_excluded(conn):
    _insert_txn(conn, "2026-01-05", 3000, "income", category="Salary")
    _insert_txn(conn, "2026-01-10", -1500, "expense", category="Rent")
    _insert_txn(conn, "2026-01-12", -1000, "internal_transfer", description="To Savings")
    _insert_txn(conn, "2026-01-12", 1000, "internal_transfer", description="From Checking")
    conn.commit()

    result = compute_sankey(conn, "2026-01-01", "2026-01-31")

    assert result["income_total"] == 3000
    assert result["hard_outputs"] == 1500


def test_personal_transfer_excluded_like_internal_transfer(conn):
    _insert_txn(conn, "2026-01-05", 3000, "income", category="Salary")
    _insert_txn(conn, "2026-01-10", -1500, "expense", category="Rent")
    _insert_txn(conn, "2026-01-12", -500, "personal_transfer", description="Paid Mom back")
    conn.commit()

    result = compute_sankey(conn, "2026-01-01", "2026-01-31")

    assert result["income_total"] == 3000
    assert result["hard_outputs"] == 1500  # the personal_transfer row doesn't count as spending


def test_foreign_currency_converted_to_base(conn):
    _insert_txn(conn, "2026-01-05", 1000, "income", category="Salary", currency="USD")
    cny_rate = RATES["CNY"]
    _insert_txn(conn, "2026-01-06", 1000, "income", category="Gift", currency="CNY")
    _insert_txn(conn, "2026-01-10", -500, "expense", category="Rent", currency="USD")
    conn.commit()

    result = compute_sankey(conn, "2026-01-01", "2026-01-31")

    assert result["income_total"] == pytest.approx(1000 + 1000 * cny_rate)
    assert result["hard_outputs"] == pytest.approx(500)


def test_links_include_pct_of_income(conn):
    _insert_txn(conn, "2026-01-05", 1000, "income", category="Salary")
    _insert_txn(conn, "2026-01-10", -600, "expense", category="Rent")
    conn.commit()

    result = compute_sankey(conn, "2026-01-01", "2026-01-31")

    salary_link = next(l for l in result["links"] if l["from"] == "Salary")
    assert salary_link["pct"] == pytest.approx(100.0)
    rent_link = next(l for l in result["links"] if l["to"] == "Living Expenses")
    assert rent_link["pct"] == pytest.approx(60.0)


def test_not_attributable_income_excluded_from_income_total(conn):
    _insert_txn(conn, "2026-01-05", 2000, "income", category="Salary")
    _insert_txn(
        conn, "2026-01-07", 800, "income", category="Passing Money",
        description="From a friend, headed to someone else", attributable_to_user=0,
    )
    _insert_txn(conn, "2026-01-10", -500, "expense", category="Rent")
    conn.commit()

    result = compute_sankey(conn, "2026-01-01", "2026-01-31")

    # Only the real $2000 salary counts as income — the $800 pass-through
    # deposit doesn't inflate income_total just because it touched the
    # account, mirroring how a Not-Mine expense doesn't inflate spending.
    assert result["income_total"] == 2000
    assert result["residual"] == 1500
    assert not any(s["label"] == "Passing Money" for s in result["sources"])


def test_not_attributable_expense_excluded_from_hard_outputs(conn):
    _insert_txn(conn, "2026-01-05", 4000, "income", category="Salary")
    _insert_txn(conn, "2026-01-10", -2000, "expense", category="Dining")  # the user's own spending
    _insert_txn(
        conn, "2026-01-12", -3000, "expense", category="Travel",
        description="Flights for a relative, bought as a gift", attributable_to_user=0,
    )
    conn.commit()

    result = compute_sankey(conn, "2026-01-01", "2026-01-31")

    # Only the user's own $2000 counts — the $3000 gift purchase is excluded
    # entirely from the income-vs-spending comparison, even though it's
    # still a normal 'expense' row (Spending's category breakdown still
    # sees it — that's a separate, unfiltered query in aggregates.py).
    assert result["hard_outputs"] == 2000
    assert result["residual"] == 2000
    assert not any(s["label"] == "From Savings" for s in result["sources"])


def test_manual_they_paid_iou_counts_as_paid_by_others(conn):
    _insert_txn(conn, "2026-01-05", 3000, "income", category="Salary")
    conn.execute(
        "INSERT INTO ious (date, person, description, direction, amount) "
        "VALUES ('2026-01-10', 'Mom', 'Mom paid my rent directly', 'they_paid', 400)"
    )
    # I lent a friend cash: a receivable, not spending
    conn.execute(
        "INSERT INTO ious (date, person, description, direction, amount) "
        "VALUES ('2026-01-11', 'Alex', 'Cash loan', 'i_paid', 100)"
    )
    conn.commit()

    result = compute_sankey(conn, "2026-01-01", "2026-01-31")

    assert result["hard_outputs"] == 400
    dest_labels = {d["label"]: d["amount"] for d in result["destinations"]}
    assert dest_labels["Paid by Others"] == 400


def test_transaction_ious_are_not_counted_twice(conn):
    # A purchase on a card someone else pays is already in Living
    # Expenses — it's an IOU too, but derived live, never stored in `ious`.
    _insert_txn(conn, "2026-01-05", 3000, "income", category="Salary")
    _insert_txn(conn, "2026-01-10", -200, "expense", category="Groceries", description="Family card purchase")
    conn.commit()

    result = compute_sankey(conn, "2026-01-01", "2026-01-31")

    assert result["hard_outputs"] == 200
    assert not any(d["label"] == "Paid by Others" for d in result["destinations"])
