"""Inventory logic: products, stock movements and product listings.

The golden rule of this module: **stock never changes silently**. The only
function that changes ``products.quantity`` is ``apply_stock_movement``, and it
always writes a matching row to the ``transactions`` table (the stock ledger).
Sales, restocks, manual adjustments and a new product's opening stock all go
through it.

Every query includes ``user_id`` so one business can never see or change
another business's data.
"""

import sqlite3

from database import transaction
from helpers import like_pattern, paginate, to_db_time, utc_now

INCREASE_TYPES = ("RESTOCK", "ADJUSTMENT_IN")
DECREASE_TYPES = ("SALE", "ADJUSTMENT_OUT")
TRANSACTION_TYPES = INCREASE_TYPES + DECREASE_TYPES

TRANSACTION_LABELS = {
    "SALE": "Sale",
    "RESTOCK": "Restock",
    "ADJUSTMENT_IN": "Adjustment (+)",
    "ADJUSTMENT_OUT": "Adjustment (−)",
}


class ValidationFailed(Exception):
    """Raised when the database rejects a value the form checks could not catch.

    ``errors`` maps a form field name to a message, e.g. {"sku": "..."}.
    """

    def __init__(self, errors):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


class StockError(Exception):
    """A stock movement was refused (unknown product, archived, or not enough stock)."""


# --------------------------------------------------------------------------
# Stock status
# --------------------------------------------------------------------------

def stock_status(quantity, reorder_level):
    """Return 'out', 'low' or 'ok'.

    * out: quantity is exactly zero
    * low: quantity is above zero but not above the reorder level
    * ok : anything else
    """
    if quantity == 0:
        return "out"
    if quantity <= reorder_level:
        return "low"
    return "ok"


# SQL versions of the same rules, used by listing filters. Keep them in sync
# with stock_status() above - tests check that both agree.
STATUS_SQL = {
    "out": "quantity = 0",
    "low": "quantity > 0 AND quantity <= reorder_level",
    "ok": "quantity > reorder_level",
}


# --------------------------------------------------------------------------
# Reading products
# --------------------------------------------------------------------------

def get_product(db, user_id, product_id):
    """Fetch one product owned by this user (archived included), or None."""
    return db.execute(
        "SELECT * FROM products WHERE id = ? AND user_id = ?", (product_id, user_id)
    ).fetchone()


def list_categories(db, user_id):
    # GROUP BY ... NOCASE so "Drinks" and "drinks" appear as one filter choice.
    rows = db.execute(
        "SELECT category FROM products WHERE user_id = ? AND is_archived = 0 "
        "GROUP BY category COLLATE NOCASE ORDER BY category COLLATE NOCASE",
        (user_id,),
    ).fetchall()
    return [row["category"] for row in rows]


# The sort dropdown sends a key; we map it to a fixed ORDER BY fragment here.
# User input is never pasted into SQL - only these literal strings are.
SORT_OPTIONS = {
    "name_asc": ("Name (A–Z)", "name COLLATE NOCASE ASC"),
    "name_desc": ("Name (Z–A)", "name COLLATE NOCASE DESC"),
    "quantity_asc": ("Quantity (low to high)", "quantity ASC"),
    "quantity_desc": ("Quantity (high to low)", "quantity DESC"),
    "price_asc": ("Price (low to high)", "selling_price ASC"),
    "price_desc": ("Price (high to low)", "selling_price DESC"),
    "created_desc": ("Newest first", "created_at DESC, id DESC"),
    "created_asc": ("Oldest first", "created_at ASC, id ASC"),
}


def list_products(db, user_id, filters, page, per_page):
    """Return (rows, pagination) for the products table.

    ``filters`` keys: q, category, status ('all'|'ok'|'low'|'out'),
    view ('active'|'archived'), sort (a key of SORT_OPTIONS).
    """
    where = ["user_id = ?", "is_archived = ?"]
    params = [user_id, 1 if filters["view"] == "archived" else 0]

    if filters["q"]:
        where.append("(name LIKE ? ESCAPE '\\' OR sku LIKE ? ESCAPE '\\')")
        pattern = like_pattern(filters["q"])
        params += [pattern, pattern]
    if filters["category"]:
        where.append("category = ? COLLATE NOCASE")
        params.append(filters["category"])
    if filters["status"] in STATUS_SQL:
        where.append(f"({STATUS_SQL[filters['status']]})")  # fixed strings from STATUS_SQL

    where_sql = " AND ".join(where)
    total = db.execute(f"SELECT COUNT(*) FROM products WHERE {where_sql}", params).fetchone()[0]
    pagination = paginate(total, page, per_page)

    order_by = SORT_OPTIONS.get(filters["sort"], SORT_OPTIONS["name_asc"])[1]
    rows = db.execute(
        f"SELECT * FROM products WHERE {where_sql} ORDER BY {order_by}, id ASC LIMIT ? OFFSET ?",
        params + [per_page, pagination["offset"]],
    ).fetchall()
    return rows, pagination


