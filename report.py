"""
Weekly report: compares each competitor's snapshots over the past 7 days,
flags what changed, and posts a summary to Slack via an incoming webhook.

Run modes (set via CLI arg in main.py):
  scrape  -> run the daily scrape + store
  report  -> build + post the weekly Slack summary
"""
import os
import json
import urllib.request
from datetime import datetime, timedelta, timezone

import db


def _fmt(v):
    return v if v else "—"


def _competitor_section(name, is_us=False):
    """Return a list of Slack blocks describing this week's change for one site."""
    snaps = db.latest_two_snapshots(name)
    if not snaps:
        return [{"type": "section",
                 "text": {"type": "mrkdwn", "text": f"*{name}*\n_No data captured yet._"}}]

    newest = snaps[0]
    prev = snaps[1] if len(snaps) > 1 else None
    changed = prev is not None and newest["content_hash"] != prev["content_hash"]

    title = f":star: {name} (us)" if is_us else name
    status = ":large_green_circle: *CHANGED*" if changed else ":white_circle: no change"

    fields = [("Discount", "discount_text"), ("Codes", "codes"),
              ("Free seeds", "free_seeds"), ("Free shipping", "shipping")]
    lines = []
    for label, key in fields:
        now_v = _fmt(newest.get(key))
        was_v = _fmt(prev.get(key)) if prev else "—"
        mark = " :arrow_left: _changed_" if (prev and now_v != was_v) else ""
        lines.append(f"• *{label}:* {now_v}{mark}")

    captured = newest["captured_at"].strftime("%b %d, %Y")
    body = (f"*{title}*  {status}\n"
            + "\n".join(lines)
            + f"\n<{newest['url']}|view page> · captured {captured}")
    return [{"type": "section", "text": {"type": "mrkdwn", "text": body}},
            {"type": "divider"}]


def build_slack_payload():
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "📊 Competitor Promo Watch"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn", "text": f"Week of {today} · Higher and Higher"}]},
        {"type": "divider"},
    ]

    # Health warnings first if any
    warnings = db.recent_health(week_ago)
    if warnings:
        wlines = "\n".join(
            f"• *{w['competitor']}* — {w['status']}: {w['detail']} "
            f"({w['checked_at'].strftime('%b %d')})"
            for w in warnings
        )
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn",
                                "text": f":warning: *Scraper health warnings*\n{wlines}"}})
        blocks.append({"type": "divider"})

    # Our own site pinned first
    blocks += _competitor_section("Barney's Farm", is_us=True)
    blocks.append({"type": "section",
                   "text": {"type": "mrkdwn", "text": "*— Competitors —*"}})
    for name in ["ILGM", "Royal Queen Seeds", "Sensi Seeds", "Seedsman"]:
        blocks += _competitor_section(name)

    return {"blocks": blocks}


def post_to_slack(payload: dict):
    webhook = os.environ["SLACK_WEBHOOK_URL"]
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Slack returned {resp.status}")


def run_report():
    payload = build_slack_payload()
    post_to_slack(payload)
    print("Weekly report posted to Slack.")
