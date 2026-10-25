import unittest

from budgetapp.errors import UserError
from budgetapp.formats import (
    FormatError,
    Layout,
    detect_layout,
    header_signature,
    parse_csv,
    read_header,
)
from tests.helpers import CHASE_CREDIT_HEADER, CITI_HEADER, fixture


def layout_of(text):
    return detect_layout(read_header(text))


def parse(text, amount_sign=1):
    return parse_csv(text, layout_of(text), amount_sign)


class DetectLayoutTest(unittest.TestCase):
    def test_single_amount_with_extras(self):
        layout = layout_of(fixture("chase_credit_1.csv"))
        self.assertEqual(
            layout,
            Layout(date="Transaction Date", description="Description", amount="Amount", type="Type", category="Category"),
        )
        self.assertTrue(layout.single_amount)

    def test_posting_date_used_when_it_is_the_only_date(self):
        layout = layout_of(fixture("chase_checking.csv"))
        self.assertEqual((layout.date, layout.amount, layout.type), ("Posting Date", "Amount", "Type"))
        # "Details" (DEBIT/CREDIT) isn't mistaken for the description.
        self.assertEqual(layout.description, "Description")

    def test_debit_and_credit_columns(self):
        layout = layout_of(fixture("citi_credit.csv"))
        self.assertEqual(
            layout, Layout(date="Date", description="Description", debit="Debit", credit="Credit", status="Status")
        )
        self.assertFalse(layout.single_amount)

    def test_minimal_and_detailed_single_amount_files(self):
        self.assertEqual(layout_of(fixture("amex_credit.csv")), Layout(date="Date", description="Description", amount="Amount"))
        detailed = layout_of(fixture("amex_credit_details.csv"))
        self.assertEqual((detailed.amount, detailed.category), ("Amount", "Category"))

    def test_other_common_names(self):
        cases = {
            "Posted Date,Payee,Withdrawal,Deposit": Layout(date="Posted Date", description="Payee", debit="Withdrawal", credit="Deposit"),
            "Trans. Date,Merchant Name,Transaction Amount": Layout(date="Trans. Date", description="Merchant Name", amount="Transaction Amount"),
            "DATE , NAME , AMOUNT": Layout(date="DATE", description="NAME", amount="AMOUNT"),
            "Date,Description,Money Out,Money In": Layout(date="Date", description="Description", debit="Money Out", credit="Money In"),
        }
        for header, expected in cases.items():
            with self.subTest(header):
                self.assertEqual(detect_layout([h.strip() for h in header.split(",")]), expected)

    def test_debit_credit_preferred_over_amount(self):
        layout = detect_layout(["Date", "Description", "Amount", "Debit", "Credit"])
        self.assertEqual((layout.amount, layout.debit, layout.credit), (None, "Debit", "Credit"))

    def test_unrecognized(self):
        for headers in (["When", "What", "How Much"], ["Date", "Amount"], ["Date", "Description", "Debit"]):
            with self.subTest(headers):
                self.assertIsNone(detect_layout(headers))

    def test_signature_ignores_case_and_punctuation_but_not_order(self):
        self.assertEqual(header_signature(["Check or Slip #", " Date"]), "check or slip|date")
        self.assertNotEqual(header_signature(["Date", "Amount"]), header_signature(["Amount", "Date"]))


