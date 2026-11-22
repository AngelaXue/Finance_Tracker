import os
import re
import sqlite3
import sys
from datetime import date
from urllib.parse import urlencode

# `python app.py --demo` explores fake data in a separate database. Must be
# decided before src.paths is imported, since every path depends on it.
if "--demo" in sys.argv:
    os.environ["FINANCE_TRACKER_DEMO"] = "1"

from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename

from src import aggregates, categorize, fx, paths
from src.db import get_connection, init_db
from src.dedup import find_possible_duplicate, unique_dedup_hash
from src.maintenance import ACCOUNT_TYPES, OWNERS, create_account
from src.parsers import csv_importer, history_importer
from src.periods import resolve_period
from src.sankey import compute_sankey

# Git doesn't track empty directories, so a fresh clone won't have these —
# recreate them on startup so the app is self-healing on a new machine.
for _dir in (paths.DATA_DIR, paths.CONFIG_DIR, paths.HISTORY_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

# Same self-healing idea for the schema itself — a fresh/deleted database
# file has no tables yet; init_db() is idempotent (CREATE TABLE IF NOT
# EXISTS + migrations), so it's safe to call every startup.
init_db()

if paths.DEMO:
    from src.seed_demo import seed_demo_database
    _conn = get_connection()
    if not _conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
        seed_demo_database()
    _conn.close()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # uploads: statements + history files

# Pastel categorical palette: sky blue / peach / mint / soft yellow /
# bubblegum pink / lilac. Blue/peach/mint/pink/lilac are @pastel-palette/
# tailwindcss's "kawaii" hue steps lightened ~50% toward white; yellow is
# averaged from user-supplied reference hexes (#fff1ab, #ffe7b0 — weighted
# toward true warm yellow, skipping their more chartreuse/yellow-green
# examples #fdffcf/#f7fa98 since that hue drifts toward the reserved green
# below). This is lighter than the dataviz skill's validator allows (its
# lightness-band/chroma-floor/CVD checks target a more saturated pastel for
# guaranteed colorblind-safe separation) — accepted as a deliberate
# tradeoff since this is a private single-user app and every chart here
# already carries text labels/legends as a fallback. Red/green still
# excluded — reserved for gain/loss below so semantic meaning stays clear.
PASTEL_COLORS = ["#ACDCF7", "#F5D0B0", "#B8E7E7", "#FFECAE", "#FFC4E2", "#CED1FF"]

# Reserved gain/loss pair, same package's red/green families. Fills are
# lightened the same ~50% as the categorical set above (soft chart marks);
# text stays at the package's "hc" step — that one keeps real WCAG contrast
# on purpose, since delta/amount figures must stay legible as text.
STATUS_GOOD_FILL = "#B6E3C7"
STATUS_BAD_FILL = "#FCC3C4"
STATUS_GOOD_TEXT = "#008126"
STATUS_BAD_TEXT = "#D60009"

# transaction_type is a fixed structural enum — never free-text on the UI,
# since a typo here silently breaks Sankey/Spending math (it's what decides
# whether a row counts as real income/spending or gets excluded as a
# transfer). method is a free-text field with these as starting suggestions.
TRANSACTION_TYPES = ["income", "expense", "internal_transfer", "personal_transfer", "investment_contribution"]
# One-line explanations, shown as tooltips on the Type dropdown
TRANSACTION_TYPE_HINTS = {
    "income": "Money you earned or received that's yours (pay, interest, refunds)",
    "expense": "Money you spent",
    "internal_transfer": "Between your own accounts (checking → savings, paying off your card)",
    "personal_transfer": "Between you and someone else, but not income or spending (paying someone back, "
                         "money you're passing along)",
    "investment_contribution": "Money moved into an investment account",
}
METHOD_SUGGESTIONS = ["Cash", "Card", "Check", "Zelle", "Venmo", "PayPal", "ACH", "Wire"]
# The "CDs & Bonds" table (cds.kind) — a fixed list, so a typo can't make a
# near-duplicate type
FIXED_INCOME_KINDS = ["CD", "Treasury", "Corporate bond", "Municipal bond", "Other"]


def _pad_date(value):
    """'2026-1-9' -> '2026-01-09'. Every date filter compares dates as
    plain strings (BETWEEN, substr), so an unpadded month/day silently
    hides a row from the Transactions page. Anything not shaped like
    Y-M-D is returned unchanged."""
    if not isinstance(value, str):
        return value
    parts = value.strip().split("-")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    return value


# --- Request-parsing helpers shared by the JSON endpoints below ---

def _money(value):
    """'1,250' / '$1,250' / 1250 → 1250.0. Amounts are shown with commas
    everywhere (see _fmt_num), so every save path must accept them back.
    Raises ValueError/TypeError on anything that isn't a number."""
    return float(str(value).replace(",", "").replace("$", "").strip())


def _iso_date(value):
    """A padded 'YYYY-MM-DD' (see _pad_date), or None if it isn't one."""
    padded = _pad_date(str(value or "").strip())
    return padded if re.fullmatch(r"\d{4}-\d{2}-\d{2}", padded) else None


def _clean_note(value):
    """Optional free text: trimmed, blank → None (stored as NULL)."""
    return str(value or "").strip() or None


def _fail(message, conn=None, status=400):
    """JSON error response, closing the request's connection if one's open."""
    if conn is not None:
        conn.close()
    return jsonify(success=False, error=message), status


@app.context_processor
def inject_palette():
    return dict(
        PASTEL_COLORS=PASTEL_COLORS,
        STATUS_GOOD_FILL=STATUS_GOOD_FILL,
        STATUS_BAD_FILL=STATUS_BAD_FILL,
        STATUS_GOOD_TEXT=STATUS_GOOD_TEXT,
        STATUS_BAD_TEXT=STATUS_BAD_TEXT,
    )


@app.context_processor
def inject_settings():
    return dict(BASE_CURRENCY=fx.BASE_CURRENCY, BASE_SYMBOL=fx.BASE_SYMBOL, currency_symbol=fx.symbol,
                DEMO=paths.DEMO)


def _merge_query(overrides: dict) -> str:
    """Build a URL for the current page with only the given query params
    changed — needed because Overview has two independent period controls
    (the page-level one and the Net Worth widget's) that must not clobber
    each other's query params when either one navigates."""
    args = request.args.to_dict()
    args.update({k: v for k, v in overrides.items() if v is not None})
    return request.path + "?" + urlencode(args)


@app.context_processor
def inject_merge_query():
    return dict(merge_query=_merge_query)


def _fmt_money(value, symbol=None):
    """-1234.5 → '-$1,234.50' (symbol defaults to the base currency's)."""
    sign = "-" if value < 0 else ""
    return f"{sign}{fx.BASE_SYMBOL if symbol is None else symbol}{abs(value):,.2f}"


def _fmt_num(value):
    """1234567.8 -> '1,234,567.80' — money without a symbol (table cells,
    and the value of editable amount boxes; every save path strips the
    commas back out via _money)."""
    return f"{value:,.2f}"


@app.context_processor
def inject_fmt_money():
    return dict(fmt_money=_fmt_money, fmt_num=_fmt_num, FIXED_INCOME_KINDS=FIXED_INCOME_KINDS)


def _period_args(prefix="", default="month"):
    return (
        request.args.get(f"{prefix}period", default),
        request.args.get(f"{prefix}start"),
        request.args.get(f"{prefix}end"),
    )


@app.route("/")
def overview():
    conn = get_connection()

    # Page-level period: drives Money Allocation / Spending / Cash Flow only.
    preset, custom_start, custom_end = _period_args()
    start, end, label = resolve_period(preset, custom_start, custom_end)

    # Net Worth widget's own period, independent of the above — defaults to
    # the entire history ("All"), like a brokerage portfolio chart.
    nw_preset, nw_custom_start, nw_custom_end = _period_args(prefix="nw_", default="all")
    if nw_preset == "all":
        nw_start = aggregates.earliest_balance_date(conn) or date.today().isoformat()
        nw_end = date.today().isoformat()
        nw_label = "All"
    else:
        nw_start, nw_end, nw_label = resolve_period(nw_preset, nw_custom_start, nw_custom_end)

    # Excludes brokerage/retirement (see aggregates.py's include_investments
    # comment) so this headline number is a stable, day-to-day figure that
    # market swings don't move between manual snapshots — CDs held in a
    # savings account still count here. The full picture, investments
    # included, lives on the Net Worth page.
    net_worth = aggregates.net_worth_breakdown(conn, include_investments=False)
    trend = aggregates.net_worth_trend(conn, nw_start, nw_end, include_investments=False)
    sankey = compute_sankey(conn, start, end)
    spending, spending_total = aggregates.spending_by_category(conn, start, end)
    currency_balances = aggregates.balances_by_currency(conn)
    ious = aggregates.iou_balances(conn)

    debts = [r for r in net_worth["rows"] if r["is_liability"]]

    min_date = aggregates.earliest_transaction_date(conn)
    nw_min_date = aggregates.earliest_balance_date(conn)
    # First-run checklist: shown until there are accounts and transactions
    setup = dict(
        accounts=conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"],
        transactions=conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"],
        needs_review=conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE category_predicted = 1").fetchone()["n"],
    )

    conn.close()
    return render_template(
        "overview.html",
        net_worth=net_worth,
        trend=trend,
        sankey=sankey,
        spending=spending[:5],
        spending_total=spending_total,
        currency_balances=currency_balances,
        ious=ious,
        debts=debts,
        setup=setup,
        period=dict(preset=preset, start=start, end=end, label=label),
        nw_period=dict(preset=nw_preset, start=nw_start, end=nw_end, label=nw_label),
        min_date=min_date,
        nw_min_date=nw_min_date,
    )


@app.route("/net-worth")
def net_worth():
    conn = get_connection()
    breakdown = aggregates.net_worth_breakdown(conn)  # full picture, investments included
    cds = aggregates.list_cds(conn)
    investments = aggregates.investment_allocation(conn)
    cd_accounts = _cd_account_rows(conn)
    balance_accounts = conn.execute(
        "SELECT name FROM accounts WHERE is_active = 1 AND account_type IN ('checking', 'savings', 'loan', 'credit_card') "
        "ORDER BY name"
    ).fetchall()
    # Accounts someone else pays: listed separately, since they're left out
    other_accounts = conn.execute(
        "SELECT name, institution, account_type, currency, iou_person FROM accounts "
        "WHERE is_active = 1 AND owner = 'other' ORDER BY name"
    ).fetchall()
    people = aggregates.iou_people(conn)
    conn.close()

    type_to_group = {
        "checking": "Cash", "savings": "Cash", "digital_wallet": "Cash", "cash": "Cash",
        "brokerage": "Investments", "retirement": "Investments",
        "credit_card": "Credit", "loan": "Loans",
    }
    # Liability balances are stored as positive "amount owed." Flip the
    # sign for display so a liability reads as negative (like an asset
    # would) — that way "more owed" is a negative change (red) and "paid
    # down" is a positive change (green), the same color logic as assets,
    # instead of needing separate inverted logic just for this group.
    # One table per currency (the base currency first), each totalled in
    # its own currency — never blended. The only cross-currency figure is
    # the combined Net Worth line under the tables, converted to the base
    # currency via fx.to_base. The base currency always gets a table (it
    # may be empty); any other currency gets one only if an account uses it.
    def new_table(currency):
        return dict(currency=currency, label=f"{currency} accounts", symbol=fx.symbol(currency),
                    groups={"Cash": [], "Investments": [], "Credit": [], "Loans": []})

    tables = {fx.BASE_CURRENCY: new_table(fx.BASE_CURRENCY)}
    for row in sorted(breakdown["rows"], key=lambda r: r["account"]["currency"]):
        if row["is_liability"]:
            row["balance"] = -row["balance"]
            row["balance_base"] = -row["balance_base"]
            if row["change_30d"] is not None:
                row["change_30d"] = -row["change_30d"]
        currency = row["account"]["currency"]
        if currency not in tables:
            tables[currency] = new_table(currency)
        group = type_to_group.get(row["account"]["account_type"], "Cash")
        tables[currency]["groups"][group].append(row)

    for table in tables.values():
        table["group_totals"] = {}
        for name, rows in table["groups"].items():
            if not rows:
                continue
            has_change = any(r["change_30d"] is not None for r in rows)
            table["group_totals"][name] = dict(
                total=sum(r["balance"] for r in rows),
                change_30d=sum(r["change_30d"] for r in rows if r["change_30d"] is not None) if has_change else None,
            )
        table["total"] = sum(t["total"] for t in table["group_totals"].values())
        table["has_rows"] = bool(table["group_totals"])

    foreign = sorted(c for c in tables if c != fx.BASE_CURRENCY and tables[c]["has_rows"])
    return render_template(
        "net_worth.html", tables=list(tables.values()), breakdown=breakdown,
        cds=cds, investments=investments, cd_accounts=cd_accounts,
        account_types=ACCOUNT_TYPES, currencies=sorted(fx.RATES), owners=OWNERS,
        rates_used=[(c, fx.RATES[c]) for c in foreign], balance_accounts=balance_accounts,
        other_accounts=other_accounts, people=people,
    )


SET_BALANCE_TYPES = ("checking", "savings", "loan", "credit_card")


@app.route("/api/accounts/set-balance", methods=["POST"])
def set_account_balance():
    """Net Worth's Set Balance form: records "on this date, this account
    held exactly X" as a balances anchor. latest_balance() then uses it
    instead of summing every transaction, adding only transactions dated
    after it. Re-submitting the same account + date replaces that anchor.
    Brokerage/retirement go through the Investments page's entry form
    (it also tracks principal); cash/digital_wallet are always summed from
    transactions, so they take no anchor."""
    payload = request.get_json(silent=True) or {}
    account_name = (payload.get("account_name") or "").strip()
    date_val = _iso_date(payload.get("date"))
    if not account_name or not date_val:
        return _fail("account and date (YYYY-MM-DD) are required")
    try:
        balance = _money(payload.get("balance"))
    except (TypeError, ValueError):
        return _fail("balance must be a number")

    conn = get_connection()
    acct_row = conn.execute("SELECT id, account_type FROM accounts WHERE name = ?", (account_name,)).fetchone()
    if not acct_row:
        return _fail(f"unrecognized account name '{account_name}'", conn)
    if acct_row["account_type"] not in SET_BALANCE_TYPES:
        return _fail("Set Balance is for checking, savings, loan, and credit card accounts", conn)
    conn.execute(
        "INSERT INTO balances (account_id, date, balance) VALUES (?,?,?) "
        "ON CONFLICT(account_id, date) DO UPDATE SET balance = excluded.balance",
        (acct_row["id"], date_val, balance),
    )
    conn.commit()
    conn.close()
    return jsonify(success=True)


@app.route("/api/accounts/add", methods=["POST"])
def add_account():
    """Creates an account from the Net Worth page's Add Account form — the
    no-terminal alternative to `maintenance.py create-account`, sharing its
    validation. An optional starting balance + as-of date writes one
    balances snapshot: without it a checking/savings/loan account's balance
    is the sum of all its transactions (see latest_balance) — right only
    if the first one is the opening deposit. Not offered for
    digital_wallet/cash, whose balance is
    always the sum of their transactions."""
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    institution = (payload.get("institution") or "").strip()
    account_type = payload.get("account_type")
    currency = payload.get("currency") or fx.BASE_CURRENCY
    owner = payload.get("owner") or "self"
    iou_person = (payload.get("iou_person") or "").strip() or None
    if not name or not institution or not account_type:
        return _fail("name, institution, and type are required")
    if currency not in fx.RATES:
        return _fail(f"currency must be one of {sorted(fx.RATES)}")
    if owner not in OWNERS:
        return _fail(f"owner must be one of {', '.join(OWNERS)}")
    if owner == "other" and not iou_person:
        return _fail("an account someone else pays needs their name (\"Paid by\")")

    balance = payload.get("starting_balance")
    balance_date = None
    if str(balance or "").strip():
        try:
            balance = _money(balance)
        except (TypeError, ValueError):
            return _fail("starting balance must be a number")
        if account_type in aggregates.COMPUTED_BALANCE_TYPES:
            return _fail("cash/digital wallet balances come from their transactions — "
                         "log the opening amount as a transaction instead")
        balance_date = _iso_date(payload.get("balance_date"))
        if not balance_date:
            return _fail("a starting balance needs an as-of date (YYYY-MM-DD)")
    else:
        balance = None

    conn = get_connection()
    try:
        result = create_account(conn, name, institution=institution, account_type=account_type,
                                currency=currency, owner=owner, iou_person=iou_person, apply=True)
    except ValueError as exc:
        return _fail(str(exc), conn)
    if result["already_existed"]:
        return _fail(f"an account named '{name}' already exists", conn)
    if balance is not None:
        account_id = conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()["id"]
        conn.execute("INSERT INTO balances (account_id, date, balance) VALUES (?,?,?)",
                     (account_id, balance_date, balance))
        conn.commit()
    csv_importer.ensure_account_folders(conn)  # its statements folder
    conn.close()
    return jsonify(success=True)


@app.route("/cash-flow")
def cash_flow():
    conn = get_connection()
    preset, custom_start, custom_end = _period_args()
    start, end, label = resolve_period(preset, custom_start, custom_end)
    sankey = compute_sankey(conn, start, end)
    min_date = aggregates.earliest_transaction_date(conn)
    conn.close()

    investing = next((d["amount"] for d in sankey["destinations"] if d["label"] == "Investing"), 0.0)
    cash_savings = next((d["amount"] for d in sankey["destinations"] if d["label"] == "Cash Savings"), 0.0)
    living_expenses = next((d["amount"] for d in sankey["destinations"] if d["label"] == "Living Expenses"), 0.0)
    saved_invested = investing + cash_savings
    savings_rate = (saved_invested / sankey["income_total"] * 100) if sankey["income_total"] else 0.0

    return render_template(
        "cash_flow.html",
        sankey=sankey,
        living_expenses=living_expenses,
        saved_invested=saved_invested,
        savings_rate=savings_rate,
        period=dict(preset=preset, start=start, end=end, label=label),
        min_date=min_date,
    )


@app.route("/spending")
def spending():
    conn = get_connection()
    preset, custom_start, custom_end = _period_args()
    start, end, label = resolve_period(preset, custom_start, custom_end)

    include_pass_through = request.args.get("pass_through") == "include"
    breakdown, total = aggregates.spending_by_category(conn, start, end, include_pass_through)
    for row in breakdown:
        row["mom_prior"] = aggregates.spending_mom_change(conn, row["category"], start, end)
        row["mom_change"] = row["amount"] - row["mom_prior"]
        row["mom_pct"] = (row["mom_change"] / row["mom_prior"] * 100) if row["mom_prior"] else None
        row["sparkline"] = aggregates.category_sparkline(conn, row["category"])

    min_date = aggregates.earliest_transaction_date(conn)
    conn.close()
    return render_template(
        "spending.html",
        breakdown=breakdown,
        total=total,
        include_pass_through=include_pass_through,
        period=dict(preset=preset, start=start, end=end, label=label),
        min_date=min_date,
    )


def _usual_value_by_description(conn, column):
    """description -> its most-used non-empty value of `column` (ties go to
    the most recent). `column` is a fixed name from this module, never
    user input."""
    assert column in ("category", "method", "transaction_type")
    rows = conn.execute(
        f"""
        WITH counts AS (
            SELECT description, {column} AS value, COUNT(*) AS n, MAX(date) AS latest
            FROM transactions
            WHERE description IS NOT NULL AND {column} IS NOT NULL AND TRIM({column}) != ''
              AND category_predicted = 0
            GROUP BY description, {column}
        ), ranked AS (
            SELECT description, value,
                   ROW_NUMBER() OVER (PARTITION BY description ORDER BY n DESC, latest DESC) AS rn
            FROM counts
        )
        SELECT description, value FROM ranked WHERE rn = 1
        """
    ).fetchall()
    return {r["description"]: r["value"] for r in rows}


@app.route("/transactions")
def transactions():
    conn = get_connection()

    # Default window: current calendar month, matching every other page's
    # default period (per CLAUDE.md) — an unbounded default was rendering
    # the entire transaction history in one page load. "All" opts back
    # into the old unbounded behavior explicitly; the custom range picker
    # covers everything in between.
    preset, custom_start, custom_end = _period_args(default="month")
    if preset == "all":
        period_start = aggregates.earliest_transaction_date(conn) or date.today().isoformat()
        period_end = date.today().isoformat()
        period_label = "All"
    else:
        period_start, period_end, period_label = resolve_period(preset, custom_start, custom_end)

    year = request.args.get("year")
    month = request.args.get("month")
    account_id = request.args.get("account_id")
    category = request.args.get("category")
    q = request.args.get("q")
    review = request.args.get("review")
    attributable = request.args.get("attributable")

    # The year/month dropdown filters are a separate, more specific date
    # filter than the period selector — when either is set, they take over
    # date-narrowing entirely rather than ANDing with the period's range,
    # which could otherwise silently produce zero rows (e.g. period
    # defaults to "This Month" in 2026, but year=2023 is picked).
    effective_start = None if (year or month) else period_start
    effective_end = None if (year or month) else period_end

    iou = request.args.get("iou")

    rows = aggregates.fetch_transactions(conn, year, month, account_id, category, q, effective_start, effective_end, review, attributable)
    # Computed here rather than in SQL — the automatic rule lives in one
    # place (aggregates.iou_status), shared with the IOUs page.
    iou_statuses = {}
    for r in rows:
        kind, person, reason = aggregates.iou_status(
            r["account_owner"], r["account_person"], r["transaction_type"], r["amount"],
            r["attributable_to_user"], r["iou_kind"], r["iou_person"],
        )
        if kind:
            iou_statuses[r["id"]] = dict(kind=kind, person=person, reason=reason)
    if iou in aggregates.IOU_KINDS:
        rows = [r for r in rows if iou_statuses.get(r["id"], {}).get("kind") == iou]
    elif iou == "any":
        rows = [r for r in rows if r["id"] in iou_statuses]
    filtered_total = sum(r["amount"] for r in rows)
    min_date = aggregates.earliest_transaction_date(conn)
    running_balances = aggregates.running_balances_by_account(conn)
    accounts = conn.execute("SELECT id, name FROM accounts ORDER BY name").fetchall()
    categories = [r["category"] for r in conn.execute(
        "SELECT DISTINCT category FROM transactions WHERE category IS NOT NULL ORDER BY category"
    ).fetchall()]
    years = [r["y"] for r in conn.execute(
        "SELECT DISTINCT substr(date, 1, 4) AS y FROM transactions ORDER BY y DESC"
    ).fetchall()]
    used_methods = [r["method"] for r in conn.execute(
        "SELECT DISTINCT method FROM transactions WHERE method IS NOT NULL ORDER BY method"
    ).fetchall()]
    methods = sorted(set(METHOD_SUGGESTIONS) | set(used_methods))
    # Autocomplete for the Description cell: most-used first, then most
    # recent, capped so the page stays light on a long history.
    descriptions = [r["description"] for r in conn.execute(
        "SELECT description FROM transactions WHERE description IS NOT NULL AND TRIM(description) != '' "
        "GROUP BY description ORDER BY COUNT(*) DESC, MAX(date) DESC LIMIT 3000"
    ).fetchall()]
    # Picking a suggested description also fills that row's empty Category/
    # Method (and a new row's untouched Type) with what's most often been
    # used for it. Unreviewed classifier guesses are skipped so a wrong
    # guess can't spread.
    suggested = set(descriptions)
    usual = {
        column: {d: v for d, v in _usual_value_by_description(conn, column).items() if d in suggested}
        for column in ("category", "method", "transaction_type")
    }
    description_defaults = {
        d: {column: values.get(d) for column, values in usual.items()}
        for d in suggested if any(d in values for values in usual.values())
    }
    # A new row starts on the account being filtered to, else blank.
    default_account = next((a["name"] for a in accounts if str(a["id"]) == account_id), "")
    needs_review_count = conn.execute(
        "SELECT COUNT(*) AS c FROM transactions WHERE category_predicted = 1"
    ).fetchone()["c"]
    people = aggregates.iou_people(conn)

    conn.close()
    return render_template(
        "transactions.html",
        rows=rows,
        filtered_total=filtered_total,
        running_balances=running_balances,
        iou_statuses=iou_statuses,
        people=people,
        type_hints=TRANSACTION_TYPE_HINTS,
        accounts=accounts,
        default_account=default_account,
        categories=categories,
        years=years,
        methods=methods,
        suggestions=dict(accounts=[a["name"] for a in accounts], descriptions=descriptions,
                         categories=categories, defaults=description_defaults),
        transaction_types=TRANSACTION_TYPES,
        needs_review_count=needs_review_count,
        filters=dict(year=year, month=month, account_id=account_id, category=category, q=q, review=review, attributable=attributable, iou=iou),
        period=dict(preset=preset, start=period_start, end=period_end, label=period_label),
        min_date=min_date,
    )


@app.route("/api/transactions/suggest-categories", methods=["POST"])
def suggest_categories():
    conn = get_connection()
    result = categorize.predict_uncategorized(conn)
    conn.close()
    return jsonify(success=True, **result)


@app.route("/api/transactions/save", methods=["POST"])
def save_transactions():
    payload = request.get_json(silent=True) or {}
    edits = payload.get("edits", [])
    confirmed_ids = payload.get("confirmed_ids", [])
    new_rows = payload.get("new_rows", [])
    conn = get_connection()
    skipped = []
    newly_flagged = []

    for edit in edits:
        txn_id = edit.get("id")
        current = conn.execute(
            "SELECT date, account_id, amount, currency, category, description, note, method, "
            "transaction_type, manually_corrected, original_category FROM transactions WHERE id = ?",
            (txn_id,),
        ).fetchone()
        if not current:
            continue
        if "date" in edit:
            edit["date"] = _pad_date(edit["date"])

        sets, params = [], []
        final_account_id = current["account_id"]
        balance_invalidated = False

        if "category" in edit:
            new_category = edit["category"] or None
            if new_category != current["category"]:
                original = current["original_category"] if current["manually_corrected"] else current["category"]
                sets += ["category = ?", "manually_corrected = 1", "original_category = ?", "category_predicted = 0"]
                params += [new_category, original]

        if "description" in edit and edit["description"] != current["description"]:
            sets.append("description = ?")
            params.append(edit["description"])

        if "note" in edit and edit["note"] != current["note"]:
            sets.append("note = ?")
            params.append(edit["note"])

        if "method" in edit:
            new_method = edit["method"] or None
            if new_method != current["method"]:
                sets.append("method = ?")
                params.append(new_method)

        if "transaction_type" in edit and edit["transaction_type"] in TRANSACTION_TYPES \
                and edit["transaction_type"] != current["transaction_type"]:
            sets.append("transaction_type = ?")
            params.append(edit["transaction_type"])

        if "date" in edit and edit["date"] != current["date"]:
            sets.append("date = ?")
            params.append(edit["date"])
            balance_invalidated = True

        if "account_name" in edit:
            acct_row = conn.execute("SELECT id, currency FROM accounts WHERE name = ?", (edit["account_name"],)).fetchone()
            if acct_row is None:
                skipped.append({"id": txn_id, "reason": f"unrecognized account name '{edit['account_name']}'"})
            elif acct_row["id"] != current["account_id"]:
                final_account_id = acct_row["id"]
                sets += ["account_id = ?", "currency = ?"]
                params += [acct_row["id"], acct_row["currency"]]
                balance_invalidated = True

        if "amount" in edit:
            try:
                new_amount = _money(edit["amount"])
            except (TypeError, ValueError):
                new_amount = current["amount"]
            if new_amount != current["amount"]:
                sets.append("amount = ?")
                params.append(new_amount)
                balance_invalidated = True

        # dedup_hash is derived from account/date/amount/description — if
        # any of those changed, it has to be recomputed or the row's hash
        # goes stale. statement_balance is the real recorded balance for
        # the ORIGINAL row — once date/account/amount changes, that
        # recorded value no longer corresponds to this row, so it's
        # cleared back to NULL (falls back to a computed running balance
        # instead of showing a now-meaningless number).
        #
        # An edit that makes this row look like a duplicate of another
        # (same account/date/amount, exactly or by description) is never
        # blocked — same reasoning as new rows: it's genuinely ambiguous
        # whether that's really a duplicate or a second real transaction,
        # so it's saved and flagged for review instead (exclude_id=txn_id
        # so it never trivially "collides" with its own unchanged self).
        flagged_duplicate_value = 0
        if sets and (balance_invalidated or "description" in edit):
            final_date = edit.get("date", current["date"])
            final_amount = _money(edit.get("amount", current["amount"]))
            final_description = edit.get("description", current["description"])
            new_hash, exact_collision = unique_dedup_hash(
                conn, final_account_id, final_date, final_amount, final_description or "", exclude_id=txn_id,
            )
            possible_dup = find_possible_duplicate(conn, final_account_id, final_date, final_amount, exclude_id=txn_id) is not None
            sets.append("dedup_hash = ?")
            params.append(new_hash)
            if exact_collision or possible_dup:
                flagged_duplicate_value = 1
        if balance_invalidated:
            sets.append("statement_balance = NULL")

        if sets:
            sets.append("flagged_duplicate = ?")
            params.append(flagged_duplicate_value)
            if flagged_duplicate_value:
                newly_flagged.append(txn_id)

        if not sets:
            continue
        params.append(txn_id)
        conn.execute(f"UPDATE transactions SET {', '.join(sets)} WHERE id = ?", params)

    for txn_id in confirmed_ids:
        conn.execute("UPDATE transactions SET category_predicted = 0, flagged_duplicate = 0 WHERE id = ?", (txn_id,))

    created = 0
    for row in new_rows:
        try:
            row_date = _pad_date(row["date"])
            amount = _money(row["amount"])
        except (KeyError, TypeError, ValueError):
            skipped.append({"id": None, "reason": "new row missing date/amount"})
            continue
        if not (row.get("account_name") or "").strip():
            skipped.append({"id": None, "reason": "new row has no account"})
            continue
        acct_row = conn.execute("SELECT id, currency FROM accounts WHERE name = ?", (row.get("account_name"),)).fetchone()
        if not acct_row:
            skipped.append({"id": None, "reason": f"new row has an unrecognized account name '{row.get('account_name')}'"})
            continue
        account_id = acct_row["id"]
        description = row.get("description") or None
        transaction_type = row.get("transaction_type") if row.get("transaction_type") in TRANSACTION_TYPES else "expense"

        # A collision here is genuinely ambiguous — could be an accidental
        # double-click, could be a second real transaction that happens to
        # share every field (e.g. two bus rides the same day, same fare,
        # same description). Never silently drop the second one: always
        # insert, flagging it for review instead (same mechanism as
        # csv_importer.py's flagged_duplicate).
        dedup_hash, exact_collision = unique_dedup_hash(conn, account_id, row_date, amount, description or "")
        possible_dup = find_possible_duplicate(conn, account_id, row_date, amount) is not None
        flagged = exact_collision or possible_dup

        cursor = conn.execute(
            "INSERT INTO transactions "
            "(account_id, date, amount, currency, category, transaction_type, description, note, method, "
            "source, dedup_hash, flagged_duplicate) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (account_id, row_date, amount, acct_row["currency"], row.get("category") or None, transaction_type,
             description, row.get("note") or None, row.get("method") or None, "manual", dedup_hash, int(flagged)),
        )
        created += 1
        if flagged:
            newly_flagged.append(cursor.lastrowid)

    conn.commit()
    conn.close()
    return jsonify(
        success=True, count=len(edits), confirmed=len(confirmed_ids), created=created,
        skipped=skipped, newly_flagged=newly_flagged,
    )


