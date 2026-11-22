"""Query + calculation helpers shared across page routes. Keeps app.py
route handlers thin: fetch via these, render template."""

import calendar
from datetime import date, timedelta

from src.fx import to_base

LIABILITY_TYPES = ("credit_card", "loan")

# Types that never count as income or spending — money moving between
# your own accounts, or between you and other people. Excluded from Cash
# Flow, Spending and "needs a category".
TRANSFER_TYPES = ("internal_transfer", "personal_transfer")

# Account types that ALWAYS compute their balance live as the running sum
# of their own transactions, never from a balances-table snapshot (e.g.
# Venmo/PayPal, or physical cash on hand: every movement is logged as an
# actual transaction, and there's no statement to snapshot anyway). credit_card accounts are
# handled separately in latest_balance() below — they prefer a real
# statement-balance snapshot when one exists, and only fall back to the
# computed sum when there's no snapshot history at all (card CSVs have no
# running-balance column) — a real snapshot is more trustworthy since it
# doesn't silently understate debt if some transactions are missing.
COMPUTED_BALANCE_TYPES = ("digital_wallet", "cash")


def _computed_balance(conn, account_id, as_of=None):
    query = "SELECT SUM(amount) AS total FROM transactions WHERE account_id = ?"
    params = [account_id]
    if as_of:
        query += " AND date <= ?"
        params.append(as_of)
    row = conn.execute(query, params).fetchone()
    return {"balance": row["total"] or 0.0, "principal_contributed": None, "date": as_of}


# Account types whose value doesn't move with transactions alone — a
# brokerage/retirement balance changes with market movement between
# manual snapshots, so their own transactions never roll them forward.
# Only deposits matched from other accounts do (see _investment_position):
# that's money that certainly arrived, while market moves wait for the
# next snapshot. Every other type (checking, savings, loan, credit_card)
# has transactions as its complete source of truth, so those fully roll
# forward.
INVESTMENT_TYPES = ("brokerage", "retirement")
NO_ROLLFORWARD_TYPES = INVESTMENT_TYPES


def investment_accounts_by_name(conn):
    """{name: id} for every brokerage/retirement account — what the
    Investments page's dropdowns and edit endpoints validate against."""
    return {r["name"]: r["id"] for r in conn.execute(
        "SELECT id, name FROM accounts WHERE account_type IN ('brokerage', 'retirement')"
    ).fetchall()}


def investment_contributions(conn, as_of=None):
    """Every investment row on a non-investment account (money leaving
    checking for the portfolio): Type investment_contribution, or Category
    starting with "invest" (so a mis-typed one still surfaces). Rows on the
    investment account itself are skipped, so logging both legs of a
    deposit can't count it twice. `amount` is positive = money in.

    Which account it went to, in order:
      1. transactions.investment_account, set on the Investments page —
         an account id, or 'none' (not an investment: dropped);
      2. the note naming an account — its full name or first word,
         ignoring case ("roth" → Roth IRA, "individual" → Individual);
      3. the description, the same way ("Transfer to Roth IRA").
    `status`: 'matched' (counts — `account_id` is set), 'unmatched' (no
    account named), 'ambiguous' (more than one), 'wrong_type' (Category
    says investment but Type doesn't — only Type moves money), or
    'excluded'. Everything but matched/excluded is listed on Investments
    to fix."""
    accounts = conn.execute(
        "SELECT id, name FROM accounts WHERE account_type IN ('brokerage', 'retirement')"
    ).fetchall()
    keywords = {a["id"]: {a["name"].lower(), a["name"].split()[0].lower()} for a in accounts if a["name"].strip()}
    query = (
        "SELECT t.id, t.date, t.amount, t.note, t.description, t.transaction_type, t.investment_account, "
        "a.name AS from_account FROM transactions t "
        "JOIN accounts a ON a.id = t.account_id "
        "WHERE (t.transaction_type = 'investment_contribution' OR LOWER(COALESCE(t.category, '')) LIKE 'invest%') "
        "AND a.account_type NOT IN ('brokerage', 'retirement')"
    )
    params = []
    if as_of:
        query += " AND t.date <= ?"
        params.append(as_of)
    query += " ORDER BY t.date, t.id"

    def matches(text):
        text = (text or "").lower()
        return [aid for aid, words in keywords.items() if any(w in text for w in words)]

    result = []
    for r in conn.execute(query, params).fetchall():
        override = r["investment_account"]
        target, how = None, None
        if override == "none":
            status = "excluded"
        elif override and override.isdigit() and int(override) in keywords:
            target, how, status = int(override), "manual", "matched"
        else:
            # The note wins; the description is the fallback
            hits = matches(r["note"])
            how = "note"
            if not hits:
                hits, how = matches(r["description"]), "description"
            if len(hits) == 1:
                target, status = hits[0], "matched"
            else:
                status, how = ("ambiguous" if hits else "unmatched"), None
        if r["transaction_type"] != "investment_contribution" and status != "excluded":
            status = "wrong_type"
        result.append(dict(
            id=r["id"], date=r["date"], amount=-r["amount"], description=r["description"], note=r["note"],
            from_account=r["from_account"], transaction_type=r["transaction_type"], override=override,
            status=status, how=how if status == "matched" else None,
            guess_id=target, account_id=target if status == "matched" else None,
        ))
    return result


