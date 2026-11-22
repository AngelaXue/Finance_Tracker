"""Chase CSV statement parsers — two layouts, one per account kind.
csv_importer.py recognizes them by their header rows.
"""

import csv
from pathlib import Path

from src.parsers.base import (
    BaseParser, ParserError, apply_category_rules_to_row, default_card_method, find_header_line,
    map_bank_category, money_value, parse_amount, parse_date, read_lines,
)

DATE_FORMAT = "%m/%d/%Y"


class ChaseCheckingSavingsParser(BaseParser):
    """Chase Checking/Savings CSV export: header row 'Details,Posting
    Date,Description,Amount,Type,Balance,Check or Slip#'. Standard schema
    sign convention (negative = outflow, positive = inflow) — no
    negation needed. 'Type' (e.g. MISC_CREDIT, ACCT_XFER, DEPOSIT) isn't
    interpreted structurally — it's stashed in `note` for reference, since
    the description-based 'transfer' rule already covers the transfer
    case more reliably than Chase's own type vocabulary would. A non-blank
    'Check or Slip#' sets method='Check'."""

    name = "Chase checking/savings"
    institution = "Chase"
    REQUIRED_HEADERS = ["Details", "Posting Date", "Description", "Amount"]

    def parse(self, filepath: Path) -> list[dict]:
        lines = read_lines(filepath)
        header_idx = find_header_line(lines, ["Posting Date", "Description", "Amount"])
        if header_idx is None:
            raise ParserError(filepath.name, 0, "couldn't find the Posting Date/Description/Amount header row")

        reader = csv.DictReader(lines[header_idx:])
        rows = []
        for i, raw in enumerate(reader, start=header_idx + 2):
            date_raw = (raw.get("Posting Date") or "").strip()
            if not date_raw:
                continue
            date_iso = parse_date(date_raw, DATE_FORMAT, filepath.name, i)
            description = (raw.get("Description") or "").strip()
            amount = parse_amount(raw.get("Amount"), filepath.name, i, date_iso, description)

            try:
                statement_balance = money_value(raw.get("Balance"))
            except ValueError:
                statement_balance = None

            method = "Check" if (raw.get("Check or Slip#") or "").strip() else None

            chase_type = (raw.get("Type") or "").strip()
            note = f"Chase type: {chase_type}" if chase_type else None

            transaction_type = "income" if amount >= 0 else "expense"
            category, transaction_type, category_predicted = apply_category_rules_to_row(description, transaction_type)

            rows.append(dict(
                date=date_iso, amount=amount, currency="USD", description=description,
                transaction_type=transaction_type, category=category, category_predicted=category_predicted,
                source="csv_parsed", statement_balance=statement_balance, method=method, note=note,
            ))
        return rows


class ChaseCreditCardParser(BaseParser):
    """Chase Credit Card CSV export: header row 'Transaction Date,Post
    Date,Description,Category,Type,Amount,Memo'. Post Date, Type, and
    Memo are ignored — Type is redundant with description (a payoff
    already reads as "Payment Thank You - Web", handled by the
    CATEGORY_RULES entry for that exact phrase) and Memo is always empty.
    Chase's own Category (e.g. "Travel", "Food & Drink") is used as a
    fallback source for this app's `category` via base.BANK_CATEGORY_MAP —
    only when CATEGORY_RULES (description-based) didn't already supply
    one — and always flagged category_predicted so it shows up for
    review like any other auto-filled category, never silently trusted.
    The raw Chase value is kept in `note` regardless, for reference. Sign
    convention (confirmed): negative = purchase/outflow, positive =
    payment/credit — already matches this schema, no negation needed.
    `method` defaults to 'Card' for every row (see default_card_method in
    base.py), overridden to Venmo/Zelle/PayPal if the description names
    one of those rails instead."""

    name = "Chase credit card"
    institution = "Chase"
    REQUIRED_HEADERS = ["Transaction Date", "Post Date", "Description", "Category", "Type", "Amount"]

    def parse(self, filepath: Path) -> list[dict]:
        lines = read_lines(filepath)
        header_idx = find_header_line(lines, ["Transaction Date", "Description", "Amount"])
        if header_idx is None:
            raise ParserError(filepath.name, 0, "couldn't find the Transaction Date/Description/Amount header row")

        reader = csv.DictReader(lines[header_idx:])
        rows = []
        for i, raw in enumerate(reader, start=header_idx + 2):
            date_raw = (raw.get("Transaction Date") or "").strip()
            if not date_raw:
                continue
            date_iso = parse_date(date_raw, DATE_FORMAT, filepath.name, i)
            description = (raw.get("Description") or "").strip()
            amount = parse_amount(raw.get("Amount"), filepath.name, i, date_iso, description)

            chase_category = (raw.get("Category") or "").strip()
            note = f"Chase category: {chase_category}" if chase_category else None

            transaction_type = "expense" if amount < 0 else "income"
            category, transaction_type, category_predicted = apply_category_rules_to_row(description, transaction_type)

            if category is None and chase_category:
                mapped = map_bank_category(chase_category)
                if mapped:
                    category = mapped
                    category_predicted = True

            rows.append(dict(
                date=date_iso, amount=amount, currency="USD", description=description,
                transaction_type=transaction_type, category=category, category_predicted=category_predicted,
                source="csv_parsed", note=note, method=default_card_method(description),
            ))
        return rows