class SingleAmountTest(unittest.TestCase):
    def test_purchases_negative_files_are_kept_as_is(self):
        rows = parse(fixture("chase_credit_1.csv")).rows
        self.assertEqual(len(rows), 9)
        first = rows[0]
        self.assertEqual((first.date, first.amount_cents, first.bank_category, first.bank_type), ("2026-10-16", -3115, "Travel", "Sale"))
        self.assertEqual(first.description, "LYFT *RIDE TRIP A1")  # spaces collapsed

    def test_purchases_positive_files_are_flipped(self):
        rows = parse(fixture("amex_credit.csv"), amount_sign=-1).rows
        amounts = {r.description: r.amount_cents for r in rows}
        self.assertEqual(len(rows), 9)
        self.assertEqual(amounts["STREAMING TV SUBSCRIPTN SAN JOSE CA"], -6499)
        self.assertEqual(amounts["MOBILE PAYMENT - THANK YOU"], 25000)
        self.assertEqual(amounts["BISTRO MAIN ST SPRINGFIELD"], 1200)  # a refund

    def test_transaction_date_not_post_date(self):
        rows = parse(fixture("chase_credit_1.csv")).rows
        self.assertEqual(rows[1].date, "2026-10-14")  # posted 10/16

    def test_checking_whole_dollars(self):
        rows = parse(fixture("chase_checking.csv")).rows
        self.assertEqual((len(rows), rows[0].amount_cents), (27, -15000))

    def test_detailed_download_with_category_and_multiline_cells(self):
        rows = parse(fixture("amex_credit_details.csv"), amount_sign=-1).rows
        self.assertEqual(
            [(r.description, r.amount_cents, r.bank_category) for r in rows],
            [("BISTRO MAIN ST SPRINGFIELD", -8810, "Restaurant-Restaurant"), ("MOBILE PAYMENT - THANK YOU", 25000, None)],
        )


class DebitCreditTest(unittest.TestCase):
    def test_debits_out_credits_in_whatever_the_printed_sign(self):
        rows = parse(fixture("citi_credit.csv")).rows
        amounts = {r.description: r.amount_cents for r in rows}
        self.assertEqual(len(rows), 16)
        self.assertEqual(amounts["ACME INSURANCE SPRINGFIELD IL"], -5840)
        self.assertEqual(amounts["ONLINE PAYMENT, THANK YOU"], 128470)  # printed "-$ 1,284.70"

    def test_positive_credits_and_negative_debits(self):
        text = "Date,Description,Debit,Credit\n10/01/2026,PAY,,500.00\n10/02/2026,SHOP,-20.00,\n"
        self.assertEqual([r.amount_cents for r in parse(text).rows], [50000, -2000])

    def test_zero_in_the_unused_column(self):
        text = "Date,Description,Debit,Credit\n10/01/2026,SHOP,20.00,0.00\n10/02/2026,PAY,0,15\n"
        self.assertEqual([r.amount_cents for r in parse(text).rows], [-2000, 1500])

    def test_row_needs_exactly_one_amount(self):
        with self.assertRaisesRegex(FormatError, "no amount"):
            parse(CITI_HEADER + 'Cleared,10/22/2026,"X",,\n')
        with self.assertRaisesRegex(FormatError, "more than one amount"):
            parse(CITI_HEADER + 'Cleared,10/22/2026,"X",$ 2.50,-$ 2.50\n')

    def test_pending_rows_are_left_out_and_counted(self):
        text = CITI_HEADER + 'Pending,10/23/2026,"COFFEE",$ 4.50,\n' + 'Cleared,10/22/2026,"GROCERY",$ 40.00,\n'
        parsed = parse(text)
        self.assertEqual(([r.description for r in parsed.rows], parsed.pending), (["GROCERY"], 1))

    def test_pending_is_matched_ignoring_case(self):
        text = "Status,Date,Description,Amount\nPENDING,10/01/2026,A,-1\nposted,10/01/2026,B,-2\n"
        parsed = parse(text)
        self.assertEqual(([r.description for r in parsed.rows], parsed.pending), (["B"], 1))


