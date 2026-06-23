"""
Stamp promo labels onto the 'note' column of daily_sales for specific date ranges.
Re-running is safe — it only updates the note for dates that already exist.

Run from the repo folder (where db.py lives), with DATABASE_URL set:
    python3 label_promos.py

Edit the PROMOS list below to add/change labels. Format:
    ("YYYY-MM-DD", "YYYY-MM-DD", "label")   # start, end (inclusive), note text
"""
import db

PROMOS = [
    ("2026-06-11", "2026-06-15", "40% off — 40th anniversary"),
    ("2026-06-16", "2026-06-30", "30% storewide + double free seeds"),
]


def run():
    db.init_sales_schema()
    total = 0
    with db.get_conn() as conn, conn.cursor() as cur:
        for start, end, label in PROMOS:
            cur.execute("""
                UPDATE daily_sales
                SET note = %s
                WHERE sale_date BETWEEN %s AND %s
            """, (label, start, end))
            print(f"  {start} → {end}: labeled '{label}' ({cur.rowcount} days)")
            total += cur.rowcount
        conn.commit()
    print(f"Done. Labeled {total} days.")


if __name__ == "__main__":
    run()
