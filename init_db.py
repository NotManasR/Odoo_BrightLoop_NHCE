import os
import psycopg2

# Get database URL from environment or use local PostgreSQL default
DATABASE_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://postgres:postgres@localhost:5432/brightloop'
)


# SQL Schema and Initial Seed Data
SQL_SCRIPT = """
-- 1. Users
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100),
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(50) NOT NULL
        CHECK (role IN ('Admin', 'Staff', 'Customer'))
);

-- 2. Products
CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    type VARCHAR(100),
    sales_price DECIMAL(10, 2) NOT NULL,
    cost_price DECIMAL(10, 2),
    notes TEXT
);

-- 3. Product Variants
CREATE TABLE IF NOT EXISTS product_variants (
    id SERIAL PRIMARY KEY,
    product_id INT REFERENCES products(id) ON DELETE CASCADE,
    variant_name VARCHAR(255) NOT NULL,
    extra_price DECIMAL(10, 2) DEFAULT 0.00
);

-- 4. Plans
CREATE TABLE IF NOT EXISTS plans (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    price DECIMAL(10, 2) NOT NULL,
    billing_period VARCHAR(50)
        CHECK (billing_period IN ('Daily', 'Weekly', 'Monthly', 'Yearly')),
    min_quantity INT DEFAULT 1,
    available_start_date DATE,
    available_end_date DATE,
    is_closable BOOLEAN DEFAULT TRUE,
    is_pausable BOOLEAN DEFAULT TRUE,
    is_renewable BOOLEAN DEFAULT TRUE
);

-- 5. Taxes
CREATE TABLE IF NOT EXISTS taxes (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    percentage DECIMAL(5, 2) NOT NULL
);

-- 6. Discounts
CREATE TABLE IF NOT EXISTS discounts (
    id SERIAL PRIMARY KEY,
    product_id INT REFERENCES products(id) ON DELETE SET NULL,
    discount_type VARCHAR(20)
        CHECK (discount_type IN ('fixed', 'percent')),
    discount_value DECIMAL(10, 2) NOT NULL,
    min_purchase DECIMAL(10, 2) DEFAULT 0,
    min_quantity INT DEFAULT 0,
    start_date DATE,
    end_date DATE,
    usage_limit INT,
    times_used INT DEFAULT 0
);

-- 7. Subscriptions
CREATE TABLE IF NOT EXISTS subscriptions (
    id SERIAL PRIMARY KEY,
    subscription_number VARCHAR(100) UNIQUE NOT NULL,
    customer_id INT REFERENCES users(id) ON DELETE CASCADE,
    plan_id INT REFERENCES plans(id),
    status VARCHAR(50)
        CHECK (status IN ('Draft', 'Quotation', 'Confirmed', 'Active', 'Closed')),
    start_date DATE,
    expiry_date DATE,
    payment_terms VARCHAR(100)
);

-- 8. Subscription Lines
CREATE TABLE IF NOT EXISTS subscription_lines (
    id SERIAL PRIMARY KEY,
    subscription_id INT REFERENCES subscriptions(id) ON DELETE CASCADE,
    product_id INT REFERENCES products(id),
    tax_id INT REFERENCES taxes(id),
    quantity INT NOT NULL DEFAULT 1,
    unit_price DECIMAL(10, 2) NOT NULL
);

-- 9. Invoices
CREATE TABLE IF NOT EXISTS invoices (
    id SERIAL PRIMARY KEY,
    invoice_number VARCHAR(100) UNIQUE NOT NULL,
    subscription_id INT REFERENCES subscriptions(id),
    customer_id INT REFERENCES users(id),
    status VARCHAR(50)
        CHECK (status IN ('Draft', 'Confirmed', 'Paid')),
    due_date DATE,
    subtotal DECIMAL(10, 2) NOT NULL,
    tax_total DECIMAL(10, 2) DEFAULT 0,
    discount_total DECIMAL(10, 2) DEFAULT 0,
    total_amount DECIMAL(10, 2) NOT NULL
);

-- 10. Invoice Lines
CREATE TABLE IF NOT EXISTS invoice_lines (
    id SERIAL PRIMARY KEY,
    invoice_id INT REFERENCES invoices(id) ON DELETE CASCADE,
    quantity INT NOT NULL,
    price DECIMAL(10, 2) NOT NULL,
    tax_amount DECIMAL(10, 2) DEFAULT 0,
    discount_amount DECIMAL(10, 2) DEFAULT 0
);

-- 11. Payments
CREATE TABLE IF NOT EXISTS payments (
    id SERIAL PRIMARY KEY,
    invoice_id INT REFERENCES invoices(id) ON DELETE CASCADE,
    method VARCHAR(100) NOT NULL,
    amount DECIMAL(10, 2) NOT NULL,
    payment_date DATE NOT NULL,
    note TEXT
);


-- ===================================
-- SEED INITIAL MOCK DATA
-- ===================================

INSERT INTO users (id, name, email, password_hash, role)
VALUES
    (1, 'System Admin', 'admin@brightloop.com', 'pwd_admin', 'Admin'),
    (2, 'Internal Staff', 'staff@brightloop.com', 'pwd_staff', 'Staff'),
    (3, 'Shell Procurement', 'procurement@shell.com', 'pwd_shell', 'Customer'),
    (4, 'Infosys Accounts', 'accounts@infosys.com', 'pwd_infosys', 'Customer')
ON CONFLICT (email) DO NOTHING;


INSERT INTO products (
    id, name, type, sales_price, cost_price, notes
)
VALUES (
    1, 'Business ERP Software', 'Service',
    1000.00, 400.00, 'Enterprise ERP Suite'
)
ON CONFLICT (id) DO NOTHING;


INSERT INTO product_variants (
    id, product_id, variant_name, extra_price
)
VALUES (
    1, 1, 'Odoo Brand Edition', 560.00
)
ON CONFLICT (id) DO NOTHING;


INSERT INTO plans (
    id, name, price, billing_period, min_quantity,
    is_closable, is_pausable, is_renewable
)
VALUES (
    1, 'Standard Monthly Plan', 1000.00,
    'Monthly', 1, TRUE, TRUE, TRUE
)
ON CONFLICT (id) DO NOTHING;


INSERT INTO taxes (id, name, percentage)
VALUES (1, 'Standard Sales Tax', 10.00)
ON CONFLICT (id) DO NOTHING;


INSERT INTO discounts (
    id, product_id, discount_type, discount_value,
    min_quantity, usage_limit, times_used
)
VALUES (
    1, 1, 'percent', 10.00, 5, 100, 0
)
ON CONFLICT (id) DO NOTHING;


-- Synchronise PostgreSQL sequences with inserted IDs
SELECT setval('users_id_seq', (SELECT MAX(id) FROM users));
SELECT setval('products_id_seq', (SELECT MAX(id) FROM products));
SELECT setval('product_variants_id_seq', (SELECT MAX(id) FROM product_variants));
SELECT setval('plans_id_seq', (SELECT MAX(id) FROM plans));
SELECT setval('taxes_id_seq', (SELECT MAX(id) FROM taxes));
SELECT setval('discounts_id_seq', (SELECT MAX(id) FROM discounts));
"""


def setup_database():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute(SQL_SCRIPT)

    conn.commit()

    print("✅ PostgreSQL database successfully initialized and seeded!")

    cursor.close()
    conn.close()


if __name__ == '__main__':
    setup_database()