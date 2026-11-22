"""Ongoing pipeline: imports downloaded bank CSV statements.

Where files go: incoming_statements/ has one folder per account, named
exactly like the account (created automatically for every account). Put
each downloaded CSV in its account's folder — the folder says which
account it belongs to, so file names don't matter. (The Import page's
upload does the same thing.)

How a file is read: its header row picks the layout —
    1. a dedicated bank adapter (KNOWN_PARSERS: Bank of America, Chase),
    2. a user-defined format from config/csv_formats.json,
    3. otherwise auto-detection of common column names (generic.py).

Signs: this app stores money out as negative. Dedicated adapters and
custom formats know their bank's convention. An auto-detected file with
a single Amount column on a credit card or loan account is ambiguous —
many card exports list purchases as positive — so with sign="auto" it's
flipped when most of its amounts are positive (cards are mostly
purchases). The preview says when that happened; pass sign="asis" or
"flip" to override.

    python -m src.parsers.csv_importer --audit
        A real dry run: every insert happens inside a transaction that's
        rolled back, so the counts match a real import exactly. Prints
        counts and errors only, never amounts.

    python -m src.parsers.csv_importer --import
        The real import. Each file that imports cleanly is then deleted
        (statements are a privacy liability once they're in the database);
        --keep-files leaves them. Afterwards, uncategorized rows get
        category suggestions (flagged for review).

Every row is always inserted — never silently skipped on a duplicate
match. A same account/date/amount/description match (exact dedup_hash
collision) or a same account/date/amount match with a different
description (src/dedup.py's find_possible_duplicate) both get
flagged_duplicate=1 instead: there is no way to tell, from the data
alone, whether that's the same statement being re-imported or two
genuinely different transactions that happen to share every one of
those fields (e.g. two bus rides the same day, same fare, same
description) — silently skipping risked losing the second real one,
so both are kept and left for the user to review (highlighted on the
Transactions page) and delete by hand if it really is a duplicate.

This never creates an account: a folder that doesn't match an account
fails loudly, rather than guessing at a new one.
"""

import argparse
import re
from pathlib import Path

from src import categorize, paths
from src.db import get_connection, init_db
from src.dedup import find_possible_duplicate, unique_dedup_hash
from src.parsers.base import ParserError, default_card_method, read_lines
from src.parsers.bofa import BofACheckingSavingsParser, BofACreditCardParser
from src.parsers.chase import ChaseCheckingSavingsParser, ChaseCreditCardParser
from src.parsers.generic import GenericCSVParser, custom_format_detects, load_custom_formats

INCOMING_DIR = paths.INCOMING_DIR
CSV_FORMATS_PATH = paths.CSV_FORMATS_PATH

DUPLICATE_MARKER = "[flagged duplicate — same account/date/amount as an existing transaction, verify before keeping]"

# Dedicated adapters, checked first (most specific header first)
KNOWN_PARSERS = [ChaseCreditCardParser, ChaseCheckingSavingsParser, BofACheckingSavingsParser, BofACreditCardParser]

SIGN_MODES = ("auto", "asis", "flip")
STATEMENT_SUFFIXES = (".csv",)
LIABILITY_TYPES = ("credit_card", "loan")


def account_folder_name(account_name: str) -> str:
    """The folder for an account: its name, minus characters a file system
    won't accept in a folder name."""
    return re.sub(r'[\\/:*?"<>|]', "-", account_name).strip().rstrip(".") or "account"


def ensure_account_folders(conn) -> dict:
    """Creates incoming_statements/<account>/ for every active account
    (never removes anything). Returns {folder name: account row}."""
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    folders = {}
    for acct in conn.execute(
        "SELECT id, name, account_type, currency FROM accounts WHERE is_active = 1 ORDER BY name"
    ).fetchall():
        folder = account_folder_name(acct["name"])
        (INCOMING_DIR / folder).mkdir(exist_ok=True)
        folders[folder] = acct
    return folders


