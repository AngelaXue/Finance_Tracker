import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.db as db
from src import aggregates, categorize
from src.dedup import compute_dedup_hash
from src.parsers import csv_importer, history_importer


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_app_routes.db")
    # Nothing a test does may touch the real statements/history folders or classifier
    monkeypatch.setattr(csv_importer, "INCOMING_DIR", tmp_path / "incoming_statements")
    monkeypatch.setattr(csv_importer, "CSV_FORMATS_PATH", tmp_path / "csv_formats.json")
    monkeypatch.setattr(history_importer, "HISTORY_DIR", tmp_path / "import_history")
    monkeypatch.setattr(categorize, "MODEL_PATH", tmp_path / "classifier.joblib")
    db.init_db()
    import app as flask_app_module
    flask_app_module.app.testing = True
    return flask_app_module.app.test_client()


def test_help_page_renders_with_expected_sections(client):
    resp = client.get("/help")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    for anchor in (
        "quick-start", "type-vs-category", "not-mine", "personal-transfer", "card-payoffs",
        "net-worth-adjustments", "duplicates", "categorization", "currencies",
        "digital-wallets", "ious", "periods", "importing", "terminal",
        "logging", "investments", "overview", "net-worth-page", "cash-flow", "spending", "transactions",
        "import-page",
    ):
        assert f'id="{anchor}"' in html
    # General topics first, then pages in sidebar order
    order = [html.index(f'<details class="card help-section" id="{a}"') for a in (
        "quick-start", "type-vs-category", "terminal", "overview", "net-worth-page", "cash-flow", "spending",
        "transactions", "import-page", "investments", "ious")]
    assert order == sorted(order)
    assert "Expand all" in html and "Collapse all" in html and 'class="card help-section"' in html


def test_sidebar_links_to_help_page(client):
    resp = client.get("/transactions")
    html = resp.get_data(as_text=True)
    assert 'href="/help"' in html


def test_transactions_page_type_header_links_to_help_anchor(client):
    resp = client.get("/transactions")
    html = resp.get_data(as_text=True)
    assert 'href="/help#type-vs-category"' in html


def test_adding_two_identical_new_rows_keeps_both_flagged(client):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO accounts (name, institution, account_type, currency, owner) VALUES (?,?,?,?,?)",
        ("Checking", "Bank", "checking", "USD", "self"),
    )
    conn.commit()
    conn.close()

    new_row = dict(
        date="2026-01-05", account_name="Checking", amount="-2.75",
        description="MTA SUBWAY", transaction_type="expense",
    )
    resp = client.post("/api/transactions/save", json={
        "edits": [], "confirmed_ids": [], "new_rows": [new_row, new_row],
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["created"] == 2  # both kept, neither silently dropped
    assert body["skipped"] == []

    conn = db.get_connection()
    rows = conn.execute(
        "SELECT flagged_duplicate FROM transactions WHERE description = 'MTA SUBWAY' ORDER BY id"
    ).fetchall()
    conn.close()
    assert len(rows) == 2
    assert rows[0]["flagged_duplicate"] == 0  # first occurrence: unflagged
    assert rows[1]["flagged_duplicate"] == 1  # second: flagged for review


def test_new_row_response_reports_newly_flagged_id(client):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO accounts (name, institution, account_type, currency, owner) VALUES (?,?,?,?,?)",
        ("Checking", "Bank", "checking", "USD", "self"),
    )
    conn.commit()
    conn.close()

    new_row = dict(
        date="2026-01-05", account_name="Checking", amount="-2.75",
        description="MTA SUBWAY", transaction_type="expense",
    )
    resp = client.post("/api/transactions/save", json={
        "edits": [], "confirmed_ids": [], "new_rows": [new_row, new_row],
    })
    body = resp.get_json()
    assert len(body["newly_flagged"]) == 1

    conn = db.get_connection()
    flagged_id = body["newly_flagged"][0]
    row = conn.execute("SELECT flagged_duplicate FROM transactions WHERE id = ?", (flagged_id,)).fetchone()
    conn.close()
    assert row["flagged_duplicate"] == 1


def test_editing_a_row_into_a_duplicate_is_saved_and_flagged_not_blocked(client):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO accounts (name, institution, account_type, currency, owner) VALUES (?,?,?,?,?)",
        ("Checking", "Bank", "checking", "USD", "self"),
    )
    conn.commit()
    account_id = conn.execute("SELECT id FROM accounts").fetchone()["id"]

    h1 = compute_dedup_hash(account_id, "2026-01-05", -2.75, "MTA SUBWAY")
    h2 = compute_dedup_hash(account_id, "2026-01-06", -2.75, "MTA SUBWAY")
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, description, "
        "source, dedup_hash) VALUES (?,?,?, 'USD', 'expense', ?, 'manual', ?)",
        (account_id, "2026-01-05", -2.75, "MTA SUBWAY", h1),
    )
    cursor = conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, description, "
        "source, dedup_hash) VALUES (?,?,?, 'USD', 'expense', ?, 'manual', ?)",
        (account_id, "2026-01-06", -2.75, "MTA SUBWAY", h2),
    )
    other_id = cursor.lastrowid
    conn.commit()
    conn.close()

    # Edit the second row's date so it now exactly matches the first row —
    # this used to raise sqlite3.IntegrityError and get silently blocked.
    resp = client.post("/api/transactions/save", json={
        "edits": [{"id": other_id, "date": "2026-01-05"}], "confirmed_ids": [], "new_rows": [],
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["skipped"] == []  # not blocked
    assert other_id in body["newly_flagged"]

    conn = db.get_connection()
    row = conn.execute("SELECT date, flagged_duplicate FROM transactions WHERE id = ?", (other_id,)).fetchone()
    conn.close()
    assert row["date"] == "2026-01-05"  # the edit was actually applied
    assert row["flagged_duplicate"] == 1


def test_editing_a_flagged_row_without_a_new_collision_clears_the_flag(client):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO accounts (name, institution, account_type, currency, owner) VALUES (?,?,?,?,?)",
        ("Checking", "Bank", "checking", "USD", "self"),
    )
    conn.commit()
    account_id = conn.execute("SELECT id FROM accounts").fetchone()["id"]
    h = compute_dedup_hash(account_id, "2026-01-05", -2.75, "MTA SUBWAY")
    cursor = conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, description, "
        "source, dedup_hash, flagged_duplicate) VALUES (?,?,?, 'USD', 'expense', ?, 'manual', ?, 1)",
        (account_id, "2026-01-05", -2.75, "MTA SUBWAY", h),
    )
    txn_id = cursor.lastrowid
    conn.commit()
    conn.close()

    resp = client.post("/api/transactions/save", json={
        "edits": [{"id": txn_id, "date": "2026-02-01"}], "confirmed_ids": [], "new_rows": [],
    })
    assert resp.status_code == 200

    conn = db.get_connection()
    row = conn.execute("SELECT flagged_duplicate FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    conn.close()
    assert row["flagged_duplicate"] == 0


def test_add_iou_entry(client):
    resp = client.post("/api/ious/add", json={
        "date": "2026-1-15", "person": "Mom", "description": "Paid my phone bill",
        "direction": "they_paid", "amount": "-1,500",
    })
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True

    conn = db.get_connection()
    row = conn.execute("SELECT * FROM ious").fetchone()
    conn.close()
    assert (row["date"], row["person"], row["direction"], row["amount"]) == ("2026-01-15", "Mom", "they_paid", 1500.0)


@pytest.mark.parametrize("payload", [
    {"date": "2026-01-15"},
    {"date": "2026-01-15", "person": "Mom", "description": "x", "direction": "bogus", "amount": 10},
    {"date": "2026-01-15", "person": "Mom", "description": "x", "direction": "they_paid", "amount": "lots"},
    {"date": "2026-01-15", "person": "Mom", "description": "x", "direction": "they_paid", "amount": 0},
    {"date": "2026-01-15", "person": "", "description": "x", "direction": "they_paid", "amount": 5},
    {"date": "not a date", "person": "Mom", "description": "x", "direction": "they_paid", "amount": 5},
    {"date": "2026-01-15", "person": "Mom", "description": "", "direction": "i_paid", "amount": 5},
])
def test_add_iou_entry_rejects_bad_input(client, payload):
    resp = client.post("/api/ious/add", json=payload)
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


def test_starting_balance_is_signed_and_may_be_zero(client):
    assert client.post("/api/ious/add", json={
        "date": "2026-01-01", "person": "Alex", "direction": "starting_balance", "amount": "-40"}).status_code == 200
    assert client.post("/api/ious/add", json={
        "date": "2026-01-01", "person": "Sam", "direction": "starting_balance", "amount": 0}).status_code == 200
    conn = db.get_connection()
    ledgers = {l["person"]: l for l in aggregates.iou_ledgers(conn)}
    conn.close()
    assert ledgers["Alex"]["balance"] == -40.0  # Alex owed you 40
    assert ledgers["Sam"]["balance"] == 0.0


def test_delete_iou_entries(client):
    client.post("/api/ious/add", json={
        "date": "2026-01-01", "person": "Mom", "description": "x", "direction": "they_paid", "amount": 100})
    conn = db.get_connection()
    entry_id = conn.execute("SELECT id FROM ious").fetchone()["id"]
    conn.close()

    resp = client.post("/api/ious/delete", json={"ids": [entry_id]})
    assert resp.status_code == 200
    assert resp.get_json()["deleted"] == 1
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) AS c FROM ious").fetchone()["c"] == 0
    conn.close()


