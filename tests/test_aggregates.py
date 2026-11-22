import sqlite3

import pytest

from src.db import SCHEMA
from src import aggregates


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    yield connection
    connection.close()


def _insert_account(conn, name, account_type, currency="USD", owner="self"):
    cur = conn.execute(
        "INSERT INTO accounts (name, institution, account_type, currency, owner) VALUES (?,?,?,?,?)",
        (name, "Bank", account_type, currency, owner),
    )
    return cur.lastrowid


def _insert_balance(conn, account_id, date, balance):
    conn.execute(
        "INSERT INTO balances (account_id, date, balance) VALUES (?,?,?)",
        (account_id, date, balance),
    )


def _insert_txn(conn, account_id, date, amount, description="txn", transaction_type="expense", currency="USD"):
    dedup_hash = f"{account_id}-{date}-{amount}-{description}-{transaction_type}"
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, "
        "description, source, dedup_hash) VALUES (?,?,?,?,?,?,'manual',?)",
        (account_id, date, amount, currency, transaction_type, description, dedup_hash),
    )


def test_latest_balance_rolls_forward_transactions_after_the_anchor(conn):
    checking = _insert_account(conn, "Checking", "checking", owner="self")
    _insert_balance(conn, checking, "2026-01-31", 1000.0)
    _insert_txn(conn, checking, "2026-02-05", -100.0)
    _insert_txn(conn, checking, "2026-02-10", 50.0)

    # As of a date with no newer balances-table snapshot at all, still
    # reflects both transactions dated after the anchor.
    row = aggregates.latest_balance(conn, checking, as_of="2026-08-01")
    assert row["balance"] == 950.0  # 1000 - 100 + 50

    # As of a date strictly between the two transactions, only the first
    # is rolled in.
    row = aggregates.latest_balance(conn, checking, as_of="2026-02-07")
    assert row["balance"] == 900.0

    # Before the anchor itself: no roll-forward, and the anchor's own
    # transaction (if any) isn't double-counted against itself.
    row = aggregates.latest_balance(conn, checking, as_of="2026-01-31")
    assert row["balance"] == 1000.0


def test_latest_balance_does_not_roll_forward_brokerage_or_retirement(conn):
    # These change with market movement, not just transactions - CLAUDE.md
    # expects them to be updated monthly, by hand, so a transaction-only
    # roll-forward would silently understate real gains/losses.
    brokerage = _insert_account(conn, "Fidelity Brokerage", "brokerage", owner="self")
    _insert_balance(conn, brokerage, "2026-01-31", 10000.0)
    _insert_txn(conn, brokerage, "2026-02-05", 500.0, transaction_type="investment_contribution")

    row = aggregates.latest_balance(conn, brokerage, as_of="2026-08-01")
    assert row["balance"] == 10000.0  # unchanged — no roll-forward for this account type


def _insert_contribution(conn, from_account, date, amount, note):
    _insert_txn(conn, from_account, date, -amount, description=f"to {note}", transaction_type="investment_contribution")
    conn.execute("UPDATE transactions SET note = ? WHERE description = ?", (note, f"to {note}"))