@app.route("/api/transactions/set-attributable", methods=["POST"])
def set_attributable():
    """Sets attributable_to_user to an explicit value (1 = Mine, 0 = Not
    Mine) for each given transaction — the Transactions page's "Mark Mine"
    / "Mark Not Mine" buttons, reusing the existing row-selection
    checkboxes. Explicit rather than a flip, so selecting a mix of rows
    can't turn some of them the wrong way. See db.py's schema comment for
    what the flag means."""
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids", [])
    value = payload.get("value")
    if value not in (0, 1):
        return _fail("value must be 0 or 1")
    conn = get_connection()
    conn.executemany(
        "UPDATE transactions SET attributable_to_user = ? WHERE id = ?",
        [(value, txn_id) for txn_id in ids],
    )
    conn.commit()
    conn.close()
    return jsonify(success=True, updated=len(ids))


@app.route("/api/transactions/set-iou", methods=["POST"])
def set_transaction_iou():
    """The Transactions page's IOU… menu: overrides whether the given
    transactions count on the IOUs page. value: 'they_paid' / 'i_paid'
    (with `person`, required), 'none' (not an IOU), or 'auto' (back to the
    automatic rule, aggregates.iou_status)."""
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids", [])
    value = payload.get("value")
    person = (payload.get("person") or "").strip() or None
    if value not in aggregates.IOU_OVERRIDES + ("auto",):
        return _fail("value must be they_paid, i_paid, none, or auto")
    if value in aggregates.IOU_KINDS and not person:
        return _fail("say who — the person's name is required")
    kind = None if value == "auto" else value
    stored_person = person if value in aggregates.IOU_KINDS else None
    conn = get_connection()
    conn.executemany(
        "UPDATE transactions SET iou_kind = ?, iou_person = ? WHERE id = ?",
        [(kind, stored_person, txn_id) for txn_id in ids],
    )
    conn.commit()
    conn.close()
    return jsonify(success=True, updated=len(ids))


