"""Answers to fill-in-the-blank questions about your money. No AI: each
question type is a small function over the same queries the pages use.

A question has a template made of text and slots. The page renders the slots
as dropdowns (or a text box), then posts {"type", "params"} to answer().
Every answer is {"question", "text", "table", "bars", "link"}; table, bars and
link are optional.
"""

from datetime import date, timedelta

from . import budgets
from .categorize import suggest_pattern
from .errors import UserError
from .queries import (
    HOUSEHOLD,
    Filter,
    biggest_purchases,
    group_totals,
    list_transactions,
    monthly_totals,
)

EVERYTHING = "all"
EVERYONE = "all"


def _money(cents):
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def _day(iso, year=False):
    d = date.fromisoformat(iso)
    return f"{d:%B} {d.day}, {d.year}" if year else f"{d:%B} {d.day}"


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---- Periods ---------------------------------------------------------------------
# Each period has a phrase that reads inside a sentence ("this month",
# "in October 2026") and a noun for comparisons ("October 2026").

def _month_end(day: date) -> date:
    return (day.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def _preset_periods(today: date):
    month_start = today.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    week_start = today - timedelta(days=today.weekday())
    last_week_start = week_start - timedelta(days=7)
    return {
        "this-month": (month_start, _month_end(today), "this month", f"{month_start:%B %Y}"),
        "last-month": (last_month_start, last_month_end, f"last month ({last_month_start:%B})", f"{last_month_start:%B %Y}"),
        "this-week": (week_start, week_start + timedelta(days=6), "this week", "this week"),
        "last-week": (last_week_start, week_start - timedelta(days=1), "last week", "last week"),
        "last-30-days": (today - timedelta(days=29), today, "in the last 30 days", "the last 30 days"),
        "this-year": (date(today.year, 1, 1), date(today.year, 12, 31), "this year", str(today.year)),
        "last-year": (date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), "last year", str(today.year - 1)),
        "all": (None, None, "overall", "all time"),
    }


def _data_months(conn):
    return [
        row["month"]
        for row in conn.execute(
            "SELECT DISTINCT substr(date, 1, 7) AS month FROM transactions ORDER BY month DESC"
        )
    ]


def resolve_period(key: str, today: date):
    """(Filter, phrase, noun) for a period key like 'last-month' or 'month:2026-09'."""
    presets = _preset_periods(today)
    if key in presets:
        start, end, phrase, noun = presets[key]
    elif isinstance(key, str) and key.startswith("month:"):
        try:
            start = date.fromisoformat(key.removeprefix("month:") + "-01")
        except ValueError:
            raise UserError("That month isn't valid.") from None
        end = _month_end(start)
        phrase, noun = f"in {start:%B %Y}", f"{start:%B %Y}"
    else:
        raise UserError("Pick a time period.")
    flt = Filter(start=start.isoformat() if start else None, end=end.isoformat() if end else None)
    return flt, phrase, noun


def period_options(conn, today: date):
    presets = _preset_periods(today)
    options = [{"value": key, "phrase": p[2], "noun": p[3]} for key, p in presets.items()]
    for month in _data_months(conn):
        first = date.fromisoformat(month + "-01")
        options.append({"value": f"month:{month}", "phrase": f"in {first:%B %Y}", "noun": f"{first:%B %Y}"})
    return options


# ---- Slots -------------------------------------------------------------------------

def _spending_categories(conn):
    """Spending groups and subcategories in tree order, with full names."""
    return conn.execute(
        """SELECT c.id, coalesce(g.name || ' › ', '') || c.name AS name
           FROM categories c LEFT JOIN categories g ON g.id = c.parent_id
           WHERE c.kind IN ('expense', 'uncategorized')
           ORDER BY coalesce(g.sort_order, c.sort_order), coalesce(c.parent_id, c.id),
                    c.parent_id IS NOT NULL, c.sort_order, c.name"""
    ).fetchall()


def _category(conn, value):
    """(category_id or None for everything, name used in sentences)."""
    if value in (EVERYTHING, None, ""):
        return None, "everything"
    try:
        category_id = int(value)
    except (TypeError, ValueError):
        raise UserError("Pick a category.") from None
    for row in _spending_categories(conn):
        if row["id"] == category_id:
            return category_id, row["name"]
    raise UserError("That category doesn't exist (or isn't a spending category).")


