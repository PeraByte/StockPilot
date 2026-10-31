"""Authentication, sessions, CSRF, security headers and the settings page."""

import os
import re
import tempfile
import unittest
from unittest import mock

import app as app_module
from tests.base import PASSWORD, StockPilotTestCase


class RegistrationTests(StockPilotTestCase):
    def test_successful_registration_logs_the_user_in(self):
        response = self.register(self.client)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")
        users = self.rows("SELECT * FROM users")
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["email"], "owner@example.com")
        self.assertEqual(users[0]["business_name"], "Owner Shop")
        self.assertEqual(users[0]["currency_label"], "PKR")
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_password_is_hashed_never_stored_in_plain_text(self):
        self.register(self.client)
        stored = self.scalar("SELECT password_hash FROM users")
        self.assertNotIn(PASSWORD, stored)
        self.assertRegex(stored, r"^(scrypt|pbkdf2):")

    def test_email_is_normalised_to_lower_case(self):
        self.register(self.client, email="  Owner@Example.COM ")
        self.assertEqual(self.scalar("SELECT email FROM users"), "owner@example.com")

    def test_invalid_registrations_are_rejected(self):
        bad_inputs = {
            "missing name": dict(full_name=""),
            "missing business": dict(business_name="   "),
            "bad email": dict(email="not-an-email"),
            "short password": dict(password="abc1"),
            "no digit": dict(password="onlyletters"),
            "mismatch": dict(confirm="different123"),
        }
        for label, overrides in bad_inputs.items():
            with self.subTest(label):
                client = self.app.test_client()
                response = self.register(client, **overrides)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM users"), 0)

    def test_registration_form_keeps_typed_values_but_never_the_password(self):
        response = self.register(self.client, full_name="Keep Me", confirm="mismatch999")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 400)
        self.assertIn('value="Keep Me"', html)
        self.assertIn("Passwords do not match", html)
        self.assertNotIn(PASSWORD, html)

    def test_duplicate_email_is_rejected_case_insensitively(self):
        self.register(self.client)
        other = self.app.test_client()
        response = self.register(other, email="OWNER@example.com", full_name="Someone Else")
        self.assertEqual(response.status_code, 400)
        self.assertIn("already exists", response.get_data(as_text=True))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM users"), 1)


