"""Local web server: static files plus a small JSON API.

Two guards keep other websites from using the API through your browser:
the Host header must be a local name (blocks DNS rebinding), and every
request that changes data needs an X-Budget header, which a cross-site page
can't send without a CORS preflight this server never approves.
"""

import json
import re
import sys
import threading
import traceback
import webbrowser
from dataclasses import dataclass
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import ask, budgets, db, ledger, queries
from .config import ROOT, Config
from .errors import NotFound, UserError
from .importer import NeedsInput, delete_layout, import_csv, list_layouts, save_layout, undo_import

STATIC_DIR = ROOT / "static"
MAX_BODY_BYTES = 10 * 1024 * 1024
LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "[::1]"}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}


@dataclass
class Request:
    conn: object
    query: dict
    body: bytes
    headers: object

    def json(self) -> dict:
        try:
            data = json.loads(self.body or b"{}")
        except json.JSONDecodeError:
            raise UserError("The request body isn't valid JSON.") from None
        if not isinstance(data, dict):
            raise UserError("The request body must be a JSON object.")
        return data

    def int_query(self, name):
        value = self.query.get(name)
        if value in (None, ""):
            return None
        try:
            return int(value)
        except ValueError:
            raise UserError(f"'{name}' must be a number.") from None


def _int(value, name):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise UserError(f"'{name}' must be a number.") from None


# --- API handlers: each returns (status, payload) ---------------------------

def get_categories(req):
    return 200, ledger.list_categories(req.conn)


def get_accounts(req):
    return 200, ledger.list_accounts(req.conn)


def post_account(req):
    data = req.json()
    return 201, ledger.create_account(req.conn, data.get("name"))


def post_import(req, account_id):
    filename = unquote(req.headers.get("X-Filename", "")) or "upload.csv"
    try:
        text = req.body.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UserError("The file isn't a text CSV (it couldn't be read as UTF-8).") from None
    sign = req.query.get("amount_sign")
    try:
        summary = import_csv(
            req.conn,
            int(account_id),
            filename,
            text,
            allow_new_columns=req.query.get("allow_new_columns") == "1",
            amount_sign=_int(sign, "amount_sign") if sign else None,
        ).to_dict()
    except NeedsInput as question:
        # Not an error: the page asks the user, then retries.
        return 200, question.to_dict()
    summary["budget_alerts"] = budgets.budget_status(req.conn, date.today())["alerts"]
    return 201, summary


def get_layouts(req):
    return 200, list_layouts(req.conn)


def post_layout(req):
    data = req.json()
    return 201, save_layout(req.conn, data.get("headers"), data.get("layout"))


def delete_layout_route(req, layout_id):
    delete_layout(req.conn, int(layout_id))
    return 200, {}


def get_batches(req):
    return 200, ledger.list_batches(req.conn)


def delete_batch(req, batch_id):
    return 200, {"removed": undo_import(req.conn, int(batch_id))}


def _filter(req) -> queries.Filter:
    category_id = req.int_query("category_id")
    if req.query.get("uncategorized") == "1":
        category_id = ledger.uncategorized_id(req.conn)
    return queries.Filter(
        start=req.query.get("start") or None,
        end=req.query.get("end") or None,
        category_id=category_id,
        category_exact=req.query.get("category_exact") == "1" or req.query.get("uncategorized") == "1",
        account_id=req.int_query("account_id"),
        text=req.query.get("q") or None,
        person=_person_query(req.query.get("person")),
        needs_review=req.query.get("needs_review") == "1",
    )


def _person_query(value):
    if not value:
        return None
    if value == queries.HOUSEHOLD:
        return queries.HOUSEHOLD
    return _int(value, "person")


def get_transactions(req):
    result = queries.list_transactions(
        req.conn,
        _filter(req),
        sort=req.query.get("sort", "date"),
        direction=req.query.get("dir", "desc"),
    )
    if req.query.get("with_recent") == "1":
        # How the same store was categorized before, to make review quick.
        for row in result["rows"]:
            row["recent"] = ledger.recent_choices(req.conn, row["description"], row["id"])
    return 200, result


def get_transaction(req, transaction_id):
    return 200, ledger.get_transaction(req.conn, int(transaction_id))


def put_split(req, transaction_id):
    return 200, ledger.split_transaction(req.conn, int(transaction_id), req.json().get("parts"))


def delete_split(req, transaction_id):
    return 200, ledger.unsplit_transaction(req.conn, int(transaction_id))


def patch_part(req, part_id):
    data = req.json()
    if "person_id" in data:
        return 200, ledger.set_part_person(req.conn, int(part_id), _optional_id(data, "person_id"))
    category_id = _int(data.get("category_id"), "category_id")
    return 200, ledger.set_part_category(req.conn, int(part_id), category_id)


