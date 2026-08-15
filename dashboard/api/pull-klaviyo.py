"""
Vercel serverless endpoint: POST /api/pull-klaviyo
Pulls US Klaviyo campaign performance from the Supermetrics API and writes it
into the klaviyo_campaigns table. Triggered by the "Pull Klaviyo (US)" button.

Requires two environment variables on Vercel:
  DATABASE_URL          - the Railway Postgres connection string (already set)
  SUPERMETRICS_API_KEY  - a Supermetrics API key (Hub -> avatar -> API keys)

The Supermetrics query mirrors what we validated interactively:
  ds_id       = KLAV   (Klaviyo)
  ds_accounts = XBhvea (Barney's Souvenirs BV)
  fields      = campaign send date, name, subject, recipients, open/click/bounce rates
US filtering is done here (campaign name contains 'US' or 'USA'), because the
Klaviyo campaign naming convention tags region in the campaign name.

Returns JSON: {"ok": true, "imported": N, "range": "start..end"} or {"error": ...}
"""
import os
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, date, timedelta
from http.server import BaseHTTPRequestHandler

import psycopg2

SUPERMETRICS_URL = "https://api.supermetrics.com/enterprise/v2/query/data/json"
DS_ID = "KLAV"
DS_ACCOUNTS = "XBhvea"
FIELDS = ("campaign_send_date,campaign_name,campaign_subject,"
          "klaviyo_total_recipients,klaviyo_open_rate,klaviyo_click_rate,"
          "klaviyo_bounce_rate")


def _get_conn():
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


