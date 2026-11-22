import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.db as db
from src.dedup import compute_dedup_hash
from src.maintenance import create_account, list_balance_dates, retag_card_payoff_checking_legs, retag_personal_transfers, set_account_owner


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_maintenance.db")
    db.init_db()
    c = db.get_connection()
    yield c
    c.close()


def make_account(conn, name, account_type="checking", currency="USD", owner="self"):
    cur = conn.execute(
        "INSERT INTO accounts (name, institution, account_type, currency, owner) VALUES (?,?,?,?,?)",
        (name, "Bank", account_type, currency, owner),
    )
    return cur.lastrowid


def make_txn(conn, account_id, date, amount, description, transaction_type="expense", note=None):
    h = compute_dedup_hash(account_id, date, amount, description)
    cur = conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, description, note, "
        "source, dedup_hash) VALUES (?,?,?,?,?,?,?,?,?)",
        (account_id, date, amount, "USD", transaction_type, description, note, "manual", h),
    )
    return cur.lastrowid


def test_set_account_owner_dry_run_then_apply(conn):
    make_account(conn, "Family Card", account_type="credit_card")

    dry = set_account_owner(conn, "Family Card", "other", iou_person="Mom", apply=False)
    assert dry == {"matched": 1, "applied": False}
    row = conn.execute("SELECT owner, iou_person FROM accounts WHERE name = ?", ("Family Card",)).fetchone()
    assert row["owner"] == "self"  # untouched by dry run

    applied = set_account_owner(conn, "Family Card", "other", iou_person="Mom", apply=True)
    assert applied == {"matched": 1, "applied": True}
    row = conn.execute("SELECT owner, iou_person FROM accounts WHERE name = ?", ("Family Card",)).fetchone()
    assert (row["owner"], row["iou_person"]) == ("other", "Mom")

    set_account_owner(conn, "Family Card", "self", apply=True)
    row = conn.execute("SELECT owner, iou_person FROM accounts WHERE name = ?", ("Family Card",)).fetchone()
    assert (row["owner"], row["iou_person"]) == ("self", None)  # the person goes with it


def test_set_account_owner_other_requires_a_person(conn):
    make_account(conn, "Family Card", account_type="credit_card")
    with pytest.raises(ValueError):
        set_account_owner(conn, "Family Card", "other", apply=True)
    with pytest.raises(ValueError):
        set_account_owner(conn, "Family Card", "parents", apply=True)


def test_set_account_owner_no_match(conn):
    result = set_account_owner(conn, "Nonexistent Account", "joint", apply=True)
    assert result == {"matched": 0, "applied": True}


