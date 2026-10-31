from budgetapp import amazon, ledger
from budgetapp.errors import NotFound, UserError
from budgetapp.importer import import_csv
from budgetapp.queries import Filter, list_transactions
from tests.helpers import DbTestCase

# Made-up orders in the shape of Amazon's Order History.csv (trimmed columns).
ORDER_HEADER = (
    "ASIN,Department,Order Date,Order ID,Order Status,Original Quantity,Product Name,"
    "Ship Date,Total Amount,Total Discounts\n"
)


def order_row(asin, order_id, product, total, ordered="2026-10-01", shipped="2026-10-02", department="Not Available",
              status="Closed", discount="0"):
    return (
        f'{asin},{department},{ordered}T10:00:00Z,{order_id},{status},1,"{product}",'
        f"{shipped + 'T12:00:00Z' if shipped else 'Not Available'},{total},{discount}\n"
    )


ORDERS = ORDER_HEADER + "".join([
    # One item, one shipment.
    order_row("B000000001", "900-0000001-0000001", "Mint Toothpaste 2 Pack", "6.49", shipped="2026-10-02", department="Health and Beauty"),
    # Three items in one shipment: one charge of $40.47, split by item.
    order_row("B000000002", "900-0000002-0000002", "Building Blocks Set", "24.99", ordered="2026-10-03", shipped="2026-10-04", department="Toy"),
    order_row("B000000003", "900-0000002-0000002", "Birthday Card", "4.99", ordered="2026-10-03", shipped="2026-10-04"),
    order_row("B000000001", "900-0000002-0000002", "Mint Toothpaste 2 Pack", "10.49", ordered="2026-10-03", shipped="2026-10-04",
              department="Health and Beauty", discount="'-1.12'"),
    # Two shipments charged once as a whole order ($30.00 on 10/09).
    order_row("B000000004", "900-0000003-0000003", "Desk Lamp", "18.00", ordered="2026-10-05", shipped="2026-10-06"),
    order_row("B000000005", "900-0000003-0000003", "Light Bulbs", "12.00", ordered="2026-10-05", shipped="2026-10-09"),
    # Cancelled: ignored.
    order_row("B000000006", "900-0000004-0000004", "Cancelled Thing", "99.00", shipped=None, status="Cancelled"),
])

CARD = "Date,Description,Amount\n" + "".join([
    "10/02/2026,AMAZON MKTPL*AA11BB22,-6.49\n",
    "10/05/2026,AMZN Mktp US*CC33DD44,-40.47\n",
    "10/09/2026,AMAZON.COM*EE55FF66,-30.00\n",
    "10/02/2026,CORNER STORE,-6.49\n",  # same amount, not Amazon
    "10/12/2026,AMAZON PRIME*MEMBER,-14.99\n",  # no order in the file
])


class AmazonTestCase(DbTestCase):
    def setUp(self):
        super().setUp()
        self.card = self.make_account("Prime Card")
        import_csv(self.conn, self.card, "card.csv", CARD)

    def txn(self, description):
        return self.conn.execute("SELECT id FROM transactions WHERE description = ?", (description,)).fetchone()[0]

    def matched(self):
        rows = self.conn.execute(
            "SELECT t.description, m.order_id, m.ship_date FROM amazon_matches m JOIN transactions t ON t.id = m.transaction_id"
        )
        return {r[0]: (r[1], r[2]) for r in rows}


