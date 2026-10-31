"""Dashboard, reports, transaction history and CSV exports.

Report tests use fixed timestamps so the expected numbers never depend on the
day the tests are run. The test time zone is Asia/Karachi (UTC+5).
"""

import json
import re
import unittest
from datetime import datetime, timedelta

import database
import helpers
import reports
from tests.base import StockPilotTestCase


def chart_data(html):
    """Read the JSON the page embeds for its charts."""
    match = re.search(r'<script type="application/json" id="chart-data">(.*?)</script>', html, re.S)
    assert match, "page has no chart data block"
    return json.loads(match.group(1))


class ReportFixture(StockPilotTestCase):
    """One product (price 100.00, cost 60.00) and three sales on known days.

    UTC time              Karachi time          quantity  revenue   profit
    2026-01-10 08:00      10 Jan 13:00          1         100.00    40.00
    2026-01-10 20:00      11 Jan 01:00 (!)      2         200.00    80.00
    2026-01-12 06:00      12 Jan 11:00          3         300.00    120.00
    """

    def setUp(self):
        super().setUp()
        self.owner, self.owner_id = self.make_user()
        self.pid = self.add_product(self.owner_id, name="Report Widget", sku="RW-1", cost=6000, price=10000,
                                    qty=100, now=datetime(2026, 1, 1, 0, 0, 0))
        for when, qty in [(datetime(2026, 1, 10, 8, 0, 0), 1), (datetime(2026, 1, 10, 20, 0, 0), 2),
                          (datetime(2026, 1, 12, 6, 0, 0), 3)]:
            self.sell(self.owner_id, [(self.pid, qty)], now=when)

    def report(self, start, end):
        with self.app_db() as db:
            start_utc, end_utc = helpers.day_bounds_utc(helpers.parse_date(start), helpers.parse_date(end))
            return (reports.sales_totals(db, self.owner_id, start_utc, end_utc),
                    reports.daily_breakdown(db, self.owner_id, helpers.parse_date(start), helpers.parse_date(end)))