def test_retag_transfers_matches_exact_description_case_insensitive(conn):
    acct = make_account(conn, "BofA Checking")
    repay_id = make_txn(conn, acct, "2026-01-01", -500.0, "Repay")
    transfer_id = make_txn(conn, acct, "2026-01-02", -300.0, "  transfer to FAMILY  ")
    unrelated_id = make_txn(conn, acct, "2026-01-03", -50.0, "Groceries")

    dry = retag_personal_transfers(conn, "BofA Checking", descriptions=["Repay", "Transfer to family"], apply=False)
    assert dry["matched"] == 2
    assert dry["applied"] is False
    # nothing changed yet
    for txn_id in (repay_id, transfer_id, unrelated_id):
        row = conn.execute("SELECT transaction_type FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        assert row["transaction_type"] == "expense"

    applied = retag_personal_transfers(conn, "BofA Checking", descriptions=["Repay", "Transfer to family"], apply=True)
    assert applied["matched"] == 2

    assert conn.execute("SELECT transaction_type FROM transactions WHERE id = ?", (repay_id,)).fetchone()["transaction_type"] == "personal_transfer"
    assert conn.execute("SELECT transaction_type FROM transactions WHERE id = ?", (transfer_id,)).fetchone()["transaction_type"] == "personal_transfer"
    assert conn.execute("SELECT transaction_type FROM transactions WHERE id = ?", (unrelated_id,)).fetchone()["transaction_type"] == "expense"


def test_retag_transfers_skips_already_internal_transfer(conn):
    acct = make_account(conn, "BofA Checking")
    already_id = make_txn(conn, acct, "2026-01-01", -300.0, "Transfer to family", transaction_type="internal_transfer")

    result = retag_personal_transfers(conn, "BofA Checking", descriptions=["Transfer to family"], apply=True)
    assert result["matched"] == 0  # already internal_transfer, not re-matched/re-touched
    row = conn.execute("SELECT transaction_type FROM transactions WHERE id = ?", (already_id,)).fetchone()
    assert row["transaction_type"] == "internal_transfer"


def test_retag_transfers_scoped_to_account_name(conn):
    bofa = make_account(conn, "BofA Checking")
    other = make_account(conn, "Chase Checking")
    bofa_id = make_txn(conn, bofa, "2026-01-01", -500.0, "Repay")
    other_id = make_txn(conn, other, "2026-01-01", -500.0, "Repay")

    result = retag_personal_transfers(conn, "BofA Checking", descriptions=["Repay"], apply=True)
    assert result["matched"] == 1
    assert conn.execute("SELECT transaction_type FROM transactions WHERE id = ?", (bofa_id,)).fetchone()["transaction_type"] == "personal_transfer"
    assert conn.execute("SELECT transaction_type FROM transactions WHERE id = ?", (other_id,)).fetchone()["transaction_type"] == "expense"

    result_again = retag_personal_transfers(conn, "BofA Checking", descriptions=["Repay"], apply=True)
    assert result_again["matched"] == 0  # idempotent — nothing left to match


def test_retag_transfers_by_note_contains_excludes_income(conn):
    acct = make_account(conn, "BofA Checking")
    # description is a generic "Repay" that also covers repaying other
    # people, and category "Reimbursement" also holds unrelated entries —
    # note is the only reliable discriminator here.
    alex_repay_id = make_txn(conn, acct, "2026-01-01", -500.0, "Repay", note="Alex - concert")
    other_repay_id = make_txn(conn, acct, "2026-01-02", -80.0, "Repay", note="Dinner split with Sam")
    someone_reimbursed_me_id = make_txn(
        conn, acct, "2026-01-03", 200.0, "Reimbursement", transaction_type="income", note="Alex paid me back",
    )

    dry = retag_personal_transfers(conn, "BofA Checking", note_contains="Alex", apply=False)
    assert dry["matched"] == 1

    applied = retag_personal_transfers(conn, "BofA Checking", note_contains="Alex", apply=True)
    assert applied["matched"] == 1
    assert conn.execute("SELECT transaction_type FROM transactions WHERE id = ?", (alex_repay_id,)).fetchone()["transaction_type"] == "personal_transfer"
    # unrelated "Repay" (different note) untouched
    assert conn.execute("SELECT transaction_type FROM transactions WHERE id = ?", (other_repay_id,)).fetchone()["transaction_type"] == "expense"
    # income row with the same note untouched — only transaction_type='expense' rows are eligible
    assert conn.execute("SELECT transaction_type FROM transactions WHERE id = ?", (someone_reimbursed_me_id,)).fetchone()["transaction_type"] == "income"


def test_retag_transfers_requires_description_or_note(conn):
    make_account(conn, "BofA Checking")
    with pytest.raises(ValueError):
        retag_personal_transfers(conn, "BofA Checking", apply=False)


def test_create_account_dry_run_then_apply(conn):
    dry = create_account(conn, "Chase Credit Card", institution="Chase", account_type="credit_card", apply=False)
    assert dry == {"created": False, "already_existed": False}
    assert conn.execute("SELECT id FROM accounts WHERE name = ?", ("Chase Credit Card",)).fetchone() is None

    applied = create_account(conn, "Chase Credit Card", institution="Chase", account_type="credit_card", apply=True)
    assert applied == {"created": True, "already_existed": False}
    row = conn.execute("SELECT * FROM accounts WHERE name = ?", ("Chase Credit Card",)).fetchone()
    assert row is not None
    assert row["institution"] == "Chase"
    assert row["account_type"] == "credit_card"
    assert row["currency"] == "USD"  # fx.BASE_CURRENCY by default
    assert row["owner"] == "self"


def test_create_account_already_exists_is_a_noop(conn):
    make_account(conn, "Chase Credit Card", account_type="credit_card")

    result = create_account(conn, "Chase Credit Card", apply=True)
    assert result == {"created": False, "already_existed": True}
    count = conn.execute("SELECT COUNT(*) AS c FROM accounts WHERE name = ?", ("Chase Credit Card",)).fetchone()["c"]
    assert count == 1  # not duplicated


def test_create_account_requires_institution_and_type(conn):
    with pytest.raises(ValueError):
        create_account(conn, "Some Random Bank", apply=True)


def test_create_account_validates_currency_and_owner(conn):
    with pytest.raises(ValueError):
        create_account(conn, "Euro", institution="Bank", account_type="checking", currency="XYZ", apply=True)
    with pytest.raises(ValueError):
        create_account(conn, "Family Card", institution="Bank", account_type="credit_card", owner="other", apply=True)
    create_account(conn, "Family Card", institution="Bank", account_type="credit_card", owner="other",
                   iou_person="Mom", apply=True)
    row = conn.execute("SELECT owner, iou_person FROM accounts WHERE name = 'Family Card'").fetchone()
    assert (row["owner"], row["iou_person"]) == ("other", "Mom")


def test_create_account_new_account_with_explicit_metadata(conn):
    dry = create_account(conn, "Ally HYSA", institution="Ally", account_type="savings", currency="USD", apply=False)
    assert dry == {"created": False, "already_existed": False}

    applied = create_account(conn, "Ally HYSA", institution="Ally", account_type="savings", currency="USD", apply=True)
    assert applied == {"created": True, "already_existed": False}
    row = conn.execute("SELECT * FROM accounts WHERE name = ?", ("Ally HYSA",)).fetchone()
    assert row["institution"] == "Ally"
    assert row["account_type"] == "savings"
    assert row["currency"] == "USD"
    assert row["owner"] == "self"


def test_create_account_new_account_rejects_bad_account_type(conn):
    with pytest.raises(ValueError):
        create_account(conn, "Ally HYSA", institution="Ally", account_type="not_a_real_type", apply=True)


def test_retag_card_payoffs_matches_amount_and_nearby_date(conn):
    checking = make_account(conn, "BofA Checking", account_type="checking")
    card = make_account(conn, "BofA Credit Card", account_type="credit_card")
    make_txn(conn, card, "2026-01-10", -75.00, "Trader Joe's", transaction_type="expense")
    card_payment_id = make_txn(conn, card, "2026-01-15", 500.00, "Payment Thank You - Web", transaction_type="internal_transfer")
    checking_leg_id = make_txn(conn, checking, "2026-01-16", -500.00, "Online Banking transfer to CRD 1234", transaction_type="expense")
    unrelated_id = make_txn(conn, checking, "2026-01-17", -60.00, "Groceries", transaction_type="expense")

    dry = retag_card_payoff_checking_legs(conn, "BofA Checking", apply=False)
    assert dry["matched"] == 1
    assert conn.execute("SELECT transaction_type FROM transactions WHERE id = ?", (checking_leg_id,)).fetchone()["transaction_type"] == "expense"

    applied = retag_card_payoff_checking_legs(conn, "BofA Checking", apply=True)
    assert applied["matched"] == 1
    assert conn.execute("SELECT transaction_type FROM transactions WHERE id = ?", (checking_leg_id,)).fetchone()["transaction_type"] == "internal_transfer"
    # unrelated expense, and the card payment's own row, untouched
    assert conn.execute("SELECT transaction_type FROM transactions WHERE id = ?", (unrelated_id,)).fetchone()["transaction_type"] == "expense"
    assert conn.execute("SELECT transaction_type FROM transactions WHERE id = ?", (card_payment_id,)).fetchone()["transaction_type"] == "internal_transfer"

    result_again = retag_card_payoff_checking_legs(conn, "BofA Checking", apply=True)
    assert result_again["matched"] == 0  # idempotent — already internal_transfer, no longer matches 'expense'


def test_retag_card_payoffs_respects_date_window(conn):
    checking = make_account(conn, "BofA Checking", account_type="checking")
    card = make_account(conn, "BofA Credit Card", account_type="credit_card")
    make_txn(conn, card, "2026-01-01", 500.00, "Payment Thank You - Web", transaction_type="internal_transfer")
    far_checking_leg_id = make_txn(conn, checking, "2026-01-20", -500.00, "Card Payment", transaction_type="expense")

    result = retag_card_payoff_checking_legs(conn, "BofA Checking", date_window_days=5, apply=True)
    assert result["matched"] == 0
    assert conn.execute("SELECT transaction_type FROM transactions WHERE id = ?", (far_checking_leg_id,)).fetchone()["transaction_type"] == "expense"


def test_retag_card_payoffs_ignores_a_card_someone_else_owns(conn):
    checking = make_account(conn, "BofA Checking", account_type="checking")
    family = make_account(conn, "Family Card", account_type="credit_card", owner="other")
    make_txn(conn, family, "2026-01-15", 500.00, "Payment", transaction_type="internal_transfer")
    checking_leg_id = make_txn(conn, checking, "2026-01-16", -500.00, "Transfer to Family Card", transaction_type="expense")

    result = retag_card_payoff_checking_legs(conn, "BofA Checking", apply=True)
    assert result["matched"] == 0
    assert conn.execute("SELECT transaction_type FROM transactions WHERE id = ?", (checking_leg_id,)).fetchone()["transaction_type"] == "expense"


def test_retag_card_payoffs_unknown_account_raises(conn):
    with pytest.raises(ValueError):
        retag_card_payoff_checking_legs(conn, "Nonexistent Checking", apply=False)


def _insert_balance(conn, account_id, date):
    conn.execute("INSERT INTO balances (account_id, date, balance) VALUES (?,?,?)", (account_id, date, 0.0))


def test_list_balance_dates_groups_by_account(conn):
    checking = make_account(conn, "Checking")
    savings = make_account(conn, "Savings")
    _insert_balance(conn, checking, "2026-01-29")
    _insert_balance(conn, checking, "2026-02-26")
    _insert_balance(conn, savings, "2026-02-07")
    conn.commit()

    result = list_balance_dates(conn)
    assert result["Checking"] == ["2026-01-29", "2026-02-26"]
    assert result["Savings"] == ["2026-02-07"]


def test_list_balance_dates_respects_start_end(conn):
    checking = make_account(conn, "Checking")
    _insert_balance(conn, checking, "2026-01-01")
    _insert_balance(conn, checking, "2026-02-15")
    _insert_balance(conn, checking, "2026-03-01")
    conn.commit()

    result = list_balance_dates(conn, start="2026-02-01", end="2026-02-28")
    assert result["Checking"] == ["2026-02-15"]


def test_list_balance_dates_excludes_an_account_someone_else_owns(conn):
    checking = make_account(conn, "Checking", owner="self")
    family = make_account(conn, "Family Card", owner="other")
    _insert_balance(conn, checking, "2026-01-01")
    _insert_balance(conn, family, "2026-01-01")
    conn.commit()

    result = list_balance_dates(conn)
    assert "Checking" in result
    assert "Family Card" not in result


def test_list_balance_dates_omits_accounts_with_no_snapshots(conn):
    make_account(conn, "Checking")  # no balances rows at all
    conn.commit()

    result = list_balance_dates(conn)
    assert result == {}
