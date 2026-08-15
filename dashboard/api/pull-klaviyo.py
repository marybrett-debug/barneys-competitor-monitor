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
        "settings": {"report_type": "MetricExportCampaign"},
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
            conn.commit()
            cur.close(); conn.close()

            if not seen_dates:
                return self._send(200, {"ok": True, "imported": 0,
                                        "message": "No US campaigns found in range."})
            seen_dates.sort()
            return self._send(200, {"ok": True, "imported": n,
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
