"""
BrightLoop subscription & billing API  (merged version)

Install (use fpdf2, NOT the old "fpdf" package -- they clash):
    pip uninstall -y fpdf
    pip install flask flask-cors psycopg2-binary fpdf2 python-dateutil APScheduler

One-time DB changes needed by the recurring billing engine:
    ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS next_billing_date DATE;
    ALTER TABLE discounts     ADD COLUMN IF NOT EXISTS times_used INTEGER DEFAULT 0;

Environment variables (all optional for local use):
    SECRET_KEY    signing key for login / reset tokens
    DATABASE_URL  postgres connection string
    DEMO_MODE     "1" (default) returns the reset link in the API response,
                  set to "0" in production
    FLASK_DEBUG   "1" (default) or "0"
"""

import io
import re
import os
import datetime

from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from fpdf import FPDF
from dateutil.relativedelta import relativedelta
from apscheduler.schedulers.background import BackgroundScheduler
import psycopg2
import psycopg2.errors
from psycopg2.extras import RealDictCursor


app = Flask(__name__)
CORS(app)

# Secret key used to sign login and password-reset tokens
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-me')
serializer = URLSafeTimedSerializer(app.secret_key)

DATABASE_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://postgres:postgres@localhost:5432/brightloop'
)

# When on, /api/forgot-password returns the reset link so the demo page can
# open it. Turn off (DEMO_MODE=0) anywhere real users exist.
DEMO_MODE = os.environ.get('DEMO_MODE', '1') == '1'

# scrypt (the default) is missing from some Python builds (e.g. macOS 3.9),
# so every password in this project is hashed with pbkdf2.
HASH_METHOD = 'pbkdf2:sha256'

PASSWORD_RULES_MESSAGE = (
    "Password must be more than 8 characters with an uppercase letter, "
    "a lowercase letter and a special character"
)


def get_db():
    conn = psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor
    )
    return conn


def hash_password(password):
    return generate_password_hash(password, method=HASH_METHOD)


def password_is_strong(password):
    return not (
        len(password) <= 8
        or not re.search(r'[A-Z]', password)
        or not re.search(r'[a-z]', password)
        or not re.search(r'[^A-Za-z0-9]', password)
    )


# ============================================================
# 0. SIGNUP & LOGIN
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
    if not password_is_strong(password):
        return jsonify({"error": PASSWORD_RULES_MESSAGE}), 400

    password_hash = hash_password(password)

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