def test_contributions_are_matched_to_an_account_by_note(conn):
    checking = _insert_account(conn, "Checking", "checking")
    roth = _insert_account(conn, "Roth IRA", "retirement")
    indiv = _insert_account(conn, "Individual", "brokerage")
    _insert_contribution(conn, checking, "2026-01-05", 500.0, "ROTH for 2026")
    _insert_contribution(conn, checking, "2026-01-06", 300.0, "individual acct")
    _insert_contribution(conn, checking, "2026-01-07", 100.0, "")  # no note
    _insert_contribution(conn, checking, "2026-01-08", 50.0, "roth + individual")  # names both

    # no note, but the description names the account
    _insert_txn(conn, checking, "2026-01-09", -200.0, description="Transfer to Roth IRA",
                transaction_type="investment_contribution")
    # the note wins over a description naming a different account
    _insert_txn(conn, checking, "2026-01-10", -70.0, description="Transfer to Roth IRA",
                transaction_type="investment_contribution")
    conn.execute("UPDATE transactions SET note = 'individual' WHERE date = '2026-01-10'")

    contributions = aggregates.investment_contributions(conn)
    by_date = {c["date"]: c for c in contributions}
    assert by_date["2026-01-09"]["account_id"] == roth
    assert by_date["2026-01-10"]["account_id"] == indiv
    by_desc = {c["description"]: c for c in contributions}
    assert by_desc["to ROTH for 2026"]["account_id"] == roth
    assert by_desc["to ROTH for 2026"]["amount"] == 500.0  # money in, positive
    assert by_desc["to individual acct"]["account_id"] == indiv
    assert by_desc["to "]["account_id"] is None
    assert by_desc["to roth + individual"]["account_id"] is None and by_desc["to roth + individual"]["status"] == "ambiguous"


def test_contributions_roll_investment_value_and_principal_forward(conn):
    checking = _insert_account(conn, "Checking", "checking")
    roth = _insert_account(conn, "Roth IRA", "retirement")
    # no snapshot yet: value and principal are just what's been deposited
    _insert_contribution(conn, checking, "2026-01-05", 1000.0, "roth")
    row = aggregates.latest_balance(conn, roth)
    assert row["balance"] == 1000.0 and row["principal_contributed"] == 1000.0
    assert aggregates.latest_balance(conn, roth, as_of="2026-01-01") is None

    # a snapshot replaces the value; principal keeps summing (blank principal)
    conn.execute("INSERT INTO balances (account_id, date, balance) VALUES (?, '2026-02-01', 1100.0)", (roth,))
    _insert_contribution(conn, checking, "2026-03-01", 500.0, "roth")
    row = aggregates.latest_balance(conn, roth)
    assert row["balance"] == 1600.0  # 1100 snapshot + 500 deposited since
    assert row["principal_contributed"] == 1500.0
    summary = aggregates.investment_summary(conn, [roth])
    assert summary["growth"] == 100.0

    # a typed principal overrides from its date; later deposits add on top
    conn.execute("INSERT INTO balances (account_id, date, balance, principal_contributed) "
                 "VALUES (?, '2026-04-01', 5000.0, 4000.0)", (roth,))
    _insert_contribution(conn, checking, "2026-05-01", 200.0, "roth")
    row = aggregates.latest_balance(conn, roth)
    assert row["balance"] == 5200.0 and row["principal_contributed"] == 4200.0

    trend = aggregates.investment_trend(conn, account_ids=[roth])
    assert [p["date"] for p in trend] == ["2026-01-05", "2026-02-01", "2026-03-01", "2026-04-01", "2026-05-01"]


def test_latest_balance_credit_card_still_falls_back_when_no_snapshot_exists(conn):
    # Unaffected by the roll-forward change: a credit card with zero
    # balances-table history at all still uses the existing computed-sum
    # fallback (negated), same as before.
    card = _insert_account(conn, "Card", "credit_card", owner="self")
    _insert_txn(conn, card, "2026-01-05", -200.0)

    row = aggregates.latest_balance(conn, card, as_of="2026-08-01")
    assert row["balance"] == 200.0  # owed, expressed as positive


