"""Optional one-time import of older history — for anyone who already
kept their finances in a spreadsheet (or can export them from another
app). Not needed to use the app: without it, history simply starts with
the first bank CSV you import.

Put .csv and/or .xlsx files in import_history/ (or upload them on the
Import page), shaped like the templates in static/examples/:

  Transactions (required) — one row per transaction:
      Date, Account, Amount            required. Amount is signed:
                                       negative = money out.
      Description, Category, Type,     optional. Type is one of the
      Method, Note                     transaction types (blank = income
                                       if positive, expense if negative,
                                       then the description rules apply).
      Balance                          optional: the account's balance
                                       after this row, if you tracked it.
                                       The last one each month also
                                       becomes a recorded balance, so Net
                                       Worth history is right.
  Accounts (optional) — creates accounts that don't exist yet:
      Name, Institution, Type          required
      Currency, Owner, Paid By         optional (Paid By: for Owner
                                       "other", who pays it)
  Balances (optional) — known balances on given dates:
      Date, Account, Balance

In a workbook, these are sheets named Transactions (or the first sheet),
Accounts and Balances. As CSV files, a file whose name contains
"accounts" or "balances" is read as that kind; anything else as
transactions.

    python -m src.parsers.history_importer --audit
        Dry run inside a rolled-back transaction: counts, problems, and
        the distinct Category labels with counts (check them for
        near-duplicates like "Groceries" vs "grocery" first — they become
        the categorizer's training data). Never prints amounts.

    python -m src.parsers.history_importer --import

Safe to re-run: a transaction already present (same account, date,
amount and description) is skipped, not duplicated. Unlike the CSV
pipeline, rows with a problem are skipped and reported rather than
failing the whole file, since one bad row in years of history shouldn't
block the rest — fix them and run it again.
"""

import argparse
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from src import fx, paths
from src.db import get_connection, init_db
from src.dedup import compute_dedup_hash
from src.parsers.base import ParserError, apply_category_rules_to_row, default_card_method, money_value
from src.parsers.generic import pick_date_format

HISTORY_DIR = paths.HISTORY_DIR
SUFFIXES = (".csv", ".xlsx")

TRANSACTION_TYPES = ("income", "expense", "internal_transfer", "personal_transfer", "investment_contribution")
TYPE_ALIASES = {"transfer": "internal_transfer", "family_transfer": "personal_transfer",
                "investment": "investment_contribution"}
ACCOUNT_TYPES = ("checking", "savings", "credit_card", "brokerage", "retirement", "loan", "digital_wallet", "cash")
OWNERS = ("self", "joint", "other")


def _key(name) -> str:
    return " ".join(str(name).strip().lower().replace("_", " ").split())


