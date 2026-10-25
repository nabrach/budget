"""Reading and changing accounts, transactions, categories and rules."""

import sqlite3

from .categorize import normalize, recategorize, suggest_pattern
from .db import transaction
from .errors import NotFound, UserError

MAX_NAME_LENGTH = 60

# Categories the app relies on; they can be renamed but not deleted.
BUILT_IN_KINDS = ("transfer", "uncategorized")


def _clean_name(name, what, example):
    name = " ".join(str(name or "").split())
    if not name:
        raise UserError(f"Give the {what} a name, like “{example}”.")
    if len(name) > MAX_NAME_LENGTH:
        raise UserError(f"Names can be at most {MAX_NAME_LENGTH} characters.")
    return name


# ---- Accounts ----------------------------------------------------------------

def list_accounts(conn):
    rows = conn.execute(
        """SELECT a.id, a.name, a.amount_sign,
                  (SELECT count(*) FROM transactions t WHERE t.account_id = a.id) AS transaction_count,
                  (SELECT max(imported_at) FROM import_batches b WHERE b.account_id = a.id) AS last_import
           FROM accounts a ORDER BY a.name"""
    )
    return [dict(row) for row in rows]


def _require_account(conn, account_id):
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise NotFound("That account doesn't exist.")
    return row


def create_account(conn, name, amount_sign=None):
    """amount_sign: 1 if this account's files show purchases as negative, -1
    if positive, None to ask on the first import that needs it."""
    name = _clean_name(name, "account", "Chase Freedom")
    if amount_sign not in (None, 1, -1):
        raise UserError("amount_sign must be 1, -1 or empty.")
    try:
        with transaction(conn):
            account_id = conn.execute(
                "INSERT INTO accounts (name, amount_sign) VALUES (?, ?)", (name, amount_sign)
            ).lastrowid
    except sqlite3.IntegrityError:
        raise UserError(f"You already have an account called “{name}”.") from None
    return {"id": account_id, "name": name, "amount_sign": amount_sign}


def set_amount_sign(conn, account_id, amount_sign):
    """Change which way this account's future imports read single-amount files."""
    _require_account(conn, account_id)
    if amount_sign not in (1, -1):
        raise UserError("amount_sign must be 1 (purchases negative) or -1 (purchases positive).")
    with transaction(conn):
        conn.execute("UPDATE accounts SET amount_sign = ? WHERE id = ?", (amount_sign, account_id))
    return {"id": account_id, "amount_sign": amount_sign}


def rename_account(conn, account_id, name):
    _require_account(conn, account_id)
    name = _clean_name(name, "account", "Chase Freedom")
    try:
        with transaction(conn):
            conn.execute("UPDATE accounts SET name = ? WHERE id = ?", (name, account_id))
    except sqlite3.IntegrityError:
        raise UserError(f"You already have an account called “{name}”.") from None
    return {"id": account_id, "name": name}


