import tempfile
import unittest
from datetime import date
from pathlib import Path

from budgetapp import budgets, db, ledger
from budgetapp.errors import NotFound, UserError
from budgetapp.importer import import_csv
from budgetapp.queries import HOUSEHOLD, Filter, group_totals, list_transactions
from tests.helpers import CHASE_CREDIT_HEADER, DbTestCase, category_id

CARD = CHASE_CREDIT_HEADER + "".join(
    [
        "10/02/2026,10/02/2026,BJJ ACADEMY,,Sale,-150.00,\n",
        "10/03/2026,10/03/2026,BOOKSTORE,,Sale,-40.00,\n",
        "10/04/2026,10/04/2026,CORNER GROCER,Groceries,Sale,-80.00,\n",
        "10/05/2026,10/05/2026,HAIR STUDIO,,Sale,-35.00,\n",
    ]
)
OCT_10 = date(2026, 10, 10)


class PeopleTestCase(DbTestCase):
    def setUp(self):
        super().setUp()
        self.alex = ledger.create_person(self.conn, "Alex")["id"]
        self.sam = ledger.create_person(self.conn, "Sam")["id"]
        self.account = self.make_account()
        self.personal = self.category_id("Personal")

    def import_card(self):
        import_csv(self.conn, self.account, "card.csv", CARD)

    def txn(self, description):
        return self.conn.execute("SELECT id FROM transactions WHERE description = ?", (description,)).fetchone()[0]

    def people_by_description(self):
        rows = self.conn.execute(
            "SELECT t.description, p.name FROM transactions t LEFT JOIN people p ON p.id = t.person_id"
        )
        return {r[0]: r[1] or "Household" for r in rows}


class PeopleListTest(PeopleTestCase):
    def test_list_in_order(self):
        self.assertEqual([p["name"] for p in ledger.list_people(self.conn)], ["Alex", "Sam"])

    def test_names(self):
        with self.assertRaisesRegex(UserError, "already someone"):
            ledger.create_person(self.conn, "alex")
        with self.assertRaisesRegex(UserError, "Household"):
            ledger.create_person(self.conn, "household")
        ledger.rename_person(self.conn, self.sam, "Sammy")
        self.assertEqual([p["name"] for p in ledger.list_people(self.conn)], ["Alex", "Sammy"])
        with self.assertRaises(NotFound):
            ledger.rename_person(self.conn, 999, "X")


class AssigningPeopleTest(PeopleTestCase):
    def test_everything_starts_as_household(self):
        self.import_card()
        self.assertEqual(set(self.people_by_description().values()), {"Household"})

    def test_rules_can_set_the_person(self):
        ledger.create_rule(self.conn, "BJJ ACADEMY", self.category_id("Health"), self.sam)
        self.import_card()
        found = self.people_by_description()
        self.assertEqual((found["BJJ ACADEMY"], found["BOOKSTORE"]), ("Sam", "Household"))

    def test_new_rule_updates_existing_transactions(self):
        self.import_card()
        ledger.create_rule(self.conn, "HAIR STUDIO", self.category_id("Health › Haircut"), self.alex)
        self.assertEqual(self.people_by_description()["HAIR STUDIO"], "Alex")

    def test_changing_a_rules_person(self):
        rule = ledger.create_rule(self.conn, "BJJ ACADEMY", self.category_id("Health"), self.sam)
        self.import_card()
        ledger.update_rule(self.conn, rule["id"], person_id=self.alex)
        self.assertEqual(self.people_by_description()["BJJ ACADEMY"], "Alex")
        ledger.update_rule(self.conn, rule["id"], person_id=None)
        self.assertEqual(self.people_by_description()["BJJ ACADEMY"], "Household")
        saved = next(r for r in ledger.list_rules(self.conn) if r["id"] == rule["id"])
        self.assertEqual(saved["category_id"], self.category_id("Health"))  # unchanged

    def test_a_person_set_by_hand_is_kept(self):
        self.import_card()
        ledger.set_person(self.conn, self.txn("BJJ ACADEMY"), self.alex)
        ledger.create_rule(self.conn, "BJJ ACADEMY", self.category_id("Health"), self.sam)
        self.assertEqual(self.people_by_description()["BJJ ACADEMY"], "Alex")

    def test_person_and_category_are_independent(self):
        self.import_card()
        ledger.set_category(self.conn, self.txn("BOOKSTORE"), self.personal)  # category by hand
        ledger.create_rule(self.conn, "BOOKSTORE", self.category_id("Gifts"), self.sam)
        found = self.categories_by_description(self.account)
        self.assertEqual((found["BOOKSTORE"], self.people_by_description()["BOOKSTORE"]), ("Personal", "Sam"))

    def test_split_parts_have_their_own_people(self):
        self.import_card()
        order = self.txn("BOOKSTORE")
        ledger.split_transaction(self.conn, order, [
            {"amount_cents": -2500, "category_id": self.personal, "person_id": self.alex},
            {"amount_cents": -1500, "category_id": self.personal, "person_id": self.sam},
        ])
        rows = list_transactions(self.conn, Filter(person=self.sam))["rows"]
        self.assertEqual([(r["description"], r["amount_cents"], r["person_name"]) for r in rows], [("BOOKSTORE", -1500, "Sam")])
        part = ledger.get_transaction(self.conn, order)["parts"][1]["id"]
        ledger.set_part_person(self.conn, part, None)
        self.assertEqual(list_transactions(self.conn, Filter(person=self.sam))["count"], 0)
        with self.assertRaisesRegex(UserError, "split"):
            ledger.set_person(self.conn, order, self.alex)


