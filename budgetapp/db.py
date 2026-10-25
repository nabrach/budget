"""SQLite connection, schema migrations and default data."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import categorize

SCHEMA_V1 = """
CREATE TABLE categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind TEXT NOT NULL CHECK (kind IN ('expense', 'income', 'transfer', 'uncategorized')),
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    format TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE import_batches (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    filename TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    rows_in_file INTEGER NOT NULL,
    rows_added INTEGER NOT NULL,
    rows_skipped INTEGER NOT NULL
);

-- Description keyword -> category. Patterns are stored normalized (uppercase,
-- single spaces) and match whole words anywhere in the description.
CREATE TABLE rules (
    id INTEGER PRIMARY KEY,
    pattern TEXT NOT NULL UNIQUE,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The bank's own category name (e.g. Chase "Food & Drink") -> your category.
CREATE TABLE bank_categories (
    bank_category TEXT PRIMARY KEY COLLATE NOCASE,
    category_id INTEGER NOT NULL REFERENCES categories(id)
);

CREATE TABLE transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    description TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    bank_category TEXT,
    bank_type TEXT,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    -- How the category was chosen: rule, bank (bank category or type), manual, none.
    category_source TEXT NOT NULL CHECK (category_source IN ('rule', 'bank', 'manual', 'none')),
    -- date|amount|description, used to spot rows already imported.
    fingerprint TEXT NOT NULL
);

CREATE INDEX transactions_fingerprint ON transactions(account_id, fingerprint);
CREATE INDEX transactions_date ON transactions(date);
CREATE INDEX transactions_batch ON transactions(batch_id);
"""


def _run_script(conn, script):
    # Not executescript(): it commits any open transaction first.
    # Statements are split at ";", so comments in scripts mustn't contain one.
    for statement in script.split(";"):
        if statement.strip():
            conn.execute(statement)


# The original defaults. Frozen: later migrations reshape them (see v5).
V1_CATEGORIES = [
    ("Housing", "expense"), ("Utilities", "expense"), ("Groceries", "expense"),
    ("Dining", "expense"), ("Transportation", "expense"), ("Gas", "expense"),
    ("Shopping", "expense"), ("Entertainment", "expense"), ("Subscriptions", "expense"),
    ("Health", "expense"), ("Travel", "expense"), ("Personal Care", "expense"),
    ("Gifts", "expense"), ("Fees", "expense"), ("Income", "income"),
    ("Transfer", "transfer"), ("Uncategorized", "uncategorized"),
]
V1_RULES = {
    "PAYMENT THANK YOU": "Transfer",
    "ONLINE PAYMENT, THANK YOU": "Transfer",
    "PAYMENT - THANK YOU": "Transfer",
    "ADJ REDIST": "Transfer",
    "PAYMENT TO CHASE CARD": "Transfer",
    "CITI CARD ONLINE PAYMENT": "Transfer",
    "AMERICAN EXPRESS ACH PMT": "Transfer",
    "ONLINE REALTIME TRANSFER": "Transfer",
    "ONLINE TRANSFER": "Transfer",
    "AUTOPAY": "Transfer",
    "PAYROLL": "Income",
    "LYFT": "Transportation",
    "UBER": "Transportation",
    "UBER EATS": "Dining",
    "NETFLIX": "Subscriptions",
    "SPOTIFY": "Subscriptions",
}
V1_BANK_CATEGORIES = {
    "Food & Drink": "Dining", "Groceries": "Groceries", "Gas": "Gas", "Travel": "Travel",
    "Shopping": "Shopping", "Home": "Shopping", "Entertainment": "Entertainment",
    "Bills & Utilities": "Utilities", "Health & Wellness": "Health", "Personal": "Personal Care",
    "Gifts & Donations": "Gifts", "Fees & Adjustments": "Fees", "Automotive": "Transportation",
}


def _migrate_v1(conn):
    _run_script(conn, SCHEMA_V1)
    conn.executemany(
        "INSERT INTO categories (name, kind, sort_order) VALUES (?, ?, ?)",
        [(name, kind, i) for i, (name, kind) in enumerate(V1_CATEGORIES)],
    )
    ids = {row["name"]: row["id"] for row in conn.execute("SELECT id, name FROM categories")}
    conn.executemany(
        "INSERT INTO rules (pattern, category_id) VALUES (?, ?)",
        [(categorize.normalize(p), ids[c]) for p, c in V1_RULES.items()],
    )
    conn.executemany(
        "INSERT INTO bank_categories (bank_category, category_id) VALUES (?, ?)",
        [(b, ids[c]) for b, c in V1_BANK_CATEGORIES.items()],
    )


SCHEMA_V2 = """
-- Monthly spending limit per expense category. No row = no budget.
CREATE TABLE budgets (
    category_id INTEGER PRIMARY KEY REFERENCES categories(id) ON DELETE CASCADE,
    monthly_limit_cents INTEGER NOT NULL CHECK (monthly_limit_cents > 0)
);

