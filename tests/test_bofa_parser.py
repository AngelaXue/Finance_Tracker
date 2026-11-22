import pytest

from src.parsers.base import ParserError
from src.parsers.bofa import BofACheckingSavingsParser, BofACreditCardParser

CHECKING_CSV = """\
Description,,,
Summary Amt.,,,
Beginning balance as of 01/01/2026,,,"1,000.00"
,,,
Date,Description,Amount,Running Bal.
01/05/2026,Direct Deposit Payroll,"1,500.00","2,500.00"
01/08/2026,Trader Joe's #123,-42.50,"2,457.50"
01/10/2026,Transfer to Savings,-500.00,"1,957.50"
"""

CREDIT_CARD_CSV = """\
Posted Date,Reference Number,Payee,Address,Amount
01/06/2026,123456789,Trader Joe's,Seattle WA,-42.50
01/09/2026,987654321,Payment Thank You - Web,,500.00
01/10/2026,555555555,VENMO PAYMENT,,-20.00
"""

# Real-world shape: a "Beginning balance as of..." anchor row right after
# the header, with a populated Date and no Amount — not a preamble line
# (those are skipped by find_header_line already), a genuine data row
# that isn't a transaction.
CHECKING_CSV_WITH_OPENING_BALANCE_ROW = """\
Date,Description,Amount,Running Bal.
02/02/2026,Beginning balance as of 02/02/2026,,"1,000.00"
02/03/2026,Trader Joe's,-42.50,"957.50"
"""


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content)
    return path


def test_bofa_checking_savings_skips_leading_metadata_and_parses_rows(tmp_path):
    path = _write(tmp_path, "BofA_Checking.csv", CHECKING_CSV)
    rows = BofACheckingSavingsParser().parse(path)

    assert len(rows) == 3
    assert rows[0]["date"] == "2026-01-05"
    assert rows[0]["amount"] == 1500.0
    assert rows[0]["transaction_type"] == "income"
    assert rows[0]["statement_balance"] == 2500.0

    assert rows[1]["description"] == "Trader Joe's #123"
    assert rows[1]["amount"] == -42.5
    assert rows[1]["transaction_type"] == "expense"


def test_bofa_checking_savings_applies_transfer_rule(tmp_path):
    path = _write(tmp_path, "BofA_Saving.csv", CHECKING_CSV)
    rows = BofACheckingSavingsParser().parse(path)

    transfer_row = next(r for r in rows if "Transfer" in r["description"])
    assert transfer_row["transaction_type"] == "internal_transfer"


def test_bofa_checking_savings_skips_beginning_balance_anchor_row(tmp_path):
    path = _write(tmp_path, "BofA_Checking.csv", CHECKING_CSV_WITH_OPENING_BALANCE_ROW)
    rows = BofACheckingSavingsParser().parse(path)

    assert len(rows) == 1
    assert rows[0]["description"] == "Trader Joe's"


def test_bofa_checking_savings_raises_on_missing_header(tmp_path):
    path = _write(tmp_path, "BofA_Checking.csv", "not,a,real,file\n")
    with pytest.raises(ParserError):
        BofACheckingSavingsParser().parse(path)


def test_bofa_credit_card_amount_matches_schema_convention_directly(tmp_path):
    path = _write(tmp_path, "BofA_CC.csv", CREDIT_CARD_CSV)
    rows = BofACreditCardParser().parse(path)

    assert len(rows) == 3
    purchase = rows[0]
    assert purchase["date"] == "2026-01-06"
    assert purchase["description"] == "Trader Joe's"
    assert purchase["amount"] == -42.5  # negative = purchase in the raw CSV, no negation needed
    assert purchase["transaction_type"] == "expense"
    assert purchase["method"] == "Card"  # default for an ordinary purchase


def test_bofa_credit_card_detects_venmo_in_description(tmp_path):
    path = _write(tmp_path, "BofA_CC.csv", CREDIT_CARD_CSV)
    rows = BofACreditCardParser().parse(path)

    venmo_row = next(r for r in rows if "VENMO" in r["description"])
    assert venmo_row["method"] == "Venmo"


def test_bofa_credit_card_payment_thank_you_becomes_internal_transfer(tmp_path):
    path = _write(tmp_path, "BofA_CC.csv", CREDIT_CARD_CSV)
    rows = BofACreditCardParser().parse(path)

    payment = rows[1]
    assert payment["amount"] == 500.0  # positive = payment/credit in the raw CSV
    assert payment["transaction_type"] == "internal_transfer"


def test_bofa_credit_card_raises_on_bad_amount(tmp_path):
    bad_csv = "Posted Date,Reference Number,Payee,Address,Amount\n01/06/2026,1,Somewhere,,not-a-number\n"
    path = _write(tmp_path, "BofA_CC.csv", bad_csv)
    with pytest.raises(ParserError):
        BofACreditCardParser().parse(path)
