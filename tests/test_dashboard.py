import unittest
from datetime import date

from budgetapp import budgets
from budgetapp.queries import category_rank, monthly_totals
from tests.test_queries import QueryTestCase

# The QueryTestCase data in October 2026: Groceries $20 minus a $5 refund,
# Food › Restaurants $5, payroll $1,000 and a card payment (a transfer).
OCT_10 = date(2026, 10, 10)  # 10 of 31 days gone, 21 left


class BudgetStatusTest(QueryTestCase):
    # Short keyword names for the categories these tests budget.
    NAMES = {"Groceries": "Food › Groceries", "Restaurants": "Food › Restaurants", "Food": "Food", "Entertainment": "Entertainment"}

    def set_budgets(self, overall=None, warn=80, **limits):
        budgets.save_budgets(
            self.conn,
            [{"category_id": self.category_id(self.NAMES[name]), "monthly_limit_cents": cents} for name, cents in limits.items()],
            overall,
            warn,
        )

    def by_name(self, status):
        return {c["name"]: c for c in status["categories"]}

    def test_no_budgets(self):
        status = budgets.budget_status(self.conn, OCT_10)
        self.assertEqual(status["categories"], [])
        self.assertIsNone(status["overall"])
        self.assertEqual(status["alerts"], [])
        self.assertEqual(status["spent_cents"], 2000)
        self.assertEqual((status["label"], status["days_left"]), ("October 2026", 21))

    def test_states_and_messages(self):
        self.set_budgets(overall=2500, Groceries=2000, Restaurants=400, Entertainment=10000)
        status = budgets.budget_status(self.conn, OCT_10)
        found = self.by_name(status)
        self.assertEqual(found["Food › Restaurants"]["state"], "over")
        self.assertEqual(found["Food › Groceries"]["state"], "pace")  # 75% spent, 32% of month gone
        self.assertEqual(found["Entertainment"]["state"], "ok")
        self.assertEqual(status["overall"]["state"], "warn")  # $20 of $25
        self.assertEqual(
            status["alerts"],
            [
                "Food › Restaurants is over budget by $1.00 ($5.00 of $4.00).",
                "Total spending is at 80% of its budget ($20.00 of $25.00) with 21 days left.",
                "Food › Groceries is ahead of pace: 75% of its budget spent with 32% of the month gone.",
            ],
        )

    def test_group_budget_includes_its_subcategories(self):
        self.set_budgets(Food=2500, Groceries=1000)
        found = self.by_name(budgets.budget_status(self.conn, OCT_10))
        self.assertEqual(found["Food"]["spent_cents"], 2000)  # Groceries $15 + Restaurants $5
        self.assertEqual(found["Food"]["state"], "warn")  # 80%
        self.assertEqual(found["Food › Groceries"]["state"], "over")  # tracked on its own

    def test_pace_warning_fades_as_the_month_goes_on(self):
        self.set_budgets(Groceries=2000)
        late = budgets.budget_status(self.conn, date(2026, 10, 25))
        self.assertEqual(self.by_name(late)["Food › Groceries"]["state"], "ok")

    def test_warning_level_is_adjustable(self):
        self.set_budgets(warn=70, Groceries=2000)
        late = budgets.budget_status(self.conn, date(2026, 10, 25))
        self.assertEqual(self.by_name(late)["Food › Groceries"]["state"], "warn")

    def test_exactly_at_the_limit_is_a_warning_not_over(self):
        self.set_budgets(Restaurants=500)
        self.assertEqual(self.by_name(budgets.budget_status(self.conn, OCT_10))["Food › Restaurants"]["state"], "warn")

    def test_last_day_wording(self):
        self.set_budgets(Restaurants=600)
        status = budgets.budget_status(self.conn, date(2026, 10, 31))
        self.assertEqual(
            status["alerts"], ["Food › Restaurants is at 83% of its budget ($5.00 of $6.00) on the last day of the month."]
        )

    def test_other_months_are_not_counted(self):
        self.set_budgets(Entertainment=5000)  # the $100 purchase was in Dec 2025
        self.assertEqual(self.by_name(budgets.budget_status(self.conn, OCT_10))["Entertainment"]["spent_cents"], 0)


class MonthlyTotalsTest(QueryTestCase):
    def test_every_month_is_present(self):
        months = monthly_totals(self.conn, "2026-08-01", "2026-10-31")
        self.assertEqual(
            [(m["label"], m["spent_cents"], m["income_cents"]) for m in months],
            [("Aug 2026", 0, 0), ("Sep 2026", 1000, 0), ("Oct 2026", 2000, 100000)],
        )
        october = months[-1]
        self.assertEqual((october["start"], october["end"]), ("2026-10-01", "2026-10-31"))
        self.assertEqual(
            october["by_category"],
            {str(self.category_id("Food")): 2000},  # per group: Groceries $15 + Restaurants $5
        )

    def test_category_rank_is_all_time_spending(self):
        names = {self.category_id(n): n for n in ("Entertainment", "Food")}
        self.assertEqual([names[i] for i in category_rank(self.conn)], ["Entertainment", "Food"])


if __name__ == "__main__":
    unittest.main()
