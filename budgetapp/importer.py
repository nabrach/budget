"""Importing a CSV file into an account.

An import can stop and ask before saving anything (NeedsInput) when:
- "columns": the app can't tell which column is which;
- "direction": a single-amount file's purchases could be negative or
  positive, and the account hasn't confirmed which (or the file looks the
  opposite of what the account expects);
- "new_columns": the file's columns differ from the account's last import.
The page answers, then retries the import.
"""

import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, replace

from .categorize import Categorizer, normalize
from .db import transaction
from .errors import NotFound, UserError
from .formats import Layout, Row, detect_layout, header_signature, parse_csv, preview_rows, read_header

# Too few rows to judge direction from.
MIN_ROWS_TO_JUDGE = 5


class NeedsInput(Exception):
    """The import needs an answer from the user before it can go ahead."""

    def __init__(self, kind: str, **details):
        super().__init__(kind)
        self.kind = kind
        self.details = details

    def to_dict(self):
        return {"needs": self.kind, **self.details}


@dataclass
class ImportSummary:
    batch_id: int
    account_name: str
    filename: str
    rows_in_file: int
    rows_added: int
    rows_skipped: int
    # Not cleared yet, so left out; they'll import once the bank marks them cleared.
    rows_pending: int
    uncategorized: int
    first_date: str
    last_date: str

    def to_dict(self):
        return asdict(self)


def fingerprint(row: Row) -> str:
    return f"{row.date}|{row.amount_cents}|{normalize(row.description)}"


# ---- Saved column layouts --------------------------------------------------------

def saved_layout(conn, headers) -> Layout | None:
    row = conn.execute(
        "SELECT layout FROM column_layouts WHERE signature = ?", (header_signature(headers),)
    ).fetchone()
    return Layout(**json.loads(row["layout"])) if row else None


def save_layout(conn, headers, choices):
    """Remember which column is which for files with these headers."""
    if not isinstance(headers, list) or not all(isinstance(h, str) for h in headers) or not headers:
        raise UserError("'headers' must be the file's list of column names.")
    layout = Layout.from_dict(choices, headers)
    with transaction(conn):
        conn.execute(
            """INSERT INTO column_layouts (signature, headers, layout) VALUES (?, ?, ?)
               ON CONFLICT (signature) DO UPDATE SET layout = excluded.layout""",
            (header_signature(headers), json.dumps(headers), json.dumps(layout.to_dict())),
        )
    return layout.to_dict()


def list_layouts(conn):
    return [
        {"id": row["id"], "headers": json.loads(row["headers"]), "layout": json.loads(row["layout"])}
        for row in conn.execute("SELECT * FROM column_layouts ORDER BY id")
    ]


def delete_layout(conn, layout_id):
    with transaction(conn):
        if not conn.execute("DELETE FROM column_layouts WHERE id = ?", (layout_id,)).rowcount:
            raise NotFound("That saved layout doesn't exist.")


# ---- Amount direction --------------------------------------------------------------
# Guessing which way a single-amount file's purchases go. Counting negative
# vs positive rows isn't enough: card statements are full of payments and
# credits (5 of 9 rows in an Amex sample). So rows "vote", and rows with a
# clue (wording, a Type, a Category) count for more than plain rows.

# Wording that means money coming in.
_MONEY_IN_WORDS = re.compile(
    r"\bTHANK YOU\b|\bREFUND|\bRETURN|\bCASH ?BACK\b|\bPAYROLL\b|\bDIRECT DEP|\bCREDIT$"
)
_PURCHASE_TYPES = {"sale", "purchase", "debit"}
_MONEY_IN_TYPES = {"payment", "return", "refund", "credit"}
CLUE_WEIGHT = 3
CATEGORY_WEIGHT = 2
# A file "looks reversed" only on strong evidence: the vote must point the
# other way by at least this much, and by at least this share of the rows.
REVERSED_MIN_SCORE = 3
REVERSED_MIN_SHARE = 0.3


def _direction_score(rows) -> int:
    """Positive if purchases look negative in this file, negative if they look positive."""
    score = 0
    for row in rows:
        if not row.amount_cents:
            continue
        negative = row.amount_cents < 0
        bank_type = (row.bank_type or "").lower()
        if _MONEY_IN_WORDS.search(normalize(row.description)) or bank_type in _MONEY_IN_TYPES:
            score += -CLUE_WEIGHT if negative else CLUE_WEIGHT  # money in is negative -> purchases positive
        elif bank_type in _PURCHASE_TYPES:
            score += CLUE_WEIGHT if negative else -CLUE_WEIGHT
        elif row.bank_category:
            score += CATEGORY_WEIGHT if negative else -CATEGORY_WEIGHT
        else:
            score += 1 if negative else -1
    return score


def _purchase_sign_guess(rows) -> int:
    """1 if purchases look negative in this file, -1 if positive."""
    return 1 if _direction_score(rows) >= 0 else -1


def _looks_reversed(rows, amount_sign) -> bool:
    nonzero = [r for r in rows if r.amount_cents]
    if len(nonzero) < MIN_ROWS_TO_JUDGE:
        return False
    against = -_direction_score(nonzero) * amount_sign  # > 0 when the file disagrees with the account
    return against >= max(REVERSED_MIN_SCORE, REVERSED_MIN_SHARE * len(nonzero))