def test_set_attributable_marks_not_mine_then_mine(client):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO accounts (name, institution, account_type, currency, owner, iou_person) VALUES (?,?,?,?,?,?)",
        ("Family Card", "Bank", "credit_card", "USD", "other", "Mom"),
    )
    acct_id = conn.execute("SELECT id FROM accounts").fetchone()["id"]
    h = compute_dedup_hash(acct_id, "2026-01-01", -3000.0, "Flights for Mom")
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, "
        "description, source, dedup_hash) VALUES (?,?,?, 'USD', 'expense', ?, 'manual', ?)",
        (acct_id, "2026-01-01", -3000.0, "Flights for Mom", h),
    )
    conn.commit()
    txn_id = conn.execute("SELECT id FROM transactions").fetchone()["id"]
    assert conn.execute("SELECT attributable_to_user FROM transactions WHERE id = ?", (txn_id,)).fetchone()["attributable_to_user"] == 1
    conn.close()

    def attributable():
        conn = db.get_connection()
        value = conn.execute("SELECT attributable_to_user FROM transactions WHERE id = ?", (txn_id,)).fetchone()[0]
        conn.close()
        return value

    resp = client.post("/api/transactions/set-attributable", json={"ids": [txn_id], "value": 0})
    assert resp.status_code == 200
    assert resp.get_json()["updated"] == 1
    assert attributable() == 0

    # marking Not Mine again is a no-op, not a flip back
    client.post("/api/transactions/set-attributable", json={"ids": [txn_id], "value": 0})
    assert attributable() == 0
    conn = db.get_connection()
    assert aggregates.iou_ledgers(conn) == []  # a Not Mine purchase isn't owed
    conn.close()

    # Mark Mine reverses it
    client.post("/api/transactions/set-attributable", json={"ids": [txn_id], "value": 1})
    assert attributable() == 1


def test_set_attributable_rejects_bad_value(client):
    resp = client.post("/api/transactions/set-attributable", json={"ids": [1], "value": "yes"})
    assert resp.status_code == 400


