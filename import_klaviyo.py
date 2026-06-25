"""
Import Klaviyo email-campaign engagement + subjects into klaviyo_campaigns.
These show on the dashboard tied to promo windows (engagement vs sales lift).

CSV columns (from the prepared klaviyo_campaigns.csv):
    send_date, subject, campaign_name, open_rate, click_rate, recipients, unsubscribes

Re-importing the same (send_date, subject) updates it. Safe to re-run.

Usage:
    python3 import_klaviyo.py klaviyo_campaigns.csv
"""
import sys
import csv
from datetime import datetime

import db


def _num(s):
    s = (s or "").replace(",", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _int(s):
    v = _num(s)
    return int(v) if v is not None else None


def import_csv(path):
    db.init_klaviyo_schema()
    n, skipped = 0, 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [h.strip().lower() for h in reader.fieldnames]
        for row in reader:
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items()}
            sd = row.get("send_date", "")
            subj = row.get("subject", "")
            if not sd or not subj:
                skipped += 1
                continue
            try:
                send = datetime.strptime(sd[:10], "%Y-%m-%d").date()
            except ValueError:
                skipped += 1
                continue
            db.upsert_campaign(
                send_date=send, subject=subj,
                campaign_name=row.get("campaign_name") or None,
                open_rate=_num(row.get("open_rate")),
                click_rate=_num(row.get("click_rate")),
                recipients=_int(row.get("recipients")),
                unsubscribes=_int(row.get("unsubscribes")),
            )
            n += 1
    print(f"Imported/updated {n} campaigns (skipped {skipped}).")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 import_klaviyo.py <csv>")
        sys.exit(1)
    import_csv(sys.argv[1])
