"""Monthly budget limits and the warning threshold."""

from collections import defaultdict
from datetime import date, timedelta

from .db import transaction
from .errors import UserError
from .queries import Filter, list_transactions

DEFAULT_WARN_PCT = 80
MAX_LIMIT_CENTS = 100_000_000_00  # $100M: anything bigger is a typo


def _setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_budgets(conn):
    """limits: [{"category_id", "person_id", "monthly_limit_cents"}], where
    person_id None means the budget covers everyone."""
    overall = _setting(conn, "overall_monthly_limit_cents")
    return {
        "limits": [
            dict(row)
            for row in conn.execute(
                "SELECT category_id, person_id, monthly_limit_cents FROM budgets ORDER BY category_id, person_id"
            )
        ],
        "overall_cents": int(overall) if overall else None,
        "warn_pct": int(_setting(conn, "warn_pct", DEFAULT_WARN_PCT)),
    }


def _limit(value, what):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise UserError(f"The limit for {what} must be a whole number of cents.")
    if not 0 < value <= MAX_LIMIT_CENTS:
        raise UserError(f"The limit for {what} must be more than $0.")
    return value


def save_budgets(conn, limits, overall_cents, warn_pct):
    """Replace all budgets.

    limits: [{"category_id", "person_id" (optional, None = everyone),
    "monthly_limit_cents" (None = no budget)}].
    """
    if not isinstance(limits, list):
        raise UserError("'limits' must be a list of budgets.")
    if isinstance(warn_pct, bool) or not isinstance(warn_pct, int) or not 1 <= warn_pct <= 99:
        raise UserError("The warning level must be a whole percentage from 1 to 99.")
    overall_cents = _limit(overall_cents, "all spending")

    categories = {row["id"]: row for row in conn.execute("SELECT id, name, kind FROM categories")}
    people = {row["id"]: row["name"] for row in conn.execute("SELECT id, name FROM people")}
    rows, seen = [], set()
    for item in limits:
        if not isinstance(item, dict):
            raise UserError("Each budget needs a category and an amount.")
        try:
            category = categories[int(item.get("category_id"))]
        except (KeyError, TypeError, ValueError):
            raise UserError("A budget refers to a category that doesn't exist.") from None
        person_id = item.get("person_id")
        if person_id is not None and person_id not in people:
            raise UserError("A budget refers to a person who doesn't exist.")
        what = category["name"] + (f" ({people[person_id]})" if person_id else "")
        cents = _limit(item.get("monthly_limit_cents"), what)
        if cents is None:
            continue
        if category["kind"] != "expense":
            raise UserError(f"{category['name']} isn't a spending category, so it can't have a budget.")
        if (category["id"], person_id) in seen:
            raise UserError(f"{what} has two budgets.")
        seen.add((category["id"], person_id))
        rows.append((category["id"], person_id, cents))

    with transaction(conn):
        conn.execute("DELETE FROM budgets")
        conn.executemany(
            "INSERT INTO budgets (category_id, person_id, monthly_limit_cents) VALUES (?, ?, ?)", rows
        )
        if overall_cents is None:
            conn.execute("DELETE FROM settings WHERE key = 'overall_monthly_limit_cents'")
        else:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('overall_monthly_limit_cents', ?)",
                (str(overall_cents),),
            )
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('warn_pct', ?)", (str(warn_pct),)
        )
    return get_budgets(conn)


# ---- Status for the current month ---------------------------------------------

# "Ahead of pace" when the share of a budget spent is this many percentage
# points above the share of the month gone, e.g. 70% spent with 40% gone.
PACE_MARGIN_PCT = 15


def _money(cents):
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return start, end


def _status(name, spent, limit, warn_pct, elapsed_pct, days_left):
    pct = spent * 100 / limit
    left = "on the last day of the month" if days_left == 0 else f"with {days_left} day{'s' if days_left != 1 else ''} left"
    if spent > limit:
        state = "over"
        message = f"{name} is over budget by {_money(spent - limit)} ({_money(spent)} of {_money(limit)})."
    elif pct >= warn_pct:
        state = "warn"
        message = f"{name} is at {pct:.0f}% of its budget ({_money(spent)} of {_money(limit)}) {left}."
    elif pct >= elapsed_pct + PACE_MARGIN_PCT:
        state = "pace"
        message = f"{name} is ahead of pace: {pct:.0f}% of its budget spent with {elapsed_pct:.0f}% of the month gone."
    else:
        state = "ok"
        message = None
    return {
        "name": name,
        "spent_cents": spent,
        "limit_cents": limit,
        "pct": round(pct, 1),
        "state": state,
        "message": message,
    }


def budget_status(conn, today: date):
    """How this month's spending compares with each budget, plus warning messages."""
    start, end = month_bounds(today)
    month = Filter(start=start.isoformat(), end=end.isoformat())
    settings = get_budgets(conn)
    warn_pct = settings["warn_pct"]
    elapsed_pct = today.day * 100 / end.day
    days_left = end.day - today.day

    # Spending this month per (category, person) and per (category, everyone);
    # a group's totals include its subcategories.
    spent = defaultdict(int)
    for row in conn.execute(
        """SELECT t.category_id, c.parent_id, t.person_id, -sum(t.amount_cents) AS spent
           FROM entries t JOIN categories c ON c.id = t.category_id
           WHERE t.date BETWEEN ? AND ? AND c.kind IN ('expense', 'uncategorized')
           GROUP BY t.category_id, t.person_id""",
        (start.isoformat(), end.isoformat()),
    ):
        for category_id in (row["category_id"], row["parent_id"]):
            if category_id is None:
                continue
            spent[(category_id, None)] += row["spent"]
            if row["person_id"] is not None:
                spent[(category_id, row["person_id"])] += row["spent"]

    categories = []
    for row in conn.execute(
        """SELECT c.id, coalesce(g.name || ' › ', '') || c.name AS name, b.monthly_limit_cents,
                  b.person_id, p.name AS person_name
           FROM budgets b JOIN categories c ON c.id = b.category_id
           LEFT JOIN categories g ON g.id = c.parent_id
           LEFT JOIN people p ON p.id = b.person_id
           ORDER BY coalesce(g.sort_order, c.sort_order), coalesce(c.parent_id, c.id), c.parent_id IS NOT NULL,
                    c.sort_order, b.person_id IS NOT NULL, p.sort_order"""
    ):
        name = row["name"] + (f" ({row['person_name']})" if row["person_name"] else "")
        status = _status(name, spent[(row["id"], row["person_id"])], row["monthly_limit_cents"], warn_pct, elapsed_pct, days_left)
        categories.append({"category_id": row["id"], "person_id": row["person_id"], **status})

    total = list_transactions(conn, month, limit=0)["spent_cents"]
    overall = None
    if settings["overall_cents"]:
        overall = _status("Total spending", total, settings["overall_cents"], warn_pct, elapsed_pct, days_left)

    # Worst first: over, then near the limit, then ahead of pace.
    order = {"over": 0, "warn": 1, "pace": 2}
    flagged = [s for s in ([overall] if overall else []) + categories if s["state"] != "ok"]
    flagged.sort(key=lambda s: order[s["state"]])
    return {
        "month": start.isoformat()[:7],
        "label": f"{start:%B %Y}",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days_left": days_left,
        "elapsed_pct": round(elapsed_pct, 1),
        "warn_pct": warn_pct,
        "spent_cents": total,
        "overall": overall,
        "categories": categories,
        "alerts": [s["message"] for s in flagged],
    }