class ReportCalculationTests(ReportFixture):
    def test_totals_for_the_whole_range(self):
        totals, _ = self.report("2026-01-10", "2026-01-12")
        self.assertEqual((totals["sales_count"], totals["units"], totals["revenue"], totals["profit"]),
                         (3, 6, 60000, 24000))

    def test_range_is_inclusive_of_both_end_dates(self):
        totals, _ = self.report("2026-01-12", "2026-01-12")
        self.assertEqual((totals["sales_count"], totals["revenue"]), (1, 30000))
        totals, _ = self.report("2026-01-10", "2026-01-10")
        self.assertEqual((totals["sales_count"], totals["revenue"]), (1, 10000))

    def test_a_sale_just_after_local_midnight_belongs_to_the_next_local_day(self):
        totals, daily = self.report("2026-01-11", "2026-01-11")
        self.assertEqual((totals["sales_count"], totals["revenue"]), (1, 20000))
        self.assertEqual(daily[0]["revenue"], 20000)

    def test_range_with_no_sales_is_all_zero(self):
        totals, daily = self.report("2025-12-01", "2025-12-05")
        self.assertEqual((totals["sales_count"], totals["units"], totals["revenue"], totals["profit"]), (0, 0, 0, 0))
        self.assertEqual(len(daily), 5)
        self.assertTrue(all(day["revenue"] == 0 and day["sales_count"] == 0 for day in daily))

    def test_daily_breakdown_is_zero_filled_and_ordered(self):
        _, daily = self.report("2026-01-09", "2026-01-13")
        self.assertEqual([d["date"].isoformat() for d in daily],
                         ["2026-01-09", "2026-01-10", "2026-01-11", "2026-01-12", "2026-01-13"])
        self.assertEqual([d["revenue"] for d in daily], [0, 10000, 20000, 30000, 0])
        self.assertEqual([d["profit"] for d in daily], [0, 4000, 8000, 12000, 0])
        self.assertEqual([d["units"] for d in daily], [0, 1, 2, 3, 0])
        self.assertEqual([d["sales_count"] for d in daily], [0, 1, 1, 1, 0])

    def test_daily_totals_add_up_to_the_range_total(self):
        totals, daily = self.report("2026-01-01", "2026-01-31")
        self.assertEqual(sum(d["revenue"] for d in daily), totals["revenue"])
        self.assertEqual(sum(d["profit"] for d in daily), totals["profit"])

    def test_profit_uses_the_cost_at_the_time_of_sale(self):
        self.post(self.owner, f"/products/{self.pid}/edit", self.product_form(
            sku="RW-1", name="Report Widget", cost_price="99", selling_price="150"))
        totals, _ = self.report("2026-01-10", "2026-01-12")
        self.assertEqual((totals["revenue"], totals["profit"]), (60000, 24000))   # unchanged by the price edit

    def test_best_sellers(self):
        other = self.add_product(self.owner_id, name="Other Thing", sku="OT-1", cost=100, price=500, qty=50,
                                 now=datetime(2026, 1, 1))
        self.sell(self.owner_id, [(other, 10)], now=datetime(2026, 1, 11, 6, 0, 0))
        with self.app_db() as db:
            start_utc, end_utc = helpers.day_bounds_utc(helpers.parse_date("2026-01-10"), helpers.parse_date("2026-01-12"))
            ranking = reports.best_sellers(db, self.owner_id, start_utc, end_utc)
            self.assertEqual([(r["name"], r["units"], r["revenue"], r["profit"]) for r in ranking],
                             [("Other Thing", 10, 5000, 4000), ("Report Widget", 6, 60000, 24000)])
            narrow = reports.best_sellers(db, self.owner_id, *helpers.day_bounds_utc(
                helpers.parse_date("2026-01-12"), helpers.parse_date("2026-01-12")))
            self.assertEqual([r["name"] for r in narrow], ["Report Widget"])
            self.assertEqual(len(reports.best_sellers(db, self.owner_id, start_utc, end_utc, limit=1)), 1)

    def test_best_sellers_still_include_archived_products(self):
        self.post(self.owner, f"/products/{self.pid}/archive")
        with self.app_db() as db:
            start_utc, end_utc = helpers.day_bounds_utc(helpers.parse_date("2026-01-10"), helpers.parse_date("2026-01-12"))
            self.assertEqual([r["name"] for r in reports.best_sellers(db, self.owner_id, start_utc, end_utc)],
                             ["Report Widget"])

    def test_other_users_sales_are_never_counted(self):
        other, other_id = self.make_user("other@example.com")
        other_product = self.add_product(other_id, qty=50, price=99999)
        self.sell(other_id, [(other_product, 5)], now=datetime(2026, 1, 11, 6, 0, 0))
        totals, _ = self.report("2026-01-10", "2026-01-12")
        self.assertEqual(totals["revenue"], 60000)


class ReportRangeParsingTests(StockPilotTestCase):
    def setUp(self):
        super().setUp()
        self.owner, self.owner_id = self.make_user()

    def parse(self, start, end):
        with self.app.app_context():
            return reports.parse_report_range(start, end)

    def test_default_is_the_last_thirty_days_including_today(self):
        with self.app.app_context():
            today = helpers.local_today()
            start, end, error = reports.parse_report_range("", "")
        self.assertEqual((end, (end - start).days, error), (today, 29, None))

    def test_valid_range(self):
        start, end, error = self.parse("2026-02-01", "2026-02-28")
        self.assertEqual((start.isoformat(), end.isoformat(), error), ("2026-02-01", "2026-02-28", None))

    def test_a_single_day_is_valid(self):
        start, end, error = self.parse("2026-02-01", "2026-02-01")
        self.assertEqual((start, end, error), (start, start, None))

    def test_invalid_ranges_fall_back_with_a_message(self):
        cases = {
            "garbage": ("hello", "world", "valid dates"),
            "only one date": ("2026-02-01", "", "both dates"),
            "impossible date": ("2026-02-30", "2026-03-01", "valid dates"),
            "reversed": ("2026-03-01", "2026-02-01", "must not be after"),
            "too long": ("2024-01-01", "2026-01-01", "at most 366 days"),
        }
        for label, (start_text, end_text, expected) in cases.items():
            with self.subTest(label):
                start, end, error = self.parse(start_text, end_text)
                self.assertIn(expected, error)
                self.assertEqual((end - start).days, 29)   # fell back to the default window

    def test_exactly_366_days_is_allowed(self):
        _, _, error = self.parse("2026-01-01", "2027-01-01")
        self.assertIsNone(error)

    def test_page_shows_the_message_and_does_not_crash(self):
        for query in ["from=bad&to=worse", "from=2026-03-01&to=2026-02-01", "from=2020-01-01&to=2026-01-01"]:
            with self.subTest(query):
                response = self.owner.get(f"/reports?{query}")
                self.assertEqual(response.status_code, 200)
                self.assertIn("Showing the last 30 days instead", response.get_data(as_text=True))


