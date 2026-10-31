"""Amazon orders: what an Amazon charge bought, and splitting it by item.

Amazon's card charges just say "AMAZON MKTPL*…". Its order history export
("Request your data" → Your Orders → Order History.csv) lists every item. A
catch-up import stores the items and matches them to charges:

- Amazon charges your card per shipment, so items are grouped by order and
  ship date. A group matches a charge of exactly its total (each item's Total
  Amount, which includes tax and discounts) posted from 2 days before to 3 days
  after it shipped.
- Failing that, a whole order matches a charge of its total up to 10 days
  after it was ordered (some multi-shipment orders are charged once).
- Only money-out charges whose description mentions Amazon or AMZN are used.
- One charge per group, closest date first; a pair is only made when it's
  clearly the closest for both. Anything left with several equally close
  candidates is a tie for you to pick.

Charges still Uncategorized and unsplit are listed for review: each item gets
a guessed category (the same product before, then the item's Department) and
Apply sets one category or splits the charge by category.
"""

import csv
import io
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

from .db import transaction
from .errors import NotFound, UserError
from .ledger import _require_category, _require_person, set_category, set_person, split_transaction

REQUIRED = ("Order ID", "Order Date", "ASIN", "Product Name", "Total Amount")
SHIP_BEFORE, SHIP_AFTER = 2, 3  # days a charge can post before / after a shipment
ORDER_AFTER = 10  # days after the order date for a whole-order charge
_AMAZON = "(upper(t.description) LIKE '%AMAZON%' OR upper(t.description) LIKE '%AMZN%')"


# ---- Reading the file -------------------------------------------------------------------

def is_order_history(headers) -> bool:
    return all(column in headers for column in REQUIRED)


def _cents(value):
    # Amazon writes some amounts as "'-1.12'".
    text = (value or "").strip().strip("'").replace(",", "").replace("$", "")
    if not text or text.lower() == "not available":
        return 0
    try:
        return int((Decimal(text) * 100).to_integral_value())
    except InvalidOperation:
        raise UserError(f"Can't read the amount “{value}” in the Amazon file.") from None


def _day(value):
    """'2026-10-19T04:51:13Z' -> '2026-10-19', or None for 'Not Available'."""
    match = re.match(r"\d{4}-\d{2}-\d{2}", (value or "").strip())
    return match.group(0) if match else None


