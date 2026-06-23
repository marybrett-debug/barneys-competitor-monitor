"""
Convert a Bagisto 'Retail Orders / Sales Per Day' export into the CSV format
that import_sales.py expects.

Bagisto export format:
  - first line is a `sep=;` directive (skipped)
  - semicolon-separated
  - columns: Date (DD-MM-YYYY); Sales (order count); Net Total; Sub Total; Shipping

Output format (import_sales.py):
  date (YYYY-MM-DD), revenue, orders, units, note
  - revenue  <- Net Total
  - orders   <- Sales
  - units    <- left blank (not in this export)

Usage:
  python3 convert_bagisto_sales.py <bagisto_export.csv> <output.csv>
"""
import sys
import csv
from datetime import datetime


def convert(in_path, out_path):
    with open(in_path, encoding="utf-8-sig", newline="") as f:
        lines = f.read().splitlines()

    # Drop a leading "sep=;" directive line if present
    if lines and lines[0].lower().startswith("sep="):
        lines = lines[1:]

    reader = csv.DictReader(lines, delimiter=";")
    reader.fieldnames = [h.strip() for h in reader.fieldnames]

    rows_out = []
    skipped = 0
    for row in reader:
        row = {k.strip(): (v or "").strip() for k, v in row.items()}
        raw_date = row.get("Date", "")
        if not raw_date:
            skipped += 1
            continue
        try:
            d = datetime.strptime(raw_date, "%d-%m-%Y").date()
        except ValueError:
            print(f"  ! bad date skipped: {raw_date}")
            skipped += 1
            continue
        rows_out.append({
            "date": d.isoformat(),
            "revenue": row.get("Net Total", ""),
            "orders": row.get("Sales", ""),
            "units": "",
            "note": "",
        })

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "revenue", "orders", "units", "note"])
        w.writeheader()
        w.writerows(rows_out)

    print(f"Converted {len(rows_out)} rows -> {out_path} (skipped {skipped})")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 convert_bagisto_sales.py <bagisto_export.csv> <output.csv>")
        sys.exit(1)
    convert(sys.argv[1], sys.argv[2])