class LoginLogoutTests(StockPilotTestCase):
    def setUp(self):
        super().setUp()
        self.make_user()

    def test_successful_login(self):
        client = self.app.test_client()
        response = self.login(client)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")
        self.assertEqual(client.get("/").status_code, 200)

    def test_login_ignores_email_capitalisation(self):
        client = self.app.test_client()
        self.assertEqual(self.login(client, email="OWNER@Example.com").status_code, 302)

    def test_wrong_password_and_unknown_email_get_the_same_message(self):
        messages = []
        for email, password in [("owner@example.com", "wrong-password1"), ("nobody@example.com", PASSWORD)]:
            client = self.app.test_client()
            response = self.login(client, email=email, password=password)
            self.assertEqual(response.status_code, 401)
            html = response.get_data(as_text=True)
            self.assertIn("Incorrect email or password.", html)
            messages.append(re.search(r'class="form-error".*?</div>', html, re.S).group(0))
            self.assertEqual(client.get("/").status_code, 302)  # still logged out
        self.assertEqual(messages[0], messages[1])  # nothing reveals which emails have accounts

    def test_empty_login_fields_are_rejected(self):
        response = self.login(self.app.test_client(), email="", password="")
        self.assertEqual(response.status_code, 400)

    def test_private_pages_redirect_anonymous_visitors_to_login(self):
        paths = ["/", "/products", "/products/new", "/products/1", "/products/1/edit", "/sales",
                 "/sales/history", "/sales/1", "/transactions", "/reports", "/settings",
                 "/export/products.csv", "/export/sales.csv", "/export/transactions.csv"]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login", response.headers["Location"])

    def test_private_mutations_reject_anonymous_visitors(self):
        for path in ["/products/1/archive", "/products/1/restore", "/products/1/stock", "/settings/profile", "/settings/password"]:
            with self.subTest(path=path):
                response = self.post(self.client, path, {})
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login", response.headers["Location"])

    def test_api_endpoints_answer_401_json_when_logged_out(self):
        response = self.client.get("/api/products/search")
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.get_json()["ok"])
        response = self.post_json(self.client, "/api/checkout", {"items": []})
        self.assertEqual(response.status_code, 401)

    def test_logout_ends_the_session(self):
        client = self.app.test_client()
        self.login(client)
        self.assertEqual(client.get("/").status_code, 200)
        response = self.post(client, "/logout")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        self.assertEqual(client.get("/").status_code, 302)

    def test_logout_requires_post(self):
        client = self.app.test_client()
        self.login(client)
        self.assertEqual(client.get("/logout").status_code, 405)
        self.assertEqual(client.get("/").status_code, 200)  # GET did not log out

    def test_login_replaces_the_old_session(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["planted"] = "attacker-value"
        self.login(client)
        with client.session_transaction() as session:
            self.assertNotIn("planted", session)
            self.assertIn("user_id", session)

    def test_next_parameter_only_allows_local_paths(self):
        client = self.app.test_client()
        response = self.login(client, next="//evil.example/steal")
        self.assertEqual(response.headers["Location"], "/")
        client = self.app.test_client()
        response = self.login(client, next="/products")
        self.assertEqual(response.headers["Location"], "/products")

    def test_visiting_login_while_logged_in_redirects_to_dashboard(self):
        client = self.app.test_client()
        self.login(client)
        self.assertEqual(client.get("/login").status_code, 302)


class CsrfAndHeaderTests(StockPilotTestCase):
    def test_post_without_a_csrf_token_is_rejected(self):
        response = self.client.post("/login", data={"email": "a@b.co", "password": PASSWORD})
        self.assertEqual(response.status_code, 400)

    def test_post_with_a_wrong_csrf_token_is_rejected(self):
        self.client.get("/login")
        response = self.client.post("/login", data={"email": "a@b.co", "password": PASSWORD, "csrf_token": "forged"})
        self.assertEqual(response.status_code, 400)

    def test_token_rendered_in_the_form_works(self):
        html = self.client.get("/register").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        response = self.client.post("/register", data={
            "csrf_token": token, "full_name": "Real Form", "business_name": "Shop",
            "email": "real@example.com", "password": PASSWORD, "confirm_password": PASSWORD,
        })
        self.assertEqual(response.status_code, 302)

    def test_json_endpoints_also_require_the_csrf_header(self):
        client, _ = self.make_user()
        response = client.post("/api/checkout", json={"items": [{"product_id": 1, "quantity": 1}]})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])

    def test_security_headers_and_cookie_flags(self):
        response = self.client.get("/login")
        csp = response.headers["Content-Security-Policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_pages_contain_no_inline_scripts_or_styles(self):
        client, _ = self.make_user()
        for path in ["/", "/products", "/products/new", "/sales", "/reports", "/settings"]:
            html = client.get(path).get_data(as_text=True)
            with self.subTest(path=path):
                self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)(?![^>]*application/json)[^>]*>", html))
                self.assertNotRegex(html, r'\sstyle="')
                self.assertNotRegex(html, r'\sonclick=')

    def test_password_hash_never_appears_in_pages(self):
        client, _ = self.make_user()
        stored = self.scalar("SELECT password_hash FROM users")
        for path in ["/", "/settings", "/products", "/reports"]:
            self.assertNotIn(stored, client.get(path).get_data(as_text=True))

    def test_server_errors_do_not_leak_a_traceback(self):
        client, _ = self.make_user()
        self.app.config["TESTING"] = False
        self.app.config["PROPAGATE_EXCEPTIONS"] = False
        with mock.patch("reports.dashboard_data", side_effect=RuntimeError("secret internal detail")):
            response = client.get("/")
        self.assertEqual(response.status_code, 500)
        html = response.get_data(as_text=True)
        self.assertIn("Something went wrong", html)
        self.assertNotIn("secret internal detail", html)
        self.assertNotIn("Traceback", html)


