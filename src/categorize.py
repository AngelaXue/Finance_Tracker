"""Local transaction categorizer — TF-IDF + Naive Bayes, trained on the
user's own historical data. Runs entirely on-device; no cloud API calls.
"""

import joblib
from sklearn.naive_bayes import MultinomialNB
from sklearn.pipeline import Pipeline
from sklearn.feature_extraction.text import TfidfVectorizer

from src import paths

MODEL_PATH = paths.MODEL_PATH

# Below this confidence, predict_category refuses to guess rather than
# defaulting to the most common training category — see predict_category's
# docstring for why that default happens without this. 0.3 rather than
# higher, deliberately: with many categories and short descriptions, even
# good matches rarely score far above that, so a stricter cutoff tends to
# flag nothing at all. 0.3 surfaces the rows with the model's best (if
# modest) signal while still leaving the noisier majority honestly blank —
# every flagged row still goes through manual review before being trusted.
MIN_CONFIDENCE = 0.3

# Explicit rules, checked before the statistical classifier — first match
# wins, so put more specific patterns above more general ones (e.g. "roth
# ira" above the generic "transfer" catch-all, so a Roth IRA contribution
# doesn't get swallowed by the plain-transfer rule). This is the "teach it
# directly" mechanism: a statistical model can only learn from repeated
# examples, but a structural pattern like "this phrase always means this"
# doesn't need repetition — add a tuple here instead.
#
# Each entry is (pattern, category, transaction_type). pattern is matched
# as a case-insensitive substring against the description. category and/or
# transaction_type may be None to mean "leave that field alone."
CATEGORY_RULES: list[tuple[str, str | None, str | None]] = [
    ("roth ira", "Investing", "investment_contribution"),
    ("401k", "Investing", "investment_contribution"),
    ("brokerage", "Investing", "investment_contribution"),
    # Common card-side wording for a payoff (e.g. Chase's "Payment Thank
    # You - Web") — paying off a card from checking isn't real spending,
    # same reasoning as the generic transfer rule below. Add your own
    # banks' wording here.
    ("payment thank you", None, "internal_transfer"),
    ("payment - thank you", None, "internal_transfer"),
    ("payment, thank you", None, "internal_transfer"),
    ("transfer", None, "internal_transfer"),
]


def apply_rules(description: str) -> tuple[str | None, str | None] | None:
    """Returns (category, transaction_type) from the first matching rule
    in CATEGORY_RULES, or None if nothing matches."""
    if not description:
        return None
    lowered = description.lower()
    for pattern, category, transaction_type in CATEGORY_RULES:
        if pattern in lowered:
            return category, transaction_type
    return None


def train_classifier(conn) -> int:
    """Train on every categorized transaction in the DB (manual corrections
    are free training data — they're already reflected in `category`).
    Returns the number of examples trained on, or 0 if there isn't enough
    labeled data yet to train.
    """
    rows = conn.execute(
        "SELECT description, category FROM transactions "
        "WHERE category IS NOT NULL AND description IS NOT NULL"
    ).fetchall()

    if len(rows) < 2:
        return 0

    descriptions = [row["description"] for row in rows]
    categories = [row["category"] for row in rows]

    if len(set(categories)) < 2:
        return 0

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    pipeline = Pipeline([
        ("tfidf", TfidfVectorizer()),
        # fit_prior=False: without this, Naive Bayes' prior is each
        # category's training frequency, so on a description with little
        # or no vocabulary overlap with training data (weak/no evidence
        # either way), it falls back to whichever category was most
        # common in training — e.g. everything becomes "Dining" just
        # because Dining dominates the credit-card training data, not
        # because the text actually looks like dining. A uniform prior
        # means a low-evidence row gets a low-confidence, near-uniform
        # probability spread instead of a confident wrong guess — which
        # the MIN_CONFIDENCE check below can then correctly refuse.
        ("clf", MultinomialNB(fit_prior=False)),
    ])
    pipeline.fit(descriptions, categories)
    joblib.dump(pipeline, MODEL_PATH)
    return len(rows)


def predict_category(description: str, min_confidence: float = MIN_CONFIDENCE) -> str | None:
    """Returns None both when there's no model yet AND when the model's
    top guess is below min_confidence — i.e. "I don't have enough signal
    to guess" is a valid, expected outcome, not just a missing-model
    fallback. Never silently returns a low-confidence guess as if it were
    a solid one."""
    if not MODEL_PATH.exists() or not description:
        return None
    pipeline = joblib.load(MODEL_PATH)
    probabilities = pipeline.predict_proba([description])[0]
    best_index = probabilities.argmax()
    if probabilities[best_index] < min_confidence:
        return None
    return pipeline.classes_[best_index]


def predict_uncategorized(conn) -> dict:
    """Retrain on whatever's currently labeled, then for every uncategorized
    row: check CATEGORY_RULES first, then fall back to the statistical
    classifier. A rule/prediction that sets `category` flags the row with
    category_predicted=1 so it shows up on the Transactions page for
    review rather than being silently trusted. A rule that only corrects
    `transaction_type` (the generic transfer rule, with category left
    None) is applied directly with no flag — there's no category to
    review, and internal_transfer/personal_transfer rows never need
    one.

    Returns {"flagged": n, "auto_classified": n} — the former needs your
    review, the latter was a structural correction applied outright.

    Worth knowing: the classifier only knows the vocabulary it has seen.
    Merchant-style card descriptions ("TRADER JOE'S #123") and hand-typed
    ones ("Food repay") look very different, so predictions on a style it
    hasn't seen much of will be rougher. That's exactly why this flags
    for review instead of just filling category silently — and why it
    improves as you categorize more.
    """
    train_classifier(conn)  # best-effort; predict_category handles "no model" on its own

    rows = conn.execute(
        "SELECT id, description FROM transactions WHERE category IS NULL AND description IS NOT NULL "
        "AND transaction_type NOT IN ('internal_transfer', 'personal_transfer')"
    ).fetchall()

    flagged, auto_classified = 0, 0
    for row in rows:
        rule_match = apply_rules(row["description"])
        if rule_match is not None:
            category, transaction_type = rule_match
            sets, params = [], []
            if category is not None:
                sets += ["category = ?", "category_predicted = 1"]
                params.append(category)
            if transaction_type is not None:
                sets.append("transaction_type = ?")
                params.append(transaction_type)
            if sets:
                params.append(row["id"])
                conn.execute(f"UPDATE transactions SET {', '.join(sets)} WHERE id = ?", params)
                flagged += 1 if category is not None else 0
                auto_classified += 1 if category is None else 0
            continue

        predicted = predict_category(row["description"])
        if predicted is None:
            continue
        conn.execute(
            "UPDATE transactions SET category = ?, category_predicted = 1 WHERE id = ?",
            (predicted, row["id"]),
        )
        flagged += 1

    conn.commit()
    return {"flagged": flagged, "auto_classified": auto_classified}
