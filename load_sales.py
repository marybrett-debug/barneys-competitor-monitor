"""
One-shot loader: reads the Bagisto 'Sales Per Day' export directly and loads it
into the daily_sales table. No conversion step, no arguments needed.

It looks for the Bagisto CSV in your Downloads folder automatically. If your file
has a different name, edit the SRC line below to point at it.

Run from the repo folder (where db.py lives), with DATABASE_URL set:
    python3 load_sales.py
"""
import csv
import glob
import os
from datetime import datetime
import db

# Auto-find the Bagisto export in Downloads (any file starting with "Retail").
candidates = sorted(
    glob.glob(os.path.expanduser("~/Downloads/Retail*Sales*Per*Day*.csv"))
    + glob.glob(os.path.expanduser("~/Downloads/Retail*.csv")),
    key=os.path.getmtime, reverse=True)

SRC = candidates[0] if candidates else None

if not SRC:
    raise SystemExit(
        "Could not find the Bagisto export in ~/Downloads.\n"
        "Edit this file and set SRC to the full path of your CSV.")

print("Reading:", SRC)

lines = open(SRC, encoding="utf-8-sig").read().splitlines()
if lines and lines[0].lower().startswith("sep="):
    lines = lines[1:]

reader = csv.DictReader(lines, delimiter=";")
reader.fieldnames = [h.strip() for h in reader.fieldnames]

db.init_sales_schema()

n = 0
for row in reader:
    row = {k.strip(): (v or "").strip() for k, v in row.items()}
    d = row.get("Date", "")
    if not d:
        continue
    try:
        dt = datetime.strptime(d, "%d-%m-%Y").date()
    except ValueError:
        print("  skip bad date:", d)
        continue
    rev = row.get("Net Total") or None
    orders = row.get("Sales") or None
    db.upsert_sales(
        sale_date=dt,
        revenue=float(rev) if rev else None,
        orders=int(float(orders)) if orders else None,
        units=None,
        note=None,
    )
    n += 1

print(f"Imported {n} days into daily_sales.")