def manual_deposits(conn, as_of=None):
    """"Principal added" entries typed on the Investments page
    (investment_deposits table), oldest first."""
    query = "SELECT id, account_id, date, amount, note FROM investment_deposits"
    params = []
    if as_of:
        query += " WHERE date <= ?"
        params.append(as_of)
    return [dict(r) for r in conn.execute(query + " ORDER BY date, id", params).fetchall()]


def investment_inflows(conn, as_of=None):
    """All money that went into investment accounts: matched contribution
    transactions + hand-entered "principal added". Each {date, amount,
    account_id, source}. This is what principal is the sum of."""
    inflows = [
        dict(date=c["date"], amount=c["amount"], account_id=c["account_id"], source="transaction")
        for c in investment_contributions(conn, as_of) if c["status"] == "matched"
    ]
    inflows += [
        dict(date=d["date"], amount=d["amount"], account_id=d["account_id"], source="manual")
        for d in manual_deposits(conn, as_of)
    ]
    return inflows


def _investment_position(conn, account_id, anchor, as_of=None):
    """Brokerage/retirement value + principal. Value is the latest snapshot
    plus inflows dated after it (deposits only, never market moves — the
    next snapshot replaces the estimate). Principal is the sum of inflows
    (matched transactions + hand-entered principal added). A snapshot with
    a typed principal_contributed (older data; the form no longer asks for
    it) still overrides from its date, with later inflows on top."""
    inflows = [i for i in investment_inflows(conn, as_of) if i["account_id"] == account_id]
    query = (
        "SELECT principal_contributed, date FROM balances "
        "WHERE account_id = ? AND principal_contributed IS NOT NULL"
    )
    params = [account_id]
    if as_of:
        query += " AND date <= ?"
        params.append(as_of)
    principal_anchor = conn.execute(query + " ORDER BY date DESC LIMIT 1", params).fetchone()
    principal_since = principal_anchor["date"] if principal_anchor else ""
    principal = (principal_anchor["principal_contributed"] if principal_anchor else 0.0) + sum(
        i["amount"] for i in inflows if i["date"] > principal_since
    )

    if anchor is None:
        if not inflows:
            return None  # nothing deposited or recorded yet — no fake 0
        return {"balance": sum(i["amount"] for i in inflows), "principal_contributed": principal,
                "date": as_of, "source": "inflows", "snapshot_date": None}
    added = sum(i["amount"] for i in inflows if i["date"] > anchor["date"])
    return {"balance": anchor["balance"] + added, "principal_contributed": principal,
            "date": as_of or anchor["date"], "source": "snapshot", "snapshot_date": anchor["date"],
            "added_since_snapshot": added}


