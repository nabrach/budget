import unittest

from budgetapp import ledger
from budgetapp.errors import UserError
from budgetapp.importer import import_csv
from budgetapp.queries import Filter, group_totals, list_transactions
from tests.helpers import CHASE_CREDIT_HEADER, DbTestCase

CARD = CHASE_CREDIT_HEADER + "".join(
    [
        "12/31/2025,12/31/2025,OLD THING,Entertainment,Sale,-100.00,\n",
        "09/28/2026,09/28/2026,GROCERY A,Groceries,Sale,-10.00,\n",  # a Monday
        "10/04/2026,10/04/2026,GROCERY A,Groceries,Sale,-20.00,\n",  # the Sunday after
        "10/05/2026,10/05/2026,COFFEE 50%OFF_DEAL,Food & Drink,Sale,-5.00,\n",
        "10/06/2026,10/06/2026,GROCERY A,Groceries,Return,5.00,\n",
        "10/07/2026,10/07/2026,Payment Thank You-Mobile,,Payment,500.00,\n",
    ]
)
CHECKING = (
    "Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\n"
    "CREDIT,10/01/2026,ACME PAYROLL PPD ID: 123,1000.00,ACH_CREDIT,1000.00,\n"
)


class QueryTestCase(DbTestCase):
    def setUp(self):
        super().setUp()
        self.card = self.make_account("Card")
        self.checking = self.make_account("Checking")
        import_csv(self.conn, self.card, "card.csv", CARD)
        import_csv(self.conn, self.checking, "checking.csv", CHECKING)


class ListTransactionsTest(QueryTestCase):
    def descriptions(self, result):
        return [row["description"] for row in result["rows"]]

    def test_totals_leave_out_transfers_and_subtract_refunds(self):
        result = list_transactions(self.conn)
        self.assertEqual(result["count"], 7)
        self.assertEqual(result["spent_cents"], 13000)  # 100 + 10 + 20 + 5 - 5
        self.assertEqual(result["income_cents"], 100000)

    def test_date_range_is_inclusive(self):
        result = list_transactions(self.conn, Filter(start="2026-10-01", end="2026-10-31"))
        self.assertEqual(result["count"], 5)
        self.assertEqual(result["spent_cents"], 2000)

    def test_filter_by_category_and_account(self):
        groceries = self.category_id("Food › Groceries")
        self.assertEqual(list_transactions(self.conn, Filter(category_id=groceries))["count"], 3)
        self.assertEqual(list_transactions(self.conn, Filter(account_id=self.checking))["count"], 1)

    def test_a_group_includes_its_subcategories(self):
        food = self.category_id("Food")
        self.assertEqual(list_transactions(self.conn, Filter(category_id=food))["count"], 4)
        self.assertEqual(list_transactions(self.conn, Filter(category_id=food, category_exact=True))["count"], 0)

    def test_text_search_ignores_case_and_treats_wildcards_literally(self):
        self.assertEqual(self.descriptions(list_transactions(self.conn, Filter(text="grocery"))), ["GROCERY A"] * 3)
        self.assertEqual(self.descriptions(list_transactions(self.conn, Filter(text="50%"))), ["COFFEE 50%OFF_DEAL"])
        self.assertEqual(list_transactions(self.conn, Filter(text="%"))["count"], 1)
        self.assertEqual(list_transactions(self.conn, Filter(text="_"))["count"], 1)

    def test_sort_by_amount(self):
        rows = list_transactions(self.conn, sort="amount", direction="desc")["rows"]
        self.assertEqual([r["amount_cents"] for r in rows], [100000, 50000, 500, -500, -1000, -2000, -10000])

    def test_sort_by_category_name(self):
        rows = list_transactions(self.conn, sort="category", direction="asc")["rows"]
        self.assertEqual(
            [r["category_name"] for r in rows],
            ["Entertainment", "Food › Groceries", "Food › Groceries", "Food › Groceries", "Food › Restaurants", "Income", "Transfer"],
        )

    def test_default_order_is_newest_first(self):
        rows = list_transactions(self.conn)["rows"]
        self.assertEqual(rows[0]["date"], "2026-10-07")
        self.assertEqual(rows[-1]["date"], "2025-12-31")

    def test_limit_keeps_full_count(self):
        result = list_transactions(self.conn, limit=2)
        self.assertEqual((len(result["rows"]), result["count"]), (2, 7))

    def test_bad_input(self):
        with self.assertRaises(UserError):
            list_transactions(self.conn, sort="nope")
        with self.assertRaises(UserError):
            list_transactions(self.conn, direction="sideways")
        with self.assertRaisesRegex(UserError, "date like"):
            Filter(start="10/01/2026")
        with self.assertRaisesRegex(UserError, "after the end"):
            Filter(start="2026-10-31", end="2026-10-01")


