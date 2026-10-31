import tempfile
import unittest
from datetime import date
from pathlib import Path

from budgetapp import db, ledger, transfers
from budgetapp.ask import answer
from budgetapp.errors import NotFound, UserError
from budgetapp.importer import import_csv, undo_import
from budgetapp.queries import Filter, list_transactions
from tests.helpers import DbTestCase

HEADER = "Date,Description,Amount\n"

# Made-up files. The checking side of the first payment isn't recognized by any
# rule (it's Uncategorized); the card side is ("ONLINE PAYMENT, THANK YOU").
CHECKING = HEADER + "".join([
    "10/03/2026,XYZ BILLPAY 0042,-500.00\n",
    "10/05/2026,ACME CORP PAYROLL,2000.00\n",
    "10/06/2026,CORNER GROCER,-42.10\n",
    "10/20/2026,CITI CARD ONLINE PAYMENT 0001,-120.00\n",
    "10/28/2026,CORNER GROCER,-18.00\n",
])
CARD = HEADER + "".join([
    "10/01/2026,BOOKSTORE,-30.00\n",
    "10/02/2026,\"ONLINE PAYMENT, THANK YOU\",500.00\n",
    "10/19/2026,\"ONLINE PAYMENT, THANK YOU\",120.00\n",
    "10/27/2026,HARDWARE STORE,-64.00\n",
])
OCT_29 = date(2026, 10, 29)


class TransferTestCase(DbTestCase):
    def setUp(self):
        super().setUp()
        self.checking = self.make_account("Checking")
        self.card = self.make_account("Card")

    def import_both(self):
        self.checking_batch = import_csv(self.conn, self.checking, "checking.csv", CHECKING).batch_id
        self.card_batch = import_csv(self.conn, self.card, "card.csv", CARD).batch_id

    def txn(self, description):
        return self.conn.execute("SELECT id FROM transactions WHERE description = ?", (description,)).fetchone()[0]

    def by_amount(self, cents):
        return self.conn.execute("SELECT id FROM transactions WHERE amount_cents = ?", (cents,)).fetchone()[0]

    def category_of(self, transaction_id):
        return self.conn.execute(
            "SELECT c.name, t.category_source FROM transactions t JOIN categories c ON c.id = t.category_id WHERE t.id = ?",
            (transaction_id,),
        ).fetchone()[:]

    def pairs(self):
        return [(s["out"]["description"], s["in"]["description"], s["days"], s["tie"], s["clear"]) for s in transfers.suggestions(self.conn)]

    def add(self, account, date_, description, cents):
        """Add one transaction through an import, as the app would."""
        dollars = f"{cents / 100:.2f}"
        return import_csv(self.conn, account, "extra.csv", HEADER + f'{date_},"{description}",{dollars}\n').batch_id


class SuggestionTest(TransferTestCase):
    def test_payments_are_suggested_with_the_closest_date(self):
        self.import_both()
        self.assertEqual(
            self.pairs(),
            [
                ("CITI CARD ONLINE PAYMENT 0001", "ONLINE PAYMENT, THANK YOU", 1, False, True),
                ("XYZ BILLPAY 0042", "ONLINE PAYMENT, THANK YOU", 1, False, True),
            ],
        )

    def test_import_summary_counts_suggestions(self):
        import_csv(self.conn, self.checking, "checking.csv", CHECKING)
        summary = import_csv(self.conn, self.card, "card.csv", CARD)
        self.assertEqual((summary.transfer_suggestions, summary.transfers_linked), (2, 0))

    def test_what_is_not_suggested(self):
        self.import_both()
        cases = [
            # Same amount, but neither side looks like a transfer.
            (self.checking, "11/02/2026", "COFFEE BAR", -700, self.card, "11/02/2026", "REFUND COFFEE", 700),
            # More than 5 days apart.
            (self.checking, "11/01/2026", "ONLINE TRANSFER 1", -900, self.card, "11/08/2026", "PAYMENT THANK YOU", 900),
            # Same account.
            (self.card, "11/03/2026", "ONLINE TRANSFER 2", -800, self.card, "11/03/2026", "PAYMENT THANK YOU", 800),
        ]
        for case in cases:
            self.add(case[0], case[1], case[2], case[3])
            self.add(case[4], case[5], case[6], case[7])
        self.assertEqual(len(self.pairs()), 2)  # just the two from the files

    def test_split_transactions_are_left_out(self):
        self.import_both()
        payment = self.txn("XYZ BILLPAY 0042")
        food = self.category_id("Food")
        ledger.split_transaction(self.conn, payment, [
            {"amount_cents": -400, "category_id": food}, {"amount_cents": -49600, "category_id": food},
        ])
        self.assertEqual([p[0] for p in self.pairs()], ["CITI CARD ONLINE PAYMENT 0001"])

    def test_equally_close_candidates_are_a_tie(self):
        self.add(self.card, "10/10/2026", "PAYMENT THANK YOU", 25000)
        self.add(self.checking, "10/09/2026", "ONLINE TRANSFER A", -25000)
        self.add(self.checking, "10/11/2026", "ONLINE TRANSFER B", -25000)
        found = self.pairs()
        self.assertEqual(len(found), 2)
        self.assertTrue(all(tie and not clear for *_, tie, clear in found))

    def test_a_category_set_by_hand_is_not_clear(self):
        self.import_both()
        ledger.set_category(self.conn, self.txn("XYZ BILLPAY 0042"), self.category_id("Housing"))
        found = {p[0]: p[4] for p in self.pairs()}
        self.assertEqual(found, {"XYZ BILLPAY 0042": False, "CITI CARD ONLINE PAYMENT 0001": True})


