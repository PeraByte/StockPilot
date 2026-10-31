import os
import secrets
import sqlite3
import tempfile
from datetime import timedelta

from flask import (  # pyright: ignore[reportMissingImports]
    Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for,
)

import database
import helpers
import inventory
import reports
import sales
import validators
from database import get_db
from helpers import login_required

STATUS_OPTIONS = [
    ("all", "All stock levels"),
    ("ok", "Well stocked"),
    ("low", "Low stock"),
    ("out", "Out of stock"),
]


# ==========================================================================
# Application factory
# ==========================================================================

# Environment variables set by serverless platforms (Vercel, AWS Lambda, Netlify, Google Cloud Run).
# On these platforms the code folder is read-only, only a temporary folder is writable, and every
# running copy of the app ("instance") has its OWN temporary folder that disappears when the copy
# is recycled. That is fatal for a SQLite file and for a secret key stored in a file: see DEPLOYMENT.md.
SERVERLESS_MARKERS = ("VERCEL", "AWS_LAMBDA_FUNCTION_NAME", "NETLIFY", "K_SERVICE")
MIN_SECRET_KEY_LENGTH = 16


def running_on_serverless():
    return any(os.environ.get(name) for name in SERVERLESS_MARKERS)


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    if running_on_serverless():
        app.instance_path = os.path.join(tempfile.gettempdir(), "stockpilot")  # the only writable place
    os.makedirs(app.instance_path, exist_ok=True)  # the database and secret key live here

    app.config.update(
        DATABASE=os.environ.get("STOCKPILOT_DATABASE", os.path.join(app.instance_path, "stockpilot.db")),
        TIMEZONE=os.environ.get("STOCKPILOT_TIMEZONE", "Asia/Karachi"),
        DEFAULT_CURRENCY="PKR",
        PER_PAGE=10,
        MAX_CONTENT_LENGTH=1024 * 1024,  # 1 MB is plenty for our forms and JSON
        SESSION_COOKIE_NAME="stockpilot_session",
        SESSION_COOKIE_HTTPONLY=True,    # JavaScript cannot read the session cookie
        SESSION_COOKIE_SAMESITE="Lax",   # not sent on cross-site POSTs
        # Secure cookies need HTTPS. Plain http://localhost would drop the cookie,
        # so this is opt-in: set STOCKPILOT_COOKIE_SECURE=1 when deploying over HTTPS.
        SESSION_COOKIE_SECURE=os.environ.get("STOCKPILOT_COOKIE_SECURE") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )
    if test_config:
        app.config.update(test_config)
    # Flask's own default is SECRET_KEY = None, so test for "not set", not "missing".
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = load_secret_key(app)

    if running_on_serverless() and os.path.abspath(app.config["DATABASE"]).startswith(tempfile.gettempdir()):
        app.logger.warning(
            "Serverless platform detected: the SQLite database at %s is NOT shared between running copies "
            "of this app and is lost when a copy is recycled, so users will be logged out and data will "
            "seem to vanish. Use a host with a persistent disk (see DEPLOYMENT.md).", app.config["DATABASE"])

    database.register_database(app)
    with app.app_context():
        database.init_db()  # safe to repeat: only creates what is missing

    register_hooks(app)
    register_template_helpers(app)
    register_auth_routes(app)
    register_dashboard_routes(app)
    register_product_routes(app)
    register_sales_routes(app)
    register_report_routes(app)
    register_settings_routes(app)
    register_error_handlers(app)
    return app


