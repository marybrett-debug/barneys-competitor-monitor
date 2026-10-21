"""
Vercel serverless endpoint: /api/strain-sales
Daily sales per strain, for the BOGO Results tab.

POST: upload the admin's "Retail pack sizes sold" export (Excel or CSV) as
      multipart/form-data. Columns: Date, Product ID, Product Name, Pack 1,
      Pack 3, Pack 5, Pack 10, Pack 25, Packs count, Loose Count, Total.
      Every date in the file replaces what was stored for that date, so
      re-uploading an overlapping range is safe.
      Returns {"ok": true, "imported": rows, "days": N, "range": "start..end"}
GET:  ?from=YYYY-MM-DD&to=YYYY-MM-DD → {"rows": [[date, product, total, packs, pack_10], ...],
      "range": "first..last stored"}
"""
import os
import io
import csv
import json
from datetime import datetime, date
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import psycopg2

# header text in the export → our column
COLUMNS = {
    "date": "sale_date", "product id": "product_id", "product name": "product_name",
    "pack 1": "pack_1", "pack 3": "pack_3", "pack 5": "pack_5", "pack 10": "pack_10",
    "pack 25": "pack_25", "packs count": "packs_count", "loose count": "loose_count",
    "total": "total",
}
NUMERIC = ["pack_1", "pack_3", "pack_5", "pack_10", "pack_25", "packs_count", "loose_count", "total"]


def _get_conn():
    url = os.environ["DATABASE_URL"]
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return psycopg2.connect(url)


def _ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS strain_sales (
            sale_date    DATE NOT NULL,
            product_id   TEXT NOT NULL,
            product_name TEXT NOT NULL,
            pack_1 NUMERIC, pack_3 NUMERIC, pack_5 NUMERIC, pack_10 NUMERIC, pack_25 NUMERIC,
            packs_count NUMERIC, loose_count NUMERIC, total NUMERIC,
            PRIMARY KEY (sale_date, product_id))
    """)


def _parse_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def _num(v):
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v or "").replace(",", "").replace("$", "").strip()
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def parse_rows(table):
    """table: iterable of row lists, header first. Returns a list of dicts keyed by our columns."""
    header = None
    out = []
    for r in table:
        if r is None:
            continue
        cells = [("" if c is None else c) for c in r]
        if header is None:
            low = [str(c).strip().lower() for c in cells]
            if "date" in low and "product name" in low:
                header = {i: COLUMNS[h] for i, h in enumerate(low) if h in COLUMNS}
            continue
        row = {col: cells[i] for i, col in header.items() if i < len(cells)}
        d = _parse_date(row.get("sale_date"))
        name = str(row.get("product_name") or "").strip()
        if not d or not name:      # skips the "Totals" line and blanks
            continue
        rec = {"sale_date": d, "product_name": name,
               "product_id": str(row.get("product_id") or name).strip()}
        for k in NUMERIC:
            rec[k] = _num(row.get(k))
        out.append(rec)
    if header is None:
        raise ValueError("This doesn't look like the Retail pack sizes sold export: "
                         "no header row with Date and Product Name.")
    missing = [c for c in ("total", "packs_count", "pack_10") if c not in header.values()]
    if missing:
        raise ValueError("The file is missing these columns: " + ", ".join(missing))
    return out


def rows_from_upload(fname, payload):
    name = (fname or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
        return parse_rows(wb.active.iter_rows(values_only=True))
    text = payload.decode("utf-8-sig", "ignore")
    lines = text.splitlines()
    if lines and lines[0].lower().startswith("sep="):
        lines = lines[1:]
    delim = ";" if lines and lines[0].count(";") > lines[0].count(",") else ","
    return parse_rows(csv.reader(lines, delimiter=delim))


def _extract_file(content_type, body):
    """Same multipart handling as upload-sales.py."""
    if "boundary=" not in (content_type or ""):
        return None, None
    from email.parser import BytesParser
    from email.policy import default as default_policy
    header = ("Content-Type: " + content_type + "\r\n\r\n").encode()
    msg = BytesParser(policy=default_policy).parsebytes(header + body)
    for part in msg.iter_parts():
        if "filename" in part.get("Content-Disposition", ""):
            return part.get_filename() or "", part.get_payload(decode=True) or b""
    return None, None


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            q = parse_qs(urlparse(self.path).query)
            lo = _parse_date((q.get("from") or [""])[0])
            hi = _parse_date((q.get("to") or [""])[0])
            conn = _get_conn()
            cur = conn.cursor()
            _ensure_table(cur)
            conn.commit()
            cur.execute("SELECT min(sale_date), max(sale_date) FROM strain_sales")
            first, last = cur.fetchone()
            rows = []
            if lo and hi:
                cur.execute("""
                    SELECT sale_date, product_name, total, packs_count, pack_10
                    FROM strain_sales WHERE sale_date BETWEEN %s AND %s
                    ORDER BY sale_date
                """, (lo, hi))
                rows = [[d.isoformat(), n, float(t or 0), float(p or 0), float(p10 or 0)]
                        for d, n, t, p, p10 in cur.fetchall()]
            cur.close(); conn.close()
            return self._send(200, {"rows": rows,
                                    "first": first.isoformat() if first else None,
                                    "last": last.isoformat() if last else None})
        except KeyError:
            return self._send(500, {"error": "DATABASE_URL not configured on the server."})
        except Exception as e:
            return self._send(500, {"error": f"Could not read strain sales: {e}"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            fname, payload = _extract_file(self.headers.get("Content-Type", ""), self.rfile.read(length))
            if not payload:
                return self._send(400, {"error": "No file found in upload."})
            rows = rows_from_upload(fname, payload)
            if not rows:
                return self._send(400, {"error": "No strain rows found in the file. Nothing was changed."})

            days = sorted({r["sale_date"] for r in rows})
            conn = _get_conn()
            cur = conn.cursor()
            _ensure_table(cur)
            cur.execute("DELETE FROM strain_sales WHERE sale_date = ANY(%s)", (days,))
            cols = ["sale_date", "product_id", "product_name"] + NUMERIC
            # the export can list a product twice on one day; add those together
            merged = {}
            for r in rows:
                key = (r["sale_date"], r["product_id"])
                if key in merged:
                    for k in NUMERIC:
                        merged[key][k] += r[k]
                else:
                    merged[key] = dict(r)
            from psycopg2.extras import execute_values
            execute_values(cur, f"INSERT INTO strain_sales ({', '.join(cols)}) VALUES %s",
                           [tuple(r[c] for c in cols) for r in merged.values()])
            cur.execute("""
                CREATE TABLE IF NOT EXISTS data_updates (
                  kind TEXT PRIMARY KEY, updated_at TIMESTAMPTZ NOT NULL,
                  source TEXT, detail TEXT)
            """)
            cur.execute("""
                INSERT INTO data_updates (kind, updated_at, source, detail)
                VALUES ('strain_sales', now(), 'import', %s)
                ON CONFLICT (kind) DO UPDATE SET
                  updated_at=now(), source='import', detail=EXCLUDED.detail
            """, (f"{days[0]} .. {days[-1]}",))
            conn.commit()
            cur.close(); conn.close()
            return self._send(200, {"ok": True, "imported": len(merged), "days": len(days),
                                    "range": f"{days[0].isoformat()} .. {days[-1].isoformat()}"})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except KeyError:
            return self._send(500, {"error": "DATABASE_URL not configured on the server."})
        except Exception as e:
            return self._send(500, {"error": f"Import failed: {e}"})