class SecretKeyTests(unittest.TestCase):
    """Regression tests: Flask's default SECRET_KEY is None, which once left sessions broken."""

    def test_a_key_file_is_created_once_and_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            fake_app = mock.Mock(instance_path=folder)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("STOCKPILOT_SECRET_KEY", None)
                first = app_module.load_secret_key(fake_app)
                second = app_module.load_secret_key(fake_app)
            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 32)
            if os.name == "posix":
                self.assertEqual(os.stat(os.path.join(folder, "secret_key")).st_mode & 0o777, 0o600)

    def test_workers_starting_at_the_same_moment_all_get_the_same_complete_key(self):
        """Regression: a worker used to be able to read the key file while another was still writing it."""
        import threading
        for _ in range(60):
            with tempfile.TemporaryDirectory() as folder:
                fake_app, barrier, keys = mock.Mock(instance_path=folder), threading.Barrier(8), []

                def worker():
                    barrier.wait()
                    keys.append(app_module.load_secret_key(fake_app))

                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("STOCKPILOT_SECRET_KEY", None); os.environ.pop("SECRET_KEY", None)
                    threads = [threading.Thread(target=worker) for _ in range(8)]
                    for thread in threads: thread.start()
                    for thread in threads: thread.join()
                self.assertEqual(len(set(keys)), 1, keys)
                self.assertEqual(len(keys[0]), 64)
                self.assertEqual([f for f in os.listdir(folder) if f.endswith(".tmp")], [])   # no leftovers

    def test_environment_variable_wins(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"STOCKPILOT_SECRET_KEY": "a-long-random-key-0123456789abcdef"}):
            self.assertEqual(app_module.load_secret_key(mock.Mock(instance_path=folder)), "a-long-random-key-0123456789abcdef")

    def test_create_app_without_a_configured_key_still_has_working_sessions(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"STOCKPILOT_SECRET_KEY": "a-long-random-key-0123456789abcdef"}):
            app = app_module.create_app({"TESTING": True, "DATABASE": os.path.join(folder, "t.db")})
            self.assertEqual(app.config["SECRET_KEY"], "a-long-random-key-0123456789abcdef")
            self.assertEqual(app.test_client().get("/login").status_code, 200)

    def test_short_or_guessable_keys_are_refused(self):
        for weak in ["7272", "secret", "changeme", "x" * 15]:
            with self.subTest(key=weak), tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"STOCKPILOT_SECRET_KEY": weak}):
                with self.assertRaisesRegex(RuntimeError, "too short"):
                    app_module.load_secret_key(mock.Mock(instance_path=folder))

    def test_plain_secret_key_variable_is_also_accepted(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"SECRET_KEY": "a-long-random-key-0123456789abcdef"}):
            os.environ.pop("STOCKPILOT_SECRET_KEY", None)
            self.assertEqual(app_module.load_secret_key(mock.Mock(instance_path=folder)), "a-long-random-key-0123456789abcdef")

    def test_serverless_platforms_must_get_the_key_from_the_environment(self):
        """A key FILE cannot work there: each running copy would invent its own key and log everyone out."""
        for marker in ["VERCEL", "AWS_LAMBDA_FUNCTION_NAME"]:
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {marker: "1"}):
                os.environ.pop("STOCKPILOT_SECRET_KEY", None); os.environ.pop("SECRET_KEY", None)
                with self.assertRaisesRegex(RuntimeError, "STOCKPILOT_SECRET_KEY is not set"):
                    app_module.load_secret_key(mock.Mock(instance_path=folder))
                self.assertFalse(os.path.exists(os.path.join(folder, "secret_key")))  # no stray per-instance key file

    def test_serverless_app_with_a_key_starts_and_uses_a_temporary_instance_folder(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"VERCEL": "1", "STOCKPILOT_SECRET_KEY": "a-long-random-key-0123456789abcdef"}):
            with self.assertLogs(level="WARNING") as logs:     # the ephemeral-database warning
                app = app_module.create_app({"TESTING": True})
            self.assertTrue(app.instance_path.startswith(tempfile.gettempdir()))
            self.assertIn("NOT shared", "\n".join(logs.output))
            self.assertEqual(app.test_client().get("/login").status_code, 200)

    def test_importing_the_module_has_no_side_effects_off_serverless(self):
        self.assertFalse(app_module.running_on_serverless())
        self.assertFalse(hasattr(app_module, "app"))   # created only on hosts that import and look for it


class SessionDurabilityTests(StockPilotTestCase):
    """Regression tests for 'it asks me to log in again after every click' (Vercel/multi-worker hosting)."""

    def worker(self, **overrides):
        """Another running copy of the app - as a second gunicorn worker or serverless instance would be."""
        config = {"TESTING": True, "DATABASE": self.db_path, "SECRET_KEY": "test-secret-key",
                  "TIMEZONE": "Asia/Karachi", "PER_PAGE": 10}
        config.update(overrides)
        return app_module.create_app(config)

    @staticmethod
    def hand_over_cookie(client_from, app_to):
        cookie = client_from.get_cookie("stockpilot_session")
        client = app_to.test_client()
        client.set_cookie("stockpilot_session", cookie.value)
        return client

    def test_login_cookie_is_persistent_not_a_browser_session_cookie(self):
        client = self.app.test_client()
        self.register(client)
        cookies = [c for c in client.get("/").headers.getlist("Set-Cookie") if c.startswith("stockpilot_session=")]
        self.assertTrue(cookies and "expires=" in cookies[-1].lower(), cookies)   # lasts 12 hours, refreshed on each use

    def test_session_survives_many_clicks(self):
        client, _ = self.make_user()
        for path in ["/", "/products", "/products/new", "/sales", "/sales/history", "/transactions", "/reports", "/settings"] * 3:
            self.assertEqual(client.get(path).status_code, 200, path)

    def test_add_product_then_save_works_when_a_different_worker_handles_the_save(self):
        """The exact complaint: open 'Add product' on one copy of the app, press 'Save product' on another."""
        owner, user_id = self.make_user()
        page = owner.get("/products/new")
        self.assertEqual(page.status_code, 200)
        token = self.csrf(owner)
        other_worker = self.hand_over_cookie(owner, self.worker())
        response = other_worker.post("/products/new", data={**self.product_form(), "csrf_token": token})
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:300])
        self.assertTrue(response.headers["Location"].startswith("/products/"))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM products WHERE user_id = ?", (user_id,)), 1)

    def test_workers_with_different_keys_reject_each_others_cookies(self):
        """What happens on Vercel without a shared STOCKPILOT_SECRET_KEY: documented so nobody is surprised."""
        owner, _ = self.make_user()
        stranger = self.hand_over_cookie(owner, self.worker(SECRET_KEY="a-completely-different-secret"))
        response = stranger.get("/products/new")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_workers_with_separate_databases_do_not_share_logins(self):
        """...and why a SQLite file in a per-instance temp folder cannot work, even with a shared key."""
        owner, _ = self.make_user()
        with tempfile.TemporaryDirectory() as other_folder:
            stranger = self.hand_over_cookie(owner, self.worker(DATABASE=os.path.join(other_folder, "other.db")))
            response = stranger.get("/products/new")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])


