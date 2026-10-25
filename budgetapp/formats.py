"""Reading bank CSV files of any shape.

A file's layout (which column is the date, the description, the amount...) is
worked out from its header row, by recognizing common column names. Files
the app can't recognize get a layout the user picks once, saved by header row.

Amounts are stored in cents, negative for money leaving the account:
- One Amount column: banks disagree on the sign, so each account remembers
  which way its purchases go (amount_sign) and the parser applies it.
- Debit and Credit columns: Debit is money out and Credit money in, whatever
  sign the bank prints (Citi prints credits as "-$ 140.25").
"""

import csv
import io
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .errors import UserError


class FormatError(UserError):
    """The file can't be read."""


# Column names each field is recognized by, in order of preference, after
# normalizing (lowercase, punctuation dropped, spaces collapsed). The
# transaction date wins over the posting date.
SYNONYMS = {
    "date": ["transaction date", "trans date", "date", "posting date", "post date", "posted date"],
    "description": ["description", "payee", "merchant", "merchant name", "name", "transaction description"],
    "amount": ["amount", "transaction amount"],
    "debit": ["debit", "debits", "withdrawal", "withdrawals", "money out", "debit amount"],
    "credit": ["credit", "credits", "deposit", "deposits", "money in", "credit amount"],
    "status": ["status"],
    "type": ["type", "transaction type"],
    "category": ["category"],
}

# Status values meaning "not final yet": skipped until they clear.
PENDING_STATUSES = {"pending"}

DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d")


def normalize_header(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", name.lower()).split())


def header_signature(headers) -> str:
    """A stable key for a header row, used to remember layouts per file shape."""
    return "|".join(normalize_header(h) for h in headers)


@dataclass(frozen=True)
class Layout:
    """Which column holds what. Values are the file's own header names."""

    date: str
    description: str
    amount: str | None = None  # one signed column...
    debit: str | None = None  # ...or a money-out and a money-in column
    credit: str | None = None
    status: str | None = None
    type: str | None = None
    category: str | None = None

    @property
    def single_amount(self) -> bool:
        return self.amount is not None

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict, headers) -> "Layout":
        """Build and check a layout the user picked for these headers."""
        if not isinstance(data, dict):
            raise UserError("The column choices must be an object.")
        present = set(headers)
        fields = {}
        for field in ("date", "description", "amount", "debit", "credit", "status", "type", "category"):
            value = data.get(field) or None
            if value is not None and value not in present:
                raise UserError(f"The file has no column called “{value}”.")
            fields[field] = value
        if not fields["date"] or not fields["description"]:
            raise UserError("Pick the date and description columns.")
        if fields["amount"]:
            fields["debit"] = fields["credit"] = None
        elif not (fields["debit"] and fields["credit"]):
            raise UserError("Pick the amount column, or both the money-out and money-in columns.")
        used = [v for v in fields.values() if v]
        if len(used) != len(set(used)):
            raise UserError("Each column can only be used once.")
        return cls(**fields)


def detect_layout(headers) -> Layout | None:
    """Recognize a layout from common column names, or None if it can't."""
    by_name = {}
    for header in headers:
        by_name.setdefault(normalize_header(header), header)

    def find(field):
        return next((by_name[n] for n in SYNONYMS[field] if n in by_name), None)

    date, description = find("date"), find("description")
    debit, credit, amount = find("debit"), find("credit"), find("amount")
    if not (date and description):
        return None
    if debit and credit:
        amount = None  # separate columns say which way money went; prefer them
    elif amount:
        debit = credit = None
    else:
        return None
    return Layout(
        date=date,
        description=description,
        amount=amount,
        debit=debit,
        credit=credit,
        status=find("status"),
        type=find("type"),
        category=find("category"),
    )


@dataclass(frozen=True)
class Row:
    line: int
    date: str  # yyyy-mm-dd
    description: str
    amount_cents: int
    bank_category: str | None
    bank_type: str | None


@dataclass(frozen=True)
class ParsedFile:
    rows: list[Row]
    # Rows left out because they haven't cleared yet.
    pending: int = 0


def read_header(text: str) -> list[str]:
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    for header in reader:
        if any(cell.strip() for cell in header):
            return [h.strip() for h in header]
    raise FormatError("The file is empty.")