@app.route("/api/transactions/delete", methods=["POST"])
def delete_transactions():
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids", [])
    conn = get_connection()
    deleted, blocked = 0, []
    for txn_id in ids:
        try:
            cursor = conn.execute("DELETE FROM transactions WHERE id = ?", (txn_id,))
            if cursor.rowcount:
                deleted += 1
        except sqlite3.IntegrityError:
            blocked.append(txn_id)
    conn.commit()
    conn.close()
    return jsonify(success=True, deleted=deleted, blocked=blocked)


def _cd_account_rows(conn):
    """Accounts a CD can live in — shared by Net Worth and Investments, so
    the CD table edits the same way on both pages."""
    return conn.execute(
        "SELECT name FROM accounts WHERE is_active = 1 AND owner IN ('self', 'joint') "
        "AND account_type IN ('savings', 'brokerage', 'retirement') ORDER BY name"
    ).fetchall()


@app.route("/api/investments/assign", methods=["POST"])
def assign_investment():
    """Investments page: say which account an investment transaction went
    to, or that it isn't an investment. Body: {"assignments":
    [{"id", "target"}, ...]} (the staged Save), or {"ids": [...], "target"}
    for one target. target = an investment account id, 'none', or 'auto'
    (back to note/description matching). Validated all-or-nothing. Only
    sets transactions.investment_account — Type is untouched."""
    payload = request.get_json(silent=True) or {}
    assignments = payload.get("assignments")
    if assignments is None:
        assignments = [{"id": i, "target": payload.get("target")} for i in payload.get("ids") or []]
    if not assignments or not all(isinstance(a, dict) and isinstance(a.get("id"), int) for a in assignments):
        return _fail("no transactions selected")
    conn = get_connection()
    investment_ids = {str(i) for i in aggregates.investment_accounts_by_name(conn).values()}
    values = []
    for a in assignments:
        target = str(a.get("target") or "").strip()
        if target == "auto":
            values.append((None, a["id"]))
        elif target == "none" or target in investment_ids:
            values.append((target, a["id"]))
        else:
            return _fail("pick an investment account, 'none', or 'auto'", conn)
    updated = sum(
        conn.execute("UPDATE transactions SET investment_account = ? WHERE id = ?", v).rowcount for v in values
    )
    conn.commit()
    conn.close()
    return jsonify(success=True, updated=updated)


