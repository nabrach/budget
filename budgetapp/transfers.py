"""Linking the two sides of a transfer between your own accounts.

Paying a card from checking shows up twice: money out of Checking and money
into the card. Linking pairs them, so both are Transfer (neither spending nor
income) and each shows where the money went.

A suggestion pairs two transactions that
- are in different accounts, not split and not already linked,
- have the same amount going opposite ways,
- posted at most MATCH_DAYS apart,
- and at least one of them already looks like a transfer (it's in Transfer).
When a transaction has several candidates the closest date wins; equally close
ones are all suggested (a "tie") and you pick. Pairs you turned down aren't
suggested again.

You confirm suggestions. With the auto_link_transfers setting on, clear ones
(no tie, and no side whose category you set by hand) link on import instead.
Suggestions aren't stored; they're worked out from the data each time.
"""

from .categorize import Categorizer, normalize, suggest_pattern
from .db import transaction
from .errors import NotFound, UserError
from .queries import _LINKED, _SPLIT, MATCH_DAYS, Filter, list_transactions

# How far apart a pair you link yourself can be.
MANUAL_DAYS = 30
AUTO_LINK_KEY = "auto_link_transfers"

_DETAILS = """SELECT t.id, t.account_id, a.name AS account_name, t.date, t.description,
                     t.amount_cents, t.category_source, c.kind AS category_kind,
                     coalesce(g.name || ' › ', '') || c.name AS category_name,
                     {split} AS split, {linked} AS linked
              FROM transactions t JOIN accounts a ON a.id = t.account_id
              JOIN categories c ON c.id = t.category_id
              LEFT JOIN categories g ON g.id = c.parent_id""".format(
    split=_SPLIT.format(id="t.id"), linked=_LINKED.format(id="t.id")
)


def _details(conn, ids):
    ids = list(ids)
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {row["id"]: dict(row) for row in conn.execute(f"{_DETAILS} WHERE t.id IN ({marks})", ids)}


def _side(row):
    """What the page shows for one side of a pair."""
    return {key: row[key] for key in ("id", "account_name", "date", "description", "amount_cents", "category_name")}


def _hand_set(row):
    """Categorized by hand as something other than a transfer."""
    return row["category_source"] == "manual" and row["category_kind"] != "transfer"


# ---- Suggestions ---------------------------------------------------------------------

def _candidate_pairs(conn):
    """(out_id, in_id, days apart) for every pair that could be a transfer."""
    rows = conn.execute(
        f"""SELECT o.id AS out_id, i.id AS in_id,
                   CAST(round(abs(julianday(o.date) - julianday(i.date))) AS INTEGER) AS days
            FROM transactions o
            JOIN transactions i ON i.amount_cents = -o.amount_cents AND i.account_id != o.account_id
            JOIN categories co ON co.id = o.category_id
            JOIN categories ci ON ci.id = i.category_id
            WHERE o.amount_cents < 0
              AND abs(julianday(o.date) - julianday(i.date)) <= ?
              AND (co.kind = 'transfer' OR ci.kind = 'transfer')
              AND NOT {_SPLIT.format(id="o.id")} AND NOT {_SPLIT.format(id="i.id")}
              AND NOT {_LINKED.format(id="o.id")} AND NOT {_LINKED.format(id="i.id")}
              AND NOT EXISTS (SELECT 1 FROM transfer_links r
                              WHERE r.out_id = o.id AND r.in_id = i.id AND r.status = 'rejected')""",
        (MATCH_DAYS,),
    )
    return [(r["out_id"], r["in_id"], r["days"]) for r in rows]