def _seed_iou_data():
    """A card Mom pays: a purchase (counts automatically) and a gift marked
    Not Mine (ignored). On checking: money from Alex and a Zelle to Mom —
    neither counts until marked."""
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO accounts (name, institution, account_type, currency, owner, iou_person) VALUES "
        "('Family Card', 'Bank', 'credit_card', 'USD', 'other', 'Mom'), "
        "('Checking', 'Bank', 'checking', 'USD', 'self', NULL)"
    )
    family, checking = [r["id"] for r in conn.execute("SELECT id FROM accounts ORDER BY id").fetchall()]
    rows = [
        (family, "2026-01-05", -100.0, "expense", "Groceries", 1),
        (family, "2026-01-06", -300.0, "expense", "Gift for aunt", 0),
        (checking, "2026-01-10", 500.0, "personal_transfer", "From Alex for concert tickets", 1),
        (checking, "2026-01-20", -150.0, "personal_transfer", "Zelle to mom", 1),
    ]
    ids = []
    for acct, d, amt, tt, desc, attributable in rows:
        cur = conn.execute(
            "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, description, "
            "source, dedup_hash, attributable_to_user) VALUES (?,?,?, 'USD', ?, ?, 'manual', ?, ?)",
            (acct, d, amt, tt, desc, compute_dedup_hash(acct, d, amt, desc), attributable),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    return ids


def _ledgers():
    conn = db.get_connection()
    result = {l["person"]: l for l in aggregates.iou_ledgers(conn)}
    conn.close()
    return result


def test_ious_derive_from_an_account_someone_else_pays(client):
    _seed_iou_data()
    ledgers = _ledgers()
    # only the 100 groceries: the Not Mine gift and the unmarked transfers don't count
    assert list(ledgers) == ["Mom"]
    assert ledgers["Mom"]["balance"] == 100.0


def test_set_iou_marks_transactions_and_auto_undoes(client):
    groceries, gift, from_alex, zelle = _seed_iou_data()
    resp = client.post("/api/transactions/set-iou", json={"ids": [zelle], "value": "i_paid", "person": "Mom"})
    assert resp.status_code == 200
    client.post("/api/transactions/set-iou", json={"ids": [from_alex], "value": "they_paid", "person": "Alex"})
    client.post("/api/transactions/set-iou", json={"ids": [groceries], "value": "none"})

    ledgers = _ledgers()
    assert ledgers["Mom"]["balance"] == -150.0  # groceries opted out, 150 paid
    assert ledgers["Alex"]["balance"] == 500.0  # Alex paid you 500 → you owe Alex

    # "auto" clears an override — the groceries count again
    client.post("/api/transactions/set-iou", json={"ids": [groceries], "value": "auto"})
    assert _ledgers()["Mom"]["balance"] == -50.0


@pytest.mark.parametrize("payload", [
    {"ids": [1], "value": "maybe"},
    {"ids": [1], "value": "they_paid"},  # who?
    {"ids": [1], "value": "i_paid", "person": "  "},
])
def test_set_iou_rejects_bad_input(client, payload):
    assert client.post("/api/transactions/set-iou", json=payload).status_code == 400


def test_starting_balance_replaces_earlier_entries(client):
    groceries, gift, from_alex, zelle = _seed_iou_data()
    client.post("/api/transactions/set-iou", json={"ids": [zelle], "value": "i_paid", "person": "Mom"})
    resp = client.post("/api/ious/add", json={
        "date": "2026-1-10", "person": "Mom", "description": "Carried over from my old spreadsheet",
        "direction": "starting_balance", "amount": 1000,
    })
    assert resp.status_code == 200

    mom = _ledgers()["Mom"]
    # Groceries (01-05) is replaced; only the 01-20 repayment counts after it
    assert mom["starting_balance_date"] == "2026-01-10"
    assert mom["balance"] == 850.0
    assert mom["replaced_count"] == 1


def test_transactions_page_embeds_autocomplete_suggestions(client):
    import json
    import re
    _seed_iou_data()
    page = client.get("/transactions").get_data(as_text=True)  # this month: no rows shown
    match = re.search(r'<script type="application/json" id="suggestion-data">(.*?)</script>', page, re.S)
    assert match
    data = json.loads(match.group(1))
    assert data["accounts"] == ["Checking", "Family Card"]
    # drawn from all history, not just the rows currently filtered in
    assert "Zelle to mom" in data["descriptions"]
    assert len(data["descriptions"]) == 4
    assert '<option value="Mom">' in page  # the IOU person suggestions


def test_autocomplete_defaults_use_most_common_reviewed_values(client):
    import json
    import re
    conn = db.get_connection()
    conn.execute("INSERT INTO accounts (name, institution, account_type, currency) VALUES ('Card', 'Bank', 'credit_card', 'USD')")
    acct = conn.execute("SELECT id FROM accounts").fetchone()["id"]
    rows = [
        # (date, description, category, method, category_predicted)
        ("2026-01-01", "Verizon", "Phone", "Card", 0),
        ("2026-02-01", "Verizon", "Utilities", "Card", 0),
        ("2026-03-01", "Verizon", "Utilities", "Zelle", 0),
        ("2026-04-01", "Verizon", "Dining", None, 1),  # unreviewed guesses are ignored...
        ("2026-04-02", "Verizon", "Dining", None, 1),
        ("2026-04-03", "Verizon", "Dining", None, 1),  # ...even when they outnumber
        ("2026-05-01", "Mystery", None, None, 0),
    ]
    for i, (d, desc, cat, method, predicted) in enumerate(rows):
        conn.execute(
            "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, description, category, "
            "method, category_predicted, source, dedup_hash) VALUES (?,?,-10,'USD','expense',?,?,?,?,'manual',?)",
            (acct, d, desc, cat, method, predicted, f"h{i}"),
        )
    conn.commit()
    conn.close()

    page = client.get("/transactions").get_data(as_text=True)
    data = json.loads(re.search(r'id="suggestion-data">(.*?)</script>', page, re.S).group(1))
    assert data["defaults"]["Verizon"] == {"category": "Utilities", "method": "Card", "transaction_type": "expense"}
    # no category/method on record, but a type always is
    assert data["defaults"]["Mystery"] == {"category": None, "method": None, "transaction_type": "expense"}
    assert data["categories"] == ["Dining", "Phone", "Utilities"]
    assert 'list="category-options"' not in page  # Category uses the custom dropdown now


def test_autocomplete_defaults_include_most_common_type(client):
    import json
    import re
    conn = db.get_connection()
    conn.execute("INSERT INTO accounts (name, institution, account_type, currency) VALUES ('Checking', 'Bank', 'checking', 'USD')")
    acct = conn.execute("SELECT id FROM accounts").fetchone()["id"]
    for i, tt in enumerate(["personal_transfer", "personal_transfer", "expense"]):
        conn.execute(
            "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, description, "
            "source, dedup_hash) VALUES (?, ?, -10, 'USD', ?, 'Zelle to mom', 'manual', ?)",
            (acct, f"2026-01-0{i + 1}", tt, f"t{i}"),
        )
    conn.commit()
    conn.close()

    page = client.get("/transactions").get_data(as_text=True)
    data = json.loads(re.search(r'id="suggestion-data">(.*?)</script>', page, re.S).group(1))
    assert data["defaults"]["Zelle to mom"]["transaction_type"] == "personal_transfer"


def test_new_row_account_defaults_to_filtered_account(client):
    import re
    conn = db.get_connection()
    conn.execute("INSERT INTO accounts (name, institution, account_type, currency) VALUES ('A Checking', 'Bank', 'checking', 'USD')")
    conn.execute("INSERT INTO accounts (name, institution, account_type, currency) VALUES ('Z Card', 'Bank', 'credit_card', 'USD')")
    card_id = conn.execute("SELECT id FROM accounts WHERE name = 'Z Card'").fetchone()["id"]
    conn.commit()
    conn.close()

    def new_row_account(page):
        return re.search(r'data-new-field="account_name">(.*?)</textarea>', page, re.S).group(1)

    assert new_row_account(client.get("/transactions").get_data(as_text=True)) == ""
    filtered = client.get(f"/transactions?account_id={card_id}").get_data(as_text=True)
    assert new_row_account(filtered) == "Z Card"


def test_transactions_page_iou_filter_and_badges(client):
    groceries, gift, from_alex, zelle = _seed_iou_data()
    client.post("/api/transactions/set-iou", json={"ids": [zelle], "value": "i_paid", "person": "Mom"})
    import re

    def visible(url):
        # the autocomplete data lists every description, filtered or not
        return re.sub(r'<script type="application/json" id="suggestion-data">.*?</script>', "",
                      client.get(url).get_data(as_text=True), flags=re.S)

    html = visible("/transactions?period=all&iou=they_paid")
    assert "Groceries" in html
    assert "Gift for aunt" not in html and "Zelle to mom" not in html and "From Alex" not in html
    assert "iou-badge-they_paid" in html
    html = visible("/transactions?period=all&iou=any")
    assert "Zelle to mom" in html and "iou-badge-i_paid" in html and "iou-badge-manual" in html


def test_settlement_marks_are_reminders_only(client):
    groceries, gift, from_alex, zelle = _seed_iou_data()
    client.post("/api/transactions/set-iou", json={"ids": [zelle], "value": "i_paid", "person": "Mom"})
    client.post("/api/ious/add", json={
        "date": "2026-01-15", "person": "Mom", "description": "Mom paid my phone bill", "direction": "they_paid",
        "amount": 40,
    })
    conn = db.get_connection()
    manual_id = conn.execute("SELECT id FROM ious").fetchone()["id"]
    conn.close()
    before = _ledgers()["Mom"]["balance"]

    resp = client.post("/api/ious/set-settlement", json={
        "items": [{"source": "transaction", "id": groceries}, {"source": "manual", "id": manual_id}],
        "value": "settled",
    })
    assert resp.status_code == 200
    assert resp.get_json()["updated"] == 2
    client.post("/api/ious/set-settlement", json={"items": [{"source": "transaction", "id": zelle}], "value": "pending"})

    mom = _ledgers()["Mom"]
    assert mom["balance"] == before  # unchanged by the marks
    assert mom["they_paid_marks"] == {"pending": 0.0, "settled": 140.0, "unmarked": 0.0}
    assert mom["i_paid_marks"]["pending"] == 150.0

    # null clears the mark
    client.post("/api/ious/set-settlement", json={"items": [{"source": "transaction", "id": zelle}], "value": None})
    assert _ledgers()["Mom"]["i_paid_marks"]["unmarked"] == 150.0


def test_set_settlement_rejects_bad_value(client):
    resp = client.post("/api/ious/set-settlement", json={"items": [], "value": "done"})
    assert resp.status_code == 400


def test_ious_page_lists_people_and_opens_one(client):
    _seed_iou_data()
    client.post("/api/ious/add", json={
        "date": "2026-01-02", "person": "Alex", "description": "Lent cash", "direction": "i_paid", "amount": 30})
    html = client.get("/ious").get_data(as_text=True)
    assert "You owe $100.00" in html and "Owes you $30.00" in html
    assert "Groceries" in html  # the largest balance (Mom) opens by default
    html = client.get("/ious?person=Alex").get_data(as_text=True)
    assert "Lent cash" in html and "Groceries" not in html


def test_ious_page_empty_state(client):
    html = client.get("/ious").get_data(as_text=True)
    assert "No IOUs yet" in html


def test_overview_lists_ious_with_debts(client):
    _seed_iou_data()
    html = client.get("/").get_data(as_text=True)
    assert "You owe Mom" in html and "/ious?person=Mom" in html


def test_transactions_page_defaults_to_this_month(client):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO accounts (name, institution, account_type, currency, owner) VALUES (?,?,?,?,?)",
        ("Checking", "Bank", "checking", "USD", "self"),
    )
    acct_id = conn.execute("SELECT id FROM accounts").fetchone()["id"]
    old_date = "2020-01-15"
    this_month_date = date.today().replace(day=1).isoformat()
    h_old = compute_dedup_hash(acct_id, old_date, -111.0, "old txn")
    h_new = compute_dedup_hash(acct_id, this_month_date, -222.0, "this month txn")
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, description, "
        "source, dedup_hash) VALUES (?,?,?, 'USD', 'expense', ?, 'manual', ?)",
        (acct_id, old_date, -111.0, "old txn", h_old),
    )
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, description, "
        "source, dedup_hash) VALUES (?,?,?, 'USD', 'expense', ?, 'manual', ?)",
        (acct_id, this_month_date, -222.0, "this month txn", h_new),
    )
    conn.commit()
    conn.close()

    # No period param -> defaults to this month, excluding the old row
    resp = client.get("/transactions")
    html = resp.get_data(as_text=True)
    assert "-222.00" in html
    assert "-111.00" not in html

    # period=all opts back into seeing everything
    resp = client.get("/transactions?period=all")
    html = resp.get_data(as_text=True)
    assert "-222.00" in html
    assert "-111.00" in html