class ReportsPageTests(ReportFixture):
    def test_report_page_shows_the_real_numbers(self):
        html = self.owner.get("/reports?from=2026-01-10&to=2026-01-12").get_data(as_text=True)
        for expected in ["PKR 600.00", "PKR 240.00", "Report Widget"]:
            self.assertIn(expected, html)

    def test_chart_data_matches_the_database(self):
        data = chart_data(self.owner.get("/reports?from=2026-01-10&to=2026-01-12").get_data(as_text=True))
        self.assertEqual(data["daily"]["labels"], ["10 Jan", "11 Jan", "12 Jan"])
        self.assertEqual(data["daily"]["revenue"], [10000, 20000, 30000])
        self.assertEqual(data["daily"]["profit"], [4000, 8000, 12000])
        self.assertTrue(data["daily"]["has_data"])
        self.assertEqual((data["best_sellers"]["labels"], data["best_sellers"]["values"]), (["Report Widget"], [6]))
        self.assertEqual(data["currency"], "PKR")

    def test_empty_range_marks_the_chart_as_empty_instead_of_inventing_data(self):
        html = self.owner.get("/reports?from=2025-01-01&to=2025-01-07").get_data(as_text=True)
        self.assertFalse(chart_data(html)["daily"]["has_data"])
        self.assertIn("No sales in this date range", html)
        self.assertIn("Nothing sold in this range", html)

    def test_report_requires_login(self):
        response = self.app.test_client().get("/reports")
        self.assertEqual(response.status_code, 302)

    def test_low_stock_section_lists_only_active_products_at_or_below_threshold(self):
        self.add_product(self.owner_id, name="Nearly Gone", qty=2, threshold=5)
        self.add_product(self.owner_id, name="Totally Gone", qty=0, threshold=5)
        self.add_product(self.owner_id, name="Plenty Left", qty=50, threshold=5)
        archived = self.add_product(self.owner_id, name="Archived Low", qty=1, threshold=5)
        self.post(self.owner, f"/products/{archived}/archive")
        self.clear_flashes(self.owner)
        with self.app_db() as db:
            rows = reports.low_stock_report(db, self.owner_id)
        self.assertEqual([r["name"] for r in rows], ["Totally Gone", "Nearly Gone"])   # out of stock first
        self.assertEqual([r["shortfall"] for r in rows], [5, 3])
        html = self.owner.get("/reports").get_data(as_text=True)
        self.assertIn("Nearly Gone", html)
        self.assertNotIn("Plenty Left", html)
        self.assertNotIn("Archived Low", html)

    def test_inventory_valuation_uses_active_products_only(self):
        # fixture product: 100 - 6 = 94 units left
        self.add_product(self.owner_id, name="Valued", cost=1000, price=1500, qty=10)
        archived = self.add_product(self.owner_id, name="Hidden Value", cost=999999, price=999999, qty=10)
        self.post(self.owner, f"/products/{archived}/archive")
        with self.app_db() as db:
            totals = reports.inventory_totals(db, self.owner_id)
        self.assertEqual(totals["total_units"], 94 + 10)
        self.assertEqual(totals["cost_value"], 94 * 6000 + 10 * 1000)
        self.assertEqual(totals["retail_value"], 94 * 10000 + 10 * 1500)

    def test_category_distribution_groups_categories_regardless_of_capitals(self):
        self.add_product(self.owner_id, name="One", category="Drinks", qty=4)
        self.add_product(self.owner_id, name="Two", category="drinks", qty=6)
        self.add_product(self.owner_id, name="Three", category="Empty Shelf", qty=0)
        with self.app_db() as db:
            rows = reports.category_distribution(db, self.owner_id)
        by_name = {r["category"].lower(): r["units"] for r in rows}
        self.assertEqual(by_name["drinks"], 10)
        self.assertNotIn("empty shelf", by_name)   # zero units are left out of the chart


