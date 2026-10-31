"""
Creates (or updates) the starting accounts for BrightLoop.

Signup on the website always makes a 'Customer', so the first Admin and
Staff accounts have to be created here, straight in the database.

Run once from the Backend folder, inside your venv:
    python3 seed_users.py

Safe to run again: existing rows are updated, missing rows are created.
These are demo passwords. Change them before anyone else uses the app.
"""

import os
import psycopg2
from werkzeug.security import generate_password_hash

DATABASE_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://postgres:postgres@localhost:5432/brightloop'
)

# (email, username, role, password) -- role must be exactly Admin, Staff or Customer
USERS = [
    ('admin@brightloop.com',  'Admin',   'Admin',    'Admin#1234'),
    ('staff@brightloop.com',  'Staff',   'Staff',    'Staff#1234'),
    ('procurement@shell.com', 'Shell',   'Customer', 'Shell#1234'),
    ('accounts@infosys.com',  'Infosys', 'Customer', 'Infosys#1234'),
]

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

try:
    for email, name, role, password in USERS:
        # pbkdf2 so the hash matches what the login route checks against
        pw_hash = generate_password_hash(password, method='pbkdf2:sha256')

        cur.execute(
            "UPDATE users SET name = %s, password_hash = %s, role = %s WHERE email = %s",
            (name, pw_hash, role, email)
        )

        if cur.rowcount == 0:
            cur.execute(
                "INSERT INTO users (name, email, password_hash, role) "
                "VALUES (%s, %s, %s, %s)",
                (name, email, pw_hash, role)
            )
            print(f"created  {email}  ({role})")
        else:
            print(f"updated  {email}  ({role})")

    conn.commit()
    print("Seed users done.")
except Exception:
    conn.rollback()
    raise
finally:
    cur.close()
    conn.close()