class RepairsTest(unittest.TestCase):
    """Real exports: quoted or unquoted descriptions, "-$ 140.25" or "-$1284.70",
    refunds in the Credit column, and commas that aren't quoted."""

    def test_every_variant(self):
        parsed = parse(fixture("citi_credit_variants.csv"))
        self.assertEqual(
            [(r.description, r.amount_cents) for r in parsed.rows],
            [
                ("DESIGN APP* I00000-11111111 AUSTIN TX null XXXXXXXXXXXX0001", -1285),
                ("ONLINE PAYMENT, THANK YOU", 14025),
                ("ONLINE PAYMENT, THANK YOU", 121080),
                ("PHOTO PRINTS, INC. 5550000000 CA null XXXXXXXXXXXX0002", -1370),
                ("FRAME SHOP, LLC SPRINGFIELD ST", -4200),  # unquoted comma in the description
                ("BIG FURNITURE SPRINGFIELD ST", -245000),  # unquoted comma in the amount
                ("CRAFT STORE 0000 SPRINGFIELD ST", 1160),  # refund in the Credit column
                ("CRAFT STORE 0000 SPRINGFIELD ST", -2990),
            ],
        )
        self.assertEqual(parsed.pending, 1)

    def test_comma_in_both_description_and_amount(self):
        row = parse(CITI_HEADER + "Cleared,10/07/2026,FRAME SHOP, LLC SPRINGFIELD ST,$ 1,042.00,\n").rows[0]
        self.assertEqual((row.description, row.amount_cents), ("FRAME SHOP, LLC SPRINGFIELD ST", -104200))

    def test_split_amount_over_a_million(self):
        row = parse(CITI_HEADER + "Cleared,10/07/2026,HOUSE,$ 1,234,567.89,\n").rows[0]
        self.assertEqual(row.amount_cents, -123456789)

    def test_split_amount_spilling_into_an_empty_column(self):
        row = parse(CITI_HEADER + 'Cleared,10/22/2026,"BIG TV",$ 12,345.67,\n').rows[0]
        self.assertEqual(row.amount_cents, -1234567)

    def test_row_that_cannot_be_fitted_is_an_error(self):
        with self.assertRaisesRegex(FormatError, "more columns than the header"):
            parse(CITI_HEADER + 'Cleared,10/22/2026,"A",B,$ 2.50,X\n')

    def test_extra_trailing_comma(self):
        text = "Date,Description,Amount\n10/01/2026,A,-1.00,\n"
        self.assertEqual(parse(text).rows[0].amount_cents, -100)


class AmountAndDateTest(unittest.TestCase):
    def parse_amount(self, amount):
        text = CHASE_CREDIT_HEADER + f'10/01/2026,10/01/2026,TEST,,Sale,"{amount}",\n'
        return parse(text).rows[0].amount_cents

    def test_amount_variants(self):
        for text, cents in {"-1,234.56": -123456, "$12.5": 1250, "(9.99)": -999, "-$ 1,284.70": -128470, "-$1284.70": -128470}.items():
            with self.subTest(text):
                self.assertEqual(self.parse_amount(text), cents)

    def test_date_styles(self):
        for value in ("10/03/2026", "10/03/26", "2026-10-03"):
            with self.subTest(value):
                text = f"Date,Description,Amount\n{value},A,-1\n"
                self.assertEqual(parse(text).rows[0].date, "2026-10-03")

    def test_bad_row_rejects_whole_file(self):
        text = CHASE_CREDIT_HEADER + "10/01/2026,10/01/2026,GOOD,,Sale,-1.00,\n" + "not a date,10/01/2026,BAD,,Sale,-1.00,\n"
        with self.assertRaisesRegex(FormatError, "line 3.*can't read date"):
            parse(text)

    def test_byte_order_mark_and_blank_lines_are_ignored(self):
        text = "﻿" + CHASE_CREDIT_HEADER + "10/01/2026,10/01/2026,A,,Sale,-1,\n\n,,,,,,\n"
        self.assertEqual(len(parse(text).rows), 1)

    def test_empty_file(self):
        with self.assertRaisesRegex(FormatError, "empty"):
            read_header("")


class LayoutFromChoicesTest(unittest.TestCase):
    def test_valid(self):
        layout = Layout.from_dict({"date": "When", "description": "What", "amount": "How Much", "category": ""}, ["When", "What", "How Much"])
        self.assertEqual(layout, Layout(date="When", description="What", amount="How Much"))

    def test_amount_wins_over_debit_credit(self):
        layout = Layout.from_dict({"date": "A", "description": "B", "amount": "C", "debit": "D", "credit": "E"}, list("ABCDE"))
        self.assertEqual((layout.amount, layout.debit), ("C", None))

    def test_invalid(self):
        with self.assertRaises(UserError):
            Layout.from_dict("nope", ["A"])


if __name__ == "__main__":
    unittest.main()