class LinkTest(TransferTestCase):
    def setUp(self):
        super().setUp()
        self.import_both()
        self.out = self.txn("XYZ BILLPAY 0042")
        self.into = self.by_amount(50000)

    def spent(self):
        return list_transactions(self.conn, Filter(start="2026-10-01", end="2026-10-31"), limit=0)["spent_cents"]

    def test_linking_makes_both_sides_transfers(self):
        before = self.spent()
        result = transfers.link(self.conn, self.out, self.into)
        self.assertEqual(result["other"]["account_name"], "Card")
        self.assertEqual(self.category_of(self.out)[0], "Transfer")
        self.assertEqual(self.spent(), before - 50000)  # the payment no longer counts as spending
        self.assertNotIn("XYZ BILLPAY 0042", [p[0] for p in self.pairs()])

    def test_rows_show_the_other_side(self):
        transfers.link(self.conn, self.out, self.into)
        rows = transfers.annotate(self.conn, list_transactions(self.conn)["rows"])
        by_id = {r["id"]: r["transfer"] for r in rows}
        self.assertEqual(by_id[self.out]["status"], "linked")
        self.assertEqual(by_id[self.out]["other"]["id"], self.into)
        self.assertEqual(by_id[self.into]["other"]["account_name"], "Checking")
        suggested = by_id[self.txn("CITI CARD ONLINE PAYMENT 0001")]
        self.assertEqual((suggested["status"], len(suggested["options"])), ("suggested", 1))
        self.assertIsNone(by_id[self.txn("BOOKSTORE")])

    def test_unlink_restores_categories_and_is_remembered(self):
        transfers.link(self.conn, self.out, self.into)
        transfers.unlink(self.conn, self.into)
        self.assertEqual(self.category_of(self.out), ("Uncategorized", "none"))
        self.assertEqual(self.category_of(self.into), ("Transfer", "rule"))
        self.assertNotIn("XYZ BILLPAY 0042", [p[0] for p in self.pairs()])
        self.assertEqual(transfers.get_settings(self.conn)["confirmed"], 0)
        with self.assertRaises(NotFound):
            transfers.unlink(self.conn, self.into)

    def test_unlink_restores_a_category_set_by_hand(self):
        housing = self.category_id("Housing")
        ledger.set_category(self.conn, self.out, housing)
        transfers.link(self.conn, self.out, self.into)
        transfers.unlink(self.conn, self.out)
        self.assertEqual(self.category_of(self.out), ("Housing", "manual"))

    def test_not_a_match(self):
        transfers.reject(self.conn, self.into, self.out)  # either order
        self.assertNotIn("XYZ BILLPAY 0042", [p[0] for p in self.pairs()])
        transfers.link(self.conn, self.txn("CITI CARD ONLINE PAYMENT 0001"), self.by_amount(12000))
        self.assertEqual({k: v for k, v in transfers.get_settings(self.conn).items() if k in ("confirmed", "decided")},
                         {"confirmed": 1, "decided": 2})

    def test_linked_rows_keep_their_category(self):
        transfers.link(self.conn, self.out, self.into)
        with self.assertRaisesRegex(UserError, "Unlink it first"):
            ledger.set_category(self.conn, self.out, self.category_id("Food"))
        with self.assertRaisesRegex(UserError, "Unlink it first"):
            ledger.split_transaction(self.conn, self.out, [])
        ledger.create_rule(self.conn, "XYZ BILLPAY", self.category_id("Housing"))
        self.assertEqual(self.category_of(self.out)[0], "Transfer")

    def test_rules_still_apply_after_unlinking(self):
        transfers.link(self.conn, self.out, self.into)
        ledger.create_rule(self.conn, "XYZ BILLPAY", self.category_id("Housing"))
        transfers.unlink(self.conn, self.out)
        self.assertEqual(self.category_of(self.out), ("Housing", "rule"))

    def test_checks(self):
        cases = [
            ((self.out, self.out), "two different"),
            ((self.out, self.txn("BOOKSTORE")), "amounts don't match"),
        ]
        for ids, message in cases:
            with self.subTest(message), self.assertRaisesRegex(UserError, message):
                transfers.link(self.conn, *ids)
        transfers.link(self.conn, self.out, self.into)
        with self.assertRaisesRegex(UserError, "already linked"):
            transfers.link(self.conn, self.out, self.into)
        with self.assertRaises(NotFound):
            transfers.link(self.conn, self.out, 9999)


