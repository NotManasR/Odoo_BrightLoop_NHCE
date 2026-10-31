import re
from werkzeug.security import generate_password_hash
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor
import datetime
import os


app = Flask(__name__)
CORS(app)

# Secret key used to sign password-reset tokens
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-me')
serializer = URLSafeTimedSerializer(app.secret_key)


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


# ============================================================
# 0. SIGNUP
# ============================================================

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

    password_hash = generate_password_hash(password)

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
    except psycopg2.errors.UniqueViolation as e:
        conn.rollback()
        if e.diag.constraint_name == 'users_name_unique':
            return jsonify({"error": "This username is already taken"}), 409
        return jsonify({"error": "An account with this email already exists"}), 409
    finally:
        cursor.close()
        conn.close()

    return jsonify(user), 201


# ============================================================
# PASSWORD RESET
# ============================================================

@app.route('/api/forgot-password', methods=['POST'])
def forgot_password():
    data = request.json or {}
    email = (data.get('email') or '').strip().lower()

    if not email:
        return jsonify({"error": "Email is required"}), 400

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, email FROM users WHERE email = %s", (email,))
    user = cursor.fetchone()
    cursor.close()
    conn.close()

    if not user:
        return jsonify({
            "message": "If an account exists with that email, a reset link has been sent."
        }), 200

    # Token is valid for 1 hour (checked when it is used)
    token = serializer.dumps(email, salt='password-reset-salt')
    reset_url = f"{request.host_url}reset_password.html?token={token}"

    # In production, send via SMTP. For local testing/demo, print to console
    print(f"[RESET LINK FOR {email}]: {reset_url}")

    return jsonify({
        "message": "Password reset link sent to registered email",
        "reset_url": reset_url
    }), 200


@app.route('/api/reset-password', methods=['POST'])
def reset_password():
    data = request.json or {}
    token = data.get('token')
    new_password = data.get('password') or ''

    if not token or not new_password:
        return jsonify({"error": "Token and new password are required"}), 400

    if (len(new_password) <= 8
            or not re.search(r'[A-Z]', new_password)
            or not re.search(r'[a-z]', new_password)
            or not re.search(r'[^A-Za-z0-9]', new_password)):
        return jsonify({"error": "Password must be more than 8 characters with an uppercase letter, a lowercase letter and a special character"}), 400

    try:
        email = serializer.loads(token, salt='password-reset-salt', max_age=3600)
    except SignatureExpired:
        return jsonify({"error": "The reset link has expired"}), 400
    except BadSignature:
        return jsonify({"error": "Invalid reset token"}), 400

    password_hash = generate_password_hash(new_password)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET password_hash = %s WHERE email = %s RETURNING id",
        (password_hash, email)
    )
    updated_user = cursor.fetchone()
    conn.commit()
    cursor.close()
    conn.close()

    if not updated_user:
        return jsonify({"error": "User not found"}), 404

    return jsonify({"message": "Password updated successfully"}), 200


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
# 2. GET SUBSCRIPTIONS & LINE ITEMS (role-aware)
# ============================================================

@app.route('/api/subscriptions', methods=['GET'])
def get_subscriptions():
    role = request.headers.get('X-User-Role', 'Customer')
    user_id = request.headers.get('X-User-Id')

    is_staff = role in ['Admin', 'Staff']

    # A customer without a valid numeric ID sees nothing
    if not is_staff and not (user_id and user_id.isdigit()):
        return jsonify([]), 200

    conn = get_db()
    cursor = conn.cursor()

    base_query = """
        SELECT
            s.*,
            u.email AS customer_email,
            p.name AS plan_name
        FROM subscriptions s
        JOIN users u ON s.customer_id = u.id
        LEFT JOIN plans p ON s.plan_id = p.id
    """

    if is_staff:
        # Admins & Staff see all subscriptions
        cursor.execute(base_query + " ORDER BY s.id DESC")
    else:
        # Customers ONLY see subscriptions assigned to their customer ID
        cursor.execute(
            base_query + " WHERE s.customer_id = %s ORDER BY s.id DESC",
            (int(user_id),)
        )

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

    return jsonify(result), 200


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
# 6. GET INVOICES LIST (role-aware)
# ============================================================

@app.route('/api/invoices', methods=['GET'])
def get_invoices():
    role = request.headers.get('X-User-Role', 'Customer')
    user_id = request.headers.get('X-User-Id')

    is_staff = role in ['Admin', 'Staff']

    # A customer without a valid numeric ID sees nothing
    if not is_staff and not (user_id and user_id.isdigit()):
        return jsonify([]), 200

    conn = get_db()
    cursor = conn.cursor()

    base_query = """
        SELECT
            i.*,
            u.email AS customer_email
        FROM invoices i
        JOIN users u ON i.customer_id = u.id
    """

    if is_staff:
        cursor.execute(base_query + " ORDER BY i.id DESC")
    else:
        cursor.execute(
            base_query + " WHERE i.customer_id = %s ORDER BY i.id DESC",
            (int(user_id),)
        )

    invoices = [dict(row) for row in cursor.fetchall()]

    cursor.close()
    conn.close()

    return jsonify(invoices), 200


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


