import unittest

from budgetapp import ledger
from budgetapp.categorize import suggest_pattern
from budgetapp.errors import UserError
from budgetapp.importer import import_csv
from tests.helpers import CHASE_CREDIT_HEADER, DbTestCase, fixture


class DefaultCategoriesTest(DbTestCase):
    def test_credit_card(self):
        account = self.make_account()
        import_csv(self.conn, account, "a.csv", fixture("chase_credit_1.csv"))
        import_csv(self.conn, account, "b.csv", fixture("chase_credit_2.csv"))
        found = self.categories_by_description(account)
        # The LYFT rule beats the bank's "Travel" category.
        self.assertEqual(found["LYFT *RIDE TRIP A1"], "Travel › Rideshare")
        # Bank category "Food & Drink" -> Food › Restaurants.
        self.assertEqual(found["OFFICECAFETERIA J23"], "Food › Restaurants")
        # Mixed stores (Chase "Shopping") wait for review: one category or a split.
        self.assertEqual(found["AMAZON MKTPL*AB0000001"], "Uncategorized")
        self.assertEqual(found["Payment Thank You-Mobile"], "Transfer")

    def test_checking(self):
        account = self.make_account("Chase Checking")
        summary = import_csv(self.conn, account, "c.csv", fixture("chase_checking.csv"))
        found = self.categories_by_description(account)
        self.assertEqual(found["ACME CORP PAYROLL PPD ID: 0000000002"], "Income")
        self.assertEqual(found["Payment to Chase card ending in 0001 10/13"], "Transfer")
        self.assertEqual(found["CITI CARD ONLINE PAYMENT 000000000000001 WEB ID: CITICTP"], "Transfer")
        self.assertEqual(found["AMERICAN EXPRESS ACH PMT M0001 WEB ID: 0000000006"], "Transfer")
        self.assertEqual(found["VENMO PAYMENT 1000000000001 WEB ID: 0000000003"], "Uncategorized")

        counts = dict(
            self.conn.execute(
                """SELECT c.name, count(*) FROM transactions t
                   JOIN categories c ON c.id = t.category_id GROUP BY c.name"""
            ).fetchall()
        )
        self.assertEqual(counts, {"Transfer": 15, "Income": 2, "Uncategorized": 10})
        self.assertEqual(summary.uncategorized, 10)

    def test_citi(self):
        account = self.make_account("Citi Double Cash")
        summary = import_csv(self.conn, account, "citi.CSV", fixture("citi_credit.csv"))
        found = self.categories_by_description(account)
        self.assertEqual(found["ONLINE PAYMENT, THANK YOU"], "Transfer")
        # Citi files have no category column, so everything else waits for your rules.
        self.assertEqual(found["NAME-CHEAP.COM* ABCDEF SPRINGFIELD ST null XXXXXXXXXXXX0000"], "Subscriptions › Namecheap")
        self.assertEqual(summary.uncategorized, 14)

    def test_amex(self):
        account = self.make_account("Amex Platinum", amount_sign=-1)
        summary = import_csv(self.conn, account, "amex.csv", fixture("amex_credit.csv"))
        found = self.categories_by_description(account)
        self.assertEqual(found["MOBILE PAYMENT - THANK YOU"], "Transfer")
        self.assertEqual(found["ADJ REDIST PURCHASE BAL"], "Transfer")
        self.assertEqual(found["DR ADJ REDIST CADV PRIN"], "Transfer")
        self.assertEqual(found["UBER EATS help.uber.com CA"], "Food › Restaurants")
        self.assertEqual(summary.uncategorized, 5)

    def test_transfer_type_without_a_matching_rule(self):
        # An Amex-style card payment wording no rule knows, typed "Payment" by Chase.
        account = self.make_account()
        text = CHASE_CREDIT_HEADER + "10/01/2026,10/01/2026,AUTOMATIC PYMT,,Payment,100.00,\n"
        import_csv(self.conn, account, "a.csv", text)
        self.assertEqual(self.categories_by_description(account)["AUTOMATIC PYMT"], "Transfer")

    def test_refund_stays_in_its_category_and_offsets_spending(self):
        account = self.make_account()
        text = (
            CHASE_CREDIT_HEADER
            + "10/01/2026,10/02/2026,CORNER GROCER 1234,Groceries,Sale,-50.00,\n"
            + "10/05/2026,10/06/2026,CORNER GROCER 1234,Groceries,Return,50.00,\n"
        )
        import_csv(self.conn, account, "a.csv", text)
        total = self.conn.execute(
            "SELECT sum(amount_cents) FROM transactions WHERE category_id = ?",
            (self.category_id("Food › Groceries"),),
        ).fetchone()[0]
        self.assertEqual(total, 0)

    def test_rules_match_whole_words_only(self):
        account = self.make_account()
        text = CHASE_CREDIT_HEADER + "10/01/2026,10/01/2026,TUBERS FARM STAND,,Sale,-5,\n"
        import_csv(self.conn, account, "a.csv", text)
        self.assertEqual(self.categories_by_description(account)["TUBERS FARM STAND"], "Uncategorized")

    def test_longest_rule_wins(self):
        account = self.make_account()
        text = (
            CHASE_CREDIT_HEADER
            + "10/01/2026,10/01/2026,UBER EATS 123,,Sale,-20,\n"
            + "10/01/2026,10/01/2026,UBER TRIP 456,,Sale,-15,\n"
        )
        import_csv(self.conn, account, "a.csv", text)
        found = self.categories_by_description(account)
        self.assertEqual(found["UBER EATS 123"], "Food › Restaurants")
        self.assertEqual(found["UBER TRIP 456"], "Travel › Rideshare")


class ManualCategoryAndRulesTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.account = self.make_account("Chase Checking")
        import_csv(self.conn, self.account, "c.csv", fixture("chase_checking.csv"))
        self.venmo_ids = [
            row["id"]
            for row in self.conn.execute(
                "SELECT id FROM transactions WHERE description LIKE 'VENMO PAYMENT%' ORDER BY id"
            )
        ]

    def category_of(self, transaction_id):
        return self.conn.execute(
            """SELECT c.name FROM transactions t JOIN categories c ON c.id = t.category_id
               WHERE t.id = ?""",
            (transaction_id,),
        ).fetchone()["name"]

    def test_set_category_suggests_a_rule(self):
        result = ledger.set_category(self.conn, self.venmo_ids[0], self.category_id("Gifts"))
        self.assertEqual(result["suggested_pattern"], "VENMO PAYMENT")
        self.assertEqual(self.category_of(self.venmo_ids[0]), "Gifts")

    def test_no_rule_suggested_when_moving_to_uncategorized(self):
        result = ledger.set_category(self.conn, self.venmo_ids[0], self.category_id("Uncategorized"))
        self.assertIsNone(result["suggested_pattern"])

    def test_new_rule_applies_to_existing_transactions(self):
        result = ledger.create_rule(self.conn, "venmo  payment", self.category_id("Entertainment"))
        self.assertEqual(result["pattern"], "VENMO PAYMENT")
        self.assertEqual(result["updated"], 3)
        for transaction_id in self.venmo_ids:
            self.assertEqual(self.category_of(transaction_id), "Entertainment")

    def test_new_rule_does_not_override_manual_choices(self):
        ledger.set_category(self.conn, self.venmo_ids[0], self.category_id("Gifts"))
        result = ledger.create_rule(self.conn, "VENMO PAYMENT", self.category_id("Entertainment"))
        self.assertEqual(result["updated"], 2)
        self.assertEqual(self.category_of(self.venmo_ids[0]), "Gifts")

    def test_new_rule_applies_to_future_imports(self):
        ledger.create_rule(self.conn, "VENMO PAYMENT", self.category_id("Entertainment"))
        other = self.make_account("Other Checking")
        import_csv(self.conn, other, "c.csv", fixture("chase_checking.csv"))
        found = self.categories_by_description(other)
        self.assertEqual(found["VENMO PAYMENT 1000000000001 WEB ID: 0000000003"], "Entertainment")

    def test_rule_validation(self):
        with self.assertRaisesRegex(UserError, "already a rule"):
            ledger.create_rule(self.conn, "payroll", self.category_id("Income"))
        with self.assertRaises(UserError):
            ledger.create_rule(self.conn, " x ", self.category_id("Income"))
        with self.assertRaises(UserError):
            ledger.create_rule(self.conn, "VENMO", self.category_id("Uncategorized"))


class SuggestPatternTest(unittest.TestCase):
    def test_examples(self):
        cases = {
            "AMAZON MKTPL*AB0000001": "AMAZON MKTPL",
            "Amazon.com*AB0000003": "AMAZON.COM",
            "LYFT   *RIDE TRIP A1": "LYFT",
            "OFFICECAFETERIA J23": "OFFICECAFETERIA",
            "LAKESIDE GRILL SPRINGFIELD": "LAKESIDE GRILL SPRINGFIELD",
            "VENMO PAYMENT 1000000000001 WEB ID: 0000000003": "VENMO PAYMENT",
            "ACME CORP PAYROLL PPD ID: 0000000002": "ACME CORP PAYROLL",
            "Zelle payment from Acme Insurance BAC0000000": "ZELLE PAYMENT FROM ACME INSURANCE",
            "7-ELEVEN 34512": "7-ELEVEN",
            # Citi descriptions
            "THRIFT SHOP # 182 SPRINGFIELD ST": "THRIFT SHOP",
            "WAREHOUSE CLUB #4829 SPRINGFIELD ST": "WAREHOUSE CLUB",
            "RESALE - 1121 SPRINGFIELD ST": "RESALE",
            "NAME-CHEAP.COM* ABCDEF SPRINGFIELD ST null XXXXXXXXXXXX0000": "NAME-CHEAP.COM",
            "COMMUNITY CHURCH SPRINGFIELD ST null XXXXXXXXXXXX0000": "COMMUNITY CHURCH SPRINGFIELD ST",
        }
        for description, expected in cases.items():
            with self.subTest(description):
                self.assertEqual(suggest_pattern(description), expected)


if __name__ == "__main__":
    unittest.main()
