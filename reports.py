"""Analytics: dashboard numbers, date-range reports and export queries.

All figures are calculated from the user's real records on every request;
nothing is cached or invented. Sales are grouped by LOCAL calendar day (see the
time-zone note in helpers.py). SQLite cannot do time-zone maths, so we fetch the
sales in the range and group them in Python - perfectly fast at small-business
scale and easy to follow.
"""

from datetime import timedelta

from helpers import day_bounds_utc, like_pattern, local_today, paginate, parse_date, to_local

MAX_REPORT_DAYS = 366


# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------

def inventory_totals(db, user_id):
    """Counts and values across ACTIVE products."""
    return db.execute(
        "SELECT COUNT(*) AS active_products, "
        "COALESCE(SUM(quantity), 0) AS total_units, "
        "COALESCE(SUM(CASE WHEN quantity > 0 AND quantity <= reorder_level THEN 1 ELSE 0 END), 0) AS low_stock, "
        "COALESCE(SUM(CASE WHEN quantity = 0 THEN 1 ELSE 0 END), 0) AS out_of_stock, "
        "COALESCE(SUM(quantity * cost_price), 0) AS cost_value, "
        "COALESCE(SUM(quantity * selling_price), 0) AS retail_value "
        "FROM products WHERE user_id = ? AND is_archived = 0",
        (user_id,),
    ).fetchone()


def sales_totals(db, user_id, start_utc, end_utc):
    """Revenue, gross profit, number of sales and units sold in [start, end)."""
    row = db.execute(
        "SELECT COUNT(*) AS sales_count, COALESCE(SUM(total_amount), 0) AS revenue, "
        "COALESCE(SUM(total_profit), 0) AS profit FROM sales "
        "WHERE user_id = ? AND created_at >= ? AND created_at < ?",
        (user_id, start_utc, end_utc),
    ).fetchone()
    units = db.execute(
        "SELECT COALESCE(SUM(i.quantity), 0) FROM sale_items i JOIN sales s ON s.id = i.sale_id "
        "WHERE s.user_id = ? AND s.created_at >= ? AND s.created_at < ?",
        (user_id, start_utc, end_utc),
    ).fetchone()[0]
    return {
        "sales_count": row["sales_count"],
        "revenue": row["revenue"],
        "profit": row["profit"],
        "units": units,
    }


def daily_breakdown(db, user_id, start_day, end_day):
    """One entry per local day from start_day to end_day (zero-filled).

    Days with no sales appear with zeros - that is the true figure, not made-up data.
    """
    start_utc, end_utc = day_bounds_utc(start_day, end_day)
    rows = db.execute(
        "SELECT s.created_at, s.total_amount, s.total_profit, "
        "(SELECT COALESCE(SUM(quantity), 0) FROM sale_items WHERE sale_id = s.id) AS units "
        "FROM sales s WHERE s.user_id = ? AND s.created_at >= ? AND s.created_at < ?",
        (user_id, start_utc, end_utc),
    ).fetchall()

    days = {}
    day = start_day
    while day <= end_day:
        days[day] = {"date": day, "sales_count": 0, "units": 0, "revenue": 0, "profit": 0}
        day += timedelta(days=1)

    for row in rows:
        bucket = days[to_local(row["created_at"]).date()]
        bucket["sales_count"] += 1
        bucket["units"] += row["units"]
        bucket["revenue"] += row["total_amount"]
        bucket["profit"] += row["total_profit"]
    return list(days.values())


def best_sellers(db, user_id, start_utc, end_utc, limit=5):
    return db.execute(
        "SELECT p.id AS product_id, p.name, p.sku, SUM(i.quantity) AS units, "
        "SUM(i.line_total) AS revenue, SUM(i.line_profit) AS profit "
        "FROM sale_items i JOIN sales s ON s.id = i.sale_id JOIN products p ON p.id = i.product_id "
        "WHERE s.user_id = ? AND s.created_at >= ? AND s.created_at < ? "
        "GROUP BY p.id ORDER BY units DESC, revenue DESC, p.name COLLATE NOCASE LIMIT ?",
        (user_id, start_utc, end_utc, limit),
    ).fetchall()


def category_distribution(db, user_id):
    """Units in stock per category (active products with stock only)."""
    return db.execute(
        "SELECT category, SUM(quantity) AS units, COUNT(*) AS products, "
        "SUM(quantity * cost_price) AS cost_value FROM products "
        "WHERE user_id = ? AND is_archived = 0 GROUP BY category COLLATE NOCASE "
        "HAVING SUM(quantity) > 0 ORDER BY units DESC, category COLLATE NOCASE",
        (user_id,),
    ).fetchall()


def low_stock_report(db, user_id):
    """Products at or below their reorder level, out-of-stock first."""
    return db.execute(
        "SELECT id, name, sku, category, quantity, reorder_level, cost_price, "
        "(reorder_level - quantity) AS shortfall FROM products "
        "WHERE user_id = ? AND is_archived = 0 AND quantity <= reorder_level "
        "ORDER BY quantity ASC, name COLLATE NOCASE",
        (user_id,),
    ).fetchall()


# --------------------------------------------------------------------------
# Page data
# --------------------------------------------------------------------------