def _parse_entry_edits(conn, edits, amount_field, amount_label, exists):
    """Shared by the Investments page's two All-entries edit endpoints
    (current values in `balances`, principal added in
    `investment_deposits`). Each edit is {id, <changed fields>}: date,
    account_name, `amount_field`, note. Returns (updates, error) —
    validates every edit before anything is written, so a save is all or
    nothing."""
    accounts = aggregates.investment_accounts_by_name(conn)
    updates = []
    for edit in edits:
        row_id = edit.get("id")
        if not isinstance(row_id, int) or not exists(row_id):
            return None, "entry not found — reload the page"
        columns = {}
        if "date" in edit:
            columns["date"] = _iso_date(edit["date"])
            if not columns["date"]:
                return None, "date must be YYYY-MM-DD"
        if "account_name" in edit:
            if edit["account_name"] not in accounts:
                return None, f"'{edit['account_name']}' isn't an investment account"
            columns["account_id"] = accounts[edit["account_name"]]
        if amount_field in edit:
            try:
                columns[amount_field] = _money(edit[amount_field])
            except (TypeError, ValueError):
                return None, f"{amount_label} must be a number"
        if "note" in edit:
            columns["note"] = _clean_note(edit["note"])
        if columns:
            updates.append((row_id, columns))
    return updates, None