class SettingsTests(StockPilotTestCase):
    def setUp(self):
        super().setUp()
        self.owner, self.owner_id = self.make_user()

    def test_profile_update(self):
        response = self.post(self.owner, "/settings/profile", {
            "full_name": "New Name", "business_name": "New Shop", "currency_label": "USD"})
        self.assertEqual(response.status_code, 302)
        user = self.rows("SELECT * FROM users")[0]
        self.assertEqual((user["full_name"], user["business_name"], user["currency_label"]), ("New Name", "New Shop", "USD"))
        self.assertIn("USD", self.owner.get("/").get_data(as_text=True))

    def test_invalid_profile_values_are_rejected(self):
        for overrides in [{"full_name": ""}, {"business_name": "x" * 101}, {"currency_label": ""},
                          {"currency_label": "TOO-LONG-LABEL"}, {"currency_label": "a b"}]:
            with self.subTest(overrides=overrides):
                data = {"full_name": "Ok", "business_name": "Ok", "currency_label": "PKR", **overrides}
                self.assertEqual(self.post(self.owner, "/settings/profile", data).status_code, 400)
        self.assertEqual(self.scalar("SELECT full_name FROM users"), "Owner One")

    def test_changing_the_currency_label_does_not_convert_amounts(self):
        product = self.add_product(self.owner_id, price=12000, cost=8000, qty=5)
        sale_id = self.sell(self.owner_id, [(product, 2)])
        before = self.rows("SELECT total_amount, total_profit FROM sales WHERE id = ?", (sale_id,))
        self.post(self.owner, "/settings/profile", {"full_name": "Owner One", "business_name": "Owner Shop", "currency_label": "USD"})
        self.assertEqual(before, self.rows("SELECT total_amount, total_profit FROM sales WHERE id = ?", (sale_id,)))
        self.assertEqual(self.product(product)["selling_price"], 12000)
        receipt = self.owner.get(f"/sales/{sale_id}").get_data(as_text=True)
        self.assertIn("USD 240.00", receipt)

    def test_password_change_requires_the_current_password(self):
        response = self.post(self.owner, "/settings/password", {
            "current_password": "wrong-password1", "new_password": "newpassword1", "confirm_password": "newpassword1"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("current password is incorrect", response.get_data(as_text=True))
        self.assertEqual(self.login(self.app.test_client(), password="newpassword1").status_code, 401)

    def test_password_change_validates_the_new_password(self):
        for new, confirm in [("short1", "short1"), ("newpassword1", "different1"), (PASSWORD, PASSWORD)]:
            with self.subTest(new=new, confirm=confirm):
                response = self.post(self.owner, "/settings/password", {
                    "current_password": PASSWORD, "new_password": new, "confirm_password": confirm})
                self.assertEqual(response.status_code, 400)

    def test_password_change_works_and_old_password_stops_working(self):
        response = self.post(self.owner, "/settings/password", {
            "current_password": PASSWORD, "new_password": "newpassword1", "confirm_password": "newpassword1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.owner.get("/settings").status_code, 200)  # still logged in
        self.assertEqual(self.login(self.app.test_client(), password=PASSWORD).status_code, 401)
        self.assertEqual(self.login(self.app.test_client(), password="newpassword1").status_code, 302)

    def test_settings_only_change_the_logged_in_users_account(self):
        other, _ = self.make_user("other@example.com", "Other Person", "Other Shop")
        self.post(other, "/settings/profile", {"full_name": "Changed", "business_name": "Changed Shop", "currency_label": "EUR"})
        owner_row = self.rows("SELECT * FROM users WHERE email = 'owner@example.com'")[0]
        self.assertEqual((owner_row["full_name"], owner_row["currency_label"]), ("Owner One", "PKR"))


if __name__ == "__main__":
    unittest.main()
