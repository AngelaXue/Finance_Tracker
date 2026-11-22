"""Cash flow allocation + residual (savings) calculation.

Income is never treated as strictly less than or greater than output —
savings/investing are themselves counted as an output, so income and
output balance by construction. internal_transfer / personal_transfer
transactions never participate in this calculation (they're money moving
between the user's own accounts, or to/from other people — not real
income or spending).

All amounts are converted to the base currency (src/fx.py's manually-
updated rate table) before summing, so a transaction in another currency
doesn't get added to a total as if it were the same unit.
"""

from src.fx import to_base


def _sum_base(conn, sql, params):
    rows = conn.execute(sql, params).fetchall()
    return sum(to_base(r["amount"], r["currency"]) for r in rows)


def compute_sankey(conn, start_date: str, end_date: str) -> dict:
    # attributable_to_user = 0 ("Not Mine") applies symmetrically to income
    # too — e.g. money that lands in the user's account only to be handed
    # to someone else shouldn't inflate income_total any more than a gift
    # purchase should inflate living_expenses. If only part of a deposit
    # was actually kept, that's a per-transaction split (edit the row down
    # to the kept portion, add a second Not-Mine row for the rest) — the
    # flag is per-row, not per-portion.
    income_txn_rows = conn.execute(
        """
        SELECT COALESCE(category, 'Other Income') AS label, amount, currency
        FROM transactions
        WHERE transaction_type = 'income' AND attributable_to_user = 1 AND date BETWEEN ? AND ?
        """,
        (start_date, end_date),
    ).fetchall()
    income_by_label = {}
    for r in income_txn_rows:
        income_by_label[r["label"]] = income_by_label.get(r["label"], 0.0) + to_base(r["amount"], r["currency"])
    income_rows = sorted(
        ({"label": label, "total": total} for label, total in income_by_label.items()),
        key=lambda r: r["total"],
        reverse=True,
    )

    # Not Mine expenses (e.g. a purchase that was really a gift paid for
    # by someone else) still count fully in Spending's category breakdown
    # (aggregates.spending_by_category, deliberately unfiltered), but
    # excluding them here keeps Cash Flow's income-vs-spending honest.
    living_expenses = -_sum_base(
        conn,
        """
        SELECT amount, currency FROM transactions
        WHERE transaction_type = 'expense' AND attributable_to_user = 1 AND date BETWEEN ? AND ?
        """,
        (start_date, end_date),
    )

    investing = -_sum_base(
        conn,
        """
        SELECT amount, currency FROM transactions
        WHERE transaction_type = 'investment_contribution' AND date BETWEEN ? AND ?
        """,
        (start_date, end_date),
    )

    # Manual IOU entries where someone else paid for something of yours
    # directly — no transaction in your accounts, so it isn't in
    # living_expenses, but it was still consumed this period (and is now
    # owed). IOUs that come from transactions are already counted above
    # and never stored in `ious`, so nothing here double-counts. The ious
    # table is always in the base currency.
    paid_by_others = conn.execute(
        """
        SELECT SUM(amount) AS total FROM ious
        WHERE direction = 'they_paid' AND date BETWEEN ? AND ?
        """,
        (start_date, end_date),
    ).fetchone()["total"] or 0.0

    income_total = sum(row["total"] for row in income_rows)
    hard_outputs = living_expenses + investing + paid_by_others
    residual = income_total - hard_outputs

    sources = [{"label": row["label"], "amount": row["total"]} for row in income_rows]

    destinations = []
    if living_expenses > 0:
        destinations.append({"label": "Living Expenses", "amount": living_expenses})
    if investing > 0:
        destinations.append({"label": "Investing", "amount": investing})
    if paid_by_others > 0:
        destinations.append({"label": "Paid by Others", "amount": paid_by_others})

    if residual >= 0:
        if residual > 0:
            destinations.append({"label": "Cash Savings", "amount": residual})
    else:
        sources.append({"label": "From Savings", "amount": -residual})

    # pct is always relative to income_total (not to hard_outputs or to the
    # opposite side) — a single consistent denominator so every link
    # answers the same question, "what share of my income is this,"
    # whether it's a source flowing in or a destination flowing out.
    def _pct(amount):
        return (amount / income_total * 100) if income_total else 0.0

    links = [{"from": s["label"], "to": "Income", "flow": s["amount"], "pct": _pct(s["amount"])} for s in sources if s["amount"] > 0]
    links += [{"from": "Income", "to": d["label"], "flow": d["amount"], "pct": _pct(d["amount"])} for d in destinations if d["amount"] > 0]

    return {
        "income_total": income_total,
        "hard_outputs": hard_outputs,
        "residual": residual,
        "sources": sources,
        "destinations": destinations,
        "links": links,
    }
