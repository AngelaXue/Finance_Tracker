"""Currencies and exchange rates — edited by hand, never a live feed.

BASE_CURRENCY is the one currency combined totals are reported in (Net
Worth's grand total, Cash Flow, Spending, IOUs). Each account keeps its
own currency everywhere else; amounts are only converted when they have
to be added together.

RATES: 1 unit of that currency = RATES[currency] units of BASE_CURRENCY.
Only currencies listed here can be picked for an account. The non-base
rates below are rough placeholders — update them to today's rates before
relying on any combined total. To report in another currency, change
BASE_CURRENCY, set its own rate to 1.0, and re-express the others
relative to it.
"""

BASE_CURRENCY = "USD"

RATES = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "CAD": 0.73,
    "AUD": 0.66,
    "JPY": 0.0067,
    "CNY": 0.14,
    "INR": 0.012,
}

SYMBOLS = {
    "USD": "$", "EUR": "€", "GBP": "£", "CAD": "$", "AUD": "$",
    "JPY": "¥", "CNY": "¥", "INR": "₹",
}


def to_base(amount: float, currency: str) -> float:
    if currency not in RATES:
        raise ValueError(f"No FX rate configured for currency: {currency}")
    return amount * RATES[currency]


def symbol(currency: str) -> str:
    """Display symbol; falls back to the code itself ("CHF 12.00")."""
    return SYMBOLS.get(currency, f"{currency} ")


BASE_SYMBOL = symbol(BASE_CURRENCY)