def pending_files(conn) -> list[dict]:
    """Every statement waiting in incoming_statements/, oldest name first:
    {path, name, account (row or None), error}."""
    folders = ensure_account_folders(conn)
    found = []
    for path in sorted(INCOMING_DIR.rglob("*")):
        if not path.is_file() or path.name.startswith(".") or path.suffix.lower() not in STATEMENT_SUFFIXES:
            continue
        rel = path.relative_to(INCOMING_DIR)
        entry = dict(path=path, name=str(rel), account=None, error=None)
        if len(rel.parts) != 2:
            entry["error"] = ("not inside an account's folder — move it into the folder named after "
                              "the account it belongs to")
        elif rel.parts[0] not in folders:
            entry["error"] = f"no account named '{rel.parts[0]}' — create it first, or move the file"
        else:
            entry["account"] = folders[rel.parts[0]]
        found.append(entry)
    return found


def detect_parser(lines: list[str], filename: str):
    for parser_cls in KNOWN_PARSERS:
        if parser_cls.detect(lines):
            return parser_cls()
    for custom in load_custom_formats(CSV_FORMATS_PATH):
        if custom_format_detects(custom, lines):
            return custom
    generic = GenericCSVParser.from_lines(lines)
    if generic:
        return generic
    raise ParserError(
        filename, 0,
        "couldn't recognize this layout — no row has a date column plus an amount (or debit/credit) column. "
        "Describe it in config/csv_formats.json (see the README)",
    )


def parse_statement(path: Path, account, sign: str = "auto") -> dict:
    """Parses one file for one account. Returns {format, rows, flipped,
    sign_note}; raises ParserError. Rows get the account's currency, and
    on a credit card a missing method defaults to Card."""
    if sign not in SIGN_MODES:
        raise ValueError(f"sign must be one of {SIGN_MODES}")
    parser = detect_parser(read_lines(path), path.name)
    rows = parser.parse(path)

    flipped, sign_note = False, None
    if sign == "flip":
        flipped, sign_note = True, "signs flipped (you chose Flip)"
    elif sign == "auto" and isinstance(parser, GenericCSVParser) and parser.auto and parser.has_single_amount \
            and account["account_type"] in LIABILITY_TYPES:
        positives = sum(1 for r in rows if r["amount"] > 0)
        if positives > len(rows) - positives:
            flipped = True
            sign_note = ("signs flipped automatically — most amounts were positive, and on a card that usually "
                         "means purchases are listed as positive. Choose 'As in file' if that's wrong.")
    if flipped:
        for r in rows:
            r["amount"] = -r["amount"]
            # The type was guessed from the sign; re-guess unless a rule set it
            if r["transaction_type"] in ("income", "expense"):
                r["transaction_type"] = "income" if r["amount"] >= 0 else "expense"

    for r in rows:
        r["currency"] = account["currency"]
        if account["account_type"] == "credit_card" and not r.get("method"):
            r["method"] = default_card_method(r["description"])
    return dict(format=parser.name, rows=rows, flipped=flipped, sign_note=sign_note)


def _insert_rows(conn, account_id, rows) -> int:
    """Inserts every row (see the module docstring on duplicates); returns
    how many were flagged as possible duplicates."""
    flagged_count = 0
    for row in rows:
        dedup_hash, exact_collision = unique_dedup_hash(conn, account_id, row["date"], row["amount"], row["description"])
        possible_dup = find_possible_duplicate(conn, account_id, row["date"], row["amount"]) is not None
        flagged = exact_collision or possible_dup
        note = row.get("note")
        if flagged:
            flagged_count += 1
            note = f"{note} {DUPLICATE_MARKER}" if note else DUPLICATE_MARKER
        conn.execute(
            "INSERT INTO transactions (account_id, date, amount, currency, category, category_predicted, "
            "transaction_type, description, note, method, source, dedup_hash, statement_balance, "
            "flagged_duplicate) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                account_id, row["date"], row["amount"], row["currency"], row.get("category"),
                int(bool(row.get("category_predicted"))), row["transaction_type"], row["description"], note,
                row.get("method"), row["source"], dedup_hash, row.get("statement_balance"), int(flagged),
            ),
        )
    return flagged_count


