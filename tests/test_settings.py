import tempfile
import unittest
from pathlib import Path

from budgetapp import budgets, db, ledger
from budgetapp.errors import NotFound, UserError
from budgetapp.formats import header_signature, read_header
from budgetapp.importer import import_csv, undo_import
from budgetapp.queries import Filter, group_totals, list_transactions
from tests.helpers import DbTestCase, fixture


class MigrationTest(DbTestCase):
    def test_init_is_repeatable(self):
        db.init(self.db_path)  # already initialized in setUp
        (version,) = self.conn.execute("PRAGMA user_version").fetchone()
        self.assertEqual(version, len(db.MIGRATIONS))


class OlderDatabaseTest(unittest.TestCase):
    """Upgrading databases created by earlier versions of the app."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "old.db"

    def tearDown(self):
        self._tmp.cleanup()

    def old_database(self, version):
        conn = db.connect(self.path)
        conn.execute("PRAGMA foreign_keys = OFF")  # as db.init does, for table rebuilds
        for number, migration in enumerate(db.MIGRATIONS[:version], start=1):
            with db.transaction(conn):
                migration(conn)
                conn.execute(f"PRAGMA user_version = {number}")
        return conn

    def test_v3_adds_amex_rules(self):
        conn = self.old_database(2)
        conn.execute("DELETE FROM rules WHERE pattern IN ('PAYMENT - THANK YOU', 'ADJ REDIST')")
        db.init(self.path)
        patterns = {r["pattern"] for r in ledger.list_rules(conn)}
        self.assertTrue({"PAYMENT - THANK YOU", "ADJ REDIST"} <= patterns)
        conn.close()

    def test_v3_keeps_a_rule_the_user_already_has(self):
        conn = self.old_database(2)
        fees = conn.execute("SELECT id FROM categories WHERE name = 'Fees'").fetchone()["id"]
        conn.execute("UPDATE rules SET category_id = ? WHERE pattern = 'ADJ REDIST'", (fees,))
        db.init(self.path)
        rule = next(r for r in ledger.list_rules(conn) if r["pattern"] == "ADJ REDIST")
        self.assertEqual(rule["category_name"], "Fees")
        conn.close()

    def test_v4_replaces_bank_formats(self):
        conn = self.old_database(3)
        for name, fmt in [("Checking", "chase_checking"), ("Freedom", "chase_credit"), ("Citi", "citi_credit"), ("Amex", "amex_credit")]:
            conn.execute("INSERT INTO accounts (name, format) VALUES (?, ?)", (name, fmt))
        db.init(self.path)
        accounts = {
            row["name"]: (row["amount_sign"], row["columns_signature"])
            for row in conn.execute("SELECT * FROM accounts")
        }
        self.assertEqual(accounts["Checking"], (1, header_signature(read_header(fixture("chase_checking.csv")))))
        self.assertEqual(accounts["Freedom"], (1, header_signature(read_header(fixture("chase_credit_1.csv")))))
        self.assertEqual(accounts["Citi"], (None, header_signature(read_header(fixture("citi_credit.csv")))))
        self.assertEqual(accounts["Amex"], (-1, header_signature(read_header(fixture("amex_credit.csv")))))
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(accounts)")]
        self.assertNotIn("format", columns)
        conn.close()

    def test_v5_moves_old_categories_into_the_new_tree(self):
        conn = self.old_database(4)
        ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM categories")}
        conn.execute("INSERT INTO accounts (name, amount_sign) VALUES ('Card', 1)")
        conn.execute("INSERT INTO import_batches (account_id, filename, rows_in_file, rows_added, rows_skipped) VALUES (1, 'a.csv', 4, 4, 0)")
        rows = [
            ("COFFEE SHOP", "Dining", "manual"),  # set by hand: should stay set, in its new place
            ("AMAZON ORDER", "Shopping", "bank"),  # Shopping is dropped: back to review
            ("FUEL STOP", "Gas", "rule"),
            ("LYFT RIDE", "Transportation", "rule"),
        ]
        for i, (description, category, source) in enumerate(rows):
            conn.execute(
                """INSERT INTO transactions (account_id, batch_id, date, description, amount_cents,
                       category_id, category_source, fingerprint) VALUES (1, 1, '2026-10-01', ?, -100, ?, ?, ?)""",
                (description, ids[category], source, str(i)),
            )
        conn.execute("INSERT INTO rules (pattern, category_id) VALUES ('FUEL STOP', ?)", (ids["Gas"],))
        conn.execute("INSERT INTO budgets (category_id, monthly_limit_cents) VALUES (?, 60000)", (ids["Groceries"],))
        conn.execute("INSERT INTO budgets (category_id, monthly_limit_cents) VALUES (?, 5000)", (ids["Shopping"],))
        db.init(self.path)

        found = dict(conn.execute(
            """SELECT t.description, coalesce(g.name || ' › ', '') || c.name FROM transactions t
               JOIN categories c ON c.id = t.category_id LEFT JOIN categories g ON g.id = c.parent_id"""
        ).fetchall())
        self.assertEqual(found, {
            "COFFEE SHOP": "Food › Restaurants",
            "AMAZON ORDER": "Uncategorized",
            "FUEL STOP": "Transportation › Gas",
            "LYFT RIDE": "Travel › Rideshare",
        })
        source = conn.execute("SELECT category_source FROM transactions WHERE description = 'COFFEE SHOP'").fetchone()[0]
        self.assertEqual(source, "manual")
        rule = next(r for r in ledger.list_rules(conn) if r["pattern"] == "FUEL STOP")
        self.assertEqual(rule["category_name"], "Transportation › Gas")
        limits = budgets.get_budgets(conn)["limits"]
        groceries = conn.execute(
            "SELECT c.id FROM categories c JOIN categories g ON g.id = c.parent_id WHERE g.name = 'Food' AND c.name = 'Groceries'"
        ).fetchone()[0]
        self.assertEqual(limits, [{"category_id": groceries, "person_id": None, "monthly_limit_cents": 60000}])  # Shopping's is gone
        conn.close()

    def test_v4_upgraded_accounts_import_without_questions(self):
        conn = self.old_database(3)
        conn.execute("INSERT INTO accounts (name, format) VALUES ('Checking', 'chase_checking')")
        db.init(self.path)
        account = conn.execute("SELECT id FROM accounts").fetchone()["id"]
        self.assertEqual(import_csv(conn, account, "c.csv", fixture("chase_checking.csv")).rows_added, 27)
        conn.close()


class BudgetTest(DbTestCase):
    def limit(self, category, cents, person_id=None):
        return {"category_id": self.category_id(category), "person_id": person_id, "monthly_limit_cents": cents}

    def test_defaults(self):
        self.assertEqual(
            budgets.get_budgets(self.conn), {"limits": [], "overall_cents": None, "warn_pct": 80}
        )

    def test_save_and_replace(self):
        budgets.save_budgets(self.conn, [self.limit("Food › Groceries", 60000), self.limit("Food", 90000)], 300000, 75)
        saved = budgets.save_budgets(self.conn, [self.limit("Food › Groceries", 65000), self.limit("Food", None)], None, 90)
        self.assertEqual(
            saved,
            {"limits": [self.limit("Food › Groceries", 65000)], "overall_cents": None, "warn_pct": 90},
        )

    def test_per_person_budgets(self):
        alex = ledger.create_person(self.conn, "Alex")["id"]
        sam = ledger.create_person(self.conn, "Sam")["id"]
        saved = budgets.save_budgets(
            self.conn, [self.limit("Personal", 20000, alex), self.limit("Personal", 15000, sam)], None, 80
        )
        self.assertEqual(len(saved["limits"]), 2)

    def test_validation(self):
        bad = [
            ([self.limit("Food › Groceries", 0)], None, 80),
            ([self.limit("Food › Groceries", -5)], None, 80),
            ([self.limit("Food › Groceries", 12.5)], None, 80),
            ([self.limit("Food › Groceries", 100)], None, 100),
            ([self.limit("Food › Groceries", 100)], None, 0),
            ([{"category_id": 999, "monthly_limit_cents": 100}], None, 80),
            ([self.limit("Income", 100)], None, 80),
            ([self.limit("Food", 100), self.limit("Food", 200)], None, 80),
            ([self.limit("Food", 100, person_id=999)], None, 80),
            ({}, None, 80),
            ([], 0, 80),
        ]
        for limits, overall, warn in bad:
            with self.subTest(limits=limits, overall=overall, warn=warn):
                with self.assertRaises(UserError):
                    budgets.save_budgets(self.conn, limits, overall, warn)

    def test_deleting_a_category_removes_its_budget(self):
        budgets.save_budgets(self.conn, [self.limit("Food › Groceries", 60000)], None, 80)
        ledger.delete_category(self.conn, self.category_id("Food › Groceries"))
        self.assertEqual(budgets.get_budgets(self.conn)["limits"], [])


class CategoryTreeTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.account = self.make_account()
        import_csv(self.conn, self.account, "a.csv", fixture("chase_credit_1.csv"))

    def names(self):
        return [c["full_name"] for c in ledger.list_categories(self.conn)]

    def test_default_tree_in_order(self):
        names = self.names()
        self.assertEqual(names[:5], ["Housing", "Housing › Mortgage", "Housing › HOA", "Housing › Maintenance/Repair", "Housing › Insurance"])
        self.assertIn("Food › Groceries", names)
        self.assertIn("Travel › Rideshare", names)
        self.assertNotIn("Shopping", names)
        self.assertEqual(names[-3:], ["Income", "Transfer", "Uncategorized"])
        # The same name can be used in different groups.
        self.assertIn("Transportation › Insurance", names)

    def test_create_group_and_subcategory(self):
        pets = ledger.create_category(self.conn, "  Pets ", "expense")
        food = ledger.create_category(self.conn, "Food", parent_id=pets["id"])
        self.assertEqual(food["full_name"], "Pets › Food")
        names = self.names()
        # New groups go after the spending groups, before Income and the built-ins.
        self.assertEqual(names[-5:], ["Pets", "Pets › Food", "Income", "Transfer", "Uncategorized"])

    def test_rename(self):
        result = ledger.rename_category(self.conn, self.category_id("Food › Restaurants"), "Eating out")
        self.assertEqual(result["full_name"], "Food › Eating out")
        self.assertEqual(self.categories_by_description(self.account)["LAKESIDE GRILL SPRINGFIELD"], "Food › Eating out")

    def test_names_are_unique_within_a_group(self):
        with self.assertRaisesRegex(UserError, "already a category called “food”"):
            ledger.create_category(self.conn, "food", "expense")
        with self.assertRaisesRegex(UserError, "in Food"):
            ledger.create_category(self.conn, "groceries", parent_id=self.category_id("Food"))
        with self.assertRaisesRegex(UserError, "in Food"):
            ledger.rename_category(self.conn, self.category_id("Food › Restaurants"), "Groceries")

    def test_only_two_levels(self):
        with self.assertRaisesRegex(UserError, "only go in a group"):
            ledger.create_category(self.conn, "Deep", parent_id=self.category_id("Food › Groceries"))
        with self.assertRaisesRegex(UserError, "can't have subcategories"):
            ledger.create_category(self.conn, "Sub", parent_id=self.category_id("Transfer"))

    def test_only_spending_or_income_can_be_created(self):
        with self.assertRaises(UserError):
            ledger.create_category(self.conn, "Moves", "transfer")

    def test_move_subcategory_to_another_group(self):
        ledger.move_category(self.conn, self.category_id("Food › Restaurants"), self.category_id("Entertainment"))
        self.assertEqual(self.categories_by_description(self.account)["LAKESIDE GRILL SPRINGFIELD"], "Entertainment › Restaurants")

    def test_move_to_top_level_and_back(self):
        restaurants = self.category_id("Food › Restaurants")
        ledger.move_category(self.conn, restaurants, None)
        self.assertIn("Restaurants", self.names())
        ledger.move_category(self.conn, restaurants, self.category_id("Food"))
        self.assertIn("Food › Restaurants", self.names())

    def test_move_rules(self):
        food = self.category_id("Food")
        with self.assertRaisesRegex(UserError, "has subcategories"):
            ledger.move_category(self.conn, food, self.category_id("Entertainment"))
        with self.assertRaisesRegex(UserError, "built in"):
            ledger.move_category(self.conn, self.category_id("Transfer"), food)
        with self.assertRaisesRegex(UserError, "in Housing"):
            ledger.move_category(self.conn, self.category_id("Transportation › Insurance"), self.category_id("Housing"))

    def test_built_in_categories_can_be_renamed_but_not_deleted(self):
        transfer = self.category_id("Transfer")
        ledger.rename_category(self.conn, transfer, "Between my accounts")
        with self.assertRaisesRegex(UserError, "built in"):
            ledger.delete_category(self.conn, transfer)
        with self.assertRaisesRegex(UserError, "built in"):
            ledger.delete_category(self.conn, self.category_id("Uncategorized"))

    def test_deleting_a_subcategory_moves_everything_up_to_its_group(self):
        rideshare = self.category_id("Travel › Rideshare")
        result = ledger.delete_category(self.conn, rideshare)
        self.assertEqual((result["moved"], result["moved_to"]), (6, "Travel"))
        self.assertEqual(self.categories_by_description(self.account)["LYFT *RIDE TRIP A1"], "Travel")
        lyft = next(r for r in ledger.list_rules(self.conn) if r["pattern"] == "LYFT")
        self.assertEqual(lyft["category_name"], "Travel")  # the rule moved up too

    def test_deleting_a_group_resorts_its_transactions(self):
        result = ledger.delete_category(self.conn, self.category_id("Travel"))
        self.assertEqual((result["moved"], result["rules_removed"]), (6, 2))  # LYFT and UBER rules
        self.assertEqual(self.categories_by_description(self.account)["LYFT *RIDE TRIP A1"], "Uncategorized")
        self.assertNotIn("Travel › Rideshare", self.names())

    def test_delete_unknown(self):
        with self.assertRaises(NotFound):
            ledger.delete_category(self.conn, 999)

    def test_list_includes_usage_counts(self):
        restaurants = next(c for c in ledger.list_categories(self.conn) if c["full_name"] == "Food › Restaurants")
        self.assertEqual((restaurants["transaction_count"], restaurants["rule_count"]), (3, 1))  # UBER EATS
        food = next(c for c in ledger.list_categories(self.conn) if c["full_name"] == "Food")
        self.assertTrue(food["has_children"])


class SplitTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.account = self.make_account()
        import_csv(self.conn, self.account, "a.csv", fixture("chase_credit_2.csv"))
        # AMAZON MKTPL*AB0000005, -$418.37
        self.order = self.conn.execute("SELECT id FROM transactions WHERE amount_cents = -41837").fetchone()[0]
        self.gifts, self.personal = self.category_id("Gifts"), self.category_id("Personal")
        self.groceries = self.category_id("Food › Groceries")

    def split(self, *parts):
        return ledger.split_transaction(
            self.conn, self.order, [{"amount_cents": a, "category_id": c} for a, c in parts]
        )

    def test_parts_replace_the_transaction_in_totals(self):
        self.split((-30000, self.gifts), (-10000, self.personal), (-1837, self.groceries))
        by_label = {g["label"]: g["spent_cents"] for g in group_totals(self.conn, Filter(), "category")}
        self.assertEqual((by_label["Gifts"], by_label["Personal"], by_label["Food"]), (30000, 10000, 1837))
        rows = list_transactions(self.conn, Filter(category_id=self.gifts))["rows"]
        self.assertEqual([(r["id"], r["amount_cents"], r["part_number"], r["parts_count"]) for r in rows], [(self.order, -30000, 1, 3)])

    def test_overall_total_is_unchanged(self):
        before = list_transactions(self.conn)["spent_cents"]
        self.split((-30000, self.gifts), (-11837, self.personal))
        self.assertEqual(list_transactions(self.conn)["spent_cents"], before)

    def test_parts_must_add_up(self):
        with self.assertRaisesRegex(UserError, r"add up to \$400.00.*\$418.37.*\$18.37 to go"):
            self.split((-30000, self.gifts), (-10000, self.personal))

    def test_validation(self):
        with self.assertRaisesRegex(UserError, "at least two"):
            self.split((-41837, self.gifts))
        with self.assertRaisesRegex(UserError, "Part 2 needs an amount"):
            self.split((-41837, self.gifts), (0, self.personal))
        with self.assertRaises(UserError):
            self.split((-41837, self.gifts), (-1, 999))

    def test_resplitting_replaces_parts(self):
        self.split((-30000, self.gifts), (-11837, self.personal))
        result = self.split((-35000, self.gifts), (-6837, self.personal))
        self.assertEqual([p["amount_cents"] for p in result["parts"]], [-35000, -6837])

    def test_a_split_transaction_cannot_get_a_single_category(self):
        self.split((-30000, self.gifts), (-11837, self.personal))
        with self.assertRaisesRegex(UserError, "split"):
            ledger.set_category(self.conn, self.order, self.gifts)

    def test_part_category_can_change(self):
        result = self.split((-30000, self.gifts), (-11837, self.personal))
        ledger.set_part_category(self.conn, result["parts"][1]["id"], self.groceries)
        rows = list_transactions(self.conn, Filter(category_id=self.groceries))["rows"]
        self.assertEqual([r["amount_cents"] for r in rows], [-11837])

    def test_rules_never_change_a_split(self):
        self.split((-30000, self.gifts), (-11837, self.personal))
        ledger.create_rule(self.conn, "AMAZON MKTPL", self.groceries)
        self.assertEqual(len(ledger.get_transaction(self.conn, self.order)["parts"]), 2)
        rows = list_transactions(self.conn, Filter(category_id=self.gifts))["rows"]
        self.assertEqual(len(rows), 1)

    def test_unsplit_goes_back_to_one_category(self):
        self.split((-30000, self.gifts), (-11837, self.personal))
        ledger.unsplit_transaction(self.conn, self.order)
        rows = [r for r in list_transactions(self.conn)["rows"] if r["id"] == self.order]
        self.assertEqual([(r["amount_cents"], r["category_name"]) for r in rows], [(-41837, "Uncategorized")])

    def test_reimport_does_not_duplicate_a_split_transaction(self):
        self.split((-30000, self.gifts), (-11837, self.personal))
        summary = import_csv(self.conn, self.account, "a.csv", fixture("chase_credit_2.csv"))
        self.assertEqual(summary.rows_added, 0)

    def test_undo_import_removes_parts(self):
        self.split((-30000, self.gifts), (-11837, self.personal))
        batch = ledger.list_batches(self.conn)[0]["id"]
        undo_import(self.conn, batch)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM transaction_parts").fetchone()[0], 0)

    def test_recent_choices_for_the_same_store(self):
        amazon = [r[0] for r in self.conn.execute("SELECT id FROM transactions WHERE description LIKE 'AMAZON MKTPL%' ORDER BY date DESC, id DESC")]
        ledger.set_category(self.conn, amazon[1], self.gifts)
        self.split((-30000, self.gifts), (-11837, self.personal))
        ledger.set_category(self.conn, amazon[-1], self.personal)
        recent = ledger.recent_choices(self.conn, "AMAZON MKTPL*NEW", exclude_transaction_id=amazon[0])
        self.assertEqual(recent, ["Gifts", "Split", "Personal"])


class RuleTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.account = self.make_account()
        import_csv(self.conn, self.account, "a.csv", fixture("chase_credit_1.csv"))
        self.lyft_rule = next(r for r in ledger.list_rules(self.conn) if r["pattern"] == "LYFT")

    def test_list_is_alphabetical(self):
        patterns = [r["pattern"] for r in ledger.list_rules(self.conn)]
        self.assertEqual(patterns, sorted(patterns))

    def test_changing_a_rule_recategorizes(self):
        result = ledger.update_rule(self.conn, self.lyft_rule["id"], self.category_id("Travel"))
        self.assertEqual(result["updated"], 6)
        self.assertEqual(self.categories_by_description(self.account)["LYFT *RIDE TRIP A1"], "Travel")

    def test_deleting_a_rule_recategorizes(self):
        result = ledger.delete_rule(self.conn, self.lyft_rule["id"])
        self.assertEqual(result["updated"], 6)  # back to Chase's "Travel"

    def test_rule_cannot_point_at_uncategorized(self):
        with self.assertRaises(UserError):
            ledger.update_rule(self.conn, self.lyft_rule["id"], self.category_id("Uncategorized"))

    def test_unknown_rule(self):
        with self.assertRaises(NotFound):
            ledger.delete_rule(self.conn, 999)


class BankCategoryTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.account = self.make_account()
        import_csv(self.conn, self.account, "a.csv", fixture("chase_credit_1.csv"))

    def test_list_shows_mappings_and_usage(self):
        listed = {b["bank_category"]: b for b in ledger.list_bank_categories(self.conn)}
        self.assertEqual(listed["Food & Drink"]["category_id"], self.category_id("Food › Restaurants"))
        self.assertEqual(listed["Food & Drink"]["transaction_count"], 3)
        self.assertEqual(listed["Gas"]["transaction_count"], 0)
        self.assertNotIn("Shopping", listed)  # mixed stores go to review instead

    def test_unmapped_bank_categories_seen_in_transactions_are_listed(self):
        ledger.set_bank_category(self.conn, "Food & Drink", None)
        listed = {b["bank_category"]: b for b in ledger.list_bank_categories(self.conn)}
        self.assertIsNone(listed["Food & Drink"]["category_id"])
        self.assertEqual(self.categories_by_description(self.account)["LAKESIDE GRILL SPRINGFIELD"], "Uncategorized")

    def test_remap_recategorizes(self):
        result = ledger.set_bank_category(self.conn, "Food & Drink", self.category_id("Food › Groceries"))
        self.assertEqual(result["updated"], 3)
        self.assertEqual(self.categories_by_description(self.account)["LAKESIDE GRILL SPRINGFIELD"], "Food › Groceries")


class AccountManagementTest(DbTestCase):
    def test_rename(self):
        account = self.make_account("Card")
        ledger.rename_account(self.conn, account, "Chase Sapphire")
        self.assertEqual(ledger.list_accounts(self.conn)[0]["name"], "Chase Sapphire")
        self.make_account("Other")
        with self.assertRaisesRegex(UserError, "already have"):
            ledger.rename_account(self.conn, account, "other")

    def test_delete_removes_everything_for_that_account_only(self):
        keep = self.make_account("Keep")
        drop = self.make_account("Drop")
        import_csv(self.conn, keep, "a.csv", fixture("chase_credit_1.csv"))
        import_csv(self.conn, drop, "a.csv", fixture("chase_credit_2.csv"))
        self.assertEqual(ledger.delete_account(self.conn, drop), {"removed": 12})
        self.assertEqual([a["name"] for a in ledger.list_accounts(self.conn)], ["Keep"])
        self.assertEqual(len(ledger.list_batches(self.conn)), 1)

    def test_delete_unknown(self):
        with self.assertRaises(NotFound):
            ledger.delete_account(self.conn, 999)


if __name__ == "__main__":
    unittest.main()
