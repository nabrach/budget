import unittest

from budgetapp import ledger
from budgetapp.errors import NotFound, UserError
from budgetapp.importer import NeedsInput, import_csv, list_layouts, save_layout, undo_import
from tests.helpers import CHASE_CREDIT_HEADER, CITI_HEADER, DbTestCase, fixture

COFFEE = "10/05/2026,10/05/2026,BLUE BOTTLE COFFEE,Food & Drink,Sale,-3.50,\n"


def count_transactions(conn, account_id):
    return conn.execute(
        "SELECT count(*) FROM transactions WHERE account_id = ?", (account_id,)
    ).fetchone()[0]


class ImportTest(DbTestCase):
    def test_import_summary(self):
        account = self.make_account()
        summary = import_csv(self.conn, account, "chase_cc1.csv", fixture("chase_credit_1.csv"))
        self.assertEqual(summary.rows_in_file, 9)
        self.assertEqual(summary.rows_added, 9)
        self.assertEqual(summary.rows_skipped, 0)
        self.assertEqual(summary.uncategorized, 0)
        self.assertEqual((summary.first_date, summary.last_date), ("2026-10-12", "2026-10-16"))

    def test_every_shape_imports_into_any_account(self):
        account = self.make_account()
        for name in ("chase_credit_1.csv", "chase_checking.csv", "citi_credit.csv"):
            import_csv(self.conn, account, name, fixture(name), allow_new_columns=True)
        self.assertEqual(count_transactions(self.conn, account), 9 + 27 + 16)

    def test_empty_file_is_rejected(self):
        with self.assertRaisesRegex(UserError, "no transactions"):
            import_csv(self.conn, self.make_account(), "empty.csv", CHASE_CREDIT_HEADER)

    def test_all_pending_file_is_rejected(self):
        account = self.make_account("Citi")
        text = CITI_HEADER + 'Pending,10/23/2026,"COFFEE",$ 4.50,\n'
        with self.assertRaisesRegex(UserError, "still pending"):
            import_csv(self.conn, account, "citi.csv", text)

    def test_summary_reports_pending_rows(self):
        account = self.make_account("Citi")
        text = (
            CITI_HEADER
            + 'Pending,10/23/2026,"COFFEE",$ 4.50,\n'
            + 'Cleared,10/22/2026,"GROCERY",$ 40.00,\n'
        )
        summary = import_csv(self.conn, account, "citi.csv", text)
        self.assertEqual((summary.rows_added, summary.rows_pending), (1, 1))

    def test_missing_account(self):
        with self.assertRaises(NotFound):
            import_csv(self.conn, 999, "x.csv", fixture("chase_credit_1.csv"))


class DuplicateTest(DbTestCase):
    def test_reimporting_the_same_file_adds_nothing(self):
        account = self.make_account()
        import_csv(self.conn, account, "a.csv", fixture("chase_credit_1.csv"))
        summary = import_csv(self.conn, account, "a.csv", fixture("chase_credit_1.csv"))
        self.assertEqual((summary.rows_added, summary.rows_skipped), (0, 9))
        self.assertEqual(count_transactions(self.conn, account), 9)

    def test_overlapping_file_adds_only_new_rows(self):
        account = self.make_account()
        lines = fixture("chase_credit_1.csv").splitlines(keepends=True)
        import_csv(self.conn, account, "first.csv", "".join(lines[:6]))  # header + 5 rows
        summary = import_csv(self.conn, account, "all.csv", "".join(lines))
        self.assertEqual((summary.rows_added, summary.rows_skipped), (4, 5))

    def test_identical_purchases_in_one_file_are_both_kept(self):
        account = self.make_account()
        summary = import_csv(self.conn, account, "a.csv", CHASE_CREDIT_HEADER + COFFEE * 2)
        self.assertEqual(summary.rows_added, 2)

    def test_identical_purchases_are_counted_across_files(self):
        account = self.make_account()
        import_csv(self.conn, account, "a.csv", CHASE_CREDIT_HEADER + COFFEE * 2)
        again = import_csv(self.conn, account, "b.csv", CHASE_CREDIT_HEADER + COFFEE * 2)
        self.assertEqual(again.rows_added, 0)
        third = import_csv(self.conn, account, "c.csv", CHASE_CREDIT_HEADER + COFFEE * 3)
        self.assertEqual((third.rows_added, third.rows_skipped), (1, 2))

    def test_same_purchase_in_different_accounts_is_not_a_duplicate(self):
        first = self.make_account("Card A")
        second = self.make_account("Card B")
        import_csv(self.conn, first, "a.csv", fixture("chase_credit_1.csv"))
        summary = import_csv(self.conn, second, "a.csv", fixture("chase_credit_1.csv"))
        self.assertEqual(summary.rows_added, 9)