def get_totals(req):
    return 200, queries.group_totals(req.conn, _filter(req), req.query.get("group", ""))


def _optional_id(data, name):
    """An id from JSON that may be null (e.g. person_id null = Household)."""
    value = data.get(name)
    return None if value is None else _int(value, name)


def patch_transaction(req, transaction_id):
    data = req.json()
    if "person_id" in data:
        return 200, ledger.set_person(req.conn, int(transaction_id), _optional_id(data, "person_id"))
    category_id = _int(data.get("category_id"), "category_id")
    return 200, ledger.set_category(req.conn, int(transaction_id), category_id)


def post_rule(req):
    data = req.json()
    category_id = _int(data.get("category_id"), "category_id")
    return 201, ledger.create_rule(req.conn, data.get("pattern"), category_id, _optional_id(data, "person_id"))


def get_rules(req):
    return 200, ledger.list_rules(req.conn)


def patch_rule(req, rule_id):
    data = req.json()
    changes = {}
    if "category_id" in data:
        changes["category_id"] = _int(data["category_id"], "category_id")
    if "person_id" in data:
        changes["person_id"] = _optional_id(data, "person_id")
    return 200, ledger.update_rule(req.conn, int(rule_id), **changes)


def get_people(req):
    return 200, ledger.list_people(req.conn)


def post_person(req):
    return 201, ledger.create_person(req.conn, req.json().get("name"))


def patch_person(req, person_id):
    return 200, ledger.rename_person(req.conn, int(person_id), req.json().get("name"))


def delete_person(req, person_id):
    return 200, ledger.delete_person(req.conn, int(person_id))


def delete_rule(req, rule_id):
    return 200, ledger.delete_rule(req.conn, int(rule_id))


def post_category(req):
    data = req.json()
    parent_id = data.get("parent_id")
    return 201, ledger.create_category(
        req.conn,
        data.get("name"),
        data.get("kind") or "expense",
        _int(parent_id, "parent_id") if parent_id is not None else None,
    )


def patch_category(req, category_id):
    data = req.json()
    if "parent_id" in data:
        parent_id = data["parent_id"]
        return 200, ledger.move_category(
            req.conn, int(category_id), _int(parent_id, "parent_id") if parent_id is not None else None
        )
    return 200, ledger.rename_category(req.conn, int(category_id), data.get("name"))


def delete_category(req, category_id):
    return 200, ledger.delete_category(req.conn, int(category_id))


def get_bank_categories(req):
    return 200, ledger.list_bank_categories(req.conn)


def put_bank_category(req):
    data = req.json()
    category_id = data.get("category_id")
    if category_id is not None:
        category_id = _int(category_id, "category_id")
    return 200, ledger.set_bank_category(req.conn, data.get("bank_category"), category_id)


def patch_account(req, account_id):
    data = req.json()
    if "amount_sign" in data:
        return 200, ledger.set_amount_sign(req.conn, int(account_id), data["amount_sign"])
    return 200, ledger.rename_account(req.conn, int(account_id), data.get("name"))


def delete_account(req, account_id):
    return 200, ledger.delete_account(req.conn, int(account_id))


def get_dashboard(req):
    """This month's budget status plus chart data for a date range.

    Without start/end the range covers all your transactions.
    """
    today = date.today()
    _, month_end = budgets.month_bounds(today)
    span = queries.data_range(req.conn)
    start = req.query.get("start") or (span[0] if span else today.replace(day=1).isoformat())
    end = req.query.get("end") or max(span[1] if span else "", month_end.isoformat())
    flt = queries.Filter(start=start, end=end)
    return 200, {
        "budget": budgets.budget_status(req.conn, today),
        "range": {"start": start, "end": end},
        "monthly": queries.monthly_totals(req.conn, start, end),
        "categories": [g for g in queries.group_totals(req.conn, flt, "category") if g["spent_cents"] > 0],
        "category_rank": queries.category_rank(req.conn),
    }


def get_questions(req):
    return 200, ask.question_list(req.conn, date.today())


def post_question(req):
    data = req.json()
    return 200, ask.answer(req.conn, data.get("type"), data.get("params", {}), date.today())


def get_budgets(req):
    return 200, budgets.get_budgets(req.conn)


def put_budgets(req):
    data = req.json()
    return 200, budgets.save_budgets(
        req.conn, data.get("limits", []), data.get("overall_cents"), data.get("warn_pct")
    )