def _apply_updates(conn, table, updates):
    """UPDATE each (id, {column: value}). Table and column names only ever
    come from this module's code, never from the request."""
    for row_id, columns in updates:
        assignments = ", ".join(f"{column} = ?" for column in columns)
        conn.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", (*columns.values(), row_id))


@app.route("/api/balances/update", methods=["POST"])
def update_investment_snapshots():
    """Investments page, All entries: edit current-value rows (brokerage/
    retirement snapshots only — bank balances are never touched from
    here). Moving one onto a date its account already has is refused."""
    edits = (request.get_json(silent=True) or {}).get("edits") or []
    if not edits:
        return _fail("nothing to save")
    conn = get_connection()

    def exists(snap_id):
        return conn.execute(
            "SELECT 1 FROM balances b JOIN accounts a ON a.id = b.account_id "
            "WHERE b.id = ? AND a.account_type IN ('brokerage', 'retirement')", (snap_id,)
        ).fetchone() is not None

    updates, error = _parse_entry_edits(conn, edits, "balance", "current value", exists)
    if error:
        return _fail(error, conn)
    try:
        _apply_updates(conn, "balances", updates)
    except sqlite3.IntegrityError:
        conn.rollback()
        return _fail("that account already has a current value on that date", conn)
    conn.commit()
    conn.close()
    return jsonify(success=True, updated=len(updates))


