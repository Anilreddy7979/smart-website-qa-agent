import psycopg


connection = psycopg.connect(
    host="localhost",
    port=5432,
    dbname="smart_qa",
    user="postgres",
    password="tiger"
)

cursor = connection.cursor()

cursor.execute(
    """
    INSERT INTO scans
    (website_url, status_code, page_title)
    VALUES (%s, %s, %s)
    """,
    (
        "https://example.com",
        200,
        "Example Domain"
    )
)

connection.commit()

print("✅ Scan inserted into PostgreSQL!")

cursor.close()
connection.close()

print("✅ Database connection closed!")