# BrightLoop: Unified Subscription Management System

Replace spreadsheet-based subscription tracking with one web platform for **products, plans, quotations, subscriptions, rule-driven billing, automated recurring invoices, payments, PDF invoices, customer and admin portals, and an executive dashboard.**

Built for the Odoo Hackathon at NHCE.
Repository: https://github.com/NotManasR/Odoo_BrightLoop_NHCE

---

## Highlights

- **Full billing lifecycle:** quote, confirm, activate, invoice, pay, report.
- **Automated recurring billing:** a daily scheduler (00:05 server time) invoices every Active subscription that is due, then advances its `next_billing_date` by the plan's period (daily, weekly, monthly, yearly). Month-end dates are handled (Jan 31 becomes Feb 28).
- **Rule-driven pricing:** taxes and discounts come from database tables (date windows, usage limits, minimum amount and quantity), not hardcoded rates.
- **Safe billing runs:** one shared billing engine for the scheduler and the manual triggers, row locking to prevent double billing, and a single transaction that rolls back on failure.
- **Invoice PDF export and print:** download any invoice as a PDF or print it from the browser; customers can only open their own.
- **Secure accounts:** hashed passwords, server-side validation, unique email and username, signed password-reset tokens that expire after 1 hour.
- **Role-aware portals:** separate Admin and Customer pages; customers see only their own subscriptions and invoices.
- **Admin catalog:** manage products, variants, plans, taxes and discount rules.
- **KPI dashboard:** active subscriptions, revenue, payment count, outstanding balance.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, Flask, Flask-CORS |
| Database | PostgreSQL (`psycopg2`) |
| Scheduling | APScheduler, python-dateutil |
| PDF | fpdf2 |
| Security | Werkzeug password hashing, `itsdangerous` signed tokens |
| Frontend | HTML5, CSS3, vanilla JavaScript (Fetch API, `localStorage`) |

---

## Project Structure

```
.
├── Backend/
│   ├── init_db.py                  # Creates 11 tables and seeds starter data
│   ├── seed_pass.py                # Seed helper script
│   └── server.py                   # Flask REST API, billing engine, scheduler, PDF export
├── FrontEnd/                       # Sign-in, sign-up and password-reset pages
├── frontpage.html                  # Landing page
├── Customer.html                   # Customer portal
├── BrightLoop – Admin (1).html     # Admin console
├── .gitignore
└── README.md
```

**Architecture:** Browser (HTML/JS) -> Flask REST API (`server.py`) -> PostgreSQL. The frontend never touches the database directly.

---

## Database

Eleven related tables: `users`, `products`, `product_variants`, `plans`, `taxes`, `discounts`, `subscriptions`, `subscription_lines`, `invoices`, `invoice_lines`, `payments`.

**Relationship chain:** `users -> subscriptions -> invoices -> payments`, with `subscription_lines` under subscriptions and `invoice_lines` under invoices.

- `DECIMAL(10,2)` for all money values (no floating-point rounding errors).
- `CHECK` constraints enforce allowed roles, statuses, billing periods and discount types.
- Foreign keys with `CASCADE` / `SET NULL` rules keep data consistent.
- `subscriptions.next_billing_date` drives recurring billing.
- `init_db.py` is idempotent: safe to re-run (`IF NOT EXISTS`, `ON CONFLICT DO NOTHING`), and it re-syncs ID sequences after seeding.

---

## Workflow

```
Subscription:  Draft -> Quotation -> Confirmed -> Active -> Closed
Invoice:       Draft -> Confirmed -> Paid
```

1. **Quote:** a subscription is created with status `Quotation` and one line item.
2. **Confirm and activate:** the status moves to `Confirmed`, then `Active`.
3. **Bill:** the billing engine turns due Active subscriptions into invoices.
4. **Pay:** a payment is recorded and the invoice becomes `Paid`.
5. **Report:** the dashboard recalculates from the tables on every request.

---

## Billing Engine

One function, `bill_due_subscriptions`, powers the daily scheduler, `POST /api/billing/run`, and `POST /api/admin/trigger-recurring-billing`.

**Which subscriptions are billed:** status `Active`, and `next_billing_date` is empty (never billed) or today or earlier.

**For each line:**

1. **Subtotal** = quantity x unit price
2. **Tax** = the rate linked to the line
3. **Discounts** = every matching rule (per product or global), skipped if outside its dates, over its usage limit, or below its minimum amount or quantity; capped at the subtotal
4. **Total** = subtotal - discount + tax

