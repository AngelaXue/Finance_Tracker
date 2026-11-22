import shutil
from pathlib import Path

import pandas as pd
import pytest

import src.db as db
from src.parsers import history_importer

EXAMPLES = Path(__file__).resolve().parent.parent / "static" / "examples"


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "history.db")
    folder = tmp_path / "import_history"
    folder.mkdir()
    monkeypatch.setattr(history_importer, "HISTORY_DIR", folder)
    db.init_db()
    return folder


def _query(sql):
    conn = db.get_connection()
    rows = conn.execute(sql).fetchall()
    conn.close()
    return rows


def test_the_shipped_templates_import_cleanly(history):
    for name in ("history_accounts.csv", "history_transactions.csv", "history_balances.csv"):
        shutil.copy(EXAMPLES / name, history / name)

    stats = history_importer.run(apply=True)
    assert stats.errors == []
    assert stats.accounts_created == 4
    assert stats.inserted == 8

    family = _query("SELECT owner, iou_person FROM accounts WHERE name = 'Family Card'")[0]
    assert (family["owner"], family["iou_person"]) == ("other", "Mom")
    rows = {r["description"]: r for r in _query("SELECT * FROM transactions")}
    assert rows["Zelle to Mom"]["transaction_type"] == "personal_transfer"
    assert rows["Zelle to Mom"]["source"] == "history_import"
    # blank Type: worked out from the sign, then the description rules
    assert rows["TRADER JOE'S #552"]["transaction_type"] == "expense"
    assert rows["PAYMENT THANK YOU"]["transaction_type"] == "internal_transfer"
    # a card row with no Method defaults to Card
    assert rows["Local Cafe"]["method"] == "Card"
    # given balances + the month's last running balance become recorded balances
    balances = {(r["date"], r["balance"]) for r in _query("SELECT date, balance FROM balances")}
    assert ("2024-01-25", 1488.03) in balances and ("2023-12-31", 8200.0) in balances


def test_dry_run_writes_nothing_and_rerun_is_safe(history):
    shutil.copy(EXAMPLES / "history_accounts.csv", history / "accounts.csv")
    shutil.copy(EXAMPLES / "history_transactions.csv", history / "transactions.csv")

    preview = history_importer.run(apply=False)
    assert preview.inserted == 8 and preview.categories["Groceries"] == 1
    assert _query("SELECT COUNT(*) AS n FROM transactions")[0]["n"] == 0
    assert _query("SELECT COUNT(*) AS n FROM accounts")[0]["n"] == 0

    history_importer.run(apply=True)
    again = history_importer.run(apply=True)
    assert again.inserted == 0 and again.already_present == 8
    assert _query("SELECT COUNT(*) AS n FROM transactions")[0]["n"] == 8


def test_bad_rows_are_skipped_and_reported_without_amounts(history):
    conn = db.get_connection()
    conn.execute("INSERT INTO accounts (name, institution, account_type, currency) VALUES ('Checking', 'B', 'checking', 'USD')")
    conn.commit()
    conn.close()
    (history / "txns.csv").write_text(
        "Date,Account,Amount,Description,Type\n"
        "2024-02-01,Checking,-10.00,ok,\n"
        "2024-02-02,Nope,-11.11,unknown account,\n"
        "2024-02-03,Checking,abc,bad amount,\n"
        "2024-02-04,Checking,-12.34,bad type,spendy\n"
    )
    stats = history_importer.run(apply=True)
    assert stats.inserted == 1
    assert len(stats.errors) == 3
    text = " ".join(str(e) for e in stats.errors)
    assert "Nope" in text and "spendy" in text
    assert "11.11" not in text and "12.34" not in text  # never amounts
    assert (history / "txns.csv").exists()  # nothing deleted when something went wrong


def test_a_workbook_with_named_sheets(history):
    path = history / "my finances.xlsx"
    with pd.ExcelWriter(path) as writer:
        pd.DataFrame({"Name": ["Checking"], "Institution": ["Bank"], "Type": ["checking"]}).to_excel(
            writer, sheet_name="Accounts", index=False)
        pd.DataFrame({"Date": [pd.Timestamp("2024-03-01"), pd.Timestamp("2024-03-02")],
                      "Account": ["Checking", "Checking"], "Amount": [1000.0, -25.5],
                      "Description": ["Pay", "Lunch"], "Category": ["Salary", "Dining"]}).to_excel(
            writer, sheet_name="Transactions", index=False)
    stats = history_importer.run(apply=True, delete_after=True)
    assert stats.errors == []
    assert stats.inserted == 2
    assert not path.exists()  # deleted after a clean import
    lunch = _query("SELECT * FROM transactions WHERE description = 'Lunch'")[0]
    assert (lunch["date"], lunch["amount"], lunch["category"]) == ("2024-03-02", -25.5, "Dining")


def test_missing_required_column_is_reported(history):
    (history / "txns.csv").write_text("Date,Amount\n2024-01-01,5\n")
    stats = history_importer.run(apply=False)
    assert stats.inserted == 0
    assert "Account" in str(stats.errors[0]) or "account" in str(stats.errors[0])