def _choice(value, allowed, what):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = None
    if number not in allowed:
        raise UserError(f"Pick {what}.")
    return number


def _link(flt: Filter, **extra):
    link = {"start": flt.start, "end": flt.end}
    link.update({k: v for k, v in extra.items() if v is not None})
    return link


def _spent(conn, flt):
    """(spent, number of purchases and refunds), leaving out transfers and income."""
    summary = list_transactions(conn, flt, limit=0)
    return summary["spent_cents"], summary["spending_count"]


# ---- Question types --------------------------------------------------------------

def _person(conn, value):
    """(Filter person value, who for sentences) for "all", "household" or a person's id."""
    if value in (EVERYONE, None, ""):
        return None, "You"
    if value == HOUSEHOLD:
        return HOUSEHOLD, "The household"
    try:
        person_id = int(value)
    except (TypeError, ValueError):
        raise UserError("Pick who.") from None
    row = conn.execute("SELECT name FROM people WHERE id = ?", (person_id,)).fetchone()
    if row is None:
        raise UserError("That person doesn't exist.")
    return person_id, row["name"]


def spend_on(conn, today, person, category, period):
    person_filter, who = _person(conn, person)
    category_id, name = _category(conn, category)
    base, phrase, _ = resolve_period(period, today)
    flt = Filter(start=base.start, end=base.end, category_id=category_id, person=person_filter)
    spent, count = _spent(conn, flt)
    on = "" if category_id is None else f" on {name}"
    if count == 0:
        text = f"{who} didn't spend anything{on} {phrase}."
    else:
        text = f"{who} spent {_money(spent)}{on} {phrase}, across {_plural(count, 'transaction')}."
    return {"text": text, "link": _link(flt, category_id=category_id, person=person_filter)}


def spend_at(conn, today, merchant, period):
    merchant = " ".join(str(merchant or "").split())
    if len(merchant) < 2:
        raise UserError("Type at least 2 letters of the merchant's name.")
    base, phrase, _ = resolve_period(period, today)
    flt = Filter(start=base.start, end=base.end, text=merchant)
    spent, count = _spent(conn, flt)
    if count == 0:
        text = f"No transactions mention “{merchant}” {phrase}."
    else:
        text = f"You spent {_money(spent)} at “{merchant}” {phrase}, across {_plural(count, 'transaction')}."
    return {"text": text, "link": _link(flt, text=merchant)}


def where_money_went(conn, today, period):
    flt, phrase, _ = resolve_period(period, today)
    groups = [g for g in group_totals(conn, flt, "category") if g["spent_cents"] > 0]
    if not groups:
        return {"text": f"You didn't spend anything {phrase}."}
    total = sum(g["spent_cents"] for g in groups)
    top = groups[0]
    share = top["spent_cents"] * 100 / total
    return {
        "text": (
            f"You spent {_money(total)} {phrase}. The biggest category was {top['label']} "
            f"at {_money(top['spent_cents'])} ({share:.0f}% of your spending)."
        ),
        "bars": [{"label": g["label"], "cents": g["spent_cents"], "link": _link(flt, category_id=g["filter"]["category_id"])} for g in groups],
        "table": {
            "headers": ["Category", "Spent", "Share", "Transactions"],
            "rows": [[g["label"], _money(g["spent_cents"]), f"{g['spent_cents'] * 100 / total:.0f}%", g["count"]] for g in groups],
        },
        "link": _link(flt),
    }


def top_merchants(conn, today, count, period):
    count = _choice(count, (3, 5, 10), "how many merchants")
    flt, phrase, _ = resolve_period(period, today)
    groups = [g for g in group_totals(conn, flt, "merchant") if g["spent_cents"] > 0][:count]
    if not groups:
        return {"text": f"You didn't spend anything {phrase}."}
    top = groups[0]
    return {
        "text": f"Your top merchant {phrase} was {top['label']}, at {_money(top['spent_cents'])}.",
        "bars": [{"label": g["label"], "cents": g["spent_cents"], "link": _link(flt, text=g["filter"]["text"])} for g in groups],
        "table": {
            "headers": ["Merchant", "Spent", "Transactions"],
            "rows": [[g["label"], _money(g["spent_cents"]), g["count"]] for g in groups],
        },
    }