def delete_account(conn, account_id):
    """Delete an account with all its imports and transactions."""
    _require_account(conn, account_id)
    with transaction(conn):
        removed = conn.execute(
            "DELETE FROM transactions WHERE account_id = ?", (account_id,)
        ).rowcount
        conn.execute("DELETE FROM import_batches WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    return {"removed": removed}


def list_batches(conn):
    return [
        dict(row)
        for row in conn.execute(
            """SELECT b.id, b.filename, b.imported_at, b.rows_in_file, b.rows_added,
                      b.rows_skipped, a.name AS account_name
               FROM import_batches b JOIN accounts a ON a.id = b.account_id
               ORDER BY b.id DESC"""
        )
    ]


# ---- Categories --------------------------------------------------------------
# Two levels: groups (parent_id NULL) and subcategories. Names are unique
# within a group; full names read "Food › Groceries".

SEPARATOR = " › "

# Tree order: groups by sort_order, each followed by its subcategories.
CATEGORY_TREE_SQL = f"""
    SELECT c.id, c.name, c.kind, c.parent_id,
           coalesce(g.name || '{SEPARATOR}', '') || c.name AS full_name,
           g.name AS group_name
    FROM categories c LEFT JOIN categories g ON g.id = c.parent_id
    ORDER BY coalesce(g.sort_order, c.sort_order), coalesce(c.parent_id, c.id),
             c.parent_id IS NOT NULL, c.sort_order, c.name
"""


def list_categories(conn):
    counts = dict(conn.execute("SELECT category_id, count(*) FROM entries GROUP BY category_id").fetchall())
    rules = dict(conn.execute("SELECT category_id, count(*) FROM rules GROUP BY category_id").fetchall())
    parents = {row[0] for row in conn.execute("SELECT DISTINCT parent_id FROM categories WHERE parent_id IS NOT NULL")}
    return [
        dict(
            row,
            transaction_count=counts.get(row["id"], 0),
            rule_count=rules.get(row["id"], 0),
            has_children=row["id"] in parents,
        )
        for row in conn.execute(CATEGORY_TREE_SQL)
    ]


def uncategorized_id(conn):
    return conn.execute("SELECT id FROM categories WHERE kind = 'uncategorized'").fetchone()["id"]


def _require_category(conn, category_id, error=UserError):
    row = conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
    if row is None:
        raise error("That category doesn't exist.")
    return row


def full_name(conn, category_id):
    row = conn.execute(
        f"""SELECT coalesce(g.name || '{SEPARATOR}', '') || c.name FROM categories c
            LEFT JOIN categories g ON g.id = c.parent_id WHERE c.id = ?""",
        (category_id,),
    ).fetchone()
    return row[0] if row else None


def _group_for_child(conn, parent_id):
    """Check that parent_id can hold subcategories, and return it."""
    parent = _require_category(conn, parent_id)
    if parent["parent_id"] is not None:
        raise UserError("Subcategories can only go in a group, not in another subcategory.")
    if parent["kind"] not in ("expense", "income"):
        raise UserError(f"{parent['name']} can't have subcategories.")
    return parent


def _name_taken(name, parent):
    where = f" in {parent['name']}" if parent else ""
    return UserError(f"There's already a category called “{name}”{where}.")


def create_category(conn, name, kind="expense", parent_id=None):
    """A new group, or a subcategory when parent_id is given (it takes the group's kind)."""
    name = _clean_name(name, "category", "Pets")
    parent = _group_for_child(conn, parent_id) if parent_id is not None else None
    if parent:
        kind = parent["kind"]
    elif kind not in ("expense", "income"):
        raise UserError("New categories are either spending or income.")
    try:
        with transaction(conn):
            if parent:
                (order,) = conn.execute("SELECT coalesce(max(sort_order), 0) + 1 FROM categories").fetchone()
            else:
                # New groups go after the other spending groups, before Income and the built-ins.
                (order,) = conn.execute(
                    "SELECT coalesce(max(sort_order), 0) + 1 FROM categories WHERE parent_id IS NULL AND kind = 'expense'"
                ).fetchone()
                conn.execute(
                    "UPDATE categories SET sort_order = sort_order + 1 WHERE parent_id IS NULL AND sort_order >= ?",
                    (order,),
                )
            category_id = conn.execute(
                "INSERT INTO categories (name, kind, sort_order, parent_id) VALUES (?, ?, ?, ?)",
                (name, kind, order, parent_id),
            ).lastrowid
    except sqlite3.IntegrityError:
        raise _name_taken(name, parent) from None
    return {"id": category_id, "name": name, "kind": kind, "parent_id": parent_id, "full_name": full_name(conn, category_id)}


def rename_category(conn, category_id, name):
    category = _require_category(conn, category_id, NotFound)
    name = _clean_name(name, "category", "Pets")
    parent = _require_category(conn, category["parent_id"]) if category["parent_id"] else None
    try:
        with transaction(conn):
            conn.execute("UPDATE categories SET name = ? WHERE id = ?", (name, category_id))
    except sqlite3.IntegrityError:
        raise _name_taken(name, parent) from None
    return {"id": category_id, "name": name, "full_name": full_name(conn, category_id)}


def move_category(conn, category_id, parent_id):
    """Move a category into a group (parent_id) or make it a group (None)."""
    category = _require_category(conn, category_id, NotFound)
    if category["kind"] in BUILT_IN_KINDS:
        raise UserError(f"{category['name']} is built in and can't be moved.")
    parent = None
    if parent_id is not None:
        parent = _group_for_child(conn, parent_id)
        if parent["id"] == category_id:
            raise UserError("A category can't go inside itself.")
        if conn.execute("SELECT 1 FROM categories WHERE parent_id = ?", (category_id,)).fetchone():
            raise UserError(f"{category['name']} has subcategories, so it can only be a group.")
        if parent["kind"] != category["kind"]:
            raise UserError("Spending and income categories can't be mixed in one group.")
    try:
        with transaction(conn):
            conn.execute("UPDATE categories SET parent_id = ? WHERE id = ?", (parent_id, category_id))
    except sqlite3.IntegrityError:
        raise _name_taken(category["name"], parent) from None
    return {"id": category_id, "parent_id": parent_id, "full_name": full_name(conn, category_id)}


def delete_category(conn, category_id):
    """Delete a category.

    A subcategory's transactions, rules and bank mappings move up to its group.
    A group's (and its subcategories') transactions are re-sorted by the
    remaining rules or go to Uncategorized, and their rules are removed.
    Budgets on deleted categories are removed either way.
    """
    category = _require_category(conn, category_id, NotFound)
    if category["kind"] in BUILT_IN_KINDS:
        raise UserError(f"{category['name']} is built in and can't be deleted (you can rename it).")
    with transaction(conn):
        if category["parent_id"] is not None:
            parent_id = category["parent_id"]
            moved = conn.execute(
                "UPDATE transactions SET category_id = ? WHERE category_id = ?", (parent_id, category_id)
            ).rowcount
            moved += conn.execute(
                "UPDATE transaction_parts SET category_id = ? WHERE category_id = ?", (parent_id, category_id)
            ).rowcount
            conn.execute("UPDATE rules SET category_id = ? WHERE category_id = ?", (parent_id, category_id))
            conn.execute("UPDATE bank_categories SET category_id = ? WHERE category_id = ?", (parent_id, category_id))
            conn.execute("DELETE FROM budgets WHERE category_id = ?", (category_id,))
            conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
            return {"moved": moved, "rules_removed": 0, "moved_to": full_name(conn, parent_id)}

        ids = [category_id] + [r[0] for r in conn.execute("SELECT id FROM categories WHERE parent_id = ?", (category_id,))]
        marks = ",".join("?" * len(ids))
        fallback = uncategorized_id(conn)
        rules_removed = conn.execute(f"DELETE FROM rules WHERE category_id IN ({marks})", ids).rowcount
        conn.execute(f"DELETE FROM bank_categories WHERE category_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM budgets WHERE category_id IN ({marks})", ids)
        moved = conn.execute(
            f"UPDATE transactions SET category_id = ?, category_source = 'none' WHERE category_id IN ({marks})",
            (fallback, *ids),
        ).rowcount
        moved += conn.execute(
            f"UPDATE transaction_parts SET category_id = ? WHERE category_id IN ({marks})", (fallback, *ids)
        ).rowcount
        conn.execute(f"DELETE FROM categories WHERE parent_id = ?", (category_id,))
        conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        recategorize(conn)
    return {"moved": moved, "rules_removed": rules_removed, "moved_to": None}


def _is_split(conn, transaction_id):
    return conn.execute("SELECT 1 FROM transaction_parts WHERE transaction_id = ? LIMIT 1", (transaction_id,)).fetchone()


def set_category(conn, transaction_id, category_id):
    """Set a category by hand. Rules won't change it afterwards.

    Returns a suggested rule pattern so the page can offer to save a rule.
    """
    category = _require_category(conn, category_id)
    with transaction(conn):
        row = conn.execute(
            "SELECT description FROM transactions WHERE id = ?", (transaction_id,)
        ).fetchone()
        if row is None:
            raise NotFound("That transaction doesn't exist.")
        if _is_split(conn, transaction_id):
            raise UserError("This transaction is split. Change its parts instead.")
        conn.execute(
            "UPDATE transactions SET category_id = ?, category_source = 'manual' WHERE id = ?",
            (category_id, transaction_id),
        )
    offer_rule = category["kind"] != "uncategorized"
    return {
        "id": transaction_id,
        "category_id": category_id,
        "suggested_pattern": suggest_pattern(row["description"]) if offer_rule else None,
    }


# ---- Splitting -------------------------------------------------------------------

def get_transaction(conn, transaction_id):
    row = conn.execute(
        """SELECT t.id, t.date, t.description, t.amount_cents, t.category_id, t.person_id, a.name AS account_name
           FROM transactions t JOIN accounts a ON a.id = t.account_id WHERE t.id = ?""",
        (transaction_id,),
    ).fetchone()
    if row is None:
        raise NotFound("That transaction doesn't exist.")
    parts = [
        dict(p)
        for p in conn.execute(
            "SELECT id, amount_cents, category_id, person_id FROM transaction_parts WHERE transaction_id = ? ORDER BY id",
            (transaction_id,),
        )
    ]
    return {**dict(row), "parts": parts}


def split_transaction(conn, transaction_id, parts):
    """Replace a transaction's parts. parts: [{"amount_cents", "category_id", "person_id"}, ...]
    with at least two parts that add up exactly to the transaction's amount.
    person_id is optional (None = Household)."""
    txn = get_transaction(conn, transaction_id)
    if not isinstance(parts, list) or len(parts) < 2:
        raise UserError("A split needs at least two parts.")
    cleaned = []
    for i, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            raise UserError("Each part needs an amount and a category.")
        amount = part.get("amount_cents")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount == 0:
            raise UserError(f"Part {i} needs an amount.")
        try:
            category_id = int(part.get("category_id"))
        except (TypeError, ValueError):
            raise UserError(f"Part {i} needs a category.") from None
        _require_category(conn, category_id)
        person_id = part.get("person_id")
        if person_id is not None:
            try:
                person_id = int(person_id)
            except (TypeError, ValueError):
                raise UserError(f"Part {i}'s person isn't valid.") from None
            _require_person(conn, person_id)
        cleaned.append((amount, category_id, person_id))
    total = sum(amount for amount, _, _ in cleaned)
    if total != txn["amount_cents"]:
        difference = (txn["amount_cents"] - total) / 100
        raise UserError(
            f"The parts add up to ${abs(total) / 100:,.2f}, but the transaction is "
            f"${abs(txn['amount_cents']) / 100:,.2f} ({'+' if difference > 0 else '-'}${abs(difference):,.2f} to go)."
        )
    with transaction(conn):
        conn.execute("DELETE FROM transaction_parts WHERE transaction_id = ?", (transaction_id,))
        conn.executemany(
            "INSERT INTO transaction_parts (transaction_id, amount_cents, category_id, person_id) VALUES (?, ?, ?, ?)",
            [(transaction_id, amount, category_id, person_id) for amount, category_id, person_id in cleaned],
        )
    return get_transaction(conn, transaction_id)


def unsplit_transaction(conn, transaction_id):
    """Remove the parts. The transaction goes back to the category it had before."""
    get_transaction(conn, transaction_id)
    with transaction(conn):
        conn.execute("DELETE FROM transaction_parts WHERE transaction_id = ?", (transaction_id,))
        recategorize(conn)
    return get_transaction(conn, transaction_id)


def set_part_category(conn, part_id, category_id):
    _require_category(conn, category_id)
    with transaction(conn):
        if not conn.execute("UPDATE transaction_parts SET category_id = ? WHERE id = ?", (category_id, part_id)).rowcount:
            raise NotFound("That part doesn't exist.")
    return {"id": part_id, "category_id": category_id}


def recent_choices(conn, description, exclude_transaction_id, limit=3):
    """How you categorized the last few transactions from the same store, newest
    first: full category names, or "Split"."""
    merchant = suggest_pattern(description)
    if not merchant:
        return []
    escaped = merchant.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = conn.execute(
        f"""SELECT t.id, coalesce(g.name || '{SEPARATOR}', '') || c.name AS name, c.kind,
                   EXISTS (SELECT 1 FROM transaction_parts p WHERE p.transaction_id = t.id) AS split
            FROM transactions t JOIN categories c ON c.id = t.category_id
            LEFT JOIN categories g ON g.id = c.parent_id
            WHERE upper(t.description) LIKE ? ESCAPE '\\' AND t.id != ?
            ORDER BY t.date DESC, t.id DESC""",
        (f"{escaped}%", exclude_transaction_id),
    )
    choices = []
    for row in rows:
        if row["split"]:
            choices.append("Split")
        elif row["kind"] != "uncategorized":
            choices.append(row["name"])
        if len(choices) == limit:
            break
    return choices


# ---- Rules -------------------------------------------------------------------

def list_rules(conn):
    return [
        dict(row)
        for row in conn.execute(
            f"""SELECT r.id, r.pattern, r.category_id, r.person_id, p.name AS person_name,
                       coalesce(g.name || '{SEPARATOR}', '') || c.name AS category_name
                FROM rules r JOIN categories c ON c.id = r.category_id
                LEFT JOIN categories g ON g.id = c.parent_id
                LEFT JOIN people p ON p.id = r.person_id
                ORDER BY r.pattern"""
        )
    ]


def _rule_target(conn, category_id):
    category = _require_category(conn, category_id)
    if category["kind"] == "uncategorized":
        raise UserError("Rules can't send things to Uncategorized.")
    return category


def create_rule(conn, pattern, category_id, person_id=None):
    """Save a keyword rule and apply it to everything not set by hand.
    person_id (optional) also sets whose spending matching transactions are."""
    pattern = normalize(str(pattern or ""))
    if len(pattern) < 2:
        raise UserError("The rule needs at least 2 characters to match on.")
    _rule_target(conn, category_id)
    if person_id is not None:
        _require_person(conn, person_id)
    with transaction(conn):
        existing = conn.execute("SELECT category_id FROM rules WHERE pattern = ?", (pattern,)).fetchone()
        if existing:
            raise UserError(f"There's already a rule for “{pattern}” (→ {full_name(conn, existing[0])}).")
        rule_id = conn.execute(
            "INSERT INTO rules (pattern, category_id, person_id) VALUES (?, ?, ?)", (pattern, category_id, person_id)
        ).lastrowid
        updated = recategorize(conn)
    return {"id": rule_id, "pattern": pattern, "category_id": category_id, "person_id": person_id, "updated": updated}


def _require_rule(conn, rule_id):
    row = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if not row:
        raise NotFound("That rule doesn't exist.")
    return row


_UNCHANGED = object()


def update_rule(conn, rule_id, category_id=_UNCHANGED, person_id=_UNCHANGED):
    """Change a rule's category and/or person (None = no person)."""
    rule = _require_rule(conn, rule_id)
    category_id = rule["category_id"] if category_id is _UNCHANGED else category_id
    person_id = rule["person_id"] if person_id is _UNCHANGED else person_id
    _rule_target(conn, category_id)
    if person_id is not None:
        _require_person(conn, person_id)
    with transaction(conn):
        conn.execute("UPDATE rules SET category_id = ?, person_id = ? WHERE id = ?", (category_id, person_id, rule_id))
        updated = recategorize(conn)
    return {"id": rule_id, "category_id": category_id, "person_id": person_id, "updated": updated}


def delete_rule(conn, rule_id):
    _require_rule(conn, rule_id)
    with transaction(conn):
        conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        updated = recategorize(conn)
    return {"updated": updated}


# ---- People ----------------------------------------------------------------------
# Whose spending a transaction is. No person means Household (shared).

def list_people(conn):
    return [dict(r) for r in conn.execute("SELECT id, name FROM people ORDER BY sort_order, id")]


def _require_person(conn, person_id, error=UserError):
    row = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
    if row is None:
        raise error("That person doesn't exist.")
    return row


def create_person(conn, name):
    name = _clean_name(name, "person", "Alex")
    if name.lower() == "household":
        raise UserError("“Household” is used for shared spending. Pick another name.")
    try:
        with transaction(conn):
            (order,) = conn.execute("SELECT coalesce(max(sort_order), 0) + 1 FROM people").fetchone()
            person_id = conn.execute("INSERT INTO people (name, sort_order) VALUES (?, ?)", (name, order)).lastrowid
    except sqlite3.IntegrityError:
        raise UserError(f"There's already someone called “{name}”.") from None
    return {"id": person_id, "name": name}


def rename_person(conn, person_id, name):
    _require_person(conn, person_id, NotFound)
    name = _clean_name(name, "person", "Alex")
    if name.lower() == "household":
        raise UserError("“Household” is used for shared spending. Pick another name.")
    try:
        with transaction(conn):
            conn.execute("UPDATE people SET name = ? WHERE id = ?", (name, person_id))
    except sqlite3.IntegrityError:
        raise UserError(f"There's already someone called “{name}”.") from None
    return {"id": person_id, "name": name}


def delete_person(conn, person_id):
    """Remove a person: their transactions go back to Household, rules stop
    setting them, and their budgets are removed."""
    _require_person(conn, person_id, NotFound)
    with transaction(conn):
        moved = conn.execute(
            "UPDATE transactions SET person_id = NULL, person_manual = 0 WHERE person_id = ?", (person_id,)
        ).rowcount
        moved += conn.execute("UPDATE transaction_parts SET person_id = NULL WHERE person_id = ?", (person_id,)).rowcount
        conn.execute("UPDATE rules SET person_id = NULL WHERE person_id = ?", (person_id,))
        budgets_removed = conn.execute("DELETE FROM budgets WHERE person_id = ?", (person_id,)).rowcount
        conn.execute("DELETE FROM people WHERE id = ?", (person_id,))
        recategorize(conn)
    return {"moved": moved, "budgets_removed": budgets_removed}


def set_person(conn, transaction_id, person_id):
    """Set whose spending this is, by hand (None = Household). Rules won't change it."""
    if person_id is not None:
        _require_person(conn, person_id)
    with transaction(conn):
        if not conn.execute("SELECT 1 FROM transactions WHERE id = ?", (transaction_id,)).fetchone():
            raise NotFound("That transaction doesn't exist.")
        if _is_split(conn, transaction_id):
            raise UserError("This transaction is split. Set the person on its parts instead.")
        conn.execute(
            "UPDATE transactions SET person_id = ?, person_manual = 1 WHERE id = ?", (person_id, transaction_id)
        )
    return {"id": transaction_id, "person_id": person_id}


def set_part_person(conn, part_id, person_id):
    if person_id is not None:
        _require_person(conn, person_id)
    with transaction(conn):
        if not conn.execute("UPDATE transaction_parts SET person_id = ? WHERE id = ?", (person_id, part_id)).rowcount:
            raise NotFound("That part doesn't exist.")
    return {"id": part_id, "person_id": person_id}


# ---- Bank categories -----------------------------------------------------------

def list_bank_categories(conn):
    """Every bank category that has a mapping or appears in your transactions."""
    return [
        dict(row)
        for row in conn.execute(
            """SELECT name AS bank_category, (SELECT category_id FROM bank_categories m
                                              WHERE m.bank_category = name) AS category_id,
                      (SELECT count(*) FROM transactions t WHERE t.bank_category = name) AS transaction_count
               FROM (SELECT bank_category AS name FROM bank_categories
                     UNION
                     SELECT DISTINCT bank_category FROM transactions WHERE bank_category IS NOT NULL)
               ORDER BY name COLLATE NOCASE"""
        )
    ]


def set_bank_category(conn, bank_category, category_id):
    """Map a bank's category to yours, or remove the mapping when category_id is None."""
    bank_category = str(bank_category or "").strip()
    if not bank_category:
        raise UserError("Which bank category?")
    if category_id is not None:
        _rule_target(conn, category_id)
    with transaction(conn):
        conn.execute("DELETE FROM bank_categories WHERE bank_category = ?", (bank_category,))
        if category_id is not None:
            conn.execute(
                "INSERT INTO bank_categories (bank_category, category_id) VALUES (?, ?)",
                (bank_category, category_id),
            )
        updated = recategorize(conn)
    return {"bank_category": bank_category, "category_id": category_id, "updated": updated}
