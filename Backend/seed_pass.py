import os
import psycopg2
from werkzeug.security import generate_password_hash

conn = psycopg2.connect(os.environ['DATABASE_URL'])
cur = conn.cursor()

users = [
    ('admin@brightloop.com', 'Admin', 'Admin#1234'),
    ('staff@brightloop.com', 'Staff', 'Staff#1234'),
    ('procurement@shell.com', 'Shell', 'Shell#1234'),
    ('accounts@infosys.com', 'Infosys', 'Infosys#1234'),
]

for email, name, pw in users:
    cur.execute(
        "UPDATE users SET name = %s, password_hash = %s WHERE email = %s",
        (name, generate_password_hash(pw, method='pbkdf2:sha256'), email)
    )

conn.commit()
cur.close()
conn.close()
print("Seed users updated")