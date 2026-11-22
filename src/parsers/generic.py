"""A configurable CSV parser for any bank without a dedicated adapter.

Two ways it gets its column mapping:

1. **Auto-detected** (GenericCSVParser.from_lines): the first row whose
   column names include a date column and either an amount column or a
   debit + credit pair (names from the *_COLUMNS lists below,
   case-insensitive). Covers most banks' "Download as CSV".
2. **User-defined** formats in config/csv_formats.json (see
   static/examples/csv_formats.example.json), for layouts the
   auto-detection gets wrong — e.g. day-first dates, decimal commas, or a
   card that exports purchases as positive numbers. Each is recognized by
   its own column names and checked before auto-detection.

Mapping keys: date, description, amount — or debit + credit instead of
amount — plus optional balance (running balance after each row),
category (the bank's own label, used as a suggestion), check_number,
date_format (strptime, e.g. "%d/%m/%Y"; otherwise worked out from the
file), negate (flip every sign), decimal_comma ("1.234,56"), and match
(a list of other column names that must also be present — makes a
format specific to one bank, so it doesn't catch every file that merely
has Date/Description/Amount).
"""

import csv
import json
from datetime import datetime
from pathlib import Path

from src.parsers.base import (
    BaseParser, ParserError, apply_category_rules_to_row, find_header_line, header_fields,
    map_bank_category, money_value, parse_amount, parse_date, read_lines,
)

# Candidate column names, most specific first
DATE_COLUMNS = ["transaction date", "trans. date", "trans date", "date", "posted date", "posting date",
                "post date", "booking date", "value date"]
DESCRIPTION_COLUMNS = ["description", "payee", "merchant", "merchant name", "name", "transaction description",
                       "original description", "narrative", "details", "memo"]
AMOUNT_COLUMNS = ["amount", "transaction amount", "amount (usd)"]
DEBIT_COLUMNS = ["debit", "debits", "withdrawal", "withdrawals", "withdrawal amount", "debit amount",
                 "money out", "paid out", "outflow"]
CREDIT_COLUMNS = ["credit", "credits", "deposit", "deposits", "deposit amount", "credit amount",
                  "money in", "paid in", "inflow"]
BALANCE_COLUMNS = ["running bal.", "running balance", "balance"]
CATEGORY_COLUMNS = ["category"]
CHECK_COLUMNS = ["check number", "check no", "check #", "check or slip#"]

# Tried in order; the first one that reads EVERY date in the file wins, so
# a file with a day above 12 settles month-first vs. day-first by itself.
# A file where both work (every day ≤ 12) reads month-first — set
# date_format in a custom format if your bank is day-first.
DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%d/%m/%y", "%Y/%m/%d", "%d.%m.%Y",
                "%m-%d-%Y", "%d-%m-%Y", "%b %d, %Y", "%d %b %Y", "%B %d, %Y", "%Y%m%d"]

# Summary rows some exports mix in among real transactions (no amount)
SUMMARY_PREFIXES = ("beginning balance", "opening balance", "ending balance", "closing balance",
                    "balance forward")

FORMAT_KEYS = {"name", "date", "description", "amount", "debit", "credit", "balance", "category",
               "check_number", "date_format", "negate", "decimal_comma", "match"}


def pick_date_format(values: list[str]) -> str | None:
    """The first of DATE_FORMATS that parses every (non-blank) value."""
    values = [v.strip() for v in values if v and v.strip()]
    for fmt in DATE_FORMATS:
        try:
            for v in values:
                datetime.strptime(v, fmt)
            return fmt
        except ValueError:
            continue
    return None


