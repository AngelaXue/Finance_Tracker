import sqlite3

import pytest

from src.db import SCHEMA
from src import categorize


@pytest.fixture
def conn(tmp_path, monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO accounts (id, name, institution, account_type, currency, owner) "
        "VALUES (1, 'Card', 'Bank', 'credit_card', 'USD', 'self')"
    )
    # Isolate the model file per test run so tests don't interfere with a
    # real classifier.joblib or with each other.
    monkeypatch.setattr(categorize, "MODEL_PATH", tmp_path / "classifier.joblib")
    yield connection
    connection.close()


def _insert(conn, description, category):
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, category, "
        "transaction_type, description, source, dedup_hash) "
        "VALUES (1, '2026-01-01', -20, 'USD', ?, 'expense', ?, 'manual', ?)",
        (category, description, description),
    )


def test_out_of_vocabulary_description_does_not_default_to_majority_class(conn):
    # Heavily imbalanced on purpose — this reproduces the real bug where
    # everything got predicted as the most common training category.
    # Uses 5 categories (not fewer) deliberately: with too few categories,
    # random/uniform baseline confidence alone can exceed MIN_CONFIDENCE
    # (e.g. 3 categories -> ~33% baseline, above the 0.3 threshold, which
    # would make this test meaningless) — the real data has 18 categories
    # (~5.6% baseline), so 5 is a closer, still-meaningful approximation.
    for desc in ["Chipotle", "Sushi Place", "Pizza Night", "Diner Downtown", "Cafe Latte"]:
        _insert(conn, desc, "Dining")
    _insert(conn, "Trader Joes", "Groceries")
    _insert(conn, "Amazon", "Shopping")
    _insert(conn, "Uber", "Transportation")
    _insert(conn, "Comcast", "Utilities")
    conn.commit()

    categorize.train_classifier(conn)

    assert categorize.predict_category("Zzyx Qwerty Repay") is None


def test_clear_match_still_predicts_confidently(conn):
    for desc in ["Chipotle", "Sushi Place", "Pizza Night", "Diner Downtown", "Cafe Latte"]:
        _insert(conn, desc, "Dining")
    _insert(conn, "Trader Joes", "Groceries")
    _insert(conn, "Amazon", "Shopping")
    conn.commit()

    categorize.train_classifier(conn)

    assert categorize.predict_category("Trader Joes") == "Groceries"
    assert categorize.predict_category("Chipotle") == "Dining"


def test_predict_uncategorized_skips_internal_transfers(conn):
    for desc in ["Chipotle", "Sushi Place", "Pizza Night", "Diner Downtown"]:
        _insert(conn, desc, "Dining")
    _insert(conn, "Trader Joes", "Groceries")
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, category, "
        "transaction_type, description, source, dedup_hash) "
        "VALUES (1, '2026-01-02', -300, 'USD', NULL, 'internal_transfer', 'Transfer to Savings', 'manual', 'h1')"
    )
    conn.commit()

    categorize.predict_uncategorized(conn)

    row = conn.execute("SELECT category, category_predicted FROM transactions WHERE description = 'Transfer to Savings'").fetchone()
    assert row["category"] is None
    assert row["category_predicted"] == 0


def test_apply_rules_specific_pattern_beats_generic_transfer():
    # "roth ira" must win over the generic "transfer" catch-all even
    # though both patterns appear in the same description.
    assert categorize.apply_rules("Transfer to Roth IRA") == ("Investing", "investment_contribution")


def test_apply_rules_generic_transfer_sets_type_only():
    assert categorize.apply_rules("Transfer to Savings") == (None, "internal_transfer")


def test_apply_rules_no_match_returns_none():
    assert categorize.apply_rules("Completely unrelated text") is None


def test_apply_rules_payment_thank_you_sets_internal_transfer():
    assert categorize.apply_rules("Payment Thank You - Web") == (None, "internal_transfer")


def test_predict_uncategorized_applies_investment_rule_and_flags_for_review(conn):
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, category, "
        "transaction_type, description, source, dedup_hash) "
        "VALUES (1, '2026-01-03', -500, 'USD', NULL, 'expense', 'Transfer to Roth IRA', 'manual', 'h2')"
    )
    conn.commit()

    result = categorize.predict_uncategorized(conn)

    row = conn.execute(
        "SELECT category, transaction_type, category_predicted FROM transactions WHERE description = 'Transfer to Roth IRA'"
    ).fetchone()
    assert row["category"] == "Investing"
    assert row["transaction_type"] == "investment_contribution"
    assert row["category_predicted"] == 1
    assert result["flagged"] == 1
    assert result["auto_classified"] == 0


def test_predict_uncategorized_generic_transfer_rule_does_not_flag(conn):
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, category, "
        "transaction_type, description, source, dedup_hash) "
        "VALUES (1, '2026-01-04', -100, 'USD', NULL, 'expense', 'Transfer to Savings', 'manual', 'h3')"
    )
    conn.commit()

    result = categorize.predict_uncategorized(conn)

    row = conn.execute(
        "SELECT category, transaction_type, category_predicted FROM transactions WHERE description = 'Transfer to Savings'"
    ).fetchone()
    assert row["category"] is None
    assert row["transaction_type"] == "internal_transfer"
    assert row["category_predicted"] == 0
    assert result["flagged"] == 0
    assert result["auto_classified"] == 1