@app.route("/api/balances/delete", methods=["POST"])
def delete_investment_snapshots():
    """Investments page, All entries: delete current-value rows (brokerage/
    retirement only — bank balances are never touched from here)."""
    ids = [i for i in (request.get_json(silent=True) or {}).get("ids") or [] if isinstance(i, int)]
    if not ids:
        return _fail("nothing selected")
    conn = get_connection()
    deleted = sum(
        conn.execute(
            "DELETE FROM balances WHERE id = ? AND account_id IN "
            "(SELECT id FROM accounts WHERE account_type IN ('brokerage', 'retirement'))", (i,)
        ).rowcount
        for i in ids
    )
    conn.commit()
    conn.close()
    return jsonify(success=True, deleted=deleted)


@app.route("/api/investments/guesses")
def investment_guesses():
    """Where each listed transaction looks like it went, judged from its
    current note/description — so the Investments page can pre-fill its
    dropdowns after a note is edited on Transactions. Read-only."""
    wanted = {int(i) for i in (request.args.get("ids") or "").split(",") if i.strip().isdigit()}
    conn = get_connection()
    contributions = aggregates.investment_contributions(conn)
    conn.close()
    return jsonify({
        str(c["id"]): dict(
            guess=c["guess_id"] if c["transaction_type"] == "investment_contribution" else None,
            transaction_type=c["transaction_type"],
        )
        for c in contributions if c["id"] in wanted
    })


def _investment_period(conn, prefix=""):
    """The Investments page's period, from `<prefix>period/start/end`. None
    for an account's own selector that hasn't been touched — it follows
    the page-level one."""
    if prefix and not request.args.get(f"{prefix}period"):
        return None
    preset, custom_start, custom_end = _period_args(prefix=prefix, default="all")
    if preset == "all":
        start = aggregates.earliest_investment_balance_date(conn) or date.today().isoformat()
        end, label = date.today().isoformat(), "All"
    else:
        start, end, label = resolve_period(preset, custom_start, custom_end)
    return dict(preset=preset, start=start, end=end, label=label)


@app.route("/investments")
def investments():
    conn = get_connection()

    period = _investment_period(conn)
    inv_start, inv_end = period["start"], period["end"]

    summary = aggregates.investment_summary(conn)
    trend = aggregates.investment_trend(conn, inv_start, inv_end)
    fixed_income = aggregates.fixed_income_trend(conn, inv_start, inv_end)
    allocation = aggregates.investment_allocation(conn)
    min_date = aggregates.earliest_investment_balance_date(conn)
    investment_accounts = conn.execute(
        "SELECT id, name FROM accounts WHERE account_type IN ('brokerage', 'retirement') "
        "AND is_active = 1 ORDER BY name"
    ).fetchall()
    # One identically-shaped section per account (Roth IRA, Individual, ...)
    contributions = aggregates.investment_contributions(conn)
    deposits = aggregates.manual_deposits(conn)
    all_cds = aggregates.list_cds(conn)["cds"]
    sections = []
    for acct in investment_accounts:
        current = aggregates.latest_balance(conn, acct["id"])
        cds = [cd for cd in all_cds if cd["account_id"] == acct["id"]]
        # Each account's chart has its own date row; untouched, it follows the top one
        prefix = f"a{acct['id']}_"
        acct_period = _investment_period(conn, prefix) or period
        sections.append(dict(
            id=acct["id"],
            name=acct["name"],
            prefix=prefix,
            period=acct_period,
            summary=aggregates.investment_summary(conn, [acct["id"]]),
            trend=aggregates.investment_trend(conn, acct_period["start"], acct_period["end"], [acct["id"]]),
            fixed_income=aggregates.fixed_income_trend(conn, acct_period["start"], acct_period["end"], [acct["id"]]),
            snapshot_date=current.get("snapshot_date") if current else None,
            added_since_snapshot=current.get("added_since_snapshot", 0.0) if current else 0.0,
            contribution_count=sum(1 for c in contributions if c["account_id"] == acct["id"]),
            deposit_count=sum(1 for d in deposits if d["account_id"] == acct["id"]),
            cds=cds,
            cds_total=sum(cd["principal"] for cd in cds),
        ))
    needs_attention = [c for c in contributions if c["status"] in ("unmatched", "ambiguous", "wrong_type")]
    entries = aggregates.investment_entries(conn, contributions)
    cd_accounts = _cd_account_rows(conn)
    conn.close()
    return render_template(
        "investments.html",
        summary=summary,
        trend=trend,
        fixed_income=fixed_income,
        allocation=allocation,
        sections=sections,
        needs_attention=needs_attention,
        entries=entries,
        cd_accounts=cd_accounts,
        period=period,
        min_date=min_date,
        investment_accounts=investment_accounts,
    )


@app.route("/api/investments/entry", methods=["POST"])
def add_investment_entry():
    """The Investments page's Add a Snapshot form: account + date, then
    current value and/or principal added — at least one — plus an optional
    note saved on whichever rows it creates. Current value writes a
    balances snapshot (same account + date replaces it); principal added
    writes an investment_deposits row (new money in, counted like a matched
    contribution transaction). Principal added that looks like a deposit
    already logged on Transactions (same account, same amount, within 5
    days) is refused with needs_confirm (409) until resent with
    confirm_duplicate — otherwise that money would count twice."""
    payload = request.get_json(silent=True) or {}
    account_name = (payload.get("account_name") or "").strip()
    date_val = _iso_date(payload.get("date"))
    if not account_name or not date_val:
        return _fail("account and date (YYYY-MM-DD) are required")

    raw_value = str(payload.get("balance") or "").strip()
    raw_added = str(payload.get("principal_added") or "").strip()
    if not raw_value and not raw_added:
        return _fail("enter a current value, principal added, or both")
    try:
        balance = _money(raw_value) if raw_value else None
    except (TypeError, ValueError):
        return _fail("current value must be a number")
    try:
        added = _money(raw_added) if raw_added else None
    except (TypeError, ValueError):
        return _fail("principal added must be a number")
    if added == 0:
        return _fail("principal added can't be 0")

    conn = get_connection()
    account_id = aggregates.investment_accounts_by_name(conn).get(account_name)
    if account_id is None:
        return _fail("pick a brokerage/retirement account", conn)

    if added is not None and not payload.get("confirm_duplicate"):
        target = date.fromisoformat(date_val)
        looks_logged = any(
            c["account_id"] == account_id and abs(c["amount"] - added) < 0.005
            and abs((date.fromisoformat(c["date"]) - target).days) <= 5
            for c in aggregates.investment_contributions(conn) if c["status"] == "matched"
        )
        if looks_logged:
            conn.close()
            return jsonify(success=False, needs_confirm=True, error=(
                "A deposit of this amount to this account is already logged on Transactions within 5 days "
                "of this date. Adding it here too would count it twice."
            )), 409

    note = _clean_note(payload.get("note"))
    if balance is not None:
        conn.execute(
            "INSERT INTO balances (account_id, date, balance, note) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(account_id, date) DO UPDATE SET balance = excluded.balance, "
            "note = COALESCE(excluded.note, balances.note)",
            (account_id, date_val, balance, note),
        )
    if added is not None:
        conn.execute(
            "INSERT INTO investment_deposits (account_id, date, amount, note) VALUES (?, ?, ?, ?)",
            (account_id, date_val, added, note),
        )
    conn.commit()
    conn.close()
    return jsonify(success=True)


