"""Small reusable helpers: money, dates, CSRF, login protection, pagination, CSV.

Nothing in this file talks to the database. Keeping these functions small and
independent makes them easy to read, reuse and unit-test.
"""

import csv
import hmac
import io
import re
import secrets
from datetime import datetime, time, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Response, current_app, flash, g, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------
# Money is stored and calculated as a whole number of minor units (paisa).
# 125.50 is stored as 12550. Integer arithmetic is exact, so totals never
# drift by a paisa the way floating-point numbers can (0.1 + 0.2 != 0.3).

MAX_MONEY_MINOR = 99_999_999_999  # 999,999,999.99 - far above any realistic price
_MONEY_PATTERN = re.compile(r"[0-9]+(\.[0-9]{1,2})?")  # [0-9] on purpose: \d also matches non-Latin digits


def parse_money(text):
    """Convert text such as '1,250.5' into minor units (125050).

    Raises ValueError with a message that is safe to show to the user.
    """
    cleaned = (text or "").strip().replace(",", "")
    if not cleaned:
        raise ValueError("Enter an amount.")
    if cleaned.startswith("-"):
        raise ValueError("Amount cannot be negative.")
    if not _MONEY_PATTERN.fullmatch(cleaned):
        raise ValueError("Enter a valid amount with at most two decimals, such as 125.50.")
    whole, _, fraction = cleaned.partition(".")
    minor_units = int(whole) * 100 + int((fraction + "00")[:2])
    if minor_units > MAX_MONEY_MINOR:
        raise ValueError("Amount is too large.")
    return minor_units


def format_money(minor_units, label=""):
    """12550 -> 'PKR 125.50' (thousands separators, always two decimals)."""
    minor_units = int(minor_units or 0)
    sign = "-" if minor_units < 0 else ""
    whole, fraction = divmod(abs(minor_units), 100)
    number = f"{whole:,}.{fraction:02d}"
    return f"{sign}{label} {number}" if label else f"{sign}{number}"


def money_input_value(minor_units):
    """12550 -> '125.50' - the plain form used inside <input> fields and CSV files."""
    whole, fraction = divmod(abs(int(minor_units or 0)), 100)
    sign = "-" if minor_units and minor_units < 0 else ""
    return f"{sign}{whole}.{fraction:02d}"


_INTEGER_PATTERN = re.compile(r"[0-9]{1,9}")


def parse_whole_number(text, *, label, minimum=0, maximum=1_000_000):
    """Parse a whole number such as a quantity. Raises ValueError with a friendly message."""
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError(f"{label} is required.")
    if cleaned.startswith("-"):
        raise ValueError(f"{label} cannot be negative.")
    if not _INTEGER_PATTERN.fullmatch(cleaned):
        raise ValueError(f"{label} must be a whole number.")
    number = int(cleaned)
    if number < minimum:
        raise ValueError(f"{label} must be at least {minimum}.")
    if number > maximum:
        raise ValueError(f"{label} cannot be more than {maximum:,}.")
    return number


def choice(value, allowed, default):
    """Return value if it is one of the allowed options, otherwise the default."""
    return value if value in allowed else default


# --------------------------------------------------------------------------
# Dates and time zones
# --------------------------------------------------------------------------
# The database stores UTC text ('YYYY-MM-DD HH:MM:SS'). We convert to the
# business's local time zone only for display and for "today" / date-range
# calculations. The zone is set with the STOCKPILOT_TIMEZONE setting.

