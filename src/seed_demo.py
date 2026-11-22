"""Fills the DEMO database (data/demo.db) with clearly fake data, so every
page has something realistic to show before you add your own.

    python app.py --demo          # seeds it on first run, then serves it
    python -m src.seed_demo       # wipe and re-create the demo data

It never touches your real database (data/finances.db). Deterministic:
the same fake history every time, dated relative to today.
"""

import random
from datetime import date

from src import db, paths
from src.dedup import compute_dedup_hash

ACCOUNTS = [
    dict(name="Everyday Checking", institution="Demo Bank", account_type="checking", currency="USD"),
    dict(name="High-Yield Savings", institution="Demo Bank", account_type="savings", currency="USD"),
    dict(name="Rewards Card", institution="Demo Card Co", account_type="credit_card", currency="USD"),
    dict(name="Roth IRA", institution="Demo Brokerage", account_type="retirement", currency="USD"),
    dict(name="Brokerage", institution="Demo Brokerage", account_type="brokerage", currency="USD"),
    dict(name="Family Card", institution="Demo Bank", account_type="credit_card", currency="USD",
         owner="other", iou_person="Mom"),
    dict(name="Euro Account", institution="Demo Europe Bank", account_type="checking", currency="EUR"),
    dict(name="Venmo", institution="Venmo", account_type="digital_wallet", currency="USD"),
    dict(name="Cash", institution="Wallet", account_type="cash", currency="USD"),
]

EXPENSES = {
    "Groceries": ["TRADER JOE'S #552", "WHOLE FOODS MARKET", "SAFEWAY 1234", "Farmers Market"],
    "Dining": ["CHIPOTLE 0921", "Sushi Place", "Local Cafe", "Pizza Night"],
    "Transportation": ["UBER *TRIP", "SHELL OIL 5741", "Metro Transit"],
    "Utilities": ["City Power & Light", "Comcast Internet", "Water Utility"],
    "Entertainment": ["NETFLIX.COM", "Movie Theater", "Concert Tickets"],
    "Shopping": ["AMAZON MKTPL", "TARGET 00012", "Bookstore"],
    "Health": ["CVS PHARMACY", "Dental Office"],
}


def _months(n):
    today = date.today()
    year, month = today.year, today.month
    out = []
    for _ in range(n):
        out.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(out))


def _day(year, month, day):
    """A date in that month, never after today (the current month is partial)."""
    d = date(year, month, min(day, 28))
    return min(d, date.today()).isoformat()


