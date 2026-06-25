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
            SELECT competitor, captured_at, headline, discount_text, codes,
                   free_seeds, shipping, spend_tiers, promo_ends,
                   left(raw_text, 4000) AS raw_text, content_hash
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

        # Klaviyo email campaigns
        try:
            cur.execute("""
                SELECT send_date, subject, campaign_name, open_rate, click_rate,
                       recipients, unsubscribes
                FROM klaviyo_campaigns
                ORDER BY send_date ASC
            """)
            campaigns = _serialize(cur.fetchall())
        except Exception:
            campaigns = []

        # all special offers across weeks — grouped by capture day on the client
        try:
            cur.execute("""
                SELECT strain, offer, price, was_price, is_discounted,
                       currency, captured_at::date AS week
                FROM special_offers
                ORDER BY captured_at::date DESC, strain ASC
            """)
            special_offers = _serialize(cur.fetchall())
        except Exception:
            special_offers = []

    # ---- promo performance: avg daily revenue during vs baseline around it ----
    performance = _compute_performance(sales, promos)
    _attach_engagement(performance, campaigns)

    # ---- current competitor state: latest snapshot per competitor ----
    competitor_state = _latest_competitor_state(snapshots)
    # ---- competitor state grouped by week (for the week filter) ----
    competitor_weeks = _competitor_state_by_week(snapshots)

    # ---- monthly year-over-year comparison ----
    monthly = _compute_monthly(sales, promos, campaigns)

    return {"sales": sales, "snapshots": snapshots,
            "launches": launches, "prices": prices,
            "promos": promos, "performance": performance,
            "monthly": monthly, "campaigns": campaigns,
            "special_offers": special_offers,
            "competitor_state": competitor_state,
            "competitor_weeks": competitor_weeks}


def _latest_competitor_state(snapshots):
    """Return the most recent snapshot for each competitor with all promo fields,
    for the 'what is each competitor offering right now' table."""
    latest = {}
    for s in snapshots:
        c = s.get("competitor")
        if not c:
            continue
        prev = latest.get(c)
        if prev is None or (s.get("captured_at") or "") > (prev.get("captured_at") or ""):
            latest[c] = s
    out = []
    for c in sorted(latest.keys()):
        s = latest[c]
        out.append({
            "competitor": c,
            "captured_at": s.get("captured_at"),
            "headline": s.get("headline"),
            "discount_text": s.get("discount_text"),
            "codes": s.get("codes"),
            "free_seeds": s.get("free_seeds"),
            "shipping": s.get("shipping"),
            "spend_tiers": s.get("spend_tiers"),
            "promo_ends": s.get("promo_ends"),
            "raw_text": s.get("raw_text"),
        })
    return out


def _competitor_state_by_week(snapshots):
    """Group competitor snapshots by ISO week (Monday date). For each week,
    keep the latest snapshot per competitor within that week. Returns:
      { "weeks": [list of week-start dates, newest first],
        "byWeek": { week: [ {competitor, ...fields}, ... ] } }
    so the dashboard can offer a week dropdown."""
    from datetime import date as _date, timedelta as _td

    def parse(d):
        try:
            return _date.fromisoformat((d or "")[:10])
        except Exception:
            return None

    def week_start(d):
        # Monday of that date's week
        return (d - _td(days=d.weekday())).isoformat()

    # week -> competitor -> latest snapshot in that week
    grouped = {}
    for s in snapshots:
        c = s.get("competitor")
        d = parse(s.get("captured_at"))
        if not c or not d:
            continue
        wk = week_start(d)
        slot = grouped.setdefault(wk, {})
        prev = slot.get(c)
        if prev is None or (s.get("captured_at") or "") > (prev.get("captured_at") or ""):
            slot[c] = s

    weeks = sorted(grouped.keys(), reverse=True)
    by_week = {}
    for wk in weeks:
        comps = grouped[wk]
        by_week[wk] = [{
            "competitor": c,
            "captured_at": comps[c].get("captured_at"),
            "headline": comps[c].get("headline"),
            "discount_text": comps[c].get("discount_text"),
            "codes": comps[c].get("codes"),
            "free_seeds": comps[c].get("free_seeds"),
            "shipping": comps[c].get("shipping"),
            "spend_tiers": comps[c].get("spend_tiers"),
            "promo_ends": comps[c].get("promo_ends"),
            "raw_text": comps[c].get("raw_text"),
        } for c in sorted(comps.keys())]
    return {"weeks": weeks, "byWeek": by_week}