DB_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_timezone():
    name = current_app.config.get("TIMEZONE", "UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def utc_now():
    """Current UTC time as a naive datetime without microseconds."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def to_db_time(moment):
    return moment.strftime(DB_TIME_FORMAT)


def to_local(db_text):
    """Convert a stored UTC string into an aware datetime in the local zone."""
    moment = datetime.strptime(db_text, DB_TIME_FORMAT).replace(tzinfo=timezone.utc)
    return moment.astimezone(get_timezone())


def format_local(db_text, pattern="%d %b %Y, %I:%M %p"):
    if not db_text:
        return ""
    return to_local(db_text).strftime(pattern)


def local_today():
    return datetime.now(get_timezone()).date()


def parse_date(text):
    """'2026-03-15' -> date, or None when the text is empty or invalid."""
    try:
        return datetime.strptime((text or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def local_midnight_as_utc(day):
    """UTC database timestamp for 00:00 local time on the given date."""
    local_start = datetime.combine(day, time.min, tzinfo=get_timezone())
    return to_db_time(local_start.astimezone(timezone.utc))


def day_bounds_utc(start_day, end_day):
    """UTC [start, end) timestamps covering start_day..end_day inclusive (local dates)."""
    return local_midnight_as_utc(start_day), local_midnight_as_utc(end_day + timedelta(days=1))


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------

def parse_page(text):
    try:
        return max(1, int(text))
    except (TypeError, ValueError):
        return 1


def paginate(total, page, per_page):
    """Work out the numbers a template needs to draw page links."""
    pages = max(1, -(-total // per_page))  # ceiling division
    page = min(max(page, 1), pages)
    offset = (page - 1) * per_page
    return {
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "total": total,
        "offset": offset,
        "first": offset + 1 if total else 0,
        "last": min(offset + per_page, total),
    }


def like_pattern(text):
    """Build a LIKE pattern that treats %, _ and \\ typed by the user literally.

    Use together with `ESCAPE '\\'` in the SQL.
    """
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------

def hash_password(password):
    """Hash a password with Werkzeug (scrypt by default, PBKDF2 where scrypt is missing)."""
    try:
        return generate_password_hash(password)
    except (ValueError, AttributeError):  # some Python builds lack scrypt
        return generate_password_hash(password, method="pbkdf2:sha256")


_dummy_hash = None


def verify_password(stored_hash, password):
    """check_password_hash wrapper that also takes the same time for unknown users.

    When a login uses an email that does not exist, stored_hash is None. We
    still hash-compare against a throw-away hash so response time does not
    reveal which emails have accounts.
    """
    global _dummy_hash
    if stored_hash is None:
        if _dummy_hash is None:
            _dummy_hash = hash_password(secrets.token_urlsafe(16))
        check_password_hash(_dummy_hash, password)
        return False
    return check_password_hash(stored_hash, password)


# --------------------------------------------------------------------------
# CSRF protection
# --------------------------------------------------------------------------
# Every form includes a hidden random token that is also stored in the user's
# session. A forged request from another website cannot read that token, so it
# cannot supply it. JavaScript requests send it in the X-CSRF-Token header.

def get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def csrf_is_valid():
    expected = session.get("csrf_token", "")
    submitted = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    return bool(expected) and hmac.compare_digest(expected, submitted)


# --------------------------------------------------------------------------
# Login protection and redirects
# --------------------------------------------------------------------------

def login_required(view):
    """Decorator: only logged-in users may use the view."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            if request.path.startswith("/api/"):
                return jsonify(ok=False, error="Your session has ended. Please log in again."), 401
            flash("Please log in to continue.", "info")
            if request.method == "GET":
                target = request.full_path.rstrip("?")
                return redirect(url_for("login", next=target))
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def safe_next_url(target):
    """Only allow redirects to paths on this site (prevents open-redirect attacks)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


# --------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------
# Spreadsheet programs treat a cell that starts with = + - @ as a formula.
# Someone could name a product '=HYPERLINK("http://evil")' and have it run when
# the owner opens the export. Prefixing an apostrophe turns it back into text.
# Only user-typed TEXT is passed through csv_safe; numbers we format ourselves
# (some are legitimately negative) are written as they are.

_FORMULA_STARTS = ("=", "+", "-", "@")


def csv_safe(value):
    text = "" if value is None else str(value)
    if text.lstrip(" \t\r\n")[:1] in _FORMULA_STARTS or text[:1] in ("\t", "\r"):
        return "'" + text
    return text


def csv_response(filename, header, rows):
    """Build a downloadable CSV response (UTF-8 with BOM so Excel shows accents correctly)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    response = Response("﻿" + buffer.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def today_stamp():
    """Date part used in export file names, e.g. 20261023."""
    return local_today().strftime("%Y%m%d")