def read_order_history(text):
    """Item rows from Amazon's Order History.csv, leaving out cancelled ones."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames or not is_order_history([h.strip() for h in reader.fieldnames]):
        raise UserError(
            "This doesn't look like Amazon's Order History.csv (it needs the columns "
            + ", ".join(REQUIRED) + ")."
        )
    items = []
    for row in reader:
        row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
        if row.get("Order Status", "").lower() == "cancelled" or not row["Order ID"]:
            continue
        order_date = _day(row["Order Date"])
        if not order_date:
            continue
        quantity = row.get("Original Quantity", "1")
        items.append({
            "order_id": row["Order ID"],
            "order_date": order_date,
            "ship_date": _day(row.get("Ship Date")),
            "asin": row["ASIN"],
            "product_name": " ".join(row["Product Name"].split()) or "(no name)",
            "department": None if row.get("Department", "") in ("", "Not Available") else row["Department"],
            "quantity": int(quantity) if quantity.isdigit() else 1,
            "total_cents": _cents(row["Total Amount"]),
        })
    return items


def import_orders(conn, text):
    """Store the file's items (replacing those of any order already stored),
    then match charges. Returns a summary for the page."""
    items = read_order_history(text)
    orders = {item["order_id"] for item in items}
    with transaction(conn):
        known = {r[0] for r in conn.execute("SELECT DISTINCT order_id FROM amazon_items")}
        for order_id in orders & known:
            conn.execute("DELETE FROM amazon_items WHERE order_id = ?", (order_id,))
        conn.executemany(
            """INSERT INTO amazon_items (order_id, order_date, ship_date, asin, product_name, department, quantity, total_cents)
               VALUES (:order_id, :order_date, :ship_date, :asin, :product_name, :department, :quantity, :total_cents)""",
            items,
        )
        matched_now = match(conn)
    charges = _amazon_charges(conn)
    matched = {r[0] for r in conn.execute("SELECT transaction_id FROM amazon_matches")}
    in_range = [c for c in charges if items and c["date"] >= min(i["order_date"] for i in items)]
    review_list = review(conn)
    tied = {t["charge"]["id"] for t in review_list["ties"]}
    return {
        "orders": len(orders),
        "new_orders": len(orders - known),
        "items": len(items),
        "matched": matched_now,
        "unmatched_charges": sum(1 for c in in_range if c["id"] not in matched and c["id"] not in tied),
        "to_review": len(review_list["charges"]) + len(review_list["ties"]),
    }


# ---- Matching -------------------------------------------------------------------------

def _amazon_charges(conn):
    return [
        dict(r)
        for r in conn.execute(
            f"""SELECT t.id, t.date, -t.amount_cents AS cents FROM transactions t
                WHERE t.amount_cents < 0 AND {_AMAZON} ORDER BY t.date, t.id"""
        )
    ]


def _groups(conn):
    """Every chargeable group: {(order_id, ship_date or None): {"date", "cents"}}.
    ship_date None is the whole order."""
    groups = {}
    orders = defaultdict(lambda: {"cents": 0, "date": None})
    for row in conn.execute(
        """SELECT order_id, ship_date, min(order_date) AS order_date, sum(total_cents) AS cents
           FROM amazon_items GROUP BY order_id, ship_date"""
    ):
        if row["ship_date"]:
            groups[(row["order_id"], row["ship_date"])] = {"date": row["ship_date"], "cents": row["cents"]}
        order = orders[row["order_id"]]
        order["cents"] += row["cents"]
        order["date"] = min(filter(None, [order["date"], row["order_date"]]))
    for order_id, order in orders.items():
        groups[(order_id, None)] = order
    return groups


def _days(a, b):
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def _edges(charges, groups):
    """(charge id, group key, rank): every possible pairing. Lower ranks are
    better: shipments before whole orders, then fewer days apart."""
    by_cents = defaultdict(list)
    for key, group in groups.items():
        by_cents[group["cents"]].append(key)
    edges = []
    for charge in charges:
        for key in by_cents.get(charge["cents"], []):
            gap = _days(charge["date"], groups[key]["date"])
            if key[1] is not None and -SHIP_BEFORE <= gap <= SHIP_AFTER:
                edges.append((charge["id"], key, (0, abs(gap))))
            elif key[1] is None and 0 <= gap <= ORDER_AFTER:
                edges.append((charge["id"], key, (1, gap)))
    return edges


def _conflicts(key, taken_orders, taken_groups):
    """A group can't be used if it, or (for a whole order) any of its
    shipments, or (for a shipment) its whole order, is already matched."""
    order_id, ship_date = key
    if key in taken_groups:
        return True
    if ship_date is None:
        return order_id in taken_orders
    return (order_id, None) in taken_groups


def _taken(conn):
    rows = conn.execute("SELECT transaction_id, order_id, ship_date FROM amazon_matches").fetchall()
    return {r["transaction_id"] for r in rows}, {(r["order_id"], r["ship_date"]) for r in rows}, {r["order_id"] for r in rows}


def _open_edges(conn):
    """Pairings still possible: unmatched charges with free groups."""
    matched, taken_groups, taken_orders = _taken(conn)
    charges = [c for c in _amazon_charges(conn) if c["id"] not in matched]
    groups = _groups(conn)
    return [e for e in _edges(charges, groups) if not _conflicts(e[1], taken_orders, taken_groups)], groups


def match(conn):
    """Pair charges with orders where it's clear. Returns how many were paired.
    Call inside a transaction."""
    # A newer file can change an order (say, a shipment that hadn't gone out yet).
    # Drop matches that no longer add up, unless you picked them.
    groups = _groups(conn)
    for m in conn.execute(
        """SELECT m.transaction_id, m.order_id, m.ship_date, -t.amount_cents AS cents
           FROM amazon_matches m JOIN transactions t ON t.id = m.transaction_id WHERE m.chosen = 0"""
    ).fetchall():
        group = groups.get((m["order_id"], m["ship_date"]))
        if group is None or group["cents"] != m["cents"]:
            conn.execute("DELETE FROM amazon_matches WHERE transaction_id = ?", (m["transaction_id"],))

    paired = 0
    while True:
        edges, _ = _open_edges(conn)
        best_charge, best_group = {}, {}
        for charge_id, key, rank in edges:
            best_charge.setdefault(charge_id, []).append((rank, key))
            best_group.setdefault(key, []).append((rank, charge_id))
        made = False
        for charge_id, options in best_charge.items():
            top = min(rank for rank, _ in options)
            keys = [key for rank, key in options if rank == top]
            if len(keys) != 1:
                continue  # several equally close orders: a tie for you
            key = keys[0]
            rivals = [c for rank, c in best_group[key] if rank <= top and c != charge_id]
            if rivals:
                continue  # another charge is as close to this order
            conn.execute(
                "INSERT INTO amazon_matches (transaction_id, order_id, ship_date) VALUES (?, ?, ?)",
                (charge_id, key[0], key[1]),
            )
            paired += 1
            made = True
            break  # matching one can free or block others: look again
        if not made:
            return paired


# ---- Reading matches --------------------------------------------------------------------

def _items(conn, order_id, ship_date):
    sql = "SELECT id, asin, product_name, department, quantity, total_cents, ship_date FROM amazon_items WHERE order_id = ?"
    params = [order_id]
    if ship_date is not None:
        sql += " AND ship_date = ?"
        params.append(ship_date)
    return [dict(r) for r in conn.execute(sql + " ORDER BY id", params)]


def _guesses(conn, items):
    """Add a guessed category_id (and person_id) to each item."""
    for item in items:
        product = conn.execute("SELECT category_id, person_id FROM amazon_products WHERE asin = ?", (item["asin"],)).fetchone()
        department = None
        if not product and item["department"]:
            department = conn.execute(
                "SELECT category_id FROM amazon_departments WHERE department = ?", (item["department"],)
            ).fetchone()
        item["category_id"] = product["category_id"] if product else department["category_id"] if department else None
        item["person_id"] = product["person_id"] if product else None
        item["guess"] = "product" if product else "department" if department else None
    return items


def _group_info(conn, order_id, ship_date):
    items = _items(conn, order_id, ship_date)
    return {
        "order_id": order_id,
        "ship_date": ship_date,
        "order_date": conn.execute("SELECT min(order_date) FROM amazon_items WHERE order_id = ?", (order_id,)).fetchone()[0],
        "items": items,
    }


def _needs_review(conn, transaction_id):
    row = conn.execute(
        """SELECT c.kind = 'uncategorized'
                  AND NOT EXISTS (SELECT 1 FROM transaction_parts p WHERE p.transaction_id = t.id)
           FROM transactions t JOIN categories c ON c.id = t.category_id WHERE t.id = ?""",
        (transaction_id,),
    ).fetchone()
    return bool(row and row[0])


def _charge(conn, transaction_id):
    row = conn.execute(
        """SELECT t.id, t.date, t.description, t.amount_cents, a.name AS account_name
           FROM transactions t JOIN accounts a ON a.id = t.account_id WHERE t.id = ?""",
        (transaction_id,),
    ).fetchone()
    return dict(row) if row else None


def _alternatives(conn, transaction_id, groups=None):
    """Other free orders this charge could be for, closest first."""
    charge = conn.execute("SELECT id, date, -amount_cents AS cents FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    _, taken_groups, taken_orders = _taken(conn)
    groups = groups or _groups(conn)
    options = [
        (rank, key) for _, key, rank in _edges([dict(charge)], groups)
        if not _conflicts(key, taken_orders, taken_groups)
    ]
    return [_group_info(conn, *key) for _, key in sorted(options)]


def review(conn):
    """Amazon charges waiting for you: matched ones still Uncategorized and
    unsplit (with guessed categories), and ties to pick an order for."""
    charges = []
    for row in conn.execute(
        """SELECT m.transaction_id, m.order_id, m.ship_date FROM amazon_matches m
           JOIN transactions t ON t.id = m.transaction_id ORDER BY t.date DESC, t.id DESC"""
    ):
        if _needs_review(conn, row["transaction_id"]):
            group = _group_info(conn, row["order_id"], row["ship_date"])
            _guesses(conn, group["items"])
            charges.append({
                "charge": _charge(conn, row["transaction_id"]),
                "order": group,
                "alternatives": len(_alternatives(conn, row["transaction_id"])),
            })
    edges, groups = _open_edges(conn)
    tied = defaultdict(list)
    for charge_id, key, rank in sorted(edges, key=lambda e: e[2]):
        tied[charge_id].append(key)
    ties = [
        {"charge": _charge(conn, charge_id), "options": [_group_info(conn, *key) for key in keys]}
        for charge_id, keys in tied.items()
        if _needs_review(conn, charge_id)
    ]
    ties.sort(key=lambda t: t["charge"]["date"], reverse=True)
    return {"charges": charges, "ties": ties}


def for_transactions(conn, rows):
    """Add "amazon" to transaction rows: None or {"items", "review"}."""
    ids = {r["id"] for r in rows}
    matches = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for m in conn.execute(f"SELECT * FROM amazon_matches WHERE transaction_id IN ({marks})", list(ids)):
            matches[m["transaction_id"]] = m
    for row in rows:
        m = matches.get(row["id"])
        row["amazon"] = None if m is None else {
            "items": _items(conn, m["order_id"], m["ship_date"]),
            "review": _needs_review(conn, row["id"]),
        }
    return rows


def details(conn, transaction_id):
    """One charge's match for its popup: items with guesses, and other orders."""
    m = conn.execute("SELECT * FROM amazon_matches WHERE transaction_id = ?", (transaction_id,)).fetchone()
    if m is None:
        raise NotFound("That charge isn't matched to an Amazon order.")
    group = _group_info(conn, m["order_id"], m["ship_date"])
    _guesses(conn, group["items"])
    return {
        "charge": _charge(conn, transaction_id),
        "order": group,
        "review": _needs_review(conn, transaction_id),
        "alternatives": _alternatives(conn, transaction_id),
    }