def test_latest_balance_sums_transactions_when_no_snapshot_exists(conn):
    # e.g. a CD account created after the Excel import: opening deposit +
    # interest, never a recorded balance — used to read as nothing (0).
    cd = _insert_account(conn, "CD", "savings")
    _insert_txn(conn, cd, "2026-01-15", 10000.0, transaction_type="internal_transfer")
    _insert_txn(conn, cd, "2026-02-15", 40.0, transaction_type="income")

    row = aggregates.latest_balance(conn, cd)
    assert row["balance"] == 10040.0 and row["source"] == "computed"
    assert aggregates.latest_balance(conn, cd, as_of="2026-01-31")["balance"] == 10000.0
    assert aggregates.latest_balance(conn, cd, as_of="2026-01-01") is None  # nothing logged yet — no fake 0

    # a loan is negated like a credit card (owed is stored positive)
    loan = _insert_account(conn, "Loan", "loan")
    _insert_txn(conn, loan, "2026-01-01", -5000.0)
    assert aggregates.latest_balance(conn, loan)["balance"] == 5000.0

    # an account with no transactions at all still has no balance
    assert aggregates.latest_balance(conn, _insert_account(conn, "Empty", "checking")) is None
    # brokerage stays snapshot-only — market value can't be summed
    brokerage = _insert_account(conn, "Brokerage", "brokerage")
    _insert_txn(conn, brokerage, "2026-01-01", 500.0, transaction_type="investment_contribution")
    assert aggregates.latest_balance(conn, brokerage) is None


def test_a_recorded_balance_overrides_the_transaction_sum(conn):
    cd = _insert_account(conn, "CD", "savings")
    _insert_txn(conn, cd, "2026-02-15", 40.0, transaction_type="income")  # deposit was never logged
    _insert_balance(conn, cd, "2026-02-15", 10040.0)  # Set Balance
    _insert_txn(conn, cd, "2026-03-15", 40.0, transaction_type="income")

    row = aggregates.latest_balance(conn, cd)
    assert row["balance"] == 10080.0  # anchor + later interest; same-day interest already included
    assert row.get("source") is None

    breakdown = aggregates.net_worth_breakdown(conn)
    assert breakdown["rows"][0]["from_transactions"] is False


def test_net_worth_excludes_an_account_someone_else_pays(conn):
    checking = _insert_account(conn, "Checking", "checking", owner="self")
    family = _insert_account(conn, "Family Card", "credit_card", owner="other")
    _insert_balance(conn, checking, "2026-01-01", 1000.0)
    _insert_txn(conn, family, "2026-01-01", -500.0)  # no balances row -> computed fallback

    breakdown = aggregates.net_worth_breakdown(conn)
    account_ids = {row["account"]["id"] for row in breakdown["rows"]}
    assert checking in account_ids
    assert family not in account_ids
    assert breakdown["total_liabilities"] == 0.0
    assert breakdown["net_worth"] == 1000.0


def test_net_worth_includes_self_and_joint_but_not_other(conn):
    self_acct = _insert_account(conn, "Self Checking", "checking", owner="self")
    joint_acct = _insert_account(conn, "Joint Savings", "savings", owner="joint")
    other_acct = _insert_account(conn, "Family Card", "credit_card", owner="other")
    _insert_balance(conn, self_acct, "2026-01-01", 100.0)
    _insert_balance(conn, joint_acct, "2026-01-01", 200.0)
    _insert_balance(conn, other_acct, "2026-01-01", 50.0)

    breakdown = aggregates.net_worth_breakdown(conn)
    account_ids = {row["account"]["id"] for row in breakdown["rows"]}
    assert account_ids == {self_acct, joint_acct}


def test_net_worth_trend_excludes_an_account_someone_else_pays(conn):
    checking = _insert_account(conn, "Checking", "checking", owner="self")
    family = _insert_account(conn, "Family Card", "credit_card", owner="other")
    _insert_balance(conn, checking, "2026-01-01", 1000.0)
    _insert_balance(conn, family, "2026-01-01", 500.0)

    trend = aggregates.net_worth_trend(conn, "2026-01-01", "2026-01-31")
    assert len(trend) > 1  # a 30-day span plots weekly, not one point
    assert all(t["net_worth"] == 1000.0 for t in trend)  # the family card's 500 never counted as a liability