def suggestions(conn):
    """Possible transfers, newest first: [{"out", "in", "days", "tie", "clear"}].

    A pair is kept when it's the closest candidate for both of its sides. "tie"
    means a side has another, equally close candidate (shown too, so you pick).
    "clear" pairs could link without asking: no tie, nothing set by hand.
    """
    pairs = _candidate_pairs(conn)
    closest = {}
    for out_id, in_id, days in pairs:
        for side in (out_id, in_id):
            closest[side] = min(closest.get(side, days), days)
    kept = [p for p in pairs if p[2] == closest[p[0]] == closest[p[1]]]
    uses = {}
    for out_id, in_id, _ in kept:
        for side in (out_id, in_id):
            uses[side] = uses.get(side, 0) + 1
    rows = _details(conn, {side for p in kept for side in p[:2]})
    result = []
    for out_id, in_id, days in kept:
        out, into = rows[out_id], rows[in_id]
        tie = uses[out_id] > 1 or uses[in_id] > 1
        result.append({
            "out": _side(out),
            "in": _side(into),
            "days": days,
            "tie": tie,
            "clear": not tie and not _hand_set(out) and not _hand_set(into),
        })
    result.sort(key=lambda s: (max(s["out"]["date"], s["in"]["date"]), s["out"]["id"]), reverse=True)
    return result


def suggestions_for(conn, transaction_ids):
    """How many suggestions involve any of these transactions (e.g. one import's)."""
    ids = set(transaction_ids)
    return sum(1 for s in suggestions(conn) if s["out"]["id"] in ids or s["in"]["id"] in ids)


# ---- Linking ----------------------------------------------------------------------------

def _pair(conn, first_id, second_id, max_days):
    """(out row, in row) after checking the two can be linked."""
    if first_id == second_id:
        raise UserError("Pick two different transactions.")
    rows = _details(conn, {first_id, second_id})
    if len(rows) != 2:
        raise NotFound("That transaction doesn't exist.")
    a, b = rows[first_id], rows[second_id]
    if a["account_id"] == b["account_id"]:
        raise UserError("Both are in the same account. A transfer moves money between two of your accounts.")
    if a["amount_cents"] != -b["amount_cents"]:
        raise UserError("The amounts don't match: a transfer is the same amount out of one account and into another.")
    for row in (a, b):
        if row["split"]:
            raise UserError(f"“{row['description']}” is split. Unsplit it first to link it as a transfer.")
        if row["linked"]:
            raise UserError(f"“{row['description']}” is already linked to another transfer. Unlink it first.")
    days = conn.execute("SELECT abs(julianday(?) - julianday(?))", (a["date"], b["date"])).fetchone()[0]
    if days > max_days:
        raise UserError(f"These are {round(days)} days apart. Transfers can be at most {max_days} days apart.")
    return (a, b) if a["amount_cents"] < 0 else (b, a)


def _transfer_category(conn):
    return conn.execute("SELECT id FROM categories WHERE kind = 'transfer'").fetchone()[0]


def _link(conn, out, into, origin):
    """Link two checked rows. Call inside a transaction."""
    prev = {
        side["id"]: conn.execute("SELECT category_id, category_source FROM transactions WHERE id = ?", (side["id"],)).fetchone()
        for side in (out, into)
    }
    conn.execute(
        """INSERT INTO transfer_links (out_id, in_id, status, origin, out_prev_category_id, out_prev_source,
                                       in_prev_category_id, in_prev_source)
           VALUES (?, ?, 'linked', ?, ?, ?, ?, ?)
           ON CONFLICT (out_id, in_id) DO UPDATE SET
               status = 'linked', origin = excluded.origin, decided_at = datetime('now'),
               out_prev_category_id = excluded.out_prev_category_id, out_prev_source = excluded.out_prev_source,
               in_prev_category_id = excluded.in_prev_category_id, in_prev_source = excluded.in_prev_source""",
        (
            out["id"], into["id"], origin,
            prev[out["id"]]["category_id"], prev[out["id"]]["category_source"],
            prev[into["id"]]["category_id"], prev[into["id"]]["category_source"],
        ),
    )
    # "manual" so rules leave linked rows alone; the link says why.
    conn.execute(
        "UPDATE transactions SET category_id = ?, category_source = 'manual' WHERE id IN (?, ?)",
        (_transfer_category(conn), out["id"], into["id"]),
    )


