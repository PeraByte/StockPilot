"""Checkout, receipts, the point-of-sale API and sales history."""

import threading
import unittest
from datetime import datetime
from unittest import mock

import database
import inventory
import sales
from tests.base import StockPilotTestCase


class CheckoutTests(StockPilotTestCase):
    """The core promise: a sale is recorded completely, correctly, or not at all."""

    def setUp(self):
        super().setUp()
        self.owner, self.owner_id = self.make_user()
        # A: cost 80.00, price 120.00, 10 in stock.  B: cost 10.00, price 20.50, 5 in stock.
        self.a = self.add_product(self.owner_id, name="Product A", sku="A-1", cost=8000, price=12000, qty=10)
        self.b = self.add_product(self.owner_id, name="Product B", sku="B-1", cost=1000, price=2050, qty=5)

    def checkout(self, items, **extra):
        return self.post_json(self.owner, "/api/checkout", {"items": items, **extra})

    def counts(self):
        return (self.scalar("SELECT COUNT(*) FROM sales"), self.scalar("SELECT COUNT(*) FROM sale_items"),
                self.scalar("SELECT COUNT(*) FROM transactions"),
                self.scalar("SELECT SUM(quantity) FROM products"))

    def test_successful_checkout_records_everything(self):
        response = self.checkout([{"product_id": self.a, "quantity": 2}, {"product_id": self.b, "quantity": 1}],
                                 notes="  Walk-in customer ")
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual((body["receipt_number"], body["total"]), ("S-000001", 26050))
        self.assertEqual(body["receipt_url"], f"/sales/{body['sale_id']}")

        sale = self.rows("SELECT * FROM sales")[0]
        self.assertEqual((sale["user_id"], sale["receipt_number"], sale["total_amount"], sale["total_cost"],
                          sale["total_profit"], sale["notes"]), (self.owner_id, 1, 26050, 17000, 9050, "Walk-in customer"))

        items = {i["product_id"]: i for i in self.rows("SELECT * FROM sale_items")}
        self.assertEqual((items[self.a]["quantity"], items[self.a]["unit_price"], items[self.a]["unit_cost_snapshot"],
                          items[self.a]["line_total"], items[self.a]["line_profit"],
                          items[self.a]["product_name_snapshot"], items[self.a]["sku_snapshot"]),
                         (2, 12000, 8000, 24000, 8000, "Product A", "A-1"))
        self.assertEqual((items[self.b]["quantity"], items[self.b]["line_total"], items[self.b]["line_profit"]),
                         (1, 2050, 1050))

        self.assertEqual(self.product(self.a)["quantity"], 8)
        self.assertEqual(self.product(self.b)["quantity"], 4)

        sale_rows = self.rows("SELECT * FROM transactions WHERE transaction_type = 'SALE' ORDER BY product_id")
        self.assertEqual([(t["product_id"], t["quantity"], t["unit_price"], t["stock_after"], t["sale_id"], t["notes"])
                          for t in sale_rows],
                         [(self.a, 2, 12000, 8, sale["id"], "Sale S-000001"),
                          (self.b, 1, 2050, 4, sale["id"], "Sale S-000001")])

    def test_totals_equal_the_sum_of_the_lines(self):
        self.checkout([{"product_id": self.a, "quantity": 3}, {"product_id": self.b, "quantity": 5}])
        sale = self.rows("SELECT * FROM sales")[0]
        self.assertEqual(sale["total_amount"], self.scalar("SELECT SUM(line_total) FROM sale_items"))
        self.assertEqual(sale["total_profit"], self.scalar("SELECT SUM(line_profit) FROM sale_items"))
        self.assertEqual(sale["total_amount"], 3 * 12000 + 5 * 2050)

    def test_browser_supplied_prices_and_totals_are_ignored(self):
        response = self.checkout(
            [{"product_id": self.a, "quantity": 1, "unit_price": 1, "price": 1, "selling_price": 1, "cost_price": 0}],
            total=1, total_amount=1, total_profit=999999, receipt_number=777)
        self.assertEqual(response.status_code, 201)
        sale = self.rows("SELECT * FROM sales")[0]
        self.assertEqual((sale["total_amount"], sale["total_cost"], sale["receipt_number"]), (12000, 8000, 1))

    def test_duplicate_lines_for_one_product_are_merged(self):
        self.assertEqual(self.checkout([{"product_id": self.a, "quantity": 2}, {"product_id": self.a, "quantity": 3}]).status_code, 201)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sale_items"), 1)
        self.assertEqual(self.product(self.a)["quantity"], 5)

    def test_selling_exactly_all_the_stock_is_allowed(self):
        self.assertEqual(self.checkout([{"product_id": self.b, "quantity": 5}]).status_code, 201)
        self.assertEqual(self.product(self.b)["quantity"], 0)

    def test_insufficient_stock_is_refused_and_nothing_changes(self):
        before = self.counts()
        response = self.checkout([{"product_id": self.a, "quantity": 2}, {"product_id": self.b, "quantity": 6}])
        self.assertEqual(response.status_code, 409)
        body = response.get_json()
        self.assertFalse(body["ok"])
        self.assertIn("Product B", body["error"])
        self.assertEqual(body["items"], [{"product_id": self.b, "available": 5}])
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.product(self.a)["quantity"], 10)   # the valid line was not sold either

    def test_invalid_payloads_are_rejected(self):
        bad_carts = {
            "empty cart": [],
            "not a list": "nope",
            "missing items": None,
            "item not an object": [5],
            "zero quantity": [{"product_id": self.a, "quantity": 0}],
            "negative quantity": [{"product_id": self.a, "quantity": -1}],
            "fractional quantity": [{"product_id": self.a, "quantity": 1.5}],
            "string quantity": [{"product_id": self.a, "quantity": "2"}],
            "boolean quantity": [{"product_id": self.a, "quantity": True}],
            "boolean product id": [{"product_id": True, "quantity": 1}],
            "string product id": [{"product_id": str(self.a), "quantity": 1}],
            "missing quantity": [{"product_id": self.a}],
            "missing product id": [{"quantity": 1}],
            "zero product id": [{"product_id": 0, "quantity": 1}],
            "huge quantity": [{"product_id": self.a, "quantity": 100_001}],
            "too many lines": [{"product_id": self.a, "quantity": 1}] * 101,
        }
        before = self.counts()
        for label, cart in bad_carts.items():
            with self.subTest(label):
                response = self.post_json(self.owner, "/api/checkout", {"items": cart})
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()["ok"])
        self.assertEqual(self.counts(), before)

    def test_malformed_request_bodies(self):
        before = self.counts()
        token = {"X-CSRF-Token": self.csrf(self.owner)}
        for label, kwargs in {"not json": dict(data="items=1", content_type="text/plain"),
                              "json list": dict(json=[1, 2]),
                              "broken json": dict(data="{oops", content_type="application/json")}.items():
            with self.subTest(label):
                response = self.owner.post("/api/checkout", headers=token, **kwargs)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["ok"], False)
        self.assertEqual(self.counts(), before)

    def test_notes_length_limit(self):
        self.assertEqual(self.checkout([{"product_id": self.b, "quantity": 1}], notes="x" * 200).status_code, 201)
        response = self.checkout([{"product_id": self.b, "quantity": 1}], notes="x" * 201)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sales"), 1)

    def test_checkout_needs_login_and_a_csrf_token(self):
        payload = {"items": [{"product_id": self.a, "quantity": 1}]}
        # anonymous visitor: CSRF token is checked first, then login
        anonymous = self.app.test_client()
        self.assertIn(self.post_json(anonymous, "/api/checkout", payload).status_code, (401, 403))
        # logged in but no CSRF header
        response = self.owner.post("/api/checkout", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        # logged in with a wrong token
        response = self.owner.post("/api/checkout", json=payload, headers={"X-CSRF-Token": "wrong"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sales"), 0)
        self.assertEqual(self.product(self.a)["quantity"], 10)

    def test_checkout_rejects_get(self):
        self.assertEqual(self.owner.get("/api/checkout").status_code, 405)

    def test_receipt_numbers_count_up_per_business(self):
        other, other_id = self.make_user("other@example.com")
        other_product = self.add_product(other_id, qty=5)
        first = self.checkout([{"product_id": self.a, "quantity": 1}]).get_json()
        second = self.checkout([{"product_id": self.a, "quantity": 1}]).get_json()
        theirs = self.post_json(other, "/api/checkout", {"items": [{"product_id": other_product, "quantity": 1}]}).get_json()
        self.assertEqual((first["receipt_number"], second["receipt_number"], theirs["receipt_number"]),
                         ("S-000001", "S-000002", "S-000001"))


class CheckoutAtomicityTests(StockPilotTestCase):
    def setUp(self):
        super().setUp()
        self.owner, self.owner_id = self.make_user()
        self.a = self.add_product(self.owner_id, name="Product A", sku="A-1", qty=10)
        self.b = self.add_product(self.owner_id, name="Product B", sku="B-1", qty=10)

    def snapshot(self):
        return (self.scalar("SELECT COUNT(*) FROM sales"), self.scalar("SELECT COUNT(*) FROM sale_items"),
                self.scalar("SELECT COUNT(*) FROM transactions"), self.product(self.a)["quantity"],
                self.product(self.b)["quantity"])

    def test_failure_halfway_rolls_back_the_whole_sale(self):
        real = inventory.apply_stock_movement
        calls = []

        def fail_on_second_call(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("simulated crash after the first product was already deducted")
            return real(*args, **kwargs)

        before = self.snapshot()
        with self.app_db() as db, mock.patch.object(inventory, "apply_stock_movement", side_effect=fail_on_second_call):
            with self.assertRaises(RuntimeError):
                sales.checkout(db, self.owner_id, [{"product_id": self.a, "quantity": 2},
                                                   {"product_id": self.b, "quantity": 2}])
            self.assertFalse(db.in_transaction)   # the transaction was closed, not left dangling
        self.assertEqual(len(calls), 2)           # the first deduction really happened before the failure
        self.assertEqual(self.snapshot(), before)  # ...and was undone

    def test_stock_error_inside_the_transaction_becomes_a_conflict_and_rolls_back(self):
        real = inventory.apply_stock_movement
        calls = []

        def refuse_second(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise inventory.StockError("Not enough stock")
            return real(*args, **kwargs)

        before = self.snapshot()
        with self.app_db() as db, mock.patch.object(inventory, "apply_stock_movement", side_effect=refuse_second):
            with self.assertRaises(sales.CheckoutError) as caught:
                sales.checkout(db, self.owner_id, [{"product_id": self.a, "quantity": 1},
                                                   {"product_id": self.b, "quantity": 1}])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.snapshot(), before)

    def test_a_failed_sale_does_not_use_up_a_receipt_number(self):
        with self.app_db() as db:
            with self.assertRaises(sales.CheckoutError):
                sales.checkout(db, self.owner_id, [{"product_id": self.a, "quantity": 99}])
        self.sell(self.owner_id, [(self.a, 1)])
        self.assertEqual(self.rows("SELECT receipt_number FROM sales")[0]["receipt_number"], 1)

    def test_two_simultaneous_checkouts_cannot_oversell_the_last_unit(self):
        last = self.add_product(self.owner_id, name="Last One", sku="LAST", qty=1)
        barrier = threading.Barrier(2)
        outcomes = []

        def buyer():
            connection = database.connect(self.db_path)   # each thread gets its own connection
            try:
                barrier.wait(timeout=5)
                try:
                    outcomes.append(("sold", sales.checkout(connection, self.owner_id, [{"product_id": last, "quantity": 1}])))
                except sales.CheckoutError as problem:
                    outcomes.append(("refused", problem.status))
            finally:
                connection.close()

        threads = [threading.Thread(target=buyer) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        kinds = sorted(kind for kind, _ in outcomes)
        self.assertEqual(kinds, ["refused", "sold"], outcomes)
        self.assertEqual([value for kind, value in outcomes if kind == "refused"], [409])
        self.assertEqual(self.product(last)["quantity"], 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sale_items WHERE product_id = ?", (last,)), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM transactions WHERE product_id = ? AND transaction_type = 'SALE'", (last,)), 1)

    def test_database_rejects_inconsistent_sale_totals(self):
        connection = database.connect(self.db_path)
        try:
            with self.assertRaises(Exception) as caught:
                connection.execute(
                    "INSERT INTO sales (user_id, receipt_number, total_amount, total_cost, total_profit, created_at) "
                    "VALUES (?, 1, 1000, 400, 999, '2026-01-01 00:00:00')", (self.owner_id,))
            self.assertIn("CHECK", str(caught.exception).upper())
        finally:
            connection.close()

    def test_database_rejects_inconsistent_sale_lines(self):
        sale_id = self.sell(self.owner_id, [(self.a, 1)])
        connection = database.connect(self.db_path)
        try:
            with self.assertRaises(Exception):
                connection.execute(
                    "INSERT INTO sale_items (sale_id, product_id, product_name_snapshot, sku_snapshot, quantity, "
                    "unit_cost_snapshot, unit_price, line_total, line_profit) VALUES (?, ?, 'x', 'x', 2, 100, 500, 999, 0)",
                    (sale_id, self.a))
        finally:
            connection.close()


class CheckoutIsolationTests(StockPilotTestCase):
    def setUp(self):
        super().setUp()
        self.alice, self.alice_id = self.make_user("alice@example.com", "Alice A", "Alice Shop")
        self.bob, self.bob_id = self.make_user("bob@example.com", "Bob B", "Bob Shop")
        self.alice_product = self.add_product(self.alice_id, name="Alices Item", qty=10)
        self.bob_product = self.add_product(self.bob_id, name="Bobs Item", qty=10)

    def test_cannot_sell_another_users_product(self):
        response = self.post_json(self.bob, "/api/checkout", {"items": [{"product_id": self.alice_product, "quantity": 1}]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.product(self.alice_product)["quantity"], 10)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sales"), 0)
        # the refusal must not reveal that the product exists for someone else
        self.assertNotIn("Alices Item", response.get_data(as_text=True))

    def test_mixed_cart_with_a_foreign_product_sells_nothing(self):
        response = self.post_json(self.bob, "/api/checkout", {"items": [
            {"product_id": self.bob_product, "quantity": 1}, {"product_id": self.alice_product, "quantity": 1}]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.product(self.bob_product)["quantity"], 10)

    def test_unknown_product_id_is_refused(self):
        response = self.post_json(self.bob, "/api/checkout", {"items": [{"product_id": 99999, "quantity": 1}]})
        self.assertEqual(response.status_code, 409)

    def test_receipts_are_private(self):
        sale_id = self.sell(self.alice_id, [(self.alice_product, 1)])
        self.assertEqual(self.alice.get(f"/sales/{sale_id}").status_code, 200)
        self.assertEqual(self.bob.get(f"/sales/{sale_id}").status_code, 404)
        self.assertEqual(self.app.test_client().get(f"/sales/{sale_id}").status_code, 302)  # -> login

    def test_sales_history_and_search_only_show_your_own(self):
        self.sell(self.alice_id, [(self.alice_product, 1)])
        self.assertNotIn("Alices Item", self.bob.get("/sales/history").get_data(as_text=True))
        self.assertNotIn("Alices Item", self.bob.get("/sales/history?q=Alices").get_data(as_text=True))
        names = [p["name"] for p in self.bob.get("/api/products/search?q=Item").get_json()["products"]]
        self.assertEqual(names, ["Bobs Item"])


class UnavailableProductTests(StockPilotTestCase):
    def setUp(self):
        super().setUp()
        self.owner, self.owner_id = self.make_user()

    def test_archived_products_cannot_be_sold(self):
        pid = self.add_product(self.owner_id, qty=5)
        self.post(self.owner, f"/products/{pid}/archive")
        response = self.post_json(self.owner, "/api/checkout", {"items": [{"product_id": pid, "quantity": 1}]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.product(pid)["quantity"], 5)

    def test_out_of_stock_products_cannot_be_sold(self):
        pid = self.add_product(self.owner_id, qty=0)
        response = self.post_json(self.owner, "/api/checkout", {"items": [{"product_id": pid, "quantity": 1}]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["items"], [{"product_id": pid, "available": 0}])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sales"), 0)

    def test_stock_changed_in_another_tab_is_caught_by_the_server(self):
        """The browser thinks 5 are available; someone else sold 4 meanwhile."""
        pid = self.add_product(self.owner_id, qty=5)
        self.sell(self.owner_id, [(pid, 4)])
        response = self.post_json(self.owner, "/api/checkout", {"items": [{"product_id": pid, "quantity": 5}]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["items"], [{"product_id": pid, "available": 1}])
        self.assertEqual(self.product(pid)["quantity"], 1)


class PointOfSaleTests(StockPilotTestCase):
    def setUp(self):
        super().setUp()
        self.owner, self.owner_id = self.make_user()

    def test_pos_page_loads_and_has_a_csrf_token_for_the_script(self):
        response = self.owner.get("/sales")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("/static/js/sales.js", html)
        self.assertIn(self.csrf(self.owner), html)

    def test_pos_requires_login(self):
        response = self.app.test_client().get("/sales")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_search_returns_only_sellable_products(self):
        self.add_product(self.owner_id, name="Visible Pen", sku="PEN-1", price=1550, qty=7)
        self.add_product(self.owner_id, name="Gone Pen", sku="PEN-2", qty=0)
        archived = self.add_product(self.owner_id, name="Archived Pen", sku="PEN-3", qty=3)
        self.post(self.owner, f"/products/{archived}/archive")
        products = self.owner.get("/api/products/search?q=pen").get_json()["products"]
        self.assertEqual(len(products), 1)
        self.assertEqual({k: products[0][k] for k in ("name", "sku", "price", "available")},
                         {"name": "Visible Pen", "sku": "PEN-1", "price": 1550, "available": 7})

    def test_search_matches_sku_and_handles_wildcards_literally(self):
        self.add_product(self.owner_id, name="Plain Mug", sku="MUG-77")
        self.add_product(self.owner_id, name="100% Cotton", sku="COT-1")
        self.assertEqual([p["name"] for p in self.owner.get("/api/products/search?q=mug-7").get_json()["products"]], ["Plain Mug"])
        self.assertEqual([p["name"] for p in self.owner.get("/api/products/search?q=%25").get_json()["products"]], ["100% Cotton"])
        self.assertEqual(len(self.owner.get("/api/products/search").get_json()["products"]), 2)  # blank = everything

    def test_search_requires_login_and_returns_json_401(self):
        response = self.app.test_client().get("/api/products/search")
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.get_json()["ok"])


class ReceiptTests(StockPilotTestCase):
    def setUp(self):
        super().setUp()
        self.owner, self.owner_id = self.make_user(business_name="Corner Store")
        self.pid = self.add_product(self.owner_id, name="Original Name", sku="ORIG-1", cost=5000, price=9000, qty=10)

    def test_receipt_shows_the_sale(self):
        sale_id = self.sell(self.owner_id, [(self.pid, 3)], notes="Paid cash")
        html = self.owner.get(f"/sales/{sale_id}").get_data(as_text=True)
        for expected in ["Corner Store", "S-000001", "Original Name", "ORIG-1", "PKR 90.00", "PKR 270.00", "Paid cash",
                         "PKR 120.00"]:  # 120.00 = profit 3 x (90 - 50)
            self.assertIn(expected, html)

    def test_receipt_does_not_change_when_the_product_is_edited_or_archived(self):
        sale_id = self.sell(self.owner_id, [(self.pid, 2)])
        self.post(self.owner, f"/products/{self.pid}/edit", self.product_form(
            name="Renamed Product", sku="NEW-9", cost_price="1", selling_price="1"))
        self.post(self.owner, f"/products/{self.pid}/archive")
        self.clear_flashes(self.owner)
        html = self.owner.get(f"/sales/{sale_id}").get_data(as_text=True)
        self.assertIn("Original Name", html)
        self.assertIn("ORIG-1", html)
        self.assertIn("PKR 180.00", html)
        self.assertNotIn("Renamed Product", html)

    def test_receipt_escapes_notes(self):
        sale_id = self.sell(self.owner_id, [(self.pid, 1)], notes="<script>alert(1)</script>")
        html = self.owner.get(f"/sales/{sale_id}").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_missing_receipt_is_a_404(self):
        self.assertEqual(self.owner.get("/sales/424242").status_code, 404)

    def test_receipt_has_a_print_button_and_hides_owner_details_from_print(self):
        sale_id = self.sell(self.owner_id, [(self.pid, 1)])
        html = self.owner.get(f"/sales/{sale_id}").get_data(as_text=True)
        self.assertIn("data-print", html)
        self.assertIn("no-print", html)


class SalesHistoryTests(StockPilotTestCase):
    def setUp(self):
        super().setUp()
        self.owner, self.owner_id = self.make_user()
        self.pen = self.add_product(self.owner_id, name="Gel Pen", sku="PEN-1", qty=100)
        self.mug = self.add_product(self.owner_id, name="Tea Mug", sku="MUG-1", qty=100)
        # 2026-01-10 08:00 UTC = 13:00 in Karachi; 2026-01-12 06:00 UTC = 11:00 in Karachi
        self.first = self.sell(self.owner_id, [(self.pen, 1)], notes="first sale", now=datetime(2026, 1, 10, 8, 0, 0))
        self.second = self.sell(self.owner_id, [(self.mug, 2)], notes="second sale", now=datetime(2026, 1, 12, 6, 0, 0))

    def html(self, query=""):
        return self.owner.get(f"/sales/history?{query}").get_data(as_text=True)

    def test_newest_sale_comes_first(self):
        html = self.html()
        self.assertLess(html.index("S-000002"), html.index("S-000001"))

    def test_search_by_product_note_and_receipt_number(self):
        self.assertIn("S-000001", self.html("q=gel"))
        self.assertNotIn("S-000002", self.html("q=gel"))
        self.assertIn("S-000002", self.html("q=second"))
        self.assertNotIn("S-000001", self.html("q=second"))
        self.assertIn("S-000002", self.html("q=S-000002"))
        self.assertNotIn("S-000001", self.html("q=S-000002"))
        self.assertIn("S-000002", self.html("q=MUG-1"))   # by SKU, any capitals
        self.assertNotIn("S-000001", self.html("q=MUG-1"))

    def test_date_filter_uses_local_days(self):
        html = self.html("from=2026-01-11&to=2026-01-12")
        self.assertIn("S-000002", html)
        self.assertNotIn("S-000001", html)
        html = self.html("from=2026-01-10&to=2026-01-10")
        self.assertIn("S-000001", html)
        self.assertNotIn("S-000002", html)

    def test_local_midnight_boundary(self):
        """23:30 UTC on Jan 10 is 04:30 on Jan 11 in Karachi, so it belongs to Jan 11."""
        late = self.sell(self.owner_id, [(self.pen, 1)], now=datetime(2026, 1, 10, 23, 30, 0))
        self.assertTrue(late)
        on_tenth = self.html("from=2026-01-10&to=2026-01-10")
        on_eleventh = self.html("from=2026-01-11&to=2026-01-11")
        self.assertIn("S-000001", on_tenth)
        self.assertNotIn("S-000003", on_tenth)
        self.assertIn("S-000003", on_eleventh)

    def test_invalid_dates_show_a_message_instead_of_crashing(self):
        for query in ["from=banana", "to=2026-13-45", "from=2026-02-01&to=2026-01-01"]:
            with self.subTest(query):
                response = self.owner.get(f"/sales/history?{query}")
                self.assertEqual(response.status_code, 200)
                self.assertTrue("YYYY-MM-DD" in response.get_data(as_text=True) or "after the end date" in response.get_data(as_text=True))

    def test_empty_history_shows_a_friendly_message(self):
        other, _ = self.make_user("empty@example.com")
        self.assertIn("No sales", other.get("/sales/history").get_data(as_text=True))

    def test_pagination(self):
        for _ in range(12):
            self.sell(self.owner_id, [(self.pen, 1)])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sales"), 14)
        self.assertIn("of 14", self.html())
        self.assertIn("S-000001", self.html("page=2"))   # oldest sales land on the last page


if __name__ == "__main__":
    unittest.main()