def _first(fields_lower: dict, candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate in fields_lower:
            return fields_lower[candidate]
    return None


def detect_mapping(fields: list[str]) -> dict | None:
    """A column mapping for a header row, or None if it doesn't have a
    date column plus an amount column or a debit + credit pair."""
    lower = {}
    for f in fields:
        lower.setdefault(f.lower(), f)
    mapping = {"date": _first(lower, DATE_COLUMNS)}
    if not mapping["date"]:
        return None
    rest = {k: v for k, v in lower.items() if v != mapping["date"]}
    mapping["amount"] = _first(rest, AMOUNT_COLUMNS)
    if not mapping["amount"]:
        mapping["debit"], mapping["credit"] = _first(rest, DEBIT_COLUMNS), _first(rest, CREDIT_COLUMNS)
        if not (mapping["debit"] and mapping["credit"]):
            return None
    mapping["description"] = _first(rest, DESCRIPTION_COLUMNS)
    mapping["balance"] = _first(rest, BALANCE_COLUMNS)
    mapping["category"] = _first(rest, CATEGORY_COLUMNS)
    mapping["check_number"] = _first(rest, CHECK_COLUMNS)
    return {k: v for k, v in mapping.items() if v}


class GenericCSVParser(BaseParser):
    institution = ""

    def __init__(self, mapping: dict, name: str = "Generic CSV (auto-detected)", auto: bool = True):
        self.mapping = mapping
        self.name = name
        # auto=True: the mapping was guessed from column names, so the sign
        # convention is unknown too (see csv_importer's sign handling)
        self.auto = auto

    @property
    def has_single_amount(self) -> bool:
        return "amount" in self.mapping

    def _columns(self) -> list[str]:
        cols = [self.mapping["date"]]
        cols += [self.mapping[k] for k in ("amount", "debit", "credit") if self.mapping.get(k)]
        return cols

    def parse(self, filepath: Path) -> list[dict]:
        lines = read_lines(filepath)
        header_idx = find_header_line(lines, self._columns())
        if header_idx is None:
            raise ParserError(filepath.name, 0, f"couldn't find a header row with {', '.join(self._columns())}")

        m = self.mapping
        decimal_comma = bool(m.get("decimal_comma"))
        raw_rows = [(i, raw) for i, raw in enumerate(csv.DictReader(lines[header_idx:]), start=header_idx + 2)
                    if (raw.get(m["date"]) or "").strip()]
        date_format = m.get("date_format") or pick_date_format([raw.get(m["date"]) for _, raw in raw_rows])
        if raw_rows and not date_format:
            first_i, first = raw_rows[0]
            raise ParserError(filepath.name, first_i,
                              "dates aren't in a recognized format — set date_format in config/csv_formats.json",
                              date=(first.get(m["date"]) or "").strip())

        rows = []
        for i, raw in raw_rows:
            date_iso = parse_date(raw[m["date"]], date_format, filepath.name, i)
            description = (raw.get(m.get("description")) or "").strip() if m.get("description") else ""
            if "amount" in m:
                if not (raw.get(m["amount"]) or "").strip() and description.lower().startswith(SUMMARY_PREFIXES):
                    continue
                amount = parse_amount(raw.get(m["amount"]), filepath.name, i, date_iso, description, decimal_comma)
            else:
                try:
                    debit = money_value(raw.get(m["debit"]), decimal_comma)
                    credit = money_value(raw.get(m["credit"]), decimal_comma)
                except ValueError:
                    raise ParserError(filepath.name, i, "unrecognized amount format", date=date_iso,
                                      description=description)
                if debit is None and credit is None:
                    if description.lower().startswith(SUMMARY_PREFIXES):
                        continue
                    raise ParserError(filepath.name, i, "no debit or credit amount", date=date_iso,
                                      description=description)
                # Some banks write debits as negative numbers, some positive
                amount = abs(credit or 0.0) - abs(debit or 0.0)
            if m.get("negate"):
                amount = -amount

            statement_balance = None
            if m.get("balance"):
                try:
                    statement_balance = money_value(raw.get(m["balance"]), decimal_comma)
                except ValueError:
                    statement_balance = None  # optional — falls back to a computed running balance

            transaction_type = "income" if amount >= 0 else "expense"
            category, transaction_type, category_predicted = apply_category_rules_to_row(description, transaction_type)

            note = None
            bank_category = (raw.get(m["category"]) or "").strip() if m.get("category") else ""
            if bank_category:
                note = f"Bank category: {bank_category}"
                if category is None and map_bank_category(bank_category):
                    category, category_predicted = map_bank_category(bank_category), True

            method = "Check" if m.get("check_number") and (raw.get(m["check_number"]) or "").strip() else None

            rows.append(dict(
                date=date_iso, amount=amount, currency="USD", description=description,
                transaction_type=transaction_type, category=category, category_predicted=category_predicted,
                source="csv_parsed", statement_balance=statement_balance, method=method, note=note,
            ))
        return rows

    @classmethod
    def from_lines(cls, lines: list[str]):
        """Auto-detected parser for a file, or None if no row looks like a
        usable header (only the first 30 lines are searched)."""
        for line in lines[:30]:
            mapping = detect_mapping(header_fields(line))
            if mapping:
                return cls(mapping)
        return None


def load_custom_formats(path: Path) -> list[GenericCSVParser]:
    """User-defined layouts from config/csv_formats.json — a JSON list of
    mappings (keys in FORMAT_KEYS). A missing file means none. A broken
    one fails loudly rather than silently falling back to guessing."""
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ParserError(path.name, exc.lineno, f"not valid JSON ({exc.msg})")
    if not isinstance(entries, list):
        raise ParserError(path.name, 0, "should be a JSON list of formats")
    parsers = []
    for n, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or not entry.get("date") or not (
            entry.get("amount") or (entry.get("debit") and entry.get("credit"))
        ):
            raise ParserError(path.name, n, "each format needs date, and amount or debit + credit")
        unknown = set(entry) - FORMAT_KEYS
        if unknown:
            raise ParserError(path.name, n, f"unknown key(s): {', '.join(sorted(unknown))}")
        if not isinstance(entry.get("match", []), list):
            raise ParserError(path.name, n, "match should be a list of column names")
        mapping = {k: v for k, v in entry.items() if k != "name" and v not in (None, "")}
        parsers.append(GenericCSVParser(mapping, name=entry.get("name") or f"Custom format {n}", auto=False))
    return parsers


def custom_format_detects(parser: GenericCSVParser, lines: list[str]) -> bool:
    """A custom format applies when one header row has every column it
    names."""
    named = [parser.mapping[k] for k in ("date", "description", "amount", "debit", "credit", "balance",
                                          "category", "check_number") if parser.mapping.get(k)]
    return find_header_line(lines, named + list(parser.mapping.get("match", []))) is not None
