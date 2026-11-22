"""One-off maintenance operations on real data — run manually via the CLI
below, never invoked automatically by the app. Every operation defaults to
a dry run that prints only a matched row *count*, never transaction
content (descriptions/amounts/balances) — pass --apply to actually commit.

    python -m src.maintenance --help
"""

import argparse

from src import db, fx

ACCOUNT_TYPES = ("checking", "savings", "credit_card", "brokerage", "retirement", "loan", "digital_wallet", "cash")
# 'other' = someone else settles the account (kept out of net worth; its
# spending feeds that person's IOU — see aggregates.iou_status)
OWNERS = ("self", "joint", "other")


def list_balance_dates(conn, start=None, end=None):
    """Read-only diagnostic — never writes anything, no --apply concept.
    Returns {account_name: [dates]}, dates only, NEVER balance values,
    for every self/joint-owned account with any balances rows in
    [start, end] (unbounded if omitted).

    Useful for a Net Worth trend spike/dip that looks unexplained from
    the Transactions page alone: net_worth_trend() (aggregates.py) plots
    a point at every date ANY account has a balance snapshot, using each
    OTHER account's most recent snapshot as of that date. If two
    accounts don't snapshot on the same day, a transfer between them can
    get counted on one side while the other still shows a stale,
    pre-transfer balance — for that one date, the same money can
    momentarily look like it's in both places. Comparing the printed
    dates across accounts (e.g. one account snapshots near month-end,
    another has an odd date in between) is usually enough to confirm
    this without needing to look at any actual balance value."""
    accounts = conn.execute(
        "SELECT id, name FROM accounts WHERE owner IN ('self', 'joint') ORDER BY name"
    ).fetchall()
    result = {}
    for acct in accounts:
        query = "SELECT date FROM balances WHERE account_id = ?"
        params = [acct["id"]]
        if start:
            query += " AND date >= ?"
            params.append(start)
        if end:
            query += " AND date <= ?"
            params.append(end)
        query += " ORDER BY date"
        dates = [r["date"] for r in conn.execute(query, params).fetchall()]
        if dates:
            result[acct["name"]] = dates
    return result


def create_account(conn, name, institution=None, account_type=None, currency=None, owner="self",
                   iou_person=None, apply=False):
    """Creates `name` in the accounts table if it doesn't already exist —
    shared by the Net Worth page's Add Account form and the CLI below, so
    both validate the same way. `currency` defaults to fx.BASE_CURRENCY;
    owner 'other' needs `iou_person` (who settles it).

    csv_importer.py deliberately never creates accounts on its own; this
    is the explicit, reviewable way to do it."""
    existing = conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()
    if existing:
        return {"created": False, "already_existed": True}

    currency = currency or fx.BASE_CURRENCY
    if not name or not institution or not account_type:
        raise ValueError("name, institution and account_type are all required for a new account")
    if account_type not in ACCOUNT_TYPES:
        raise ValueError(f"account_type must be one of {ACCOUNT_TYPES}, got '{account_type}'")
    if currency not in fx.RATES:
        raise ValueError(f"currency must be one of {sorted(fx.RATES)} (add a rate in src/fx.py for others)")
    if owner not in OWNERS:
        raise ValueError(f"owner must be one of {OWNERS}")
    iou_person = (iou_person or "").strip() or None
    if owner == "other" and not iou_person:
        raise ValueError("an account someone else pays (owner 'other') needs the person's name")

    if not apply:
        return {"created": False, "already_existed": False}
    conn.execute(
        "INSERT INTO accounts (name, institution, account_type, currency, owner, iou_person) VALUES (?,?,?,?,?,?)",
        (name, institution, account_type, currency, owner, iou_person if owner == "other" else None),
    )
    conn.commit()
    return {"created": True, "already_existed": False}