def search_for_sale(db, user_id, query, limit=30):
    """Active, in-stock products matching a search - feeds the point of sale."""
    sql = (
        "SELECT id, name, sku, category, selling_price, quantity FROM products "
        "WHERE user_id = ? AND is_archived = 0 AND quantity > 0"
    )
    params = [user_id]
    if query:
        sql += " AND (name LIKE ? ESCAPE '\\' OR sku LIKE ? ESCAPE '\\')"
        pattern = like_pattern(query)
        params += [pattern, pattern]
    sql += " ORDER BY name COLLATE NOCASE LIMIT ?"
    params.append(limit)
    return db.execute(sql, params).fetchall()


def product_sales_summary(db, user_id, product_id):
    """Lifetime units sold and revenue for one product."""
    return db.execute(
        "SELECT COALESCE(SUM(si.quantity), 0) AS units, COALESCE(SUM(si.line_total), 0) AS revenue "
        "FROM sale_items si JOIN sales s ON s.id = si.sale_id "
        "WHERE s.user_id = ? AND si.product_id = ?",
        (user_id, product_id),
    ).fetchone()


def recent_movements(db, user_id, product_id, limit=10):
    return db.execute(
        "SELECT t.*, s.receipt_number FROM transactions t "
        "LEFT JOIN sales s ON s.id = t.sale_id "
        "WHERE t.user_id = ? AND t.product_id = ? "
        "ORDER BY t.created_at DESC, t.id DESC LIMIT ?",
        (user_id, product_id, limit),
    ).fetchall()


# --------------------------------------------------------------------------
# Stock movements - the ONLY code that changes products.quantity
# --------------------------------------------------------------------------

def apply_stock_movement(db, user_id, product_id, movement_type, quantity, unit_price,
                         notes="", sale_id=None, now=None):
    """Change a product's stock by ``quantity`` and record why.

    MUST be called inside ``transaction(db)`` so the stock update and the ledger
    row succeed or fail together. Returns the new stock level.

    The decrease branch contains ``AND quantity >= ?`` so the database itself
    refuses to go below zero, even if two requests race for the last item.
    """
    now_text = to_db_time(now or utc_now())

    if movement_type in INCREASE_TYPES:
        cursor = db.execute(
            "UPDATE products SET quantity = quantity + ?, updated_at = ? "
            "WHERE id = ? AND user_id = ? AND is_archived = 0",
            (quantity, now_text, product_id, user_id),
        )
    elif movement_type in DECREASE_TYPES:
        cursor = db.execute(
            "UPDATE products SET quantity = quantity - ?, updated_at = ? "
            "WHERE id = ? AND user_id = ? AND is_archived = 0 AND quantity >= ?",
            (quantity, now_text, product_id, user_id, quantity),
        )
    else:
        raise ValueError(f"Unknown movement type: {movement_type}")

    if cursor.rowcount != 1:
        raise StockError(_explain_refusal(db, user_id, product_id, quantity))

    stock_after = db.execute(
        "SELECT quantity FROM products WHERE id = ? AND user_id = ?", (product_id, user_id)
    ).fetchone()["quantity"]

    db.execute(
        "INSERT INTO transactions (user_id, product_id, sale_id, transaction_type, quantity, "
        "unit_price, total_amount, stock_after, created_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, product_id, sale_id, movement_type, quantity, unit_price,
         quantity * unit_price, stock_after, now_text, notes),
    )
    return stock_after