**After invoicing,** `next_billing_date` moves forward by the plan's period, so a period is never billed twice. Invoice numbers look like `INV-SUB-1761890000-20261031`.

**Example:** 10 x 1000 = 10,000, tax 10% = +1,000, discount 10% = -1,000, total **10,000**.

---

## API Overview

Base URL: `http://localhost:5000`

| Area | Endpoints |
|---|---|
| Accounts | `POST /api/signup`, `POST /api/forgot-password`, `POST /api/reset-password` |
| Catalog | `GET /api/products`, `POST /api/products` |
| Subscriptions | `GET /api/subscriptions`, `POST /api/subscriptions/quote`, `POST /api/subscriptions/transition` |
| Billing | `POST /api/billing/run`, `POST /api/admin/trigger-recurring-billing` |
| Invoices | `GET /api/invoices`, `GET /api/invoices/<id>`, `GET /api/invoices/<id>/pdf`, `POST /api/invoices/pay` |
| Dashboard | `GET /api/dashboard` |
| Admin | `GET, POST /api/admin/products`, `/api/admin/plans`, `/api/admin/taxes`, `/api/admin/discounts`; `POST /api/admin/product-variants` |

**Roles:** subscription and invoice endpoints read the `X-User-Role` and `X-User-Id` headers. Admin and Staff get all rows; customers get only their own.

---

## Getting Started

**Prerequisites:** Python 3.12, PostgreSQL

```bash
git clone https://github.com/NotManasR/Odoo_BrightLoop_NHCE.git
cd Odoo_BrightLoop_NHCE/Backend
pip install flask flask-cors psycopg2-binary fpdf2 apscheduler python-dateutil

psql -U postgres -c "CREATE DATABASE brightloop;"

python init_db.py       # tables and seed data
python seed_pass.py     # seed helper
python server.py        # API on http://localhost:5000
```

Then open `frontpage.html` in a browser, or serve the project folder with `python -m http.server 5500` and visit `http://localhost:5500/frontpage.html`.

**Upgrading an existing database** (if it was created before recurring billing or usernames):

```sql
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS next_billing_date DATE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS name VARCHAR(100);
ALTER TABLE users ADD CONSTRAINT users_name_unique UNIQUE (name);
```

**Optional environment variables**

| Variable | Default |
|---|---|
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/brightloop` |
| `SECRET_KEY` | `dev-secret-change-me` |

**Seed data:** 2 internal users (Admin, Staff), 2 customers, 1 product with a variant, 1 monthly plan, 10% tax, and a 10% discount for quantity of 5 or more.

---

## Quick Demo

```bash
# 1. Create a quotation
curl -X POST localhost:5000/api/subscriptions/quote -H "Content-Type: application/json" -d '{"quantity":10}'

# 2. Confirm, then activate (use the returned subscription_id)
curl -X POST localhost:5000/api/subscriptions/transition -H "Content-Type: application/json" -d '{"subscription_id":1,"status":"Confirmed"}'
curl -X POST localhost:5000/api/subscriptions/transition -H "Content-Type: application/json" -d '{"subscription_id":1,"status":"Active"}'

# 3. Run billing
curl -X POST localhost:5000/api/billing/run

# 4. Download the invoice PDF
curl -H "X-User-Role: Staff" localhost:5000/api/invoices/1/pdf -o invoice1.pdf

# 5. Pay the invoice
curl -X POST localhost:5000/api/invoices/pay -H "Content-Type: application/json" -d '{"invoice_id":1}'

# 6. Check the dashboard
curl localhost:5000/api/dashboard
```

Use your real `subscription_id` and `invoice_id` if they differ from 1. Running `billing/run` a second time creates nothing, because the next billing date is a month away.

---

## Security

**Implemented:** hashed and salted passwords, parameterized SQL (no SQL injection), server-side validation, new accounts always created as `Customer`, expiring signed reset tokens, per-customer access check on invoice PDFs.

**Before production:**

- Replace header-based roles with verified tokens (JWT or sessions).
- Email reset links instead of returning them in the API response.
- Set a strong `SECRET_KEY` and turn off `debug=True`.
- Restrict CORS to the real frontend origin.
- Run the scheduler as a real job (cron or Celery) instead of inside Flask.

---

## Roadmap

- [ ] Token-verified roles and permission checks on admin, billing and payment routes
- [ ] Validated subscription status transitions
- [ ] Quotes that read price and tax from the catalog
- [ ] Partial payments and overdue detection
- [ ] Product names on invoice lines
- [ ] Edit and delete for catalog and rules
- [ ] Automated tests