def biggest(conn, today, count, period):
    count = _choice(count, (3, 5, 10), "how many purchases")
    flt, phrase, _ = resolve_period(period, today)
    rows = biggest_purchases(conn, flt, count)
    if not rows:
        return {"text": f"You didn't make any purchases {phrase}."}
    top = rows[0]
    return {
        "text": (
            f"Your biggest purchase {phrase} was {_money(-top['amount_cents'])} at "
            f"{suggest_pattern(top['description'])} on {_day(top['date'])}."
        ),
        "table": {
            "headers": ["Date", "Description", "Category", "Amount"],
            "rows": [
                [_day(r["date"], year=True), r["description"], r["category_name"], _money(-r["amount_cents"])]
                for r in rows
            ],
        },
        "link": {**_link(flt), "sort": "amount", "dir": "asc"},
    }


def compare(conn, today, category, period_a, period_b):
    category_id, name = _category(conn, category)
    first, _, noun_a = resolve_period(period_a, today)
    second, _, noun_b = resolve_period(period_b, today)
    if (first.start, first.end) == (second.start, second.end):
        raise UserError("Pick two different periods to compare.")
    a, _ = _spent(conn, Filter(start=first.start, end=first.end, category_id=category_id))
    b, _ = _spent(conn, Filter(start=second.start, end=second.end, category_id=category_id))
    on = "in total" if category_id is None else f"on {name}"
    text = f"You spent {_money(a)} {on} in {noun_a} and {_money(b)} in {noun_b}"
    if a == b:
        text += ", the same amount."
    else:
        change = f"{_money(abs(b - a))} {'more' if b > a else 'less'}"
        pct = f" ({(b - a) * 100 / a:+.0f}%)" if a > 0 else ""
        text += f": {change} in {noun_b}{pct}."
    return {
        "text": text,
        "table": {"headers": ["Period", "Spent"], "rows": [[noun_a, _money(a)], [noun_b, _money(b)]]},
    }


def average_monthly(conn, today, category, months):
    months = _choice(months, (3, 6, 12), "how many months")
    category_id, name = _category(conn, category)
    # Full months only: the current month is still in progress.
    end = today.replace(day=1) - timedelta(days=1)
    start = end.replace(day=1)
    for _ in range(months - 1):
        start = (start - timedelta(days=1)).replace(day=1)
    rows = monthly_totals(conn, start.isoformat(), end.isoformat(), category_id=category_id)

    def spent(m):
        return m["spent_cents"]

    total = sum(spent(m) for m in rows)
    on = "" if category_id is None else f" on {name}"
    span = f"{rows[0]['label']} to {rows[-1]['label']}"
    if total == 0:
        text = f"You didn't spend anything{on} in the last {months} full months ({span})."
    else:
        text = f"Over the last {months} full months ({span}), you spent an average of {_money(round(total / months))} a month{on}."
    return {
        "text": text,
        "table": {"headers": ["Month", "Spent"], "rows": [[m["label"], _money(spent(m))] for m in rows]},
        "link": _link(Filter(start=start.isoformat(), end=end.isoformat()), category_id=category_id),
    }


def saved(conn, today, period):
    flt, phrase, _ = resolve_period(period, today)
    summary = list_transactions(conn, flt, limit=0)
    income, spent = summary["income_cents"], summary["spent_cents"]
    difference = income - spent
    lead = phrase[:1].upper() + phrase[1:]  # not .capitalize(): it lowercases "October"
    if income == 0 and spent == 0:
        text = f"There's no income or spending {phrase}."
    elif difference >= 0:
        text = f"Yes. {lead} money in was {_money(income)} and you spent {_money(spent)}, so you saved {_money(difference)}."
    else:
        text = f"No. {lead} money in was {_money(income)} and you spent {_money(spent)}, so you spent {_money(-difference)} more than came in."
    return {
        "text": text,
        "table": {"headers": ["", "Amount"], "rows": [["Money in", _money(income)], ["Spent", _money(spent)], ["Difference", _money(difference)]]},
        "link": _link(flt),
    }