class ManualLinkTest(TransferTestCase):
    def test_candidates_and_wider_window(self):
        self.import_both()
        self.add(self.checking, "10/01/2026", "MONEY MOVE", -7700)
        self.add(self.card, "10/20/2026", "CREDIT", 7700)
        self.add(self.card, "10/25/2026", "CREDIT 2", 7700)
        out = self.txn("MONEY MOVE")
        found = transfers.candidates(self.conn, out)
        self.assertEqual([c["description"] for c in found["candidates"]], ["CREDIT", "CREDIT 2"])
        with self.assertRaisesRegex(UserError, "at most 5 days"):
            transfers.link(self.conn, out, self.txn("CREDIT"))  # a suggestion can't be that far apart
        transfers.link(self.conn, out, self.txn("CREDIT"), origin="manual")
        self.assertEqual(self.category_of(out)[0], "Transfer")

    def test_more_than_30_days_apart(self):
        self.add(self.checking, "09/01/2026", "MONEY MOVE", -7700)
        self.add(self.card, "10/20/2026", "CREDIT", 7700)
        self.assertEqual(transfers.candidates(self.conn, self.txn("MONEY MOVE"))["candidates"], [])
        with self.assertRaisesRegex(UserError, "at most 30 days"):
            transfers.link(self.conn, self.txn("MONEY MOVE"), self.txn("CREDIT"), origin="manual")