def link(conn, first_id, second_id, origin="suggestion"):
    """Link two transactions as the two sides of one transfer.

    origin "suggestion" (you confirmed one) allows MATCH_DAYS apart; "manual"
    (you picked the other side yourself) allows MANUAL_DAYS.
    """
    if origin not in ("suggestion", "manual"):
        raise UserError("origin must be 'suggestion' or 'manual'.")
    with transaction(conn):
        out, into = _pair(conn, first_id, second_id, MATCH_DAYS if origin == "suggestion" else MANUAL_DAYS)
        _link(conn, out, into, origin)
    return link_for(conn, first_id)


def reject(conn, first_id, second_id):
    """Mark a suggested pair as not a match, so it isn't suggested again."""
    with transaction(conn):
        rows = _details(conn, {first_id, second_id})
        if first_id not in rows or second_id not in rows:
            raise NotFound("That transaction doesn't exist.")
        a, b = rows[first_id], rows[second_id]
        out, into = (a, b) if a["amount_cents"] < 0 else (b, a)
        existing = conn.execute(
            "SELECT status FROM transfer_links WHERE out_id = ? AND in_id = ?", (out["id"], into["id"])
        ).fetchone()
        if existing and existing["status"] == "linked":
            raise UserError("These are linked. Unlink them instead.")
        conn.execute(
            """INSERT INTO transfer_links (out_id, in_id, status, origin) VALUES (?, ?, 'rejected', 'suggestion')
               ON CONFLICT (out_id, in_id) DO NOTHING""",
            (out["id"], into["id"]),
        )
    return {"out_id": out["id"], "in_id": into["id"], "status": "rejected"}


def _restore(conn, transaction_id, prev_category_id, prev_source, categorizer):
    """Put back a side's category from before it was linked. Call inside a transaction.

    A category you'd set by hand comes back as it was; otherwise rules decide
    again (they may have changed since).
    """
    if prev_source == "manual" and prev_category_id is not None:
        conn.execute(
            "UPDATE transactions SET category_id = ?, category_source = 'manual' WHERE id = ?",
            (prev_category_id, transaction_id),
        )
        return
    row = conn.execute(
        "SELECT description, bank_category, bank_type FROM transactions WHERE id = ?", (transaction_id,)
    ).fetchone()
    category_id, source = categorizer.categorize(row["description"], row["bank_category"], row["bank_type"])
    conn.execute(
        "UPDATE transactions SET category_id = ?, category_source = ? WHERE id = ?", (category_id, source, transaction_id)
    )


def _linked_row(conn, transaction_id):
    return conn.execute(
        "SELECT * FROM transfer_links WHERE status = 'linked' AND ? IN (out_id, in_id)", (transaction_id,)
    ).fetchone()


def unlink(conn, transaction_id):
    """Unlink the transfer this transaction is part of. Both sides get their
    earlier categories back, and the pair counts as not a match."""
    with transaction(conn):
        row = _linked_row(conn, transaction_id)
        if row is None:
            raise NotFound("That transaction isn't linked to a transfer.")
        categorizer = Categorizer(conn)
        _restore(conn, row["out_id"], row["out_prev_category_id"], row["out_prev_source"], categorizer)
        _restore(conn, row["in_id"], row["in_prev_category_id"], row["in_prev_source"], categorizer)
        conn.execute("UPDATE transfer_links SET status = 'rejected', decided_at = datetime('now') WHERE id = ?", (row["id"],))
    return {"out_id": row["out_id"], "in_id": row["in_id"], "status": "rejected"}