def test_transactions_page_filters_by_attributable(client):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO accounts (name, institution, account_type, currency, owner) VALUES (?,?,?,?,?)",
        ("Checking", "Bank", "checking", "USD", "self"),
    )
    acct_id = conn.execute("SELECT id FROM accounts").fetchone()["id"]
    h1 = compute_dedup_hash(acct_id, "2026-01-01", -100.0, "mine")
    h2 = compute_dedup_hash(acct_id, "2026-01-02", -200.0, "not mine")
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, description, "
        "source, dedup_hash, attributable_to_user) VALUES (?,?,?, 'USD', 'expense', ?, 'manual', ?, 1)",
        (acct_id, "2026-01-01", -100.0, "mine", h1),
    )
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, description, "
        "source, dedup_hash, attributable_to_user) VALUES (?,?,?, 'USD', 'expense', ?, 'manual', ?, 0)",
        (acct_id, "2026-01-02", -200.0, "not mine", h2),
    )
    conn.commit()
    conn.close()

    # period=all, since these fixture dates predate "this month" (the
    # route's default window) and this test is about the attributable
    # filter, not the period default.
    resp = client.get("/transactions?attributable=1&period=all")
    html = resp.get_data(as_text=True)
    assert "-100.00" in html
    assert "-200.00" not in html

    resp = client.get("/transactions?attributable=0&period=all")
    html = resp.get_data(as_text=True)
    assert "-200.00" in html
    assert "-100.00" not in html


def test_transactions_page_shows_filtered_total(client):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO accounts (name, institution, account_type, currency, owner) VALUES (?,?,?,?,?)",
        ("Checking", "Bank", "checking", "USD", "self"),
    )
    acct_id = conn.execute("SELECT id FROM accounts").fetchone()["id"]
    for i, amt in enumerate([-100.0, -50.0, 200.0]):
        h = compute_dedup_hash(acct_id, f"2026-01-0{i+1}", amt, f"txn{i}")
        conn.execute(
            "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, "
            "description, source, dedup_hash) VALUES (?,?,?, 'USD', 'expense', ?, 'manual', ?)",
            (acct_id, f"2026-01-0{i+1}", amt, f"txn{i}", h),
        )
    conn.commit()
    conn.close()

    # period=all, since these fixture dates predate "this month" (the
    # route's default window) and this test is about the filtered-total
    # summary line, not the period default.
    resp = client.get("/transactions?period=all")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Filtered total" in html
    assert "50.00" in html  # sum(-100, -50, 200) == 50.00


def test_add_account_with_starting_balance(client):
    resp = client.post("/api/accounts/add", json={
        "name": "交通银行借记卡", "institution": "交通银行", "account_type": "checking",
        "currency": "CNY", "owner": "self", "starting_balance": "1000", "balance_date": "2026-10-1",
    })
    assert resp.status_code == 200
    conn = db.get_connection()
    acct = conn.execute("SELECT * FROM accounts WHERE name = '交通银行借记卡'").fetchone()
    assert acct["institution"] == "交通银行" and acct["currency"] == "CNY" and acct["account_type"] == "checking"
    snap = conn.execute("SELECT date, balance FROM balances WHERE account_id = ?", (acct["id"],)).fetchone()
    conn.close()
    assert snap["date"] == "2026-10-01" and snap["balance"] == 1000.0  # date padded like elsewhere