def over_budget(conn, today):
    status = budgets.budget_status(conn, today)
    entries = ([status["overall"]] if status["overall"] else []) + status["categories"]
    if not entries:
        return {"text": "You haven't set any budgets yet. You can add them in Settings."}
    labels = {"ok": "On track", "pace": "Ahead of pace", "warn": "Near limit", "over": "Over budget"}
    if status["alerts"]:
        text = " ".join(status["alerts"])
    else:
        text = f"You're within all your budgets for {status['label']} so far."
    return {
        "text": text,
        "table": {
            "headers": ["Budget", "Spent", "Limit", "Status"],
            "rows": [[e["name"], _money(e["spent_cents"]), _money(e["limit_cents"]), labels[e["state"]]] for e in entries],
        },
        "link": {"start": status["start"], "end": status["end"]},
    }


# Templates: strings are text, dicts are slots.
QUESTIONS = [
    ("spend_on", spend_on, ["How much did ", {"slot": "person"}, " spend on ", {"slot": "category"}, " ", {"slot": "period"}, "?"]),
    ("spend_at", spend_at, ["How much did I spend at ", {"slot": "merchant"}, " ", {"slot": "period"}, "?"]),
    ("where_money_went", where_money_went, ["Where did my money go ", {"slot": "period"}, "?"]),
    ("top_merchants", top_merchants, ["What were my top ", {"slot": "count"}, " merchants ", {"slot": "period"}, "?"]),
    ("biggest", biggest, ["What were my ", {"slot": "count"}, " biggest purchases ", {"slot": "period"}, "?"]),
    ("compare", compare, ["Compare spending on ", {"slot": "category"}, ": ", {"slot": "period_a", "kind": "noun"}, " vs ", {"slot": "period_b", "kind": "noun"}]),
    ("average_monthly", average_monthly, ["What's my average monthly spending on ", {"slot": "category"}, " over the last ", {"slot": "months"}, " full months?"]),
    ("saved", saved, ["Did I save money ", {"slot": "period"}, "?"]),
    ("over_budget", over_budget, ["Am I over budget this month?"]),
]
_HANDLERS = {key: handler for key, handler, _ in QUESTIONS}

DEFAULTS = {
    "person": EVERYONE,
    "category": EVERYTHING,
    "period": "this-month",
    "period_a": "last-month",
    "period_b": "this-month",
    "count": "5",
    "months": "3",
    "merchant": "",
}


def question_list(conn, today: date):
    """Question templates plus the options for each kind of slot."""
    return {
        "questions": [{"type": key, "parts": parts} for key, _, parts in QUESTIONS],
        "options": {
            "person": [{"value": EVERYONE, "label": "everyone"}, {"value": HOUSEHOLD, "label": "the household (shared)"}]
            + [{"value": str(row["id"]), "label": row["name"]} for row in conn.execute("SELECT id, name FROM people ORDER BY sort_order, id")],
            "category": [{"value": EVERYTHING, "label": "everything"}]
            + [{"value": str(row["id"]), "label": row["name"]} for row in _spending_categories(conn)],
            "period": period_options(conn, today),
            "count": [{"value": str(n), "label": str(n)} for n in (3, 5, 10)],
            "months": [{"value": str(n), "label": str(n)} for n in (3, 6, 12)],
        },
        "defaults": DEFAULTS,
    }


def _question_text(conn, today, parts, params):
    options = question_list(conn, today)["options"]
    words = []
    for part in parts:
        if isinstance(part, str):
            words.append(part)
            continue
        slot, value = part["slot"], params.get(part["slot"])
        if slot == "merchant":
            words.append(f"“{' '.join(str(value or '').split())}”")
            continue
        kind = "period" if slot.startswith("period") else slot
        match = next((o for o in options[kind] if o["value"] == str(value)), None)
        label = (match.get(part.get("kind", "phrase")) or match.get("label")) if match else str(value)
        words.append(label)
    return "".join(words)


def answer(conn, question_type: str, params: dict, today: date):
    if question_type not in _HANDLERS:
        raise UserError("That isn't a question I know how to answer.")
    if not isinstance(params, dict):
        raise UserError("'params' must be an object.")
    _, handler, parts = next(q for q in QUESTIONS if q[0] == question_type)
    slots = [p["slot"] for p in parts if isinstance(p, dict)]
    values = {slot: params.get(slot, DEFAULTS[slot]) for slot in slots}
    result = handler(conn, today, **values)
    return {
        "question": _question_text(conn, today, parts, values),
        "table": None,
        "bars": None,
        "link": None,
        **result,
    }