def run(apply: bool, signs: dict | None = None, keep_files: bool = False, sample_size: int = 0) -> dict:
    """Imports (apply=True) or previews (apply=False) everything pending.
    `signs` maps a file's name (as listed) to a SIGN_MODES value; anything
    not listed is "auto". Returns {files: [...], inserted, flagged,
    errors, suggested}. Each file result: name, account, format, rows,
    flagged, first/last date, flipped, sign_note, error, and with
    sample_size the first rows (for the Import page's preview — never
    printed by the CLI)."""
    signs = signs or {}
    init_db()
    conn = get_connection()
    results = []
    try:
        for entry in pending_files(conn):
            result = dict(name=entry["name"], account=entry["account"]["name"] if entry["account"] else None,
                          format=None, rows=0, flagged=0, first_date=None, last_date=None, flipped=False,
                          sign_note=None, sign=signs.get(entry["name"], "auto"), error=entry["error"],
                          sample=[], path=entry["path"])
            results.append(result)
            if entry["error"]:
                continue
            try:
                parsed = parse_statement(entry["path"], entry["account"], result["sign"])
            except (ParserError, ValueError) as exc:
                result["error"] = str(exc)
                continue
            rows = parsed["rows"]
            result.update(format=parsed["format"], rows=len(rows), flipped=parsed["flipped"],
                          sign_note=parsed["sign_note"])
            if rows:
                dates = sorted(r["date"] for r in rows)
                result.update(first_date=dates[0], last_date=dates[-1])
            result["sample"] = [dict(date=r["date"], description=r["description"], amount=r["amount"],
                                     transaction_type=r["transaction_type"]) for r in rows[:sample_size]]
            result["flagged"] = _insert_rows(conn, entry["account"]["id"], rows)

        suggested = 0
        if apply:
            conn.commit()
            # Best effort: a classifier hiccup shouldn't undo a good import
            try:
                suggested = categorize.predict_uncategorized(conn)["flagged"]
            except Exception:  # noqa: BLE001 — reported as 0 suggestions
                suggested = 0
            if not keep_files:
                for r in results:
                    if not r["error"]:
                        r["path"].unlink(missing_ok=True)
        else:
            conn.rollback()
    finally:
        conn.close()

    for r in results:
        r.pop("path")
    ok = [r for r in results if not r["error"]]
    return dict(files=results, inserted=sum(r["rows"] for r in ok), flagged=sum(r["flagged"] for r in ok),
                errors=[f"{r['name']}: {r['error']}" for r in results if r["error"]], suggested=suggested)


def _main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--audit", action="store_true", help="Dry run — parse and report, no database changes")
    group.add_argument("--import", dest="do_import", action="store_true", help="Real import")
    parser.add_argument("--sign", choices=SIGN_MODES, default="auto",
                        help="auto (default), asis (use the file's signs), or flip (negate every amount)")
    parser.add_argument("--keep-files", action="store_true", help="Don't delete files after importing them")
    args = parser.parse_args()

    init_db()
    conn = get_connection()
    names = [e["name"] for e in pending_files(conn)]
    conn.close()
    result = run(apply=args.do_import, signs={n: args.sign for n in names}, keep_files=args.keep_files)

    if not result["files"]:
        print(f"No statements waiting. Put CSV files in the account folders inside {INCOMING_DIR}")
        return
    for f in result["files"]:
        if f["error"]:
            print(f"  ✗ {f['name']}: {f['error']}")
        else:
            span = f" ({f['first_date']} → {f['last_date']})" if f["rows"] else ""
            print(f"  ✓ {f['name']}: {f['rows']} rows, {f['format']}{span}"
                  + (f", {f['flagged']} possible duplicates" if f["flagged"] else ""))
            if f["sign_note"]:
                print(f"      {f['sign_note']}")
    mode = "Would insert" if args.audit else "Inserted"
    print(f"\n{mode}: {result['inserted']}")
    label = "would be flagged" if args.audit else "flagged"
    print(f"Possible duplicates ({label} for review, still inserted): {result['flagged']}")
    if args.do_import:
        print(f"Category suggestions to review: {result['suggested']}")
        if not args.keep_files:
            print("Imported files were deleted; files with errors were left in place.")
    if result["errors"]:
        print(f"\n{len(result['errors'])} file(s) not imported — fix and re-run.")


if __name__ == "__main__":
    _main()
