"""
Import historical Barney's Farm promo windows from a CSV into barneys_promos.
These render as labeled bands on the dashboard chart across the full timeline.

CSV columns (header row required, case-insensitive):
    start_date  -> YYYY-MM-DD   (required)
    end_date    -> YYYY-MM-DD   (optional; defaults to start_date)
    promo_name  -> text         (required)
    discount    -> text         (optional, e.g. "30% off", "BOGO")
    notes       -> text         (optional)

Re-importing the same (start_date, promo_name) updates that row, so it's safe
to re-run with corrections or additions.

Usage:
    python3 import_barneys_promos.py barneys_historical_promos.csv
"""
import sys
import csv
from datetime import datetime

import db


def import_csv(path):
    db.init_promo_schema()
    n, skipped = 0, 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [h.strip().lower() for h in reader.fieldnames]
        for row in reader:
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items()}
            sd = row.get("start_date", "")
            name = row.get("promo_name", "")
            if not sd or not name:
                skipped += 1
                continue
            try:
                start = datetime.strptime(sd, "%Y-%m-%d").date()
            except ValueError:
                print(f"  ! bad start_date skipped: {sd}")
                skipped += 1
                continue
            ed = row.get("end_date", "")
            try:
                end = datetime.strptime(ed, "%Y-%m-%d").date() if ed else start
            except ValueError:
                end = start
            db.upsert_barneys_promo(
                start_date=start, end_date=end, promo_name=name,
                discount=row.get("discount") or None,
                notes=row.get("notes") or None,
            )
            n += 1
    print(f"Imported/updated {n} promos (skipped {skipped}).")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 import_barneys_promos.py <csv>")
        sys.exit(1)
    import_csv(sys.argv[1])
