import unittest
from datetime import date

from budgetapp import budgets, ledger
from budgetapp.ask import answer, question_list
from budgetapp.errors import UserError
from tests.test_queries import QueryTestCase

# QueryTestCase data: Dec 31 2025 OLD THING $100 (Entertainment); Sep 28 GROCERY A $10;
# Oct 4 GROCERY A $20; Oct 5 COFFEE $5 (Food › Restaurants); Oct 6 GROCERY A refund $5;
# Oct 7 card payment (transfer); Oct 1 payroll $1,000.
OCT_10 = date(2026, 10, 10)


class AskTest(QueryTestCase):
    def ask(self, question_type, today=OCT_10, **params):
        return answer(self.conn, question_type, params, today)

    def test_spend_on_everything(self):
        result = self.ask("spend_on", category="all", period="this-month")
        self.assertEqual(result["question"], "How much did everyone spend on everything this month?")
        # Transfers and income don't count as transactions here.
        self.assertEqual(result["text"], "You spent $20.00 this month, across 3 transactions.")
        self.assertEqual(result["link"], {"start": "2026-10-01", "end": "2026-10-31"})

    def test_spend_on_a_category(self):
        groceries = self.category_id("Food › Groceries")
        result = self.ask("spend_on", category=str(groceries), period="this-month")
        self.assertEqual(result["text"], "You spent $15.00 on Food › Groceries this month, across 2 transactions.")
        self.assertEqual(result["link"]["category_id"], groceries)

    def test_spend_on_nothing(self):
        result = self.ask("spend_on", category=str(self.category_id("Transportation › Gas")), period="this-month")
        self.assertEqual(result["text"], "You didn't spend anything on Transportation › Gas this month.")

    def test_last_month_reads_naturally(self):
        result = self.ask("spend_on", category=str(self.category_id("Food › Groceries")), period="last-month")
        self.assertEqual(result["question"], "How much did everyone spend on Food › Groceries last month (September)?")
        self.assertEqual(result["text"], "You spent $10.00 on Food › Groceries last month (September), across 1 transaction.")

    def test_spend_on_for_a_person_or_the_household(self):
        alex = ledger.create_person(self.conn, "Alex")["id"]
        coffee = self.conn.execute("SELECT id FROM transactions WHERE description LIKE 'COFFEE%'").fetchone()[0]
        ledger.set_person(self.conn, coffee, alex)
        result = self.ask("spend_on", person=str(alex), category="all", period="this-month")
        self.assertEqual(result["question"], "How much did Alex spend on everything this month?")
        self.assertEqual(result["text"], "Alex spent $5.00 this month, across 1 transaction.")
        self.assertEqual(result["link"]["person"], alex)
        shared = self.ask("spend_on", person="household", category="all", period="this-month")
        self.assertEqual(shared["text"], "The household spent $15.00 this month, across 2 transactions.")

    def test_spend_at_merchant(self):
        result = self.ask("spend_at", merchant="grocery", period="all")
        self.assertEqual(result["question"], "How much did I spend at “grocery” overall?")
        self.assertEqual(result["text"], "You spent $25.00 at “grocery” overall, across 3 transactions.")
        self.assertEqual(result["link"]["text"], "grocery")

    def test_where_money_went(self):
        result = self.ask("where_money_went", period="all")
        self.assertEqual(
            result["text"],
            "You spent $130.00 overall. The biggest category was Entertainment at $100.00 (77% of your spending).",
        )
        self.assertEqual([b["label"] for b in result["bars"]], ["Entertainment", "Food"])
        self.assertEqual(result["table"]["rows"][1], ["Food", "$30.00", "23%", 4])

    def test_top_merchants(self):
        result = self.ask("top_merchants", count="3", period="all")
        self.assertEqual(result["question"], "What were my top 3 merchants overall?")
        self.assertEqual(result["text"], "Your top merchant overall was OLD THING, at $100.00.")
        self.assertEqual([r[0] for r in result["table"]["rows"]], ["OLD THING", "GROCERY A", "COFFEE"])

    def test_biggest_purchases(self):
        result = self.ask("biggest", count="3", period="all")
        self.assertEqual(result["text"], "Your biggest purchase overall was $100.00 at OLD THING on December 31.")
        self.assertEqual([r[-1] for r in result["table"]["rows"]], ["$100.00", "$20.00", "$10.00"])
        self.assertEqual(result["table"]["rows"][0][0], "December 31, 2025")

    def test_compare(self):
        groceries = str(self.category_id("Food › Groceries"))
        result = self.ask("compare", category=groceries, period_a="month:2026-09", period_b="month:2026-10")
        self.assertEqual(result["question"], "Compare spending on Food › Groceries: September 2026 vs October 2026")
        self.assertEqual(
            result["text"],
            "You spent $10.00 on Food › Groceries in September 2026 and $15.00 in October 2026: $5.00 more in October 2026 (+50%).",
        )

    def test_compare_needs_two_different_periods(self):
        with self.assertRaisesRegex(UserError, "two different"):
            self.ask("compare", category="all", period_a="this-month", period_b="month:2026-10")

    def test_average_uses_full_months_only(self):
        groceries = str(self.category_id("Food › Groceries"))
        result = self.ask("average_monthly", today=date(2026, 11, 5), category=groceries, months="3")
        self.assertEqual(
            result["text"],
            "Over the last 3 full months (Aug 2026 to Oct 2026), you spent an average of $8.33 a month on Food › Groceries.",
        )
        self.assertEqual([r[1] for r in result["table"]["rows"]], ["$0.00", "$10.00", "$15.00"])

    def test_saved(self):
        result = self.ask("saved", period="this-month")
        self.assertEqual(
            result["text"], "Yes. This month money in was $1,000.00 and you spent $20.00, so you saved $980.00."
        )
        overspent = self.ask("saved", period="month:2025-12")
        self.assertTrue(overspent["text"].startswith("No. In December 2025 money in was $0.00"))

    def test_over_budget(self):
        self.assertIn("haven't set any budgets", self.ask("over_budget")["text"])
        budgets.save_budgets(self.conn, [{"category_id": self.category_id("Food › Restaurants"), "monthly_limit_cents": 400}], None, 80)
        result = self.ask("over_budget")
        self.assertEqual(result["text"], "Food › Restaurants is over budget by $1.00 ($5.00 of $4.00).")
        self.assertEqual(result["table"]["rows"], [["Food › Restaurants", "$5.00", "$4.00", "Over budget"]])

    def test_bad_input(self):
        cases = [
            ("nope", {}),
            ("spend_on", {"category": "999", "period": "this-month"}),
            ("spend_on", {"category": str(self.category_id("Income")), "period": "this-month"}),
            ("spend_on", {"category": "all", "period": "someday"}),
            ("spend_on", {"category": "all", "period": "month:2026-13"}),
            ("spend_at", {"merchant": "a", "period": "all"}),
            ("top_merchants", {"count": "7", "period": "all"}),
        ]
        for question_type, params in cases:
            with self.subTest(question_type, **params):
                with self.assertRaises(UserError):
                    self.ask(question_type, **params)

    def test_question_list(self):
        listed = question_list(self.conn, OCT_10)
        self.assertEqual(len(listed["questions"]), 9)
        periods = [o["value"] for o in listed["options"]["period"]]
        self.assertIn("this-month", periods)
        self.assertEqual([p for p in periods if p.startswith("month:")], ["month:2026-10", "month:2026-09", "month:2025-12"])
        categories = [o["label"] for o in listed["options"]["category"]]
        self.assertEqual(categories[0], "everything")
        self.assertIn("Uncategorized", categories)
        self.assertNotIn("Income", categories)
        self.assertNotIn("Transfer", categories)

    def test_every_question_answers_with_defaults(self):
        for question in question_list(self.conn, OCT_10)["questions"]:
            if question["type"] == "spend_at":
                continue  # needs a merchant
            with self.subTest(question["type"]):
                result = answer(self.conn, question["type"], {}, OCT_10)
                self.assertTrue(result["text"])
                self.assertTrue(result["question"])


if __name__ == "__main__":
    unittest.main()