def seed(conn):
    rng = random.Random(42)
    for table in ("ious", "cds", "investment_deposits", "balances", "transactions", "accounts"):
        conn.execute(f"DELETE FROM {table}")

    ids = {}
    for a in ACCOUNTS:
        ids[a["name"]] = conn.execute(
            "INSERT INTO accounts (name, institution, account_type, currency, owner, iou_person) VALUES (?,?,?,?,?,?)",
            (a["name"], a["institution"], a["account_type"], a["currency"], a.get("owner", "self"),
             a.get("iou_person")),
        ).lastrowid

    txns = []

    def add(account, d, amount, ttype, description, category=None, **extra):
        txns.append(dict(account_id=ids[account], date=d, amount=round(amount, 2),
                         currency="EUR" if account == "Euro Account" else "USD", transaction_type=ttype,
                         description=description, category=category, **extra))

    months = _months(12)
    checking, savings, card_owed = 3500.0, 9000.0, 0.0
    roth_value, brokerage_value = 6000.0, 4000.0
    for n, (y, m) in enumerate(months):
        for day in (1, 15):
            pay = rng.uniform(2600, 2800)
            add("Everyday Checking", _day(y, m, day), pay, "income", "ACME CORP PAYROLL", "Salary")
            checking += pay
        add("Everyday Checking", _day(y, m, 2), -1850.0, "expense", "Rent - Maple Apartments", "Rent")
        checking -= 1850.0
        interest = rng.uniform(20, 35)
        add("High-Yield Savings", _day(y, m, 28), interest, "income", "Interest Paid", "Interest")
        savings += interest

        for _ in range(rng.randint(14, 20)):
            category = rng.choice(list(EXPENSES))
            amount = -rng.uniform(6, 140)
            add("Rewards Card", _day(y, m, rng.randint(1, 27)), amount, "expense",
                rng.choice(EXPENSES[category]), category, method="Card")
            card_owed -= amount

        # Card payoff: both legs are internal_transfer
        payoff = round(card_owed, 2)
        add("Everyday Checking", _day(y, m, 25), -payoff, "internal_transfer", "Rewards Card Autopay")
        add("Rewards Card", _day(y, m, 25), payoff, "internal_transfer", "PAYMENT THANK YOU")
        checking -= payoff
        card_owed = 0.0

        add("Everyday Checking", _day(y, m, 26), -400.0, "internal_transfer", "Transfer to Savings")
        add("High-Yield Savings", _day(y, m, 26), 400.0, "internal_transfer", "Transfer from Checking")
        checking -= 400.0
        savings += 400.0

        # Investing: the note names the account it went to
        add("Everyday Checking", _day(y, m, 16), -500.0, "investment_contribution", "Transfer to Roth IRA",
            "Investing", note="roth")
        add("Everyday Checking", _day(y, m, 16), -300.0, "investment_contribution", "Online transfer",
            "Investing", note="brokerage")
        checking -= 800.0
        roth_value = (roth_value + 500.0) * rng.uniform(0.99, 1.025)
        brokerage_value = (brokerage_value + 300.0) * rng.uniform(0.98, 1.03)

        # A card Mom pays off — its spending is owed to her (IOUs page)
        for _ in range(rng.randint(1, 3)):
            add("Family Card", _day(y, m, rng.randint(1, 27)), -rng.uniform(30, 160), "expense",
                rng.choice(EXPENSES["Groceries"] + EXPENSES["Shopping"]), "Groceries", method="Card")
        if n % 3 == 2:
            add("Everyday Checking", _day(y, m, 20), -300.0, "personal_transfer", "Zelle to Mom",
                "Repayment", method="Zelle", iou_kind="i_paid", iou_person="Mom")
            checking -= 300.0

        # Internet shared with a roommate: the bill is split into my half
        # (expense) and Alex's half (I paid for him), and Alex pays me back
        # on Venmo a few days later — except this month, not yet
        add("Everyday Checking", _day(y, m, 8), -40.0, "expense", "Comcast Internet", "Utilities")
        add("Everyday Checking", _day(y, m, 8), -40.0, "personal_transfer", "Comcast Internet (Alex's half)",
            "Shared bills", iou_kind="i_paid", iou_person="Alex")
        checking -= 80.0
        if n < len(months) - 1:
            add("Venmo", _day(y, m, 12), 40.0, "personal_transfer", "Alex - internet", method="Venmo",
                iou_kind="they_paid", iou_person="Alex")
        if rng.random() < 0.5:
            add("Venmo", _day(y, m, rng.randint(1, 27)), -rng.uniform(10, 30), "expense", "Coffee with friends",
                "Dining", method="Venmo")
        add("Everyday Checking", _day(y, m, 5), -100.0, "internal_transfer", "ATM Withdrawal", method="Cash")
        add("Cash", _day(y, m, 5), 100.0, "internal_transfer", "ATM Withdrawal", method="Cash")
        checking -= 100.0
        add("Cash", _day(y, m, rng.randint(6, 27)), -rng.uniform(20, 70), "expense", "Farmers Market",
            "Groceries", method="Cash")

        # A small euro account with its own spending
        add("Euro Account", _day(y, m, rng.randint(1, 27)), -rng.uniform(10, 60), "expense", "Bäckerei Müller",
            "Dining", method="Card")

        # Month-end balances, like a statement would show
        for name, balance in (("Everyday Checking", checking), ("High-Yield Savings", savings)):
            conn.execute("INSERT OR IGNORE INTO balances (account_id, date, balance) VALUES (?,?,?)",
                         (ids[name], _day(y, m, 28), round(balance, 2)))
        if n % 3 == 2 or n == len(months) - 1:  # investment values typed in every few months
            for name, value in (("Roth IRA", roth_value), ("Brokerage", brokerage_value)):
                conn.execute("INSERT OR IGNORE INTO balances (account_id, date, balance, note) VALUES (?,?,?,?)",
                             (ids[name], _day(y, m, 28), round(value, 2), "From the monthly statement"))

    first = months[0]
    conn.execute("INSERT INTO balances (account_id, date, balance) VALUES (?,?,?)",
                 (ids["Euro Account"], _day(*first, 1), 1200.0))
    conn.execute("INSERT INTO investment_deposits (account_id, date, amount, note) VALUES (?,?,?,?)",
                 (ids["Roth IRA"], _day(*first, 1), 6000.0, "Balance rolled over before tracking"))
    conn.execute("INSERT INTO investment_deposits (account_id, date, amount, note) VALUES (?,?,?,?)",
                 (ids["Brokerage"], _day(*first, 1), 4000.0, "Balance before tracking"))
    conn.execute("INSERT INTO cds (account_id, institution, principal, apy, opened_date, maturity_date, note, kind) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (ids["Brokerage"], "US Treasury", 2000.0, 4.1, _day(*months[3], 10),
                  f"{date.today().year + 1}-06-30", "Treasury note", "Treasury"))
    conn.execute("INSERT INTO ious (date, person, description, direction, amount) VALUES (?,?,?,?,?)",
                 (_day(*months[-3], 12), "Alex", "Concert tickets I bought for Alex", "i_paid", 85.0))
    conn.execute("INSERT INTO ious (date, person, description, direction, amount) VALUES (?,?,?,?,?)",
                 (_day(*first, 1), "Mom", "Owed from before tracking", "starting_balance", 250.0))

    # A few rows left for the user to review, like a fresh import would
    y, m = months[-1]
    add("Rewards Card", _day(y, m, 3), -23.5, "expense", "SQ *BLUE BOTTLE COFFEE", "Dining", category_predicted=1)
    add("Rewards Card", _day(y, m, 4), -61.2, "expense", "PAYPAL *ETSY SELLER", None, method="PayPal")

    for t in txns:
        conn.execute(
            "INSERT OR IGNORE INTO transactions (account_id, date, amount, currency, category, transaction_type, "
            "description, method, note, source, dedup_hash, category_predicted, iou_kind, iou_person) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t["account_id"], t["date"], t["amount"], t["currency"], t["category"], t["transaction_type"],
             t["description"], t.get("method"), t.get("note"), "manual",
             compute_dedup_hash(t["account_id"], t["date"], t["amount"], t["description"]),
             t.get("category_predicted", 0), t.get("iou_kind"), t.get("iou_person")),
        )
    conn.commit()
    return dict(accounts=len(ACCOUNTS), transactions=len(txns), months=len(months))


def seed_demo_database():
    """(Re)creates data/demo.db with the fake data — whatever db.DB_PATH
    currently points at is restored afterwards."""
    previous = db.DB_PATH
    db.DB_PATH = paths.DEMO_DB_PATH
    try:
        db.init_db()
        conn = db.get_connection()
        try:
            return seed(conn)
        finally:
            conn.close()
    finally:
        db.DB_PATH = previous


if __name__ == "__main__":
    result = seed_demo_database()
    print(f"Demo data: {result['accounts']} accounts, {result['transactions']} transactions over "
          f"{result['months']} months, in {paths.DEMO_DB_PATH.relative_to(paths.BASE_DIR)}.")
    print("Run `python app.py --demo` to explore it. Your real database was not touched.")