def dashboard_data(db, user_id):
    today = local_today()
    today_start, today_end = day_bounds_utc(today, today)
    thirty_start, _ = day_bounds_utc(today - timedelta(days=29), today)

    totals = inventory_totals(db, user_id)
    today_sales = sales_totals(db, user_id, today_start, today_end)
    week = daily_breakdown(db, user_id, today - timedelta(days=6), today)
    sellers = best_sellers(db, user_id, thirty_start, today_end, limit=5)
    categories = category_distribution(db, user_id)

    recent_transactions = db.execute(
        "SELECT t.*, p.name AS product_name, s.receipt_number FROM transactions t "
        "JOIN products p ON p.id = t.product_id LEFT JOIN sales s ON s.id = t.sale_id "
        "WHERE t.user_id = ? ORDER BY t.created_at DESC, t.id DESC LIMIT 8",
        (user_id,),
    ).fetchall()
    recent_sales = db.execute(
        "SELECT s.*, (SELECT COALESCE(SUM(quantity), 0) FROM sale_items WHERE sale_id = s.id) AS units "
        "FROM sales s WHERE s.user_id = ? ORDER BY s.created_at DESC, s.id DESC LIMIT 5",
        (user_id,),
    ).fetchall()

    return {
        "totals": totals,
        "today": today_sales,
        "week": week,
        "best_sellers": sellers,
        "categories": categories,
        "recent_transactions": recent_transactions,
        "recent_sales": recent_sales,
    }


def report_data(db, user_id, start_day, end_day):
    start_utc, end_utc = day_bounds_utc(start_day, end_day)
    return {
        "totals": sales_totals(db, user_id, start_utc, end_utc),
        "daily": daily_breakdown(db, user_id, start_day, end_day),
        "best_sellers": best_sellers(db, user_id, start_utc, end_utc, limit=10),
        "low_stock": low_stock_report(db, user_id),
        "inventory": inventory_totals(db, user_id),
    }


def parse_report_range(from_text, to_text):
    """Turn query-string dates into (start_day, end_day, error_message).

    Defaults to the last 30 days including today. If the input is unusable we
    fall back to the default and return a message explaining why.
    """
    today = local_today()
    default = (today - timedelta(days=29), today)
    start_day, end_day = parse_date(from_text), parse_date(to_text)

    if not from_text and not to_text:
        return default[0], default[1], None
    if start_day is None or end_day is None:
        return default[0], default[1], "Enter both dates as valid dates. Showing the last 30 days instead."
    if start_day > end_day:
        return default[0], default[1], "The start date must not be after the end date. Showing the last 30 days instead."
    if (end_day - start_day).days + 1 > MAX_REPORT_DAYS:
        return default[0], default[1], f"Please choose a range of at most {MAX_REPORT_DAYS} days. Showing the last 30 days instead."
    return start_day, end_day, None


# --------------------------------------------------------------------------
# Transactions list (history page) and export rows
# --------------------------------------------------------------------------

def _transaction_where(user_id, filters):
    where, params = ["t.user_id = ?"], [user_id]
    if filters.get("q"):
        where.append("(p.name LIKE ? ESCAPE '\\' OR p.sku LIKE ? ESCAPE '\\' OR t.notes LIKE ? ESCAPE '\\')")
        pattern = like_pattern(filters["q"])
        params += [pattern, pattern, pattern]
    if filters.get("product_id"):
        where.append("t.product_id = ?")
        params.append(filters["product_id"])
    if filters.get("type"):
        where.append("t.transaction_type = ?")
        params.append(filters["type"])
    if filters.get("start_utc"):
        where.append("t.created_at >= ?")
        params.append(filters["start_utc"])
    if filters.get("end_utc"):
        where.append("t.created_at < ?")
        params.append(filters["end_utc"])
    return " AND ".join(where), params


def list_transactions(db, user_id, filters, page, per_page):
    where_sql, params = _transaction_where(user_id, filters)
    base = "FROM transactions t JOIN products p ON p.id = t.product_id LEFT JOIN sales s ON s.id = t.sale_id"
    total = db.execute(f"SELECT COUNT(*) {base} WHERE {where_sql}", params).fetchone()[0]
    pagination = paginate(total, page, per_page)
    rows = db.execute(
        f"SELECT t.*, p.name AS product_name, p.sku AS product_sku, p.is_archived, s.receipt_number "
        f"{base} WHERE {where_sql} ORDER BY t.created_at DESC, t.id DESC LIMIT ? OFFSET ?",
        params + [per_page, pagination["offset"]],
    ).fetchall()
    return rows, pagination


def export_transactions(db, user_id, filters):
    where_sql, params = _transaction_where(user_id, filters)
    return db.execute(
        "SELECT t.created_at, p.name AS product_name, p.sku AS product_sku, t.transaction_type, "
        "t.quantity, t.unit_price, t.total_amount, t.stock_after, s.receipt_number, t.notes "
        "FROM transactions t JOIN products p ON p.id = t.product_id LEFT JOIN sales s ON s.id = t.sale_id "
        f"WHERE {where_sql} ORDER BY t.created_at ASC, t.id ASC",
        params,
    ).fetchall()


def export_products(db, user_id):
    return db.execute(
        "SELECT name, sku, category, description, cost_price, selling_price, quantity, reorder_level, "
        "is_archived, created_at, updated_at FROM products WHERE user_id = ? "
        "ORDER BY name COLLATE NOCASE, id",
        (user_id,),
    ).fetchall()


def export_sales(db, user_id, start_utc, end_utc):
    """One CSV row per sold line item, with the sale's receipt number and date."""
    return db.execute(
        "SELECT s.receipt_number, s.created_at, i.product_name_snapshot, i.sku_snapshot, i.quantity, "
        "i.unit_price, i.unit_cost_snapshot, i.line_total, i.line_profit, s.notes "
        "FROM sale_items i JOIN sales s ON s.id = i.sale_id "
        "WHERE s.user_id = ? AND s.created_at >= ? AND s.created_at < ? "
        "ORDER BY s.created_at ASC, s.id ASC, i.id ASC",
        (user_id, start_utc, end_utc),
    ).fetchall()