class ImportAndMatchTest(AmazonTestCase):
    def test_reading_the_file(self):
        items = amazon.read_order_history(ORDERS)
        self.assertEqual(len(items), 6)  # the cancelled one is left out
        toothpaste = items[3]
        self.assertEqual((toothpaste["total_cents"], toothpaste["ship_date"], toothpaste["department"]), (1049, "2026-10-04", "Health and Beauty"))
        self.assertIsNone(items[2]["department"])  # "Not Available"
        self.assertEqual(items[1]["department"], "Toy")
        with self.assertRaisesRegex(UserError, "Order History.csv"):
            amazon.read_order_history("Date,Description,Amount\n")

    def test_matches_shipments_and_whole_orders(self):
        summary = amazon.import_orders(self.conn, ORDERS)
        self.assertEqual(
            {k: summary[k] for k in ("orders", "items", "matched", "unmatched_charges", "to_review")},
            {"orders": 3, "items": 6, "matched": 3, "unmatched_charges": 1, "to_review": 3},
        )
        self.assertEqual(self.matched(), {
            "AMAZON MKTPL*AA11BB22": ("900-0000001-0000001", "2026-10-02"),
            "AMZN Mktp US*CC33DD44": ("900-0000002-0000002", "2026-10-04"),
            "AMAZON.COM*EE55FF66": ("900-0000003-0000003", None),
        })

    def test_importing_again_changes_nothing(self):
        amazon.import_orders(self.conn, ORDERS)
        summary = amazon.import_orders(self.conn, ORDERS)
        self.assertEqual((summary["new_orders"], summary["matched"]), (0, 0))
        self.assertEqual(len(self.matched()), 3)

    def test_closest_date_wins_and_true_ties_wait(self):
        orders = ORDER_HEADER + "".join([
            order_row("B1", "901-1", "Socks", "10.82", shipped="2026-10-20"),
            order_row("B2", "901-2", "Tape", "10.82", shipped="2026-10-21"),
            order_row("B3", "901-3", "Batteries", "21.65", shipped="2026-10-19"),
            order_row("B4", "901-4", "Pens", "21.65", shipped="2026-10-19"),
        ])
        import_csv(self.conn, self.card, "more.csv", "Date,Description,Amount\n" + "".join([
            "10/19/2026,AMAZON MKTPL*X1,-10.82\n",
            "10/21/2026,AMAZON MKTPL*X2,-10.82\n",
            "10/19/2026,AMAZON MKTPL*X3,-21.65\n",
        ]))
        amazon.import_orders(self.conn, orders)
        found = self.matched()
        self.assertEqual((found["AMAZON MKTPL*X1"][0], found["AMAZON MKTPL*X2"][0]), ("901-1", "901-2"))
        self.assertNotIn("AMAZON MKTPL*X3", found)
        [tie] = amazon.review(self.conn)["ties"]
        self.assertEqual(sorted(o["order_id"] for o in tie["options"]), ["901-3", "901-4"])
        amazon.choose(self.conn, self.txn("AMAZON MKTPL*X3"), "901-4", "2026-10-19")
        self.assertEqual(self.matched()["AMAZON MKTPL*X3"], ("901-4", "2026-10-19"))
        self.assertEqual(amazon.review(self.conn)["ties"], [])

    def test_a_whole_order_charge_rules_out_its_shipments(self):
        # Order 902-1's first shipment is $21.65, but the order was charged once
        # ($50.00), so the $21.65 charge must be order 902-2.
        orders = ORDER_HEADER + "".join([
            order_row("B1", "902-1", "Rug", "21.65", ordered="2026-10-19", shipped="2026-10-19"),
            order_row("B2", "902-1", "Mat", "28.35", ordered="2026-10-19", shipped="2026-10-22"),
            order_row("B3", "902-2", "Mug", "21.65", ordered="2026-10-19", shipped="2026-10-19"),
        ])
        import_csv(self.conn, self.card, "more.csv", "Date,Description,Amount\n10/19/2026,AMAZON MKTPL*Y1,-21.65\n10/22/2026,AMAZON MKTPL*Y2,-50.00\n")
        amazon.import_orders(self.conn, orders)
        found = self.matched()
        self.assertEqual((found["AMAZON MKTPL*Y1"], found["AMAZON MKTPL*Y2"]), (("902-2", "2026-10-19"), ("902-1", None)))

    def test_choosing_an_order_that_does_not_fit(self):
        amazon.import_orders(self.conn, ORDERS)
        with self.assertRaisesRegex(UserError, "doesn't match"):
            amazon.choose(self.conn, self.txn("AMAZON MKTPL*AA11BB22"), "900-0000003-0000003", None)
        with self.assertRaises(NotFound):
            amazon.choose(self.conn, self.txn("CORNER STORE"), "900-0000001-0000001", "2026-10-02")