def test_add_account_rejects_duplicates_and_bad_input(client):
    ok = {"name": "HYSA", "institution": "Marcus", "account_type": "savings", "currency": "USD"}
    assert client.post("/api/accounts/add", json=ok).status_code == 200
    assert client.post("/api/accounts/add", json=ok).status_code == 400  # already exists
    assert client.post("/api/accounts/add", json={**ok, "name": "X", "account_type": "bogus"}).status_code == 400
    assert client.post("/api/accounts/add", json={**ok, "name": "X", "currency": "XYZ"}).status_code == 400
    assert client.post("/api/accounts/add", json={**ok, "name": "X", "owner": "parents"}).status_code == 400
    assert client.post("/api/accounts/add", json={**ok, "name": "X", "starting_balance": "5"}).status_code == 400  # no date
    assert client.post("/api/accounts/add", json={
        **ok, "name": "Wallet", "account_type": "cash", "starting_balance": "5", "balance_date": "2026-01-01",
    }).status_code == 400  # computed-balance types take no snapshot
    assert client.post("/api/accounts/add", json={"name": "Y"}).status_code == 400


def test_update_cds_edits_fields_and_rejects_bad_values(client):
    for name in ("Marcus CD", "Ally HYSA"):
        client.post("/api/accounts/add", json={"name": name, "institution": "Bank", "account_type": "savings"})
    assert client.post("/api/cds/add", json={
        "account_name": "Marcus CD", "institution": "Marcus", "principal": "5000", "apy": "4.5",
    }).status_code == 200
    conn = db.get_connection()
    cd_id = conn.execute("SELECT id FROM cds").fetchone()["id"]
    conn.close()

    resp = client.post("/api/cds/update", json={"edits": [{
        "id": cd_id, "note": "renews in spring", "principal": "5,250", "apy": "", "maturity_date": "2027-3-1",
        "account_name": "Ally HYSA",
    }]})
    assert resp.status_code == 200 and resp.get_json()["updated"] == 1
    conn = db.get_connection()
    cd = conn.execute("SELECT c.*, a.name AS account_name FROM cds c JOIN accounts a ON a.id = c.account_id").fetchone()
    conn.close()
    assert cd["note"] == "renews in spring" and cd["principal"] == 5250.0 and cd["apy"] is None
    assert cd["maturity_date"] == "2027-03-01" and cd["account_name"] == "Ally HYSA"
    assert cd["institution"] == "Marcus"  # untouched fields stay as they were

    # all or nothing: a bad value anywhere rejects the whole save
    resp = client.post("/api/cds/update", json={"edits": [
        {"id": cd_id, "note": "should not save"}, {"id": cd_id, "principal": "lots"},
    ]})
    assert resp.status_code == 400
    conn = db.get_connection()
    assert conn.execute("SELECT note FROM cds").fetchone()["note"] == "renews in spring"
    conn.close()
    assert client.post("/api/cds/update", json={"edits": [{"id": cd_id, "institution": " "}]}).status_code == 400
    assert client.post("/api/cds/update", json={"edits": [{"id": cd_id, "account_name": "Nope"}]}).status_code == 400

    html = client.get("/net-worth").get_data(as_text=True)
    assert 'data-cd-field="note"' in html and "data-cd-save-btn" in html


def test_set_balance_anchors_an_account_and_replaces_same_date(client):
    client.post("/api/accounts/add", json={"name": "CD", "institution": "Bank", "account_type": "savings"})
    client.post("/api/transactions/save", json={"edits": [], "confirmed_ids": [], "new_rows": [
        {"date": "2026-02-15", "account_name": "CD", "amount": "40", "transaction_type": "income",
         "description": "Interest"},
    ]})
    html = client.get("/net-worth").get_data(as_text=True)
    assert 'title="No recorded balance' in html  # summed, flagged for checking

    assert client.post("/api/accounts/set-balance", json={
        "account_name": "CD", "date": "2026-2-15", "balance": "9,000"}).status_code == 200
    assert client.post("/api/accounts/set-balance", json={
        "account_name": "CD", "date": "2026-02-15", "balance": "10040"}).status_code == 200  # replaces
    conn = db.get_connection()
    rows = conn.execute("SELECT date, balance FROM balances").fetchall()
    conn.close()
    assert [(r["date"], r["balance"]) for r in rows] == [("2026-02-15", 10040.0)]
    html = client.get("/net-worth").get_data(as_text=True)
    assert 'title="No recorded balance' not in html

    client.post("/api/accounts/add", json={"name": "Fidelity", "institution": "F", "account_type": "brokerage"})
    for bad in ({"account_name": "Fidelity", "date": "2026-01-01", "balance": "1"},
                {"account_name": "Nope", "date": "2026-01-01", "balance": "1"},
                {"account_name": "CD", "date": "2026-01-01", "balance": "lots"},
                {"account_name": "CD", "balance": "1"}):
        assert client.post("/api/accounts/set-balance", json=bad).status_code == 400


def test_net_worth_page_splits_tables_by_currency(client):
    for payload in (
        {"name": "BofA Checking", "institution": "Bank of America", "account_type": "checking", "currency": "USD",
         "starting_balance": "500", "balance_date": "2026-01-01"},
        {"name": "交通银行借记卡", "institution": "交通银行", "account_type": "checking", "currency": "CNY",
         "starting_balance": "1000", "balance_date": "2026-01-01"},
    ):
        assert client.post("/api/accounts/add", json=payload).status_code == 200
    html = client.get("/net-worth").get_data(as_text=True)
    assert "USD accounts" in html and "CNY accounts" in html
    assert html.index("BofA Checking") < html.index("CNY accounts") < html.index("交通银行借记卡")
    assert "¥1,000.00" in html and "Total (CNY)" in html
    assert "1 CNY = $0.14" in html  # the rate used is shown
    assert ">USD</th>" not in html  # the converted-USD column is gone
    assert "Net Worth (all accounts, in USD)" in html
    assert "$640.00" in html  # 500 + 1000 × 0.14


def test_spending_and_cash_flow_pages_render_with_pass_through_toggle(client):
    page = client.get("/spending").get_data(as_text=True)
    assert "Include pass-through" in page and "pass_through=include" in page
    page = client.get("/spending?pass_through=include").get_data(as_text=True)
    assert "pass_through=exclude" in page
    page = client.get("/cash-flow").get_data(as_text=True)
    assert "Living Expenses</strong> is everything" in page