class UndoTest(DbTestCase):
    def test_undo_removes_only_that_import(self):
        account = self.make_account()
        lines = fixture("chase_credit_1.csv").splitlines(keepends=True)
        import_csv(self.conn, account, "first.csv", "".join(lines[:6]))
        second = import_csv(self.conn, account, "all.csv", "".join(lines))
        self.assertEqual(undo_import(self.conn, second.batch_id), 4)
        self.assertEqual(count_transactions(self.conn, account), 5)
        self.assertEqual(len(ledger.list_batches(self.conn)), 1)

    def test_undo_twice_says_not_found(self):
        summary = import_csv(self.conn, self.make_account(), "a.csv", fixture("chase_credit_1.csv"))
        undo_import(self.conn, summary.batch_id)
        with self.assertRaises(NotFound):
            undo_import(self.conn, summary.batch_id)

    def test_undone_rows_can_be_imported_again(self):
        account = self.make_account()
        summary = import_csv(self.conn, account, "a.csv", fixture("chase_credit_1.csv"))
        undo_import(self.conn, summary.batch_id)
        again = import_csv(self.conn, account, "a.csv", fixture("chase_credit_1.csv"))
        self.assertEqual(again.rows_added, 9)


class NewColumnsTest(DbTestCase):
    def test_warns_when_columns_differ_from_last_import(self):
        account = self.make_account()
        import_csv(self.conn, account, "card.csv", fixture("chase_credit_1.csv"))
        with self.assertRaises(NeedsInput) as asked:
            import_csv(self.conn, account, "checking.csv", fixture("chase_checking.csv"))
        self.assertEqual(asked.exception.kind, "new_columns")
        self.assertIn("Chase Freedom", asked.exception.details["message"])
        self.assertEqual(count_transactions(self.conn, account), 9)

    def test_import_anyway(self):
        account = self.make_account()
        import_csv(self.conn, account, "card.csv", fixture("chase_credit_1.csv"))
        summary = import_csv(self.conn, account, "checking.csv", fixture("chase_checking.csv"), allow_new_columns=True)
        self.assertEqual(summary.rows_added, 27)
        # The new columns become the ones future imports are compared with.
        import_csv(self.conn, account, "checking.csv", fixture("chase_checking.csv"))

    def test_first_import_never_warns(self):
        account = self.make_account()
        import_csv(self.conn, account, "checking.csv", fixture("chase_checking.csv"))

    def test_same_columns_in_a_different_order_still_warn(self):
        account = self.make_account()
        import_csv(self.conn, account, "a.csv", "Date,Description,Amount\n10/01/2026,A,-1\n")
        with self.assertRaises(NeedsInput):
            import_csv(self.conn, account, "b.csv", "Description,Date,Amount\nB,10/02/2026,-1\n")


class DirectionTest(DbTestCase):
    AMEX_LIKE = "Date,Description,Amount\n" + "".join(
        f"10/{d:02d}/2026,SHOP {d},{d}.00\n" for d in range(1, 7)
    ) + "10/20/2026,PAYMENT - THANK YOU,-50.00\n"

    def ask(self, account, text, **options):
        with self.assertRaises(NeedsInput) as asked:
            import_csv(self.conn, account, "a.csv", text, **options)
        return asked.exception

    def test_first_single_amount_file_asks_with_a_guess(self):
        account = ledger.create_account(self.conn, "Amex")["id"]
        question = self.ask(account, self.AMEX_LIKE)
        self.assertEqual(question.kind, "direction")
        self.assertEqual(question.details["reason"], "first")
        self.assertEqual(question.details["guess"], -1)  # most rows positive: purchases are positive
        examples = question.details["examples"]
        self.assertEqual([e["description"] for e in examples], ["SHOP 6", "SHOP 5", "SHOP 4", "PAYMENT - THANK YOU"])
        self.assertEqual(count_transactions(self.conn, account), 0)

    def test_answer_is_used_and_remembered(self):
        account = ledger.create_account(self.conn, "Amex")["id"]
        import_csv(self.conn, account, "a.csv", self.AMEX_LIKE, amount_sign=-1)
        amounts = dict(self.conn.execute("SELECT description, amount_cents FROM transactions").fetchall())
        self.assertEqual(amounts["SHOP 6"], -600)
        self.assertEqual(amounts["PAYMENT - THANK YOU"], 5000)
        self.assertEqual(ledger.list_accounts(self.conn)[0]["amount_sign"], -1)
        # Next time it doesn't ask.
        later = self.AMEX_LIKE.replace("10/", "11/")
        self.assertEqual(import_csv(self.conn, account, "b.csv", later).rows_added, 7)

    def test_file_that_looks_reversed_asks_again(self):
        account = self.make_account("Card", amount_sign=1)  # expects purchases negative
        question = self.ask(account, self.AMEX_LIKE)
        self.assertEqual((question.details["reason"], question.details["guess"]), ("reversed", -1))

    def test_keeping_the_old_direction_after_a_reversed_warning(self):
        account = self.make_account("Card", amount_sign=1)
        summary = import_csv(self.conn, account, "a.csv", self.AMEX_LIKE, amount_sign=1)
        self.assertEqual(summary.rows_added, 7)

    def test_small_files_are_not_judged(self):
        account = self.make_account("Card", amount_sign=1)
        text = "Date,Description,Amount\n10/01/2026,REFUND,5.00\n10/02/2026,REFUND 2,6.00\n"
        self.assertEqual(import_csv(self.conn, account, "a.csv", text).rows_added, 2)

    def guess(self, text):
        self.guesses = getattr(self, "guesses", 0) + 1
        account = ledger.create_account(self.conn, f"Guess {self.guesses}")["id"]
        with self.assertRaises(NeedsInput) as asked:
            import_csv(self.conn, account, "a.csv", text)
        return asked.exception.details["guess"]

    def test_guess_uses_payment_and_credit_wording(self):
        # More negative rows than positive, but they're payments and credits.
        self.assertEqual(self.guess(fixture("amex_credit.csv")), -1)

    def test_guess_for_files_that_show_purchases_as_negative(self):
        self.assertEqual(self.guess(fixture("chase_credit_1.csv")), 1)
        self.assertEqual(self.guess(fixture("chase_credit_2.csv")), 1)
        self.assertEqual(self.guess(fixture("chase_checking.csv")), 1)

    def test_guess_uses_a_type_column(self):
        text = "Date,Description,Type,Amount\n" + "".join(
            f"10/0{d}/2026,STORE {d},Sale,{d}.00\n" for d in range(1, 3)
        ) + "".join(f"10/0{d}/2026,TRANSFER {d},Payment,-{d}.00\n" for d in range(3, 6))
        self.assertEqual(self.guess(text), -1)

    def test_debit_credit_files_never_ask(self):
        account = ledger.create_account(self.conn, "Citi")["id"]
        self.assertEqual(import_csv(self.conn, account, "c.csv", fixture("citi_credit.csv")).rows_added, 16)


