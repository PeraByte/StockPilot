"""Database helpers for StockPilot.

A thin layer around Python's built-in sqlite3 module:

* ``get_db()``       - one connection per request, stored on Flask's ``g`` object.
* ``transaction()``  - context manager that wraps several statements so they
                       either ALL succeed or ALL roll back.
* ``init_db()``      - creates missing tables from schema.sql (never deletes data).
* ``reset_db()``     - drops everything and rebuilds it (development only).
* Two CLI commands:  ``flask --app app init-db`` and ``flask --app app reset-db``.
"""

import os
import sqlite3
from contextlib import contextmanager

import click
from flask import current_app, g

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

# Tables in dependency order (children first) so they can be dropped safely.
TABLES_CHILD_FIRST = ("sale_items", "transactions", "sales", "products", "users")


def connect(path):
    """Open a SQLite connection configured the way StockPilot needs it."""
    # isolation_level=None turns off Python's implicit transaction handling.
    # We issue BEGIN / COMMIT / ROLLBACK ourselves in transaction(), which makes
    # it obvious (and testable) exactly which statements belong together.
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    # SQLite ignores foreign keys unless this is switched on for EVERY connection.
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def get_db():
    """Return this request's database connection, opening it on first use."""
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(exception=None):
    """Close the connection at the end of the request."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def transaction(db):
    """Run a block of statements as one atomic unit.

    ``BEGIN IMMEDIATE`` takes SQLite's write lock straight away. Two checkouts
    that arrive at the same moment therefore run one after the other, so the
    second one sees the stock left behind by the first. If anything raises an
    exception the whole block is rolled back and the exception continues upward.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


def init_db():
    """Create any missing tables and indexes. Existing data is left untouched."""
    db = get_db()
    with open(SCHEMA_PATH, encoding="utf-8") as schema_file:
        db.executescript(schema_file.read())


def reset_db():
    """Delete every table and recreate an empty schema. DESTROYS ALL DATA."""
    db = get_db()
    db.execute("PRAGMA foreign_keys = OFF")  # allow dropping in any order
    for table in TABLES_CHILD_FIRST:
        db.execute(f"DROP TABLE IF EXISTS {table}")  # fixed names from the tuple above
    db.execute("PRAGMA foreign_keys = ON")
    init_db()


@click.command("init-db")
def init_db_command():
    """Create the database tables if they do not exist yet (safe to repeat)."""
    init_db()
    click.echo(f"Database ready: {current_app.config['DATABASE']}")


@click.command("reset-db")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def reset_db_command(yes):
    """DELETE ALL DATA and rebuild an empty database."""
    if not yes:
        click.confirm(
            "This will permanently delete ALL users, products and sales. Continue?",
            abort=True,
        )
    reset_db()
    click.echo("Database reset. All previous data has been deleted.")


def register_database(app):
    """Hook the database helpers into a Flask application."""
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
    app.cli.add_command(reset_db_command)