ROUTES = [
    ("GET", r"/api/categories", get_categories),
    ("POST", r"/api/categories", post_category),
    ("PATCH", r"/api/categories/(\d+)", patch_category),
    ("DELETE", r"/api/categories/(\d+)", delete_category),
    ("GET", r"/api/bank-categories", get_bank_categories),
    ("PUT", r"/api/bank-categories", put_bank_category),
    ("GET", r"/api/budgets", get_budgets),
    ("GET", r"/api/dashboard", get_dashboard),
    ("GET", r"/api/ask", get_questions),
    ("POST", r"/api/ask", post_question),
    ("PUT", r"/api/budgets", put_budgets),
    ("GET", r"/api/accounts", get_accounts),
    ("POST", r"/api/accounts", post_account),
    ("PATCH", r"/api/accounts/(\d+)", patch_account),
    ("DELETE", r"/api/accounts/(\d+)", delete_account),
    ("POST", r"/api/accounts/(\d+)/import", post_import),
    ("GET", r"/api/layouts", get_layouts),
    ("POST", r"/api/layouts", post_layout),
    ("DELETE", r"/api/layouts/(\d+)", delete_layout_route),
    ("GET", r"/api/batches", get_batches),
    ("DELETE", r"/api/batches/(\d+)", delete_batch),
    ("GET", r"/api/transactions", get_transactions),
    ("GET", r"/api/totals", get_totals),
    ("GET", r"/api/transactions/(\d+)", get_transaction),
    ("PATCH", r"/api/transactions/(\d+)", patch_transaction),
    ("PUT", r"/api/transactions/(\d+)/split", put_split),
    ("DELETE", r"/api/transactions/(\d+)/split", delete_split),
    ("PATCH", r"/api/parts/(\d+)", patch_part),
    ("GET", r"/api/people", get_people),
    ("POST", r"/api/people", post_person),
    ("PATCH", r"/api/people/(\d+)", patch_person),
    ("DELETE", r"/api/people/(\d+)", delete_person),
    ("GET", r"/api/rules", get_rules),
    ("POST", r"/api/rules", post_rule),
    ("PATCH", r"/api/rules/(\d+)", patch_rule),
    ("DELETE", r"/api/rules/(\d+)", delete_rule),
]
ROUTES = [(method, re.compile(pattern), handler) for method, pattern, handler in ROUTES]


def make_handler(db_path: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Budget"

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def log_request(self, code="-", size="-"):
            # Log API calls and failures, not every static file.
            failed = isinstance(code, int) and code >= 400
            if self.path.startswith("/api/") or failed:
                super().log_request(code, size)

        def _dispatch(self, method):
            hostname = re.sub(r":\d+$", "", self.headers.get("Host", ""))
            if hostname not in LOCAL_HOSTNAMES:
                return self._send_json(403, {"error": "This app only answers on localhost."})
            url = urlsplit(self.path)
            if url.path.startswith("/api/"):
                return self._api(method, url)
            if method == "GET":
                return self._static(url.path)
            self._send_json(405, {"error": "Method not allowed."})

        def _api(self, method, url):
            if method != "GET" and self.headers.get("X-Budget") != "1":
                return self._send_json(403, {"error": "Missing X-Budget header."})
            for route_method, pattern, handler in ROUTES:
                match = pattern.fullmatch(url.path)
                if match and route_method == method:
                    break
            else:
                return self._send_json(404, {"error": "Not found."})

            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                return self._send_json(413, {"error": "That file is too large (10 MB max)."})
            body = self.rfile.read(length) if length else b""
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}

            conn = db.connect(db_path)
            try:
                status, payload = handler(Request(conn, query, body, self.headers), *match.groups())
            except NotFound as e:
                status, payload = 404, {"error": str(e)}
            except UserError as e:
                status, payload = 400, {"error": str(e)}
            except Exception:
                traceback.print_exc()
                status, payload = 500, {"error": "Something went wrong. Details are in the terminal."}
            finally:
                conn.close()
            self._send_json(status, payload)

        def _static(self, path):
            name = "index.html" if path == "/" else path.removeprefix("/static/")
            file = (STATIC_DIR / name).resolve()
            if file.parent != STATIC_DIR or not file.is_file() or file.suffix not in CONTENT_TYPES:
                return self._send_json(404, {"error": "Not found."})
            self._send(200, CONTENT_TYPES[file.suffix], file.read_bytes())

        def _send_json(self, status, payload):
            self._send(status, "application/json", json.dumps(payload).encode())

        def _send(self, status, content_type, body):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def run(config: Config) -> None:
    db.init(config.db_path)
    server = ThreadingHTTPServer((config.host, config.port), make_handler(config.db_path))
    url = f"http://localhost:{server.server_address[1]}/"
    print(f"Budget is running at {url}  (data: {config.db_path})")
    print("Press Ctrl+C to stop.")
    if config.open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    finally:
        server.server_close()