-- App-wide settings, e.g. overall_monthly_limit_cents and warn_pct.
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT INTO settings (key, value) VALUES ('warn_pct', '80')
"""


def _migrate_v2(conn):
    _run_script(conn, SCHEMA_V2)


# Default Transfer rules added after v1 shipped (for Amex). New databases get
# them from DEFAULT_RULES in v1, so this only fills in older ones.
V3_TRANSFER_RULES = ("PAYMENT - THANK YOU", "ADJ REDIST")


def _migrate_v3(conn):
    for pattern in V3_TRANSFER_RULES:
        conn.execute(
            """INSERT OR IGNORE INTO rules (pattern, category_id)
               SELECT ?, id FROM categories WHERE kind = 'transfer'""",
            (pattern,),
        )


SCHEMA_V4 = """
-- 1 if this account's files show purchases as negative numbers, -1 if as
-- positive. NULL until the first single-amount file is confirmed.
ALTER TABLE accounts ADD COLUMN amount_sign INTEGER CHECK (amount_sign IN (1, -1));

-- Header row (normalized) of the last file imported, to warn when it changes.
ALTER TABLE accounts ADD COLUMN columns_signature TEXT;

-- Column choices the user made for files the app couldn't recognize.
CREATE TABLE column_layouts (
    id INTEGER PRIMARY KEY,
    signature TEXT NOT NULL UNIQUE,
    headers TEXT NOT NULL,  -- JSON list, as in the file
    layout TEXT NOT NULL,   -- JSON object: field -> column name
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

# Accounts used to be tied to a bank format. Keep what each one implied.
_V4_FORMATS = {
    "chase_checking": (1, "details|posting date|description|amount|type|balance|check or slip"),
    "chase_credit": (1, "transaction date|post date|description|category|type|amount|memo"),
    "citi_credit": (None, "status|date|description|debit|credit"),
    "amex_credit": (-1, "date|description|amount"),
}


def _migrate_v4(conn):
    _run_script(conn, SCHEMA_V4)
    for key, (sign, signature) in _V4_FORMATS.items():
        conn.execute(
            "UPDATE accounts SET amount_sign = ?, columns_signature = ? WHERE format = ?",
            (sign, signature, key),
        )
    conn.execute("ALTER TABLE accounts DROP COLUMN format")


# ---- v5: groups and subcategories -------------------------------------------------

# Two levels: groups, each with optional subcategories. Kept generic; personal
# subcategories (e.g. a specific charity) are added in the app, not here.
V5_TREE = [
    ("Housing", ["Mortgage", "HOA", "Maintenance/Repair", "Insurance"]),
    ("Utilities", ["Electric", "Water", "Internet", "Phone"]),
    ("Health", ["Gym", "Haircut", "Vitamins", "Medical"]),
    ("Personal", []),
    ("Subscriptions", ["Claude", "Canva", "Adobe", "Namecheap", "Proton"]),
    ("Education", ["529"]),
    ("Transportation", ["Gas", "Insurance", "Maintenance/Repair"]),
    ("Food", ["Groceries", "Restaurants"]),
    ("Gifts", []),
    ("Giving", []),
    ("Entertainment", []),
    ("Insurance", ["Life"]),
    ("Travel", ["Parking", "Airfare", "Rideshare"]),
    ("Fees", []),
]
# Old top-level categories that become something else: (group, subcategory or None).
V5_MOVES = {
    "Groceries": ("Food", "Groceries"),
    "Dining": ("Food", "Restaurants"),
    "Gas": ("Transportation", "Gas"),
    "Personal Care": ("Personal", None),
}
# Dropped: its transactions go to Uncategorized for review (mixed stores like Amazon).
V5_DROPPED = ["Shopping"]
V5_RULES = {
    "LYFT": ("Travel", "Rideshare"),
    "UBER": ("Travel", "Rideshare"),
    "ANTHROPIC": ("Subscriptions", "Claude"),
    "CANVA": ("Subscriptions", "Canva"),
    "ADOBE": ("Subscriptions", "Adobe"),
    "NAME-CHEAP": ("Subscriptions", "Namecheap"),
    "NAMECHEAP": ("Subscriptions", "Namecheap"),
    "PROTON": ("Subscriptions", "Proton"),
}
V5_BANK_CATEGORIES = {
    "Home": ("Housing", "Maintenance/Repair"),
    "Automotive": ("Transportation", "Maintenance/Repair"),
}

SCHEMA_V5 = """
CREATE TABLE categories_new (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('expense', 'income', 'transfer', 'uncategorized')),
    sort_order INTEGER NOT NULL DEFAULT 0,
    -- The group this subcategory is in, or NULL for a group (top level).
    parent_id INTEGER REFERENCES categories(id)
);
INSERT INTO categories_new (id, name, kind, sort_order) SELECT id, name, kind, sort_order FROM categories;
DROP TABLE categories;
ALTER TABLE categories_new RENAME TO categories;
-- Names are unique within a group, so Housing › Insurance and Transportation ›
-- Insurance can both exist.
CREATE UNIQUE INDEX categories_name ON categories (coalesce(parent_id, 0), name COLLATE NOCASE)
"""


def _migrate_v5(conn):
    _run_script(conn, SCHEMA_V5)

    def find(name, parent_id=None):
        row = conn.execute(
            "SELECT id FROM categories WHERE name = ? COLLATE NOCASE AND parent_id IS ?",
            (name, parent_id),
        ).fetchone()
        return row["id"] if row else None

    def ensure(name, parent_id=None):
        return find(name, parent_id) or conn.execute(
            "INSERT INTO categories (name, kind, parent_id) VALUES (?, 'expense', ?)", (name, parent_id)
        ).lastrowid

    def merge(old_id, new_id):
        """Point everything at old_id to new_id, then remove old_id."""
        for table in ("transactions", "rules", "bank_categories"):
            conn.execute(f"UPDATE {table} SET category_id = ? WHERE category_id = ?", (new_id, old_id))
        # Budgets move too, unless the new category already has one.
        conn.execute("UPDATE OR IGNORE budgets SET category_id = ? WHERE category_id = ?", (new_id, old_id))
        conn.execute("DELETE FROM budgets WHERE category_id = ?", (old_id,))
        conn.execute("DELETE FROM categories WHERE id = ?", (old_id,))

    tree = {}
    for group, subs in V5_TREE:
        group_id = ensure(group)
        tree[group] = group_id
        for sub in subs:
            tree[(group, sub)] = ensure(sub, group_id)

    for old, (group, sub) in V5_MOVES.items():
        old_id = find(old)
        if old_id:
            merge(old_id, tree[(group, sub)] if sub else tree[group])

    uncategorized = conn.execute("SELECT id FROM categories WHERE kind = 'uncategorized'").fetchone()["id"]
    for old in V5_DROPPED:
        old_id = find(old)
        if old_id:
            conn.execute("DELETE FROM rules WHERE category_id = ?", (old_id,))
            conn.execute("DELETE FROM bank_categories WHERE category_id = ?", (old_id,))
            conn.execute("DELETE FROM budgets WHERE category_id = ?", (old_id,))
            conn.execute(
                "UPDATE transactions SET category_id = ?, category_source = 'none' WHERE category_id = ?",
                (uncategorized, old_id),
            )
            conn.execute("DELETE FROM categories WHERE id = ?", (old_id,))

    for pattern, (group, sub) in V5_RULES.items():
        conn.execute(
            """INSERT INTO rules (pattern, category_id) VALUES (?, ?)
               ON CONFLICT (pattern) DO UPDATE SET category_id = excluded.category_id""",
            (pattern, tree[(group, sub)]),
        )
    for bank_category, (group, sub) in V5_BANK_CATEGORIES.items():
        conn.execute(
            "INSERT OR REPLACE INTO bank_categories (bank_category, category_id) VALUES (?, ?)",
            (bank_category, tree[(group, sub)]),
        )

    # Order: the tree as listed, then any other groups, then the built-ins.
    order = 0
    for group, subs in V5_TREE:
        conn.execute("UPDATE categories SET sort_order = ? WHERE id = ?", (order, tree[group]))
        order += 1
        for sub in subs:
            conn.execute("UPDATE categories SET sort_order = ? WHERE id = ?", (order, tree[(group, sub)]))
            order += 1
    for row in conn.execute(
        """SELECT id FROM categories WHERE parent_id IS NULL AND id NOT IN ({})
           ORDER BY kind = 'expense' DESC, kind = 'income' DESC, kind = 'transfer' DESC, sort_order""".format(
            ",".join(str(i) for i in (tree[g] for g, _ in V5_TREE))
        )
    ).fetchall():
        conn.execute("UPDATE categories SET sort_order = ? WHERE id = ?", (order, row["id"]))
        order += 1


# ---- v6: split transactions ---------------------------------------------------------

SCHEMA_V6 = """
-- A split transaction's parts. Parts replace the transaction in every total.
CREATE TABLE transaction_parts (
    id INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    amount_cents INTEGER NOT NULL CHECK (amount_cents != 0),
    category_id INTEGER NOT NULL REFERENCES categories(id)
);
CREATE INDEX transaction_parts_transaction ON transaction_parts(transaction_id);

-- What every total, chart and budget counts: unsplit transactions, plus the
-- parts of split ones.
CREATE VIEW entries AS
SELECT t.id AS transaction_id, NULL AS part_id, t.account_id, t.batch_id, t.date,
       t.description, t.amount_cents, t.category_id, t.category_source, t.bank_category
FROM transactions t
WHERE NOT EXISTS (SELECT 1 FROM transaction_parts p WHERE p.transaction_id = t.id)
UNION ALL
SELECT t.id, p.id, t.account_id, t.batch_id, t.date,
       t.description, p.amount_cents, p.category_id, 'manual', t.bank_category
FROM transaction_parts p JOIN transactions t ON t.id = p.transaction_id
"""


def _migrate_v6(conn):
    _run_script(conn, SCHEMA_V6)


# ---- v7: people ---------------------------------------------------------------------

SCHEMA_V7 = """
CREATE TABLE people (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    sort_order INTEGER NOT NULL DEFAULT 0
);

-- Whose spending this is. NULL means Household (shared).
ALTER TABLE transactions ADD COLUMN person_id INTEGER REFERENCES people(id);
-- 1 when you set the person by hand, so rules leave it alone.
ALTER TABLE transactions ADD COLUMN person_manual INTEGER NOT NULL DEFAULT 0;
ALTER TABLE transaction_parts ADD COLUMN person_id INTEGER REFERENCES people(id);
-- A rule can set the person as well as the category.
ALTER TABLE rules ADD COLUMN person_id INTEGER REFERENCES people(id);

-- Budgets per category and person. person_id NULL means everyone.
CREATE TABLE budgets_new (
    id INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    person_id INTEGER REFERENCES people(id) ON DELETE CASCADE,
    monthly_limit_cents INTEGER NOT NULL CHECK (monthly_limit_cents > 0)
);
INSERT INTO budgets_new (category_id, monthly_limit_cents) SELECT category_id, monthly_limit_cents FROM budgets;
DROP TABLE budgets;
ALTER TABLE budgets_new RENAME TO budgets;
CREATE UNIQUE INDEX budgets_category_person ON budgets (category_id, coalesce(person_id, 0));

DROP VIEW entries;
CREATE VIEW entries AS
SELECT t.id AS transaction_id, NULL AS part_id, t.account_id, t.batch_id, t.date,
       t.description, t.amount_cents, t.category_id, t.category_source, t.bank_category, t.person_id
FROM transactions t
WHERE NOT EXISTS (SELECT 1 FROM transaction_parts p WHERE p.transaction_id = t.id)
UNION ALL
SELECT t.id, p.id, t.account_id, t.batch_id, t.date,
       t.description, p.amount_cents, p.category_id, 'manual', t.bank_category, p.person_id
FROM transaction_parts p JOIN transactions t ON t.id = p.transaction_id
"""


def _migrate_v7(conn):
    _run_script(conn, SCHEMA_V7)


# Append new migrations here; never edit one that has shipped.
MIGRATIONS = [_migrate_v1, _migrate_v2, _migrate_v3, _migrate_v4, _migrate_v5, _migrate_v6, _migrate_v7]


def connect(path: Path) -> sqlite3.Connection:
    # isolation_level=None: no hidden transactions; use transaction() explicitly.
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(path: Path) -> None:
    """Create the database if needed and bring it up to the latest schema."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        # Some migrations rebuild tables that others point to, which SQLite only
        # allows with foreign keys off. Each migration is checked before commit.
        conn.execute("PRAGMA foreign_keys = OFF")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for number, migration in enumerate(MIGRATIONS[version:], start=version + 1):
            with transaction(conn):
                migration(conn)
                problems = conn.execute("PRAGMA foreign_key_check").fetchall()
                if problems:
                    raise RuntimeError(f"Migration {number} left broken references: {problems[:5]}")
                conn.execute(f"PRAGMA user_version = {number}")
        if version < len(MIGRATIONS):
            # Migrations can change rules and categories. Re-sort afterwards, once,
            # with today's code (migrations themselves must not call app code,
            # since it may expect tables a later migration creates).
            conn.execute("PRAGMA foreign_keys = ON")
            with transaction(conn):
                categorize.recategorize(conn)
    finally:
        conn.close()


@contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
