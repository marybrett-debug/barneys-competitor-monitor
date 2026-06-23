"""
Import daily sales from a CSV into the daily_sales table.

Usage:
    python import_sales.py sales.csv

Expected CSV columns (header row required, case-insensitive):
    date      -> YYYY-MM-DD   (required)
    revenue   -> number       (optional)
    orders    -> integer      (optional)
    units     -> integer      (optional)
    note      -> text         (optional)

Re-uploading a date overwrites that day's row, so it's safe to re-run with
corrected numbers or an extended file.
"""
import sys
import csv
from datetime import datetime

import db


def _num(v):
    v = (v or "").strip().replace(",", "").replace("$", "")
    return float(v) if v else None


def _int(v):
    v = (v or "").strip().replace(",", "")
    return int(float(v)) if v else None


def import_csv(path: str):
    db.init_sales_schema()
    imported, skipped = 0, 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # normalise header keys to lowercase
        reader.fieldnames = [h.strip().lower() for h in reader.fieldnames]
        for row in reader:
            row = {k.strip().lower(): v for k, v in row.items()}
            raw_date = (row.get("date") or "").strip()
            if not raw_date:
                skipped += 1
                continue
            try:
                sale_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                print(f"  ! skipping bad date: {raw_date}")
                skipped += 1
                continue
            db.upsert_sales(
                sale_date=sale_date,
                revenue=_num(row.get("revenue")),
                orders=_int(row.get("orders")),
                units=_int(row.get("units")),
                note=(row.get("note") or "").strip() or None,
            )
            imported += 1
    print(f"Sales import complete. imported/updated={imported} skipped={skipped}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python import_sales.py <path-to-csv>")
        sys.exit(1)
    import_csv(sys.argv[1])