def _query_supermetrics(api_key, start_date, end_date):
    """Call the Supermetrics data API and return a list of row lists.
    Uses the synchronous JSON query endpoint with a Bearer token."""
    body = {
        "ds_id": DS_ID,
        "ds_accounts": DS_ACCOUNTS,
        "start_date": start_date,
        "end_date": end_date,
        "fields": FIELDS,
        "settings": {"no_headers": False},
        "max_rows": 5000,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(SUPERMETRICS_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", "Bearer " + api_key)
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    # response shape: {"data": [ [header...], [row...], ... ]} under meta/data
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


import re as _re

def _normalize_promo_base(name, subj):
    """Reduce a campaign name to a stable 'base' that groups a launch email with
    its follow-ups/reminders. Strips numbered prefix, [Follow-up], END/Last Chance,
    region suffix, and issue tags."""
    s = name or ""
    s = _re.sub(r"\[follow-?up\]", "", s, flags=_re.I)
    s = _re.sub(r"^\s*\d+\.\s*", "", s)              # leading "9. "
    s = _re.sub(r"\bEND\b", "", s, flags=_re.I)
    s = _re.sub(r"last chance", "", s, flags=_re.I)
    s = _re.sub(r"\(promo issue\)", "", s, flags=_re.I)
    s = _re.sub(r"\bderry\b", "", s, flags=_re.I)
    # strip region suffix: - US / - USA / - ROW etc at the end
    s = _re.sub(r"[-–]\s*(usa?|row|de|uk\/?ir|fr|it|es|nl|at)\s*$", "", s, flags=_re.I)
    s = _re.sub(r"\bMJB\b", "", s)
    s = _re.sub(r"\s+", " ", s).strip(" -–|")
    return s.strip()


def _classify_promo(base, subjects):
    """Return (is_major, discount) inferred from the campaign name + subjects."""
    blob = (base + " " + " ".join(subjects)).upper()
    discount = None
    m = _re.search(r"(\d{1,2})\s*%\s*OFF", blob)
    if m:
        discount = m.group(1) + "% off"
    elif "BOGO" in blob or "BUY 1 GET 1" in blob or "BUY ONE GET ONE" in blob:
        discount = "BOGO"
    elif "FREE SEEDS" in blob:
        discount = "Free seeds"
    # major if it's a sale/discount/anniversary; minor if strain-of-week / new release
    major_kw = ("% OFF", "SALE", "BOGO", "ANNIVERSARY", "BLACK FRIDAY", "CYBER",
                "GREEN WEDNESDAY", "40%", "30%", "25%", "20%", "50%")
    minor_kw = ("SOW", "STRAIN OF THE WEEK", "SOM", "NEW RELEASE", "NEW RELEASES")
    is_major = any(k in blob for k in major_kw) and not any(k in blob for k in minor_kw)
    return is_major, discount


def _derive_and_insert_promos(cur, conn, collected):
    """Group US campaigns into promo windows and INSERT only promos that don't
    already exist in barneys_promos. Never updates/overwrites existing rows, so
    hand-curated history is protected. Returns count of promos added."""
    if not collected:
        return 0
    from datetime import timedelta
    # group by normalized base; within a group, split runs >21 days apart
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

    # find existing (start_date, promo_name) pairs to avoid overwrite
    cur.execute("SELECT start_date, promo_name FROM barneys_promos")
    existing_rows = cur.fetchall()
    existing = set((str(r[0]), (r[1] or "").strip().lower()) for r in existing_rows)
    # protect curated history: only auto-insert promos that start AFTER the latest
    # curated promo already on file (so we fill the forward gap, never duplicate
    # or overwrite hand-curated Jan-June work). If the table is empty, allow all.
    curated_cutoff = None
    if existing_rows:
        try:
            curated_cutoff = max(str(r[0]) for r in existing_rows)
        except Exception:
            curated_cutoff = None

    added = 0
    for base, cluster in windows:
        dates = [c[0] for c in cluster]
        subjects = [c[1] for c in cluster]
        start = min(dates)
        # skip anything at/before the curated cutoff — protects existing history
        if curated_cutoff and start.isoformat() <= curated_cutoff:
            continue
        end = max(dates) + timedelta(days=3)   # tail for the offer after last email
        is_major, discount = _classify_promo(base, subjects)
        promo_name = base[:120]
        key = (start.isoformat(), promo_name.strip().lower())
        if key in existing:
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


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        # status check: confirms the endpoint deployed and the key is present
        has_key = bool(os.environ.get("SUPERMETRICS_API_KEY"))
        self._send(200, {"ok": True, "endpoint": "pull-klaviyo",
                         "api_key_configured": has_key,
                         "message": "POST here to pull US Klaviyo campaigns."})

    def do_POST(self):
        try:
            api_key = os.environ.get("SUPERMETRICS_API_KEY")
            if not api_key:
                return self._send(400, {"error": "SUPERMETRICS_API_KEY is not set on the server. Add it in Vercel > Settings > Environment Variables."})

            # default: last 2 years through today
            end = date.today()
            start = end - timedelta(days=730)
            rows = _query_supermetrics(api_key, start.isoformat(), end.isoformat())

            # first row is the header; map columns by position
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

            conn = _get_conn()
            cur = conn.cursor()
            n = 0
            seen_dates = []
            collected = []   # (date, campaign_name, subject) for promo derivation
            for r in rows[1:]:
                name = str(r[i_name]) if i_name is not None and i_name < len(r) else ""
                # US-only: campaign name tags region
                if not ("US" in name.upper()):
                    continue
                sd = str(r[i_date])[:10] if i_date is not None else ""
                try:
                    send = datetime.strptime(sd, "%Y-%m-%d").date()
                except ValueError:
                    continue
                subj = str(r[i_subj]) if i_subj is not None and i_subj < len(r) else ""
                if not subj:
                    continue
                # rates arrive as decimals (0.51) -> store as percent (51.0)
                orate = _num(r[i_open]) if i_open is not None and i_open < len(r) else None
                crate = _num(r[i_click]) if i_click is not None and i_click < len(r) else None
                if orate is not None:
                    orate = round(orate * 100, 2)
                if crate is not None:
                    crate = round(crate * 100, 2)
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
                seen_dates.append(send)
                collected.append((send, name, subj))
            conn.commit()

            # ---- derive promo windows from campaigns, insert only NEW ones ----
            promos_added = _derive_and_insert_promos(cur, conn, collected)

            # ---- record this update as 'manual' (button click) ----
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS data_updates (
                      kind TEXT PRIMARY KEY, updated_at TIMESTAMPTZ NOT NULL,
                      source TEXT, detail TEXT)
                """)
                cur.execute("""
                    INSERT INTO data_updates (kind, updated_at, source, detail)
                    VALUES ('klaviyo', now(), 'manual', %s)
                    ON CONFLICT (kind) DO UPDATE SET
                      updated_at=now(), source='manual', detail=EXCLUDED.detail
                """, (f"{n} US campaigns, {promos_added} promos derived",))
                conn.commit()
            except Exception:
                conn.rollback()

            cur.close(); conn.close()

            if not seen_dates:
                return self._send(200, {"ok": True, "imported": 0,
                                        "message": "No US campaigns found in range."})
            seen_dates.sort()
            return self._send(200, {"ok": True, "imported": n,
                                    "promos_added": promos_added,
                                    "range": f"{seen_dates[0]} .. {seen_dates[-1]}"})
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:200]
            except Exception:
                pass
            return self._send(502, {"error": f"Supermetrics API error {e.code}: {detail}"})
        except Exception as e:
            return self._send(500, {"error": f"Klaviyo pull failed: {e}"})