def test_balances_by_currency_excludes_an_account_someone_else_owns(conn):
    checking = _insert_account(conn, "Checking", "checking", currency="USD", owner="self")
    other_savings = _insert_account(conn, "Their Savings", "savings", currency="USD", owner="other")
    _insert_balance(conn, checking, "2026-01-01", 1000.0)
    _insert_balance(conn, other_savings, "2026-01-01", 300.0)

    result = aggregates.balances_by_currency(conn)
    assert result["by_currency"]["USD"] == 1000.0


def test_net_worth_counts_personal_transfer_money_like_any_real_balance(conn):
    # personal_transfer gets no special net-worth adjustment (see
    # aggregates.py's net_worth_breakdown comment for why: summing
    # personal_transfer rows can't tell a two-legged pass-through from a
    # one-way repayment, so any such adjustment drifts). Net worth is just
    # the real, current account balance.
    checking = _insert_account(conn, "Checking", "checking", owner="self")
    _insert_balance(conn, checking, "2026-01-10", 1800.0)
    _insert_txn(conn, checking, "2026-01-05", 800.0, transaction_type="personal_transfer")

    breakdown = aggregates.net_worth_breakdown(conn)
    assert breakdown["rows"][0]["balance"] == 1800.0
    assert breakdown["net_worth"] == 1800.0


def test_net_worth_breakdown_include_investments_false_excludes_brokerage_and_retirement(conn):
    checking = _insert_account(conn, "Checking", "checking", owner="self")
    cd = _insert_account(conn, "Marcus CD", "savings", owner="self")
    ira = _insert_account(conn, "Roth IRA", "retirement", owner="self")
    _insert_balance(conn, checking, "2026-01-01", 1000.0)
    _insert_balance(conn, cd, "2026-01-01", 500.0)
    _insert_balance(conn, ira, "2026-01-01", 2000.0)

    full = aggregates.net_worth_breakdown(conn)
    assert full["net_worth"] == 3500.0

    excl = aggregates.net_worth_breakdown(conn, include_investments=False)
    account_ids = {row["account"]["id"] for row in excl["rows"]}
    assert ira not in account_ids
    assert checking in account_ids and cd in account_ids  # a CD is tagged 'savings', never excluded by this flag
    assert excl["net_worth"] == 1500.0


def test_net_worth_trend_include_investments_false_excludes_brokerage_and_retirement(conn):
    checking = _insert_account(conn, "Checking", "checking", owner="self")
    ira = _insert_account(conn, "Roth IRA", "retirement", owner="self")
    _insert_balance(conn, checking, "2026-01-01", 1000.0)
    _insert_balance(conn, ira, "2026-01-01", 2000.0)

    trend = aggregates.net_worth_trend(conn, "2026-01-01", "2026-01-31", include_investments=False)
    assert len(trend) > 1
    assert all(t["net_worth"] == 1000.0 for t in trend)  # the IRA's 2000 never counted


def test_list_cds_reads_current_balance_from_linked_account_without_double_counting(conn):
    # A CD's own row never carries new money — it's a memo layer over
    # whichever account already holds it. A standalone CD account here:
    # current_balance should just be that account's own latest_balance,
    # and total_principal must never be added into net worth anywhere.
    cd_account = _insert_account(conn, "Marcus CD", "savings", owner="self")
    _insert_balance(conn, cd_account, "2026-01-01", 5000.0)
    conn.execute(
        "INSERT INTO cds (account_id, institution, principal, apy, opened_date, maturity_date) "
        "VALUES (?, 'Marcus', 5000.0, 4.5, '2025-06-01', '2026-06-01')",
        (cd_account,),
    )

    result = aggregates.list_cds(conn)
    assert len(result["cds"]) == 1
    assert result["cds"][0]["current_balance"] == 5000.0
    assert result["cds"][0]["account_name"] == "Marcus CD"
    assert result["total_principal"] == 5000.0

    # Net worth is computed straight from the account table — a cds row
    # existing must not change it at all.
    breakdown = aggregates.net_worth_breakdown(conn)
    assert breakdown["net_worth"] == 5000.0


