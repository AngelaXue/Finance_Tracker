"""Bank of America CSV statement parsers — two layouts, one per account
kind. csv_importer.py recognizes them by their header rows.
"""

import csv
from pathlib import Path

from src.parsers.base import (
    BaseParser, ParserError, apply_category_rules_to_row, default_card_method, find_header_line,
    money_value, parse_amount, parse_date, read_lines,
)

DATE_FORMAT = "%m/%d/%Y"


class BofACheckingSavingsParser(BaseParser):
    """BofA Checking/Savings CSV export: a few metadata lines, then a
    header row 'Date,Description,Amount,Running Bal.'. Standard schema
    sign convention (negative = outflow, positive = inflow) — no
    negation needed."""

    name = "Bank of America checking/savings"
    institution = "Bank of America"
    REQUIRED_HEADERS = ["Date", "Description", "Amount", "Running Bal."]

    def parse(self, filepath: Path) -> list[dict]:
        lines = read_lines(filepath)
        header_idx = find_header_line(lines, self.REQUIRED_HEADERS)
        if header_idx is None:
            raise ParserError(filepath.name, 0, "couldn't find the Date/Description/Amount/Running Bal. header row")

        reader = csv.DictReader(lines[header_idx:])
        rows = []
        for i, raw in enumerate(reader, start=header_idx + 2):
            date_raw = (raw.get("Date") or "").strip()
            if not date_raw:
                continue
            description = (raw.get("Description") or "").strip()
            # BofA includes a "Beginning balance as of MM/DD/YYYY" anchor
            # row (Date populated, Amount blank) giving the opening balance
            # for the exported range — it's not a real transaction, so skip
            # it rather than failing loudly on what would otherwise look
            # like a malformed amount.
            if description.lower().startswith("beginning balance"):
                continue
            date_iso = parse_date(date_raw, DATE_FORMAT, filepath.name, i)
            amount = parse_amount(raw.get("Amount"), filepath.name, i, date_iso, description)

            try:
                statement_balance = money_value(raw.get("Running Bal."))
            except ValueError:
                statement_balance = None  # optional field — falls back to a computed running balance

            transaction_type = "income" if amount >= 0 else "expense"
            category, transaction_type, category_predicted = apply_category_rules_to_row(description, transaction_type)

            rows.append(dict(
                date=date_iso, amount=amount, currency="USD", description=description,
                transaction_type=transaction_type, category=category, category_predicted=category_predicted,
                source="csv_parsed", statement_balance=statement_balance,
            ))
        return rows


class BofACreditCardParser(BaseParser):
    """BofA Credit Card CSV export: header row 'Posted Date,Reference
    Number,Payee,Address,Amount'. Reference Number and Address are
    ignored. Sign convention (confirmed): negative = purchase, positive =
    payment/credit — already matches this schema's negative-is-outflow
    convention, no negation needed. `method` defaults to 'Card' for every
    row (see default_card_method in base.py), overridden to
    Venmo/Zelle/PayPal if the Payee names one of those rails instead."""

    name = "Bank of America credit card"
    institution = "Bank of America"
    REQUIRED_HEADERS = ["Posted Date", "Payee", "Amount"]

    def parse(self, filepath: Path) -> list[dict]:
        lines = read_lines(filepath)
        header_idx = find_header_line(lines, self.REQUIRED_HEADERS)
        if header_idx is None:
            raise ParserError(filepath.name, 0, "couldn't find the Posted Date/Payee/Amount header row")

        reader = csv.DictReader(lines[header_idx:])
        rows = []
        for i, raw in enumerate(reader, start=header_idx + 2):
            date_raw = (raw.get("Posted Date") or "").strip()
            if not date_raw:
                continue
            date_iso = parse_date(date_raw, DATE_FORMAT, filepath.name, i)
            description = (raw.get("Payee") or "").strip()
            amount = parse_amount(raw.get("Amount"), filepath.name, i, date_iso, description)

            transaction_type = "expense" if amount < 0 else "income"
            category, transaction_type, category_predicted = apply_category_rules_to_row(description, transaction_type)

            rows.append(dict(
                date=date_iso, amount=amount, currency="USD", description=description,
                transaction_type=transaction_type, category=category, category_predicted=category_predicted,
                source="csv_parsed", method=default_card_method(description),
            ))
        return rows