def retag_card_payoff_checking_legs(conn, checking_account_name, date_window_days=5, apply=False):
    """Re-tags 'expense' outflows on checking_account_name to
    'internal_transfer' when they're really the checking-side leg of a
    credit card payoff — detected by matching amount (checking's outflow
    exactly offsets a credit card inflow) and a nearby date, rather than
    by description text.

    Why not description matching: the card's own inflow almost always has
    bank-standardized wording ("Payment Thank You - Web") that
    CATEGORY_RULES can catch reliably, but checking's outflow side is
    phrased inconsistently across banks ("Online Banking transfer to CRD
    1234", "Credit Card Payment", etc.) and is impractical to enumerate.
    A checking outflow whose exact amount is offset by an
    already-internal_transfer-tagged credit card inflow within a few days
    is a much more reliable signal that it's the same payoff, not new
    spending — without this fix, the same real-world payment gets counted
    twice: once as the original card purchase, again as this "expense".

    Only self/joint-owned credit_card accounts are considered (matching
    net worth's own owner scoping) — paying off a card someone else owns
    is a personal_transfer (and an IOU), not an internal transfer."""
    checking_id_row = conn.execute("SELECT id FROM accounts WHERE name = ?", (checking_account_name,)).fetchone()
    if checking_id_row is None:
        raise ValueError(f"no account named '{checking_account_name}' exists")
    checking_id = checking_id_row["id"]

    rows = conn.execute(
        """
        SELECT t.id FROM transactions t
        WHERE t.account_id = ? AND t.transaction_type = 'expense'
        AND EXISTS (
            SELECT 1 FROM transactions c
            JOIN accounts ca ON ca.id = c.account_id
            WHERE ca.account_type = 'credit_card' AND ca.owner IN ('self', 'joint')
              AND c.transaction_type = 'internal_transfer'
              AND c.amount = -t.amount
              AND ABS(julianday(c.date) - julianday(t.date)) <= ?
        )
        """,
        (checking_id, date_window_days),
    ).fetchall()
    ids = [r["id"] for r in rows]
    if apply and ids:
        conn.executemany(
            "UPDATE transactions SET transaction_type = 'internal_transfer' WHERE id = ?",
            [(i,) for i in ids],
        )
        conn.commit()
    return {"matched": len(ids), "applied": apply}


def set_account_owner(conn, account_name, owner, iou_person=None, apply=False):
    """Sets accounts.owner for every account matching account_name exactly.
    Used to mark an account someone else settles (e.g. a card a family
    member pays off) as not the user's own asset/liability, so it's
    excluded from net worth — see aggregates.py's owner IN ('self',
    'joint') filters — and its spending counts as owed to `iou_person`."""
    if owner not in OWNERS:
        raise ValueError(f"owner must be one of {OWNERS}")
    iou_person = (iou_person or "").strip() or None
    if owner == "other" and not iou_person:
        raise ValueError("owner 'other' needs the person's name (--person)")
    matches = conn.execute("SELECT id FROM accounts WHERE name = ?", (account_name,)).fetchall()
    ids = [r["id"] for r in matches]
    if apply and ids:
        conn.execute("UPDATE accounts SET owner = ?, iou_person = ? WHERE name = ?",
                     (owner, iou_person if owner == "other" else None, account_name))
        conn.commit()
    return {"matched": len(ids), "applied": apply}