def test_list_cds_for_a_cd_nested_inside_a_retirement_account_is_display_only(conn):
    # A CD held inside an IRA: the linked account is the retirement account
    # itself, whose balance (updated monthly, by hand, like any other
    # brokerage/retirement account) already includes this CD's value.
    # list_cds() must show it for reference without net worth ever
    # counting it a second time.
    ira = _insert_account(conn, "Roth IRA", "retirement", owner="self")
    _insert_balance(conn, ira, "2026-01-01", 12000.0)
    conn.execute(
        "INSERT INTO cds (account_id, institution, principal) VALUES (?, 'Fidelity', 3000.0)",
        (ira,),
    )

    result = aggregates.list_cds(conn)
    assert result["cds"][0]["current_balance"] == 12000.0  # the whole IRA, not just the CD's slice
    assert result["total_principal"] == 3000.0

    breakdown = aggregates.net_worth_breakdown(conn)
    assert breakdown["net_worth"] == 12000.0  # unaffected by the cds row — no double count


def test_net_worth_trend_rolls_forward_real_transactions_between_snapshots(conn):
    # A $500 internal transfer checking -> savings, backed by real
    # transaction rows (as any genuine transfer always is) dated between
    # the two accounts' mismatched balance-snapshot dates (checking near
    # month-end, savings early in the month). True total net worth is a
    # constant 1200 throughout. latest_balance()'s roll-forward (using
    # the actual transfer transactions, not just the balance snapshots)
    # is what keeps every weekly checkpoint correct, not just the ones
    # that happen to land on month boundaries.
    checking = _insert_account(conn, "Checking", "checking", owner="self")
    savings = _insert_account(conn, "Savings", "savings", owner="self")
    _insert_balance(conn, checking, "2026-01-30", 1000.0)  # before the transfer
    _insert_balance(conn, checking, "2026-02-27", 500.0)   # after sending 500 out
    _insert_balance(conn, savings, "2026-01-10", 200.0)    # before the transfer
    _insert_balance(conn, savings, "2026-02-08", 700.0)    # after receiving 500
    _insert_txn(conn, checking, "2026-02-03", -500.0, description="Transfer to Savings", transaction_type="internal_transfer")
    _insert_txn(conn, savings, "2026-02-03", 500.0, description="Transfer from Checking", transaction_type="internal_transfer")

    trend = aggregates.net_worth_trend(conn, "2026-01-31", "2026-02-28")
    # Every point from checking's first anchor onward should be exactly
    # 1200 — including the weekly checkpoints that land strictly between
    # the two accounts' own snapshot dates (e.g. 2026-02-11), which is
    # exactly where the old bug used to show a spike to 1700.
    assert len(trend) > 2  # confirms this is weekly, not monthly, resolution
    assert all(t["net_worth"] == 1200.0 for t in trend)


def test_spending_hides_pass_through_unless_included(conn):
    card = _insert_account(conn, "Card", "credit_card")
    _insert_txn(conn, card, "2026-03-01", -30.0, description="Lunch")
    _insert_txn(conn, card, "2026-03-02", -200.0, description="Work flight")
    _insert_txn(conn, card, "2026-03-03", -50.0, description="Hotel")
    conn.execute("UPDATE transactions SET category = 'Dining' WHERE description = 'Lunch'")
    conn.execute("UPDATE transactions SET category = 'Pass-Through' WHERE description = 'Work flight'")
    conn.execute("UPDATE transactions SET category = 'pass through' WHERE description = 'Hotel'")

    rows, total = aggregates.spending_by_category(conn, "2026-03-01", "2026-03-31")
    assert [r["category"] for r in rows] == ["Dining"]
    assert total == 30.0

    rows, total = aggregates.spending_by_category(conn, "2026-03-01", "2026-03-31", include_pass_through=True)
    assert total == 280.0


