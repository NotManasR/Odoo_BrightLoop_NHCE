import re
from werkzeug.security import generate_password_hash
from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor
import datetime
import os


app = Flask(__name__)
CORS(app)


DATABASE_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://postgres:postgres@localhost:5432/brightloop'
)


def get_db():
    conn = psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor
    )
    return conn

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.json or {}

    name = (data.get('username') or '').strip()
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''

    if len(name) < 3:
        return jsonify({"error": "Username must be at least 3 characters"}), 400
    if not re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', email):
        return jsonify({"error": "Enter a valid email address"}), 400
    if (len(password) <= 8
            or not re.search(r'[A-Z]', password)
            or not re.search(r'[a-z]', password)
            or not re.search(r'[^A-Za-z0-9]', password)):
        return jsonify({"error": "Password must be more than 8 characters with an uppercase letter, a lowercase letter and a special character"}), 400

    password_hash = generate_password_hash(password,method='pbkdf2:sha256')

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO users (name, email, password_hash, role)
            VALUES (%s, %s, %s, 'Customer')
            RETURNING id, name, email, role
        """, (name, email, password_hash))
        user = cursor.fetchone()
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({"error": "An account with this email already exists"}), 409
    finally:
        cursor.close()
        conn.close()

    return jsonify(user), 201
# ============================================================
# 1. GET PRODUCTS & VARIANTS
# ============================================================

@app.route('/api/products', methods=['GET'])
def get_products():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM products")
    products = cursor.fetchall()

    result = []

    for p in products:
        p_dict = dict(p)

        cursor.execute(
            "SELECT * FROM product_variants WHERE product_id = %s",
            (p['id'],)
        )

        p_dict['variants'] = [
            dict(v) for v in cursor.fetchall()
        ]

        result.append(p_dict)

    cursor.close()
    conn.close()

    return jsonify(result)


# ============================================================
# 2. GET SUBSCRIPTIONS & LINE ITEMS
# ============================================================

@app.route('/api/subscriptions', methods=['GET'])
def get_subscriptions():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            s.*,
            u.email AS customer_email,
            p.name AS plan_name
        FROM subscriptions s
        JOIN users u ON s.customer_id = u.id
        LEFT JOIN plans p ON s.plan_id = p.id
    """)

    subs = cursor.fetchall()

    result = []

    for s in subs:
        s_dict = dict(s)

        cursor.execute("""
            SELECT
                sl.*,
                pr.name AS product_name,
                t.percentage AS tax_rate
            FROM subscription_lines sl
            JOIN products pr ON sl.product_id = pr.id
            LEFT JOIN taxes t ON sl.tax_id = t.id
            WHERE sl.subscription_id = %s
        """, (s['id'],))

        s_dict['lines'] = [
            dict(l) for l in cursor.fetchall()
        ]

        result.append(s_dict)

    cursor.close()
    conn.close()

    return jsonify(result)


# ============================================================
# 3. CREATE QUOTATION
# ============================================================

@app.route('/api/subscriptions/quote', methods=['POST'])
def create_quote():
    data = request.json or {}

    customer_id = data.get('customer_id', 3)  # Default: Shell (id=3)
    plan_id = data.get('plan_id', 1)
    quantity = data.get('quantity', 10)

    conn = get_db()
    cursor = conn.cursor()

    sub_num = f"SUB-{int(datetime.datetime.now().timestamp())}"

    cursor.execute("""
        INSERT INTO subscriptions (
            subscription_number,
            customer_id,
            plan_id,
            status,
            start_date,
            expiry_date,
            payment_terms
        )
        VALUES (
            %s, %s, %s, 'Quotation',
            CURRENT_DATE,
            CURRENT_DATE + INTERVAL '30 days',
            'Net 30'
        )
        RETURNING id
    """, (sub_num, customer_id, plan_id))

    sub_id = cursor.fetchone()['id']

    cursor.execute("""
        INSERT INTO subscription_lines (
            subscription_id,
            product_id,
            tax_id,
            quantity,
            unit_price
        )
        VALUES (%s, 1, 1, %s, 1000.00)
    """, (sub_id, quantity))

    conn.commit()

    cursor.close()
    conn.close()

    return jsonify({
        "message": "Quotation created",
        "subscription_id": sub_id,
        "subscription_number": sub_num
    }), 201


# ============================================================
# 4. STATE MACHINE TRANSITION
# ============================================================

@app.route('/api/subscriptions/transition', methods=['POST'])
def transition_subscription():
    data = request.json or {}

    sub_id = data.get('subscription_id')
    new_status = data.get('status')

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE subscriptions SET status = %s WHERE id = %s",
        (new_status, sub_id)
    )

    conn.commit()

    cursor.close()
    conn.close()

    return jsonify({
        "message": f"Subscription updated to {new_status}"
    })


# ============================================================
# 5. RUN BILLING CYCLE
# ============================================================