def _dedupe_subjects(subjects):
    """Collapse near-duplicate / reminder subjects to distinct messaging.
    Keys on the offer tokens so reminder sends of the same promo collapse."""
    import re as _re

    def normalize(s):
        core = (s or "").lower()
        core = core.encode("ascii", "ignore").decode("ascii")
        core = _re.sub(r"[^a-z0-9 ]", " ", core)
        core = _re.sub(r"\s+", " ", core).strip()
        # drop decorative / urgency / filler tokens so reminders match the launch
        drop = {"last","chance","ends","end","tonight","midnight","monday","sunday",
                "soon","final","hours","hour","hurry","left","dont","miss","happy",
                "today","now","extended","offer","reminder","starts","start","live",
                "our","is","the","a","an","for","your","you","x","plus","get","got",
                "save","off"}
        toks = [t for t in core.split() if t not in drop and len(t) > 1]
        return " ".join(sorted(set(toks)))

    seen = set()
    out = []
    for s in subjects:
        key = normalize(s)
        if key and key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _attach_engagement(performance, campaigns):
    """For each promo in the performance list, average the open/click rates of
    the email campaigns that went out during its window, and attach the subjects."""
    from datetime import date as _date

    def parse(d):
        try:
            return _date.fromisoformat(d[:10])
        except Exception:
            return None

    cs = []
    for c in campaigns:
        d = parse(c.get("send_date", ""))
        if d:
            cs.append((d, c))

    for p in performance:
        start = parse(p.get("start_date", ""))
        end = parse(p.get("end_date", "")) or start
        if not start:
            continue
        during = [c for (d, c) in cs if start <= d <= end]
        if not during:
            p["email_count"] = 0
            p["avg_open"] = None
            p["avg_click"] = None
            p["subjects"] = []
            continue
        opens = [c["open_rate"] for c in during if c.get("open_rate") is not None]
        clicks = [c["click_rate"] for c in during if c.get("click_rate") is not None]
        p["email_count"] = len(during)
        p["avg_open"] = round(sum(opens) / len(opens), 1) if opens else None
        p["avg_click"] = round(sum(clicks) / len(clicks), 2) if clicks else None
        p["subjects"] = _dedupe_subjects([c["subject"] for c in during])


def _compute_monthly(sales, promos, campaigns=None):
    """Aggregate revenue/orders by calendar month and attach the promo(s) and
    email subjects that ran in that month. Returns {year: {month: {...}}}."""
    from datetime import date as _date
    campaigns = campaigns or []

    def parse(d):
        try:
            return _date.fromisoformat(d[:10])
        except Exception:
            return None

    data = {}  # year -> month -> {rev, orders, days}
    for s in sales:
        d = parse(s.get("sale_date", ""))
        if not d:
            continue
        y, m = d.year, d.month
        cell = data.setdefault(y, {}).setdefault(m, {"rev": 0.0, "orders": 0, "days": 0})
        cell["rev"] += float(s["revenue"]) if s.get("revenue") is not None else 0.0
        cell["orders"] += int(s["orders"]) if s.get("orders") is not None else 0
        cell["days"] += 1

    # attach promos: a promo belongs to a month if its window overlaps it
    promo_by_ym = {}  # (year,month) -> list of {label, notes}
    for p in promos:
        ps, pe = parse(p.get("start_date", "")), parse(p.get("end_date", ""))
        if not ps:
            continue
        pe = pe or ps
        y, m = ps.year, ps.month
        while (y < pe.year) or (y == pe.year and m <= pe.month):
            label = p.get("promo_name", "")
            if p.get("discount"):
                label += f" ({p['discount']})"
            promo_by_ym.setdefault((y, m), []).append(
                {"label": label, "notes": p.get("notes") or ""})
            m += 1
            if m > 12:
                m = 1
                y += 1

    # attach email subjects by the month they were sent
    subjects_by_ym = {}  # (year,month) -> list of {subject, open, click}
    for c in campaigns:
        d = parse(c.get("send_date", ""))
        if not d:
            continue
        subjects_by_ym.setdefault((d.year, d.month), []).append({
            "subject": c.get("subject", ""),
            "open_rate": c.get("open_rate"),
            "click_rate": c.get("click_rate"),
        })

    years = sorted(data.keys())
    months = {}
    for m in range(1, 13):
        months[m] = {}
        for y in years:
            cell = data.get(y, {}).get(m)
            if cell:
                months[m][y] = {
                    "revenue": round(cell["rev"], 2),
                    "orders": cell["orders"],
                    "days": cell["days"],
                    "promos": promo_by_ym.get((y, m), []),
                    "subjects": subjects_by_ym.get((y, m), []),
                }
    return {"years": years, "months": months}


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
            "notes": p.get("notes"),
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