@app.route("/api/investments/deposits/update", methods=["POST"])
def update_investment_deposits():
    """Investments page, All entries: edit principal-added rows."""
    edits = (request.get_json(silent=True) or {}).get("edits") or []
    if not edits:
        return _fail("nothing to save")
    conn = get_connection()

    def exists(dep_id):
        return conn.execute("SELECT 1 FROM investment_deposits WHERE id = ?", (dep_id,)).fetchone() is not None

    updates, error = _parse_entry_edits(conn, edits, "amount", "principal added", exists)
    if error:
        return _fail(error, conn)
    _apply_updates(conn, "investment_deposits", updates)
    conn.commit()
    conn.close()
    return jsonify(success=True, updated=len(updates))


@app.route("/api/investments/deposits/delete", methods=["POST"])
def delete_investment_deposits():
    """Investments page, All entries: delete principal-added rows."""
    ids = [i for i in (request.get_json(silent=True) or {}).get("ids") or [] if isinstance(i, int)]
    if not ids:
        return _fail("nothing selected")
    conn = get_connection()
    deleted = sum(conn.execute("DELETE FROM investment_deposits WHERE id = ?", (i,)).rowcount for i in ids)
    conn.commit()
    conn.close()
    return jsonify(success=True, deleted=deleted)


def _parse_apy(value):
    """'4.5' / '4.5%' → 4.5; blank → None. Raises ValueError otherwise."""
    value = str(value or "").strip().rstrip("%").strip()
    return float(value) if value else None


def _optional_date(value):
    """Blank → None; otherwise a valid YYYY-MM-DD or ValueError."""
    if not str(value or "").strip():
        return None
    parsed = _iso_date(value)
    if not parsed:
        raise ValueError("date must be YYYY-MM-DD")
    return parsed


@app.route("/api/cds/add", methods=["POST"])
def add_cd():
    payload = request.get_json(silent=True) or {}
    account_name = (payload.get("account_name") or "").strip()
    institution = (payload.get("institution") or "").strip()
    if not account_name or not institution:
        return _fail("account and institution are required")
    try:
        principal = _money(payload.get("principal"))
    except (TypeError, ValueError):
        return _fail("principal must be a number")
    try:
        apy = _parse_apy(payload.get("apy"))
    except ValueError:
        return _fail("APY must be a number (or blank)")
    try:
        opened, maturity = _optional_date(payload.get("opened_date")), _optional_date(payload.get("maturity_date"))
    except ValueError:
        return _fail("dates must be YYYY-MM-DD (or blank)")
    kind = payload.get("kind") or "CD"
    if kind not in FIXED_INCOME_KINDS:
        return _fail(f"type must be one of {', '.join(FIXED_INCOME_KINDS)}")

    conn = get_connection()
    acct_row = conn.execute("SELECT id FROM accounts WHERE name = ?", (account_name,)).fetchone()
    if not acct_row:
        return _fail(f"unrecognized account name '{account_name}'", conn)

    conn.execute(
        "INSERT INTO cds (account_id, institution, principal, apy, opened_date, maturity_date, note, kind) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (acct_row["id"], institution, principal, apy, opened, maturity, _clean_note(payload.get("note")), kind),
    )
    conn.commit()
    conn.close()
    return jsonify(success=True)


@app.route("/api/cds/delete", methods=["POST"])
def delete_cds():
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids", [])
    conn = get_connection()
    deleted = 0
    for cd_id in ids:
        cursor = conn.execute("DELETE FROM cds WHERE id = ?", (cd_id,))
        deleted += cursor.rowcount
    conn.commit()
    conn.close()
    return jsonify(success=True, deleted=deleted)


CD_EDITABLE_FIELDS = ("kind", "account_name", "institution", "principal", "apy", "opened_date", "maturity_date", "note")


@app.route("/api/cds/update", methods=["POST"])
def update_cds():
    """Saves the CD table's inline edits — {edits: [{id, <field>: value}]},
    only the fields that changed. Same validation as /api/cds/add. All or
    nothing: one bad value rejects the whole save, so the page never ends
    up half-saved with the error pointing at the wrong row."""
    payload = request.get_json(silent=True) or {}
    edits = payload.get("edits", [])
    conn = get_connection()
    updates = []
    for edit in edits:
        cd_id = edit.get("id")
        if not conn.execute("SELECT 1 FROM cds WHERE id = ?", (cd_id,)).fetchone():
            return _fail(f"CD {cd_id} no longer exists — reload the page", conn)
        columns = {}
        for field in CD_EDITABLE_FIELDS:
            if field not in edit:
                continue
            value = edit[field]
            value = value.strip() if isinstance(value, str) else value
            if field == "kind":
                if value not in FIXED_INCOME_KINDS:
                    return _fail(f"type must be one of {', '.join(FIXED_INCOME_KINDS)}", conn)
                columns["kind"] = value
            elif field == "account_name":
                acct_row = conn.execute("SELECT id FROM accounts WHERE name = ?", (value,)).fetchone()
                if not acct_row:
                    return _fail(f"unrecognized account name '{value}'", conn)
                columns["account_id"] = acct_row["id"]
            elif field == "institution":
                if not value:
                    return _fail("institution can't be blank", conn)
                columns["institution"] = value
            elif field == "principal":
                try:
                    columns["principal"] = _money(value)
                except (TypeError, ValueError):
                    return _fail("principal must be a number", conn)
            elif field == "apy":
                try:
                    columns["apy"] = _parse_apy(value)
                except ValueError:
                    return _fail("APY must be a number (or blank)", conn)
            elif field in ("opened_date", "maturity_date"):
                try:
                    columns[field] = _optional_date(value)
                except ValueError:
                    return _fail("dates must be YYYY-MM-DD (or blank)", conn)
            else:  # note
                columns["note"] = _clean_note(value)
        if columns:
            updates.append((cd_id, columns))

    _apply_updates(conn, "cds", updates)
    conn.commit()
    conn.close()
    return jsonify(success=True, updated=len(updates))


@app.route("/ious")
def ious():
    """One ledger per person (see aggregates.iou_ledgers); ?person= picks
    which one is open, defaulting to the largest balance."""
    conn = get_connection()
    ledgers = aggregates.iou_ledgers(conn)
    people = aggregates.iou_people(conn)
    conn.close()
    wanted = request.args.get("person")
    selected = next((l for l in ledgers if l["person"] == wanted), ledgers[0] if ledgers else None)
    return render_template("ious.html", ledgers=ledgers, selected=selected, people=people)


IOU_DIRECTIONS = ("they_paid", "i_paid", "starting_balance")


@app.route("/api/ious/add", methods=["POST"])
def add_iou():
    """A manual IOU entry — for something with no transaction behind it.
    A starting balance is signed (positive = you owed them, negative =
    they owed you) and may be 0 ("all square as of this date")."""
    payload = request.get_json(silent=True) or {}
    date_val = _iso_date(payload.get("date"))
    person = (payload.get("person") or "").strip()
    description = (payload.get("description") or "").strip()
    direction = payload.get("direction")
    if not date_val or not person or direction not in IOU_DIRECTIONS:
        return _fail("date, person, and a valid direction are required")
    if not description:
        description = "Starting balance" if direction == "starting_balance" else ""
    if not description:
        return _fail("a description is required")
    try:
        amount = _money(payload.get("amount"))
    except (TypeError, ValueError):
        return _fail("amount must be a number")
    if direction != "starting_balance":
        amount = abs(amount)
        if amount == 0:
            return _fail("amount must be non-zero")

    conn = get_connection()
    conn.execute(
        "INSERT INTO ious (date, person, description, direction, amount) VALUES (?,?,?,?,?)",
        (date_val, person, description, direction, amount),
    )
    conn.commit()
    conn.close()
    return jsonify(success=True)