def release(conn, transaction_ids):
    """Before deleting these transactions: unlink their transfers, so the other
    sides (if they stay) get their earlier categories back. Call inside a transaction."""
    doomed = set(transaction_ids)
    if not doomed:
        return
    marks = ",".join("?" * len(doomed))
    links = conn.execute(
        f"""SELECT * FROM transfer_links WHERE status = 'linked'
            AND (out_id IN ({marks}) OR in_id IN ({marks}))""",
        (*doomed, *doomed),
    ).fetchall()
    categorizer = Categorizer(conn)
    for row in links:
        for side in ("out", "in"):
            if row[f"{side}_id"] not in doomed:
                _restore(conn, row[f"{side}_id"], row[f"{side}_prev_category_id"], row[f"{side}_prev_source"], categorizer)
        conn.execute("DELETE FROM transfer_links WHERE id = ?", (row["id"],))


def auto_link(conn):
    """With the setting on, link every clear suggestion. Returns how many."""
    if not auto_link_enabled(conn):
        return 0
    linked = 0
    with transaction(conn):
        # Linking one pair can make another the closest for its sides, so repeat.
        while True:
            clear = [s for s in suggestions(conn) if s["clear"]]
            if not clear:
                break
            for s in clear:
                rows = _details(conn, {s["out"]["id"], s["in"]["id"]})
                _link(conn, rows[s["out"]["id"]], rows[s["in"]["id"]], "auto")
                linked += 1
    return linked


# ---- Reading ----------------------------------------------------------------------------

def link_for(conn, transaction_id):
    """The linked transfer this transaction is part of, or None:
    {"link_id", "other": {...the other side}}."""
    row = _linked_row(conn, transaction_id)
    if row is None:
        return None
    other_id = row["in_id"] if row["out_id"] == transaction_id else row["out_id"]
    return {"link_id": row["id"], "other": _side(_details(conn, [other_id])[other_id])}


def annotate(conn, rows):
    """Add "transfer" to each transaction row: None, {"status": "linked", "other"}
    or {"status": "suggested", "options": [other sides]}."""
    ids = {r["id"] for r in rows if not r.get("part_id")}
    linked = {}
    if ids:
        marks = ",".join("?" * len(ids))
        pairs = conn.execute(
            f"""SELECT id, out_id, in_id FROM transfer_links WHERE status = 'linked'
                AND (out_id IN ({marks}) OR in_id IN ({marks}))""",
            (*ids, *ids),
        ).fetchall()
        others = _details(conn, {side for p in pairs for side in (p["out_id"], p["in_id"])})
        for p in pairs:
            if p["out_id"] in ids:
                linked[p["out_id"]] = {"status": "linked", "link_id": p["id"], "other": _side(others[p["in_id"]])}
            if p["in_id"] in ids:
                linked[p["in_id"]] = {"status": "linked", "link_id": p["id"], "other": _side(others[p["out_id"]])}
    suggested = {}
    for s in suggestions(conn) if ids else []:
        for side, other in (("out", "in"), ("in", "out")):
            if s[side]["id"] in ids:
                suggested.setdefault(s[side]["id"], []).append(s[other])
    for row in rows:
        if row.get("part_id"):
            row["transfer"] = None
        elif row["id"] in linked:
            row["transfer"] = linked[row["id"]]
        elif row["id"] in suggested:
            row["transfer"] = {"status": "suggested", "options": suggested[row["id"]]}
        else:
            row["transfer"] = None
    return rows


def candidates(conn, transaction_id):
    """Possible other sides for linking this transaction yourself: same amount
    the other way, another account, within MANUAL_DAYS, closest first."""
    rows = _details(conn, [transaction_id])
    if transaction_id not in rows:
        raise NotFound("That transaction doesn't exist.")
    row = rows[transaction_id]
    if row["split"]:
        raise UserError("This transaction is split. Unsplit it first to link it as a transfer.")
    if row["linked"]:
        raise UserError("This transaction is already linked to a transfer.")
    found = conn.execute(
        f"""{_DETAILS}
            WHERE t.amount_cents = ? AND t.account_id != ?
              AND abs(julianday(t.date) - julianday(?)) <= ?
              AND NOT {_SPLIT.format(id="t.id")} AND NOT {_LINKED.format(id="t.id")}
            ORDER BY abs(julianday(t.date) - julianday(?)), t.date DESC
            LIMIT 20""",
        (-row["amount_cents"], row["account_id"], row["date"], MANUAL_DAYS, row["date"]),
    )
    return {"transaction": _side(row), "candidates": [_side(r) for r in found]}


