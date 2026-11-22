import json

import pytest

import src.db as db
from src import categorize
from src.dedup import compute_dedup_hash
from src.parsers import csv_importer
from src.parsers.base import ParserError
from src.parsers.bofa import BofACheckingSavingsParser, BofACreditCardParser
from src.parsers.chase import ChaseCheckingSavingsParser, ChaseCreditCardParser
from src.parsers.generic import GenericCSVParser

CHECKING_CSV = """\
Date,Description,Amount,Running Bal.
01/05/2026,Direct Deposit Payroll,1500.00,2500.00
01/08/2026,Trader Joe's,-42.50,2457.50
"""


@pytest.fixture
def incoming(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_csv_importer.db")
    monkeypatch.setattr(categorize, "MODEL_PATH", tmp_path / "classifier.joblib")
    monkeypatch.setattr(csv_importer, "CSV_FORMATS_PATH", tmp_path / "csv_formats.json")
    incoming = tmp_path / "incoming_statements"
    monkeypatch.setattr(csv_importer, "INCOMING_DIR", incoming)
    db.init_db()
    _add_account("BofA Checking", "checking")
    return incoming


def _add_account(name, account_type, currency="USD"):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO accounts (name, institution, account_type, currency, owner) VALUES (?, 'Bank', ?, ?, 'self')",
        (name, account_type, currency),
    )
    conn.commit()
    conn.close()


def _drop(incoming, account, filename, text):
    folder = incoming / account
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_text(text)
    return folder / filename


def _count():
    conn = db.get_connection()
    n = conn.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
    conn.close()
    return n


def test_folders_are_created_for_every_account(incoming):
    _add_account("Card / Travel", "credit_card")
    conn = db.get_connection()
    folders = csv_importer.ensure_account_folders(conn)
    conn.close()
    assert (incoming / "BofA Checking").is_dir()
    # characters a folder name can't hold are replaced
    assert (incoming / "Card - Travel").is_dir()
    assert folders["Card - Travel"]["name"] == "Card / Travel"


def test_import_inserts_rows_and_deletes_the_file(incoming):
    path = _drop(incoming, "BofA Checking", "anything.csv", CHECKING_CSV)

    result = csv_importer.run(apply=True)
    assert result["inserted"] == 2
    assert result["errors"] == []
    assert result["files"][0]["format"] == BofACheckingSavingsParser.name
    assert _count() == 2
    assert not path.exists()  # statements are deleted once they're in


def test_keep_files_leaves_the_file(incoming):
    path = _drop(incoming, "BofA Checking", "jan.csv", CHECKING_CSV)
    csv_importer.run(apply=True, keep_files=True)
    assert path.exists()


def test_audit_mode_reports_without_writing_or_deleting(incoming):
    path = _drop(incoming, "BofA Checking", "jan.csv", CHECKING_CSV)

    result = csv_importer.run(apply=False, sample_size=5)
    assert result["inserted"] == 2
    assert result["flagged"] == 0
    assert result["files"][0]["first_date"] == "2026-01-05"
    assert [r["amount"] for r in result["files"][0]["sample"]] == [1500.0, -42.5]
    assert _count() == 0  # rolled back, never committed
    assert path.exists()


def test_a_file_outside_any_account_folder_is_not_imported(incoming):
    incoming.mkdir(parents=True, exist_ok=True)
    (incoming / "loose.csv").write_text(CHECKING_CSV)
    result = csv_importer.run(apply=True)
    assert result["inserted"] == 0
    assert "loose.csv" in result["errors"][0] and "folder" in result["errors"][0]
    assert (incoming / "loose.csv").exists()  # left for the user to move


def test_a_folder_with_no_matching_account_reports_an_error(incoming):
    path = _drop(incoming, "Chase Checking", "jan.csv", CHECKING_CSV)  # no such account
    result = csv_importer.run(apply=True)
    assert result["inserted"] == 0
    assert "Chase Checking" in result["errors"][0]
    assert path.exists()


def test_one_bad_file_doesnt_block_the_others(incoming):
    _drop(incoming, "BofA Checking", "good.csv", CHECKING_CSV)
    bad = _drop(incoming, "BofA Checking", "bad.csv", "Date,Description,Amount,Running Bal.\n13/45/2026,X,1.00,1.00\n")
    result = csv_importer.run(apply=True)
    assert result["inserted"] == 2
    assert len(result["errors"]) == 1 and "bad.csv" in result["errors"][0]
    assert bad.exists()  # kept for fixing