def load_secret_key(app):
    """Return the key that signs session cookies.

    1. STOCKPILOT_SECRET_KEY (or SECRET_KEY) from the environment, if set. It must be long enough
       to be unguessable, because anyone who knows the key can forge a login cookie.
    2. Otherwise, on a normal server or your own computer, a random key is generated on first run
       and kept in the instance folder (excluded from git), so no secret lives in the source code.
    3. On a serverless platform a key file cannot work (every running copy would generate a
       different key and log everybody out), so the key MUST come from the environment.
    """
    from_environment = os.environ.get("STOCKPILOT_SECRET_KEY") or os.environ.get("SECRET_KEY")
    if from_environment:
        if len(from_environment) < MIN_SECRET_KEY_LENGTH:
            raise RuntimeError(
                f"STOCKPILOT_SECRET_KEY is too short: use at least {MIN_SECRET_KEY_LENGTH} characters. A short or "
                'guessable key lets anyone forge a login. Create one with: python -c "import secrets; '
                'print(secrets.token_hex(32))"')
        return from_environment
    if running_on_serverless():
        raise RuntimeError(
            "STOCKPILOT_SECRET_KEY is not set. On a serverless platform every running copy of the app would "
            "generate its own random key, so a login made on one copy is rejected by the next and users keep "
            'being sent back to the login page. Create one with: python -c "import secrets; '
            'print(secrets.token_hex(32))" and add it to your project\'s environment variables.')
    path = os.path.join(app.instance_path, "secret_key")
    if not os.path.exists(path):
        _publish_new_key_file(path)
    with open(path, encoding="utf-8") as key_file:
        return key_file.read().strip()


def _publish_new_key_file(path):
    """Create the key file so that it is either absent or complete - never half-written.

    Several server workers may start at the same moment on a fresh install. If each one simply
    created the file and then wrote into it, a worker that arrived in between would read an empty
    key, and cookies signed by one worker would be rejected by another (users logged out at random).
    So the key is written to a private temporary file first and then linked into place in one
    atomic step, which succeeds for exactly one worker; the others use that worker's key.
    """
    temporary = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as key_file:
        key_file.write(secrets.token_hex(32))
    try:
        os.link(temporary, path)
    except FileExistsError:
        pass                                  # another worker won the race: use its key
    except OSError:                           # file system without hard links: best effort
        if not os.path.exists(path):
            os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


# ==========================================================================
# Hooks that run around every request
# ==========================================================================

CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://cdn.jsdelivr.net",  # Chart.js CDN; no inline scripts are used
    "style-src 'self'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


def register_hooks(app):
    @app.before_request
    def load_user_and_check_csrf():
        # Who is logged in? g.user deliberately never includes password_hash.
        g.user = None
        user_id = session.get("user_id")
        if user_id is not None:
            g.user = get_db().execute(
                "SELECT id, full_name, business_name, email, currency_label, created_at "
                "FROM users WHERE id = ?", (user_id,),
            ).fetchone()
            if g.user is None:  # account no longer exists (e.g. database was reset)
                session.clear()

        # Every request that changes something must carry the secret CSRF token.
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not helpers.csrf_is_valid():
            abort(400, description="Your session expired or this form could not be verified. "
                                   "Please go back, reload the page and try again.")

    @app.after_request
    def add_security_headers(response):
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.endpoint != "static":
            # Private pages must not be kept in the browser cache, so pressing
            # "Back" after logging out does not reveal them.
            response.headers["Cache-Control"] = "no-store"
        return response


def register_template_helpers(app):
    env = app.jinja_env
    env.globals["csrf_token"] = helpers.get_csrf_token

    def page_url(page_number):
        """URL of the current page with a different ?page= value (keeps filters)."""
        arguments = request.args.to_dict()
        arguments["page"] = page_number
        return url_for(request.endpoint, **{**(request.view_args or {}), **arguments})

    env.globals["page_url"] = page_url
    env.globals["TRANSACTION_LABELS"] = inventory.TRANSACTION_LABELS
    env.globals["INCREASE_TYPES"] = inventory.INCREASE_TYPES

    def currency():
        if g.get("user") is not None:
            return g.user["currency_label"]
        return app.config["DEFAULT_CURRENCY"]

    env.globals["currency"] = currency

    @app.template_filter("money")
    def money_filter(minor_units):
        return helpers.format_money(minor_units, currency())

    @app.template_filter("plain_money")
    def plain_money_filter(minor_units):
        return helpers.format_money(minor_units)

    app.template_filter("money_input")(helpers.money_input_value)

    @app.template_filter("localdt")
    def local_datetime_filter(value):
        return helpers.format_local(value, "%d %b %Y, %I:%M %p")

    @app.template_filter("localdate")
    def local_date_filter(value):
        return helpers.format_local(value, "%d %b %Y")

    @app.template_filter("receipt_no")
    def receipt_number_filter(number):
        return f"S-{int(number):06d}"

    @app.template_filter("stock_status")
    def stock_status_filter(product):
        return inventory.stock_status(product["quantity"], product["reorder_level"])


