"""
Vercel serverless function: /api/data
Returns daily sales + competitor promo snapshots as JSON for the dashboard.

Reads DATABASE_URL from env (set the same Railway Postgres URL in Vercel's
project env vars — Settings → Environment Variables).

Read-only: this endpoint never writes.
"""
import os
import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

import psycopg2
from psycopg2.extras import RealDictCursor


def _conn():
    return psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=RealDictCursor)


def _serialize(rows):
    out = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, (datetime,)):
                d[k] = v.isoformat()
            elif hasattr(v, "isoformat"):   # date
                d[k] = v.isoformat()
            elif v is not None and type(v).__name__ == "Decimal":
                d[k] = float(v)
        out.append(d)
    return out


def fetch_payload(days=120):
    start = (datetime.now(timezone.utc) - timedelta(days=days))
    start_iso = start.isoformat()
    start_date = start.date().isoformat()
    end_date = datetime.now(timezone.utc).date().isoformat()

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT sale_date, revenue, orders, units, note
            FROM daily_sales
            WHERE sale_date BETWEEN %s AND %s
            ORDER BY sale_date ASC
        """, (start_date, end_date))
        sales = _serialize(cur.fetchall())

        cur.execute("""
            SELECT competitor, captured_at, discount_text, codes,
                   free_seeds, shipping, content_hash
            FROM promo_snapshots
            WHERE captured_at >= %s
            ORDER BY captured_at ASC
        """, (start_iso,))
        snapshots = _serialize(cur.fetchall())

    return {"sales": sales, "snapshots": snapshots}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            payload = fetch_payload()
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
