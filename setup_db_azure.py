"""
Azure SQL Database setup for the Reconciliation Agent portfolio project.

Same schema and seed data as the local SQLite version (setup_db.py), but
targets Azure SQL Database via pyodbc instead of a local .db file.

Reads connection details from environment variables -- never hardcode
credentials here:
  AZURE_SQL_SERVER    e.g. "reconciliation-agent-rg.database.windows.net"
  AZURE_SQL_DATABASE  e.g. "free-sql-db-7679410"
  AZURE_SQL_USERNAME
  AZURE_SQL_PASSWORD

This models a fictional beverage wholesale distributor. All company
names, product names, SKUs, and figures below are invented for demo
purposes only.
"""
import os
import pyodbc

SERVER = os.environ["AZURE_SQL_SERVER"]
DATABASE = os.environ["AZURE_SQL_DATABASE"]
USERNAME = os.environ["AZURE_SQL_USERNAME"]
PASSWORD = os.environ["AZURE_SQL_PASSWORD"]

# See agent_engine_azure.py for why this suffix is required under the
# Linux ODBC driver.
SERVER_SHORT_NAME = SERVER.split(".")[0]
UID = f"{USERNAME}@{SERVER_SHORT_NAME}"

CONNECTION_STRING = (
    "DRIVER={ODBC Driver 18 for SQL Server};"
    f"SERVER=tcp:{SERVER},1433;"
    f"DATABASE={DATABASE};"
    f"UID={UID};"
    f"PWD={PASSWORD};"
    "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
)


def get_connection():
    return pyodbc.connect(CONNECTION_STRING)


def init_mock_erp():
    conn = get_connection()
    cursor = conn.cursor()

    # Drop tables if they exist (T-SQL syntax differs from SQLite's
    # "DROP TABLE IF EXISTS")
    cursor.execute("""
        IF OBJECT_ID('orders', 'U') IS NOT NULL DROP TABLE orders;
    """)
    cursor.execute("""
        IF OBJECT_ID('inventory', 'U') IS NOT NULL DROP TABLE inventory;
    """)
    conn.commit()

    cursor.execute("""
        CREATE TABLE inventory (
            sku NVARCHAR(50) PRIMARY KEY,
            product_name NVARCHAR(200) NOT NULL,
            available_stock INT NOT NULL CHECK (available_stock >= 0)
        )
    """)

    cursor.execute("""
        CREATE TABLE orders (
            order_id NVARCHAR(50) PRIMARY KEY,
            customer_name NVARCHAR(200) NOT NULL,
            sku NVARCHAR(50) NOT NULL,
            quantity INT NOT NULL,
            delivery_status NVARCHAR(20) NOT NULL
                CHECK (delivery_status IN ('Pending', 'Dispatched', 'Delivered')),
            FOREIGN KEY (sku) REFERENCES inventory(sku)
        )
    """)
    conn.commit()

    # --- Inventory ---
    # Same deliberate ambiguity as the local version: two keg sizes, two
    # cider variants, to test the clarification path.
    inventory_rows = [
        ("HGL-LAG-50L", "Highgate Lager 50L Keg", 25),
        ("HGL-LAG-30L", "Highgate Lager 30L Keg", 40),
        ("ORC-CID-ORIG", "Orchard Press Cider Original 24x330ml", 12),
        ("ORC-CID-LIME", "Orchard Press Cider Lime 24x330ml", 18),
        ("VLE-WNE-PGR", "Vale Wine Co. Pinot Grigio Case", 40),
    ]
    cursor.executemany(
        "INSERT INTO inventory (sku, product_name, available_stock) VALUES (?, ?, ?)",
        inventory_rows,
    )

    # --- Orders ---
    order_rows = [
        ("ORD-9941", "The Crown Pub", "ORC-CID-ORIG", 10, "Pending"),
        ("ORD-8822", "The Rusty Anchor", "HGL-LAG-50L", 5, "Dispatched"),
        ("ORD-7710", "The Feathers", "VLE-WNE-PGR", 20, "Pending"),
    ]
    cursor.executemany(
        "INSERT INTO orders (order_id, customer_name, sku, quantity, delivery_status) VALUES (?, ?, ?, ?, ?)",
        order_rows,
    )
    conn.commit()
    conn.close()

    print(f"Azure SQL database '{DATABASE}' initialised on server '{SERVER}'")
    print("Ambiguous cases seeded: lager (2 keg sizes), cider (2 variants)")


if __name__ == "__main__":
    init_mock_erp()