class ReviewAndApplyTest(AmazonTestCase):
    def setUp(self):
        super().setUp()
        amazon.import_orders(self.conn, ORDERS)
        self.groceries = self.category_id("Food › Groceries")
        self.gifts = self.category_id("Gifts")
        self.order = self.txn("AMZN Mktp US*CC33DD44")

    def review_items(self, description):
        charge = next(c for c in amazon.review(self.conn)["charges"] if c["charge"]["description"] == description)
        return charge["order"]["items"]

    def test_guesses_from_the_department(self):
        items = {i["product_name"]: (i["category_id"], i["guess"]) for i in self.review_items("AMZN Mktp US*CC33DD44")}
        self.assertEqual(items["Mint Toothpaste 2 Pack"], (self.groceries, "department"))
        self.assertEqual(items["Birthday Card"], (None, None))

    def test_apply_splits_by_category_and_remembers_products(self):
        items = self.review_items("AMZN Mktp US*CC33DD44")
        choice = {"Building Blocks Set": self.gifts, "Birthday Card": self.gifts, "Mint Toothpaste 2 Pack": self.groceries}
        result = amazon.apply(self.conn, self.order, [{"item_id": i["id"], "category_id": choice[i["product_name"]]} for i in items])
        self.assertEqual((result["split"], result["parts"]), (True, 2))
        parts = sorted(p["amount_cents"] for p in ledger.get_transaction(self.conn, self.order)["parts"])
        self.assertEqual(parts, [-2998, -1049])
        self.assertNotIn(self.order, [c["charge"]["id"] for c in amazon.review(self.conn)["charges"]])
        # The same toothpaste in another order is now guessed from the product.
        self.assertEqual(self.review_items("AMAZON MKTPL*AA11BB22")[0]["guess"], "product")

    def test_one_category_means_no_split(self):
        charge = self.txn("AMAZON.COM*EE55FF66")
        items = amazon.details(self.conn, charge)["order"]["items"]
        amazon.apply(self.conn, charge, [{"item_id": i["id"], "category_id": self.gifts} for i in items])
        detail = ledger.get_transaction(self.conn, charge)
        self.assertEqual((detail["category_id"], detail["parts"]), (self.gifts, []))

    def test_already_sorted_charges_are_not_reviewed(self):
        ledger.set_category(self.conn, self.txn("AMAZON MKTPL*AA11BB22"), self.groceries)
        listed = [c["charge"]["description"] for c in amazon.review(self.conn)["charges"]]
        self.assertNotIn("AMAZON MKTPL*AA11BB22", listed)
        rows = amazon.for_transactions(self.conn, list_transactions(self.conn)["rows"])
        toothpaste = next(r for r in rows if r["description"] == "AMAZON MKTPL*AA11BB22")
        self.assertEqual((len(toothpaste["amazon"]["items"]), toothpaste["amazon"]["review"]), (1, False))

    def test_every_item_needs_a_category(self):
        items = self.review_items("AMZN Mktp US*CC33DD44")
        with self.assertRaisesRegex(UserError, "every item"):
            amazon.apply(self.conn, self.order, [{"item_id": items[0]["id"], "category_id": self.gifts}])
        with self.assertRaisesRegex(UserError, "Pick a category"):
            amazon.apply(self.conn, self.order, [{"item_id": i["id"], "category_id": None} for i in items])

    def test_department_mapping(self):
        found = {d["department"]: d["category_id"] for d in amazon.departments(self.conn)}
        self.assertEqual(found, {"Health and Beauty": self.groceries, "Toy": None})
        amazon.set_department(self.conn, "Toy", self.gifts)
        items = {i["product_name"]: i["guess"] for i in self.review_items("AMZN Mktp US*CC33DD44")}
        self.assertEqual(items["Building Blocks Set"], "department")
        amazon.set_department(self.conn, "Toy", None)
        self.assertIsNone({d["department"]: d["category_id"] for d in amazon.departments(self.conn)}["Toy"])


class NotesAndSearchTest(AmazonTestCase):
    def test_notes(self):
        store = self.txn("CORNER STORE")
        self.assertEqual(ledger.set_note(self.conn, store, "  snacks for   the trip "), {"id": store, "note": "snacks for the trip"})
        row = next(r for r in list_transactions(self.conn)["rows"] if r["id"] == store)
        self.assertEqual(row["note"], "snacks for the trip")
        ledger.set_note(self.conn, store, "")
        self.assertIsNone(next(r for r in list_transactions(self.conn)["rows"] if r["id"] == store)["note"])
        with self.assertRaises(UserError):
            ledger.set_note(self.conn, store, "x" * 501)
        with self.assertRaises(NotFound):
            ledger.set_note(self.conn, 9999, "hi")

    def test_search_finds_notes_and_items(self):
        ledger.set_note(self.conn, self.txn("CORNER STORE"), "snacks for the trip")
        amazon.import_orders(self.conn, ORDERS)
        search = lambda text: sorted(r["description"] for r in list_transactions(self.conn, Filter(text=text))["rows"])
        self.assertEqual(search("snacks"), ["CORNER STORE"])
        self.assertEqual(search("toothpaste"), ["AMAZON MKTPL*AA11BB22", "AMZN Mktp US*CC33DD44"])
        self.assertEqual(search("bulbs"), ["AMAZON.COM*EE55FF66"])
