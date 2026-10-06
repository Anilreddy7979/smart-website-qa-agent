import psycopg


connection = psycopg.connect(
    host="localhost",
    port=5432,
    dbname="smart_qa",
    user="postgres",
    password="tiger"
)

print("✅ Connected to PostgreSQL successfully!")

connection.close()

print("✅ Connection closed.")