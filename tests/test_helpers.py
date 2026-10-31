"""Unit tests for the small pure functions in helpers.py (no database needed)."""

import unittest

import helpers
import inventory
import validators


class MoneyTests(unittest.TestCase):
    def test_parse_money_accepts_valid_amounts(self):
        cases = {"0": 0, "125": 12500, "125.5": 12550, "125.50": 12550, "1,250.50": 125050,
                 " 7 ": 700, "0.05": 5, "999999999.99": 99_999_999_999}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(helpers.parse_money(text), expected)

    def test_parse_money_rejects_bad_amounts(self):
        for text in ["", "   ", "-1", "-0.50", "abc", "1.234", "1e3", "1.2.3", "12,34a", "١٢",
                     "1000000000", "NaN", "inf", None]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                helpers.parse_money(text)

    def test_format_money_uses_integers_only(self):
        self.assertEqual(helpers.format_money(0), "0.00")
        self.assertEqual(helpers.format_money(5), "0.05")
        self.assertEqual(helpers.format_money(12550, "PKR"), "PKR 125.50")
        self.assertEqual(helpers.format_money(123456789, "PKR"), "PKR 1,234,567.89")
        self.assertEqual(helpers.format_money(-5050, "PKR"), "-PKR 50.50")
        self.assertEqual(helpers.money_input_value(12550), "125.50")

    def test_money_has_no_floating_point_drift(self):
        # 0.1 + 0.2 is 0.30000000000000004 in floating point; in minor units it is exact.
        self.assertEqual(helpers.parse_money("0.10") + helpers.parse_money("0.20"), helpers.parse_money("0.30"))

    def test_parse_whole_number(self):
        self.assertEqual(helpers.parse_whole_number("42", label="Quantity"), 42)
        for text in ["", "-1", "1.5", "abc", "99999999999", "١"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                helpers.parse_whole_number(text, label="Quantity")
        with self.assertRaises(ValueError):
            helpers.parse_whole_number("0", label="Quantity", minimum=1)


class MiscHelperTests(unittest.TestCase):
    def test_paginate(self):
        page = helpers.paginate(total=25, page=3, per_page=10)
        self.assertEqual((page["pages"], page["offset"], page["first"], page["last"]), (3, 20, 21, 25))
        self.assertEqual(helpers.paginate(25, 99, 10)["page"], 3)   # too high -> last page
        self.assertEqual(helpers.paginate(25, -4, 10)["page"], 1)   # too low -> first page
        empty = helpers.paginate(0, 1, 10)
        self.assertEqual((empty["pages"], empty["first"], empty["last"]), (1, 0, 0))

    def test_like_pattern_escapes_wildcards(self):
        self.assertEqual(helpers.like_pattern("100%"), "%100\\%%")
        self.assertEqual(helpers.like_pattern("a_b"), "%a\\_b%")

    def test_safe_next_url(self):
        self.assertEqual(helpers.safe_next_url("/products?page=2"), "/products?page=2")
        for bad in ["//evil.example", "https://evil.example", "javascript:alert(1)", "/\\evil.example", "", None]:
            with self.subTest(bad=bad):
                self.assertIsNone(helpers.safe_next_url(bad))

    def test_csv_safe_neutralises_formulas(self):
        for dangerous in ['=HYPERLINK("http://evil")', "+1+1", "-2+3", "@SUM(A1)", " =1+1", "\t=1", "\r=1"]:
            with self.subTest(value=dangerous):
                self.assertTrue(helpers.csv_safe(dangerous).startswith("'"))
        for harmless in ["Notebook", "5 pack", "", None]:
            with self.subTest(value=harmless):
                self.assertFalse(helpers.csv_safe(harmless).startswith("'"))


class StockStatusTests(unittest.TestCase):
    def test_stock_status_rules(self):
        """out = exactly 0; low = above 0 and at or below the threshold; otherwise ok."""
        cases = [(0, 5, "out"), (1, 5, "low"), (5, 5, "low"), (6, 5, "ok"), (100, 5, "ok"),
                 (0, 0, "out"), (1, 0, "ok")]
        for quantity, threshold, expected in cases:
            with self.subTest(quantity=quantity, threshold=threshold):
                self.assertEqual(inventory.stock_status(quantity, threshold), expected)


class ValidatorTests(unittest.TestCase):
    def test_password_rules(self):
        form = {"full_name": "A", "business_name": "B", "email": "a@b.co", "confirm_password": ""}
        for password, valid in [("abcdefg1", True), ("short1", False), ("allletters", False),
                                ("12345678", False), ("a" * 129 + "1", False)]:
            with self.subTest(password=password):
                _, errors = validators.validate_registration({**form, "password": password, "confirm_password": password})
                self.assertEqual("password" not in errors, valid)