def latest_balance(conn, account_id, as_of=None):
    account_type = conn.execute(
        "SELECT account_type FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()["account_type"]
    if account_type in COMPUTED_BALANCE_TYPES:
        return _computed_balance(conn, account_id, as_of)

    if as_of:
        anchor = conn.execute(
            "SELECT balance, principal_contributed, date FROM balances "
            "WHERE account_id = ? AND date <= ? ORDER BY date DESC LIMIT 1",
            (account_id, as_of),
        ).fetchone()
    else:
        anchor = conn.execute(
            "SELECT balance, principal_contributed, date FROM balances "
            "WHERE account_id = ? ORDER BY date DESC LIMIT 1",
            (account_id,),
        ).fetchone()

    if anchor is None:
        if account_type == "credit_card":
            # No statement-balance history at all for this account (card
            # CSVs have no running-balance column) — fall back to the
            # running sum of its own transactions, negated (spending is a
            # negative amount, but "balance owed" is expressed as
            # positive).
            computed = _computed_balance(conn, account_id, as_of)
            computed["balance"] = -computed["balance"]
            computed["source"] = "computed"
            return computed
        if account_type in NO_ROLLFORWARD_TYPES:
            return _investment_position(conn, account_id, None, as_of)
        # No recorded balance (no starting balance given, no Set Balance
        # yet): treat it as having started at 0,
        # so the sum of its transactions IS the balance — right whenever
        # the first transaction is the opening deposit. Set Balance on the
        # Net Worth page adds a real anchor when that isn't true. Loans
        # are negated like credit cards (owed is stored positive). Nothing
        # logged yet (as of this date) → None, not a fake 0.
        query = "SELECT COUNT(*) AS n FROM transactions WHERE account_id = ?"
        params = [account_id]
        if as_of:
            query += " AND date <= ?"
            params.append(as_of)
        if conn.execute(query, params).fetchone()["n"] == 0:
            return None
        computed = _computed_balance(conn, account_id, as_of)
        if account_type in LIABILITY_TYPES:
            computed["balance"] = -computed["balance"]
        computed["source"] = "computed"
        return computed

    if account_type in NO_ROLLFORWARD_TYPES:
        return _investment_position(conn, account_id, anchor, as_of)

    # Roll the anchor forward using every transaction dated after it, so
    # the balance stays accurate even when nobody's added a newer
    # balances-table snapshot (CSV imports never write one — only a
    # starting balance, Set Balance, or the history import do) —
    # same principle as the Transactions page's own running balance
    # (statement_balance-preferred, else summed transactions).
    query = "SELECT SUM(amount) AS total FROM transactions WHERE account_id = ? AND date > ?"
    params = [account_id, anchor["date"]]
    if as_of:
        query += " AND date <= ?"
        params.append(as_of)
    delta = conn.execute(query, params).fetchone()["total"] or 0.0
    return {
        "balance": anchor["balance"] + delta,
        "principal_contributed": anchor["principal_contributed"],
        "date": as_of or anchor["date"],
    }


def net_worth_breakdown(conn, include_investments=True):
    # owner 'other' (an account someone else settles, e.g. a card a family
    # member pays off) is never the user's own liability or asset, so it's
    # excluded from net worth entirely — its spending feeds the IOUs page
    # instead.
    #
    # Deliberately no adjustment for personal_transfer money "parked" in
    # an account (money held for someone, about to be forwarded): summing
    # personal_transfer rows can't tell a two-legged pass-through (nets to
    # zero) from a one-way repayment (never does), so any such adjustment
    # drifts further off with every repayment. Net worth is simplest and
    # most trustworthy computed straight from real account balances; held
    # money shows as a temporary bump until it's forwarded.
    #
    # include_investments=False drops brokerage/retirement accounts
    # entirely (used by Overview's headline number, which is meant to stay
    # a stable, day-to-day figure that market swings don't move) — the
    # Net Worth page itself always calls this with the default (True) for
    # the complete picture. Either way, a CD is unaffected by this flag —
    # it's tagged account_type='savings' (see the cds table's own comment
    # in db.py), so it's never excluded here regardless.
    query = "SELECT * FROM accounts WHERE is_active = 1 AND owner IN ('self', 'joint')"
    if not include_investments:
        query += " AND account_type NOT IN ('brokerage', 'retirement')"
    accounts = conn.execute(query).fetchall()
    thirty_days_ago = (date.today() - timedelta(days=30)).isoformat()

    rows = []
    total_assets, total_liabilities = 0.0, 0.0
    for acct in accounts:
        current = latest_balance(conn, acct["id"])
        balance = current["balance"] if current else 0.0
        balance_base = to_base(balance, acct["currency"])
        is_liability = acct["account_type"] in LIABILITY_TYPES

        if is_liability:
            total_liabilities += balance_base
        else:
            total_assets += balance_base

        prior = latest_balance(conn, acct["id"], as_of=thirty_days_ago)
        change_30d = (balance - prior["balance"]) if prior else None

        rows.append(dict(
            account=acct, balance=balance, balance_base=balance_base,
            change_30d=change_30d, is_liability=is_liability,
            # "computed" = no recorded balance, summed from transactions —
            # flagged on Net Worth so it can be checked against the bank
            from_transactions=isinstance(current, dict) and current.get("source") == "computed"
                              and acct["account_type"] not in COMPUTED_BALANCE_TYPES,
        ))

    return dict(
        rows=rows,
        total_assets=total_assets,
        total_liabilities=total_liabilities,
        net_worth=total_assets - total_liabilities,
    )


def earliest_balance_date(conn):
    row = conn.execute("SELECT MIN(date) AS d FROM balances").fetchone()
    return row["d"]


def earliest_investment_balance_date(conn):
    """First snapshot or deposit date, whichever is earlier."""
    row = conn.execute(
        "SELECT MIN(date) AS d FROM balances WHERE account_id IN "
        "(SELECT id FROM accounts WHERE account_type IN ('brokerage', 'retirement'))"
    ).fetchone()
    dates = [row["d"]] + [i["date"] for i in investment_inflows(conn)]
    return min((d for d in dates if d), default=None)


def earliest_transaction_date(conn):
    row = conn.execute("SELECT MIN(date) AS d FROM transactions").fetchone()
    return row["d"]


def _add_month(d):
    if d.month == 12:
        return d.replace(year=d.year + 1, month=1)
    return d.replace(month=d.month + 1)


def net_worth_trend(conn, start=None, end=None, include_investments=True):
    """One point per week (spans up to ~13 months — covers the 3M/6M/1Y
    presets) or per calendar month (longer spans — 3Y/5Y/All), evaluating
    every account at the exact same as_of date per point — never at
    whichever account's own balance-snapshot date happens to fall first.
    That's what actually avoids two accounts' mismatched snapshot
    cadences making a transfer between them look like a spike (the
    receiving account already reflecting an inflow while the sending one
    is still using its last, pre-transfer balance) — not the specific
    bucket size, which is purely about how many points are worth showing
    for a given zoom level. latest_balance()'s roll-forward (see above)
    means any date can be evaluated, not just ones with an actual
    snapshot, so resolution is a free choice here.

    include_investments=False (used by Overview) drops brokerage/
    retirement accounts from every point, so the trend line isn't moved
    by market swings between manual updates — same reasoning as
    net_worth_breakdown's flag of the same name."""
    if not start or not end:
        return []

    query = "SELECT id, currency, account_type FROM accounts WHERE owner IN ('self', 'joint')"
    if not include_investments:
        query += " AND account_type NOT IN ('brokerage', 'retirement')"
    accounts = conn.execute(query).fetchall()

    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    effective_end = min(end_d, date.today())
    if effective_end < start_d:
        return []

    monthly = (end_d - start_d).days > 400
    as_of_dates = []
    if monthly:
        cursor = start_d.replace(day=1)
        while cursor <= effective_end:
            last_day = calendar.monthrange(cursor.year, cursor.month)[1]
            as_of_dates.append(date(cursor.year, cursor.month, last_day))
            cursor = _add_month(cursor)
    else:
        cursor = start_d + timedelta(days=6)
        while cursor <= effective_end:
            as_of_dates.append(cursor)
            cursor += timedelta(days=7)
    if not as_of_dates or as_of_dates[-1] < effective_end:
        as_of_dates.append(effective_end)

    trend = []
    for as_of_d in as_of_dates:
        as_of = min(as_of_d, effective_end).isoformat()
        total = 0.0
        any_data = False
        for acct in accounts:
            row = latest_balance(conn, acct["id"], as_of=as_of)
            if row:
                any_data = True
                value = to_base(row["balance"], acct["currency"])
                total += -value if acct["account_type"] in LIABILITY_TYPES else value
        if any_data:  # never plot a fake $0 point before any account has data yet
            trend.append(dict(date=as_of, net_worth=round(total, 2)))
    return trend


def balances_by_currency(conn):
    accounts = conn.execute(
        "SELECT id, currency FROM accounts WHERE is_active = 1 AND account_type NOT IN ('credit_card', 'loan') "
        "AND owner IN ('self', 'joint')"
    ).fetchall()
    totals = {}
    for acct in accounts:
        row = latest_balance(conn, acct["id"])
        if not row:
            continue
        totals[acct["currency"]] = totals.get(acct["currency"], 0.0) + row["balance"]
    combined_base = sum(to_base(amount, currency) for currency, amount in totals.items())
    return dict(by_currency=totals, combined_base=combined_base)


# Matches "Pass-Through", "pass through", "Passthrough", ... — the one place
# a category name changes what's counted (see CLAUDE.md's /spending notes).
_PASS_THROUGH_SQL = "LOWER(REPLACE(REPLACE(COALESCE(category, ''), '-', ''), ' ', '')) = 'passthrough'"


def spending_by_category(conn, start, end, include_pass_through=False):
    pass_through_filter = "" if include_pass_through else f"AND NOT {_PASS_THROUGH_SQL} "
    rows = conn.execute(
        "SELECT COALESCE(category, 'Uncategorized') AS category, SUM(-amount) AS total "
        "FROM transactions WHERE transaction_type = 'expense' AND date BETWEEN ? AND ? "
        f"{pass_through_filter}"
        "GROUP BY category ORDER BY total DESC",
        (start, end),
    ).fetchall()
    total = sum(r["total"] for r in rows)
    return [
        dict(category=r["category"], amount=r["total"], pct=(r["total"] / total * 100 if total else 0))
        for r in rows
    ], total


def _prior_period(start, end):
    start_d, end_d = date.fromisoformat(start), date.fromisoformat(end)
    length = (end_d - start_d).days
    prior_end = start_d - timedelta(days=1)
    prior_start = prior_end - timedelta(days=length)
    return prior_start.isoformat(), prior_end.isoformat()


def spending_mom_change(conn, category, start, end):
    prior_start, prior_end = _prior_period(start, end)
    row = conn.execute(
        "SELECT SUM(-amount) AS total FROM transactions "
        "WHERE transaction_type = 'expense' AND category = ? AND date BETWEEN ? AND ?",
        (category, prior_start, prior_end),
    ).fetchone()
    return row["total"] or 0.0


def category_sparkline(conn, category, months=6):
    rows = conn.execute(
        "SELECT substr(date, 1, 7) AS ym, SUM(-amount) AS total FROM transactions "
        "WHERE transaction_type = 'expense' AND category = ? "
        "GROUP BY ym ORDER BY ym DESC LIMIT ?",
        (category, months),
    ).fetchall()
    return [dict(month=r["ym"], amount=r["total"]) for r in reversed(rows)]


def _investment_account_ids(conn, account_ids=None):
    """Active brokerage/retirement accounts, optionally narrowed to some."""
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM accounts WHERE account_type IN ('brokerage', 'retirement') AND is_active = 1"
    ).fetchall()]
    return [i for i in ids if account_ids is None or i in account_ids]