def test_investment_trend_starts_at_first_deposit_and_runs_to_the_range_end(conn):
    checking = _insert_account(conn, "Checking", "checking")
    roth = _insert_account(conn, "Roth IRA", "retirement")
    indiv = _insert_account(conn, "Individual", "brokerage")
    _insert_contribution(conn, checking, "2024-01-10", 100.0, "roth")
    _insert_contribution(conn, checking, "2026-03-01", 50.0, "individual")

    # Individual had nothing in 2024 → its line starts at its first deposit
    trend = aggregates.investment_trend(conn, "2024-01-10", "2026-11-01", [indiv])
    assert [p["date"] for p in trend] == ["2026-03-01", "2026-11-01"]
    # Roth already had money before this range → its line opens at the range start
    trend = aggregates.investment_trend(conn, "2025-01-01", "2026-11-01", [roth])
    assert [p["date"] for p in trend] == ["2025-01-01", "2026-11-01"]
    assert trend[0]["value"] == 100.0


def test_fixed_income_band_boxes_each_cd_from_open_to_maturity(conn):
    indiv = _insert_account(conn, "Individual", "brokerage")
    roth = _insert_account(conn, "Roth IRA", "retirement")
    cd_sql = ("INSERT INTO cds (account_id, institution, principal, opened_date, maturity_date, kind) "
              "VALUES (?, 'B', ?, ?, ?, ?)")
    conn.execute(cd_sql, (indiv, 1000.0, "2026-01-15", "2027-01-15", "CD"))
    conn.execute(cd_sql, (indiv, 500.0, "2026-06-01", None, "Treasury"))       # no maturity → to the end
    conn.execute(cd_sql, (roth, 300.0, "2026-03-01", "2026-09-01", "CD"))
    conn.execute(cd_sql, (indiv, 999.0, None, None, "CD"))                       # no opened date → left out

    band = aggregates.fixed_income_trend(conn, "2026-01-01", "2026-12-31", [indiv])
    assert [(p["date"], p["amount"]) for p in band] == [
        ("2026-01-01", 0), ("2026-01-15", 1000), ("2026-06-01", 1500), ("2026-12-31", 1500)]
    # overall: every investment account, stacking; a maturity drops it back out
    band = {p["date"]: p["amount"] for p in aggregates.fixed_income_trend(conn, "2026-01-01", "2026-12-31")}
    assert band["2026-03-01"] == 1300 and band["2026-06-01"] == 1800 and band["2026-09-01"] == 1500
    assert aggregates.fixed_income_trend(conn, "2026-01-01", "2026-12-31", [roth])[-1]["amount"] == 0


def _insert_iou_txn(conn, account_id, date, amount, transaction_type="expense", kind=None, person=None,
                    attributable=1, description="txn"):
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, description, source, "
        "dedup_hash, iou_kind, iou_person, attributable_to_user) VALUES (?,?,?,'USD',?,?,'manual',?,?,?,?)",
        (account_id, date, amount, transaction_type, description, f"{account_id}-{date}-{amount}-{description}",
         kind, person, attributable),
    )


def test_iou_status_rules():
    status = aggregates.iou_status
    # spending on an account someone else pays → they paid for you
    assert status("other", "Mom", "expense", -50, 1, None, None) == ("they_paid", "Mom", "account")
    # a refund on it reduces that again
    assert status("other", "Mom", "income", 20, 1, None, None) == ("i_paid", "Mom", "account")
    # Not Mine and transfers on that account don't count
    assert status("other", "Mom", "expense", -50, 0, None, None) == (None, None, None)
    assert status("other", "Mom", "internal_transfer", 500, 1, None, None) == (None, None, None)
    # nothing is guessed on your own accounts, even personal transfers
    assert status("self", None, "personal_transfer", 200, 1, None, None) == (None, None, None)
    # a manual mark wins, with its own person or the account's
    assert status("self", None, "personal_transfer", -200, 1, "i_paid", "Alex") == ("i_paid", "Alex", "manual")
    assert status("other", "Mom", "expense", -50, 1, "they_paid", None) == ("they_paid", "Mom", "manual")
    assert status("other", "Mom", "expense", -50, 1, "none", None) == (None, None, None)