class DashboardTests(StockPilotTestCase):
    def setUp(self):
        super().setUp()
        self.owner, self.owner_id = self.make_user()

    def test_dashboard_for_a_brand_new_account_is_empty_but_works(self):
        response = self.owner.get("/")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("No sales recorded", html)
        self.assertIn("No stock yet", html)
        data = chart_data(html)
        self.assertFalse(data["week"]["has_data"])
        self.assertEqual(len(data["week"]["labels"]), 7)
        self.assertEqual(data["week"]["revenue"], [0] * 7)

    def test_today_figures_exclude_older_sales(self):
        pid = self.add_product(self.owner_id, cost=6000, price=10000, qty=100)
        self.sell(self.owner_id, [(pid, 2)])                                              # now
        self.sell(self.owner_id, [(pid, 5)], now=helpers_now() - timedelta(days=3))        # three days ago
        self.sell(self.owner_id, [(pid, 7)], now=helpers_now() - timedelta(days=45))       # outside the 30-day window
        with self.app_db() as db:
            data = reports.dashboard_data(db, self.owner_id)
        self.assertEqual((data["today"]["sales_count"], data["today"]["revenue"], data["today"]["profit"]),
                         (1, 20000, 8000))
        self.assertEqual(sum(d["revenue"] for d in data["week"]), 70000)   # today + 3 days ago
        self.assertEqual(len(data["week"]), 7)
        self.assertEqual([(r["units"]) for r in data["best_sellers"]], [7])  # 2 + 5; the 45-day-old sale is excluded
        self.assertEqual(len(data["recent_sales"]), 3)

    def test_kpis_match_the_inventory(self):
        self.add_product(self.owner_id, name="One", cost=1000, price=2000, qty=10, threshold=5)
        self.add_product(self.owner_id, name="Two", cost=500, price=900, qty=3, threshold=5)
        self.add_product(self.owner_id, name="Three", cost=500, price=900, qty=0, threshold=5)
        # the dashboard shows the counts ...
        html = self.owner.get("/").get_data(as_text=True)
        for label, value in [("Active products", 3), ("Units in stock", 13), ("Low-stock products", 1),
                             ("Out-of-stock products", 1)]:
            self.assertRegex(html, rf'{label}</p><p class="kpi-value">{value}</p>')
        # ... and the reports page shows the stock value: 10 x 10.00 + 3 x 5.00 + 0 x 5.00 = 115.00 at cost
        self.assertIn("PKR 115.00", self.owner.get("/reports").get_data(as_text=True))
        with self.app_db() as db:
            totals = reports.inventory_totals(db, self.owner_id)
        self.assertEqual((totals["active_products"], totals["total_units"], totals["low_stock"], totals["out_of_stock"]),
                         (3, 13, 1, 1))

    def test_dashboard_requires_login(self):
        response = self.app.test_client().get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_dashboard_does_not_show_other_users_data(self):
        other, other_id = self.make_user("other@example.com", business_name="Other Biz")
        pid = self.add_product(other_id, name="Foreign Product", qty=10)
        self.sell(other_id, [(pid, 2)])
        html = self.owner.get("/").get_data(as_text=True)
        self.assertNotIn("Foreign Product", html)
        self.assertIn("No sales recorded", html)


def helpers_now():
    return helpers.utc_now()