def test_rows_take_the_accounts_currency(incoming):
    _add_account("Euro Account", "checking", currency="EUR")
    _drop(incoming, "Euro Account", "export.csv", "Date,Description,Amount\n2026-01-05,Bakery,-4.20\n")
    csv_importer.run(apply=True)
    conn = db.get_connection()
    assert conn.execute("SELECT currency FROM transactions").fetchone()["currency"] == "EUR"
    conn.close()


def test_import_writes_mapped_chase_category_and_flags_for_review(incoming):
    _add_account("Chase Credit Card", "credit_card")
    _drop(incoming, "Chase Credit Card", "Chase1234_Activity.csv",
          "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
          "01/06/2026,01/07/2026,Dunkin,Food & Drink,Sale,-5.50,\n")

    result = csv_importer.run(apply=True)
    assert result["inserted"] == 1

    conn = db.get_connection()
    row = conn.execute("SELECT category, category_predicted, method FROM transactions WHERE description = 'Dunkin'").fetchone()
    conn.close()
    assert row["category"] == "Dining"  # mapped from Chase's "Food & Drink"
    assert row["category_predicted"] == 1  # flagged for review, not silently trusted
    assert row["method"] == "Card"


@pytest.mark.parametrize("header, expected", [
    ("Date,Description,Amount,Running Bal.", BofACheckingSavingsParser),
    ("Posted Date,Reference Number,Payee,Address,Amount", BofACreditCardParser),
    ("Details,Posting Date,Description,Amount,Type,Balance,Check or Slip#", ChaseCheckingSavingsParser),
    ("Transaction Date,Post Date,Description,Category,Type,Amount,Memo", ChaseCreditCardParser),
    ("Date,Description,Amount", GenericCSVParser),
    ("Status,Date,Description,Debit,Credit", GenericCSVParser),
])
def test_detect_parser_picks_the_layout_from_the_header(incoming, header, expected):
    lines = ["Account summary,,\n", header + "\n"]
    assert isinstance(csv_importer.detect_parser(lines, "x.csv"), expected)


def test_detect_parser_fails_loudly_on_an_unrecognized_layout(incoming):
    with pytest.raises(ParserError):
        csv_importer.detect_parser(["Foo,Bar,Baz\n", "1,2,3\n"], "mystery.csv")


CARD_POSITIVE_PURCHASES = """\
Date,Description,Amount
01/03/2026,COFFEE SHOP,4.50
01/04/2026,GROCERY STORE,62.10
01/20/2026,ONLINE PAYMENT - THANK YOU,-66.60
"""


def test_card_file_with_positive_purchases_is_flipped_automatically(incoming):
    _add_account("Other Card", "credit_card")
    _drop(incoming, "Other Card", "activity.csv", CARD_POSITIVE_PURCHASES)
    result = csv_importer.run(apply=True)
    assert result["files"][0]["flipped"] is True
    conn = db.get_connection()
    rows = {r["description"]: r for r in conn.execute("SELECT * FROM transactions").fetchall()}
    conn.close()
    assert rows["COFFEE SHOP"]["amount"] == -4.50
    assert rows["COFFEE SHOP"]["transaction_type"] == "expense"
    assert rows["ONLINE PAYMENT - THANK YOU"]["amount"] == 66.60
    assert rows["ONLINE PAYMENT - THANK YOU"]["transaction_type"] == "internal_transfer"  # the payoff rule


def test_sign_choice_overrides_auto(incoming):
    _add_account("Other Card", "credit_card")
    _drop(incoming, "Other Card", "activity.csv", CARD_POSITIVE_PURCHASES)
    result = csv_importer.run(apply=False, signs={"Other Card/activity.csv": "asis"}, sample_size=1)
    assert result["files"][0]["flipped"] is False
    assert result["files"][0]["sample"][0]["amount"] == 4.50

    _drop(incoming, "BofA Checking", "jan.csv", CHECKING_CSV)
    result = csv_importer.run(apply=False, signs={"BofA Checking/jan.csv": "flip"}, sample_size=1)
    checking = next(f for f in result["files"] if f["account"] == "BofA Checking")
    assert checking["flipped"] is True and checking["sample"][0]["amount"] == -1500.0


def test_checking_files_are_never_flipped_automatically(incoming):
    _drop(incoming, "BofA Checking", "export.csv", "Date,Description,Amount\n2026-01-05,Pay,100\n2026-01-06,Gift,50\n")
    result = csv_importer.run(apply=False)
    assert result["files"][0]["flipped"] is False