def investment_summary(conn, account_ids=None):
    """Value / principal / growth across all investment accounts, or just
    `account_ids` (one account's own section on the Investments page)."""
    current_value, principal = 0.0, 0.0
    for account_id in _investment_account_ids(conn, account_ids):
        row = latest_balance(conn, account_id)
        if row:
            current_value += row["balance"]
            principal += row["principal_contributed"] or 0.0
    growth = current_value - principal
    return dict(current_value=current_value, principal=principal, growth=growth,
                growth_pct=(growth / principal * 100) if principal else None)


def investment_trend(conn, start=None, end=None, account_ids=None):
    """One point per snapshot or deposit date (the only days
    value/principal can change), summed over the chosen accounts. With a
    range, the range's start and end get a point too — but only once
    something's recorded by then — so a line opens at the left edge if the
    money was already there, otherwise at the first deposit, and runs on
    to the end at its latest value."""
    ids = _investment_account_ids(conn, account_ids)
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    dates = {r["date"] for r in conn.execute(
        f"SELECT DISTINCT date FROM balances WHERE account_id IN ({placeholders})", ids
    ).fetchall()}
    dates |= {i["date"] for i in investment_inflows(conn) if i["account_id"] in ids}
    if start and end:
        dates = {d for d in dates if start <= d <= end} | {start, end}
    trend = []
    for d in sorted(dates):
        value, principal, has_data = 0.0, 0.0, False
        for account_id in ids:
            row = latest_balance(conn, account_id, as_of=d)
            if row:
                has_data = True
                value += row["balance"]
                principal += row["principal_contributed"] or 0.0
        if has_data:  # no fake $0 before the first deposit
            trend.append(dict(date=d, value=round(value, 2), principal=round(principal, 2)))
    return trend


