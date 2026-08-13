"""
Mock ERP database for the Reconciliation Agent portfolio project.

This models a fictional beverage wholesale distributor. All company names,
product names, SKUs, and figures below are invented for demo purposes only.

Deliberately includes ambiguous product entries (two keg sizes, two cider
variants with overlapping names) so the agent's clarification logic has
something real to be tested against — not just a clean happy-path demo.
"""
import sqlite3

DB_PATH = "mock_erp.db"


def init_mock_erp():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("DROP TABLE IF EXISTS inventory")
    cursor.execute("DROP TABLE IF EXISTS orders")

    cursor.execute("""
        CREATE TABLE inventory (
            sku TEXT PRIMARY KEY,
            product_name TEXT NOT NULL,
            available_stock INTEGER NOT NULL CHECK(available_stock >= 0)
        )
    """)

    cursor.execute("""
        CREATE TABLE orders (
            order_id TEXT PRIMARY KEY,
            customer_name TEXT NOT NULL,
            sku TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            delivery_status TEXT NOT NULL CHECK(delivery_status IN ('Pending', 'Dispatched', 'Delivered')),
            FOREIGN KEY(sku) REFERENCES inventory(sku)
        )
    """)

    # --- Inventory ---
    # Note the deliberate ambiguity: two keg sizes for the lager, and two
    # cider variants. A customer just saying "kegs" or "the cider" is
    # genuinely ambiguous — this is the test case for the clarification path.
    cursor.executemany(
        "INSERT INTO inventory VALUES (?, ?, ?)",
        [
            ("HGL-LAG-50L", "Highgate Lager 50L Keg", 25),
            ("HGL-LAG-30L", "Highgate Lager 30L Keg", 40),
            ("ORC-CID-ORIG", "Orchard Press Cider Original 24x330ml", 12),
            ("ORC-CID-LIME", "Orchard Press Cider Lime 24x330ml", 18),
            ("VLE-WNE-PGR", "Vale Wine Co. Pinot Grigio Case", 40),
        ],
    )

    # --- Orders ---
    cursor.executemany(
        "INSERT INTO orders VALUES (?, ?, ?, ?, ?)",
        [
            ("ORD-9941", "The Crown Pub", "ORC-CID-ORIG", 10, "Pending"),
            ("ORD-8822", "The Rusty Anchor", "HGL-LAG-50L", 5, "Dispatched"),
            ("ORD-7710", "The Feathers", "VLE-WNE-PGR", 20, "Pending"),
        ],
    )

    conn.commit()
    conn.close()
    print(f"Mock ERP initialised at {DB_PATH}")
    print("Ambiguous cases seeded: lager (2 keg sizes), cider (2 variants)")


if __name__ == "__main__":
    init_mock_erp()
