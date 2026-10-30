from api import get_connection
conn = get_connection()
cur = conn.cursor()

print("=== issues columns ===")
cur.execute("""
    SELECT column_name, data_type
    FROM information_schema.columns
    WHERE table_name = 'issues'
    ORDER BY ordinal_position
""")
for r in cur.fetchall():
    print(r)

print("\n=== scans columns ===")
cur.execute("""
    SELECT column_name, data_type
    FROM information_schema.columns
    WHERE table_name = 'scans'
    ORDER BY ordinal_position
""")
for r in cur.fetchall():
    print(r)

conn.close()
print("\nDone.")

