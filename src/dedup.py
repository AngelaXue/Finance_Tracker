import hashlib
import re


def normalize_description(description: str) -> str:
    normalized = (description or "").lower().strip()
    normalized = re.sub(r"[^\w\s]", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def compute_dedup_hash(account_id: int, date: str, amount: float, description: str) -> str:
    normalized_description = normalize_description(description)
    raw = f"{account_id}|{date}|{amount}|{normalized_description}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def unique_dedup_hash(conn, account_id: int, date: str, amount: float, description: str, exclude_id: int | None = None) -> tuple[str, bool]:
    """Returns (hash, was_disambiguated) — a dedup_hash guaranteed not to
    collide with an existing row's, so a row is never silently dropped by
    the UNIQUE constraint. The first occurrence of a given account/date/
    amount/description gets the plain compute_dedup_hash (identical to
    before, so it lines up with anything already imported). A second (or
    third, etc.) row sharing that exact signature — e.g. two bus rides
    the same day, same fare, same description — gets a disambiguated hash
    instead of colliding, so it can be inserted rather than silently
    skipped; was_disambiguated=True tells the caller to flag it for
    review, since it's genuinely ambiguous whether this is a second real
    transaction or the same statement re-imported (there's no way to
    tell from the data alone).

    `exclude_id` excludes one row's id from the collision check — for
    editing an existing row: without this, a row whose date/amount/
    description aren't actually changing would trivially "collide" with
    its own current hash.

    Not applied to src/parsers/history_importer.py, which relies on the
    UNIQUE constraint being a hard, un-disambiguated block so re-running
    it (an idempotent one-time import) can never double-insert."""
    base = compute_dedup_hash(account_id, date, amount, description)
    candidate = base
    occurrence = 1
    while True:
        query = "SELECT 1 FROM transactions WHERE dedup_hash = ?"
        params = [candidate]
        if exclude_id is not None:
            query += " AND id != ?"
            params.append(exclude_id)
        if not conn.execute(query, params).fetchone():
            break
        occurrence += 1
        candidate = hashlib.sha256(f"{base}|{occurrence}".encode("utf-8")).hexdigest()
    return candidate, occurrence > 1


def find_possible_duplicate(conn, account_id: int, date: str, amount: float, exclude_id: int | None = None):
    """Returns the id of an existing transaction in the same account, on
    the same date, for the same amount — regardless of description — or
    None. A second line of defense alongside compute_dedup_hash's exact
    match: a hand-typed or spreadsheet description and the same transaction's
    raw bank CSV text often don't normalize to the same string (e.g.
    "Trader Joes" vs "TRADER JOE'S #123 SEATTLE WA"), so the exact hash
    can miss a real duplicate — most likely where a history import or a
    hand-typed row meets the first CSV import, where the same real-world
    transaction could plausibly be entered both ways.

    Same date+amount in the same account is a strong signal but not
    certain (two genuinely different transactions can land on the same
    day for the same amount) — callers should flag this for the user to
    review rather than silently skip or silently insert. `exclude_id`
    excludes one row's own id — for editing an existing row, so it never
    trivially matches itself."""
    query = "SELECT id FROM transactions WHERE account_id = ? AND date = ? AND amount = ?"
    params = [account_id, date, amount]
    if exclude_id is not None:
        query += " AND id != ?"
        params.append(exclude_id)
    row = conn.execute(query, params).fetchone()
    return row["id"] if row else None
