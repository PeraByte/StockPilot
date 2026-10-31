"""Shared test setup.

Every test gets its OWN temporary SQLite file (deleted afterwards), so running
the tests can never touch the developer's real inventory database.

Two ways to create test data:
  * through HTTP (``make_user``, ``post``)  - exercises forms, validation, CSRF
  * through the service functions (``add_product``, ``sell``) - fast, and lets a
    test choose exact timestamps for the reports tests
"""

import contextlib
import csv
import io
import os
import tempfile
import unittest

import database
import inventory
import sales
from app import create_app

PASSWORD = "password123"


class StockPilotTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test.db")
        self.app = create_app({
            "TESTING": True,
            "DATABASE": self.db_path,
            "SECRET_KEY": "test-secret-key",
            "TIMEZONE": "Asia/Karachi",   # UTC+5, no daylight saving: predictable
            "PER_PAGE": 10,
        })
        self.client = self.app.test_client()  # an anonymous visitor
        self._sku_counter = 0

    def tearDown(self):
        self._tmp.cleanup()

    # ----------------------------------------------------------- database
    @contextlib.contextmanager
    def app_db(self):
        """A database connection inside an application context (what routes use)."""
        with self.app.app_context():
            yield database.get_db()

    def rows(self, sql, params=()):
        """Run a query on a separate connection and return a list of dicts."""
        connection = database.connect(self.db_path)
        try:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]
        finally:
            connection.close()

    def scalar(self, sql, params=()):
        connection = database.connect(self.db_path)
        try:
            return connection.execute(sql, params).fetchone()[0]
        finally:
            connection.close()

    def product(self, product_id):
        return self.rows("SELECT * FROM products WHERE id = ?", (product_id,))[0]

    # --------------------------------------------------------------- HTTP
    def csrf(self, client):
        """The CSRF token stored in this client's session (created if missing)."""
        with client.session_transaction() as session:
            return session.setdefault("csrf_token", "test-csrf-token")

    def post(self, client, path, data=None, **kwargs):
        data = dict(data or {})
        data["csrf_token"] = self.csrf(client)
        return client.post(path, data=data, **kwargs)

    @staticmethod
    def clear_flashes(client):
        """Drop pending flash messages (they quote product names and would show on the next page)."""
        with client.session_transaction() as session:
            session.pop("_flashes", None)

    def post_json(self, client, path, payload, **kwargs):
        return client.post(path, json=payload, headers={"X-CSRF-Token": self.csrf(client)}, **kwargs)

    def register(self, client, email="owner@example.com", full_name="Owner One",
                 business_name="Owner Shop", password=PASSWORD, confirm=None):
        return self.post(client, "/register", {
            "full_name": full_name, "business_name": business_name, "email": email,
            "password": password, "confirm_password": password if confirm is None else confirm,
        })

    def login(self, client, email="owner@example.com", password=PASSWORD, **extra):
        return self.post(client, "/login", {"email": email, "password": password, **extra})

    def make_user(self, email="owner@example.com", full_name="Owner One", business_name="Owner Shop"):
        """Register through the real form. Returns (logged_in_client, user_id)."""
        client = self.app.test_client()
        response = self.register(client, email, full_name, business_name)
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:500])
        user_id = self.scalar("SELECT id FROM users WHERE email = ?", (email.lower(),))
        return client, user_id

    @staticmethod
    def product_form(**overrides):
        form = {
            "name": "Notebook A5", "sku": "NB-A5", "category": "Stationery", "description": "",
            "cost_price": "80", "selling_price": "120", "quantity": "10", "reorder_level": "5",
        }
        form.update(overrides)
        return form

    # ------------------------------------------------- service-level data
    def add_product(self, user_id, name="Notebook", sku=None, cost=8000, price=12000, qty=10,
                    threshold=5, category="Stationery", description="", now=None):
        """Create a product directly. Prices are in minor units (8000 = 80.00)."""
        if sku is None:
            self._sku_counter += 1
            sku = f"SKU-{self._sku_counter:03d}"
        data = {
            "name": name, "sku": sku, "category": category, "description": description,
            "cost_price": cost, "selling_price": price, "reorder_level": threshold, "quantity": qty,
        }
        with self.app_db() as db:
            return inventory.create_product(db, user_id, data, now=now)

    def sell(self, user_id, items, notes="", now=None):
        """Record a sale directly. ``items`` is a list of (product_id, quantity)."""
        cart = [{"product_id": pid, "quantity": qty} for pid, qty in items]
        with self.app_db() as db:
            return sales.checkout(db, user_id, cart, notes, now=now)

    # ------------------------------------------------------------- CSV
    @staticmethod
    def parse_csv(response):
        """Return a list of rows (lists of strings) from a CSV response."""
        text = response.get_data(as_text=True)
        if text.startswith("﻿"):
            text = text[1:]
        return list(csv.reader(io.StringIO(text)))