class RemovingDataTest(TransferTestCase):
    def setUp(self):
        super().setUp()
        self.import_both()
        self.out = self.txn("XYZ BILLPAY 0042")
        self.into = self.by_amount(50000)
        transfers.link(self.conn, self.out, self.into)

    def test_undoing_an_import_unlinks(self):
        undo_import(self.conn, self.card_batch)
        self.assertEqual(self.category_of(self.out), ("Uncategorized", "none"))
        self.assertIsNone(transfers.link_for(self.conn, self.out))

    def test_deleting_an_account_unlinks(self):
        ledger.delete_account(self.conn, self.checking)
        self.assertEqual(self.category_of(self.into), ("Transfer", "rule"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM transfer_links").fetchone()[0], 0)


class AutoLinkTest(TransferTestCase):
    def test_off_by_default(self):
        self.assertFalse(transfers.get_settings(self.conn)["auto_link"])

    def test_clear_matches_link_on_import(self):
        transfers.set_auto_link(self.conn, True)
        import_csv(self.conn, self.checking, "checking.csv", CHECKING)
        summary = import_csv(self.conn, self.card, "card.csv", CARD)
        self.assertEqual((summary.transfers_linked, summary.transfer_suggestions), (2, 0))
        origins = [r[0] for r in self.conn.execute("SELECT origin FROM transfer_links")]
        self.assertEqual(origins, ["auto", "auto"])

    def test_ties_still_ask(self):
        transfers.set_auto_link(self.conn, True)
        self.add(self.checking, "10/09/2026", "ONLINE TRANSFER A", -25000)
        self.add(self.checking, "10/11/2026", "ONLINE TRANSFER B", -25000)
        summary = import_csv(self.conn, self.card, "card.csv", HEADER + "10/10/2026,PAYMENT THANK YOU,250.00\n")
        self.assertEqual((summary.transfers_linked, summary.transfer_suggestions), (0, 2))

    def test_setting_must_be_true_or_false(self):
        with self.assertRaises(UserError):
            transfers.set_auto_link(self.conn, "yes")


class NoMatchTest(TransferTestCase):
    def setUp(self):
        super().setUp()
        self.import_both()
        # A transfer to savings, which isn't imported.
        self.add(self.checking, "10/12/2026", "ONLINE TRANSFER TO SAV 0099", -10000)
        self.savings = self.txn("ONLINE TRANSFER TO SAV 0099")

    def unmatched(self):
        return [r["description"] for r in transfers.review(self.conn)["unmatched"]]

    def test_flagged_once_other_accounts_cover_the_dates(self):
        self.assertEqual(self.unmatched(), ["ONLINE TRANSFER TO SAV 0099"])
        # The card's file runs to 10/27, so a transfer on 10/25 can't be judged yet.
        self.add(self.checking, "10/25/2026", "ONLINE TRANSFER TO SAV 0099 B", -5000)
        self.assertEqual(self.unmatched(), ["ONLINE TRANSFER TO SAV 0099"])

    def test_suggested_and_linked_transfers_are_not_flagged(self):
        self.assertNotIn("XYZ BILLPAY 0042", self.unmatched())
        self.assertNotIn("CITI CARD ONLINE PAYMENT 0001", self.unmatched())
        self.assertNotIn("ONLINE PAYMENT, THANK YOU", self.unmatched())

    def test_other_side_not_tracked(self):
        transfers.untrack(self.conn, self.savings)
        self.assertEqual(self.unmatched(), [])

    def test_dont_flag_ones_like_this(self):
        review = transfers.review(self.conn)["unmatched"]
        self.assertEqual(review[0]["suggested_pattern"], "ONLINE TRANSFER TO SAV")
        ignore = transfers.add_ignore(self.conn, "online transfer to sav")
        self.assertEqual(self.unmatched(), [])
        self.assertEqual(transfers.get_settings(self.conn)["ignores"], [{"id": ignore["id"], "pattern": "ONLINE TRANSFER TO SAV"}])
        transfers.delete_ignore(self.conn, ignore["id"])
        self.assertEqual(self.unmatched(), ["ONLINE TRANSFER TO SAV 0099"])
        with self.assertRaises(UserError):
            transfers.add_ignore(self.conn, " ")

    def test_filter_on_transactions(self):
        found = list_transactions(self.conn, Filter(no_match=True))
        self.assertEqual([r["description"] for r in found["rows"]], ["ONLINE TRANSFER TO SAV 0099"])


class AskTest(TransferTestCase):
    def test_paid_toward(self):
        self.import_both()
        for s in transfers.suggestions(self.conn):
            transfers.link(self.conn, s["out"]["id"], s["in"]["id"])
        result = answer(self.conn, "paid_toward", {"account": str(self.card), "period": "this-month"}, OCT_29)
        self.assertEqual(result["text"], "You paid $620.00 toward Card this month, across 2 payments from Checking.")
        self.assertEqual(result["link"]["account_id"], self.card)

    def test_paid_toward_mentions_waiting_suggestions(self):
        self.import_both()
        result = answer(self.conn, "paid_toward", {"account": str(self.card), "period": "this-month"}, OCT_29)
        self.assertEqual(
            result["text"],
            "No linked payments went into Card this month. 2 possible transfers into it are waiting for you to review on the Import page.",
        )


class UpgradeTest(unittest.TestCase):
    def test_v8_adds_the_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = db.connect(path)
            conn.execute("PRAGMA foreign_keys = OFF")
            for number, migration in enumerate(db.MIGRATIONS[:7], start=1):
                with db.transaction(conn):
                    migration(conn)
                    conn.execute(f"PRAGMA user_version = {number}")
            db.init(path)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))
            self.assertFalse(transfers.auto_link_enabled(conn))
            self.assertEqual(transfers.suggestions(conn), [])
            conn.close()


if __name__ == "__main__":
    unittest.main()