def preview_rows(text: str, count: int = 5) -> list[list[str]]:
    """The first few data rows, for showing the user when picking columns."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    return [[c.strip() for c in r] for r in rows[1 : count + 1]]


def parse_csv(text: str, layout: Layout, amount_sign: int = 1) -> ParsedFile:
    """Parse every row, or raise FormatError without returning partial results.

    amount_sign only applies to single-amount layouts: 1 if the file already
    shows purchases as negative, -1 if it shows them as positive.
    """
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    header = None
    for record in reader:
        if any(cell.strip() for cell in record):
            header = [h.strip() for h in record]
            break
    if header is None:
        raise FormatError("The file is empty.")
    missing = [c for c in (layout.date, layout.description, layout.amount, layout.debit, layout.credit) if c and c not in header]
    if missing:
        raise FormatError(f"The file is missing the column(s) {', '.join(missing)}. Nothing was imported.")

    index = {}
    for i, name in enumerate(header):
        index.setdefault(name, i)
    rows, errors, pending = [], [], 0
    for record in reader:
        if not any(cell.strip() for cell in record):
            continue
        line = reader.line_num

        # A row wider than the header can usually be read more than one way;
        # keep the first reading whose date and amounts parse.
        row, error, is_pending = None, None, False
        for candidate in _readings(record, len(header), index.get(layout.description)):
            try:
                row, is_pending = _read_row(layout, index, candidate, line, amount_sign)
                break
            except FormatError as e:
                error = error or e
        if is_pending:
            pending += 1
        elif row:
            rows.append(row)
        elif len(record) > len(header):
            errors.append(f"line {line}: more columns than the header ({error})")
        else:
            errors.append(f"line {line}: {error}")

    if errors:
        shown = "; ".join(errors[:3]) + ("; …" if len(errors) > 3 else "")
        raise FormatError(
            f"{len(errors)} row(s) couldn't be read ({shown}). Nothing was imported."
        )
    return ParsedFile(rows=rows, pending=pending)


def _read_row(layout: Layout, index: dict, record: list[str], line: int, amount_sign: int):
    """(row, is_pending). Raises FormatError if the row can't be read."""

    def cell(column):
        if column is None:
            return None
        i = index[column]
        return record[i].strip() if i < len(record) else ""

    if layout.status and cell(layout.status).lower() in PENDING_STATUSES:
        return None, True
    description = " ".join(cell(layout.description).split())
    if not description:
        raise FormatError("missing description")
    return (
        Row(
            line=line,
            date=_parse_date(cell(layout.date)),
            description=description,
            amount_cents=_row_amount(layout, cell, amount_sign),
            bank_category=cell(layout.category) or None,
            bank_type=cell(layout.type) or None,
        ),
        False,
    )


def _row_amount(layout: Layout, cell, amount_sign: int) -> int:
    if layout.single_amount:
        value = cell(layout.amount)
        if not value:
            raise FormatError(f"no amount in {layout.amount}")
        return amount_sign * _parse_amount(value)
    out, into = cell(layout.debit), cell(layout.credit)
    # Some banks fill the unused column with 0.00 rather than leaving it empty.
    out = out if out and _parse_amount(out) != 0 else ""
    into = into if into and _parse_amount(into) != 0 else ""
    if out and into:
        raise FormatError(f"more than one amount in {layout.debit} / {layout.credit}")
    if out:
        return -abs(_parse_amount(out))
    if into:
        return abs(_parse_amount(into))
    raise FormatError(f"no amount in {layout.debit} / {layout.credit}")


# An amount cut at an unquoted thousands separator: "-$ 1" then "284.70".
_AMOUNT_HEAD = re.compile(r"^\s*-?\s*\$?\s*-?\d{1,3}(,\d{3})*$")
_AMOUNT_TAIL = re.compile(r"^\d{3}(\.\d*)?\s*$")


def _rejoin_amounts(record: list[str], width: int) -> list[str]:
    """Rejoin amounts whose thousands separators weren't quoted, while the row is too wide."""
    record = list(record)
    i = 0
    while len(record) > width and i < len(record) - 1:
        if _AMOUNT_HEAD.match(record[i]) and _AMOUNT_TAIL.match(record[i + 1]):
            record[i : i + 2] = [record[i] + "," + record[i + 1]]
        else:
            i += 1
    return record


def _readings(record: list[str], width: int, description_index: int | None) -> list[list[str]]:
    """Ways to fit a row to the header, most likely first.

    1. Split amounts rejoined (see _rejoin_amounts).
    2. Empty trailing cells dropped: some exports end every row with an extra comma.
    3. Extra cells merged back into the description: an unquoted comma in a
       merchant name, like SHUTTERFLY, INC.
    The caller keeps the first reading that parses, so a wrong guess (which
    leaves text in an amount column) is skipped.
    """
    record = _rejoin_amounts(record, width)
    if len(record) <= width:
        return [record]
    trailing = 0
    while trailing < len(record) and not record[-1 - trailing].strip():
        trailing += 1
    fits, merged = [], []
    for drop in range(trailing + 1):
        trimmed = record[: len(record) - drop]
        if len(trimmed) <= width:
            fits.append(trimmed)
            break
        if description_index is not None:
            end = description_index + (len(trimmed) - width) + 1
            merged.append(trimmed[:description_index] + [",".join(trimmed[description_index:end])] + trimmed[end:])
    # Dropping trailing empties is the more common fix, so it's tried first.
    return fits + merged


def _parse_date(value: str) -> str:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    raise FormatError(f"can't read date '{value}'")


def _parse_amount(value: str) -> int:
    # Handles "-12.50", "$ 1,284.70", "-$ 1,284.70" and "(12.50)".
    text = "".join(value.replace("$", "").replace(",", "").split())
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise FormatError(f"can't read amount '{value}'") from None
    if not amount.is_finite():
        raise FormatError(f"can't read amount '{value}'")
    cents = int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return -cents if negative else cents
