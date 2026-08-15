"""
Shared Klaviyo pull + promo-derivation logic.

Used by BOTH:
  - the Vercel endpoint dashboard/api/pull-klaviyo.py (manual button)
  - the Railway cron entry `python main.py pull_klaviyo` (weekly automation)

Keeping the logic here means the button and the cron never drift apart.

Also records each pull in the `data_updates` table (source = 'manual' or 'cron')
so the dashboard can show "last updated" and how.
"""
import os
import re
import json
import urllib.request
import urllib.error
from datetime import datetime, date, timedelta

import psycopg2

SUPERMETRICS_URL = "https://api.supermetrics.com/enterprise/v2/query/data/json"
DS_ID = "KLAV"
DS_ACCOUNTS = "XBhvea"
FIELDS = ("campaign_send_date,campaign_name,campaign_subject,"
          "klaviyo_total_recipients,klaviyo_open_rate,klaviyo_click_rate,"
          "klaviyo_bounce_rate")


def get_conn():
    url = os.environ["DATABASE_URL"]
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return psycopg2.connect(url)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _pct(v):
    """Convert a rate to a percentage in [0, 100], safely.
    Supermetrics usually returns decimals (0.51 -> 51%). If a value already looks
    like a percentage (>1), assume it's already a percent. Clamp to 100 so a bad
    value can never overflow the NUMERIC(6,2) column (max 9999.99)."""
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v <= 1.0:
        v = v * 100.0          # decimal -> percent
    # if it's already >1 we treat it as an existing percentage
    if v < 0:
        v = 0.0
    if v > 100:
        v = 100.0              # clamp; nothing over 100% and never overflows
    return round(v, 2)