def fixed_income_trend(conn, start, end, account_ids=None):
    """How much of the chosen investment accounts sits in CDs & bonds over
    time — the shaded base band on the Investments charts. Each row counts
    its principal from opened_date up to (not including) maturity_date, or
    to the end if it has none; rows without an opened_date can't be placed
    and are left out. One point per date the total changes, plus the range
    start and end, for a step line ({date, amount})."""
    ids = _investment_account_ids(conn, account_ids)
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT principal, opened_date, maturity_date FROM cds "
        f"WHERE account_id IN ({placeholders}) AND opened_date IS NOT NULL",
        ids,
    ).fetchall()
    if not rows:
        return []

    def held_on(day):
        return sum(r["principal"] for r in rows
                   if r["opened_date"] <= day and (not r["maturity_date"] or day < r["maturity_date"]))

    changes = {r["opened_date"] for r in rows} | {r["maturity_date"] for r in rows if r["maturity_date"]}
    dates = sorted({d for d in changes if start <= d <= end} | {start, end})
    return [dict(date=d, amount=round(held_on(d), 2)) for d in dates]


def investment_allocation(conn):
    accounts = conn.execute(
        "SELECT id, name FROM accounts WHERE account_type IN ('brokerage', 'retirement') AND is_active = 1"
    ).fetchall()
    allocation = []
    for acct in accounts:
        row = latest_balance(conn, acct["id"])
        balance = row["balance"] if row else 0.0
        principal = (row["principal_contributed"] or 0.0) if row else 0.0
        allocation.append(dict(name=acct["name"], balance=balance, principal=principal))
    return allocation