@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}

    # The page may send the field as identifier, username or email
    identifier = (
        data.get('identifier') or data.get('username') or data.get('email') or ''
    ).strip()
    password = data.get('password') or ''

    if not identifier or not password:
        return jsonify({"error": "Enter your email or username and password"}), 400

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, name, email, password_hash, role
        FROM users
        WHERE LOWER(email) = LOWER(%s) OR LOWER(name) = LOWER(%s)
    """, (identifier, identifier))
    user = cursor.fetchone()
    cursor.close()
    conn.close()

    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({"error": "Invalid email/username or password"}), 401

    token = serializer.dumps({"user_id": user['id'], "role": user['role']})

    return jsonify({
        "token": token,
        "user": {
            "id": user['id'],
            "name": user['name'],
            "email": user['email'],
            "role": user['role']
        }
    })


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

    generic = {
        "message": "If an account exists with that email, a reset link has been sent."
    }

    if not user:
        return jsonify(generic), 200

    # Token is valid for 1 hour (checked when it is used)
    token = serializer.dumps(email, salt='password-reset-salt')
    reset_url = f"{request.host_url}reset-password.html?token={token}"

    # In production, send via SMTP. For local testing/demo, print to console
    print(f"[RESET LINK FOR {email}]: {reset_url}")

    if not DEMO_MODE:
        return jsonify(generic), 200

    # Demo only: the link/token are returned so the demo page can open its
    # own reset page. Never do this in production.
    return jsonify({
        "message": "Password reset link sent to registered email",
        "reset_url": reset_url,
        "token": token
    }), 200


@app.route('/api/reset-password', methods=['POST'])
def reset_password():
    data = request.json or {}
    token = data.get('token')
    new_password = data.get('password') or ''

    if not token or not new_password:
        return jsonify({"error": "Token and new password are required"}), 400

    if not password_is_strong(new_password):
        return jsonify({"error": PASSWORD_RULES_MESSAGE}), 400

    try:
        email = serializer.loads(token, salt='password-reset-salt', max_age=3600)
    except SignatureExpired:
        return jsonify({"error": "The reset link has expired"}), 400
    except BadSignature:
        return jsonify({"error": "Invalid reset token"}), 400

    password_hash = hash_password(new_password)

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
# Draft -> Quotation -> Confirmed -> Active -> Closed
# ============================================================

SUBSCRIPTION_FLOW = {
    'Draft': ['Quotation'],
    'Quotation': ['Confirmed'],
    'Confirmed': ['Active'],
    'Active': ['Closed'],
    'Closed': [],
}


@app.route('/api/subscriptions/transition', methods=['POST'])
def transition_subscription():
    data = request.json or {}

    sub_id = data.get('subscription_id')
    new_status = data.get('status')

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT status FROM subscriptions WHERE id = %s", (sub_id,))
    sub = cursor.fetchone()

    if not sub:
        cursor.close()
        conn.close()
        return jsonify({"error": "Subscription not found"}), 404

    if new_status not in SUBSCRIPTION_FLOW.get(sub['status'], []):
        cursor.close()
        conn.close()
        return jsonify({
            "error": f"Cannot move from {sub['status']} to {new_status}"
        }), 400

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
# 5. PRICING ENGINE
# ============================================================

def compute_line_totals(cursor, product_id, tax_id, quantity, unit_price):
    """
    Calculates subtotal, tax, discount and total for ONE subscription line
    using the rules stored in the `taxes` and `discounts` tables.
    """
    subtotal = float(quantity) * float(unit_price)
    today = datetime.date.today()

    # 1. TAX: use the tax rate linked to this line (if any)
    total_tax = 0.0
    if tax_id:
        cursor.execute("SELECT percentage FROM taxes WHERE id = %s", (tax_id,))
        tax_row = cursor.fetchone()
        if tax_row:
            total_tax = subtotal * (float(tax_row['percentage']) / 100.0)

    # 2. DISCOUNTS: rules for this product, or rules that apply to all products
    cursor.execute("""
        SELECT * FROM discounts
        WHERE product_id IS NULL OR product_id = %s
    """, (product_id,))
    discount_rules = cursor.fetchall()

    total_discount = 0.0
    used_rule_ids = []

    for d in discount_rules:
        # Date window
        if d.get('start_date') and d['start_date'] > today:
            continue
        if d.get('end_date') and d['end_date'] < today:
            continue

        # Usage limit
        if d.get('usage_limit') is not None and (d.get('times_used') or 0) >= d['usage_limit']:
            continue

        # Minimum purchase amount and minimum quantity
        if d.get('min_purchase') and subtotal < float(d['min_purchase']):
            continue
        if d.get('min_quantity') and quantity < d['min_quantity']:
            continue

        # Discount amount
        if d['discount_type'] == 'percent':
            disc_amt = subtotal * (float(d['discount_value']) / 100.0)
        else:  # 'fixed'
            disc_amt = float(d['discount_value'])

        total_discount += disc_amt
        used_rule_ids.append(d['id'])

    # A discount can never be larger than the line itself
    total_discount = min(total_discount, subtotal)

    final_total = subtotal - total_discount + total_tax

    return {
        "subtotal": round(subtotal, 2),
        "tax_amount": round(total_tax, 2),
        "discount_amount": round(total_discount, 2),
        "total_amount": round(final_total, 2),
        "used_rule_ids": used_rule_ids
    }


# ============================================================
# 5b. RECURRING BILLING ENGINE
# ============================================================

def calculate_next_billing_date(current_date, billing_period):
    """
    Next billing date for a plan's period.
    relativedelta handles month-end cases (e.g. Jan 31 -> Feb 28).
    """
    if isinstance(current_date, str):
        current_date = datetime.datetime.strptime(current_date, '%Y-%m-%d').date()
    elif isinstance(current_date, datetime.datetime):
        current_date = current_date.date()

    period = (billing_period or 'monthly').lower()

    if period == 'daily':
        return current_date + datetime.timedelta(days=1)
    if period == 'weekly':
        return current_date + datetime.timedelta(weeks=1)
    if period == 'yearly':
        return current_date + relativedelta(years=1)
    return current_date + relativedelta(months=1)  # monthly and fallback


def bill_due_subscriptions(cursor):
    """
    Invoices every Active subscription whose next_billing_date is empty
    (never billed) or today/earlier, then moves next_billing_date forward.
    The caller commits or rolls back. Returns the new invoice numbers.
    """
    today = datetime.date.today()

    # FOR UPDATE SKIP LOCKED: if two runs overlap, each subscription is
    # picked up by only one of them, so it can't be billed twice.
    cursor.execute("""
        SELECT s.*, p.billing_period
        FROM subscriptions s
        LEFT JOIN plans p ON s.plan_id = p.id
        WHERE s.status = 'Active'
          AND (s.next_billing_date IS NULL OR s.next_billing_date <= %s)
        FOR UPDATE OF s SKIP LOCKED
    """, (today,))
    due_subs = cursor.fetchall()

    generated_invoices = []

    for sub in due_subs:
        cursor.execute("""
            SELECT *
            FROM subscription_lines
            WHERE subscription_id = %s
        """, (sub['id'],))
        lines = cursor.fetchall()

        if not lines:
            continue

        bill_date = sub['next_billing_date'] or today

        inv_subtotal = 0.0
        inv_tax = 0.0
        inv_discount = 0.0
        inv_total = 0.0
        invoice_lines_data = []
        rule_ids_to_count = []

        for line in lines:
            calc = compute_line_totals(
                cursor,
                line['product_id'],
                line['tax_id'],
                line['quantity'],
                line['unit_price']
            )

            inv_subtotal += calc['subtotal']
            inv_tax += calc['tax_amount']
            inv_discount += calc['discount_amount']
            inv_total += calc['total_amount']
            rule_ids_to_count.extend(calc['used_rule_ids'])

            invoice_lines_data.append({
                'quantity': line['quantity'],
                'price': calc['subtotal'],
                'tax': calc['tax_amount'],
                'discount': calc['discount_amount']
            })

        # One invoice number per subscription per billing date
        inv_num = f"INV-{sub['subscription_number']}-{bill_date:%Y%m%d}"

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
            round(inv_subtotal, 2),
            round(inv_tax, 2),
            round(inv_discount, 2),
            round(inv_total, 2)
        ))

        inv_id = cursor.fetchone()['id']

        for il in invoice_lines_data:
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
                il['quantity'],
                il['price'],
                il['tax'],
                il['discount']
            ))

        # Count each discount rule once per invoice it was used on
        for rule_id in set(rule_ids_to_count):
            cursor.execute("""
                UPDATE discounts
                SET times_used = COALESCE(times_used, 0) + 1
                WHERE id = %s
            """, (rule_id,))

        # Move the schedule forward so this period is never billed twice
        next_date = calculate_next_billing_date(bill_date, sub['billing_period'])
        cursor.execute(
            "UPDATE subscriptions SET next_billing_date = %s WHERE id = %s",
            (next_date, sub['id'])
        )

        generated_invoices.append(inv_num)

    return generated_invoices


def process_recurring_billing_job():
    """Opens its own connection, bills everything due, commits or rolls back."""
    conn = get_db()
    cursor = conn.cursor()
    try:
        invoices = bill_due_subscriptions(cursor)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    print(f"[{datetime.datetime.now()}] Recurring billing: "
          f"{len(invoices)} subscription(s) billed.")
    return invoices


@app.route('/api/billing/run', methods=['POST'])
def run_billing():
    invoices = process_recurring_billing_job()

    return jsonify({
        "message": (
            f"Billing cycle completed. "
            f"Generated {len(invoices)} invoices."
        ),
        "invoices": invoices
    })


@app.route('/api/admin/trigger-recurring-billing', methods=['POST'])
def trigger_recurring_billing_api():
    """Manual trigger for testing or an admin panel button."""
    invoices = process_recurring_billing_job()

    return jsonify({
        "message": "Recurring billing job completed successfully.",
        "subscriptions_billed": len(invoices),
        "invoices": invoices
    }), 200


# Runs every day at 00:05 server time. Started in the main block below.
scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(
    process_recurring_billing_job,
    'cron',
    hour=0,
    minute=5,
    id='recurring_billing',
    replace_existing=True
)


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
# 6c. INVOICE DETAIL + PDF EXPORT
# ============================================================

class InvoicePDF(FPDF):
    def header(self):
        self.set_font('Helvetica', 'B', 16)
        self.cell(0, 10, 'BRIGHTLOOP SOFTWARE',
                  new_x="LMARGIN", new_y="NEXT", align='L')
        self.set_font('Helvetica', '', 10)
        self.cell(0, 5, 'Subscription & Billing Invoice',
                  new_x="LMARGIN", new_y="NEXT", align='L')
        self.ln(10)

    def footer(self):
        self.set_y(-15)
        self.set_font('Helvetica', 'I', 8)
        self.cell(0, 10, f'Page {self.page_no()}', align='C')


def _pdf_safe(value):
    """Helvetica only supports Latin-1, so replace anything else with '?'."""
    return str(value if value is not None else '-').encode(
        'latin-1', 'replace'
    ).decode('latin-1')


def _num(value):
    """NULL-safe number for PDF maths (DB values may be Decimal or None)."""
    return float(value or 0)


def _load_invoice(invoice_id):
    """Return (invoice, lines), or (None, None) if missing or not allowed."""
    role = request.headers.get('X-User-Role', 'Customer')
    user_id = request.headers.get('X-User-Id')
    is_staff = role in ['Admin', 'Staff']

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT
                i.*,
                u.email AS customer_email,
                u.name AS customer_name,
                s.subscription_number
            FROM invoices i
            JOIN users u ON i.customer_id = u.id
            LEFT JOIN subscriptions s ON i.subscription_id = s.id
            WHERE i.id = %s
        """, (invoice_id,))
        inv = cursor.fetchone()

        if not inv:
            return None, None
        # Customers can only open their own invoices
        if not is_staff and str(inv['customer_id']) != str(user_id):
            return None, None

        cursor.execute(
            "SELECT * FROM invoice_lines WHERE invoice_id = %s ORDER BY id",
            (invoice_id,)
        )
        lines = [dict(l) for l in cursor.fetchall()]
        return dict(inv), lines
    finally:
        cursor.close()
        conn.close()