def test_iou_ledgers_keep_one_balance_per_person(conn):
    checking = _insert_account(conn, "Checking", "checking")
    family = _insert_account(conn, "Family Card", "credit_card", owner="other")
    conn.execute("UPDATE accounts SET iou_person = 'Mom' WHERE id = ?", (family,))
    _insert_iou_txn(conn, family, "2026-01-05", -100.0, description="groceries")         # Mom paid 100
    _insert_iou_txn(conn, family, "2026-01-06", -40.0, attributable=0, description="gift")  # Not Mine
    _insert_iou_txn(conn, checking, "2026-01-20", -60.0, "personal_transfer", "i_paid", "Mom", description="zelle")
    _insert_iou_txn(conn, checking, "2026-01-08", -30.0, "personal_transfer", "i_paid", "Alex", description="dinner")
    conn.execute("INSERT INTO ious (date, person, description, direction, amount) "
                 "VALUES ('2026-01-09', 'Alex', 'cash back', 'they_paid', 10)")

    ledgers = {l["person"]: l for l in aggregates.iou_ledgers(conn)}
    assert ledgers["Mom"]["balance"] == 40.0     # you owe Mom 100 - 60
    assert ledgers["Alex"]["balance"] == -20.0   # Alex owes you 30 - 10
    assert [e["description"] for e in ledgers["Mom"]["they_paid"]] == ["groceries"]
    assert ledgers["Alex"]["i_paid_total"] == 30.0
    assert [b["person"] for b in aggregates.iou_balances(conn)] == ["Mom", "Alex"]  # largest first
    assert aggregates.iou_people(conn) == ["Alex", "Mom"]


def test_a_starting_balance_replaces_earlier_entries_for_that_person_only(conn):
    checking = _insert_account(conn, "Checking", "checking")
    _insert_iou_txn(conn, checking, "2025-12-01", -500.0, "personal_transfer", "they_paid", "Mom", description="old")
    _insert_iou_txn(conn, checking, "2025-12-02", -70.0, "personal_transfer", "i_paid", "Alex", description="old alex")
    conn.execute("INSERT INTO ious (date, person, description, direction, amount) "
                 "VALUES ('2026-01-01', 'Mom', 'Starting balance', 'starting_balance', -25)")
    _insert_iou_txn(conn, checking, "2026-01-15", -100.0, "personal_transfer", "they_paid", "Mom", description="new")

    ledgers = {l["person"]: l for l in aggregates.iou_ledgers(conn)}
    mom = ledgers["Mom"]
    assert mom["balance"] == 75.0  # they owed you 25, then paid 100 for you
    assert mom["replaced_count"] == 1 and mom["starting_balance_date"] == "2026-01-01"
    # a negative starting balance sits on the I-paid side, shown positive
    assert [(e["kind"], e["amount"]) for e in mom["i_paid"]] == [("starting_balance", 25.0)]
    assert ledgers["Alex"]["balance"] == -70.0  # untouched by Mom's starting balance


def test_iou_settlement_marks_never_change_the_balance(conn):
    checking = _insert_account(conn, "Checking", "checking")
    _insert_iou_txn(conn, checking, "2026-01-15", -100.0, "personal_transfer", "they_paid", "Mom")
    before = aggregates.iou_ledgers(conn)[0]["balance"]
    conn.execute("UPDATE transactions SET iou_settlement = 'settled'")
    ledger = aggregates.iou_ledgers(conn)[0]
    assert ledger["balance"] == before
    assert ledger["they_paid_marks"] == {"pending": 0.0, "settled": 100.0, "unmarked": 0.0}
