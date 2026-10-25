"""Filtering, sorting and totalling transactions.

Everything here reads the `entries` view: unsplit transactions, plus the
parts of split ones, so a split order counts in each of its categories.

Money rules used everywhere:
- Spent = money out minus refunds, over expense categories and Uncategorized.
- Money in = income categories.
- Transfers between your own accounts count as neither.

Categories have two levels. Filtering by a group includes its subcategories
(unless `category_exact`), and "category" totals are per group.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from .categorize import suggest_pattern
from .errors import UserError

MAX_ROWS = 1000
SEPARATOR = " › "

_FULL_NAME = f"coalesce(g.name || '{SEPARATOR}', '') || c.name"
_GROUP_ID = "coalesce(c.parent_id, c.id)"

SORTS = {
    "date": "t.date",
    "amount": "t.amount_cents",
    "description": "t.description COLLATE NOCASE",
    "category": f"{_FULL_NAME} COLLATE NOCASE",
    "account": "a.name COLLATE NOCASE",
}

GROUPS = ("week", "month", "year", "category", "subcategory", "merchant", "person")

HOUSEHOLD = "household"  # the person filter value for shared (no person)

_FROM = """FROM entries t
           JOIN accounts a ON a.id = t.account_id
           JOIN categories c ON c.id = t.category_id
           LEFT JOIN categories g ON g.id = c.parent_id
           LEFT JOIN people pe ON pe.id = t.person_id"""

# Entries waiting for you: no category yet, or in a category that's budgeted
# per person but with no person set.
_NEEDS_REVIEW = """(c.kind = 'uncategorized' OR (t.person_id IS NULL AND EXISTS (
    SELECT 1 FROM budgets b WHERE b.person_id IS NOT NULL
      AND (b.category_id = t.category_id OR b.category_id = c.parent_id))))"""


@dataclass(frozen=True)
class Filter:
    start: str | None = None  # yyyy-mm-dd, inclusive
    end: str | None = None  # yyyy-mm-dd, inclusive
    category_id: int | None = None  # a group includes its subcategories...
    category_exact: bool = False  # ...unless this is set
    account_id: int | None = None
    text: str | None = None  # matched anywhere in the description, any case
    person: int | str | None = None  # a person's id, or HOUSEHOLD for shared
    needs_review: bool = False

    def __post_init__(self):
        for name in ("start", "end"):
            value = getattr(self, name)
            if value:
                try:
                    date.fromisoformat(value)
                except ValueError:
                    raise UserError(f"'{name}' must be a date like 2026-10-31.") from None
        if self.start and self.end and self.start > self.end:
            raise UserError("The start date is after the end date.")
        if self.person not in (None, HOUSEHOLD) and not isinstance(self.person, int):
            raise UserError("'person' must be a person's id or 'household'.")

    def where(self) -> tuple[str, list]:
        clauses, params = [], []
        if self.start:
            clauses.append("t.date >= ?")
            params.append(self.start)
        if self.end:
            clauses.append("t.date <= ?")
            params.append(self.end)
        if self.category_id is not None:
            if self.category_exact:
                clauses.append("t.category_id = ?")
                params.append(self.category_id)
            else:
                clauses.append("(t.category_id = ? OR c.parent_id = ?)")
                params += [self.category_id, self.category_id]
        if self.account_id is not None:
            clauses.append("t.account_id = ?")
            params.append(self.account_id)
        if self.text and self.text.strip():
            escaped = self.text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("t.description LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        if self.person == HOUSEHOLD:
            clauses.append("t.person_id IS NULL")
        elif self.person is not None:
            clauses.append("t.person_id = ?")
            params.append(self.person)
        if self.needs_review:
            clauses.append(_NEEDS_REVIEW)
        return ("WHERE " + " AND ".join(clauses) if clauses else ""), params


_SPENT = "CASE WHEN c.kind IN ('expense', 'uncategorized') THEN -t.amount_cents ELSE 0 END"
_INCOME = "CASE WHEN c.kind = 'income' THEN t.amount_cents ELSE 0 END"


def list_transactions(conn, flt: Filter = Filter(), sort="date", direction="desc", limit=MAX_ROWS):
    """Matching entries (up to limit) plus count and totals for all matches.

    A split transaction appears once per part: id is the transaction, part_id
    the part, and part_number / parts_count say which part it is.
    """
    if sort not in SORTS:
        raise UserError(f"Can't sort by '{sort}'.")
    if direction not in ("asc", "desc"):
        raise UserError("Sort direction must be 'asc' or 'desc'.")
    where, params = flt.where()

    summary = conn.execute(
        f"""SELECT count(*) AS count,
                   coalesce(sum({_SPENT}), 0) AS spent_cents,
                   coalesce(sum(CASE WHEN c.kind IN ('expense', 'uncategorized') THEN 1 ELSE 0 END), 0)
                       AS spending_count,
                   coalesce(sum({_INCOME}), 0) AS income_cents
            {_FROM} {where}""",
        params,
    ).fetchone()

    # Ties fall back to newest first, then part order, so the order is stable.
    rows = conn.execute(
        f"""SELECT t.transaction_id AS id, t.part_id, t.date, t.description, t.amount_cents,
                   t.category_id, t.category_source, t.bank_category, a.name AS account_name,
                   t.person_id, pe.name AS person_name,
                   {_FULL_NAME} AS category_name, c.kind AS category_kind,
                   (SELECT count(*) FROM transaction_parts p WHERE p.transaction_id = t.transaction_id) AS parts_count,
                   (SELECT count(*) FROM transaction_parts p
                    WHERE p.transaction_id = t.transaction_id AND p.id <= t.part_id) AS part_number
            {_FROM} {where}
            ORDER BY {SORTS[sort]} {direction.upper()}, t.date DESC, t.transaction_id DESC, t.part_id
            LIMIT ?""",
        (*params, limit),
    )
    return {"rows": [dict(row) for row in rows], **dict(summary)}


def group_totals(conn, flt: Filter, group_by: str):
    """Totals per week, month, year, category group, subcategory or merchant.

    Each group includes the filter that narrows the list down to it, so the
    page can show its transactions when clicked. Transfers are left out.
    """
    if group_by not in GROUPS:
        raise UserError(f"Can't group by '{group_by}'.")
    where, params = flt.where()
    rows = conn.execute(
        f"""SELECT t.date, t.description, t.amount_cents, c.id AS category_id, c.name AS category_name,
                   {_GROUP_ID} AS group_id, coalesce(g.name, c.name) AS group_name, c.parent_id, c.kind,
                   t.person_id, pe.name AS person_name
            {_FROM} {where}""",
        params,
    )
    has_children = {r[0] for r in conn.execute("SELECT DISTINCT parent_id FROM categories WHERE parent_id IS NOT NULL")}

    groups = {}
    totals = defaultdict(lambda: {"count": 0, "spent_cents": 0, "income_cents": 0})
    for row in rows:
        if row["kind"] == "transfer":
            continue
        key, info = _group_of(row, group_by, has_children)
        groups.setdefault(key, info)
        total = totals[key]
        total["count"] += 1
        if row["kind"] == "income":
            total["income_cents"] += row["amount_cents"]
        else:
            total["spent_cents"] -= row["amount_cents"]

    result = [{"key": key, **groups[key], **totals[key]} for key in groups]
    if group_by in ("week", "month", "year"):
        result.sort(key=lambda g: g["key"], reverse=True)
    else:
        result.sort(key=lambda g: (-g["spent_cents"], -g["income_cents"], g["label"]))
    return result


def _group_of(row, group_by, has_children):
    """(key, {"label", "filter"}) for the group this row belongs to."""
    if group_by == "category":
        return str(row["group_id"]), {
            "label": row["group_name"],
            "filter": {"category_id": row["group_id"]},
        }
    if group_by == "subcategory":
        if row["parent_id"] is not None:
            label = f"{row['group_name']}{SEPARATOR}{row['category_name']}"
            return str(row["category_id"]), {"label": label, "filter": {"category_id": row["category_id"]}}
        if row["category_id"] in has_children:
            # Put directly in a group that also has subcategories.
            return str(row["category_id"]), {
                "label": f"{row['category_name']} (other)",
                "filter": {"category_id": row["category_id"], "category_exact": True},
            }
        return str(row["category_id"]), {"label": row["category_name"], "filter": {"category_id": row["category_id"]}}
    if group_by == "merchant":
        merchant = suggest_pattern(row["description"])
        return merchant, {"label": merchant, "filter": {"text": merchant}}
    if group_by == "person":
        if row["person_id"] is None:
            return HOUSEHOLD, {"label": "Household", "filter": {"person": HOUSEHOLD}}
        return str(row["person_id"]), {"label": row["person_name"], "filter": {"person": row["person_id"]}}

    day = date.fromisoformat(row["date"])
    if group_by == "week":  # Monday to Sunday
        start = day - timedelta(days=day.weekday())
        end = start + timedelta(days=6)
        label = f"Week of {start:%b} {start.day}, {start.year}"
    elif group_by == "month":
        start = day.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        label = f"{start:%b %Y}"
    else:
        start, end = date(day.year, 1, 1), date(day.year, 12, 31)
        label = str(day.year)
    return start.isoformat(), {
        "label": label,
        "filter": {"start": start.isoformat(), "end": end.isoformat()},
    }


def data_range(conn) -> tuple[str, str] | None:
    """(first, last) transaction date, or None when there are no transactions."""
    row = conn.execute("SELECT min(date) AS first, max(date) AS last FROM transactions").fetchone()
    return (row["first"], row["last"]) if row["first"] else None


def _months(start: date, end: date):
    month = start.replace(day=1)
    while month <= end:
        following = (month + timedelta(days=32)).replace(day=1)
        yield month, following - timedelta(days=1)
        month = following


def monthly_totals(conn, start: str, end: str, category_id: int | None = None):
    """Spent and money in for every month from start to end, including empty months.

    by_category holds spending per category group id (spending kinds only).
    With category_id, only that category (and its subcategories) is counted.
    """
    flt = Filter(start=start, end=end, category_id=category_id)
    where, params = flt.where()
    rows = conn.execute(
        f"""SELECT substr(t.date, 1, 7) AS month, {_GROUP_ID} AS group_id, c.kind,
                   sum(t.amount_cents) AS total
            {_FROM} {where}
            GROUP BY month, group_id, c.kind""",
        params,
    )
    months = {
        first.isoformat()[:7]: {
            "month": first.isoformat()[:7],
            "label": f"{first:%b %Y}",
            "start": first.isoformat(),
            "end": last.isoformat(),
            "spent_cents": 0,
            "income_cents": 0,
            "by_category": {},
        }
        for first, last in _months(date.fromisoformat(start), date.fromisoformat(end))
    }
    for row in rows:
        month = months[row["month"]]
        if row["kind"] == "income":
            month["income_cents"] += row["total"]
        elif row["kind"] in ("expense", "uncategorized"):
            month["spent_cents"] -= row["total"]
            key = str(row["group_id"])
            month["by_category"][key] = month["by_category"].get(key, 0) - row["total"]
    return list(months.values())


def category_rank(conn) -> list[int]:
    """Spending category groups from most to least spent, over all time.

    Charts give colors in this order, so a group keeps its color when the
    date range changes.
    """
    return [
        row["group_id"]
        for row in conn.execute(
            f"""SELECT {_GROUP_ID} AS group_id, -sum(t.amount_cents) AS spent {_FROM}
                WHERE c.kind IN ('expense', 'uncategorized')
                GROUP BY group_id HAVING spent > 0
                ORDER BY spent DESC, min(coalesce(g.sort_order, c.sort_order))"""
        )
    ]


def biggest_purchases(conn, flt: Filter, limit: int):
    """The largest single purchases (money out in spending categories)."""
    where, params = flt.where()
    spending = "c.kind IN ('expense', 'uncategorized') AND t.amount_cents < 0"
    where = f"{where} AND {spending}" if where else f"WHERE {spending}"
    return [
        dict(row)
        for row in conn.execute(
            f"""SELECT t.transaction_id AS id, t.date, t.description, t.amount_cents,
                       {_FULL_NAME} AS category_name, a.name AS account_name
                {_FROM} {where}
                ORDER BY t.amount_cents ASC, t.date DESC LIMIT ?""",
            (*params, limit),
        )
    ]
