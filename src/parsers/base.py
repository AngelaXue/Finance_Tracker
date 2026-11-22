"""Shared interface all bank-statement parsers implement. A parser turns
one downloaded statement file into a list of dicts matching the
`transactions` schema. See CLAUDE.md "Parser error handling" for the
fail-loudly rule and the log-content rule (metadata only, never amounts).
"""

import re
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from src.categorize import apply_rules


class ParserError(Exception):
    """Raised when a row can't be confidently parsed. Deliberately only
    carries filename/row/reason plus non-monetary identifying fields —
    never `amount` or `balance` — so this exception is always safe to log
    or print in full.
    """

    def __init__(self, filename, row_number, reason, date=None, description=None, category=None):
        self.filename = filename
        self.row_number = row_number
        self.reason = reason
        self.date = date
        self.description = description
        self.category = category
        super().__init__(self._format())

    def _format(self):
        parts = [f"{self.filename} row {self.row_number}: {self.reason}"]
        if self.date:
            parts.append(f"date={self.date}")
        if self.description:
            parts.append(f"description={self.description!r}")
        if self.category:
            parts.append(f"category={self.category!r}")
        return " | ".join(parts)


class BaseParser(ABC):
    """One statement layout. `name` is shown on the Import page;
    REQUIRED_HEADERS is how csv_importer recognizes a file in this layout
    (every one of them in a single header row, case-insensitive)."""

    name: str
    institution: str = ""
    REQUIRED_HEADERS: list[str] = []

    @classmethod
    def detect(cls, lines: list[str]) -> bool:
        return bool(cls.REQUIRED_HEADERS) and find_header_line(lines, cls.REQUIRED_HEADERS) is not None

    @abstractmethod
    def parse(self, filepath: Path) -> list[dict]:
        """Parse a statement file into a list of dicts with keys:
        date, amount, currency, description, transaction_type,
        category (optional), category_predicted (optional, default
        falsy), source, and optionally method, statement_balance, and/or
        note when the source file has a column that maps to one of those.
        `currency` is a placeholder — the importer replaces it with the
        account's own. Must raise ParserError — never silently skip or
        insert malformed data — when a row's format doesn't match what
        this parser expects.
        """
        raise NotImplementedError


def read_lines(filepath: Path) -> list[str]:
    """A statement file's lines. Most banks export UTF-8 (sometimes with a
    byte-order mark); a few still use Windows-1252."""
    try:
        with open(filepath, newline="", encoding="utf-8-sig") as f:
            return f.readlines()
    except UnicodeDecodeError:
        with open(filepath, newline="", encoding="cp1252") as f:
            return f.readlines()


def parse_date(raw: str, fmt: str, filename: str, row_number: int) -> str:
    try:
        return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
    except ValueError:
        raise ParserError(filename, row_number, "unrecognized date format", date=raw.strip())


_CURRENCY_CHARS = re.compile(r"[$€£¥₹\s]")


def money_value(raw, decimal_comma: bool = False) -> float | None:
    """'1,234.50' / '$1,234.50' / '(12.00)' / '-12' → float; blank → None.
    `decimal_comma` reads European style ('1.234,50'). Raises ValueError
    on anything else."""
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    if decimal_comma:
        text = text.replace(".", "").replace(" ", "").replace(",", ".")
    text = _CURRENCY_CHARS.sub("", text).replace(",", "")
    if text.endswith("-"):  # some banks put the sign last: '12.00-'
        text, negative = text[:-1], not negative
    value = float(text)
    return -value if negative else value


def parse_amount(raw, filename: str, row_number: int, date_iso: str, description: str,
                 decimal_comma: bool = False) -> float:
    try:
        value = money_value(raw, decimal_comma)
    except ValueError:
        value = None
    if value is None:
        raise ParserError(filename, row_number, "unrecognized amount format", date=date_iso, description=description)
    return value


def apply_category_rules_to_row(description: str, transaction_type: str) -> tuple[str | None, str, bool]:
    """Shared by every parser: runs categorize.py's CATEGORY_RULES against
    a description, returning (category, transaction_type,
    category_predicted). category_predicted is True only when a rule
    supplied a category (mirrors predict_uncategorized's behavior in
    categorize.py — a rule that only corrects transaction_type, like the
    generic transfer rule, needs no review since there's no category
    guess to confirm; one that also sets category does, same as any
    other auto-filled category)."""
    category = None
    category_predicted = False
    match = apply_rules(description)
    if match:
        rule_category, rule_type = match
        if rule_category is not None:
            category = rule_category
            category_predicted = True
        if rule_type is not None:
            transaction_type = rule_type
    return category, transaction_type, category_predicted


# Banks' own category labels (e.g. Chase's "Food & Drink"), mapped to
# this app's starting set of categories. Used as a fallback suggestion
# only — a description rule wins if it also matches — and always flagged
# for review (category_predicted). Unmapped labels just leave the
# category blank; the raw label is kept in the note either way. Keys are
# lowercase. Extend or rename freely — categories are just labels.
BANK_CATEGORY_MAP = {
    "food & drink": "Dining",
    "restaurants": "Dining",
    "dining": "Dining",
    "groceries": "Groceries",
    "supermarkets": "Groceries",
    "gas": "Transportation",
    "gas stations": "Transportation",
    "automotive": "Transportation",
    "travel": "Travel",
    "bills & utilities": "Utilities",
    "utilities": "Utilities",
    "shopping": "Shopping",
    "merchandise": "Shopping",
    "entertainment": "Entertainment",
    "health & wellness": "Health",
    "health care": "Health",
    "medical": "Health",
    "personal": "Personal",
    "education": "Education",
    "home": "Home",
    "gifts & donations": "Gifts",
    "fees & adjustments": "Fees",
    "professional services": "Services",
}


def map_bank_category(raw: str | None) -> str | None:
    return BANK_CATEGORY_MAP.get((raw or "").strip().lower())


# Credit card statements have no payment-rail column of their own (unlike
# checking, where a check number says "Check"). "Card" is the correct
# default for an ordinary purchase, but the description sometimes names a
# different real rail anyway (e.g. a Venmo/Zelle/PayPal payment funded
# through the card) — checked case-insensitively since bank exports
# aren't consistent about capitalization. First match wins.
CARD_METHOD_KEYWORDS = [("venmo", "Venmo"), ("zelle", "Zelle"), ("paypal", "PayPal")]


def default_card_method(description: str | None) -> str:
    """Every row from a credit card statement gets this as its `method` —
    'Card' unless the description itself names a different rail (see
    CARD_METHOD_KEYWORDS)."""
    lowered = (description or "").lower()
    for keyword, label in CARD_METHOD_KEYWORDS:
        if keyword in lowered:
            return label
    return "Card"


def header_fields(line: str) -> list[str]:
    return [f.strip().strip('"').strip() for f in line.split(",")]


def find_header_line(lines: list[str], expected_headers: list[str]) -> int | None:
    """Returns the index of the first line in `lines` whose comma-split
    fields include every name in `expected_headers` (case-insensitive) —
    used to skip a bank export's leading metadata rows (account summary,
    date range, etc.) and locate the real header row, wherever it lands.
    Returns None if no such line is found."""
    for i, line in enumerate(lines):
        fields = {f.lower() for f in header_fields(line)}
        if all(h.lower() in fields for h in expected_headers):
            return i
    return None