def test_investments_page_has_a_section_per_account_and_lists_unmatched(client):
    conn = db.get_connection()
    for name, kind in [("Checking", "checking"), ("Roth IRA", "retirement"), ("Individual", "brokerage")]:
        conn.execute("INSERT INTO accounts (name, institution, account_type, currency) VALUES (?, 'B', ?, 'USD')", (name, kind))
    checking = conn.execute("SELECT id FROM accounts WHERE name = 'Checking'").fetchone()["id"]
    for i, note in enumerate(["roth", None]):
        conn.execute(
            "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, description, note, "
            "source, dedup_hash) VALUES (?, '2026-01-0' || ?, -100, 'USD', 'investment_contribution', 'Fidelity', ?, "
            "'manual', ?)", (checking, i + 1, note, f"inv{i}"),
        )
    conn.commit()
    conn.close()

    page = client.get("/investments").get_data(as_text=True)
    assert '<details class="inv-panel" data-collapse-key="inv-' in page
    assert "<h2>Roth IRA</h2>" in page and "<h2>Individual</h2>" in page
    assert "Investment transactions that need a look" in page and "(1)" in page

    resp = client.post("/api/investments/entry", json={"account_name": "Roth IRA", "date": "2026-2-1",
                                                       "balance": "1,250"})
    assert resp.status_code == 200
    assert db.get_connection().execute("SELECT date FROM balances").fetchone()[0] == "2026-02-01"  # padded
    resp = client.post("/api/investments/entry", json={"account_name": "Roth IRA", "date": "2026-02-01",
                                                       "balance": "abc"})
    assert resp.status_code == 400


def _seed_investment_accounts():
    conn = db.get_connection()
    for name, kind in [("Checking", "checking"), ("Roth IRA", "retirement"), ("Individual", "brokerage")]:
        conn.execute("INSERT INTO accounts (name, institution, account_type, currency) VALUES (?, 'B', ?, 'USD')", (name, kind))
    ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM accounts")}
    conn.commit()
    conn.close()
    return ids


def test_investment_assign_overrides_matching_and_can_exclude(client):
    ids = _seed_investment_accounts()
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, description, category, "
        "source, dedup_hash) VALUES (?, '2026-01-01', -100, 'USD', 'investment_contribution', 'Fidelity', NULL, 'manual', 'a')",
        (ids["Checking"],),
    )
    # Category says investment, Type doesn't — surfaced, never counted
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, description, category, "
        "source, dedup_hash) VALUES (?, '2026-01-02', -50, 'USD', 'expense', 'Robinhood', 'Investment', 'manual', 'b')",
        (ids["Checking"],),
    )
    conn.commit()
    txn = {r["description"]: r["id"] for r in conn.execute("SELECT id, description FROM transactions")}
    conn.close()

    status = lambda: {c["description"]: c for c in aggregates.investment_contributions(db.get_connection())}
    assert status()["Fidelity"]["status"] == "unmatched"
    assert status()["Robinhood"]["status"] == "wrong_type"

    assert client.post("/api/investments/assign", json={"ids": [txn["Fidelity"]], "target": ids["Individual"]}).status_code == 200
    c = status()["Fidelity"]
    assert c["status"] == "matched" and c["account_id"] == ids["Individual"] and c["how"] == "manual"

    assert client.post("/api/investments/assign", json={"ids": [txn["Robinhood"]], "target": "none"}).status_code == 200
    assert status()["Robinhood"]["status"] == "excluded"
    page = client.get("/investments").get_data(as_text=True)
    assert "need a look" not in page  # nothing left to sort out
    assert "marked not an investment — not counted" in page  # still visible in All entries

    assert client.post("/api/investments/assign", json={"ids": [txn["Fidelity"]], "target": "auto"}).status_code == 200
    assert status()["Fidelity"]["status"] == "unmatched"
    # only investment accounts are valid targets
    assert client.post("/api/investments/assign", json={"ids": [txn["Fidelity"]], "target": ids["Checking"]}).status_code == 400


def test_cds_in_an_investment_account_show_and_edit_on_investments(client):
    ids = _seed_investment_accounts()
    client.post("/api/cds/add", json={"account_name": "Roth IRA", "institution": "Marcus", "principal": "5000"})
    page = client.get("/investments").get_data(as_text=True)
    assert "CDs &amp; bonds in Roth IRA" in page and 'value="Marcus"' in page
    cd_id = db.get_connection().execute("SELECT id FROM cds").fetchone()["id"]
    assert client.post("/api/cds/update", json={"edits": [{"id": cd_id, "note": "edited on Investments"}]}).status_code == 200
    assert "edited on Investments" in client.get("/net-worth").get_data(as_text=True)


def test_investment_assign_batch_is_all_or_nothing_and_guesses_follow_the_note(client):
    ids = _seed_investment_accounts()
    conn = db.get_connection()
    for i in range(2):
        conn.execute(
            "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, description, "
            "source, dedup_hash) VALUES (?, '2026-01-01', -100, 'USD', 'investment_contribution', ?, 'manual', ?)",
            (ids["Checking"], f"Fidelity {i}", f"g{i}"),
        )
    conn.commit()
    txn = [r["id"] for r in conn.execute("SELECT id FROM transactions ORDER BY id")]
    conn.close()

    guesses = client.get(f"/api/investments/guesses?ids={txn[0]},{txn[1]}").get_json()
    assert guesses[str(txn[0])]["guess"] is None
    # the user adds "roth" to the note on Transactions; the guess follows
    conn = db.get_connection()
    conn.execute("UPDATE transactions SET note = 'roth' WHERE id = ?", (txn[0],))
    conn.commit()
    conn.close()
    assert client.get(f"/api/investments/guesses?ids={txn[0]}").get_json()[str(txn[0])]["guess"] == ids["Roth IRA"]

    # one bad target → nothing saved
    bad = client.post("/api/investments/assign", json={"assignments": [
        {"id": txn[0], "target": ids["Roth IRA"]}, {"id": txn[1], "target": "bogus"}]})
    assert bad.status_code == 400
    assert db.get_connection().execute("SELECT COUNT(*) FROM transactions WHERE investment_account IS NOT NULL").fetchone()[0] == 0

    ok = client.post("/api/investments/assign", json={"assignments": [
        {"id": txn[0], "target": ids["Roth IRA"]}, {"id": txn[1], "target": "none"}]})
    assert ok.status_code == 200 and ok.get_json()["updated"] == 2


def test_each_investment_account_has_its_own_date_row(client):
    ids = _seed_investment_accounts()
    page = client.get(f"/investments?a{ids['Roth IRA']}_period=1yr").get_data(as_text=True)
    assert f'data-period-param="a{ids["Roth IRA"]}_period"' in page
    assert f'data-period-param="a{ids["Individual"]}_period"' in page


