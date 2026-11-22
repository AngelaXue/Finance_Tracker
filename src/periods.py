from datetime import date

_DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _shift_months_back(d: date, months: int) -> date:
    total = d.month - 1 - months
    year = d.year + total // 12
    month = total % 12 + 1
    max_day = _DAYS_IN_MONTH[month - 1]
    if month == 2 and _is_leap(year):
        max_day = 29
    return date(year, month, min(d.day, max_day))


PRESETS = ("month", "3mo", "6mo", "year", "custom")


def resolve_period(preset: str = "month", custom_start: str | None = None, custom_end: str | None = None):
    """Returns (start_date, end_date, label) as ISO strings. Default is
    the current calendar month, per CLAUDE.md's page spec."""
    today = date.today()

    if preset == "custom" and custom_start and custom_end:
        return custom_start, custom_end, "Custom Range"
    if preset == "3mo":
        return _shift_months_back(today, 3).isoformat(), today.isoformat(), "Past 3 Months"
    if preset == "6mo":
        return _shift_months_back(today, 6).isoformat(), today.isoformat(), "Past 6 Months"
    if preset in ("year", "1yr"):
        label = "1 Year" if preset == "1yr" else "Past Year"
        return _shift_months_back(today, 12).isoformat(), today.isoformat(), label
    if preset == "5yr":
        return _shift_months_back(today, 60).isoformat(), today.isoformat(), "5 Years"

    return today.replace(day=1).isoformat(), today.isoformat(), "This Month"