@app.route("/api/ious/set-settlement", methods=["POST"])
def set_iou_settlement():
    """Sets the Settled/Pending reminder mark on IOU entries — items are
    {source: 'transaction'|'manual', id}, value 'settled', 'pending', or
    null to clear. Never changes a balance; see
    transactions.iou_settlement in db.py."""
    payload = request.get_json(silent=True) or {}
    items = payload.get("items", [])
    value = payload.get("value")
    if value not in ("settled", "pending", None):
        return _fail("value must be settled, pending, or null")
    conn = get_connection()
    updated = 0
    for item in items:
        if item.get("source") == "transaction":
            cursor = conn.execute("UPDATE transactions SET iou_settlement = ? WHERE id = ?", (value, item.get("id")))
        elif item.get("source") == "manual":
            cursor = conn.execute("UPDATE ious SET settlement = ? WHERE id = ?", (value, item.get("id")))
        else:
            continue
        updated += cursor.rowcount
    conn.commit()
    conn.close()
    return jsonify(success=True, updated=updated)


@app.route("/api/ious/delete", methods=["POST"])
def delete_ious():
    """Deletes manual IOU entries only — a transaction's IOU status is
    changed from the Transactions page."""
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids", [])
    conn = get_connection()
    deleted = sum(conn.execute("DELETE FROM ious WHERE id = ?", (i,)).rowcount for i in ids)
    conn.commit()
    conn.close()
    return jsonify(success=True, deleted=deleted)


# --- Import page: bank statements (csv_importer) + optional history ---

def _unique_path(folder, filename):
    """folder/filename, numbered if a file of that name is already there."""
    target = folder / filename
    n = 2
    while target.exists():
        target = folder / f"{target.stem.rsplit(' (', 1)[0]} ({n}){target.suffix}"
        n += 1
    return target


def _pending_file_path(base, name):
    """A file under `base` named by the page, refusing anything outside it."""
    candidate = (base / name).resolve()
    if base.resolve() not in candidate.parents or not candidate.is_file():
        return None
    return candidate


@app.route("/import")
def import_page():
    conn = get_connection()
    pending = csv_importer.pending_files(conn)
    accounts = conn.execute(
        "SELECT id, name, account_type FROM accounts WHERE is_active = 1 ORDER BY name"
    ).fetchall()
    conn.close()
    folders = {a["id"]: csv_importer.account_folder_name(a["name"]) for a in accounts}
    history = [p.name for p in history_importer.history_files()]
    return render_template(
        "import.html", pending=pending, accounts=accounts, folders=folders, history=history,
        incoming_dir=_display_path(csv_importer.INCOMING_DIR), history_dir=_display_path(history_importer.HISTORY_DIR),
    )


def _display_path(path):
    """A folder as shown to the user — relative to the project if inside it."""
    try:
        return str(path.relative_to(paths.BASE_DIR)) + "/"
    except ValueError:
        return str(path)


@app.route("/api/import/upload", methods=["POST"])
def upload_statements():
    """Saves uploaded CSVs into the chosen account's statements folder —
    the same as dropping them there by hand. Nothing is imported yet."""
    try:
        account_id = int(request.form.get("account_id") or 0)
    except ValueError:
        account_id = 0
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        return _fail("choose at least one CSV file")
    conn = get_connection()
    acct = conn.execute("SELECT name FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not acct:
        return _fail("pick the account these statements belong to", conn)
    csv_importer.ensure_account_folders(conn)
    conn.close()
    folder = csv_importer.INCOMING_DIR / csv_importer.account_folder_name(acct["name"])
    saved = []
    for f in files:
        name = secure_filename(f.filename) or "statement.csv"
        if not name.lower().endswith(csv_importer.STATEMENT_SUFFIXES):
            return _fail(f"'{f.filename}' isn't a .csv file — most banks offer CSV under Download / Export")
        target = _unique_path(folder, name)
        f.save(target)
        saved.append(target.name)
    return jsonify(success=True, saved=saved)


def _signs_from(payload):
    signs = payload.get("signs") or {}
    return {str(k): v for k, v in signs.items() if v in csv_importer.SIGN_MODES}


@app.route("/api/import/preview", methods=["POST"])
def preview_statements():
    """A real dry run (rolled back) of every pending statement, with a few
    sample rows per file so the signs can be checked by eye."""
    result = csv_importer.run(apply=False, signs=_signs_from(request.get_json(silent=True) or {}), sample_size=5)
    return jsonify(success=True, **result)


@app.route("/api/import/run", methods=["POST"])
def run_statement_import():
    """Imports every pending statement; files that import cleanly are
    deleted afterwards, files with errors stay for fixing."""
    result = csv_importer.run(apply=True, signs=_signs_from(request.get_json(silent=True) or {}))
    return jsonify(success=True, **result)


@app.route("/api/import/discard", methods=["POST"])
def discard_import_file():
    """Deletes one waiting file (a wrong download) without importing it.
    kind: 'statement' (incoming_statements/<account>/<file>) or 'history'."""
    payload = request.get_json(silent=True) or {}
    base = history_importer.HISTORY_DIR if payload.get("kind") == "history" else csv_importer.INCOMING_DIR
    path = _pending_file_path(base, str(payload.get("name") or ""))
    if path is None:
        return _fail("that file isn't there any more — reload the page")
    path.unlink()
    return jsonify(success=True)


@app.route("/api/import/history/upload", methods=["POST"])
def upload_history():
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        return _fail("choose at least one .csv or .xlsx file")
    history_importer.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    for f in files:
        name = secure_filename(f.filename) or "history.csv"
        if not name.lower().endswith(history_importer.SUFFIXES):
            return _fail(f"'{f.filename}' must be a .csv or .xlsx file")
        f.save(_unique_path(history_importer.HISTORY_DIR, name))
    return jsonify(success=True)


def _history_result(stats):
    return dict(accounts_created=stats.accounts_created, inserted=stats.inserted,
                already_present=stats.already_present, balances=stats.balances,
                categories=sorted(stats.categories.items(), key=lambda kv: kv[0].lower()),
                errors=[str(e) for e in stats.errors])


@app.route("/api/import/history/preview", methods=["POST"])
def preview_history():
    return jsonify(success=True, **_history_result(history_importer.run(apply=False)))


@app.route("/api/import/history/run", methods=["POST"])
def run_history_import():
    """Imports everything in import_history/; the files are deleted only
    if nothing at all went wrong (otherwise fix and re-run — it's safe)."""
    stats = history_importer.run(apply=True, delete_after=True)
    conn = get_connection()
    csv_importer.ensure_account_folders(conn)  # for any accounts it created
    conn.close()
    return jsonify(success=True, **_history_result(stats))


@app.route("/help")
def help_page():
    return render_template("help.html")


def _port():
    """--port N, else $PORT, else 5000."""
    if "--port" in sys.argv:
        return int(sys.argv[sys.argv.index("--port") + 1])
    return int(os.environ.get("PORT", 5000))


if __name__ == "__main__":
    if paths.DEMO:
        print(" * DEMO MODE — fake data in data/demo.db; your real data is untouched.")
    # 127.0.0.1 only: never reachable from another machine
    app.run(host="127.0.0.1", port=_port(), debug=True)