class FiltersTest(PeopleTestCase):
    def setUp(self):
        super().setUp()
        self.import_card()
        ledger.set_person(self.conn, self.txn("BJJ ACADEMY"), self.sam)
        ledger.set_person(self.conn, self.txn("HAIR STUDIO"), self.alex)

    def test_filter_by_person_and_household(self):
        self.assertEqual(list_transactions(self.conn, Filter(person=self.sam))["spent_cents"], 15000)
        self.assertEqual(list_transactions(self.conn, Filter(person=HOUSEHOLD))["count"], 2)

    def test_group_by_person(self):
        groups = group_totals(self.conn, Filter(), "person")
        self.assertEqual(
            [(g["label"], g["spent_cents"]) for g in groups],
            [("Sam", 15000), ("Household", 12000), ("Alex", 3500)],
        )
        self.assertEqual(groups[1]["filter"], {"person": HOUSEHOLD})

    def test_rows_include_the_person(self):
        rows = {r["description"]: r["person_name"] for r in list_transactions(self.conn)["rows"]}
        self.assertEqual((rows["BJJ ACADEMY"], rows["BOOKSTORE"]), ("Sam", None))


class ReviewListTest(PeopleTestCase):
    def setUp(self):
        super().setUp()
        self.import_card()
        for description in ("BOOKSTORE", "HAIR STUDIO"):
            ledger.set_category(self.conn, self.txn(description), self.personal)

    def review(self):
        return {r["description"] for r in list_transactions(self.conn, Filter(needs_review=True))["rows"]}

    def test_without_per_person_budgets_only_uncategorized_needs_review(self):
        self.assertEqual(self.review(), {"BJJ ACADEMY"})

    def test_per_person_budget_flags_transactions_missing_a_person(self):
        budgets.save_budgets(self.conn, [{"category_id": self.personal, "person_id": self.alex, "monthly_limit_cents": 20000}], None, 80)
        self.assertEqual(self.review(), {"BJJ ACADEMY", "BOOKSTORE", "HAIR STUDIO"})
        ledger.set_person(self.conn, self.txn("BOOKSTORE"), self.sam)
        self.assertEqual(self.review(), {"BJJ ACADEMY", "HAIR STUDIO"})

    def test_a_group_budget_covers_its_subcategories(self):
        haircut = self.category_id("Health › Haircut")
        ledger.set_category(self.conn, self.txn("HAIR STUDIO"), haircut)
        budgets.save_budgets(self.conn, [{"category_id": self.category_id("Health"), "person_id": self.alex, "monthly_limit_cents": 5000}], None, 80)
        self.assertIn("HAIR STUDIO", self.review())


class PerPersonBudgetTest(PeopleTestCase):
    def test_each_person_has_their_own_limit(self):
        self.import_card()
        for description, person in (("BOOKSTORE", self.alex), ("HAIR STUDIO", self.sam)):
            ledger.set_category(self.conn, self.txn(description), self.personal)
            ledger.set_person(self.conn, self.txn(description), person)
        budgets.save_budgets(
            self.conn,
            [
                {"category_id": self.personal, "person_id": self.alex, "monthly_limit_cents": 20000},
                {"category_id": self.personal, "person_id": self.sam, "monthly_limit_cents": 3000},
            ],
            None,
            80,
        )
        status = budgets.budget_status(self.conn, OCT_10)
        found = {s["name"]: (s["spent_cents"], s["state"]) for s in status["categories"]}
        self.assertEqual(found, {"Personal (Alex)": (4000, "ok"), "Personal (Sam)": (3500, "over")})
        self.assertEqual(status["alerts"], ["Personal (Sam) is over budget by $5.00 ($35.00 of $30.00)."])

    def test_everyone_budget_counts_all_people(self):
        self.import_card()
        ledger.set_person(self.conn, self.txn("CORNER GROCER"), self.alex)
        food = self.category_id("Food")
        budgets.save_budgets(self.conn, [{"category_id": food, "monthly_limit_cents": 10000}], None, 80)
        [status] = budgets.budget_status(self.conn, OCT_10)["categories"]
        self.assertEqual((status["name"], status["spent_cents"]), ("Food", 8000))


class DeletePersonTest(PeopleTestCase):
    def test_delete_moves_everything_back_to_household(self):
        rule = ledger.create_rule(self.conn, "BJJ ACADEMY", self.category_id("Health"), self.sam)
        self.import_card()
        ledger.set_person(self.conn, self.txn("BOOKSTORE"), self.sam)
        budgets.save_budgets(self.conn, [{"category_id": self.personal, "person_id": self.sam, "monthly_limit_cents": 100}], None, 80)
        result = ledger.delete_person(self.conn, self.sam)
        self.assertEqual(result, {"moved": 2, "budgets_removed": 1})
        self.assertEqual(set(self.people_by_description().values()), {"Household"})
        self.assertIsNone(next(r for r in ledger.list_rules(self.conn) if r["id"] == rule["id"])["person_id"])
        self.assertEqual([p["name"] for p in ledger.list_people(self.conn)], ["Alex"])


class UpgradeTest(unittest.TestCase):
    def test_v7_keeps_budgets_for_everyone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = db.connect(path)
            conn.execute("PRAGMA foreign_keys = OFF")  # as db.init does, for table rebuilds
            for number, migration in enumerate(db.MIGRATIONS[:6], start=1):
                with db.transaction(conn):
                    migration(conn)
                    conn.execute(f"PRAGMA user_version = {number}")
            food = category_id(conn, "Food")
            conn.execute("INSERT INTO budgets (category_id, monthly_limit_cents) VALUES (?, 50000)", (food,))
            db.init(path)
            self.assertEqual(
                budgets.get_budgets(conn)["limits"],
                [{"category_id": food, "person_id": None, "monthly_limit_cents": 50000}],
            )
            conn.close()


if __name__ == "__main__":
    unittest.main()
