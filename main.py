"""
Entry point for the competitor promo monitor.

Usage (set by Railway cron schedules):
    python main.py scrape           # daily — promos + product launches + prices
    python main.py report           # weekly (Mondays) — Slack summary
    python main.py special_offers   # weekly (Wednesdays) — our US special-offers page

On first run it auto-creates the schema, so a fresh Postgres works immediately.
"""
import sys
from datetime import datetime, timezone
import db
from scraper import (scrape_all, scrape_new_products, scrape_strain_prices,
                     scrape_special_offers, COMPETITORS)
from report import run_report


def do_special_offers():
    """Scrape barneysfarm.com/us/special-offer-seeds and store each strain's
    offer + price. Meant to run weekly on Wednesdays."""
    db.init_special_offers_schema()
    offers, status, detail = scrape_special_offers()
    if status == "ok":
        for o in offers:
            db.insert_special_offer(**o)
        disc = sum(1 for o in offers if o.get("is_discounted"))
        db.log_health("Barney's Farm (special offers)", "ok",
                      f"{len(offers)} offers, {disc} discounted")
        print(f"[ok] special offers: {len(offers)} strains "
              f"({disc} discounted) stored")
    else:
        db.log_health("Barney's Farm (special offers)", status, detail)
        print(f"[{status}] special offers — {detail}")


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

    # 4) On Wednesdays, also capture our own special-offers page
    if datetime.now(timezone.utc).weekday() == 2:  # Monday=0 ... Wednesday=2
        print("Wednesday — capturing special offers page…")
        do_special_offers()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "scrape"
    if mode == "scrape":
        do_scrape()
    elif mode == "report":
        db.init_schema()
        run_report()
    elif mode == "special_offers":
        do_special_offers()
    else:
        print(f"Unknown mode: {mode}. Use 'scrape', 'report', or 'special_offers'.")
        sys.exit(1)


if __name__ == "__main__":
    main()
