"""Server-side validation for every form.

Each ``validate_*`` function takes the submitted form and returns a pair:

    (clean_data, errors)

``errors`` is a dictionary {field_name: message}. If it is empty the data is
valid and ``clean_data`` holds the converted values (money as integer minor
units, numbers as int, text stripped). Browsers also check many of these rules
with HTML attributes, but those checks can be bypassed, so the server always
repeats them.
"""

import re

from helpers import parse_money, parse_whole_number

EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
SKU_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/\-]*")
CURRENCY_PATTERN = re.compile(r"[^\s<>&\"']{1,8}")

PASSWORD_MIN, PASSWORD_MAX = 8, 128
MAX_QUANTITY = 1_000_000

STOCK_ACTIONS = {
    # form value      -> (transaction type,  label)
    "restock": "RESTOCK",
    "adjust_in": "ADJUSTMENT_IN",
    "adjust_out": "ADJUSTMENT_OUT",
}


def _text(form, name):
    return (form.get(name) or "").strip()


def _single_line(form, name, label, *, maximum, required=True):
    """Read a one-line text field and check its length and characters."""
    value = " ".join(_text(form, name).split())  # collapse repeated spaces
    if not value:
        return value, (f"{label} is required." if required else None)
    if len(value) > maximum:
        return value, f"{label} must be {maximum} characters or fewer."
    if not value.isprintable():
        return value, f"{label} contains invalid characters."
    return value, None


def _check_password_strength(password, errors, field="password"):
    if len(password) < PASSWORD_MIN:
        errors[field] = f"Password must be at least {PASSWORD_MIN} characters."
    elif len(password) > PASSWORD_MAX:
        errors[field] = f"Password must be {PASSWORD_MAX} characters or fewer."
    elif not (re.search(r"[A-Za-z]", password) and re.search(r"[0-9]", password)):
        errors[field] = "Password must contain at least one letter and one number."


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------

def validate_registration(form):
    errors = {}
    full_name, error = _single_line(form, "full_name", "Full name", maximum=100)
    if error:
        errors["full_name"] = error
    business_name, error = _single_line(form, "business_name", "Business name", maximum=100)
    if error:
        errors["business_name"] = error

    email = _text(form, "email").lower()
    if not email:
        errors["email"] = "Email address is required."
    elif len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        errors["email"] = "Enter a valid email address."

    password = form.get("password") or ""
    confirmation = form.get("confirm_password") or ""
    _check_password_strength(password, errors)
    if "password" not in errors and password != confirmation:
        errors["confirm_password"] = "Passwords do not match."

    return {
        "full_name": full_name,
        "business_name": business_name,
        "email": email,
        "password": password,
    }, errors


def validate_login(form):
    errors = {}
    email = _text(form, "email").lower()
    password = form.get("password") or ""
    if not email:
        errors["email"] = "Enter your email address."
    if not password:
        errors["password"] = "Enter your password."
    return {"email": email, "password": password}, errors


def validate_profile(form):
    errors = {}
    full_name, error = _single_line(form, "full_name", "Full name", maximum=100)
    if error:
        errors["full_name"] = error
    business_name, error = _single_line(form, "business_name", "Business name", maximum=100)
    if error:
        errors["business_name"] = error
    currency_label = _text(form, "currency_label")
    if not CURRENCY_PATTERN.fullmatch(currency_label):
        errors["currency_label"] = "Use 1 to 8 characters without spaces, for example PKR, Rs or $."
    return {
        "full_name": full_name,
        "business_name": business_name,
        "currency_label": currency_label,
    }, errors


def validate_password_change(form):
    errors = {}
    current = form.get("current_password") or ""
    new = form.get("new_password") or ""
    confirmation = form.get("confirm_password") or ""
    if not current:
        errors["current_password"] = "Enter your current password."
    _check_password_strength(new, errors, field="new_password")
    if "new_password" not in errors:
        if new == current:
            errors["new_password"] = "Choose a password that is different from the current one."
        elif new != confirmation:
            errors["confirm_password"] = "New passwords do not match."
    return {"current_password": current, "new_password": new}, errors


# --------------------------------------------------------------------------
# Products and stock
# --------------------------------------------------------------------------

def validate_product(form, *, creating):
    """Validate the create / edit product form.

    The initial quantity is only read when creating. Afterwards stock may only
    change through the stock-movement form so that every change is recorded.
    """
    errors = {}

    name, error = _single_line(form, "name", "Product name", maximum=120)
    if error:
        errors["name"] = error

    sku = _text(form, "sku").upper()
    if not sku:
        errors["sku"] = "SKU is required."
    elif len(sku) > 40:
        errors["sku"] = "SKU must be 40 characters or fewer."
    elif not SKU_PATTERN.fullmatch(sku):
        errors["sku"] = "SKU may contain letters, numbers, dots, dashes, slashes and underscores only."

    category, error = _single_line(form, "category", "Category", maximum=60, required=False)
    if error:
        errors["category"] = error
    category = category or "Uncategorized"

    description = _text(form, "description")
    if len(description) > 500:
        errors["description"] = "Description must be 500 characters or fewer."

    prices = {}
    for field, label in (("cost_price", "Cost price"), ("selling_price", "Selling price")):
        try:
            prices[field] = parse_money(form.get(field))
        except ValueError as problem:
            errors[field] = f"{label}: {problem}"

    try:
        reorder_level = parse_whole_number(
            form.get("reorder_level"), label="Low-stock threshold", maximum=MAX_QUANTITY
        )
    except ValueError as problem:
        errors["reorder_level"] = str(problem)
        reorder_level = None

    quantity = 0
    if creating:
        raw_quantity = _text(form, "quantity")
        if raw_quantity:  # blank means "start with no stock"
            try:
                quantity = parse_whole_number(raw_quantity, label="Initial quantity", maximum=MAX_QUANTITY)
            except ValueError as problem:
                errors["quantity"] = str(problem)

    return {
        "name": name,
        "sku": sku,
        "category": category,
        "description": description,
        "cost_price": prices.get("cost_price"),
        "selling_price": prices.get("selling_price"),
        "reorder_level": reorder_level,
        "quantity": quantity,
    }, errors


def validate_stock_movement(form):
    errors = {}
    action = _text(form, "action")
    if action not in STOCK_ACTIONS:
        errors["action"] = "Choose what kind of stock change this is."

    quantity = None
    try:
        quantity = parse_whole_number(form.get("quantity"), label="Quantity", minimum=1, maximum=MAX_QUANTITY)
    except ValueError as problem:
        errors["quantity"] = str(problem)

    notes = _text(form, "notes")
    if len(notes) > 200:
        errors["notes"] = "Notes must be 200 characters or fewer."
    elif action in ("adjust_in", "adjust_out") and not notes:
        errors["notes"] = "Please explain the adjustment (for example: damaged, counted again, returned)."

    unit_cost = None
    raw_cost = _text(form, "unit_cost")
    if action == "restock" and raw_cost:  # blank means "use the product's current cost"
        try:
            unit_cost = parse_money(raw_cost)
        except ValueError as problem:
            errors["unit_cost"] = f"Unit cost: {problem}"

    return {
        "action": action,
        "movement_type": STOCK_ACTIONS.get(action),
        "quantity": quantity,
        "unit_cost": unit_cost,
        "notes": notes,
        "update_cost": action == "restock" and form.get("update_cost") == "on" and unit_cost is not None,
    }, errors