@app.route('/api/invoices/<int:invoice_id>', methods=['GET'])
def get_invoice_detail(invoice_id):
    inv, lines = _load_invoice(invoice_id)
    if inv is None:
        return jsonify({"error": "Invoice not found"}), 404

    inv['lines'] = lines
    return jsonify(inv), 200


@app.route('/api/invoices/<int:invoice_id>/pdf', methods=['GET'])
def invoice_pdf(invoice_id):
    inv, lines = _load_invoice(invoice_id)
    if inv is None:
        return jsonify({"error": "Invoice not found"}), 404

    customer = inv['customer_name'] or inv['customer_email']

    pdf = InvoicePDF(format='A4')
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # ---- Invoice details ----
    pdf.set_font('Helvetica', 'B', 13)
    pdf.cell(0, 8, _pdf_safe(f"Invoice {inv['invoice_number']}"),
             new_x="LMARGIN", new_y="NEXT")

    pdf.set_font('Helvetica', '', 10)
    details = [
        f"Customer: {customer} ({inv['customer_email']})",
        f"Subscription: {inv['subscription_number'] or '-'}",
        f"Status: {inv['status']}",
        f"Due date: {inv['due_date']}",
    ]
    for line in details:
        pdf.cell(0, 6, _pdf_safe(line), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)

    # ---- Line items table (widths add up to 190 mm) ----
    widths = [15, 25, 35, 35, 35, 45]
    headers = ['#', 'Qty', 'Price', 'Tax', 'Discount', 'Line total']

    pdf.set_font('Helvetica', 'B', 10)
    pdf.set_fill_color(230, 230, 230)
    for w, h in zip(widths, headers):
        pdf.cell(w, 8, h, border=1, align='C', fill=True,
                 new_x="RIGHT", new_y="TOP")
    pdf.ln(8)

    pdf.set_font('Helvetica', '', 10)
    for n, l in enumerate(lines, start=1):
        price = _num(l['price'])
        tax = _num(l['tax_amount'])
        discount = _num(l['discount_amount'])
        line_total = price + tax - discount
        cells = [
            str(n),
            str(l['quantity']),
            f"{price:.2f}",
            f"{tax:.2f}",
            f"{discount:.2f}",
            f"{line_total:.2f}",
        ]
        for w, text in zip(widths, cells):
            pdf.cell(w, 8, text, border=1, align='R',
                     new_x="RIGHT", new_y="TOP")
        pdf.ln(8)

    pdf.ln(6)

    # ---- Totals ----
    totals = [
        ('Subtotal', f"{_num(inv['subtotal']):.2f}"),
        ('Tax', f"{_num(inv['tax_total']):.2f}"),
        ('Discount', f"-{_num(inv['discount_total']):.2f}"),
    ]
    pdf.set_font('Helvetica', '', 11)
    for label, value in totals:
        pdf.cell(130, 7, '', new_x="RIGHT", new_y="TOP")
        pdf.cell(30, 7, label, align='L', new_x="RIGHT", new_y="TOP")
        pdf.cell(30, 7, value, align='R', new_x="LMARGIN", new_y="NEXT")

    pdf.set_font('Helvetica', 'B', 12)
    pdf.cell(130, 9, '', new_x="RIGHT", new_y="TOP")
    pdf.cell(30, 9, 'Total', border='T', align='L',
             new_x="RIGHT", new_y="TOP")
    pdf.cell(30, 9, f"{_num(inv['total_amount']):.2f}", border='T', align='R',
             new_x="LMARGIN", new_y="NEXT")

    buffer = io.BytesIO(bytes(pdf.output()))

    return send_file(
        buffer,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f"{inv['invoice_number']}.pdf"
    )


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
        "SELECT total_amount, status FROM invoices WHERE id = %s",
        (inv_id,)
    )

    inv = cursor.fetchone()

    if not inv:
        cursor.close()
        conn.close()
        return jsonify({"error": "Invoice not found"}), 404

    if inv['status'] == 'Paid':
        cursor.close()
        conn.close()
        return jsonify({"error": "Invoice is already paid"}), 400

    try:
        amount = float(data.get('amount') or inv['total_amount'])
    except (TypeError, ValueError):
        cursor.close()
        conn.close()
        return jsonify({"error": "Amount must be a number"}), 400

    if amount <= 0:
        cursor.close()
        conn.close()
        return jsonify({"error": "Amount must be greater than zero"}), 400

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
    debug = os.environ.get('FLASK_DEBUG', '1') == '1'

    # With debug=True Flask starts two processes (a watcher and the real
    # server); start the scheduler only in the real one, or jobs run twice.
    # With debug=False there is a single process, so start it directly.
    if not debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        scheduler.start()

    print(
        "🚀 PostgreSQL API Server starting on http://localhost:5000"
    )

    app.run(
        host='0.0.0.0',
        port=5000,
        debug=debug
    )