def wants_json():
    return request.path.startswith("/api/")


def local_filter_bounds(args):
    """Read optional ?from= and ?to= dates. Returns (start_utc, end_utc, error_message)."""
    from_text, to_text = args.get("from", ""), args.get("to", "")
    start_day, end_day = helpers.parse_date(from_text), helpers.parse_date(to_text)
    error = None
    if (from_text and start_day is None) or (to_text and end_day is None):
        error, start_day, end_day = "Enter dates in the format YYYY-MM-DD.", None, None
    elif start_day and end_day and start_day > end_day:
        error, start_day, end_day = "The start date is after the end date.", None, None
    start_utc = helpers.local_midnight_as_utc(start_day) if start_day else None
    end_utc = helpers.local_midnight_as_utc(end_day + timedelta(days=1)) if end_day else None
    return start_utc, end_utc, error


# ==========================================================================
# Accounts: register, login, logout
# ==========================================================================

def register_auth_routes(app):
    @app.route("/register", methods=["GET", "POST"])
    def register():
        if g.user:
            return redirect(url_for("dashboard"))
        form, errors = {}, {}
        if request.method == "POST":
            data, errors = validators.validate_registration(request.form)
            form = {key: request.form.get(key, "") for key in ("full_name", "business_name", "email")}
            if not errors:
                db = get_db()
                try:
                    cursor = db.execute(
                        "INSERT INTO users (full_name, business_name, email, password_hash, currency_label, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (data["full_name"], data["business_name"], data["email"],
                         helpers.hash_password(data["password"]), app.config["DEFAULT_CURRENCY"],
                         helpers.to_db_time(helpers.utc_now())),
                    )
                except sqlite3.IntegrityError:
                    # The UNIQUE constraint on users.email decides - this is safe even if
                    # two people register the same address at the same moment.
                    errors["email"] = "An account with this email already exists. Try logging in instead."
                else:
                    session.clear()  # new session on login prevents session fixation
                    session["user_id"] = cursor.lastrowid
                    session.permanent = True
                    flash(f"Welcome to StockPilot, {data['full_name']}! Add your first product to get started.", "success")
                    return redirect(url_for("dashboard"))
        return render_template("register.html", form=form, errors=errors), (400 if errors else 200)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user:
            return redirect(url_for("dashboard"))
        form, errors = {}, {}
        next_url = request.values.get("next", "")
        if request.method == "POST":
            data, errors = validators.validate_login(request.form)
            form = {"email": request.form.get("email", "")}
            if not errors:
                user = get_db().execute(
                    "SELECT id, password_hash FROM users WHERE email = ?", (data["email"],)
                ).fetchone()
                stored_hash = user["password_hash"] if user else None
                if helpers.verify_password(stored_hash, data["password"]):
                    session.clear()
                    session["user_id"] = user["id"]
                    session.permanent = True
                    return redirect(helpers.safe_next_url(next_url) or url_for("dashboard"))
                # One message for "no such email" and "wrong password".
                errors["form"] = "Incorrect email or password."
                return render_template("login.html", form=form, errors=errors, next_url=next_url), 401
        return render_template("login.html", form=form, errors=errors, next_url=next_url), (400 if errors else 200)

    @app.post("/logout")
    def logout():
        session.clear()
        flash("You have been logged out.", "info")
        return redirect(url_for("login"))


# ==========================================================================
# Dashboard
# ==========================================================================

def register_dashboard_routes(app):
    @app.get("/")
    @login_required
    def dashboard():
        data = reports.dashboard_data(get_db(), g.user["id"])
        # Chart data is passed as whole minor units; JavaScript divides by 100 only to draw axes.
        charts = {
            "currency": g.user["currency_label"],
            "week": {
                "labels": [day["date"].strftime("%a %d %b") for day in data["week"]],
                "revenue": [day["revenue"] for day in data["week"]],
                "profit": [day["profit"] for day in data["week"]],
                "has_data": any(day["sales_count"] for day in data["week"]),
            },
            "categories": {
                "labels": [row["category"] for row in data["categories"]],
                "values": [row["units"] for row in data["categories"]],
            },
            "best_sellers": {
                "labels": [row["name"] for row in data["best_sellers"]],
                "values": [row["units"] for row in data["best_sellers"]],
            },
        }
        return render_template("dashboard.html", data=data, charts=charts)


