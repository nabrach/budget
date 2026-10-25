# Budget

A local budgeting app. Import bank CSVs, auto-categorize purchases, and (soon)
track budgets, see charts and ask questions about your spending.

Runs on Python 3.12's standard library only. Nothing to install.

## Run it

```sh
python3 app.py
```

It opens http://localhost:8000. Your data is stored in `data/budget.db`
(ignored by git). To back up, copy that file.

| Setting | Default | |
|---|---|---|
| `BUDGET_DATA_DIR` | `./data` | Where the database lives |
| `PORT` | `8000` | |
| `HOST` | `127.0.0.1` | Use `0.0.0.0` inside Docker only |
| `BUDGET_NO_BROWSER` | unset | Set to `1` to skip opening a browser |

## Test it

```sh
python3 -m unittest
```

Test data is made up and lives in `tests/fixture_data.py`. Never commit real
statements: `.gitignore` blocks every CSV file and the `data/` folder.

## How it works

- **Accounts** are just names. You import from inside an account, and the
  app works out each file's columns (see below). If a file's columns differ
  from the account's last import, it warns before importing.
- **Duplicates** are skipped by matching account + date + amount +
  description. Identical purchases are counted, so two real $3.50 coffees on
  the same day are both kept.
- **Pending** rows (Citi) are left out until they clear.
- **Categories** have two levels: groups (Food) and subcategories
  (Food › Groceries). A transaction can go in either, and group totals include
  their subcategories. Manage them in Settings, or pick "+ New category…" in
  any category list.
- **Sorting into categories** happens in order: your keyword rules (longest
  match wins), the bank's transaction type (card payments become Transfer),
  the bank's own category, then Uncategorized. Changing a category offers to
  save a rule, and new rules re-sort everything you haven't set by hand.
- **Splitting**: an order that covers several categories (say, an Amazon
  order) can be split into parts, each with its own amount, category and
  person. The parts must add up exactly and replace the transaction in every
  total.
- **People**: transactions are Household (shared) unless a rule or you assign
  them to a person. Budgets can be per category for everyone or for one person
  (e.g. Personal: $200 for one person, $150 for another). The review list also
  shows transactions in per-person budgets that don't have a person yet.
- **Undo** any import from the Import page's history.
- **Transactions** can be sorted by any column, filtered by dates, account,
  category and text, and totalled by week, month, year, category or merchant.
  Click a total to see its transactions. Filters are kept in the page address,
  so any view can be bookmarked. Totals leave out transfers and subtract
  refunds.
- **Settings** holds people, monthly budgets (per category and person, plus
  an optional overall limit and the warning level), rules, categories, how the
  bank's own categories map to yours, accounts and saved column layouts.
- **Dashboard** shows this month's spending against your budgets, warnings
  (at your warning level, past 100%, or when spending is ahead of the month's
  pace) and charts of monthly spending, spending by category and money in vs
  spent. Click any bar or budget to see its transactions. The same warnings
  appear after each import.
- **Ask** answers 9 fill-in-the-blank questions from your data (no AI), such
  as "How much did I spend on Dining last month?", "What were my top 5
  merchants this year?" or "Am I over budget this month?". Answers link to
  the matching transactions.

## CSV files

Files can come from any bank. The app reads the header row and recognizes
common column names (ignoring case and punctuation):

| Field | Column names |
|---|---|
| Date | Transaction Date, Trans. Date, Date, Posting Date, Post Date, Posted Date (in that order of preference) |
| Description | Description, Payee, Merchant, Merchant Name, Name |
| Amount | Amount, Transaction Amount, **or** a pair: Debit/Credit, Withdrawal/Deposit, Money Out/Money In |
| Optional | Status (Pending rows are skipped until they clear), Type, Category |

If it can't recognize the columns, it asks you to match them once and
remembers that for files with the same header row (Settings → Saved column
layouts).

**Which way money goes.** Amounts are stored with money out as negative.

- Separate Debit and Credit columns: Debit is money out and Credit money in,
  whatever sign the bank prints (some print credits as `-$ 140.25`).
- One Amount column: banks disagree (Chase shows purchases as negative,
  Amex as positive), so each account remembers which way its purchases go.
  On the first such import the app guesses from clues in the file (payment,
  refund and credit wording, a Type or Category column) and asks you to
  confirm. If a later file looks reversed, it asks again. You can change it
  in Settings.

**Transfers.** Your keyword rules decide first. Otherwise a Type value of
Payment, LOAN_PMT or ACCT_XFER counts as a transfer.

**Repairs** for quirks seen in real exports:

- Unquoted commas in amounts: `-$ 1,284.70` arrives as two cells, `-$ 1` and
  `284.70`, and is joined back together.
- Unquoted commas in descriptions, like `FRAME SHOP, LLC`: the extra cell is
  merged back into the description.
- An extra comma at the end of each row: ignored.
- Amount styles `-12.50`, `$ 58.40`, `-$ 140.25`, `-$1284.70`, `(12.50)`,
  and dates like `10/23/2026`, `10/23/26` or `2026-10-23`.

When a row could be read more than one way, the importer keeps the reading
whose date and amounts are valid. If any row still can't be read, nothing
from the file is imported and the error names the line.

Code layout: `budgetapp/formats.py` (CSV formats), `importer.py`,
`categorize.py`, `queries.py` (filters, sorting, totals), `budgets.py`, `ask.py` (questions), `ledger.py` (changes), `db.py` (schema and
migrations), `server.py` (HTTP + JSON API). The page is `static/` (`charts.js` draws the charts).
