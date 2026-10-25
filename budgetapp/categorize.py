"""Choosing a category for a transaction.

Order: your keyword rules, then the bank's transaction type (card payments and
account transfers become Transfer), then the bank's own category, then
Uncategorized. When several rules match, the longest pattern wins, so
"UBER EATS" beats "UBER".
"""

import re

# Values of a file's Type column that mean money moved between your own
# accounts (compared ignoring case): card payments and account transfers.
TRANSFER_TYPES = {"payment", "loan_pmt", "acct_xfer"}

# Words that start the bank's reference noise in a description.
_NOISE_WORDS = {"PPD", "WEB", "ID:", "#", "NULL"}


def normalize(text: str) -> str:
    return " ".join(text.upper().split())


def _word_pattern(pattern: str) -> re.Pattern:
    # Whole-word match, so "UBER" doesn't match "TUBERS".
    return re.compile(r"(?<![A-Z0-9])" + re.escape(pattern) + r"(?![A-Z0-9])")


def suggest_pattern(description: str) -> str:
    """A rule pattern for this description, with IDs and reference numbers dropped.

    "AMAZON MKTPL*AB1234567" -> "AMAZON MKTPL"
    "VENMO PAYMENT 1000000000009 WEB ID: 0000000009" -> "VENMO PAYMENT"
    """
    full = normalize(description)
    words = []
    for word in full.split("*")[0].split():
        if word in _NOISE_WORDS or any(c.isdigit() for c in word):
            break
        words.append(word)
    # Drop trailing punctuation like the "-" in "SAVERS - 1121".
    while words and not any(c.isalnum() for c in words[-1]):
        words.pop()
    if words:
        return " ".join(words)
    # The description starts with a number (e.g. "7-ELEVEN 1234"): keep its first word.
    return full.split()[0] if full else ""


class Categorizer:
    """Loads rules once; use a new instance after rules change."""

    def __init__(self, conn):
        rules = conn.execute(
            "SELECT pattern, category_id, person_id FROM rules ORDER BY length(pattern) DESC, id DESC"
        ).fetchall()
        self.rules = [(_word_pattern(r["pattern"]), r["category_id"]) for r in rules]
        # Only rules that name a person decide the person.
        self.person_rules = [(_word_pattern(r["pattern"]), r["person_id"]) for r in rules if r["person_id"]]
        self.bank_categories = {
            row["bank_category"].lower(): row["category_id"]
            for row in conn.execute("SELECT bank_category, category_id FROM bank_categories")
        }
        kinds = {
            row["kind"]: row["id"]
            for row in conn.execute(
                "SELECT kind, id FROM categories WHERE kind IN ('transfer', 'uncategorized')"
            )
        }
        self.transfer_id = kinds["transfer"]
        self.uncategorized_id = kinds["uncategorized"]

    def categorize(self, description, bank_category, bank_type) -> tuple[int, str]:
        """Return (category_id, category_source)."""
        text = normalize(description)
        for pattern, category_id in self.rules:
            if pattern.search(text):
                return category_id, "rule"
        if bank_type and bank_type.lower() in TRANSFER_TYPES:
            return self.transfer_id, "bank"
        if bank_category and bank_category.lower() in self.bank_categories:
            return self.bank_categories[bank_category.lower()], "bank"
        return self.uncategorized_id, "none"

    def person(self, description) -> int | None:
        """The person from the longest matching rule that names one, else None (Household)."""
        text = normalize(description)
        for pattern, person_id in self.person_rules:
            if pattern.search(text):
                return person_id
        return None


def recategorize(conn) -> int:
    """Re-run rules on every transaction that isn't split.

    The category is only changed if you didn't set it by hand, and the person
    only if you didn't set that by hand. Returns how many changed category.
    Call inside a transaction.
    """
    categorizer = Categorizer(conn)
    rows = conn.execute(
        """SELECT id, description, bank_category, bank_type, category_id, category_source,
                  person_id, person_manual
           FROM transactions t
           WHERE (category_source != 'manual' OR person_manual = 0)
             AND NOT EXISTS (SELECT 1 FROM transaction_parts p WHERE p.transaction_id = t.id)"""
    ).fetchall()
    changed = 0
    for row in rows:
        if row["category_source"] != "manual":
            category_id, source = categorizer.categorize(
                row["description"], row["bank_category"], row["bank_type"]
            )
            if (category_id, source) != (row["category_id"], row["category_source"]):
                conn.execute(
                    "UPDATE transactions SET category_id = ?, category_source = ? WHERE id = ?",
                    (category_id, source, row["id"]),
                )
                changed += category_id != row["category_id"]
        if not row["person_manual"]:
            person_id = categorizer.person(row["description"])
            if person_id != row["person_id"]:
                conn.execute("UPDATE transactions SET person_id = ? WHERE id = ?", (person_id, row["id"]))
    return changed