def test_investment_snapshots_edit_and_delete(client):
    ids = _seed_investment_accounts()
    client.post("/api/investments/entry", json={"account_name": "Roth IRA", "date": "2026-01-31", "balance": "1000"})
    client.post("/api/investments/entry", json={"account_name": "Roth IRA", "date": "2026-02-28", "balance": "1100"})
    conn = db.get_connection()
    snaps = [r["id"] for r in conn.execute("SELECT id FROM balances ORDER BY date")]
    # a bank balance must never be editable/deletable from here
    conn.execute("INSERT INTO balances (account_id, date, balance) VALUES (?, '2026-01-31', 5)", (ids["Checking"],))
    bank_id = conn.execute("SELECT id FROM balances WHERE account_id = ?", (ids["Checking"],)).fetchone()["id"]
    conn.commit()
    conn.close()

    page = client.get("/investments").get_data(as_text=True)
    assert f'data-entry-key="snap:{snaps[0]}"' in page and "Delete selected" in page

    ok = client.post("/api/balances/update", json={"edits": [
        {"id": snaps[0], "balance": "1,050", "note": " from statement ", "account_name": "Individual"}]})
    assert ok.status_code == 200
    row = db.get_connection().execute("SELECT * FROM balances WHERE id = ?", (snaps[0],)).fetchone()
    assert row["balance"] == 1050 and row["note"] == "from statement" and row["account_id"] == ids["Individual"]

    # all or nothing: one bad value saves neither edit
    bad = client.post("/api/balances/update", json={"edits": [
        {"id": snaps[1], "balance": "2000"}, {"id": snaps[0], "balance": "abc"}]})
    assert bad.status_code == 400
    assert db.get_connection().execute("SELECT balance FROM balances WHERE id = ?", (snaps[1],)).fetchone()[0] == 1100
    # moving onto a date that account already has → refused
    client.post("/api/balances/update", json={"edits": [{"id": snaps[0], "account_name": "Roth IRA"}]})
    clash = client.post("/api/balances/update", json={"edits": [{"id": snaps[0], "date": "2026-02-28"}]})
    assert clash.status_code == 400
    assert client.post("/api/balances/update", json={"edits": [{"id": bank_id, "balance": "1"}]}).status_code == 400

    assert client.post("/api/balances/delete", json={"ids": [snaps[1], bank_id]}).get_json()["deleted"] == 1
    remaining = {r[0] for r in db.get_connection().execute("SELECT id FROM balances")}
    assert remaining == {snaps[0], bank_id}


def test_investment_entry_takes_value_principal_added_or_both(client):
    ids = _seed_investment_accounts()
    post = lambda **kw: client.post("/api/investments/entry", json={"account_name": "Roth IRA", **kw})

    assert post(date="2026-01-31").status_code == 400  # neither filled in
    assert post(date="2026-01-31", principal_added="abc").status_code == 400
    assert post(date="2026-01-31", principal_added="1,000").status_code == 200  # principal only
    assert post(date="2026-02-28", balance="1,080").status_code == 200  # value only
    assert post(date="2026-03-15", balance="1600", principal_added="500").status_code == 200  # both

    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM balances").fetchone()[0] == 2
    assert [r[0] for r in conn.execute("SELECT amount FROM investment_deposits ORDER BY date")] == [1000, 500]
    row = aggregates.latest_balance(conn, ids["Roth IRA"])
    # principal adds up what went in; the 3/15 value already includes that day's deposit
    assert row["principal_contributed"] == 1500 and row["balance"] == 1600
    conn.close()

    page = client.get("/investments").get_data(as_text=True)
    assert "Principal added</span>" in page and 'data-entry-key="dep:' in page


def test_principal_added_matching_a_logged_transfer_asks_first(client):
    ids = _seed_investment_accounts()
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, description, "
        "source, dedup_hash) VALUES (?, '2026-04-01', -250, 'USD', 'investment_contribution', "
        "'Transfer to Roth IRA', 'manual', 'd1')", (ids["Checking"],),
    )
    conn.commit()
    conn.close()
    body = {"account_name": "Roth IRA", "date": "2026-04-03", "principal_added": "250"}
    resp = client.post("/api/investments/entry", json=body)
    assert resp.status_code == 409 and resp.get_json()["needs_confirm"]
    assert client.post("/api/investments/entry", json={**body, "confirm_duplicate": True}).status_code == 200


def test_principal_added_entries_edit_and_delete(client):
    ids = _seed_investment_accounts()
    client.post("/api/investments/entry", json={"account_name": "Roth IRA", "date": "2026-01-01", "principal_added": "100"})
    dep_id = db.get_connection().execute("SELECT id FROM investment_deposits").fetchone()[0]
    ok = client.post("/api/investments/deposits/update", json={"edits": [
        {"id": dep_id, "amount": "150", "account_name": "Individual", "date": "2026-1-2"}]})
    assert ok.status_code == 200
    row = db.get_connection().execute("SELECT * FROM investment_deposits").fetchone()
    assert row["amount"] == 150 and row["account_id"] == ids["Individual"] and row["date"] == "2026-01-02"
    assert client.post("/api/investments/deposits/update", json={"edits": [{"id": dep_id, "amount": "x"}]}).status_code == 400
    assert client.post("/api/investments/deposits/delete", json={"ids": [dep_id]}).get_json()["deleted"] == 1


def test_investment_entries_carry_an_editable_note(client):
    _seed_investment_accounts()
    client.post("/api/investments/entry", json={"account_name": "Roth IRA", "date": "2026-05-01",
                                                "balance": "900", "principal_added": "100", "note": "May top-up"})
    conn = db.get_connection()
    snap = conn.execute("SELECT id, note FROM balances").fetchone()
    dep = conn.execute("SELECT id, note FROM investment_deposits").fetchone()
    conn.close()
    assert snap["note"] == "May top-up" and dep["note"] == "May top-up"
    assert 'value="May top-up"' in client.get("/investments").get_data(as_text=True)

    client.post("/api/balances/update", json={"edits": [{"id": snap["id"], "note": "statement value"}]})
    client.post("/api/investments/deposits/update", json={"edits": [{"id": dep["id"], "note": ""}]})
    conn = db.get_connection()
    assert conn.execute("SELECT note FROM balances").fetchone()[0] == "statement value"
    assert conn.execute("SELECT note FROM investment_deposits").fetchone()[0] is None
    conn.close()


def test_amounts_show_with_commas_and_save_back_from_them(client):
    conn = db.get_connection()
    conn.execute("INSERT INTO accounts (name, institution, account_type, currency) VALUES ('Checking', 'B', 'checking', 'USD')")
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, description, source, dedup_hash) "
        "VALUES (1, '2026-01-05', -1234.5, 'USD', 'expense', 'Rent', 'manual', 'c1')"
    )
    conn.commit()
    txn_id = conn.execute("SELECT id FROM transactions").fetchone()["id"]
    conn.close()

    page = client.get("/transactions?period=all").get_data(as_text=True)
    assert 'value="-1,234.50"' in page

    resp = client.post("/api/transactions/save", json={"edits": [{"id": txn_id, "amount": "-12,345.67"}]})
    assert resp.status_code == 200
    assert db.get_connection().execute("SELECT amount FROM transactions").fetchone()[0] == -12345.67


