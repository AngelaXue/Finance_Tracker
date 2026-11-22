import sqlite3

import pytest

from src.db import SCHEMA
from src.dedup import compute_dedup_hash, find_possible_duplicate, normalize_description, unique_dedup_hash


def test_normalize_description_strips_case_and_punctuation():
    assert normalize_description("Trader Joe's #123!") == "trader joes 123"


def test_normalize_description_collapses_whitespace():
    assert normalize_description("  Whole   Foods  ") == "whole foods"


def test_normalize_description_handles_none():
    assert normalize_description(None) == ""


def test_dedup_hash_is_deterministic():
    a = compute_dedup_hash(1, "2026-01-08", -42.50, "Trader Joe's")
    b = compute_dedup_hash(1, "2026-01-08", -42.50, "Trader Joe's")
    assert a == b


def test_dedup_hash_ignores_formatting_differences():
    a = compute_dedup_hash(1, "2026-01-08", -42.50, "TRADER JOE'S #123")
    b = compute_dedup_hash(1, "2026-01-08", -42.50, "trader joes 123")
    assert a == b


def test_dedup_hash_differs_on_amount():
    a = compute_dedup_hash(1, "2026-01-08", -42.50, "Trader Joe's")
    b = compute_dedup_hash(1, "2026-01-08", -42.51, "Trader Joe's")
    assert a != b


def test_dedup_hash_differs_on_account():
    a = compute_dedup_hash(1, "2026-01-08", -42.50, "Trader Joe's")
    b = compute_dedup_hash(2, "2026-01-08", -42.50, "Trader Joe's")
    assert a != b


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


def _insert_txn(conn, date, amount, description):
    h = compute_dedup_hash(1, date, amount, description)
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, "
        "description, source, dedup_hash) VALUES (1, ?, ?, 'USD', 'expense', ?, 'manual', ?)",
        (date, amount, description, h),
    )


def test_find_possible_duplicate_matches_same_date_and_amount_different_description(conn):
    _insert_txn(conn, "2026-01-08", -42.50, "Trader Joes")  # e.g. hand-typed in the old workbook
    conn.commit()

    match_id = find_possible_duplicate(conn, 1, "2026-01-08", -42.50)
    assert match_id is not None


def test_find_possible_duplicate_none_when_no_match(conn):
    _insert_txn(conn, "2026-01-08", -42.50, "Trader Joes")
    conn.commit()

    assert find_possible_duplicate(conn, 1, "2026-01-09", -42.50) is None  # different date
    assert find_possible_duplicate(conn, 1, "2026-01-08", -10.00) is None  # different amount


def test_find_possible_duplicate_scoped_to_account(conn):
    conn.execute(
        "INSERT INTO accounts (id, name, institution, account_type, currency, owner) "
        "VALUES (2, 'Other Checking', 'Bank', 'checking', 'USD', 'self')"
    )
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, "
        "description, source, dedup_hash) VALUES (2, '2026-01-08', -42.50, 'USD', 'expense', 'x', 'manual', 'h2')"
    )
    conn.commit()

    assert find_possible_duplicate(conn, 1, "2026-01-08", -42.50) is None


def test_unique_dedup_hash_first_occurrence_matches_plain_hash(conn):
    h, was_disambiguated = unique_dedup_hash(conn, 1, "2026-01-08", -42.50, "Trader Joe's")
    assert h == compute_dedup_hash(1, "2026-01-08", -42.50, "Trader Joe's")
    assert was_disambiguated is False


def test_unique_dedup_hash_disambiguates_repeated_identical_transaction(conn):
    # Two real bus rides, same day, same fare, same description.
    _insert_txn(conn, "2026-01-05", -2.75, "MTA SUBWAY")
    conn.commit()

    h, was_disambiguated = unique_dedup_hash(conn, 1, "2026-01-05", -2.75, "MTA SUBWAY")
    assert was_disambiguated is True
    assert h != compute_dedup_hash(1, "2026-01-05", -2.75, "MTA SUBWAY")  # doesn't collide with the first ride

    # Confirm it's actually insertable (no UNIQUE collision) and a third
    # ride gets its own distinct hash too.
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, "
        "description, source, dedup_hash) VALUES (1, '2026-01-05', -2.75, 'USD', 'expense', 'MTA SUBWAY', 'csv_parsed', ?)",
        (h,),
    )
    conn.commit()

    h3, was_disambiguated_3 = unique_dedup_hash(conn, 1, "2026-01-05", -2.75, "MTA SUBWAY")
    assert was_disambiguated_3 is True
    assert h3 not in (compute_dedup_hash(1, "2026-01-05", -2.75, "MTA SUBWAY"), h)
