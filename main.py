"""
Entry point for the competitor promo monitor.

Usage (set by Railway cron schedules):
    python main.py scrape    # daily — promos + product launches + prices
    python main.py report    # weekly (Mondays) — build + post the Slack summary

On first run it auto-creates the schema, so a fresh Postgres works immediately.
"""
import sys
import db
from scraper import (scrape_all, scrape_new_products, scrape_strain_prices,
                     COMPETITORS)
from report import run_report


def do_scrape():
    db.init_schema()
    db.init_intel_schema()

    # 1) Promo pages (now also captures spend tiers + end dates)
    stored, skipped = 0, 0
    for name, row, status, detail in scrape_all():
        if status == "ok" and row:
            db.insert_snapshot(row)
            db.log_health(name, "ok" if not detail else "warning", detail)
            stored += 1
            note = f" ({detail})" if detail else ""
            print(f"[ok] {name}: promo stored{note}")
        else:
            db.log_health(name, status, detail)
            skipped += 1
            print(f"[{status}] {name}: promo — {detail}")

    # 2) New product launches
    new_found = 0
    for name, cfg in COMPETITORS.items():
        names, status, detail = scrape_new_products(name, cfg)
        if status == "ok":
            fresh = 0
            for pn in names:
                if db.upsert_product(name, pn, cfg.get("new_url")):
                    fresh += 1
            new_found += fresh
            db.log_health(name, "ok", f"products: {len(names)} listed, {fresh} new")
            print(f"[ok] {name}: {len(names)} products, {fresh} new")
        elif status != "skip":
            db.log_health(name, status, f"products — {detail}")
            print(f"[{status}] {name}: products — {detail}")

    # 3) Head-to-head strain prices
    price_count = 0
    for name, cfg in COMPETITORS.items():
        obs, status, detail = scrape_strain_prices(name, cfg)
        for o in obs:
            db.insert_price(**o)
            price_count += 1
        if status == "ok":
            print(f"[ok] {name}: {len(obs)} strain prices")
        else:
            db.log_health(name, status, f"prices — {detail}")
            print(f"[{status}] {name}: prices — {detail}")

    print(f"Scrape complete. promos stored={stored} skipped={skipped} "
          f"new_products={new_found} prices={price_count}")


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
