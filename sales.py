"""Sales logic: checkout, receipts and the sales list.

``checkout`` is the most important function in the project. It never trusts
anything the browser says about prices or totals: the browser only sends
product ids and quantities, and the server looks up everything else.
"""

import inventory
from database import transaction
from helpers import like_pattern, paginate, to_db_time, utc_now

MAX_CART_LINES = 100
MAX_LINE_QUANTITY = 100_000
MAX_NOTE_LENGTH = 200


class CheckoutError(Exception):
    """The sale could not be completed.

    ``status`` is the HTTP status code the API should answer with and ``items``
    lists the products involved (used by the browser to refresh stock numbers).
    """

    def __init__(self, message, status=400, items=None):
        super().__init__(message)
        self.status = status
        self.items = items or []


def _clean_cart(raw_items):
    """Validate the cart sent by the browser and merge duplicate products.

    Returns {product_id: quantity}.
    """
    if not isinstance(raw_items, list) or not raw_items:
        raise CheckoutError("The cart is empty.")
    if len(raw_items) > MAX_CART_LINES:
        raise CheckoutError(f"A sale can contain at most {MAX_CART_LINES} different products.")

    cart = {}
    for entry in raw_items:
        if not isinstance(entry, dict):
            raise CheckoutError("The cart contains an invalid item.")
        product_id, quantity = entry.get("product_id"), entry.get("quantity")
        # bool is a subclass of int in Python, so exclude it explicitly (JSON true/false).
        for value in (product_id, quantity):
            if isinstance(value, bool) or not isinstance(value, int):
                raise CheckoutError("The cart contains an invalid item.")
        if product_id < 1:
            raise CheckoutError("The cart contains an invalid item.")
        if quantity < 1 or quantity > MAX_LINE_QUANTITY:
            raise CheckoutError(f"Quantities must be between 1 and {MAX_LINE_QUANTITY:,}.")
        cart[product_id] = cart.get(product_id, 0) + quantity
    return cart


def checkout(db, user_id, raw_items, notes="", now=None):
    """Record a sale and deduct stock - atomically. Returns the new sale id.

    Inside one database transaction we:
      1. look up each product (must belong to this user and not be archived),
      2. check stock using the server's own numbers,
      3. insert the sale, its items (with price/cost snapshots) and ledger rows,
      4. deduct stock.
    If ANY step fails, the transaction rolls back and nothing is saved.
    """
    cart = _clean_cart(raw_items)

    if notes is None:
        notes = ""
    if not isinstance(notes, str) or len(notes.strip()) > MAX_NOTE_LENGTH:
        raise CheckoutError(f"Notes must be {MAX_NOTE_LENGTH} characters or fewer.")
    notes = notes.strip()

    now = now or utc_now()
    now_text = to_db_time(now)

    with transaction(db):  # BEGIN IMMEDIATE: concurrent checkouts take turns
        lines, shortages = [], []
        for product_id in sorted(cart):  # fixed order keeps behaviour predictable
            wanted = cart[product_id]
            product = db.execute(
                "SELECT id, name, sku, cost_price, selling_price, quantity FROM products "
                "WHERE id = ? AND user_id = ? AND is_archived = 0",
                (product_id, user_id),
            ).fetchone()
            if product is None:
                # Same message whether the id is unknown, archived or belongs to
                # someone else, so nothing is revealed about other accounts.
                raise CheckoutError(
                    "One of the products in the cart is no longer available.",
                    status=409, items=[{"product_id": product_id, "available": 0}],
                )
            if product["quantity"] < wanted:
                shortages.append(product)
            lines.append((product, wanted))

        if shortages:
            details = ", ".join(f"{p['name']} ({p['quantity']} left)" for p in shortages)
            raise CheckoutError(
                f"Not enough stock for: {details}.",
                status=409,
                items=[{"product_id": p["id"], "available": p["quantity"]} for p in shortages],
            )

        # All amounts are computed here from database prices, never from the browser.
        total_amount = sum(p["selling_price"] * qty for p, qty in lines)
        total_cost = sum(p["cost_price"] * qty for p, qty in lines)

        receipt_number = db.execute(
            "SELECT COALESCE(MAX(receipt_number), 0) + 1 FROM sales WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        sale_id = db.execute(
            "INSERT INTO sales (user_id, receipt_number, total_amount, total_cost, total_profit, created_at, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, receipt_number, total_amount, total_cost, total_amount - total_cost, now_text, notes),
        ).lastrowid

        for product, qty in lines:
            line_total = product["selling_price"] * qty
            line_profit = line_total - product["cost_price"] * qty
            db.execute(
                "INSERT INTO sale_items (sale_id, product_id, product_name_snapshot, sku_snapshot, quantity, "
                "unit_cost_snapshot, unit_price, line_total, line_profit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sale_id, product["id"], product["name"], product["sku"], qty,
                 product["cost_price"], product["selling_price"], line_total, line_profit),
            )
            try:
                inventory.apply_stock_movement(
                    db, user_id, product["id"], "SALE", qty, product["selling_price"],
                    notes=f"Sale S-{receipt_number:06d}", sale_id=sale_id, now=now,
                )
            except inventory.StockError as error:
                # Should not happen after the checks above, but the database
                # has the final say. Raising rolls back the whole sale.
                raise CheckoutError(str(error), status=409) from error

    return sale_id


def get_sale(db, user_id, sale_id):
    """Return (sale_row, item_rows) for one of this user's sales, or (None, [])."""
    sale = db.execute(
        "SELECT * FROM sales WHERE id = ? AND user_id = ?", (sale_id, user_id)
    ).fetchone()
    if sale is None:
        return None, []
    items = db.execute(
        "SELECT * FROM sale_items WHERE sale_id = ? ORDER BY id", (sale_id,)
    ).fetchall()
    return sale, items


def list_sales(db, user_id, start_utc, end_utc, query, page, per_page):
    """Paginated sales list. ``start_utc``/``end_utc`` may be None (no date limit)."""
    where, params = ["s.user_id = ?"], [user_id]
    if start_utc:
        where.append("s.created_at >= ?")
        params.append(start_utc)
    if end_utc:
        where.append("s.created_at < ?")
        params.append(end_utc)
    if query:
        # Match a product name inside the sale, a note, or a receipt number such as "S-000012" / "12".
        where.append(
            "(s.notes LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM sale_items i WHERE i.sale_id = s.id "
            "AND (i.product_name_snapshot LIKE ? ESCAPE '\\' OR i.sku_snapshot LIKE ? ESCAPE '\\')) "
            "OR printf('S-%06d', s.receipt_number) LIKE ? ESCAPE '\\')"
        )
        pattern = like_pattern(query)
        params += [pattern, pattern, pattern, pattern]
    where_sql = " AND ".join(where)

    total = db.execute(f"SELECT COUNT(*) FROM sales s WHERE {where_sql}", params).fetchone()[0]
    pagination = paginate(total, page, per_page)
    rows = db.execute(
        f"SELECT s.*, (SELECT COALESCE(SUM(quantity), 0) FROM sale_items WHERE sale_id = s.id) AS units "
        f"FROM sales s WHERE {where_sql} ORDER BY s.created_at DESC, s.id DESC LIMIT ? OFFSET ?",
        params + [per_page, pagination["offset"]],
    ).fetchall()
    return rows, pagination
