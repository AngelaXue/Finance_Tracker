import pytest

from src.parsers.base import ParserError
from src.parsers.chase import ChaseCheckingSavingsParser, ChaseCreditCardParser

CHECKING_CSV = """\
Details,Posting Date,Description,Amount,Type,Balance,Check or Slip#
CREDIT,01/05/2026,Direct Deposit Payroll,1500.00,DEPOSIT,2500.00,
DEBIT,01/08/2026,Trader Joe's,-42.50,MISC_DEBIT,2457.50,
DEBIT,01/09/2026,Check Payment,-100.00,CHECK,2357.50,1042
"""

CREDIT_CARD_CSV = """\
Transaction Date,Post Date,Description,Category,Type,Amount,Memo
01/06/2026,01/07/2026,Trader Joe's,Groceries,Sale,-42.50,
01/09/2026,01/10/2026,Payment Thank You - Web,,Payment,500.00,
01/11/2026,01/12/2026,ZELLE TRANSFER,,Sale,-30.00,
"""


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content)
    return path


def test_chase_checking_savings_parses_rows(tmp_path):
    path = _write(tmp_path, "Chase_Checking.csv", CHECKING_CSV)
    rows = ChaseCheckingSavingsParser().parse(path)

    assert len(rows) == 3
    assert rows[0]["date"] == "2026-01-05"
    assert rows[0]["amount"] == 1500.0
    assert rows[0]["transaction_type"] == "income"
    assert rows[0]["statement_balance"] == 2500.0
    assert rows[0]["note"] == "Chase type: DEPOSIT"
    assert rows[0]["method"] is None


def test_chase_checking_savings_check_number_sets_method(tmp_path):
    path = _write(tmp_path, "Chase_Checking.csv", CHECKING_CSV)
    rows = ChaseCheckingSavingsParser().parse(path)

    check_row = next(r for r in rows if r["description"] == "Check Payment")
    assert check_row["method"] == "Check"


def test_chase_checking_savings_raises_on_missing_header(tmp_path):
    path = _write(tmp_path, "Chase_Saving.csv", "not,a,real,file\n")
    with pytest.raises(ParserError):
        ChaseCheckingSavingsParser().parse(path)


def test_chase_credit_card_parses_rows_without_negation(tmp_path):
    path = _write(tmp_path, "Chase_CC.csv", CREDIT_CARD_CSV)
    rows = ChaseCreditCardParser().parse(path)

    assert len(rows) == 3
    purchase = rows[0]
    assert purchase["date"] == "2026-01-06"
    assert purchase["amount"] == -42.5  # no negation — assumed already schema-convention
    assert purchase["transaction_type"] == "expense"
    assert purchase["note"] == "Chase category: Groceries"
    assert purchase["method"] == "Card"  # default for an ordinary purchase


def test_chase_credit_card_detects_zelle_in_description(tmp_path):
    path = _write(tmp_path, "Chase_CC.csv", CREDIT_CARD_CSV)
    rows = ChaseCreditCardParser().parse(path)

    zelle_row = next(r for r in rows if "ZELLE" in r["description"])
    assert zelle_row["method"] == "Zelle"


def test_chase_credit_card_payment_thank_you_becomes_internal_transfer(tmp_path):
    path = _write(tmp_path, "Chase_CC.csv", CREDIT_CARD_CSV)
    rows = ChaseCreditCardParser().parse(path)

    payment = rows[1]
    assert payment["amount"] == 500.0
    assert payment["transaction_type"] == "internal_transfer"
    assert payment["note"] is None  # no Category value on the payment row


def test_chase_credit_card_raises_on_bad_date(tmp_path):
    bad_csv = "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\nnot-a-date,01/07/2026,X,,Sale,-1.00,\n"
    path = _write(tmp_path, "Chase_CC.csv", bad_csv)
    with pytest.raises(ParserError):
        ChaseCreditCardParser().parse(path)