def test_cds_and_bonds_table_has_a_type(client):
    _seed_investment_accounts()
    # defaults to CD; a bond goes in the same table
    assert client.post("/api/cds/add", json={"account_name": "Individual", "institution": "Marcus",
                                             "principal": "5,000"}).status_code == 200
    assert client.post("/api/cds/add", json={"account_name": "Individual", "institution": "US Treasury",
                                             "principal": "10,000", "apy": "4.2%", "kind": "Treasury",
                                             "maturity_date": "2027-6-30"}).status_code == 200
    assert client.post("/api/cds/add", json={"account_name": "Individual", "institution": "X",
                                             "principal": "1", "kind": "Crypto"}).status_code == 400

    conn = db.get_connection()
    rows = {r["institution"]: r for r in conn.execute("SELECT * FROM cds")}
    conn.close()
    assert rows["Marcus"]["kind"] == "CD"
    bond = rows["US Treasury"]
    assert bond["kind"] == "Treasury" and bond["apy"] == 4.2 and bond["maturity_date"] == "2027-06-30"

    assert client.post("/api/cds/update", json={"edits": [{"id": bond["id"], "kind": "Corporate bond"}]}).status_code == 200
    assert client.post("/api/cds/update", json={"edits": [{"id": bond["id"], "kind": "nope"}]}).status_code == 400
    page = client.get("/net-worth").get_data(as_text=True)
    assert "CDs &amp; Bonds" in page and '<option value="Corporate bond" selected>' in page
    assert "CDs &amp; bonds in Individual" in client.get("/investments").get_data(as_text=True)


# --- Import page ---

STATEMENT = b"Date,Description,Amount\n2026-01-05,COFFEE,-4.50\n2026-01-06,PAYCHECK,2000.00\n"


def _upload(client, url, files, **form):
    from io import BytesIO
    data = dict(form)
    data["files"] = [(BytesIO(content), name) for name, content in files]
    return client.post(url, data=data, content_type="multipart/form-data")


def _make_checking(client):
    assert client.post("/api/accounts/add", json={
        "name": "My Checking", "institution": "Bank", "account_type": "checking"}).status_code == 200
    conn = db.get_connection()
    acct_id = conn.execute("SELECT id FROM accounts WHERE name = 'My Checking'").fetchone()["id"]
    conn.close()
    return acct_id


def test_adding_an_account_creates_its_statements_folder(client):
    _make_checking(client)
    assert (csv_importer.INCOMING_DIR / "My Checking").is_dir()


def test_upload_preview_and_import_statements(client):
    acct_id = _make_checking(client)
    resp = _upload(client, "/api/import/upload", [("jan export.csv", STATEMENT)], account_id=acct_id)
    assert resp.status_code == 200
    saved = csv_importer.INCOMING_DIR / "My Checking" / "jan_export.csv"
    assert saved.exists()
    page = client.get("/import").get_data(as_text=True)
    assert "jan_export.csv" in page

    preview = client.post("/api/import/preview", json={}).get_json()
    assert preview["inserted"] == 2 and preview["errors"] == []
    assert preview["files"][0]["sample"][0] == {"date": "2026-01-05", "description": "COFFEE", "amount": -4.5,
                                                "transaction_type": "expense"}
    assert saved.exists()
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"] == 0
    conn.close()

    result = client.post("/api/import/run", json={"signs": {"My Checking/jan_export.csv": "auto"}}).get_json()
    assert result["inserted"] == 2
    assert not saved.exists()  # deleted once imported
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"] == 2
    conn.close()


def test_upload_rejects_a_missing_account_or_a_non_csv(client):
    acct_id = _make_checking(client)
    assert _upload(client, "/api/import/upload", [("a.csv", STATEMENT)], account_id=999).status_code == 400
    assert _upload(client, "/api/import/upload", [("a.pdf", b"%PDF")], account_id=acct_id).status_code == 400
    assert _upload(client, "/api/import/upload", [], account_id=acct_id).status_code == 400


def test_uploading_the_same_name_twice_keeps_both(client):
    acct_id = _make_checking(client)
    _upload(client, "/api/import/upload", [("a.csv", STATEMENT)], account_id=acct_id)
    _upload(client, "/api/import/upload", [("a.csv", STATEMENT)], account_id=acct_id)
    names = sorted(p.name for p in (csv_importer.INCOMING_DIR / "My Checking").iterdir())
    assert names == ["a (2).csv", "a.csv"]


def test_discard_removes_one_file_and_refuses_paths_outside(client, tmp_path):
    acct_id = _make_checking(client)
    _upload(client, "/api/import/upload", [("a.csv", STATEMENT)], account_id=acct_id)
    outside = tmp_path / "secret.txt"
    outside.write_text("keep me")
    assert client.post("/api/import/discard", json={"kind": "statement", "name": "../secret.txt"}).status_code == 400
    assert outside.exists()
    assert client.post("/api/import/discard", json={"kind": "statement", "name": "My Checking/a.csv"}).status_code == 200
    assert not (csv_importer.INCOMING_DIR / "My Checking" / "a.csv").exists()


def test_history_upload_preview_and_import(client):
    from pathlib import Path
    examples = Path(__file__).resolve().parent.parent / "static" / "examples"
    files = [(name, (examples / name).read_bytes())
             for name in ("history_accounts.csv", "history_transactions.csv", "history_balances.csv")]
    assert _upload(client, "/api/import/history/upload", files).status_code == 200
    assert "history_transactions.csv" in client.get("/import").get_data(as_text=True)

    preview = client.post("/api/import/history/preview", json={}).get_json()
    assert preview["accounts_created"] == 4 and preview["inserted"] == 8 and preview["errors"] == []
    assert ["Groceries", 1] in preview["categories"]

    result = client.post("/api/import/history/run", json={}).get_json()
    assert result["inserted"] == 8
    assert list(history_importer.HISTORY_DIR.iterdir()) == []  # deleted after a clean import
    assert (csv_importer.INCOMING_DIR / "Family Card").is_dir()  # folders for the new accounts


def test_history_upload_rejects_other_file_types(client):
    assert _upload(client, "/api/import/history/upload", [("notes.txt", b"hi")]).status_code == 400


# --- First run, accounts someone else pays, demo data ---

def test_overview_shows_getting_started_until_there_is_data(client):
    assert "Getting started" in client.get("/").get_data(as_text=True)
    _seed_iou_data()
    assert "Getting started" not in client.get("/").get_data(as_text=True)


def test_add_account_someone_else_pays_needs_their_name(client):
    payload = {"name": "Family Card", "institution": "Bank", "account_type": "credit_card", "owner": "other"}
    assert client.post("/api/accounts/add", json=payload).status_code == 400
    assert client.post("/api/accounts/add", json={**payload, "iou_person": "Mom"}).status_code == 200
    html = client.get("/net-worth").get_data(as_text=True)
    assert "Paid by someone else" in html and "/ious?person=Mom" in html


def test_every_page_renders_with_the_demo_data(client):
    from src.seed_demo import seed
    conn = db.get_connection()
    seed(conn)
    conn.close()
    for url in ("/", "/net-worth", "/cash-flow", "/spending", "/transactions?period=all", "/import",
                "/investments", "/ious", "/ious?person=Alex", "/help"):
        assert client.get(url).status_code == 200, url
    conn = db.get_connection()
    ledgers = {l["person"]: l["balance"] for l in aggregates.iou_ledgers(conn)}
    conn.close()
    assert ledgers["Mom"] > 0  # Mom's card spending outweighs the repayments
    assert ledgers["Alex"] == -125.0  # this month's internet half + the concert tickets
