"""Products, stock movements, filters and user isolation."""

import sqlite3
import unittest

import database
import inventory
from tests.base import StockPilotTestCase


class ProductCrudTests(StockPilotTestCase):
    def setUp(self):
        super().setUp()
        self.owner, self.owner_id = self.make_user()

    def create_via_form(self, **overrides):
        return self.post(self.owner, "/products/new", self.product_form(**overrides))

    def test_create_product(self):
        response = self.create_via_form()
        self.assertEqual(response.status_code, 302)
        product = self.rows("SELECT * FROM products")[0]
        self.assertEqual(response.headers["Location"], f"/products/{product['id']}")
        self.assertEqual(
            (product["name"], product["sku"], product["category"], product["cost_price"],
             product["selling_price"], product["quantity"], product["reorder_level"], product["is_archived"]),
            ("Notebook A5", "NB-A5", "Stationery", 8000, 12000, 10, 5, 0))
        self.assertEqual(product["user_id"], self.owner_id)

    def test_opening_stock_is_recorded_in_the_ledger(self):
        self.create_via_form(quantity="25", cost_price="80.50")
        ledger = self.rows("SELECT * FROM transactions")
        self.assertEqual(len(ledger), 1)
        self.assertEqual((ledger[0]["transaction_type"], ledger[0]["quantity"], ledger[0]["unit_price"],
                          ledger[0]["stock_after"], ledger[0]["notes"]), ("RESTOCK", 25, 8050, 25, "Initial stock"))

    def test_zero_opening_stock_creates_no_ledger_row(self):
        self.create_via_form(quantity="0")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM transactions"), 0)

    def test_blank_optional_fields_use_defaults(self):
        self.create_via_form(quantity="", category="")
        product = self.rows("SELECT * FROM products")[0]
        self.assertEqual((product["quantity"], product["category"]), (0, "Uncategorized"))

    def test_sku_is_stored_in_capitals_and_names_are_trimmed(self):
        self.create_via_form(sku="nb-a5", name="   Notebook    A5  ")
        product = self.rows("SELECT * FROM products")[0]
        self.assertEqual((product["sku"], product["name"]), ("NB-A5", "Notebook A5"))

    def test_invalid_products_are_rejected_with_a_message(self):
        cases = {
            "missing name": ({"name": ""}, "Product name is required"),
            "missing sku": ({"sku": ""}, "SKU is required"),
            "bad sku characters": ({"sku": "no spaces!"}, "SKU may contain"),
            "negative cost": ({"cost_price": "-5"}, "cannot be negative"),
            "negative price": ({"selling_price": "-0.01"}, "cannot be negative"),
            "text price": ({"selling_price": "abc"}, "valid amount"),
            "three decimals": ({"cost_price": "1.234"}, "two decimals"),
            "negative quantity": ({"quantity": "-1"}, "cannot be negative"),
            "fractional quantity": ({"quantity": "2.5"}, "whole number"),
            "negative threshold": ({"reorder_level": "-3"}, "cannot be negative"),
            "missing threshold": ({"reorder_level": ""}, "required"),
            "name too long": ({"name": "x" * 121}, "120 characters"),
            "description too long": ({"description": "x" * 501}, "500 characters"),
        }
        for label, (overrides, message) in cases.items():
            with self.subTest(label):
                response = self.create_via_form(**overrides)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.get_data(as_text=True))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM products"), 0)

    def test_invalid_form_keeps_what_the_user_typed(self):
        response = self.create_via_form(name="Keep This Name", cost_price="-5")
        html = response.get_data(as_text=True)
        self.assertIn('value="Keep This Name"', html)
        self.assertIn('value="NB-A5"', html)

    def test_duplicate_sku_is_rejected_even_with_different_capitals(self):
        self.create_via_form()
        response = self.create_via_form(name="Another", sku="nb-a5")
        self.assertEqual(response.status_code, 400)
        self.assertIn("must be unique", response.get_data(as_text=True))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM products"), 1)

    def test_two_businesses_may_use_the_same_sku(self):
        other, _ = self.make_user("other@example.com")
        self.create_via_form()
        self.assertEqual(self.post(other, "/products/new", self.product_form()).status_code, 302)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM products"), 2)

    def test_edit_product(self):
        pid = self.add_product(self.owner_id, name="Old", sku="OLD-1", qty=7)
        response = self.post(self.owner, f"/products/{pid}/edit", self.product_form(
            name="New Name", sku="NEW-1", cost_price="90.25", selling_price="150", reorder_level="9", category="Office"))
        self.assertEqual(response.status_code, 302)
        product = self.product(pid)
        self.assertEqual((product["name"], product["sku"], product["cost_price"], product["selling_price"],
                          product["reorder_level"], product["category"]), ("New Name", "NEW-1", 9025, 15000, 9, "Office"))

    def test_editing_cannot_change_stock(self):
        pid = self.add_product(self.owner_id, qty=7)
        self.post(self.owner, f"/products/{pid}/edit", self.product_form(quantity="999"))
        self.assertEqual(self.product(pid)["quantity"], 7)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM transactions WHERE product_id = ?", (pid,)), 1)

    def test_edit_rejects_a_sku_used_by_another_product(self):
        self.add_product(self.owner_id, sku="TAKEN")
        pid = self.add_product(self.owner_id, sku="MINE")
        response = self.post(self.owner, f"/products/{pid}/edit", self.product_form(sku="taken"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.product(pid)["sku"], "MINE")

    def test_edit_page_prefills_the_form(self):
        pid = self.add_product(self.owner_id, name="Prefilled", sku="PRE-1", cost=8050, price=12000)
        html = self.owner.get(f"/products/{pid}/edit").get_data(as_text=True)
        for expected in ['value="Prefilled"', 'value="PRE-1"', 'value="80.50"', 'value="120.00"']:
            self.assertIn(expected, html)

    def test_product_text_is_escaped(self):
        pid = self.add_product(self.owner_id, name='<script>alert("x")</script>', description="<img src=x onerror=alert(1)>")
        for path in ["/products", f"/products/{pid}", "/sales/history"]:
            html = self.owner.get(path).get_data(as_text=True)
            self.assertNotIn("<script>alert", html)
            self.assertNotIn("<img src=x", html)

    def test_product_detail_numbers(self):
        pid = self.add_product(self.owner_id, cost=8000, price=12000, qty=10)
        html = self.owner.get(f"/products/{pid}").get_data(as_text=True)
        self.assertIn("PKR 800.00", html)      # stock value: 10 x 80.00
        self.assertIn("PKR 40.00", html)       # profit per unit
        self.assertIn("33.3%", html)           # margin: 40 / 120
        self.assertIn("PKR 1,200.00", html)    # retail value


class ArchiveTests(StockPilotTestCase):
    def setUp(self):
        super().setUp()
        self.owner, self.owner_id = self.make_user()
        self.pid = self.add_product(self.owner_id, name="Archivable Thing", sku="ARC-1", qty=10)

    def test_archive_hides_the_product_but_keeps_history(self):
        self.sell(self.owner_id, [(self.pid, 3)])
        response = self.post(self.owner, f"/products/{self.pid}/archive")
        self.assertEqual(response.status_code, 302)
        self.clear_flashes(self.owner)
        product = self.product(self.pid)
        self.assertEqual(product["is_archived"], 1)
        self.assertIsNotNone(product["archived_at"])
        # history untouched
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sales"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sale_items"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM transactions WHERE product_id = ?", (self.pid,)), 2)
        # hidden from everyday places
        self.assertNotIn("Archivable Thing", self.owner.get("/products").get_data(as_text=True))
        self.assertEqual(self.owner.get("/api/products/search").get_json()["products"], [])
        # visible in the archived view and still on the receipt
        self.assertIn("Archivable Thing", self.owner.get("/products?view=archived").get_data(as_text=True))
        self.assertIn("Archivable Thing", self.owner.get("/sales/1").get_data(as_text=True))

    def test_archived_products_do_not_count_on_the_dashboard(self):
        self.add_product(self.owner_id, qty=5)
        self.post(self.owner, f"/products/{self.pid}/archive")
        with self.app_db() as db:
            totals = __import__("reports").inventory_totals(db, self.owner_id)
        self.assertEqual((totals["active_products"], totals["total_units"]), (1, 5))

    def test_restore(self):
        self.post(self.owner, f"/products/{self.pid}/archive")
        self.post(self.owner, f"/products/{self.pid}/restore")
        product = self.product(self.pid)
        self.assertEqual((product["is_archived"], product["archived_at"]), (0, None))
        self.assertIn("Archivable Thing", self.owner.get("/products").get_data(as_text=True))

    def test_archived_products_cannot_be_edited_restocked_or_sold(self):
        self.post(self.owner, f"/products/{self.pid}/archive")
        self.assertEqual(self.post(self.owner, f"/products/{self.pid}/edit", self.product_form(name="Hacked")).status_code, 302)
        self.assertNotEqual(self.product(self.pid)["name"], "Hacked")
        response = self.post(self.owner, f"/products/{self.pid}/stock", {"action": "restock", "quantity": "5"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.product(self.pid)["quantity"], 10)
        response = self.post_json(self.owner, "/api/checkout", {"items": [{"product_id": self.pid, "quantity": 1}]})
        self.assertEqual(response.status_code, 409)

    def test_archived_sku_cannot_be_reused_and_the_message_explains(self):
        self.post(self.owner, f"/products/{self.pid}/archive")
        response = self.post(self.owner, "/products/new", self.product_form(sku="ARC-1"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("archived product", response.get_data(as_text=True))

    def test_archive_requires_post(self):
        self.assertEqual(self.owner.get(f"/products/{self.pid}/archive").status_code, 405)
        self.assertEqual(self.product(self.pid)["is_archived"], 0)

    def test_archive_button_asks_for_confirmation(self):
        html = self.owner.get(f"/products/{self.pid}").get_data(as_text=True)
        self.assertIn("data-confirm=", html)


class UserIsolationTests(StockPilotTestCase):
    def setUp(self):
        super().setUp()
        self.alice, self.alice_id = self.make_user("alice@example.com", "Alice A", "Alice Shop")
        self.bob, self.bob_id = self.make_user("bob@example.com", "Bob B", "Bob Shop")
        self.alice_product = self.add_product(self.alice_id, name="Alices Secret Gadget", sku="AL-1", qty=10)
        self.bob_product = self.add_product(self.bob_id, name="Bobs Plain Widget", sku="BO-1", qty=10)

    def test_lists_only_show_your_own_products(self):
        html = self.bob.get("/products").get_data(as_text=True)
        self.assertIn("Bobs Plain Widget", html)
        self.assertNotIn("Alices Secret Gadget", html)

    def test_other_users_product_pages_are_not_found(self):
        for path in [f"/products/{self.alice_product}", f"/products/{self.alice_product}/edit"]:
            with self.subTest(path=path):
                self.assertEqual(self.bob.get(path).status_code, 404)

    def test_other_users_products_cannot_be_modified(self):
        before = self.product(self.alice_product)
        attempts = [
            (f"/products/{self.alice_product}/edit", self.product_form(name="Hacked")),
            (f"/products/{self.alice_product}/stock", {"action": "adjust_out", "quantity": "5", "notes": "steal"}),
            (f"/products/{self.alice_product}/archive", {}),
            (f"/products/{self.alice_product}/restore", {}),
        ]
        for path, data in attempts:
            with self.subTest(path=path):
                self.assertEqual(self.post(self.bob, path, data).status_code, 404)
        self.assertEqual(before, self.product(self.alice_product))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM transactions WHERE product_id = ?", (self.alice_product,)), 1)

    def test_search_and_transactions_are_scoped_to_the_user(self):
        names = [p["name"] for p in self.bob.get("/api/products/search").get_json()["products"]]
        self.assertEqual(names, ["Bobs Plain Widget"])
        html = self.bob.get("/transactions").get_data(as_text=True)
        self.assertIn("Bobs Plain Widget", html)
        self.assertNotIn("Alices Secret Gadget", html)
        html = self.bob.get(f"/transactions?product_id={self.alice_product}").get_data(as_text=True)
        self.assertNotIn("Alices Secret Gadget", html)

    def test_database_refuses_a_ledger_row_that_mixes_two_users(self):
        connection = database.connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO transactions (user_id, product_id, transaction_type, quantity, unit_price, "
                    "total_amount, stock_after) VALUES (?, ?, 'RESTOCK', 1, 0, 0, 1)",
                    (self.bob_id, self.alice_product))
        finally:
            connection.close()


class StockMovementTests(StockPilotTestCase):
    def setUp(self):
        super().setUp()
        self.owner, self.owner_id = self.make_user()
        self.pid = self.add_product(self.owner_id, cost=8000, price=12000, qty=10, threshold=5)

    def move(self, **data):
        return self.post(self.owner, f"/products/{self.pid}/stock", data)

    def ledger(self):
        return self.rows("SELECT * FROM transactions WHERE product_id = ? ORDER BY id", (self.pid,))

    def test_restock_adds_stock_and_records_cost(self):
        response = self.move(action="restock", quantity="20", unit_cost="82.50", notes="Supplier delivery")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.product(self.pid)["quantity"], 30)
        self.assertEqual(self.product(self.pid)["cost_price"], 8000)  # not changed unless asked
        last = self.ledger()[-1]
        self.assertEqual((last["transaction_type"], last["quantity"], last["unit_price"], last["total_amount"],
                          last["stock_after"], last["notes"]), ("RESTOCK", 20, 8250, 165000, 30, "Supplier delivery"))

    def test_restock_uses_current_cost_when_blank_and_can_update_it(self):
        self.move(action="restock", quantity="1")
        self.assertEqual(self.ledger()[-1]["unit_price"], 8000)
        self.move(action="restock", quantity="1", unit_cost="95", update_cost="on")
        self.assertEqual(self.product(self.pid)["cost_price"], 9500)

    def test_manual_adjustments_need_a_reason_and_are_recorded(self):
        self.assertEqual(self.move(action="adjust_in", quantity="3").status_code, 400)
        self.assertEqual(self.move(action="adjust_out", quantity="3", notes="  ").status_code, 400)
        self.assertEqual(self.product(self.pid)["quantity"], 10)
        self.move(action="adjust_in", quantity="3", notes="Found in back room")
        self.move(action="adjust_out", quantity="4", notes="Damaged")
        self.assertEqual(self.product(self.pid)["quantity"], 9)
        types = [(r["transaction_type"], r["quantity"], r["stock_after"], r["notes"]) for r in self.ledger()[1:]]
        self.assertEqual(types, [("ADJUSTMENT_IN", 3, 13, "Found in back room"), ("ADJUSTMENT_OUT", 4, 9, "Damaged")])

    def test_stock_can_never_go_negative(self):
        response = self.move(action="adjust_out", quantity="11", notes="too many")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Not enough stock", response.get_data(as_text=True))
        self.assertEqual(self.product(self.pid)["quantity"], 10)
        self.assertEqual(len(self.ledger()), 1)  # no ledger row for the refused change
        self.move(action="adjust_out", quantity="10", notes="remove all")  # exactly zero is fine
        self.assertEqual(self.product(self.pid)["quantity"], 0)

    def test_service_level_negative_stock_guard(self):
        with self.app_db() as db:
            with self.assertRaises(inventory.StockError):
                with database.transaction(db):
                    inventory.apply_stock_movement(db, self.owner_id, self.pid, "ADJUSTMENT_OUT", 11, 0)
        self.assertEqual(self.product(self.pid)["quantity"], 10)

    def test_database_itself_refuses_negative_quantity(self):
        connection = database.connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE products SET quantity = -1 WHERE id = ?", (self.pid,))
        finally:
            connection.close()

    def test_invalid_stock_forms_are_rejected(self):
        bad = [dict(action="nonsense", quantity="1"), dict(action="restock", quantity="0"),
               dict(action="restock", quantity="-2"), dict(action="restock", quantity="1.5"),
               dict(action="restock", quantity=""), dict(action="restock", quantity="1", unit_cost="-3"),
               dict(action="restock", quantity="1", notes="x" * 201)]
        for data in bad:
            with self.subTest(data=data):
                self.assertEqual(self.move(**data).status_code, 400)
        self.assertEqual(self.product(self.pid)["quantity"], 10)

    def test_ledger_always_adds_up_to_the_current_stock(self):
        self.move(action="restock", quantity="5")
        self.move(action="adjust_out", quantity="2", notes="lost")
        self.sell(self.owner_id, [(self.pid, 4)])
        self.move(action="adjust_in", quantity="1", notes="recount")
        signed = self.scalar(
            "SELECT SUM(CASE WHEN transaction_type IN ('RESTOCK','ADJUSTMENT_IN') THEN quantity ELSE -quantity END) "
            "FROM transactions WHERE product_id = ?", (self.pid,))
        self.assertEqual(signed, self.product(self.pid)["quantity"])
        self.assertEqual(self.ledger()[-1]["stock_after"], self.product(self.pid)["quantity"])

    def test_foreign_keys_are_enforced_on_every_connection(self):
        with self.app_db() as db:
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        connection = database.connect(self.db_path)
        try:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        finally:
            connection.close()


class LowStockTests(StockPilotTestCase):
    def setUp(self):
        super().setUp()
        self.owner, self.owner_id = self.make_user()
        # threshold 4: quantities 0..8 -> out, low x4, ok x4
        self.ids = {q: self.add_product(self.owner_id, name=f"Qty{q:02d}Item", qty=q, threshold=4) for q in range(0, 9)}

    def names_on(self, status):
        html = self.owner.get(f"/products?status={status}&sort=name_asc").get_data(as_text=True)
        return {q for q in self.ids if f"Qty{q:02d}Item" in html}

    def test_filters_follow_the_low_and_out_of_stock_definitions(self):
        self.assertEqual(self.names_on("out"), {0})
        self.assertEqual(self.names_on("low"), {1, 2, 3, 4})   # above zero, at or below the threshold
        self.assertEqual(self.names_on("ok"), {5, 6, 7, 8})
        self.assertEqual(self.names_on("all"), set(range(9)))

    def test_sql_filters_agree_with_the_python_rule(self):
        for quantity, pid in self.ids.items():
            expected = inventory.stock_status(quantity, 4)
            for status in ("out", "low", "ok"):
                self.assertEqual(quantity in self.names_on(status), expected == status, (quantity, status))

    def test_dashboard_counts_match(self):
        with self.app_db() as db:
            totals = __import__("reports").inventory_totals(db, self.owner_id)
        self.assertEqual((totals["low_stock"], totals["out_of_stock"]), (4, 1))
        self.assertEqual(totals["active_products"], 9)
        self.assertEqual(totals["total_units"], sum(range(9)))

    def test_selling_the_last_units_moves_a_product_to_low_then_out(self):
        pid = self.ids[6]  # ok
        self.sell(self.owner_id, [(pid, 3)])   # 3 left -> low
        self.assertIn("Qty06Item", self.owner.get("/products?status=low").get_data(as_text=True))
        self.sell(self.owner_id, [(pid, 3)])   # 0 left -> out
        self.assertIn("Qty06Item", self.owner.get("/products?status=out").get_data(as_text=True))


class ListingTests(StockPilotTestCase):
    def setUp(self):
        super().setUp()
        self.owner, self.owner_id = self.make_user()

    def html(self, query):
        return self.owner.get(f"/products?{query}").get_data(as_text=True)

    def test_search_by_name_and_by_sku(self):
        self.add_product(self.owner_id, name="Blue Marker", sku="MRK-001", category="Art")
        self.add_product(self.owner_id, name="Red Pencil", sku="PCL-777", category="Art")
        html = self.html("q=marker")
        self.assertIn("Blue Marker", html)
        self.assertNotIn("Red Pencil", html)
        html = self.html("q=pcl-7")
        self.assertIn("Red Pencil", html)
        self.assertNotIn("Blue Marker", html)

    def test_percent_and_underscore_are_searched_literally(self):
        self.add_product(self.owner_id, name="100% Cotton Shirt")
        self.add_product(self.owner_id, name="Plain Mug")
        html = self.html("q=%25")
        self.assertIn("100% Cotton Shirt", html)
        self.assertNotIn("Plain Mug", html)
        self.assertNotIn("Plain Mug", self.html("q=_"))

    def test_category_filter(self):
        self.add_product(self.owner_id, name="Apple Thing", category="Fruit")
        self.add_product(self.owner_id, name="Hammer Thing", category="Tools")
        html = self.html("category=Tools")
        self.assertIn("Hammer Thing", html)
        self.assertNotIn("Apple Thing", html)
        self.assertIn("Hammer Thing", self.html("category=tools"))  # case-insensitive

    def test_sorting(self):
        self.add_product(self.owner_id, name="Bravo Item", price=5000, qty=30)
        self.add_product(self.owner_id, name="Alpha Item", price=9000, qty=10)
        self.add_product(self.owner_id, name="Charlie Item", price=1000, qty=20)

        def order(sort):
            html = self.html(f"sort={sort}")
            return sorted(["Alpha", "Bravo", "Charlie"], key=lambda n: html.index(f"{n} Item"))

        self.assertEqual(order("name_asc"), ["Alpha", "Bravo", "Charlie"])
        self.assertEqual(order("name_desc"), ["Charlie", "Bravo", "Alpha"])
        self.assertEqual(order("quantity_asc"), ["Alpha", "Charlie", "Bravo"])
        self.assertEqual(order("quantity_desc"), ["Bravo", "Charlie", "Alpha"])
        self.assertEqual(order("price_asc"), ["Charlie", "Bravo", "Alpha"])
        self.assertEqual(order("price_desc"), ["Alpha", "Bravo", "Charlie"])
        self.assertEqual(order("created_desc"), ["Charlie", "Alpha", "Bravo"])  # same second: ties fall back to id
        self.assertEqual(order("nonsense"), ["Alpha", "Bravo", "Charlie"])      # unknown value -> default

    def test_pagination(self):
        for number in range(1, 26):
            self.add_product(self.owner_id, name=f"Paged {number:02d}")
        first = self.html("")
        self.assertIn("Paged 01", first)
        self.assertIn("Paged 10", first)
        self.assertNotIn("Paged 11", first)
        self.assertIn("of 25", first)
        third = self.html("page=3")
        self.assertIn("Paged 25", third)
        self.assertNotIn("Paged 20", third)
        self.assertIn("Paged 25", self.html("page=99"))     # out-of-range page -> last page
        self.assertIn("Paged 01", self.html("page=abc"))    # junk -> first page

    def test_filters_survive_in_pagination_links(self):
        for number in range(1, 16):
            self.add_product(self.owner_id, name=f"Linked {number:02d}", category="Zed")
        self.assertIn("category=Zed", self.html("category=Zed&page=1"))
        self.assertIn("page=2", self.html("category=Zed"))

    def test_empty_states(self):
        self.assertIn("Your inventory is empty", self.html(""))
        self.add_product(self.owner_id, name="Exists")
        self.assertIn("No products match these filters", self.html("q=zzzz"))
        self.assertIn("No archived products", self.html("view=archived"))


if __name__ == "__main__":
    unittest.main()