def list_cds(conn):
    """Every recorded CD or bond, joined with its linked account's name and
    current balance — a detail/memo view only. `current_balance` is
    whatever that account is worth right now via the normal
    latest_balance() pipeline (already counted once in Net Worth through
    that account); `total_principal` is an informational sum of what was
    originally put into each CD, never itself added into Net Worth."""
    rows = conn.execute(
        "SELECT c.*, a.name AS account_name FROM cds c "
        "JOIN accounts a ON a.id = c.account_id "
        "ORDER BY c.maturity_date IS NULL, c.maturity_date ASC"
    ).fetchall()
    result = []
    total_principal = 0.0
    for r in rows:
        balance_row = latest_balance(conn, r["account_id"])
        result.append(dict(
            id=r["id"], institution=r["institution"], account_name=r["account_name"],
            account_id=r["account_id"], principal=r["principal"], apy=r["apy"],
            opened_date=r["opened_date"], maturity_date=r["maturity_date"], note=r["note"],
            kind=r["kind"], current_balance=balance_row["balance"] if balance_row else None,
        ))
        total_principal += r["principal"]
    return dict(cds=result, total_principal=total_principal)


def investment_entries(conn, contributions=None):
    """Everything behind the Investments numbers, newest first: manual
    snapshots (balances rows), hand-entered principal added
    (investment_deposits), and contributions picked up from Transactions
    (including excluded/unresolved ones, so any choice can be undone)."""
    if contributions is None:
        contributions = investment_contributions(conn)
    names = {r["id"]: r["name"] for r in conn.execute(
        "SELECT id, name FROM accounts WHERE account_type IN ('brokerage', 'retirement')"
    ).fetchall()}
    entries = [
        dict(kind="snapshot", id=r["id"], date=r["date"], account=names.get(r["account_id"]),
             amount=r["balance"], principal=r["principal_contributed"], note=r["note"])
        for r in conn.execute(
            "SELECT id, account_id, date, balance, principal_contributed, note FROM balances "
            "WHERE account_id IN (SELECT id FROM accounts WHERE account_type IN ('brokerage', 'retirement'))"
        ).fetchall()
    ]
    entries += [
        dict(kind="deposit", id=d["id"], date=d["date"], account=names.get(d["account_id"]),
             amount=d["amount"], note=d["note"])
        for d in manual_deposits(conn)
    ]
    entries += [dict(kind="contribution", account=names.get(c["account_id"]), **c) for c in contributions]
    entries.sort(key=lambda e: (e["date"], e["kind"] == "snapshot"), reverse=True)
    return entries


IOU_KINDS = ("they_paid", "i_paid")
IOU_OVERRIDES = IOU_KINDS + ("none",)
UNNAMED_PERSON = "(no name)"