class UnknownColumnsTest(DbTestCase):
    ODD = "When,What,How Much\n10/01/2026,BAKERY,-4.50\n10/02/2026,BOOKS,-12.00\n"

    def test_unrecognized_columns_ask_with_a_preview(self):
        with self.assertRaises(NeedsInput) as asked:
            import_csv(self.conn, self.make_account(), "odd.csv", self.ODD)
        self.assertEqual(asked.exception.kind, "columns")
        self.assertEqual(asked.exception.details["headers"], ["When", "What", "How Much"])
        self.assertEqual(asked.exception.details["rows"][0], ["10/01/2026", "BAKERY", "-4.50"])

    def test_saved_layout_is_used_next_time(self):
        save_layout(self.conn, ["When", "What", "How Much"], {"date": "When", "description": "What", "amount": "How Much"})
        summary = import_csv(self.conn, self.make_account(), "odd.csv", self.ODD)
        self.assertEqual(summary.rows_added, 2)
        self.assertEqual(len(list_layouts(self.conn)), 1)

    def test_saved_layout_beats_recognized_names(self):
        # The user says "Post Date" is the one to use, not "Transaction Date".
        headers = ["Transaction Date", "Post Date", "Description", "Amount"]
        save_layout(self.conn, headers, {"date": "Post Date", "description": "Description", "amount": "Amount"})
        text = ",".join(headers) + "\n10/01/2026,10/03/2026,A,-1\n"
        import_csv(self.conn, self.make_account(), "a.csv", text)
        self.assertEqual(self.conn.execute("SELECT date FROM transactions").fetchone()[0], "2026-10-03")

    def test_layout_validation(self):
        headers = ["When", "What", "How Much"]
        bad = [
            {"date": "When", "description": "What"},  # no amount
            {"date": "When", "description": "What", "debit": "How Much"},  # credit missing
            {"date": "Nope", "description": "What", "amount": "How Much"},
            {"date": "When", "description": "When", "amount": "How Much"},
            {"description": "What", "amount": "How Much"},
        ]
        for choices in bad:
            with self.subTest(choices=choices):
                with self.assertRaises(UserError):
                    save_layout(self.conn, headers, choices)


class AccountTest(DbTestCase):
    def test_duplicate_names_are_rejected(self):
        self.make_account("Chase Freedom")
        with self.assertRaisesRegex(UserError, "already have"):
            self.make_account("chase  freedom")

    def test_name_is_required(self):
        with self.assertRaises(UserError):
            ledger.create_account(self.conn, "  ")

    def test_amount_sign_can_be_changed(self):
        account = ledger.create_account(self.conn, "Card")["id"]
        self.assertIsNone(ledger.list_accounts(self.conn)[0]["amount_sign"])
        ledger.set_amount_sign(self.conn, account, -1)
        self.assertEqual(ledger.list_accounts(self.conn)[0]["amount_sign"], -1)
        with self.assertRaises(UserError):
            ledger.set_amount_sign(self.conn, account, 0)

    def test_list_accounts_counts_transactions(self):
        account = self.make_account()
        import_csv(self.conn, account, "a.csv", fixture("chase_credit_1.csv"))
        [listed] = ledger.list_accounts(self.conn)
        self.assertEqual(listed["transaction_count"], 9)
        self.assertEqual(listed["amount_sign"], 1)
        self.assertIsNotNone(listed["last_import"])


if __name__ == "__main__":
    unittest.main()
