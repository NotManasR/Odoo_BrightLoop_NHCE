
import sqlite3

# SQL schema and initial seed data
SQL_SCRIPT = """
-- 1. Users
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('Admin', 'Staff', 'Customer'))
);

-- 2. Products
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT,
    sales_price REAL NOT NULL,
    cost_price REAL,
    notes TEXT
);

-- 3. Product Variants
CREATE TABLE IF NOT EXISTS product_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER,
    variant_name TEXT NOT NULL,
    extra_price REAL DEFAULT 0.00,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
);

-- 4. Plans
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    price REAL NOT NULL,
    billing_period TEXT CHECK (
        billing_period IN ('Daily', 'Weekly', 'Monthly', 'Yearly')
    ),
    min_quantity INTEGER DEFAULT 1,
    available_start_date DATE,
    available_end_date DATE,
    is_closable INTEGER DEFAULT 1,
    is_pausable INTEGER DEFAULT 1,
    is_renewable INTEGER DEFAULT 1
);

-- 5. Taxes
CREATE TABLE IF NOT EXISTS taxes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    percentage REAL NOT NULL
);

-- 6. Discounts
CREATE TABLE IF NOT EXISTS discounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER,
    discount_type TEXT CHECK (discount_type IN ('fixed', 'percent')),
    discount_value REAL NOT NULL,
    min_purchase REAL DEFAULT 0,
    min_quantity INTEGER DEFAULT 0,
    start_date DATE,
    end_date DATE,
    usage_limit INTEGER,
    times_used INTEGER DEFAULT 0,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL
);

-- 7. Subscriptions
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_number TEXT UNIQUE NOT NULL,
    customer_id INTEGER NOT NULL,
    plan_id INTEGER,
    status TEXT CHECK (
        status IN ('Draft', 'Quotation', 'Confirmed', 'Active', 'Closed')
    ),
    start_date DATE,
    expiry_date DATE,
    payment_terms TEXT,
    FOREIGN KEY (customer_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (plan_id) REFERENCES plans(id)
);

-- 8. Subscription Lines
CREATE TABLE IF NOT EXISTS subscription_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    product_id INTEGER,
    tax_id INTEGER,
    quantity INTEGER NOT NULL DEFAULT 1,
    unit_price REAL NOT NULL,
    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id),
    FOREIGN KEY (tax_id) REFERENCES taxes(id)
);

-- 9. Invoices
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_number TEXT UNIQUE NOT NULL,
    subscription_id INTEGER,
    customer_id INTEGER NOT NULL,
    status TEXT CHECK (status IN ('Draft', 'Confirmed', 'Paid')),
    due_date DATE,
    subtotal REAL NOT NULL,
    tax_total REAL DEFAULT 0,
    discount_total REAL DEFAULT 0,
    total_amount REAL NOT NULL,
    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id),
    FOREIGN KEY (customer_id) REFERENCES users(id)
);

-- 10. Invoice Lines
CREATE TABLE IF NOT EXISTS invoice_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    tax_amount REAL DEFAULT 0,
    discount_amount REAL DEFAULT 0,
    FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
);

-- 11. Payments
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL,
    method TEXT NOT NULL,
    amount REAL NOT NULL,
    payment_date DATE NOT NULL,
    note TEXT,
    FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
);

-- ===================================
-- SEED INITIAL MOCK DATA
-- ===================================

INSERT OR IGNORE INTO users (
    id, email, password_hash, role
)
VALUES
    (1, 'admin@brightloop.com', 'pwd_admin', 'Admin'),
    (2, 'staff@brightloop.com', 'pwd_staff', 'Staff'),
    (3, 'procurement@shell.com', 'pwd_shell', 'Customer'),
    (4, 'accounts@infosys.com', 'pwd_infosys', 'Customer');

INSERT OR IGNORE INTO products (
    id, name, type, sales_price, cost_price, notes
)
VALUES
    (1, 'Business ERP Software', 'Service', 1000.00, 400.00,
     'Enterprise ERP Suite');

INSERT OR IGNORE INTO product_variants (
    id, product_id, variant_name, extra_price
)
VALUES
    (1, 1, 'Odoo Brand Edition', 560.00);

INSERT OR IGNORE INTO plans (
    id, name, price, billing_period, min_quantity,
    is_closable, is_pausable, is_renewable
)
VALUES
    (1, 'Standard Monthly Plan', 1000.00, 'Monthly', 1, 1, 1, 1);

INSERT OR IGNORE INTO taxes (
    id, name, percentage
)
VALUES
    (1, 'Standard Sales Tax', 10.00);

INSERT OR IGNORE INTO discounts (
    id, product_id, discount_type, discount_value,
    min_quantity, usage_limit, times_used
)
VALUES
    (1, 1, 'percent', 10.00, 5, 100, 0);
"""


def setup_database():
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()

    # Execute SQL script
    cursor.executescript(SQL_SCRIPT)
    conn.commit()

    # Verify database setup
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table';"
    )

    tables = [
        row[0]
        for row in cursor.fetchall()
        if row[0] != 'sqlite_sequence'
    ]

    print("✅ Database successfully initialized!")
    print(f"Created {len(tables)} tables: {', '.join(tables)}")

    conn.close()


if __name__ == '__main__':
    setup_database()