class GroupTotalsTest(QueryTestCase):
    def summarize(self, groups):
        return [(g["label"], g["count"], g["spent_cents"], g["income_cents"]) for g in groups]

    def test_by_week_monday_to_sunday(self):
        groups = group_totals(self.conn, Filter(), "week")
        self.assertEqual(
            self.summarize(groups),
            [
                ("Week of Oct 5, 2026", 2, 0, 0),
                ("Week of Sep 28, 2026", 3, 3000, 100000),
                ("Week of Dec 29, 2025", 1, 10000, 0),
            ],
        )
        self.assertEqual(groups[1]["filter"], {"start": "2026-09-28", "end": "2026-10-04"})

    def test_by_month(self):
        groups = group_totals(self.conn, Filter(), "month")
        self.assertEqual(
            self.summarize(groups),
            [("Oct 2026", 4, 2000, 100000), ("Sep 2026", 1, 1000, 0), ("Dec 2025", 1, 10000, 0)],
        )
        self.assertEqual(groups[0]["filter"], {"start": "2026-10-01", "end": "2026-10-31"})
        self.assertEqual(groups[1]["filter"], {"start": "2026-09-01", "end": "2026-09-30"})

    def test_by_year(self):
        groups = group_totals(self.conn, Filter(), "year")
        self.assertEqual(self.summarize(groups), [("2026", 5, 3000, 100000), ("2025", 1, 10000, 0)])
        self.assertEqual(groups[0]["filter"], {"start": "2026-01-01", "end": "2026-12-31"})

    def test_by_category_group_biggest_spending_first(self):
        groups = group_totals(self.conn, Filter(), "category")
        self.assertEqual(
            self.summarize(groups),
            [("Entertainment", 1, 10000, 0), ("Food", 4, 3000, 0), ("Income", 1, 0, 100000)],
        )
        self.assertEqual(groups[1]["filter"], {"category_id": self.category_id("Food")})

    def test_by_subcategory(self):
        groups = group_totals(self.conn, Filter(), "subcategory")
        self.assertEqual(
            self.summarize(groups),
            [
                ("Entertainment", 1, 10000, 0),
                ("Food › Groceries", 3, 2500, 0),
                ("Food › Restaurants", 1, 500, 0),
                ("Income", 1, 0, 100000),
            ],
        )
        self.assertEqual(groups[1]["filter"], {"category_id": self.category_id("Food › Groceries")})

    def test_spending_put_directly_in_a_group_with_subcategories(self):
        coffee = self.conn.execute("SELECT id FROM transactions WHERE description LIKE 'COFFEE%'").fetchone()[0]
        ledger.set_category(self.conn, coffee, self.category_id("Food"))
        groups = {g["label"]: g for g in group_totals(self.conn, Filter(), "subcategory")}
        other = groups["Food (other)"]
        self.assertEqual(other["filter"], {"category_id": self.category_id("Food"), "category_exact": True})
        self.assertEqual(list_transactions(self.conn, Filter(**other["filter"]))["count"], 1)

    def test_by_merchant(self):
        groups = group_totals(self.conn, Filter(), "merchant")
        self.assertEqual(
            [(g["label"], g["spent_cents"]) for g in groups],
            [("OLD THING", 10000), ("GROCERY A", 2500), ("COFFEE", 500), ("ACME PAYROLL", 0)],
        )
        self.assertEqual(groups[1]["filter"], {"text": "GROCERY A"})

    def test_respects_filters(self):
        groups = group_totals(self.conn, Filter(account_id=self.checking), "category")
        self.assertEqual(self.summarize(groups), [("Income", 1, 0, 100000)])

    def test_group_filter_finds_the_same_transactions(self):
        for group in group_totals(self.conn, Filter(), "merchant"):
            with self.subTest(group["label"]):
                result = list_transactions(self.conn, Filter(**group["filter"]))
                self.assertEqual(result["count"], group["count"])

    def test_unknown_group(self):
        with self.assertRaises(UserError):
            group_totals(self.conn, Filter(), "decade")


if __name__ == "__main__":
    unittest.main()
