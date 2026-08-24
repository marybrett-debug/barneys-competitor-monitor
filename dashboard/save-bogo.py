"""
Save a weekly BOGO list pasted from the dashboard.

Accepts JSON: { "week_label": "Week 8", "week_date": "2026-08-17", "text": "<pasted list>" }
Parses each line into strain + pack-size BOGOs, then replaces that week's list.

Pack-size format: each number is a pack size offered as buy-one-get-one, so
  "5+10"   -> 5+5 and 10+10
  "3+5+10" -> 3+3, 5+5, 10+10
Only pack sizes 3, 5, 10 are valid (filters out numbers embedded in strain
names like "Dos Si Dos 33" or "RS11 F1").
"""
import os
import re
import json
from http.server import BaseHTTPRequestHandler

import psycopg2

VALID_PACKS = {3, 5, 10}


def get_conn():
    url = os.environ["DATABASE_URL"]
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return psycopg2.connect(url)


def parse_bogo_line(line):
    """Return {strain, packs, bogos} or None if no BOGO tier found."""
    line = line.strip()
    if not line:
        return None
    # find the pack-size group: digits joined by + or - (e.g. 3+5, 5+10, 3+5+10, 3-5),
    # allowing a trailing "seeds" word before end of line
    m = re.search(r'(\d+(?:\s*[+\-]\s*\d+)+)\s*(?:seeds?)?\s*$', line, re.I)
    if not m:
        return None
    raw = m.group(1)
    nums = [int(x) for x in re.split(r'[+\-]', raw) if x.strip().isdigit()]
    packs = [n for n in nums if n in VALID_PACKS]
    if not packs:
        return None
    packs = sorted(set(packs))
    # strain name = everything before the tier group, minus noise
    name = line[:m.start()]
    name = re.sub(r'[-–]\s*Regular\s*[-–]?', ' ', name, flags=re.I)
    name = re.sub(r'\bSeeds?\b', '', name, flags=re.I)
    name = re.sub(r'\s+', ' ', name).strip(' -–\t')
    if not name:
        return None
    bogos = ", ".join(f"{p}+{p}" for p in packs)
    return {"strain": name, "packs": ",".join(str(p) for p in packs), "bogos": bogos}


def parse_bogo_text(text):
    rows = []
    seen = set()
    for line in text.splitlines():
        # skip a bare "Week N" header line
        if re.match(r'^\s*week\s+\d+\s*$', line, re.I):
            continue
        r = parse_bogo_line(line)
        if r and r["strain"].lower() not in seen:
            seen.add(r["strain"].lower())
            rows.append(r)
    return rows


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            week_label = (payload.get("week_label") or "").strip()
            week_date = (payload.get("week_date") or "").strip() or None
            text = payload.get("text") or ""
            if not week_label:
                return self._send(400, {"error": "week_label is required"})
            rows = parse_bogo_text(text)
            if not rows:
                return self._send(400, {"error": "No BOGO entries could be parsed from the text."})

            conn = get_conn()
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bogo_lists (
                    id SERIAL PRIMARY KEY, week_label TEXT NOT NULL, week_date DATE,
                    strain TEXT NOT NULL, packs TEXT, bogos TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now())
            """)
            cur.execute("DELETE FROM bogo_lists WHERE week_label = %s", (week_label,))
            for r in rows:
                cur.execute("""
                    INSERT INTO bogo_lists (week_label, week_date, strain, packs, bogos)
                    VALUES (%s,%s,%s,%s,%s)
                """, (week_label, week_date, r["strain"], r["packs"], r["bogos"]))
            conn.commit()
            cur.close(); conn.close()
            return self._send(200, {"ok": True, "week_label": week_label,
                                    "saved": len(rows),
                                    "multi_tier": sum(1 for r in rows if "," in r["packs"])})
        except KeyError:
            return self._send(500, {"error": "DATABASE_URL not configured on the server."})
        except Exception as e:
            return self._send(500, {"error": str(e)})