# ==========================================================================
# Products and stock
# ==========================================================================

def product_form_values(product):
    """Turn a product row into the string values shown in the edit form."""
    return {
        "name": product["name"],
        "sku": product["sku"],
        "category": "" if product["category"] == "Uncategorized" else product["category"],
        "description": product["description"],
        "cost_price": helpers.money_input_value(product["cost_price"]),
        "selling_price": helpers.money_input_value(product["selling_price"]),
        "reorder_level": str(product["reorder_level"]),
    }


FORM_FIELDS = ("name", "sku", "category", "description", "cost_price", "selling_price", "quantity", "reorder_level")


def register_product_routes(app):
    def load_product_or_404(product_id):
        product = inventory.get_product(get_db(), g.user["id"], product_id)
        if product is None:  # also true for other users' products - no information leak
            abort(404)
        return product

    @app.get("/products")
    @login_required
    def products_list():
        filters = {
            "q": request.args.get("q", "").strip()[:100],
            "category": request.args.get("category", "").strip()[:60],
            "status": helpers.choice(request.args.get("status"), ("all", "ok", "low", "out"), "all"),
            "view": helpers.choice(request.args.get("view"), ("active", "archived"), "active"),
            "sort": helpers.choice(request.args.get("sort"), inventory.SORT_OPTIONS, "name_asc"),
        }
        db = get_db()
        rows, pagination = inventory.list_products(
            db, g.user["id"], filters, helpers.parse_page(request.args.get("page")), app.config["PER_PAGE"]
        )
        return render_template(
            "products.html", products=rows, pagination=pagination, filters=filters,
            categories=inventory.list_categories(db, g.user["id"]),
            sort_options=inventory.SORT_OPTIONS, status_options=STATUS_OPTIONS,
        )

    @app.route("/products/new", methods=["GET", "POST"])
    @login_required
    def product_new():
        form, errors = {"reorder_level": "5", "quantity": "0"}, {}
        db = get_db()
        if request.method == "POST":
            form = {name: request.form.get(name, "") for name in FORM_FIELDS}
            data, errors = validators.validate_product(request.form, creating=True)
            if not errors:
                try:
                    product_id = inventory.create_product(db, g.user["id"], data)
                except inventory.ValidationFailed as failure:
                    errors = failure.errors
                else:
                    flash(f"“{data['name']}” was added to your inventory.", "success")
                    return redirect(url_for("product_detail", product_id=product_id))
        return render_template(
            "product_form.html", product=None, form=form, errors=errors,
            categories=inventory.list_categories(db, g.user["id"]),
        ), (400 if errors else 200)

    def render_product_detail(product, stock_form=None, stock_errors=None, status=200):
        db = get_db()
        selling, cost = product["selling_price"], product["cost_price"]
        metrics = {
            "stock_value": product["quantity"] * cost,
            "retail_value": product["quantity"] * selling,
            "profit_per_unit": selling - cost,
            "margin": round((selling - cost) * 100 / selling, 1) if selling else None,
            "status": inventory.stock_status(product["quantity"], product["reorder_level"]),
        }
        return render_template(
            "product_detail.html", product=product, metrics=metrics,
            sold=inventory.product_sales_summary(db, g.user["id"], product["id"]),
            movements=inventory.recent_movements(db, g.user["id"], product["id"]),
            stock_form=stock_form or {"action": "restock"}, stock_errors=stock_errors or {},
        ), status

    @app.get("/products/<int:product_id>")
    @login_required
    def product_detail(product_id):
        return render_product_detail(load_product_or_404(product_id))

    @app.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
    @login_required
    def product_edit(product_id):
        product = load_product_or_404(product_id)
        if product["is_archived"]:
            flash("Restore this product before editing it.", "warning")
            return redirect(url_for("product_detail", product_id=product_id))
        db = get_db()
        form, errors = product_form_values(product), {}
        if request.method == "POST":
            form = {name: request.form.get(name, "") for name in FORM_FIELDS}
            data, errors = validators.validate_product(request.form, creating=False)
            if not errors:
                try:
                    inventory.update_product(db, g.user["id"], product_id, data)
                except inventory.ValidationFailed as failure:
                    errors = failure.errors
                else:
                    flash("Product updated.", "success")
                    return redirect(url_for("product_detail", product_id=product_id))
        return render_template(
            "product_form.html", product=product, form=form, errors=errors,
            categories=inventory.list_categories(db, g.user["id"]),
        ), (400 if errors else 200)

    @app.post("/products/<int:product_id>/stock")
    @login_required
    def product_stock(product_id):
        product = load_product_or_404(product_id)
        data, errors = validators.validate_stock_movement(request.form)
        if not errors:
            try:
                new_level = inventory.change_stock(get_db(), g.user["id"], product_id, data)
            except inventory.StockError as problem:
                errors["quantity"] = str(problem)
            else:
                flash(f"Stock updated. “{product['name']}” now has {new_level} in stock.", "success")
                return redirect(url_for("product_detail", product_id=product_id))
        form = {key: request.form.get(key, "") for key in ("action", "quantity", "unit_cost", "notes", "update_cost")}
        return render_product_detail(product, stock_form=form, stock_errors=errors, status=400)

    @app.post("/products/<int:product_id>/archive")
    @login_required
    def product_archive(product_id):
        product = load_product_or_404(product_id)
        if inventory.archive_product(get_db(), g.user["id"], product_id):
            flash(f"“{product['name']}” was archived. Its sales and stock history are kept.", "success")
        else:
            flash("That product is already archived.", "info")
        return redirect(url_for("products_list"))

    @app.post("/products/<int:product_id>/restore")
    @login_required
    def product_restore(product_id):
        product = load_product_or_404(product_id)
        if inventory.restore_product(get_db(), g.user["id"], product_id):
            flash(f"“{product['name']}” was restored.", "success")
        return redirect(url_for("product_detail", product_id=product_id))