def test_custom_format_from_config_is_used(incoming):
    csv_importer.CSV_FORMATS_PATH.write_text(json.dumps([{
        "name": "My Credit Union", "date": "Buchungstag", "description": "Text", "amount": "Betrag",
        "date_format": "%d.%m.%Y", "decimal_comma": True,
    }]))
    _drop(incoming, "BofA Checking", "cu.csv", "Buchungstag;x\nBuchungstag,Text,Betrag\n03.02.2026,Miete,\"-1.250,00\"\n")
    result = csv_importer.run(apply=True)
    assert result["files"][0]["format"] == "My Credit Union"
    conn = db.get_connection()
    row = conn.execute("SELECT date, amount FROM transactions").fetchone()
    conn.close()
    assert (row["date"], row["amount"]) == ("2026-02-03", -1250.0)


def test_a_broken_formats_file_fails_loudly(incoming):
    csv_importer.CSV_FORMATS_PATH.write_text("{not json")
    _drop(incoming, "BofA Checking", "x.csv", "Date,Description,Amount\n2026-01-05,Pay,100\n")
    result = csv_importer.run(apply=False)
    assert result["inserted"] == 0 and "csv_formats.json" in result["errors"][0]


def test_two_identical_transactions_in_the_same_file_are_both_kept(incoming):
    # The bug this whole mechanism fixes: two genuinely different real
    # transactions (e.g. two bus rides the same day, same fare, same
    # description) must never be silently collapsed into one.
    _drop(incoming, "BofA Checking", "rides.csv",
          "Date,Description,Amount,Running Bal.\n"
          "01/05/2026,MTA SUBWAY,-2.75,997.25\n"
          "01/05/2026,MTA SUBWAY,-2.75,994.50\n")

    result = csv_importer.run(apply=True)
    assert result["inserted"] == 2  # both kept, not collapsed into 1
    assert result["flagged"] == 1  # the second one flagged for review

    conn = db.get_connection()
    rows = conn.execute("SELECT flagged_duplicate FROM transactions WHERE description = 'MTA SUBWAY' ORDER BY id").fetchall()
    conn.close()
    assert [r["flagged_duplicate"] for r in rows] == [0, 1]


def test_reimporting_the_same_file_inserts_again_flagged_not_skipped(incoming):
    # The accepted tradeoff: without a way to tell "same statement
    # re-imported" apart from "coincidentally identical real
    # transaction," re-running the same file is no longer silently
    # deduped — it's inserted again and flagged, same as any other
    # duplicate-looking row, left for the user to review/delete by hand.
    _drop(incoming, "BofA Checking", "jan.csv", CHECKING_CSV)
    csv_importer.run(apply=True)
    _drop(incoming, "BofA Checking", "jan.csv", CHECKING_CSV)

    result = csv_importer.run(apply=True)
    assert result["inserted"] == 2
    assert result["flagged"] == 2
    assert _count() == 4  # both copies kept


def test_import_mode_flags_possible_duplicate_different_description(incoming):
    # A row already present (e.g. typed by hand, or from a history import)
    # with a totally different-looking description for the same
    # date/amount — the exact dedup_hash won't catch it;
    # find_possible_duplicate should still flag it via date+amount.
    conn = db.get_connection()
    h = compute_dedup_hash(1, "2026-01-08", -42.50, "Groceries")
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, currency, transaction_type, "
        "description, source, dedup_hash) VALUES (1, '2026-01-08', -42.50, 'USD', 'expense', "
        "'Groceries', 'manual', ?)",
        (h,),
    )
    conn.commit()
    conn.close()

    _drop(incoming, "BofA Checking", "jan.csv", CHECKING_CSV)
    result = csv_importer.run(apply=True)
    assert result["flagged"] == 1  # the "Trader Joe's" CSV row vs "Groceries" existing row

    conn = db.get_connection()
    flagged = conn.execute(
        "SELECT note, flagged_duplicate FROM transactions WHERE description = \"Trader Joe's\""
    ).fetchone()
    conn.close()
    assert flagged["flagged_duplicate"] == 1
    assert "flagged duplicate" in flagged["note"]


def test_import_suggests_categories_afterwards(incoming):
    conn = db.get_connection()
    for i, (desc, cat) in enumerate([("TRADER JOES", "Groceries"), ("TRADER JOES MARKET", "Groceries"),
                                     ("SHELL GAS", "Transportation"), ("SHELL OIL GAS", "Transportation")]):
        conn.execute(
            "INSERT INTO transactions (account_id, date, amount, currency, category, transaction_type, description, "
            "source, dedup_hash) VALUES (1, ?, -10, 'USD', ?, 'expense', ?, 'manual', ?)",
            (f"2025-12-0{i + 1}", cat, desc, compute_dedup_hash(1, f"2025-12-0{i + 1}", -10, desc)),
        )
    conn.commit()
    conn.close()
    _drop(incoming, "BofA Checking", "jan.csv", "Date,Description,Amount\n2026-01-05,TRADER JOES,-30\n")
    result = csv_importer.run(apply=True)
    assert result["suggested"] == 1
