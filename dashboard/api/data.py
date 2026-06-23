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
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set in this Vercel project's environment variables.")
    # Railway public connections generally require SSL. If the URL doesn't
    # already specify sslmode, request it; fall back to a non-SSL attempt only
    # if the SSL attempt fails.
    try:
        if "sslmode=" not in url:
            return psycopg2.connect(url, cursor_factory=RealDictCursor,
                                    sslmode="require", connect_timeout=10)
        return psycopg2.connect(url, cursor_factory=RealDictCursor,
                                connect_timeout=10)
    except psycopg2.OperationalError:
        return psycopg2.connect(url, cursor_factory=RealDictCursor,
                                connect_timeout=10)


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

        # new product launches in window (table may not exist on older DBs)
        try:
            cur.execute("""
                SELECT competitor, product_name, first_seen, source_url
                FROM product_listings
                WHERE first_seen >= %s
                ORDER BY first_seen DESC
            """, (start_iso,))
            launches = _serialize(cur.fetchall())
        except Exception:
            launches = []

        # latest price per (strain, competitor)
        try:
            cur.execute("""
                SELECT DISTINCT ON (strain, competitor)
                       strain, competitor, product_name, price, currency,
                       pack_size, per_seed, in_stock, observed_at
                FROM price_observations
                ORDER BY strain, competitor, observed_at DESC
            """)
            prices = _serialize(cur.fetchall())
        except Exception:
            prices = []

    return {"sales": sales, "snapshots": snapshots,
            "launches": launches, "prices": prices}


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
            msg = str(e)
            low = msg.lower()
            if "could not translate host name" in low or "railway.internal" in low:
                hint = ("Looks like the INTERNAL Railway URL. In Vercel set DATABASE_URL "
                        "to Railway's DATABASE_PUBLIC_URL (host ends in proxy.rlwy.net).")
            elif "ssl" in low:
                hint = "SSL issue — try appending ?sslmode=require to the connection string."
            elif "does not exist" in low or "no such table" in low or "relation" in low:
                hint = ("Connected, but tables are empty/missing. Run the Railway scrape "
                        "once so the tables exist.")
            elif "database_url is not set" in low:
                hint = "Add DATABASE_URL in Vercel → Settings → Environment Variables, then redeploy."
            else:
                hint = "See the message above; check the Vercel function logs for the full traceback."
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"error": msg, "hint": hint}).encode("utf-8"))
