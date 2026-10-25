import tempfile
import unittest
from pathlib import Path

from budgetapp import db, ledger
from tests.fixture_data import FIXTURES


CHASE_CREDIT_HEADER = "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
CITI_HEADER = "Status,Date,Description,Debit,Credit\n"


FULL_NAME = "coalesce(g.name || ' › ', '') || c.name"


def category_id(conn, name: str) -> int:
    """A category's id by full name ("Food › Groceries") or a group's name ("Food")."""
    group, _, sub = name.partition(" › ")
    if sub:
        row = conn.execute(
            """SELECT c.id FROM categories c JOIN categories g ON g.id = c.parent_id
               WHERE g.name = ? AND c.name = ?""",
            (group, sub),
        ).fetchone()
    else:
        row = conn.execute("SELECT id FROM categories WHERE name = ? AND parent_id IS NULL", (name,)).fetchone()
    if row is None:
        raise LookupError(f"No category called {name!r}")
    return row["id"]


def fixture(name: str) -> str:
    """Made-up sample export text (see tests/fixture_data.py)."""
    return FIXTURES[name]


class DbTestCase(unittest.TestCase):
    """Gives each test a fresh database in a temp folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "budget.db"
        db.init(self.db_path)
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def make_account(self, name="Chase Freedom", amount_sign=1) -> int:
        """amount_sign 1: the account's files show purchases as negative (Chase);
        -1: as positive (Amex)."""
        return ledger.create_account(self.conn, name, amount_sign)["id"]

    def category_id(self, name: str) -> int:
        """By full name ("Food › Groceries") or a group's name ("Food")."""
        return category_id(self.conn, name)

    def categories_by_description(self, account_id) -> dict[str, str]:
        """description -> full category name."""
        rows = self.conn.execute(
            f"""SELECT t.description, {FULL_NAME} AS name FROM transactions t
                JOIN categories c ON c.id = t.category_id
                LEFT JOIN categories g ON g.id = c.parent_id WHERE t.account_id = ?""",
            (account_id,),
        )
        return {row["description"]: row["name"] for row in rows}