# ==========================================================================
# Sales: point of sale, checkout API, receipts, history, stock movements
# ==========================================================================

def register_sales_routes(app):
    @app.get("/sales")
    @login_required
    def pos():
        return render_template("sales.html")

    @app.get("/api/products/search")
    @login_required
    def api_product_search():
        query = request.args.get("q", "").strip()[:100]
        rows = inventory.search_for_sale(get_db(), g.user["id"], query)
        return jsonify(ok=True, products=[
            {"id": row["id"], "name": row["name"], "sku": row["sku"], "category": row["category"],
             "price": row["selling_price"], "available": row["quantity"]}
            for row in rows
        ])

    @app.post("/api/checkout")
    @login_required
    def api_checkout():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(ok=False, error="The request was not valid JSON."), 400
        db = get_db()
        try:
            sale_id = sales.checkout(db, g.user["id"], payload.get("items"), payload.get("notes", ""))
        except sales.CheckoutError as problem:
            return jsonify(ok=False, error=str(problem), items=problem.items), problem.status
        sale, _items = sales.get_sale(db, g.user["id"], sale_id)
        return jsonify(
            ok=True, sale_id=sale_id,
            receipt_number=f"S-{sale['receipt_number']:06d}",
            total=sale["total_amount"],
            receipt_url=url_for("receipt", sale_id=sale_id),
        ), 201

    @app.get("/sales/history")
    @login_required
    def sales_history():
        start_utc, end_utc, date_error = local_filter_bounds(request.args)
        query = request.args.get("q", "").strip()[:100]
        rows, pagination = sales.list_sales(
            get_db(), g.user["id"], start_utc, end_utc, query,
            helpers.parse_page(request.args.get("page")), app.config["PER_PAGE"],
        )
        return render_template(
            "sales_history.html", sales=rows, pagination=pagination, query=query,
            date_from=request.args.get("from", ""), date_to=request.args.get("to", ""),
            date_error=date_error,
        )

    @app.get("/sales/<int:sale_id>")
    @login_required
    def receipt(sale_id):
        sale, items = sales.get_sale(get_db(), g.user["id"], sale_id)
        if sale is None:
            abort(404)
        return render_template("receipt.html", sale=sale, items=items)

    @app.get("/transactions")
    @login_required
    def transactions():
        db = get_db()
        start_utc, end_utc, date_error = local_filter_bounds(request.args)
        product_id = request.args.get("product_id", type=int)
        filters = {
            "q": request.args.get("q", "").strip()[:100],
            "product_id": product_id,
            "type": helpers.choice(request.args.get("type"), inventory.TRANSACTION_TYPES, ""),
            "start_utc": start_utc,
            "end_utc": end_utc,
        }
        rows, pagination = reports.list_transactions(
            db, g.user["id"], filters, helpers.parse_page(request.args.get("page")), app.config["PER_PAGE"]
        )
        product_choices = db.execute(
            "SELECT id, name, sku, is_archived FROM products WHERE user_id = ? "
            "ORDER BY name COLLATE NOCASE LIMIT 1000", (g.user["id"],),
        ).fetchall()
        return render_template(
            "transactions.html", transactions=rows, pagination=pagination, filters=filters,
            product_choices=product_choices, date_error=date_error,
            date_from=request.args.get("from", ""), date_to=request.args.get("to", ""),
        )