def _examples(rows, guess):
    """A few rows to show when confirming direction: the biggest purchases by
    the guess, plus the biggest money-in row if there is one."""
    purchases = sorted((r for r in rows if r.amount_cents * guess < 0), key=lambda r: -abs(r.amount_cents))
    money_in = sorted((r for r in rows if r.amount_cents * guess > 0), key=lambda r: -abs(r.amount_cents))
    picked = purchases[:3] + money_in[:1]
    return [{"date": r.date, "description": r.description, "amount_cents": r.amount_cents} for r in picked]


# ---- Import ----------------------------------------------------------------------------

def import_csv(
    conn, account_id: int, filename: str, text: str, *, allow_new_columns=False, amount_sign=None
) -> ImportSummary:
    """Import all new rows from the file, or nothing if any row can't be read.

    amount_sign, when given, is the user's answer to a "direction" question:
    it's used for this file and saved as the account's direction.

    Rows already in the account are skipped. Identical rows are counted rather
    than collapsed: if the file has two identical $3.50 coffees and the account
    has one, one more is added.
    """
    account = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if account is None:
        raise NotFound("That account doesn't exist.")

    headers = read_header(text)
    signature = header_signature(headers)
    layout = saved_layout(conn, headers) or detect_layout(headers)
    if layout is None:
        raise NeedsInput("columns", headers=headers, rows=preview_rows(text))
    if account["columns_signature"] and account["columns_signature"] != signature and not allow_new_columns:
        raise NeedsInput(
            "new_columns",
            message=(
                f"This file's columns don't match earlier imports into {account['name']}. "
                "It may be from a different account."
            ),
        )

    if amount_sign not in (None, 1, -1):
        raise UserError("amount_sign must be 1 or -1.")
    parsed = parse_csv(text, layout)
    rows = parsed.rows
    sign = account["amount_sign"]
    if layout.single_amount and rows:
        guess = _purchase_sign_guess(rows)
        if amount_sign is not None:
            sign = amount_sign
        elif sign is None or _looks_reversed(rows, sign):
            raise NeedsInput(
                "direction",
                reason="first" if sign is None else "reversed",
                guess=guess,
                examples=_examples(rows, guess),
            )
        if sign == -1:
            rows = [replace(r, amount_cents=-r.amount_cents) for r in rows]

    if not rows and parsed.pending:
        raise UserError(
            f"All {parsed.pending} transactions in the file are still pending. "
            "Import again once they've cleared."
        )
    if not rows:
        raise UserError("The file has no transactions in it. Nothing was imported.")

    groups = defaultdict(list)
    for row in rows:
        groups[fingerprint(row)].append(row)

    categorizer = Categorizer(conn)
    uncategorized = 0
    with transaction(conn):
        batch_id = conn.execute(
            """INSERT INTO import_batches (account_id, filename, rows_in_file, rows_added, rows_skipped)
               VALUES (?, ?, ?, 0, 0)""",
            (account_id, filename, len(rows)),
        ).lastrowid
        added = 0
        for fp, group in groups.items():
            (existing,) = conn.execute(
                "SELECT count(*) FROM transactions WHERE account_id = ? AND fingerprint = ?",
                (account_id, fp),
            ).fetchone()
            for row in group[existing:]:
                category_id, source = categorizer.categorize(row.description, row.bank_category, row.bank_type)
                uncategorized += category_id == categorizer.uncategorized_id
                conn.execute(
                    """INSERT INTO transactions (account_id, batch_id, date, description,
                           amount_cents, bank_category, bank_type, category_id,
                           category_source, fingerprint, person_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        account_id, batch_id, row.date, row.description, row.amount_cents,
                        row.bank_category, row.bank_type, category_id, source, fp,
                        categorizer.person(row.description),
                    ),
                )
                added += 1
        conn.execute(
            "UPDATE import_batches SET rows_added = ?, rows_skipped = ? WHERE id = ?",
            (added, len(rows) - added, batch_id),
        )
        conn.execute(
            "UPDATE accounts SET columns_signature = ?, amount_sign = ? WHERE id = ?",
            (signature, sign, account_id),
        )

    dates = sorted(row.date for row in rows)
    return ImportSummary(
        batch_id=batch_id,
        account_name=account["name"],
        filename=filename,
        rows_in_file=len(rows),
        rows_added=added,
        rows_skipped=len(rows) - added,
        rows_pending=parsed.pending,
        uncategorized=uncategorized,
        first_date=dates[0],
        last_date=dates[-1],
    )


def undo_import(conn, batch_id: int) -> int:
    """Delete a batch and every transaction it added. Returns how many were removed."""
    with transaction(conn):
        if not conn.execute("SELECT 1 FROM import_batches WHERE id = ?", (batch_id,)).fetchone():
            raise NotFound("That import doesn't exist (it may already be undone).")
        removed = conn.execute("DELETE FROM transactions WHERE batch_id = ?", (batch_id,)).rowcount
        conn.execute("DELETE FROM import_batches WHERE id = ?", (batch_id,))
    return removed