class TransactionHistoryTests(StockPilotTestCase):
    def setUp(self):
        super().setUp()
        self.owner, self.owner_id = self.make_user()
        self.pen = self.add_product(self.owner_id, name="History Pen", sku="HP-1", qty=20, now=datetime(2026, 1, 1, 9, 0, 0))
        self.cup = self.add_product(self.owner_id, name="History Cup", sku="HC-1", qty=20, now=datetime(2026, 1, 1, 9, 0, 0))
        self.sell(self.owner_id, [(self.pen, 2)], now=datetime(2026, 1, 5, 9, 0, 0))
        self.post(self.owner, f"/products/{self.cup}/stock", {"action": "adjust_out", "quantity": "1", "notes": "dropped it"})

    def html(self, query=""):
        return self.owner.get(f"/transactions?{query}").get_data(as_text=True)

    def test_shows_all_kinds_of_movement(self):
        html = self.html()
        for expected in ["History Pen", "History Cup", "Sale", "Restock", "dropped it", "S-000001"]:
            self.assertIn(expected, html)

    def test_filter_by_type(self):
        html = self.html("type=SALE")
        self.assertIn("History Pen", html)
        self.assertNotIn("dropped it", html)
        self.assertIn("dropped it", self.html("type=ADJUSTMENT_OUT"))
        self.assertIn("History Pen", self.html("type=nonsense"))   # unknown type is ignored

    def test_filter_by_product(self):
        html = self.html(f"product_id={self.cup}")
        self.assertIn("dropped it", html)
        self.assertNotIn("S-000001", html)

    def test_filter_by_date(self):
        html = self.html("from=2026-01-05&to=2026-01-05")
        self.assertIn("S-000001", html)
        self.assertNotIn("Initial stock", html)

    def test_search_notes_and_names(self):
        self.assertIn("dropped it", self.html("q=dropped"))
        self.assertNotIn("S-000001", self.html("q=dropped"))
        self.assertIn("S-000001", self.html("q=HP-1"))

    def test_bad_dates_do_not_crash(self):
        self.assertEqual(self.owner.get("/transactions?from=nope").status_code, 200)

    def test_requires_login(self):
        self.assertEqual(self.app.test_client().get("/transactions").status_code, 302)