# ==========================================================================
# Reports and CSV exports
# ==========================================================================

def register_report_routes(app):
    @app.get("/reports")
    @login_required
    def reports_page():
        start_day, end_day, range_error = reports.parse_report_range(
            request.args.get("from", ""), request.args.get("to", "")
        )
        data = reports.report_data(get_db(), g.user["id"], start_day, end_day)
        charts = {
            "currency": g.user["currency_label"],
            "daily": {
                "labels": [day["date"].strftime("%d %b") for day in data["daily"]],
                "revenue": [day["revenue"] for day in data["daily"]],
                "profit": [day["profit"] for day in data["daily"]],
                "has_data": any(day["sales_count"] for day in data["daily"]),
            },
            "best_sellers": {
                "labels": [row["name"] for row in data["best_sellers"]],
                "values": [row["units"] for row in data["best_sellers"]],
            },
        }
        return render_template(
            "reports.html", data=data, charts=charts, range_error=range_error,
            date_from=start_day.isoformat(), date_to=end_day.isoformat(),
        )

    def export_bounds():
        """Optional date range for exports; strict, because a download is deliberate."""
        start_utc, end_utc, error = local_filter_bounds(request.args)
        if error:
            abort(400, description=error)
        return start_utc or "0001-01-01 00:00:00", end_utc or "9999-12-31 00:00:00"

    @app.get("/export/products.csv")
    @login_required
    def export_products_csv():
        rows = reports.export_products(get_db(), g.user["id"])
        return helpers.csv_response(
            f"stockpilot-products-{helpers.today_stamp()}.csv",
            ["name", "sku", "category", "description", "cost_price", "selling_price", "quantity",
             "low_stock_threshold", "status", "created_at_utc", "updated_at_utc"],
            [[helpers.csv_safe(r["name"]), helpers.csv_safe(r["sku"]), helpers.csv_safe(r["category"]),
              helpers.csv_safe(r["description"]), helpers.money_input_value(r["cost_price"]),
              helpers.money_input_value(r["selling_price"]), r["quantity"], r["reorder_level"],
              "archived" if r["is_archived"] else "active", r["created_at"], r["updated_at"]]
             for r in rows],
        )

    @app.get("/export/sales.csv")
    @login_required
    def export_sales_csv():
        start_utc, end_utc = export_bounds()
        rows = reports.export_sales(get_db(), g.user["id"], start_utc, end_utc)
        return helpers.csv_response(
            f"stockpilot-sales-{helpers.today_stamp()}.csv",
            ["receipt", "date_utc", "product", "sku", "quantity", "unit_price", "unit_cost",
             "line_total", "line_gross_profit", "sale_notes"],
            [[f"S-{r['receipt_number']:06d}", r["created_at"], helpers.csv_safe(r["product_name_snapshot"]),
              helpers.csv_safe(r["sku_snapshot"]), r["quantity"], helpers.money_input_value(r["unit_price"]),
              helpers.money_input_value(r["unit_cost_snapshot"]), helpers.money_input_value(r["line_total"]),
              helpers.money_input_value(r["line_profit"]), helpers.csv_safe(r["notes"])]
             for r in rows],
        )

    @app.get("/export/transactions.csv")
    @login_required
    def export_transactions_csv():
        start_utc, end_utc = export_bounds()
        filters = {"start_utc": start_utc, "end_utc": end_utc}
        rows = reports.export_transactions(get_db(), g.user["id"], filters)
        return helpers.csv_response(
            f"stockpilot-transactions-{helpers.today_stamp()}.csv",
            ["date_utc", "product", "sku", "type", "quantity", "unit_price", "total_amount",
             "stock_after", "receipt", "notes"],
            [[r["created_at"], helpers.csv_safe(r["product_name"]), helpers.csv_safe(r["product_sku"]),
              r["transaction_type"], r["quantity"], helpers.money_input_value(r["unit_price"]),
              helpers.money_input_value(r["total_amount"]), r["stock_after"],
              f"S-{r['receipt_number']:06d}" if r["receipt_number"] else "", helpers.csv_safe(r["notes"])]
             for r in rows],
        )


