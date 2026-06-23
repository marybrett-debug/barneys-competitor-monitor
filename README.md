# Barney's Farm — Competitor Promo Monitor

Daily scrape + weekly email report of competitor storewide promos
(ILGM, Royal Queen Seeds, Sensi Seeds, Seedsman), with Barney's Farm tracked
alongside and pinned to the top of each report.

Same architecture as the dropship importer: Python service on Railway,
Postgres for storage, cron schedules for timing.

---

## What it does

- **Daily** (`python main.py scrape`): three passes per run —
  (a) **promo pages**: discounts, codes, free-seed offers, shipping, plus
  **spend-threshold tiers** and **promo end dates**;
  (b) **new product launches**: scrapes each competitor's new-arrivals page and
  flags products it hasn't seen before;
  (c) **head-to-head strain prices**: searches each site for the strains in
  `TRACKED_STRAINS` and records the listed price + stock status.
  Everything is timestamped into Postgres to build history.
- **Weekly** (`python main.py report`): compares the two most recent snapshots per
  competitor and posts a summary to **Slack** (incoming webhook).
- **Sales upload** (`python import_sales.py sales.csv`): loads your daily sales
  figures into Postgres so they can be charted against promo activity. Re-uploading
  a date overwrites it.
- **Dashboard** (`/dashboard`, deploy to Vercel): a web view overlaying daily
  revenue against promo-change markers, with a promo-changes timeline and sales table.
- **Self-monitoring**: if a competitor page redesigns or blocks the scraper, it logs
  a health warning (instead of silently storing nothing) and the weekly Slack post flags it.

---

## Deploy on Railway

1. **Push this folder to GitHub.** First create an empty repo at github.com
   (New repository → name it `barneys-competitor-monitor` → do NOT add a README,
   since one is already included). Then, from inside the folder:
   ```bash
   git init
   git add .
   git commit -m "Competitor promo monitor"
   git branch -M main
   git remote add origin https://github.com/marybrett-debug/barneys-competitor-monitor.git
   git push -u origin main
   ```
   The included `.gitignore` keeps secrets (`.env`) and Python cruft out of the repo.

2. **Create a Railway project** → "Deploy from GitHub repo" → pick the repo.
   Railway auto-detects the Dockerfile.

3. **Add a Postgres plugin** to the project (New → Database → PostgreSQL).
   Railway automatically injects `DATABASE_URL` into your service. No manual wiring.

4. **Set environment variables** on the service (Variables tab):
   ```
   SLACK_WEBHOOK_URL = https://hooks.slack.com/services/XXX/YYY/ZZZ
   ```
   `DATABASE_URL` is provided automatically by the Postgres plugin.

   To get the webhook: in Slack, create an app at api.slack.com/apps →
   Incoming Webhooks → activate → "Add New Webhook to Workspace" → pick the
   channel (e.g. #promo-watch) → copy the URL.

5. **Set up two cron schedules.** In Railway, the cleanest pattern is two services
   (or two cron jobs) pointing at the same image with different start commands:

   - **Daily scrape** — cron `0 6 * * *` (06:00 UTC daily)
     start command: `python main.py scrape`
   - **Weekly report** — cron `0 13 * * 1` (Mondays 13:00 UTC ≈ 8am Dallas)
     start command: `python main.py report`

   Set these under each service's Settings → Deploy → Cron Schedule + Custom Start Command.

6. **First run**: trigger the scrape service once manually. It auto-creates the
   schema. You need at least two daily captures before the weekly diff shows changes.

> **Important — set the cron schedule, or it crash-loops.** This app is a job that
> runs once and exits, not a long-running server. Without a Cron Schedule set,
> Railway treats the clean exit as a crash and restarts it forever. Setting the
> cron schedule (step 5) tells Railway to run it on schedule and let it exit.

---

## Uploading daily sales

Sales are entered manually via CSV — no store credentials needed.

1. Make a CSV with a header row. Columns (case-insensitive): `date` (YYYY-MM-DD,
   required), `revenue`, `orders`, `units`, `note`. See `sales_template.csv`.
2. Load it into the same Postgres:
   ```bash
   python import_sales.py sales.csv
   ```
   Re-uploading a date overwrites that day, so you can re-run with corrections.

You can run this locally (set `DATABASE_URL` to the Railway Postgres connection
string, copyable from the Postgres plugin's Connect tab) or as a one-off Railway
job. Easiest: run it from your laptop whenever you have new numbers.

---

## Dashboard (Vercel)

The `/dashboard` folder is a separate deploy (same pattern as the SEO dashboard).

1. Deploy the `dashboard` folder to Vercel (new project → import → root = `dashboard`).
2. In the Vercel project's **Environment Variables**, add `DATABASE_URL` set to the
   **same** Railway Postgres connection string the scraper uses.
3. Open the deployed URL. It shows daily revenue as a line, dashed vertical markers
   on days any site changed its promo (green = Barney's Farm, grey = competitor), a
   promo-changes timeline with before/after values, and your uploaded sales table.

The dashboard is **read-only** — it never writes to the database.

---

## Notes & gotchas

- **Slack webhook**: a single incoming-webhook URL posts to one channel. Keep the
  URL secret (it's a write key to that channel) — it lives only in Railway env vars,
  never in the repo.
- **Terms of service**: automated scraping may conflict with a site's ToS. This is
  your call to make knowingly. The scraper uses a normal browser user-agent and a
  gentle once-daily cadence to stay low-impact.
- **Brittleness**: competitor URLs in `scraper.py` (the `COMPETITORS` dict) may need
  updating if they move their promo pages. When a page changes structure, you'll get
  a health warning in the weekly Slack post rather than silent failure — that's your
  cue to check the URL/signals for that competitor.
- **Adding a competitor**: add an entry to `COMPETITORS` in `scraper.py` with its
  promo URL, a few expected keyword `signals`, and optionally a `new_url` for its
  new-arrivals page. No other changes needed.
- **Tracking different strains**: edit the `TRACKED_STRAINS` list in `scraper.py`.
  These are the head-to-head strains the price scraper searches for on each site.
- **Heavier scrape = more exposure**: the medium tier now hits ~3 pages + several
  search queries per competitor per day. That's more traffic than the original
  single-page scrape, so it's more likely to trip bot-detection and leans harder on
  each site's ToS. Prices/launches are parsed heuristically and will be the first
  things to break when a site changes layout — they log health warnings rather than
  failing silently, but expect to tune the selectors/patterns occasionally.
- **Reading correlation carefully**: the dashboard shows *timing alignment*, not
  proof of cause. A sales bump during a promo is suggestive, but seasonality, paid
  ads, and competitor moves overlap. Treat it as a hypothesis generator, not a verdict.
- **Tuning what's extracted**: the regex patterns at the top of `scraper.py`
  (DISCOUNT_RE, CODE_RE, etc.) control which fields are pulled. Adjust as you learn
  what each competitor's wording looks like.

---

## Files

- `main.py` — entry point, dispatches scrape/report modes
- `scraper.py` — per-competitor config + resilient page parsing
- `db.py` — Postgres schema + queries (promos, health, daily sales)
- `report.py` — weekly diff + Slack post
- `import_sales.py` — CSV → daily_sales importer
- `sales_template.csv` — example sales upload format
- `Dockerfile` — Playwright image for Railway
- `requirements.txt` — Python deps
- `.gitignore` — keeps secrets/cruft out of git
- `dashboard/` — Vercel deploy: `index.html` (chart UI), `api/data.py`
  (read-only JSON endpoint), `vercel.json`, `requirements.txt`