class CsvExportTests(ReportFixture):
    def get_csv(self, path, client=None):
        response = (client or self.owner).get(path)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:300])
        return response, self.parse_csv(response)

    def test_products_export(self):
        response, rows = self.get_csv("/export/products.csv")
        self.assertTrue(response.mimetype.startswith("text/csv"))
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn(".csv", response.headers["Content-Disposition"])
        self.assertTrue(response.get_data(as_text=True).startswith("﻿"))   # BOM for Excel
        self.assertEqual(rows[0][:9], ["name", "sku", "category", "description", "cost_price", "selling_price",
                                       "quantity", "low_stock_threshold", "status"])
        self.assertEqual(rows[1][:9], ["Report Widget", "RW-1", "Stationery", "", "60.00", "100.00", "94", "5", "active"])
        self.assertEqual(len(rows), 2)

    def test_products_export_includes_archived_products_marked_as_such(self):
        self.post(self.owner, f"/products/{self.pid}/archive")
        _, rows = self.get_csv("/export/products.csv")
        self.assertEqual(rows[1][8], "archived")

    def test_sales_export(self):
        _, rows = self.get_csv("/export/sales.csv")
        self.assertEqual(rows[0], ["receipt", "date_utc", "product", "sku", "quantity", "unit_price", "unit_cost",
                                   "line_total", "line_gross_profit", "sale_notes"])
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[1], ["S-000001", "2026-01-10 08:00:00", "Report Widget", "RW-1", "1", "100.00", "60.00",
                                   "100.00", "40.00", ""])
        self.assertEqual([r[0] for r in rows[1:]], ["S-000001", "S-000002", "S-000003"])
        self.assertEqual(sum(float(r[7]) for r in rows[1:]), 600.0)

    def test_sales_export_date_filter_uses_local_days(self):
        _, rows = self.get_csv("/export/sales.csv?from=2026-01-11&to=2026-01-11")
        self.assertEqual([r[0] for r in rows[1:]], ["S-000002"])
        _, rows = self.get_csv("/export/sales.csv?from=2026-01-12")
        self.assertEqual([r[0] for r in rows[1:]], ["S-000003"])
        _, rows = self.get_csv("/export/sales.csv?to=2026-01-10")
        self.assertEqual([r[0] for r in rows[1:]], ["S-000001"])

    def test_exports_reject_invalid_dates(self):
        for path in ["/export/sales.csv?from=garbage", "/export/transactions.csv?to=2026-99-99",
                     "/export/sales.csv?from=2026-02-01&to=2026-01-01"]:
            with self.subTest(path):
                self.assertEqual(self.owner.get(path).status_code, 400)

    def test_transactions_export(self):
        _, rows = self.get_csv("/export/transactions.csv")
        self.assertEqual(rows[0], ["date_utc", "product", "sku", "type", "quantity", "unit_price", "total_amount",
                                   "stock_after", "receipt", "notes"])
        self.assertEqual([r[3] for r in rows[1:]], ["RESTOCK", "SALE", "SALE", "SALE"])   # oldest first
        self.assertEqual(rows[2][:9], ["2026-01-10 08:00:00", "Report Widget", "RW-1", "SALE", "1", "100.00", "100.00", "99", "S-000001"])
        self.assertEqual(rows[1][8], "")   # restocks have no receipt

    def test_exports_neutralise_spreadsheet_formulas(self):
        evil = self.add_product(self.owner_id, name='=HYPERLINK("http://evil.example","click")', sku="EVIL-1",
                                category="+cmd", description="@SUM(1+1)", qty=5, cost=100, price=300)
        self.sell(self.owner_id, [(evil, 1)], notes="-2+3", now=datetime(2026, 1, 13, 6, 0, 0))
        _, products = self.get_csv("/export/products.csv")
        row = next(r for r in products if r[1] == "EVIL-1")
        self.assertEqual((row[0][0], row[2][0], row[3][0]), ("'", "'", "'"))
        _, sale_rows = self.get_csv("/export/sales.csv")
        row = next(r for r in sale_rows if r[3] == "EVIL-1")
        self.assertEqual((row[2][0], row[9]), ("'", "'-2+3"))
        _, ledger = self.get_csv("/export/transactions.csv")
        row = next(r for r in ledger if r[2] == "EVIL-1" and r[3] == "SALE")
        self.assertEqual(row[1][0], "'")

    def test_negative_numbers_are_not_mistaken_for_formulas(self):
        loss = self.add_product(self.owner_id, name="Loss Leader", sku="LOSS-1", cost=5000, price=1000, qty=5)
        self.sell(self.owner_id, [(loss, 1)], now=datetime(2026, 1, 13, 6, 0, 0))
        _, rows = self.get_csv("/export/sales.csv")
        row = next(r for r in rows if r[3] == "LOSS-1")
        self.assertEqual(row[8], "-40.00")   # real negative profit, left as a plain number

    def test_exports_only_contain_the_logged_in_users_data(self):
        other, other_id = self.make_user("other@example.com")
        mine = "Report Widget"
        theirs = self.add_product(other_id, name="Their Secret Product", sku="SECRET-1", qty=9)
        self.sell(other_id, [(theirs, 1)], now=datetime(2026, 1, 11, 6, 0, 0))
        for path in ["/export/products.csv", "/export/sales.csv", "/export/transactions.csv"]:
            with self.subTest(path):
                text = self.owner.get(path).get_data(as_text=True)
                self.assertIn(mine, text)
                self.assertNotIn("Their Secret Product", text)
                self.assertNotIn("SECRET-1", text)
        other_text = other.get("/export/sales.csv").get_data(as_text=True)
        self.assertIn("Their Secret Product", other_text)
        self.assertNotIn(mine, other_text)

    def test_exports_require_login(self):
        anonymous = self.app.test_client()
        for path in ["/export/products.csv", "/export/sales.csv", "/export/transactions.csv"]:
            with self.subTest(path):
                response = anonymous.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login", response.headers["Location"])

    def test_empty_account_exports_only_headers(self):
        other, _ = self.make_user("empty@example.com")
        for path in ["/export/products.csv", "/export/sales.csv", "/export/transactions.csv"]:
            with self.subTest(path):
                _, rows = self.get_csv(path, other)
                self.assertEqual(len(rows), 1)


class ExportMoneyConsistencyTests(ReportFixture):
    def test_exported_sales_add_up_to_the_report_total(self):
        response = self.owner.get("/export/sales.csv")
        rows = self.parse_csv(response)[1:]
        exported_revenue = sum(round(float(r[7]) * 100) for r in rows)
        exported_profit = sum(round(float(r[8]) * 100) for r in rows)
        totals, _ = self.report("2026-01-01", "2026-12-31")
        self.assertEqual((exported_revenue, exported_profit), (totals["revenue"], totals["profit"]))


if __name__ == "__main__":
    unittest.main()