# ==========================================================================
# Settings
# ==========================================================================

def register_settings_routes(app):
    def render_settings(profile_form=None, profile_errors=None, password_errors=None, status=200):
        form = profile_form or {
            "full_name": g.user["full_name"],
            "business_name": g.user["business_name"],
            "currency_label": g.user["currency_label"],
        }
        return render_template(
            "settings.html", form=form, profile_errors=profile_errors or {},
            password_errors=password_errors or {},
        ), status

    @app.get("/settings")
    @login_required
    def settings():
        return render_settings()

    @app.post("/settings/profile")
    @login_required
    def settings_profile():
        data, errors = validators.validate_profile(request.form)
        if errors:
            form = {key: request.form.get(key, "") for key in ("full_name", "business_name", "currency_label")}
            return render_settings(profile_form=form, profile_errors=errors, status=400)
        get_db().execute(
            "UPDATE users SET full_name = ?, business_name = ?, currency_label = ? WHERE id = ?",
            (data["full_name"], data["business_name"], data["currency_label"], g.user["id"]),
        )
        flash("Settings saved. Changing the currency label only changes how amounts are "
              "displayed; no amounts were converted.", "success")
        return redirect(url_for("settings"))

    @app.post("/settings/password")
    @login_required
    def settings_password():
        data, errors = validators.validate_password_change(request.form)
        if not errors:
            db = get_db()
            row = db.execute("SELECT password_hash FROM users WHERE id = ?", (g.user["id"],)).fetchone()
            if not helpers.verify_password(row["password_hash"], data["current_password"]):
                errors["current_password"] = "Your current password is incorrect."
        if errors:
            return render_settings(password_errors=errors, status=400)
        db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (helpers.hash_password(data["new_password"]), g.user["id"]),
        )
        user_id = g.user["id"]
        session.clear()  # start a fresh session after a credential change
        session["user_id"] = user_id
        session.permanent = True
        flash("Your password was changed.", "success")
        return redirect(url_for("settings"))


# ==========================================================================
# Errors: friendly pages for people, JSON for the JavaScript API
# ==========================================================================

def register_error_handlers(app):
    def respond(error, code, title, message):
        if wants_json():
            return jsonify(ok=False, error=message), code
        return render_template("error.html", code=code, title=title, message=message), code

    @app.errorhandler(400)
    def bad_request(error):
        return respond(error, 400, "Request not accepted", getattr(error, "description", None) or "The request could not be processed.")

    @app.errorhandler(404)
    def not_found(error):
        if wants_json():
            return respond(error, 404, "", "Not found.")
        return render_template("404.html"), 404

    @app.errorhandler(405)
    def method_not_allowed(error):
        return respond(error, 405, "Method not allowed", "That action is not allowed on this page.")

    @app.errorhandler(413)
    def too_large(error):
        return respond(error, 413, "Request too large", "The request was too large.")

    @app.errorhandler(500)
    def server_error(error):
        # The traceback goes to the server log only - never to the browser.
        if wants_json():
            return respond(error, 500, "", "Something went wrong on our side. Nothing was saved. Please try again.")
        return render_template("500.html"), 500


# Some hosts (for example Vercel) import this file and look for a variable called ``app``.
# It is created only there, so importing app.py anywhere else (the tests, ``flask --app app run``,
# which finds create_app by itself) has no side effects such as creating folders or a database.
if running_on_serverless():
    app = create_app()