def retag_personal_transfers(conn, account_name, descriptions=None, note_contains=None, apply=False):
    """Re-tags transactions in account_name to 'personal_transfer' — for
    repayments to someone (e.g. paying a friend or family member back)
    that were logged as ordinary expenses. The real spend was already
    counted once via the original purchase being repaid, so leaving the
    repayment as 'expense' double-counts it in Spending/Cash Flow.
    'personal_transfer' behaves exactly like 'internal_transfer' everywhere
    (excluded from Spending/Cash Flow/needs-review) but stays a distinct
    label: money between you and other people, not between your own
    accounts.

    Only rows currently transaction_type='expense' are touched — rows
    already 'internal_transfer' (e.g. auto-tagged by the "transfer" rule
    in categorize.py) or 'income' are left alone, so an income row that
    happens to share the same note (e.g. someone's reimbursement to the
    user, noted with the same person's name) is never touched.

    At least one of `descriptions` (exact match, case-insensitive,
    whitespace-trimmed, against t.description) or `note_contains`
    (case-insensitive substring match against t.note) must be given; both
    may be combined (AND) for extra precision. `note_contains` is the more
    reliable discriminator when `category`/`description` are also reused
    for unrelated transactions (e.g. a generic "Repay" description that
    covers repaying other people too, or a "Reimbursement" category that
    also holds other people's reimbursements to the user)."""
    if not descriptions and not note_contains:
        raise ValueError("at least one of descriptions or note_contains is required")

    conditions = ["a.name = ?", "t.transaction_type = 'expense'"]
    params = [account_name]
    if descriptions:
        lowered = [d.strip().lower() for d in descriptions]
        placeholders = ",".join("?" for _ in lowered)
        conditions.append(f"LOWER(TRIM(t.description)) IN ({placeholders})")
        params += lowered
    if note_contains:
        conditions.append("LOWER(t.note) LIKE ?")
        params.append(f"%{note_contains.strip().lower()}%")

    rows = conn.execute(
        f"SELECT t.id FROM transactions t JOIN accounts a ON a.id = t.account_id "
        f"WHERE {' AND '.join(conditions)}",
        params,
    ).fetchall()
    ids = [r["id"] for r in rows]
    if apply and ids:
        conn.executemany(
            "UPDATE transactions SET transaction_type = 'personal_transfer' WHERE id = ?",
            [(i,) for i in ids],
        )
        conn.commit()
    return {"matched": len(ids), "applied": apply}


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    owner_cmd = sub.add_parser("set-owner", help="Set an account's owner (self, joint, or other = someone else pays it)")
    owner_cmd.add_argument("account_name")
    owner_cmd.add_argument("owner", choices=OWNERS)
    owner_cmd.add_argument("--person", default=None, help="Owner 'other': who pays it (their IOU)")
    owner_cmd.add_argument("--apply", action="store_true", help="Commit the change (default: dry run)")

    retag_cmd = sub.add_parser("retag-transfers", help="Re-tag repayments logged as expenses as personal_transfer")
    retag_cmd.add_argument("account_name")
    retag_cmd.add_argument(
        "--description", action="append", dest="descriptions", default=None,
        help="Exact description to match, case-insensitive (repeatable, e.g. --description Repay --description \"Transfer to family\")",
    )
    retag_cmd.add_argument(
        "--note-contains", dest="note_contains", default=None,
        help='Substring to match against the note field, case-insensitive (e.g. --note-contains "Alex")',
    )
    retag_cmd.add_argument("--apply", action="store_true", help="Commit the change (default: dry run)")

    create_cmd = sub.add_parser("create-account", help="Create an account (same as Net Worth → Add Account)")
    create_cmd.add_argument("account_name")
    create_cmd.add_argument("--institution", required=True)
    create_cmd.add_argument("--account-type", dest="account_type", required=True, choices=ACCOUNT_TYPES)
    create_cmd.add_argument("--currency", default=fx.BASE_CURRENCY)
    create_cmd.add_argument("--owner", default="self", choices=OWNERS)
    create_cmd.add_argument("--person", default=None, help="Owner 'other': who pays it")
    create_cmd.add_argument("--apply", action="store_true", help="Commit the change (default: dry run)")

    payoff_cmd = sub.add_parser(
        "retag-card-payoffs",
        help="Re-tag checking expenses that are really the checking-side leg of a credit card payoff",
    )
    payoff_cmd.add_argument("checking_account_name")
    payoff_cmd.add_argument(
        "--date-window-days", type=int, default=5,
        help="Max days apart between the checking outflow and the matching card inflow (default: 5)",
    )
    payoff_cmd.add_argument("--apply", action="store_true", help="Commit the change (default: dry run)")

    dates_cmd = sub.add_parser(
        "list-balance-dates",
        help="Read-only: print each account's balance-snapshot dates (never balance values) — for diagnosing a Net Worth trend spike/dip",
    )
    dates_cmd.add_argument("--start", default=None, help="ISO date, inclusive")
    dates_cmd.add_argument("--end", default=None, help="ISO date, inclusive")

    args = parser.parse_args()
    db.init_db()
    conn = db.get_connection()
    try:
        if args.command == "set-owner":
            result = set_account_owner(conn, args.account_name, args.owner, iou_person=args.person, apply=args.apply)
        elif args.command == "retag-transfers":
            if not args.descriptions and not args.note_contains:
                parser.error("retag-transfers requires at least one of --description or --note-contains")
            result = retag_personal_transfers(
                conn, args.account_name, descriptions=args.descriptions,
                note_contains=args.note_contains, apply=args.apply,
            )
        elif args.command == "retag-card-payoffs":
            result = retag_card_payoff_checking_legs(
                conn, args.checking_account_name, date_window_days=args.date_window_days, apply=args.apply,
            )
        elif args.command == "list-balance-dates":
            result = list_balance_dates(conn, start=args.start, end=args.end)
        else:
            result = create_account(
                conn, args.account_name, institution=args.institution, account_type=args.account_type,
                currency=args.currency, owner=args.owner, iou_person=args.person, apply=args.apply,
            )
            if result["created"]:
                from src.parsers.csv_importer import ensure_account_folders
                ensure_account_folders(conn)
    finally:
        conn.close()

    if args.command == "create-account":
        if result["already_existed"]:
            print(f"create-account: '{args.account_name}' already exists — nothing to do")
        elif result["created"]:
            print(f"create-account: '{args.account_name}' created")
        else:
            print(f"create-account: '{args.account_name}' would be created — DRY RUN, pass --apply to commit")
    elif args.command == "list-balance-dates":
        if not result:
            print("No balance snapshots found in that range.")
        for account_name, dates in result.items():
            print(f"{account_name}: {', '.join(dates)}")
    else:
        mode = "APPLIED" if result["applied"] else "DRY RUN — no changes made, pass --apply to commit"
        print(f"{args.command}: matched {result['matched']} row(s) — {mode}")


if __name__ == "__main__":
    _main()
