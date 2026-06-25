"""
Scraper for competitor promo pages.

Each competitor has a config: the URL to watch and a list of keyword "signals"
we expect to find. If none of the expected signals appear, we treat the scrape
as suspicious (page redesign / block / empty render) and log a health warning
instead of silently storing junk.

Parsing philosophy: promo pages change layout often, so we DON'T rely on fragile
CSS selectors for specific elements. Instead we pull the full visible text, then
use keyword/regex extraction to pick out the fields we care about (discounts,
codes, free-seed offers, shipping). This is far more robust than selector-based
scraping. The full raw_text is also stored so the weekly diff can catch changes
we didn't explicitly parse.
"""
import re
import hashlib
from playwright.sync_api import sync_playwright

# ---- Per-competitor configuration -------------------------------------------
# 'signals' = words we expect on a healthy promo page. Missing ALL of them = warning.
COMPETITORS = {
    "Barney's Farm": {
        "url": "https://www.barneysfarm.com/special-offer-seeds",
        "signals": ["off", "free", "seeds", "%"],
        "is_us": True,   # our own site — pinned to the top of the report
    },
    "ILGM": {
        "url": "https://ilgm.com/pages/coupons",
        "signals": ["off", "code", "seeds", "discount", "%"],
    },
    "Royal Queen Seeds": {
        "url": "https://www.royalqueenseeds.com/us/content/129-promotions",
        "signals": ["off", "free", "seeds", "shipping", "%"],
    },
    "Sensi Seeds": {
        "url": "https://sensiseeds.com/en/sale",
        "signals": ["off", "free", "seeds", "sale", "%"],
    },
    "Seedsman": {
        "url": "https://www.seedsman.com/us-en/special-offers",
        "signals": ["off", "free", "seeds", "offer", "%"],
    },
}

# ---- Field extraction patterns ----------------------------------------------
DISCOUNT_RE = re.compile(r"(up to\s*)?\d{1,2}\s*%\s*off", re.IGNORECASE)
CODE_RE = re.compile(r"\b(?:code|coupon|use)\b[:\s]*([A-Z0-9]{3,15})", re.IGNORECASE)
FREE_SEED_RE = re.compile(r"(\d+\s*free\s*seeds?|free\s*seeds?)", re.IGNORECASE)
SHIPPING_RE = re.compile(r"free\s*shipping[^.]{0,60}", re.IGNORECASE)


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "")
    return text.strip()


def _extract(raw: str) -> dict:
    discounts = sorted(set(m.group(0).strip() for m in DISCOUNT_RE.finditer(raw)))
    codes = sorted(set(m.group(1).strip() for m in CODE_RE.finditer(raw)))
    free = sorted(set(m.group(0).strip() for m in FREE_SEED_RE.finditer(raw)))
    ship = sorted(set(m.group(0).strip() for m in SHIPPING_RE.finditer(raw)))
    return {
        "discount_text": "; ".join(discounts)[:500] or None,
        "codes": ", ".join(codes)[:300] or None,
        "free_seeds": "; ".join(free)[:300] or None,
        "shipping": "; ".join(ship)[:300] or None,
    }


def _hash(parsed: dict, raw: str) -> str:
    """Hash the meaningful, parsed content so trivial page noise doesn't
    register as 'change'. Falls back to raw text if nothing parsed."""
    basis = "|".join([
        parsed.get("discount_text") or "",
        parsed.get("codes") or "",
        parsed.get("free_seeds") or "",
        parsed.get("shipping") or "",
    ])
    if not basis.strip("|"):
        basis = raw[:2000]
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def scrape_one(name: str, cfg: dict) -> dict:
    """Returns (row_for_db, health_status, health_detail)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36")
        )
        try:
            page.goto(cfg["url"], wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(2500)  # let lazy promo banners settle
            body = page.inner_text("body")
        except Exception as e:
            browser.close()
            return None, "error", f"fetch failed: {e}"
        browser.close()

    raw = _clean(body)
    low = raw.lower()

    # Health check: did we land on a real promo page?
    if len(raw) < 200:
        return None, "warning", f"page text suspiciously short ({len(raw)} chars)"
    if not any(sig.lower() in low for sig in cfg["signals"]):
        return None, "warning", "no expected promo signals found (possible redesign/block)"

    parsed = _extract(raw)
    # headline = first reasonably long line of text
    headline = next((ln for ln in body.splitlines() if len(ln.strip()) > 15), "")[:300]

    row = {
        "competitor": name,
        "url": cfg["url"],
        "headline": _clean(headline) or None,
        **parsed,
        "raw_text": raw[:8000],  # cap stored text
        "content_hash": _hash(parsed, raw),
    }
    return row, "ok", ""


def scrape_all():
    results = []
    for name, cfg in COMPETITORS.items():
        try:
            row, status, detail = scrape_one(name, cfg)
        except Exception as e:
            row, status, detail = None, "error", f"unhandled: {e}"
        results.append((name, row, status, detail))
    return results