def iou_status(owner, account_person, transaction_type, amount, attributable_to_user, kind, person):
    """Whether a transaction counts on the IOUs page, and for whom.
    Returns (kind, person, reason) — kind is 'they_paid' (money or value
    from that person to you: adds to what you owe them) or 'i_paid' (from
    you to them: reduces it, below zero = they owe you) — or (None, None,
    None). reason is 'manual' or 'account'.

    A manual override (transactions.iou_kind, set on the Transactions
    page) always wins; its person is transactions.iou_person, else the
    account's. Otherwise, automatically: spending on an account someone
    else settles (owner 'other') is something they paid for you, unless
    marked Not Mine; a refund on it (positive amount) reduces that again.
    Only expense/income rows — a transfer on that account (e.g. them
    paying the card off) isn't a repayment by you.

    Nothing else is guessed: money moving between you and someone (a
    personal_transfer) could be a loan, a repayment, or money you're only
    passing along, so the user marks those themselves."""
    if kind in IOU_KINDS:
        return kind, (person or account_person or UNNAMED_PERSON), "manual"
    if kind == "none":
        return None, None, None
    if owner == "other" and attributable_to_user and transaction_type in ("expense", "income") and amount:
        return ("they_paid" if amount < 0 else "i_paid"), (account_person or UNNAMED_PERSON), "account"
    return None, None, None


def _settlement_totals(entries):
    totals = {"pending": 0.0, "settled": 0.0, None: 0.0}
    for e in entries:
        totals[e["settlement"] if e["settlement"] in ("pending", "settled") else None] += e["amount"]
    return {"pending": round(totals["pending"], 2), "settled": round(totals["settled"], 2),
            "unmarked": round(totals[None], 2)}


def iou_ledgers(conn):
    """One ledger per person, largest balance first. Each combines
    transactions that count (see iou_status) with manual `ious` rows, and
    has a running `balance` = what you owe that person (negative = they
    owe you), in the base currency. A person's most recent
    'starting_balance' replaces everything of theirs dated on or before
    it, so years of history don't have to be entered one row at a time.

    Entries are split for the page's two side-by-side lists: they_paid
    (left) and i_paid (right). A starting balance sits on the side its
    sign points to. Settlement marks are reminders only — they never feed
    the balance."""
    rows = conn.execute(
        "SELECT t.id, t.date, t.amount, t.currency, t.description, t.transaction_type, "
        "t.attributable_to_user, t.iou_kind, t.iou_person, t.iou_settlement, "
        "a.name AS account_name, a.owner, a.iou_person AS account_person "
        "FROM transactions t JOIN accounts a ON a.id = t.account_id "
        "WHERE t.iou_kind IS NOT NULL OR a.owner = 'other'"
    ).fetchall()
    by_person = {}
    for r in rows:
        kind, person, reason = iou_status(
            r["owner"], r["account_person"], r["transaction_type"], r["amount"],
            r["attributable_to_user"], r["iou_kind"], r["iou_person"],
        )
        if kind is None:
            continue
        by_person.setdefault(person, []).append(dict(
            date=r["date"], description=r["description"] or "", account_name=r["account_name"],
            kind=kind, amount=abs(to_base(r["amount"], r["currency"])),
            source="transaction", id=r["id"], reason=reason, settlement=r["iou_settlement"],
        ))
    for r in conn.execute("SELECT * FROM ious").fetchall():
        by_person.setdefault(r["person"], []).append(dict(
            date=r["date"], description=r["description"], account_name=None,
            kind=r["direction"], amount=r["amount"], source="manual", id=r["id"], reason="manual",
            settlement=r["settlement"],
        ))

    ledgers = []
    for person, entries in by_person.items():
        # On the same date, a starting balance sorts last — it's "as of the
        # end of that day," so it replaces that day's other entries too.
        entries.sort(key=lambda e: (e["date"], e["kind"] == "starting_balance"))
        starts = [e for e in entries if e["kind"] == "starting_balance"]
        cutoff = starts[-1]["date"] if starts else None
        counted = [e for e in entries if cutoff is None or e["date"] > cutoff or e is starts[-1]]

        balance = 0.0
        for e in counted:
            if e["kind"] == "starting_balance":
                balance = e["amount"]
            elif e["kind"] == "they_paid":
                balance += e["amount"]
            else:
                balance -= e["amount"]
        counted.reverse()  # newest first

        they_paid, i_paid = [], []
        for e in counted:
            if e["kind"] == "starting_balance":
                side = they_paid if e["amount"] >= 0 else i_paid
                side.append(dict(e, amount=abs(e["amount"])))
            else:
                (they_paid if e["kind"] == "they_paid" else i_paid).append(e)
        ledgers.append(dict(
            person=person, balance=round(balance, 2), entries=counted,
            they_paid=they_paid, i_paid=i_paid,
            they_paid_total=round(sum(e["amount"] for e in they_paid), 2),
            i_paid_total=round(sum(e["amount"] for e in i_paid), 2),
            they_paid_marks=_settlement_totals(they_paid), i_paid_marks=_settlement_totals(i_paid),
            starting_balance_date=cutoff, replaced_count=len(entries) - len(counted),
        ))
    ledgers.sort(key=lambda l: (-abs(l["balance"]), l["person"].lower()))
    return ledgers