# ---- Changing matches -------------------------------------------------------------------

def choose(conn, transaction_id, order_id, ship_date=None):
    """You picked which order (or shipment) a charge was for."""
    with transaction(conn):
        charge = conn.execute(
            f"SELECT t.id FROM transactions t WHERE t.id = ? AND t.amount_cents < 0 AND {_AMAZON}", (transaction_id,)
        ).fetchone()
        if charge is None:
            raise NotFound("That isn't an Amazon charge.")
        conn.execute("DELETE FROM amazon_matches WHERE transaction_id = ?", (transaction_id,))
        options = {(g["order_id"], g["ship_date"]) for g in _alternatives(conn, transaction_id)}
        if (order_id, ship_date) not in options:
            raise UserError("That order doesn't match this charge's amount and date, or it's already matched to another charge.")
        conn.execute(
            "INSERT INTO amazon_matches (transaction_id, order_id, ship_date, chosen) VALUES (?, ?, ?, 1)",
            (transaction_id, order_id, ship_date),
        )
    return details(conn, transaction_id)


def apply(conn, transaction_id, choices):
    """Sort a matched charge by its items. choices: [{"item_id", "category_id",
    "person_id"}] for every item. Same category and person throughout → one
    category; otherwise a split with one part per category and person."""
    m = conn.execute("SELECT * FROM amazon_matches WHERE transaction_id = ?", (transaction_id,)).fetchone()
    if m is None:
        raise NotFound("That charge isn't matched to an Amazon order.")
    items = {i["id"]: i for i in _items(conn, m["order_id"], m["ship_date"])}
    if not isinstance(choices, list) or {c.get("item_id") for c in choices if isinstance(c, dict)} != set(items) or len(choices) != len(items):
        raise UserError("Pick a category for every item.")
    parts = defaultdict(int)
    for choice in choices:
        try:
            category_id = int(choice.get("category_id"))
        except (TypeError, ValueError):
            raise UserError(f"Pick a category for “{items[choice['item_id']]['product_name'][:60]}”.") from None
        _require_category(conn, category_id)
        person_id = choice.get("person_id")
        if person_id is not None:
            try:
                person_id = int(person_id)
            except (TypeError, ValueError):
                raise UserError("That person isn't valid.") from None
            _require_person(conn, person_id)
        choice["category_id"], choice["person_id"] = category_id, person_id
        parts[(category_id, person_id)] -= items[choice["item_id"]]["total_cents"]
    # Free items ($0.00) can't be a part of their own.
    parts = {key: cents for key, cents in parts.items() if cents} or {next(iter(parts)): 0}

    if len(parts) == 1:
        (category_id, person_id), _ = next(iter(parts.items()))
        set_category(conn, transaction_id, category_id)
        if person_id is not None:
            set_person(conn, transaction_id, person_id)
    else:
        split_transaction(conn, transaction_id, [
            {"amount_cents": cents, "category_id": category_id, "person_id": person_id}
            for (category_id, person_id), cents in parts.items()
        ])
    with transaction(conn):
        for choice in choices:
            conn.execute(
                """INSERT INTO amazon_products (asin, category_id, person_id) VALUES (?, ?, ?)
                   ON CONFLICT (asin) DO UPDATE SET category_id = excluded.category_id, person_id = excluded.person_id""",
                (items[choice["item_id"]]["asin"], choice["category_id"], choice["person_id"]),
            )
    return {"id": transaction_id, "split": len(parts) > 1, "parts": len(parts)}