def _text(value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def _normalize_type(raw) -> str | None:
    text = (_text(raw) or "").lower().replace(" ", "_").replace("-", "_")
    if not text:
        return None
    text = TYPE_ALIASES.get(text, text)
    if text not in TRANSACTION_TYPES:
        raise ValueError(f"unknown Type '{raw}'")
    return text


def _frames(path: Path) -> dict:
    """{kind: DataFrame} for one file; kind is transactions/accounts/balances."""
    if path.suffix.lower() == ".csv":
        name = path.stem.lower()
        kind = "accounts" if "account" in name else "balances" if "balance" in name else "transactions"
        return {kind: pd.read_csv(path, dtype=str, keep_default_na=False)}
    sheets = pd.read_excel(path, sheet_name=None)
    by_name = {_key(n): df for n, df in sheets.items()}
    frames = {}
    for kind in ("accounts", "balances"):
        if kind in by_name:
            frames[kind] = by_name[kind]
    frames["transactions"] = by_name.get("transactions", next(iter(sheets.values())))
    return frames


def _columns(df, required, optional, label):
    lookup = {_key(c): c for c in df.columns}
    missing = [c for c in required if c not in lookup]
    if missing:
        raise ParserError(label, 1, f"missing column(s): {', '.join(missing)} — found: {', '.join(map(str, df.columns))}")
    return {c: lookup.get(c) for c in required + optional}


def _dates(series):
    """ISO dates for a column of Excel dates and/or text dates."""
    texts = [v for v in series if isinstance(v, str) and v.strip()]
    fmt = pick_date_format(texts) if texts else None
    out = []
    for v in series:
        if isinstance(v, (pd.Timestamp, datetime, date)) and not pd.isna(v):
            out.append(v.strftime("%Y-%m-%d"))
        elif isinstance(v, str) and v.strip() and fmt:
            out.append(datetime.strptime(v.strip(), fmt).strftime("%Y-%m-%d"))
        else:
            out.append(None)
    return out


class Stats:
    def __init__(self):
        self.accounts_created = 0
        self.inserted = 0
        self.already_present = 0
        self.balances = 0
        self.categories = Counter()
        self.errors: list[ParserError] = []

    def skip(self, label, row, reason, **kwargs):
        self.errors.append(ParserError(label, row, reason, **kwargs))


def _import_accounts(conn, df, label, stats):
    cols = _columns(df, ["name", "institution", "type"], ["currency", "owner", "paid by"], label)
    for i, row in enumerate(df.to_dict("records"), start=2):
        name = _text(row[cols["name"]])
        if not name:
            continue
        if conn.execute("SELECT 1 FROM accounts WHERE name = ?", (name,)).fetchone():
            continue
        account_type = (_text(row[cols["type"]]) or "").lower().replace(" ", "_")
        currency = (_text(row[cols["currency"]]) if cols["currency"] else None) or fx.BASE_CURRENCY
        owner = ((_text(row[cols["owner"]]) if cols["owner"] else None) or "self").lower()
        person = _text(row[cols["paid by"]]) if cols["paid by"] else None
        institution = _text(row[cols["institution"]])
        if account_type not in ACCOUNT_TYPES:
            stats.skip(label, i, f"unknown account Type '{account_type}' (one of {', '.join(ACCOUNT_TYPES)})")
        elif currency.upper() not in fx.RATES:
            stats.skip(label, i, f"currency '{currency}' has no rate in src/fx.py")
        elif owner not in OWNERS:
            stats.skip(label, i, f"Owner must be one of {', '.join(OWNERS)}")
        elif not institution:
            stats.skip(label, i, "missing Institution")
        else:
            conn.execute(
                "INSERT INTO accounts (name, institution, account_type, currency, owner, iou_person) "
                "VALUES (?,?,?,?,?,?)",
                (name, institution, account_type, currency.upper(), owner, person if owner == "other" else None),
            )
            stats.accounts_created += 1


def _accounts_by_name(conn):
    return {r["name"]: r for r in conn.execute("SELECT id, name, account_type, currency FROM accounts").fetchall()}


def _import_transactions(conn, df, label, stats):
    cols = _columns(df, ["date", "account", "amount"],
                    ["description", "category", "type", "method", "note", "balance"], label)
    accounts = _accounts_by_name(conn)
    dates = _dates(df[cols["date"]])
    month_end = {}  # account_id -> {YYYY-MM: (date, balance)}
    for i, (row, date_iso) in enumerate(zip(df.to_dict("records"), dates), start=2):
        def opt(col):
            return _text(row[cols[col]]) if cols[col] else None

        description, category = opt("description"), opt("category")
        if not date_iso:
            stats.skip(label, i, "missing or unrecognized date", description=description)
            continue
        account = accounts.get(_text(row[cols["account"]]) or "")
        if account is None:
            stats.skip(label, i, f"no account named '{_text(row[cols['account']])}' — add it (or an Accounts sheet) first",
                       date=date_iso)
            continue
        try:
            amount = money_value(_text(row[cols["amount"]]))
            transaction_type = _normalize_type(opt("type"))
        except ValueError as exc:
            reason = str(exc) if "Type" in str(exc) else "unrecognized amount"
            stats.skip(label, i, reason, date=date_iso, description=description)
            continue
        if amount is None:
            stats.skip(label, i, "missing amount", date=date_iso, description=description)
            continue

        predicted = False
        if transaction_type is None:
            rule_category, transaction_type, predicted = apply_category_rules_to_row(
                description or "", "income" if amount >= 0 else "expense")
            if category is None:
                category = rule_category
            else:
                predicted = False
        method = opt("method") or (default_card_method(description) if account["account_type"] == "credit_card" else None)
        try:
            statement_balance = money_value(_text(row[cols["balance"]])) if cols["balance"] else None
        except ValueError:
            statement_balance = None

        cursor = conn.execute(
            "INSERT OR IGNORE INTO transactions (account_id, date, amount, currency, category, category_predicted, "
            "transaction_type, description, note, method, source, dedup_hash, statement_balance) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account["id"], date_iso, amount, account["currency"], category, int(predicted), transaction_type,
             description, opt("note"), method, "history_import",
             compute_dedup_hash(account["id"], date_iso, amount, description or ""), statement_balance),
        )
        if cursor.rowcount:
            stats.inserted += 1
            if category and not predicted:
                stats.categories[category] += 1
        else:
            stats.already_present += 1
        if statement_balance is not None:
            months = month_end.setdefault(account["id"], {})
            current = months.get(date_iso[:7])
            if current is None or date_iso >= current[0]:
                months[date_iso[:7]] = (date_iso, statement_balance)

    for account_id, months in month_end.items():
        for date_iso, balance in months.values():
            stats.balances += conn.execute(
                "INSERT OR IGNORE INTO balances (account_id, date, balance) VALUES (?,?,?)",
                (account_id, date_iso, balance),
            ).rowcount


