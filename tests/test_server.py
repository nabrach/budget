"""End-to-end checks through the real HTTP server."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from budgetapp.server import make_handler
from tests.helpers import DbTestCase, fixture


class ServerTest(DbTestCase):
    def setUp(self):
        super().setUp()
        handler = make_handler(self.db_path)
        handler.log_message = lambda *args: None  # keep test output clean
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, method, path, body=None, headers=None, json_body=None):
        headers = {"X-Budget": "1", **(headers or {})}
        if json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_import_flow(self):
        status, account = self.request(
            "POST", "/api/accounts", json_body={"name": "Chase Checking"}
        )
        self.assertEqual(status, 201)

        status, summary = self.request(
            "POST",
            f"/api/accounts/{account['id']}/import?amount_sign=1",
            body=fixture("chase_checking.csv").encode(),
            headers={"Content-Type": "text/csv", "X-Filename": "chase%20checking.csv"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(summary["rows_added"], 27)
        self.assertEqual(summary["filename"], "chase checking.csv")

        status, review = self.request("GET", "/api/transactions?uncategorized=1")
        self.assertEqual((status, review["count"]), (200, 10))

        status, found = self.request("GET", "/api/transactions?q=venmo&sort=amount&dir=asc&start=2026-10-13&end=2026-10-13")
        self.assertEqual(status, 200)
        self.assertEqual([r["amount_cents"] for r in found["rows"]], [-700, -700, -700, 1235])

        status, totals = self.request("GET", "/api/totals?group=month")
        self.assertEqual((status, [g["label"] for g in totals]), (200, ["Oct 2026"]))

        status, body = self.request("GET", "/api/transactions?start=10/01/2026")
        self.assertEqual(status, 400)
        self.assertIn("date like", body["error"])

        status, batches = self.request("GET", "/api/batches")
        self.assertEqual(len(batches), 1)
        status, result = self.request("DELETE", f"/api/batches/{batches[0]['id']}")
        self.assertEqual((status, result), (200, {"removed": 27}))

    def test_dashboard(self):
        status, account = self.request(
            "POST", "/api/accounts", json_body={"name": "Card"}
        )
        self.request(
            "POST",
            f"/api/accounts/{account['id']}/import?amount_sign=1",
            body=fixture("chase_credit_1.csv").encode(),
            headers={"Content-Type": "text/csv"},
        )
        status, data = self.request("GET", "/api/dashboard?start=2026-09-01&end=2026-10-31")
        self.assertEqual(status, 200)
        self.assertEqual([m["label"] for m in data["monthly"]], ["Sep 2026", "Oct 2026"])
        self.assertEqual(data["monthly"][1]["spent_cents"], 20300)
        self.assertEqual([c["label"] for c in data["categories"]], ["Travel", "Food"])
        self.assertIn("alerts", data["budget"])

    def test_import_includes_budget_alerts(self):
        status, account = self.request(
            "POST", "/api/accounts", json_body={"name": "Card"}
        )
        status, summary = self.request(
            "POST",
            f"/api/accounts/{account['id']}/import?amount_sign=1",
            body=fixture("chase_credit_1.csv").encode(),
            headers={"Content-Type": "text/csv"},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(summary["budget_alerts"], list)

    def test_user_errors_come_back_as_400_with_a_message(self):
        status, body = self.request("POST", "/api/accounts", json_body={"name": ""})
        self.assertEqual(status, 400)
        self.assertIn("name", body["error"])

    def test_changes_need_the_x_budget_header(self):
        req = urllib.request.Request(
            self.base + "/api/accounts", data=b"{}", method="POST", headers={"X-Budget": "0"}
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req)
        self.assertEqual(caught.exception.code, 403)

    def test_rejects_non_local_host_header(self):
        status, _ = self.request("GET", "/api/accounts", headers={"Host": "evil.example:8000"})
        self.assertEqual(status, 403)

    def test_serves_the_page_but_not_other_files(self):
        with urllib.request.urlopen(self.base + "/") as res:
            self.assertIn(b"<html", res.read())
        status, _ = self.request("GET", "/static/../budgetapp/db.py")
        self.assertEqual(status, 404)

    def test_transfer_flow(self):
        from tests.test_transfers import CARD, CHECKING

        ids = {}
        for name, text in (("Checking", CHECKING), ("Card", CARD)):
            _, account = self.request("POST", "/api/accounts", json_body={"name": name})
            status, summary = self.request(
                "POST", f"/api/accounts/{account['id']}/import?amount_sign=1", body=text.encode(),
                headers={"Content-Type": "text/csv"},
            )
            self.assertEqual(status, 201)
            ids[name] = account["id"]
        self.assertEqual(summary["transfer_suggestions"], 2)

        status, review = self.request("GET", "/api/transfers")
        self.assertEqual((status, len(review["suggestions"])), (200, 2))
        first, second = review["suggestions"]
        status, linked = self.request(
            "POST", "/api/transfers/link", json_body={"first_id": first["out"]["id"], "second_id": first["in"]["id"]}
        )
        self.assertEqual((status, linked["other"]["account_name"]), (200, "Card"))
        status, _ = self.request(
            "POST", "/api/transfers/reject", json_body={"first_id": second["out"]["id"], "second_id": second["in"]["id"]}
        )
        self.assertEqual(status, 200)

        status, found = self.request("GET", f"/api/transactions?account_id={ids['Checking']}")
        tags = {r["id"]: r["transfer"] for r in found["rows"]}
        self.assertEqual(tags[first["out"]["id"]]["status"], "linked")

        status, _ = self.request("DELETE", f"/api/transactions/{first['out']['id']}/transfer")
        self.assertEqual(status, 200)
        status, candidates = self.request("GET", f"/api/transactions/{first['out']['id']}/transfer-candidates")
        self.assertEqual((status, len(candidates["candidates"])), (200, 1))

        status, settings = self.request("PUT", "/api/transfer-settings", json_body={"auto_link": True})
        self.assertEqual((status, settings["auto_link"], settings["decided"]), (200, True, 2))
        status, ignore = self.request("POST", "/api/transfer-ignores", json_body={"pattern": "online transfer"})
        self.assertEqual(status, 201)
        status, _ = self.request("DELETE", f"/api/transfer-ignores/{ignore['id']}")
        self.assertEqual(status, 200)
        status, error = self.request("POST", "/api/transfers/link", json_body={"first_id": 1, "second_id": 1})
        self.assertEqual((status, error["error"]), (400, "Pick two different transactions."))

    def test_amazon_and_notes(self):
        from tests.test_amazon import CARD, ORDERS

        _, account = self.request("POST", "/api/accounts", json_body={"name": "Prime Card"})
        self.request("POST", f"/api/accounts/{account['id']}/import?amount_sign=1", body=CARD.encode(), headers={"Content-Type": "text/csv"})
        status, summary = self.request("POST", "/api/amazon/import", body=ORDERS.encode(), headers={"Content-Type": "text/csv"})
        self.assertEqual((status, summary["matched"]), (201, 3))
        status, review = self.request("GET", "/api/amazon")
        self.assertEqual((status, len(review["charges"]), review["status"]["orders"]), (200, 3, 3))
        entry = review["charges"][-1]
        status, detail = self.request("GET", f"/api/amazon/charges/{entry['charge']['id']}")
        self.assertEqual((status, detail["review"]), (200, True))
        _, categories = self.request("GET", "/api/categories")
        gifts = next(c["id"] for c in categories if c["name"] == "Gifts")
        status, result = self.request(
            "POST", f"/api/amazon/charges/{entry['charge']['id']}/apply",
            json_body={"items": [{"item_id": i["id"], "category_id": gifts} for i in entry["order"]["items"]]},
        )
        self.assertEqual((status, result["split"]), (200, False))
        status, _ = self.request("PUT", "/api/amazon/departments", json_body={"department": "Toy", "category_id": gifts})
        self.assertEqual(status, 200)
        status, found = self.request("GET", "/api/transactions?q=toothpaste")
        self.assertEqual((status, found["count"]), (200, 2))
        self.assertTrue(all(r["amazon"]["items"] for r in found["rows"]))
        status, note = self.request("PATCH", f"/api/transactions/{entry['charge']['id']}", json_body={"note": "for the party"})
        self.assertEqual((status, note["note"]), (200, "for the party"))
        status, error = self.request("POST", "/api/amazon/import", body=b"Date,Description,Amount\n", headers={"Content-Type": "text/csv"})
        self.assertEqual(status, 400)