# ---- Department mapping ------------------------------------------------------------------

def departments(conn):
    """Departments seen in your items, with how many items and the mapped category."""
    rows = conn.execute(
        """SELECT i.department, count(*) AS items, d.category_id
           FROM amazon_items i LEFT JOIN amazon_departments d ON d.department = i.department
           WHERE i.department IS NOT NULL GROUP BY i.department COLLATE NOCASE ORDER BY items DESC, i.department"""
    )
    return [dict(r) for r in rows]


def set_department(conn, department, category_id):
    department = " ".join(str(department or "").split())
    if not department:
        raise UserError("Which department?")
    with transaction(conn):
        if category_id is None:
            conn.execute("DELETE FROM amazon_departments WHERE department = ?", (department,))
        else:
            _require_category(conn, int(category_id))
            conn.execute(
                """INSERT INTO amazon_departments (department, category_id) VALUES (?, ?)
                   ON CONFLICT (department) DO UPDATE SET category_id = excluded.category_id""",
                (department, int(category_id)),
            )
    return {"department": department, "category_id": category_id}


def status(conn):
    """What's stored, for the Import page."""
    row = conn.execute("SELECT count(DISTINCT order_id) AS orders, count(*) AS items, max(order_date) AS latest FROM amazon_items").fetchone()
    return dict(row)
