from budgetapp import ledger, transfers
from budgetapp.errors import NotFound, UserError
from budgetapp.importer import import_csv
from budgetapp.queries import Filter, list_transactions
from tests.test_transfers import CARD, CHECKING, TransferTestCase


class DeleteTransactionsTest(TransferTestCase):
    def setUp(self):
        super().setUp()
        self.import_both()

    def descriptions(self, account):
        return sorted(r["description"] for r in list_transactions(self.conn, Filter(account_id=account))["rows"])

    def test_delete_one_and_several(self):
        result = ledger.delete_transactions(self.conn, [self.txn("BOOKSTORE")])
        self.assertEqual(result, {"deleted": 1, "amount_cents": -3000, "transfers_unlinked": 0})
        grocer = [r[0] for r in self.conn.execute("SELECT id FROM transactions WHERE description = 'CORNER GROCER'")]
        ledger.delete_transactions(self.conn, grocer + grocer)  # repeats are fine
        self.assertNotIn("CORNER GROCER", self.descriptions(self.checking))
        self.assertNotIn("BOOKSTORE", self.descriptions(self.card))

    def test_split_parts_go_too(self):
        payroll = self.txn("ACME CORP PAYROLL")
        income = self.category_id("Income")
        ledger.split_transaction(self.conn, payroll, [
            {"amount_cents": 150000, "category_id": income}, {"amount_cents": 50000, "category_id": income},
        ])
        ledger.delete_transactions(self.conn, [payroll])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM transaction_parts").fetchone()[0], 0)

    def test_linked_transfers_are_unlinked(self):
        out, into = self.txn("XYZ BILLPAY 0042"), self.by_amount(50000)
        transfers.link(self.conn, out, into)
        result = ledger.delete_transactions(self.conn, [into])
        self.assertEqual(result["transfers_unlinked"], 1)
        self.assertEqual(self.category_of(out), ("Uncategorized", "none"))

    def test_nothing_is_deleted_if_any_id_is_missing(self):
        with self.assertRaises(NotFound):
            ledger.delete_transactions(self.conn, [self.txn("BOOKSTORE"), 99999])
        self.assertIn("BOOKSTORE", self.descriptions(self.card))

    def test_bad_input(self):
        for ids in ([], None, ["1"], [True], list(range(ledger.MAX_DELETE + 1))):
            with self.subTest(ids=str(ids)[:20]), self.assertRaises(UserError):
                ledger.delete_transactions(self.conn, ids)

    def test_reimporting_brings_them_back(self):
        ledger.delete_transactions(self.conn, [self.txn("BOOKSTORE")])
        summary = import_csv(self.conn, self.card, "card.csv", CARD)
        self.assertEqual((summary.rows_added, summary.rows_skipped), (1, 3))
        self.assertIn("BOOKSTORE", self.descriptions(self.card))
        self.assertEqual(len(self.descriptions(self.checking)), len(CHECKING.splitlines()) - 1)