def review(conn):
    """Everything waiting on you: suggestions and transfers with no match."""
    unmatched = list_transactions(conn, Filter(no_match=True), limit=200)["rows"]
    for row in unmatched:
        row["suggested_pattern"] = suggest_pattern(row["description"])
    return {"suggestions": suggestions(conn), "unmatched": unmatched}


# ---- No match: dismissing and ignoring --------------------------------------------------

def untrack(conn, transaction_id):
    """"Other side isn't tracked": stop flagging this transfer."""
    with transaction(conn):
        if not conn.execute("SELECT 1 FROM transactions WHERE id = ?", (transaction_id,)).fetchone():
            raise NotFound("That transaction doesn't exist.")
        conn.execute("INSERT OR IGNORE INTO transfer_untracked (transaction_id) VALUES (?)", (transaction_id,))
    return {"id": transaction_id}


def list_ignores(conn):
    return [dict(r) for r in conn.execute("SELECT id, pattern FROM transfer_ignores ORDER BY pattern")]


def add_ignore(conn, pattern):
    """"Don't flag ones like this": transfers matching pattern are never flagged."""
    pattern = normalize(str(pattern or ""))
    if len(pattern) < 2:
        raise UserError("Type at least 2 letters of the description.")
    with transaction(conn):
        conn.execute("INSERT OR IGNORE INTO transfer_ignores (pattern) VALUES (?)", (pattern,))
        row = conn.execute("SELECT id, pattern FROM transfer_ignores WHERE pattern = ?", (pattern,)).fetchone()
    return dict(row)


def delete_ignore(conn, ignore_id):
    with transaction(conn):
        if not conn.execute("DELETE FROM transfer_ignores WHERE id = ?", (ignore_id,)).rowcount:
            raise NotFound("That entry doesn't exist.")


# ---- Settings -------------------------------------------------------------------------

def auto_link_enabled(conn):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (AUTO_LINK_KEY,)).fetchone()
    return bool(row and row["value"] == "1")


def get_settings(conn):
    """The auto-link switch, plus your record on suggestions to help decide."""
    record = conn.execute(
        """SELECT coalesce(sum(status = 'linked'), 0) AS confirmed, count(*) AS decided
           FROM transfer_links WHERE origin = 'suggestion'"""
    ).fetchone()
    return {
        "auto_link": auto_link_enabled(conn),
        "confirmed": record["confirmed"],
        "decided": record["decided"],
        "ignores": list_ignores(conn),
    }


def set_auto_link(conn, enabled):
    if not isinstance(enabled, bool):
        raise UserError("'auto_link' must be true or false.")
    with transaction(conn):
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (AUTO_LINK_KEY, "1" if enabled else "0"),
        )
    return get_settings(conn)


def paid_toward(conn, account_id, start, end):
    """Linked payments into an account between two dates (by the date they
    arrived): [{"from_account", "count", "cents"}], biggest first."""
    clauses, params = ["l.status = 'linked'", "i.account_id = ?"], [account_id]
    if start:
        clauses.append("i.date >= ?")
        params.append(start)
    if end:
        clauses.append("i.date <= ?")
        params.append(end)
    rows = conn.execute(
        f"""SELECT a.name AS from_account, count(*) AS count, sum(i.amount_cents) AS cents
            FROM transfer_links l
            JOIN transactions i ON i.id = l.in_id
            JOIN transactions o ON o.id = l.out_id
            JOIN accounts a ON a.id = o.account_id
            WHERE {' AND '.join(clauses)}
            GROUP BY a.id ORDER BY cents DESC""",
        params,
    )
    return [dict(r) for r in rows]
