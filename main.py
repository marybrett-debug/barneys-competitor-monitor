"""
Entry point for the competitor promo monitor.

Usage (set by Railway cron schedules):
    python main.py scrape    # daily — fetch + store snapshots
    python main.py report    # weekly (Mondays) — build + email the summary

On first run it auto-creates the schema, so a fresh Postgres works immediately.
"""
import sys
import db
from scraper import scrape_all
from report import run_report


def do_scrape():
    db.init_schema()
    results = scrape_all()
    stored, skipped = 0, 0
    for name, row, status, detail in results:
        if status == "ok" and row:
            db.insert_snapshot(row)
            db.log_health(name, "ok", "")
            stored += 1
            print(f"[ok] {name}: stored")
        else:
            db.log_health(name, status, detail)
            skipped += 1
            print(f"[{status}] {name}: {detail}")
    print(f"Scrape complete. stored={stored} skipped={skipped}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "scrape"
    if mode == "scrape":
        do_scrape()
    elif mode == "report":
        db.init_schema()
        run_report()
    else:
        print(f"Unknown mode: {mode}. Use 'scrape' or 'report'.")
        sys.exit(1)


if __name__ == "__main__":
    main()