def _import_balances(conn, df, label, stats):
    cols = _columns(df, ["date", "account", "balance"], [], label)
    accounts = _accounts_by_name(conn)
    dates = _dates(df[cols["date"]])
    for i, (row, date_iso) in enumerate(zip(df.to_dict("records"), dates), start=2):
        account = accounts.get(_text(row[cols["account"]]) or "")
        try:
            balance = money_value(_text(row[cols["balance"]]))
        except ValueError:
            balance = None
        if not date_iso or account is None or balance is None:
            stats.skip(label, i, "needs a date, an existing account and a balance", date=date_iso)
            continue
        stats.balances += conn.execute(
            "INSERT OR IGNORE INTO balances (account_id, date, balance) VALUES (?,?,?)",
            (account["id"], date_iso, balance),
        ).rowcount


def history_files(directory: Path | None = None) -> list[Path]:
    directory = directory or HISTORY_DIR
    if not directory.exists():
        return []
    return sorted(p for p in directory.iterdir()
                  if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in SUFFIXES)


def run(apply: bool, files: list[Path] | None = None, delete_after: bool = False) -> Stats:
    """Accounts first, then balances and transactions, across all files.
    apply=False rolls everything back (a true dry run). delete_after
    removes the files after an import with no problems at all."""
    files = history_files() if files is None else files
    init_db()
    conn = get_connection()
    stats = Stats()
    try:
        loaded = []
        for path in files:
            try:
                loaded.append((path, _frames(path)))
            except Exception as exc:  # noqa: BLE001 — unreadable file, reported by name only
                stats.skip(path.name, 0, f"couldn't read the file ({type(exc).__name__})")
        steps = (("accounts", _import_accounts), ("transactions", _import_transactions), ("balances", _import_balances))
        for kind, step in steps:
            for path, frames in loaded:
                if kind in frames:
                    label = f"{path.name} [{kind}]" if path.suffix.lower() == ".xlsx" else path.name
                    try:
                        step(conn, frames[kind], label, stats)
                    except ParserError as exc:
                        stats.errors.append(exc)
        if apply:
            conn.commit()
            if delete_after and not stats.errors:
                for path in files:
                    path.unlink(missing_ok=True)
        else:
            conn.rollback()
    finally:
        conn.close()
    return stats


def _main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--audit", action="store_true", help="Dry run: counts, problems and category labels")
    group.add_argument("--import", dest="do_import", action="store_true", help="Run the real import")
    parser.add_argument("--file", type=Path, action="append", default=None,
                        help="A specific file (repeatable; default: everything in import_history/)")
    args = parser.parse_args()

    files = args.file if args.file else history_files()
    if not files:
        print(f"No .csv/.xlsx files found in {HISTORY_DIR} — put your history files there first.")
        return
    stats = run(apply=args.do_import, files=files)
    verb = "Would" if args.audit else "Did"
    print(f"Files: {', '.join(p.name for p in files)}")
    print(f"{verb} create {stats.accounts_created} account(s)")
    print(f"{verb} insert {stats.inserted} transaction(s); {stats.already_present} already present (skipped)")
    print(f"{verb} record {stats.balances} balance(s)")
    if stats.categories:
        print(f"\n{len(stats.categories)} distinct categories (check for near-duplicates):")
        for label, count in sorted(stats.categories.items(), key=lambda kv: kv[0].lower()):
            print(f"  {label!r}: {count}")
    if stats.errors:
        print(f"\n{len(stats.errors)} row(s) skipped:")
        for err in stats.errors:
            print(f"  {err}")
    if args.do_import:
        print("\nDone. Once it looks right on the Transactions page, delete the files in import_history/.")


if __name__ == "__main__":
    _main()
