-- StockPilot database schema (SQLite)
--
-- Every statement uses IF NOT EXISTS, so running this file against an existing
-- database is safe: it creates anything that is missing and never touches rows.
-- (To wipe everything on purpose, use `flask --app app reset-db`.)
--
-- Conventions
--   * Money is stored as INTEGER minor units (paisa/cents): 125.50 is stored as 12550.
--     Integers avoid the rounding errors that floating-point numbers cause.
--   * Timestamps are stored as UTC text in the form 'YYYY-MM-DD HH:MM:SS'.
--     They are converted to local time only when displayed.
--   * Foreign keys are enforced: the application turns on `PRAGMA foreign_keys`
--     for every connection (see database.py).

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name       TEXT    NOT NULL CHECK (length(trim(full_name)) BETWEEN 1 AND 100),
    business_name   TEXT    NOT NULL CHECK (length(trim(business_name)) BETWEEN 1 AND 100),
    email           TEXT    NOT NULL UNIQUE COLLATE NOCASE CHECK (length(email) BETWEEN 3 AND 254),
    password_hash   TEXT    NOT NULL,
    -- Display label only. Changing it never converts historical amounts.
    currency_label  TEXT    NOT NULL DEFAULT 'PKR' CHECK (length(trim(currency_label)) BETWEEN 1 AND 8),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS products (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users (id),
    name           TEXT    NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 120),
    -- NOCASE: "abc-1" and "ABC-1" are the same SKU for one business.
    sku            TEXT    NOT NULL COLLATE NOCASE CHECK (length(trim(sku)) BETWEEN 1 AND 40),
    category       TEXT    NOT NULL DEFAULT 'Uncategorized',
    description    TEXT    NOT NULL DEFAULT '',
    cost_price     INTEGER NOT NULL CHECK (cost_price >= 0),      -- minor units
    selling_price  INTEGER NOT NULL CHECK (selling_price >= 0),   -- minor units
    quantity       INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    reorder_level  INTEGER NOT NULL DEFAULT 5 CHECK (reorder_level >= 0),
    -- Soft delete: archived products keep their history but disappear from
    -- everyday lists, the dashboard and the point of sale.
    is_archived    INTEGER NOT NULL DEFAULT 0 CHECK (is_archived IN (0, 1)),
    archived_at    TEXT,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    updated_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    UNIQUE (user_id, sku),
    -- Lets other tables point at (id, user_id) so the database itself can
    -- refuse rows that mix one user's data with another user's product.
    UNIQUE (id, user_id)
);

CREATE TABLE IF NOT EXISTS sales (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users (id),
    -- Per-user running number shown on receipts (S-000001, S-000002, ...).
    receipt_number  INTEGER NOT NULL CHECK (receipt_number > 0),
    total_amount    INTEGER NOT NULL CHECK (total_amount >= 0),
    total_cost      INTEGER NOT NULL CHECK (total_cost >= 0),
    -- Estimated GROSS profit (revenue - cost of goods). May be negative if
    -- something was sold below cost.
    total_profit    INTEGER NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    notes           TEXT    NOT NULL DEFAULT '',
    UNIQUE (user_id, receipt_number),
    CHECK (total_profit = total_amount - total_cost)
);

CREATE TABLE IF NOT EXISTS sale_items (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id               INTEGER NOT NULL REFERENCES sales (id),
    product_id            INTEGER NOT NULL REFERENCES products (id),
    -- Snapshots: a receipt must keep showing what was true at the time of the
    -- sale even if the product is later renamed or re-priced.
    product_name_snapshot TEXT    NOT NULL,
    sku_snapshot          TEXT    NOT NULL DEFAULT '',
    quantity              INTEGER NOT NULL CHECK (quantity > 0),
    unit_cost_snapshot    INTEGER NOT NULL CHECK (unit_cost_snapshot >= 0),
    unit_price            INTEGER NOT NULL CHECK (unit_price >= 0),
    line_total            INTEGER NOT NULL CHECK (line_total >= 0),
    line_profit           INTEGER NOT NULL,
    CHECK (line_total = quantity * unit_price),
    CHECK (line_profit = line_total - quantity * unit_cost_snapshot)
);

-- Every change to stock writes exactly one row here (the "stock ledger").
CREATE TABLE IF NOT EXISTS transactions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users (id),
    product_id       INTEGER NOT NULL,
    sale_id          INTEGER REFERENCES sales (id),
    transaction_type TEXT    NOT NULL
        CHECK (transaction_type IN ('SALE', 'RESTOCK', 'ADJUSTMENT_IN', 'ADJUSTMENT_OUT')),
    -- Always a positive number; the type says whether stock went up or down.
    quantity         INTEGER NOT NULL CHECK (quantity > 0),
    -- SALE: selling price. RESTOCK: unit cost paid. ADJUSTMENT_*: current cost.
    unit_price       INTEGER NOT NULL CHECK (unit_price >= 0),
    total_amount     INTEGER NOT NULL CHECK (total_amount >= 0),
    -- Stock level immediately after this movement, so the history explains itself.
    stock_after      INTEGER NOT NULL CHECK (stock_after >= 0),
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    notes            TEXT    NOT NULL DEFAULT '',
    -- Composite foreign key: the product must belong to the same user.
    FOREIGN KEY (product_id, user_id) REFERENCES products (id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_products_user_name     ON products (user_id, name);
CREATE INDEX IF NOT EXISTS idx_products_user_category ON products (user_id, category);
CREATE INDEX IF NOT EXISTS idx_products_user_stock    ON products (user_id, is_archived, quantity);
CREATE INDEX IF NOT EXISTS idx_transactions_user_date ON transactions (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_transactions_product   ON transactions (product_id);
CREATE INDEX IF NOT EXISTS idx_transactions_sale      ON transactions (sale_id);
CREATE INDEX IF NOT EXISTS idx_sales_user_date        ON sales (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sale_items_sale        ON sale_items (sale_id);
CREATE INDEX IF NOT EXISTS idx_sale_items_product     ON sale_items (product_id);
