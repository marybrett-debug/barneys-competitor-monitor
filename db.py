"""
Database layer for the competitor promo monitor.
Postgres on Railway. Connection string comes from the DATABASE_URL env var
(Railway injects this automatically when you attach a Postgres plugin).
"""
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone


def get_conn():
    url = os.environ["DATABASE_URL"]
    return psycopg2.connect(url, cursor_factory=RealDictCursor)


def init_schema():
    """Create tables if they don't exist. Safe to run every startup."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS promo_snapshots (
                id              SERIAL PRIMARY KEY,
                competitor      TEXT NOT NULL,
                captured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
                url             TEXT NOT NULL,
                -- parsed fields
                headline        TEXT,
                discount_text   TEXT,      -- e.g. "Up to 42% off"
                codes           TEXT,      -- comma-separated promo codes found
                free_seeds      TEXT,      -- free-seed offer text
                shipping        TEXT,      -- free shipping threshold text
                spend_tiers     TEXT,      -- spend->reward ladder text
                promo_ends      TEXT,      -- end date / countdown text
                raw_text        TEXT,      -- full cleaned page text for fallback diffing
                -- a stable hash of the meaningful content, used to detect change
                content_hash    TEXT NOT NULL
            );
        """)
        # Add new columns if upgrading an existing DB (no-op if already present)
        cur.execute("ALTER TABLE promo_snapshots ADD COLUMN IF NOT EXISTS spend_tiers TEXT;")
        cur.execute("ALTER TABLE promo_snapshots ADD COLUMN IF NOT EXISTS promo_ends TEXT;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scrape_health (
                id            SERIAL PRIMARY KEY,
                competitor    TEXT NOT NULL,
                checked_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                status        TEXT NOT NULL,   -- 'ok' | 'warning' | 'error'
                detail        TEXT
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_snap_comp_time
            ON promo_snapshots (competitor, captured_at DESC);
        """)
        conn.commit()


def insert_snapshot(row: dict):
    row = {**{"spend_tiers": None, "promo_ends": None}, **row}
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO promo_snapshots
              (competitor, url, headline, discount_text, codes,
               free_seeds, shipping, spend_tiers, promo_ends, raw_text, content_hash)
            VALUES
              (%(competitor)s, %(url)s, %(headline)s, %(discount_text)s, %(codes)s,
               %(free_seeds)s, %(shipping)s, %(spend_tiers)s, %(promo_ends)s,
               %(raw_text)s, %(content_hash)s)
        """, row)
        conn.commit()


def log_health(competitor: str, status: str, detail: str = ""):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO scrape_health (competitor, status, detail)
            VALUES (%s, %s, %s)
        """, (competitor, status, detail))
        conn.commit()


def latest_two_snapshots(competitor: str):
    """Return the two most recent snapshots for a competitor (newest first)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM promo_snapshots
            WHERE competitor = %s
            ORDER BY captured_at DESC
            LIMIT 2
        """, (competitor,))
        return cur.fetchall()


def snapshots_since(competitor: str, since_iso: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM promo_snapshots
            WHERE competitor = %s AND captured_at >= %s
            ORDER BY captured_at ASC
        """, (competitor, since_iso))
        return cur.fetchall()


def recent_health(since_iso: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM scrape_health
            WHERE checked_at >= %s AND status <> 'ok'
            ORDER BY checked_at DESC
        """, (since_iso,))
        return cur.fetchall()


# ---- Daily sales (manual CSV upload) ----------------------------------------

def init_sales_schema():
    """Sales table, keyed by date so re-uploading a day overwrites it."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_sales (
                sale_date   DATE PRIMARY KEY,
                revenue     NUMERIC(12,2),
                orders      INTEGER,
                units       INTEGER,
                note        TEXT,
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        conn.commit()


def upsert_sales(sale_date, revenue=None, orders=None, units=None, note=None):
    """Insert or update a single day's sales. Re-uploading a date overwrites it."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO daily_sales (sale_date, revenue, orders, units, note, updated_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (sale_date) DO UPDATE SET
                revenue = EXCLUDED.revenue,
                orders  = EXCLUDED.orders,
                units   = EXCLUDED.units,
                note    = EXCLUDED.note,
                updated_at = now()
        """, (sale_date, revenue, orders, units, note))
        conn.commit()


def sales_between(start_iso: str, end_iso: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT sale_date, revenue, orders, units, note
            FROM daily_sales
            WHERE sale_date BETWEEN %s AND %s
            ORDER BY sale_date ASC
        """, (start_iso, end_iso))
        return cur.fetchall()


def all_snapshots_between(start_iso: str, end_iso: str):
    """All promo snapshots in a window, for dashboard overlay."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT competitor, captured_at, discount_text, codes,
                   free_seeds, shipping, content_hash
            FROM promo_snapshots
            WHERE captured_at BETWEEN %s AND %s
            ORDER BY captured_at ASC
        """, (start_iso, end_iso))
        return cur.fetchall()


# ---- Product launches + competitor pricing (medium-tier scrape) -------------

def init_intel_schema():
    """Tables for new-product detection and head-to-head price tracking."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS product_listings (
                id           SERIAL PRIMARY KEY,
                competitor   TEXT NOT NULL,
                product_name TEXT NOT NULL,
                first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
                source_url   TEXT,
                UNIQUE (competitor, product_name)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS price_observations (
                id           SERIAL PRIMARY KEY,
                competitor   TEXT NOT NULL,
                strain       TEXT NOT NULL,       -- normalized strain key we track
                product_name TEXT,                -- as listed on their site
                price        NUMERIC(10,2),
                currency     TEXT,
                pack_size    TEXT,                -- e.g. "5 seeds" if detected
                in_stock     BOOLEAN,
                observed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                source_url   TEXT
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_price_strain_time
            ON price_observations (strain, competitor, observed_at DESC);
        """)
        conn.commit()


def upsert_product(competitor, product_name, source_url=None):
    """Record a product listing. Returns True if it's NEW (first time seen)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO product_listings (competitor, product_name, source_url)
            VALUES (%s, %s, %s)
            ON CONFLICT (competitor, product_name)
            DO UPDATE SET last_seen = now()
            RETURNING (xmax = 0) AS inserted
        """, (competitor, product_name, source_url))
        return cur.fetchone()["inserted"]


def insert_price(competitor, strain, product_name, price, currency,
                 pack_size=None, in_stock=None, source_url=None):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO price_observations
              (competitor, strain, product_name, price, currency,
               pack_size, in_stock, source_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (competitor, strain, product_name, price, currency,
              pack_size, in_stock, source_url))
        conn.commit()


def new_products_since(since_iso: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT competitor, product_name, first_seen, source_url
            FROM product_listings
            WHERE first_seen >= %s
            ORDER BY first_seen DESC
        """, (since_iso,))
        return cur.fetchall()


def latest_prices_by_strain():
    """Most recent price per (strain, competitor) for the dashboard table."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (strain, competitor)
                   strain, competitor, product_name, price, currency,
                   pack_size, in_stock, observed_at, source_url
            FROM price_observations
            ORDER BY strain, competitor, observed_at DESC
        """)
        return cur.fetchall()