# ============================================================
# 9. CREATE PRODUCT
# ============================================================

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
# 10. ADMIN CATALOG & RULE MANAGEMENT
# ============================================================

def _none_if_empty(value):
    """Turn empty strings into None so DATE/INT columns accept them."""
    return None if value in ('', None) else value


def _pick(data, *keys, default=None):
    """Return the first key present in the request body (supports aliases)."""
    for key in keys:
        if key in data:
            return data[key]
    return default


def _admin_list(table):
    # `table` is always a fixed name from this file, never user input
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(f"SELECT * FROM {table} ORDER BY id DESC")
    rows = [dict(row) for row in cursor.fetchall()]
    cursor.close()
    conn.close()
    return jsonify(rows), 200


def _admin_insert(query, params, label):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(query, params)
        new_id = cursor.fetchone()['id']
        conn.commit()
    except (psycopg2.errors.CheckViolation,
            psycopg2.errors.NotNullViolation,
            psycopg2.errors.ForeignKeyViolation,
            psycopg2.DataError):
        conn.rollback()
        return jsonify({"error": f"Invalid data for {label.lower()}"}), 400
    finally:
        cursor.close()
        conn.close()
    return jsonify({"message": f"{label} created", "id": new_id}), 201


@app.route('/api/admin/products', methods=['GET', 'POST'])
def manage_products():
    if request.method == 'GET':
        return _admin_list('products')

    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({"error": "Product name is required"}), 400

    return _admin_insert("""
        INSERT INTO products (name, type, sales_price, cost_price, notes)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
    """, (
        name,
        data.get('type', 'Service'),
        data.get('sales_price', 0),
        _none_if_empty(data.get('cost_price', 0)),
        data.get('notes', '')
    ), "Product")


@app.route('/api/admin/product-variants', methods=['POST'])
def create_product_variant():
    data = request.json or {}
    variant_name = (data.get('variant_name') or '').strip()

    if not data.get('product_id') or not variant_name:
        return jsonify({"error": "product_id and variant_name are required"}), 400

    return _admin_insert("""
        INSERT INTO product_variants (product_id, variant_name, extra_price)
        VALUES (%s, %s, %s)
        RETURNING id
    """, (
        data.get('product_id'),
        variant_name,
        data.get('extra_price', 0)
    ), "Variant")


@app.route('/api/admin/plans', methods=['GET', 'POST'])
def manage_plans():
    if request.method == 'GET':
        return _admin_list('plans')

    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({"error": "Plan name is required"}), 400

    # Database only allows: Daily, Weekly, Monthly, Yearly
    billing_period = str(data.get('billing_period', 'Monthly')).strip().capitalize()
    if billing_period not in ('Daily', 'Weekly', 'Monthly', 'Yearly'):
        return jsonify({
            "error": "billing_period must be Daily, Weekly, Monthly or Yearly"
        }), 400

    return _admin_insert("""
        INSERT INTO plans (
            name, price, billing_period, min_quantity,
            available_start_date, available_end_date,
            is_closable, is_pausable, is_renewable
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        name,
        data.get('price', 0),
        billing_period,
        data.get('min_quantity', 1),
        _none_if_empty(_pick(data, 'available_start_date', 'start_date')),
        _none_if_empty(_pick(data, 'available_end_date', 'end_date')),
        _pick(data, 'is_closable', 'closable_flag', default=True),
        _pick(data, 'is_pausable', 'pausable_flag', default=True),
        _pick(data, 'is_renewable', 'renewable_flag', default=True)
    ), "Plan")


@app.route('/api/admin/taxes', methods=['GET', 'POST'])
def manage_taxes():
    if request.method == 'GET':
        return _admin_list('taxes')

    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({"error": "Tax name is required"}), 400

    return _admin_insert("""
        INSERT INTO taxes (name, percentage)
        VALUES (%s, %s)
        RETURNING id
    """, (name, data.get('percentage', 0)), "Tax rule")


@app.route('/api/admin/discounts', methods=['GET', 'POST'])
def manage_discounts():
    if request.method == 'GET':
        return _admin_list('discounts')

    data = request.json or {}

    # Database only allows: fixed, percent ("percentage" is accepted as an alias)
    discount_type = str(data.get('discount_type', 'percent')).strip().lower()
    if discount_type == 'percentage':
        discount_type = 'percent'
    if discount_type not in ('fixed', 'percent'):
        return jsonify({"error": "discount_type must be 'fixed' or 'percent'"}), 400

    # Note: the discounts table has no `name` column, so any name is ignored
    return _admin_insert("""
        INSERT INTO discounts (
            product_id, discount_type, discount_value, min_purchase,
            min_quantity, start_date, end_date, usage_limit
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        _none_if_empty(data.get('product_id')),
        discount_type,
        _pick(data, 'discount_value', 'value', default=0),
        _pick(data, 'min_purchase', 'min_amount', default=0),
        data.get('min_quantity', 0),
        _none_if_empty(data.get('start_date')),
        _none_if_empty(data.get('end_date')),
        _none_if_empty(data.get('usage_limit'))
    ), "Discount rule")


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
