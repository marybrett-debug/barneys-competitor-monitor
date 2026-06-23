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


def fetch_payload(days=800):
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

        # our own historical promo windows (for chart bands)
        try:
            cur.execute("""
                SELECT start_date, end_date, promo_name, discount, notes, is_major
                FROM barneys_promos
                ORDER BY start_date ASC
            """)
            promos = _serialize(cur.fetchall())
        except Exception:
            promos = []

    # ---- promo performance: avg daily revenue during vs baseline around it ----
    performance = _compute_performance(sales, promos)

    return {"sales": sales, "snapshots": snapshots,
            "launches": launches, "prices": prices,
            "promos": promos, "performance": performance}


def _compute_performance(sales, promos):
    """For each promo, average daily revenue during the window vs a clean
    baseline: the 14 days before + 14 days after, EXCLUDING any day that falls
    inside another promo window (so overlapping sales don't inflate the baseline).
    Returns list ranked by lift % descending."""
    from datetime import date as _date, timedelta as _td

    def parse(d):
        try:
            return _date.fromisoformat(d[:10])
        except Exception:
            return None

    # map date -> revenue
    rev = {}
    for s in sales:
        d = parse(s.get("sale_date", ""))
        if d and s.get("revenue") is not None:
            rev[d] = float(s["revenue"])

    # build list of all promo (start,end) ranges for exclusion checks
    ranges = []
    for p in promos:
        ps, pe = parse(p.get("start_date", "")), parse(p.get("end_date", ""))
        if ps:
            ranges.append((ps, pe or ps))

    def in_any_other_promo(day, my_start, my_end):
        for (rs, re_) in ranges:
            if rs == my_start and re_ == my_end:
                continue  # skip the promo we're measuring
            if rs <= day <= re_:
                return True
        return False

    out = []
    for p in promos:
        start = parse(p.get("start_date", ""))
        end = parse(p.get("end_date", "")) or start
        if not start:
            continue
        during = [rev[d] for d in rev if start <= d <= end]
        if not during:
            continue
        # baseline: 14 days before start + 14 days after end, excluding days
        # that fall inside any OTHER promo window
        b_start = start - _td(days=14)
        b_end = end + _td(days=14)
        baseline = [rev[d] for d in rev
                    if ((b_start <= d < start) or (end < d <= b_end))
                    and not in_any_other_promo(d, start, end)]
        during_avg = sum(during) / len(during)
        base_avg = (sum(baseline) / len(baseline)) if baseline else None
        lift = ((during_avg - base_avg) / base_avg * 100) if base_avg else None
        out.append({
            "promo_name": p.get("promo_name"),
            "discount": p.get("discount"),
            "start_date": p.get("start_date"),
            "end_date": p.get("end_date"),
            "is_major": p.get("is_major"),
            "days": len(during),
            "baseline_days": len(baseline),
            "during_avg": round(during_avg, 2),
            "baseline_avg": round(base_avg, 2) if base_avg is not None else None,
            "lift_pct": round(lift, 1) if lift is not None else None,
            "total_revenue": round(sum(during), 2),
        })
    # rank by lift desc, putting None lifts last
    out.sort(key=lambda x: (x["lift_pct"] is None, -(x["lift_pct"] or 0)))
    return out


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