@app.route('/api/billing/run', methods=['POST'])
def run_billing():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT s.*
        FROM subscriptions s
        WHERE s.status = 'Active'
        AND s.id NOT IN (
            SELECT subscription_id
            FROM invoices
            WHERE subscription_id IS NOT NULL
        )
    """)

    active_subs = cursor.fetchall()

    generated_invoices = []

    for sub in active_subs:

        cursor.execute("""
            SELECT *
            FROM subscription_lines
            WHERE subscription_id = %s
        """, (sub['id'],))

        lines = cursor.fetchall()

        subtotal = float(
            sum(l['quantity'] * l['unit_price'] for l in lines)
        )

        total_qty = sum(
            l['quantity'] for l in lines
        )

        # Calculate 10% tax
        tax_total = subtotal * 0.10

        # Apply 10% discount if quantity >= 5
        discount_total = (
            subtotal * 0.10
            if total_qty >= 5
            else 0.0
        )

        total_amount = subtotal + tax_total - discount_total

        inv_num = f"INV-{sub['subscription_number']}"

        cursor.execute("""
            INSERT INTO invoices (
                invoice_number,
                subscription_id,
                customer_id,
                status,
                due_date,
                subtotal,
                tax_total,
                discount_total,
                total_amount
            )
            VALUES (
                %s, %s, %s, 'Confirmed',
                CURRENT_DATE + INTERVAL '15 days',
                %s, %s, %s, %s
            )
            RETURNING id
        """, (
            inv_num,
            sub['id'],
            sub['customer_id'],
            subtotal,
            tax_total,
            discount_total,
            total_amount
        ))

        inv_id = cursor.fetchone()['id']

        for l in lines:

            line_price = float(
                l['quantity'] * l['unit_price']
            )

            cursor.execute("""
                INSERT INTO invoice_lines (
                    invoice_id,
                    quantity,
                    price,
                    tax_amount,
                    discount_amount
                )
                VALUES (%s, %s, %s, %s, %s)
            """, (
                inv_id,
                l['quantity'],
                line_price,
                line_price * 0.10,
                (line_price * 0.10) if total_qty >= 5 else 0.0
            ))

        generated_invoices.append(inv_num)

    conn.commit()

    cursor.close()
    conn.close()

    return jsonify({
        "message": (
            f"Billing cycle completed. "
            f"Generated {len(generated_invoices)} invoices."
        ),
        "invoices": generated_invoices
    })


# ============================================================
# 6. GET INVOICES LIST
# ============================================================

@app.route('/api/invoices', methods=['GET'])
def get_invoices():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            i.*,
            u.email AS customer_email
        FROM invoices i
        JOIN users u ON i.customer_id = u.id
    """)

    invoices = cursor.fetchall()

    cursor.close()
    conn.close()

    return jsonify([
        dict(inv) for inv in invoices
    ])


# ============================================================
# 7. LOG PAYMENT
# ============================================================

@app.route('/api/invoices/pay', methods=['POST'])
def pay_invoice():
    data = request.json or {}

    inv_id = data.get('invoice_id')
    method = data.get('method', 'Bank Transfer')

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT total_amount FROM invoices WHERE id = %s",
        (inv_id,)
    )

    inv = cursor.fetchone()

    amount = data.get('amount') or (
        float(inv['total_amount']) if inv else 0.0
    )

    cursor.execute("""
        INSERT INTO payments (
            invoice_id,
            method,
            amount,
            payment_date,
            note
        )
        VALUES (
            %s, %s, %s, CURRENT_DATE,
            'Logged by internal staff'
        )
    """, (inv_id, method, amount))

    cursor.execute(
        "UPDATE invoices SET status = 'Paid' WHERE id = %s",
        (inv_id,)
    )

    conn.commit()

    cursor.close()
    conn.close()

    return jsonify({
        "message": "Payment recorded successfully and invoice marked as Paid"
    })


# ============================================================
# 8. EXECUTIVE DASHBOARD KPIs
# ============================================================

@app.route('/api/dashboard', methods=['GET'])
def get_dashboard():
    conn = get_db()
    cursor = conn.cursor()

    # Count active subscriptions
    cursor.execute("""
        SELECT COUNT(*) AS cnt
        FROM subscriptions
        WHERE status = 'Active'
    """)

    active_subs = cursor.fetchone()['cnt']

    # Calculate total revenue
    cursor.execute("""
        SELECT COALESCE(SUM(amount), 0) AS rev
        FROM payments
    """)

    total_revenue = float(
        cursor.fetchone()['rev']
    )

    # Count collected payments
    cursor.execute("""
        SELECT COUNT(*) AS cnt
        FROM payments
    """)

    collected_payments = cursor.fetchone()['cnt']

    # Calculate outstanding confirmed invoice balance
    cursor.execute("""
        SELECT COALESCE(SUM(total_amount), 0) AS bal
        FROM invoices
        WHERE status = 'Confirmed'
    """)

    overdue_balance = float(
        cursor.fetchone()['bal']
    )

    cursor.close()
    conn.close()

    return jsonify({
        "active_subscriptions": active_subs,
        "total_revenue": total_revenue,
        "collected_payments_count": collected_payments,
        "overdue_balance": overdue_balance
    })
@app.route('/api/products', methods=['POST'])
def create_product():
    data = request.json or {}

    name = (data.get('name') or '').strip()
    ptype = data.get('type')
    notes = data.get('notes')

    try:
        sales_price = float(data.get('sales_price'))
        cost_price = data.get('cost_price')
        cost_price = float(cost_price) if cost_price not in (None, '') else None
    except (TypeError, ValueError):
        return jsonify({"error": "Prices must be numbers"}), 400

    if not name:
        return jsonify({"error": "Product name is required"}), 400
    if sales_price < 0 or (cost_price is not None and cost_price < 0):
        return jsonify({"error": "Prices cannot be negative"}), 400

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO products (name, type, sales_price, cost_price, notes)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING *
    """, (name, ptype, sales_price, cost_price, notes))
    product = cursor.fetchone()
    conn.commit()

    cursor.close()
    conn.close()

    return jsonify(product), 201

# ============================================================
# RUN FLASK APPLICATION
# ============================================================

if __name__ == '__main__':
    print(
        "🚀 PostgreSQL API Server starting on http://localhost:5000"
    )

    app.run(
        host='0.0.0.0',
        port=5000,
        debug=True
    )