def _explain_refusal(db, user_id, product_id, quantity):
    product = get_product(db, user_id, product_id)
    if product is None:
        return "Product not found."
    if product["is_archived"]:
        return f"“{product['name']}” is archived. Restore it before changing its stock."
    return f"Not enough stock for “{product['name']}”: {product['quantity']} left, {quantity} requested."


def change_stock(db, user_id, product_id, data, now=None):
    """Record a restock or manual adjustment. ``data`` comes from validate_stock_movement."""
    with transaction(db):
        product = get_product(db, user_id, product_id)
        if product is None:
            raise StockError("Product not found.")
        is_restock = data["movement_type"] == "RESTOCK"
        unit_price = data["unit_cost"] if (is_restock and data["unit_cost"] is not None) else product["cost_price"]

        stock_after = apply_stock_movement(
            db, user_id, product_id, data["movement_type"], data["quantity"], unit_price,
            notes=data["notes"], now=now,
        )
        if data["update_cost"]:
            db.execute(
                "UPDATE products SET cost_price = ? WHERE id = ? AND user_id = ?",
                (data["unit_cost"], product_id, user_id),
            )
    return stock_after


# --------------------------------------------------------------------------
# Creating, editing, archiving
# --------------------------------------------------------------------------

def create_product(db, user_id, data, now=None):
    """Insert a product. Opening stock is recorded in the ledger as a RESTOCK."""
    now_text = to_db_time(now or utc_now())
    try:
        with transaction(db):
            cursor = db.execute(
                "INSERT INTO products (user_id, name, sku, category, description, cost_price, "
                "selling_price, quantity, reorder_level, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (user_id, data["name"], data["sku"], data["category"], data["description"],
                 data["cost_price"], data["selling_price"], data["reorder_level"], now_text, now_text),
            )
            product_id = cursor.lastrowid
            if data["quantity"] > 0:
                # Insert with 0 and add through the ledger so history always sums to the stock level.
                apply_stock_movement(
                    db, user_id, product_id, "RESTOCK", data["quantity"], data["cost_price"],
                    notes="Initial stock", now=now,
                )
    except sqlite3.IntegrityError as error:
        raise _translate_integrity_error(db, user_id, data["sku"], error) from error
    return product_id


def update_product(db, user_id, product_id, data, now=None):
    """Update descriptive fields and prices. Quantity is NOT editable here."""
    now_text = to_db_time(now or utc_now())
    try:
        cursor = db.execute(
            "UPDATE products SET name = ?, sku = ?, category = ?, description = ?, cost_price = ?, "
            "selling_price = ?, reorder_level = ?, updated_at = ? "
            "WHERE id = ? AND user_id = ? AND is_archived = 0",
            (data["name"], data["sku"], data["category"], data["description"], data["cost_price"],
             data["selling_price"], data["reorder_level"], now_text, product_id, user_id),
        )
    except sqlite3.IntegrityError as error:
        raise _translate_integrity_error(db, user_id, data["sku"], error) from error
    return cursor.rowcount == 1


def archive_product(db, user_id, product_id, now=None):
    """Soft delete: hide the product but keep every sale and stock record."""
    now_text = to_db_time(now or utc_now())
    cursor = db.execute(
        "UPDATE products SET is_archived = 1, archived_at = ?, updated_at = ? "
        "WHERE id = ? AND user_id = ? AND is_archived = 0",
        (now_text, now_text, product_id, user_id),
    )
    return cursor.rowcount == 1


def restore_product(db, user_id, product_id, now=None):
    now_text = to_db_time(now or utc_now())
    cursor = db.execute(
        "UPDATE products SET is_archived = 0, archived_at = NULL, updated_at = ? "
        "WHERE id = ? AND user_id = ? AND is_archived = 1",
        (now_text, product_id, user_id),
    )
    return cursor.rowcount == 1


def _translate_integrity_error(db, user_id, sku, error):
    """Turn a duplicate-SKU database error into a friendly form error."""
    if "products.sku" not in str(error):
        return error  # something unexpected: let it surface as a real error
    clash = db.execute(
        "SELECT is_archived FROM products WHERE user_id = ? AND sku = ?", (user_id, sku)
    ).fetchone()
    if clash is not None and clash["is_archived"]:
        message = "This SKU belongs to an archived product. Restore that product or choose a different SKU."
    else:
        message = "You already have a product with this SKU. SKUs must be unique."
    return ValidationFailed({"sku": message})
