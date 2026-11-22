from pathlib import Path

import pytest

from src.parsers.base import BaseParser, ParserError, default_card_method


def test_parser_error_message_excludes_amount_and_balance():
    err = ParserError(
        filename="statement.csv",
        row_number=12,
        reason="unrecognized date format",
        date="2026-01-08",
        description="Trader Joe's",
        category="Groceries",
    )
    message = str(err)
    assert "statement.csv row 12" in message
    assert "unrecognized date format" in message
    assert "2026-01-08" in message
    assert "Trader Joe's" in message
    assert "Groceries" in message
    # No monetary fields exist on ParserError at all — nothing to leak.
    assert not hasattr(err, "amount")
    assert not hasattr(err, "balance")


def test_parser_error_works_with_minimal_fields():
    err = ParserError(filename="statement.csv", row_number=3, reason="malformed row")
    assert str(err) == "statement.csv row 3: malformed row"


def test_base_parser_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        BaseParser()


def test_concrete_parser_must_implement_parse():
    class IncompleteParser(BaseParser):
        institution = "Test Bank"

    with pytest.raises(TypeError):
        IncompleteParser()


def test_concrete_parser_implements_interface():
    class DummyParser(BaseParser):
        institution = "Test Bank"

        def parse(self, filepath: Path) -> list[dict]:
            return [{"date": "2026-01-08", "amount": -10.0, "currency": "USD",
                      "description": "Test", "transaction_type": "expense", "source": "csv_parsed"}]

    rows = DummyParser().parse(Path("dummy.csv"))
    assert rows[0]["date"] == "2026-01-08"
    assert rows[0]["transaction_type"] == "expense"


def test_default_card_method_falls_back_to_card():
    assert default_card_method("Trader Joe's #123") == "Card"
    assert default_card_method("") == "Card"
    assert default_card_method(None) == "Card"


def test_default_card_method_detects_venmo_zelle_paypal_case_insensitively():
    assert default_card_method("VENMO PAYMENT") == "Venmo"
    assert default_card_method("Zelle to John") == "Zelle"
    assert default_card_method("PAYPAL *MERCHANT") == "PayPal"
    assert default_card_method("paypal instant transfer") == "PayPal"
