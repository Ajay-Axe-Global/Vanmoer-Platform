import sqlite3

db_path = "database/app.db"

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# See all tables
cursor.execute("""
    SELECT name
    FROM sqlite_master
    WHERE type='table'
""")

tables = cursor.fetchall()

for table in tables:
    print(table[0])

conn.close()