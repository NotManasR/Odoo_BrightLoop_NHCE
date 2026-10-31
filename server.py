
from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import datetime

app = Flask(__name__)

# Enables cross-origin requests for your frontend teammate
CORS(app)


def get_db():
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    return conn


# ============================================================
# 1. GET PRODUCTS & VARIANTS
# ============================================================

@app.route('/api/products', methods=['GET'])
def get_products():
    conn = get_db()

    products = conn.execute(
        "SELECT * FROM products"
    ).fetchall()

    result = []

    for p in products:
        p_dict = dict(p)

        variants = conn.execute(
            "SELECT * FROM product_variants WHERE product_id = ?",
            (p['id'],)
        ).fetchall()

        p_dict['variants'] = [
            dict(v) for v in variants
        ]

        result.append(p_dict)

    conn.close()

    return jsonify(result)


# ============================================================
# 2. GET SUBSCRIPTIONS & LINE ITEMS
# ============================================================

@app.route('/api/subscriptions', methods=['GET'])
def get_subscriptions():
    conn = get_db()

    subs = conn.execute("""
        SELECT
            s.*,
            u.email AS customer_email,
            p.name AS plan_name
        FROM subscriptions s
        JOIN users u ON s.customer_id = u.id
        LEFT JOIN plans p ON s.plan_id = p.id
    """).fetchall()

    result = []

    for s in subs:
        s_dict = dict(s)

        lines = conn.execute("""
            SELECT
                sl.*,
                pr.name AS product_name,
                t.percentage AS tax_rate
            FROM subscription_lines sl
            JOIN products pr ON sl.product_id = pr.id
            LEFT JOIN taxes t ON sl.tax_id = t.id
            WHERE sl.subscription_id = ?
        """, (s['id'],)).fetchall()

        s_dict['lines'] = [
            dict(l) for l in lines
        ]

        result.append(s_dict)

    conn.close()

    return jsonify(result)


# ============================================================
# 3. CREATE QUOTATION (Sales Rep Action)
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
            ?, ?, ?, 'Quotation',
            DATE('now'),
            DATE('now', '+30 days'),
            'Net 30'
        )
    """, (sub_num, customer_id, plan_id))

    sub_id = cursor.lastrowid

    cursor.execute("""
        INSERT INTO subscription_lines (
            subscription_id,
            product_id,
            tax_id,
            quantity,
            unit_price
        )
        VALUES (?, 1, 1, ?, 1000.00)
    """, (sub_id, quantity))

    conn.commit()
    conn.close()

    return jsonify({
        "message": "Quotation created",
        "subscription_id": sub_id,
        "subscription_number": sub_num
    }), 201


# ============================================================
# 4. STATE MACHINE TRANSITION
# Draft -> Quotation -> Confirmed -> Active -> Closed
# ============================================================

@app.route('/api/subscriptions/transition', methods=['POST'])
def transition_subscription():
    data = request.json or {}

    sub_id = data.get('subscription_id')
    new_status = data.get('status')

    conn = get_db()

    conn.execute(
        "UPDATE subscriptions SET status = ? WHERE id = ?",
        (new_status, sub_id)
    )

    conn.commit()
    conn.close()

    return jsonify({
        "message": f"Subscription updated to {new_status}"
    })


# ============================================================
# 5. RUN BILLING CYCLE
# Auto-generates invoices with taxes and discounts.
# Prevents double billing.
# ============================================================

@app.route('/api/billing/run', methods=['POST'])
def run_billing():
    conn = get_db()
    cursor = conn.cursor()

    # Select active subscriptions that have NOT been billed yet
    active_subs = cursor.execute("""
        SELECT s.*
        FROM subscriptions s
        WHERE s.status = 'Active'
        AND s.id NOT IN (
            SELECT subscription_id
            FROM invoices
            WHERE subscription_id IS NOT NULL
        )
    """).fetchall()

    generated_invoices = []

    for sub in active_subs:

        lines = cursor.execute(
            "SELECT * FROM subscription_lines WHERE subscription_id = ?",
            (sub['id'],)
        ).fetchall()

        subtotal = sum(
            l['quantity'] * l['unit_price']
            for l in lines
        )

        total_qty = sum(
            l['quantity']
            for l in lines
        )

        # Calculate 10% Tax
        tax_total = subtotal * 0.10

        # Apply 10% discount if quantity >= 5 seats
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
                ?, ?, ?, 'Confirmed',
                DATE('now', '+15 days'),
                ?, ?, ?, ?
            )
        """, (
            inv_num,
            sub['id'],
            sub['customer_id'],
            subtotal,
            tax_total,
            discount_total,
            total_amount
        ))

        inv_id = cursor.lastrowid

        for l in lines:

            line_price = l['quantity'] * l['unit_price']

            cursor.execute("""
                INSERT INTO invoice_lines (
                    invoice_id,
                    quantity,
                    price,
                    tax_amount,
                    discount_amount
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                inv_id,
                l['quantity'],
                line_price,
                line_price * 0.10,
                (line_price * 0.10) if total_qty >= 5 else 0
            ))

        generated_invoices.append(inv_num)

    conn.commit()
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

    invoices = conn.execute("""
        SELECT
            i.*,
            u.email AS customer_email
        FROM invoices i
        JOIN users u ON i.customer_id = u.id
    """).fetchall()

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

    inv = cursor.execute(
        "SELECT total_amount FROM invoices WHERE id = ?",
        (inv_id,)
    ).fetchone()

    amount = data.get('amount') or (
        inv['total_amount'] if inv else 0.0
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
            ?, ?, ?, DATE('now'),
            'Logged by internal staff'
        )
    """, (inv_id, method, amount))

    cursor.execute(
        "UPDATE invoices SET status = 'Paid' WHERE id = ?",
        (inv_id,)
    )

    conn.commit()
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

    # Count active subscriptions
    active_subs = conn.execute("""
        SELECT COUNT(*) AS cnt
        FROM subscriptions
        WHERE status = 'Active'
    """).fetchone()['cnt']

    # Calculate total revenue
    total_revenue = conn.execute("""
        SELECT COALESCE(SUM(amount), 0) AS rev
        FROM payments
    """).fetchone()['rev']

    # Count collected payments
    collected_payments = conn.execute("""
        SELECT COUNT(*) AS cnt
        FROM payments
    """).fetchone()['cnt']

    # Calculate outstanding confirmed invoice balance
    overdue_balance = conn.execute("""
        SELECT COALESCE(SUM(total_amount), 0) AS bal
        FROM invoices
        WHERE status = 'Confirmed'
    """).fetchone()['bal']

    conn.close()

    return jsonify({
        "active_subscriptions": active_subs,
        "total_revenue": total_revenue,
        "collected_payments_count": collected_payments,
        "overdue_balance": overdue_balance
    })


# ============================================================
# RUN FLASK APPLICATION
# ============================================================

if __name__ == '__main__':
    print("🚀 API Server starting on http://localhost:5000")

    app.run(
        host='0.0.0.0',
        port=5000,
        debug=True
    )