def iou_balances(conn):
    """[{person, balance}] for everyone with a non-zero balance — what
    Overview lists next to debts. Positive = you owe them."""
    return [dict(person=l["person"], balance=l["balance"])
            for l in iou_ledgers(conn) if abs(l["balance"]) >= 0.005]


def iou_people(conn):
    """Every name used anywhere for IOUs — suggestions for the person
    boxes on Transactions, IOUs and Add Account."""
    names = set()
    for sql in ("SELECT DISTINCT person AS p FROM ious",
                "SELECT DISTINCT iou_person AS p FROM transactions",
                "SELECT DISTINCT iou_person AS p FROM accounts"):
        names |= {r["p"] for r in conn.execute(sql).fetchall() if r["p"]}
    return sorted(names, key=str.lower)


def running_balances_by_account(conn):
    """Prefers each row's own recorded statement_balance (the real,
    bank-reported balance after that transaction, from its statement)
    when it has one. Only falls back to a computed
    running sum where there's no recorded value at all (credit cards,
    which never have one, or manually-added rows) — and self-corrects
    the moment a row with a recorded balance shows up again, rather than
    letting a computed drift compound indefinitely."""
    rows = conn.execute(
        "SELECT id, account_id, amount, statement_balance FROM transactions "
        "ORDER BY account_id, date ASC, id ASC"
    ).fetchall()
    running_by_account, balance_by_txn_id = {}, {}
    for r in rows:
        if r["statement_balance"] is not None:
            running_by_account[r["account_id"]] = r["statement_balance"]
        else:
            running_by_account[r["account_id"]] = running_by_account.get(r["account_id"], 0.0) + r["amount"]
        balance_by_txn_id[r["id"]] = round(running_by_account[r["account_id"]], 2)
    return balance_by_txn_id


UNCATEGORIZED = "Uncategorized"


def fetch_transactions(conn, year=None, month=None, account_id=None, category=None, q=None, start=None, end=None, review=None, attributable=None):
    query = (
        "SELECT t.*, a.name AS account_name, a.currency AS account_currency, a.owner AS account_owner, "
        "a.iou_person AS account_person "
        "FROM transactions t JOIN accounts a ON a.id = t.account_id WHERE 1=1"
    )
    params = []
    if start and end:
        query += " AND t.date BETWEEN ? AND ?"
        params += [start, end]
    if year:
        query += " AND substr(t.date, 1, 4) = ?"
        params.append(str(year))
    if month:
        query += " AND substr(t.date, 6, 2) = ?"
        params.append(f"{int(month):02d}")
    if account_id:
        query += " AND t.account_id = ?"
        params.append(account_id)
    if category == UNCATEGORIZED:
        # internal_transfer/personal_transfer rows are excluded from
        # Spending/Cash Flow regardless of category, so they never need
        # one — don't make them show up as "needs a category" alongside
        # real uncategorized spending.
        query += f" AND t.category IS NULL AND t.transaction_type NOT IN {TRANSFER_TYPES}"
    elif category:
        query += " AND t.category = ?"
        params.append(category)
    if review == "needs_review":
        query += " AND t.category_predicted = 1"
    if attributable in ("0", "1"):
        query += " AND t.attributable_to_user = ?"
        params.append(attributable)
    if q:
        query += " AND (t.description LIKE ? OR t.note LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    query += " ORDER BY t.date DESC, t.id DESC"
    return conn.execute(query, params).fetchall()