def ensure_updates_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS data_updates (
          kind        TEXT PRIMARY KEY,   -- e.g. 'klaviyo', 'sales'
          updated_at  TIMESTAMPTZ NOT NULL,
          source      TEXT,               -- 'manual' | 'cron' | 'import'
          detail      TEXT
        )
    """)


def record_update(cur, kind, source, detail=""):
    ensure_updates_table(cur)
    cur.execute("""
        INSERT INTO data_updates (kind, updated_at, source, detail)
        VALUES (%s, now(), %s, %s)
        ON CONFLICT (kind) DO UPDATE SET
          updated_at = now(), source = EXCLUDED.source, detail = EXCLUDED.detail
    """, (kind, source, detail))


def query_supermetrics(api_key, start_date, end_date):
    body = {
        "ds_id": DS_ID, "ds_accounts": DS_ACCOUNTS,
        "start_date": start_date, "end_date": end_date,
        "fields": FIELDS, "settings": {"no_headers": False}, "max_rows": 5000,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(SUPERMETRICS_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", "Bearer " + api_key)
    with urllib.request.urlopen(req, timeout=300) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    rows = None
    if isinstance(payload, dict):
        d = payload.get("data")
        if isinstance(d, list):
            rows = d
        elif isinstance(d, dict) and isinstance(d.get("data"), list):
            rows = d["data"]
    if not rows:
        raise RuntimeError("Unexpected Supermetrics response shape")
    return rows


def _normalize_promo_base(name, subj):
    s = name or ""
    s = re.sub(r"\[follow-?up\]", "", s, flags=re.I)
    s = re.sub(r"^\s*\d+\.\s*", "", s)
    s = re.sub(r"\bEND\b", "", s, flags=re.I)
    s = re.sub(r"last chance", "", s, flags=re.I)
    s = re.sub(r"\(promo issue\)", "", s, flags=re.I)
    s = re.sub(r"\bderry\b", "", s, flags=re.I)
    s = re.sub(r"[-–]\s*(usa?|row|de|uk\/?ir|fr|it|es|nl|at)\s*$", "", s, flags=re.I)
    s = re.sub(r"\bMJB\b", "", s)
    s = re.sub(r"\s+", " ", s).strip(" -–|")
    return s.strip()


def _classify_promo(base, subjects):
    blob = (base + " " + " ".join(subjects)).upper()
    discount = None
    m = re.search(r"(\d{1,2})\s*%\s*OFF", blob)
    if m:
        discount = m.group(1) + "% off"
    elif "BOGO" in blob or "BUY 1 GET 1" in blob or "BUY ONE GET ONE" in blob:
        discount = "BOGO"
    elif "FREE SEEDS" in blob:
        discount = "Free seeds"
    major_kw = ("% OFF", "SALE", "BOGO", "ANNIVERSARY", "BLACK FRIDAY", "CYBER",
                "GREEN WEDNESDAY", "40%", "30%", "25%", "20%", "50%")
    minor_kw = ("SOW", "STRAIN OF THE WEEK", "SOM", "NEW RELEASE", "NEW RELEASES")
    is_major = any(k in blob for k in major_kw) and not any(k in blob for k in minor_kw)
    return is_major, discount


def derive_and_insert_promos(cur, conn, collected):
    """Group US campaigns into promo windows; INSERT only promos starting AFTER the
    latest curated promo already on file (never overwrites hand-curated history)."""
    if not collected:
        return 0
    groups = {}
    for send, name, subj in collected:
        base = _normalize_promo_base(name, subj)
        if not base:
            continue
        groups.setdefault(base, []).append((send, subj))

    windows = []
    for base, items in groups.items():
        items.sort()
        cluster = [items[0]]
        for it in items[1:]:
            if (it[0] - cluster[-1][0]).days > 21:
                windows.append((base, cluster)); cluster = [it]
            else:
                cluster.append(it)
        windows.append((base, cluster))

    cur.execute("SELECT start_date, promo_name FROM barneys_promos")
    existing_rows = cur.fetchall()
    existing = set((str(r[0]), (r[1] or "").strip().lower()) for r in existing_rows)
    curated_cutoff = max((str(r[0]) for r in existing_rows), default=None)

    added = 0
    for base, cluster in windows:
        dates = [c[0] for c in cluster]
        subjects = [c[1] for c in cluster]
        start = min(dates)
        if curated_cutoff and start.isoformat() <= curated_cutoff:
            continue
        end = max(dates) + timedelta(days=3)
        is_major, discount = _classify_promo(base, subjects)
        promo_name = base[:120]
        if (start.isoformat(), promo_name.strip().lower()) in existing:
            continue
        notes = "Auto-derived from Klaviyo campaigns: " + "; ".join(s[:60] for s in subjects[:3])
        try:
            cur.execute("""
                INSERT INTO barneys_promos (start_date, end_date, promo_name, discount, notes, is_major)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (start_date, promo_name) DO NOTHING
            """, (start.isoformat(), end.isoformat(), promo_name, discount, notes, is_major))
            if cur.rowcount > 0:
                added += 1
        except Exception:
            conn.rollback()
            continue
    conn.commit()
    return added


def run_pull(source="manual", days=730):
    """Pull US Klaviyo campaigns, upsert them, derive promos, record the update.
    Returns a dict summary. `source` is 'manual' or 'cron'."""
    api_key = os.environ.get("SUPERMETRICS_API_KEY")
    if not api_key:
        raise RuntimeError("SUPERMETRICS_API_KEY is not set")

    end = date.today()
    start = end - timedelta(days=days)
    rows = query_supermetrics(api_key, start.isoformat(), end.isoformat())

    header = [str(c).strip().lower() for c in rows[0]]
    def idx(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None
    i_date = idx("campaign send date")
    i_name = idx("campaign name")
    i_subj = idx("subject")
    i_rec = idx("email recipients")
    i_open = idx("email open rate")
    i_click = idx("email click rate")

    conn = get_conn()
    cur = conn.cursor()
    n = 0
    seen = []
    collected = []
    for r in rows[1:]:
        name = str(r[i_name]) if i_name is not None and i_name < len(r) else ""
        if "US" not in name.upper():
            continue
        sd = str(r[i_date])[:10] if i_date is not None else ""
        try:
            send = datetime.strptime(sd, "%Y-%m-%d").date()
        except ValueError:
            continue
        subj = str(r[i_subj]) if i_subj is not None and i_subj < len(r) else ""
        if not subj:
            continue
        orate = _num(r[i_open]) if i_open is not None and i_open < len(r) else None
        crate = _num(r[i_click]) if i_click is not None and i_click < len(r) else None
        orate = _pct(orate)
        crate = _pct(crate)
        rec = _int(r[i_rec]) if i_rec is not None and i_rec < len(r) else None
        cur.execute("""
            INSERT INTO klaviyo_campaigns
              (send_date, subject, campaign_name, open_rate, click_rate, recipients, unsubscribes)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (send_date, subject) DO UPDATE SET
              campaign_name=EXCLUDED.campaign_name, open_rate=EXCLUDED.open_rate,
              click_rate=EXCLUDED.click_rate, recipients=EXCLUDED.recipients
        """, (send.isoformat(), subj, name, orate, crate, rec, None))
        n += 1
        seen.append(send)
        collected.append((send, name, subj))
    conn.commit()

    promos_added = derive_and_insert_promos(cur, conn, collected)

    detail = f"{n} US campaigns, {promos_added} promos derived"
    record_update(cur, "klaviyo", source, detail)
    conn.commit()
    cur.close(); conn.close()

    return {"imported": n, "promos_added": promos_added,
            "range": (f"{min(seen)} .. {max(seen)}" if seen else None)}
