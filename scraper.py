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
# 'url'        = primary promo page.
# 'extra_urls' = optional additional promo pages; their text is merged into the
#                same daily snapshot (useful when a site splits promos across pages).
# 'new_url'    = optional new-arrivals/new-strains page to detect product launches.
COMPETITORS = {
    "Barney's Farm": {
        "url": "https://www.barneysfarm.com/special-offer-seeds",
        "signals": ["off", "free", "seeds", "%"],
        "new_url": "https://www.barneysfarm.com/new-cannabis-seeds",
        "is_us": True,   # our own site — pinned to the top of the report
    },
    "ILGM": {
        "url": "https://ilgm.com/collections/deals",
        "signals": ["off", "code", "seeds", "discount", "%"],
        "new_url": "https://ilgm.com/collections/new-marijuana-seeds",
    },
    "Royal Queen Seeds": {
        "url": "https://www.royalqueenseeds.com/content/54-promos-and-discount-codes",
        "extra_urls": [
            "https://www.royalqueenseeds.com/content/58-cheap-cannabis-seeds",
            "https://www.royalqueenseeds.com/bogo",
        ],
        "signals": ["off", "free", "seeds", "shipping", "%"],
        "new_url": "https://www.royalqueenseeds.com/us/9-feminized-cannabis-seeds?orderby=date_add&orderway=desc",
    },
    "Sensi Seeds": {
        "url": "https://sensiseeds.com/en/sale",
        "signals": ["off", "free", "seeds", "sale", "%"],
        "new_url": "https://sensiseeds.com/en/cannabis-seeds/new",
    },
    "Seedsman": {
        "url": "https://www.seedsman.com/us-en/promotions",
        "signals": ["off", "free", "seeds", "offer", "%"],
        "new_url": "https://www.seedsman.com/us-en/new-products",
    },
}

# Head-to-head strains: strains where you compete directly. The scraper searches
# each competitor's site for these and records the listed price. Edit freely.
# Key = normalized strain name we track; values = search terms / aliases.
TRACKED_STRAINS = [
    "Gelato",
    "Runtz",
    "Mimosa",
    "Zkittlez",
    "Wedding Cake",
    "Gorilla Glue",
    "Northern Lights",
    "Amnesia Haze",
]

# ---- Field extraction patterns ----------------------------------------------
DISCOUNT_RE = re.compile(r"(up to\s*)?\d{1,2}\s*%\s*off", re.IGNORECASE)
CODE_RE = re.compile(r"\b(?:code|coupon|use)\b[:\s]*([A-Z0-9]{3,15})", re.IGNORECASE)
FREE_SEED_RE = re.compile(r"(\d+\s*free\s*seeds?|free\s*seeds?)", re.IGNORECASE)
SHIPPING_RE = re.compile(r"free\s*shipping[^.]{0,60}", re.IGNORECASE)
# spend-threshold ladders: "spend $100 get 3 free", "orders over £50", etc.
SPEND_RE = re.compile(
    r"(spend|orders?\s*over|when\s*you\s*spend|over)\s*[€£$]\s*\d{1,4}[^.\n]{0,60}",
    re.IGNORECASE)
# promo end dates / countdowns — require a month name, date, day, or time after
ENDS_RE = re.compile(
    r"(?:ends?|expires?|valid\s*(?:until|till|through))\s*"
    r"(?:on\s*)?"
    r"(?:\d{1,2}[./-]\d{1,2}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*\d{0,2}|"
    r"(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*|"
    r"midnight|tonight|today|tomorrow|\d{1,2}\s*(?:am|pm))",
    re.IGNORECASE)
# price like $39.00, £24, €19.95 (symbol before) OR 19,95 € (symbol after)
PRICE_RE = re.compile(r"([€£$])\s*(\d{1,4}(?:[.,]\d{2})?)")
PRICE_TRAILING_RE = re.compile(r"(\d{1,4}(?:[.,]\d{2})?)\s*([€£$])")
# pack size: "5 seeds", "pack of 10", "10 seed", "3-seeds", "10er", "x5"
PACK_RE = re.compile(
    r"(?:pack\s*of\s*(\d{1,2})|"
    r"(\d{1,2})\s*[-\s]?seeds?\b|"
    r"(\d{1,2})\s*er\b|"
    r"x\s*(\d{1,2})\b)",
    re.IGNORECASE)


def _parse_price_and_pack(text):
    """From a chunk of text, return (currency, price, pack_size_int) best-effort.
    Pairs the first price with the nearest pack-size number found in the text.
    Handles both '$39.00' and '39,95 €' formats. Returns Nones if no price."""
    pm = PRICE_RE.search(text or "")
    if pm:
        cur = pm.group(1)
        price = float(pm.group(2).replace(",", "."))
    else:
        pm = PRICE_TRAILING_RE.search(text or "")
        if not pm:
            return None, None, None
        cur = pm.group(2)
        price = float(pm.group(1).replace(",", "."))
    pack = None
    km = PACK_RE.search(text or "")
    if km:
        for g in km.groups():
            if g:
                pack = int(g)
                break
    return cur, price, pack


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "")
    return text.strip()


def _extract(raw: str) -> dict:
    discounts = sorted(set(m.group(0).strip() for m in DISCOUNT_RE.finditer(raw)))
    codes = sorted(set(m.group(1).strip() for m in CODE_RE.finditer(raw)))
    free = sorted(set(m.group(0).strip() for m in FREE_SEED_RE.finditer(raw)))
    ship = sorted(set(m.group(0).strip() for m in SHIPPING_RE.finditer(raw)))
    spend = sorted(set(_clean(m.group(0)) for m in SPEND_RE.finditer(raw)
                       if "shipping" not in m.group(0).lower()))
    ends = sorted(set(_clean(m.group(0)) for m in ENDS_RE.finditer(raw)))
    return {
        "discount_text": "; ".join(discounts)[:500] or None,
        "codes": ", ".join(codes)[:300] or None,
        "free_seeds": "; ".join(free)[:300] or None,
        "shipping": "; ".join(ship)[:300] or None,
        "spend_tiers": "; ".join(spend)[:600] or None,
        "promo_ends": "; ".join(ends)[:300] or None,
    }


def _hash(parsed: dict, raw: str) -> str:
    """Hash the meaningful, parsed content so trivial page noise doesn't
    register as 'change'. Falls back to raw text if nothing parsed."""
    basis = "|".join([
        parsed.get("discount_text") or "",
        parsed.get("codes") or "",
        parsed.get("free_seeds") or "",
        parsed.get("shipping") or "",
        parsed.get("spend_tiers") or "",
        parsed.get("promo_ends") or "",
    ])
    if not basis.strip("|"):
        basis = raw[:2000]
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def scrape_one(name: str, cfg: dict) -> dict:
    """Fetch the primary promo URL plus any extra_urls, merge their visible text,
    and parse the combined content into one daily snapshot.
    Returns (row_for_db, health_status, health_detail)."""
    urls = [cfg["url"]] + list(cfg.get("extra_urls", []))
    texts = []
    fetched = []
    first_error = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36")
        )
        for u in urls:
            try:
                page.goto(u, wait_until="networkidle", timeout=45000)
                page.wait_for_timeout(2500)  # let lazy promo banners settle
                texts.append(page.inner_text("body"))
                fetched.append(u)
            except Exception as e:
                if first_error is None:
                    first_error = f"{u}: {e}"
        browser.close()

    if not texts:
        return None, "error", f"all promo URLs failed (first: {first_error})"

    body = "\n".join(texts)
    raw = _clean(body)
    low = raw.lower()

    # Health check: did we land on real promo pages?
    if len(raw) < 200:
        return None, "warning", f"page text suspiciously short ({len(raw)} chars)"
    if not any(sig.lower() in low for sig in cfg["signals"]):
        return None, "warning", "no expected promo signals found (possible redesign/block)"

    parsed = _extract(raw)
    # headline = first reasonably long line of text
    headline = next((ln for ln in body.splitlines() if len(ln.strip()) > 15), "")[:300]

    # if some (but not all) extra URLs failed, still store but note it
    detail = ""
    if len(fetched) < len(urls):
        missed = len(urls) - len(fetched)
        detail = f"{missed} of {len(urls)} promo pages failed to load"

    row = {
        "competitor": name,
        "url": " | ".join(fetched),
        "headline": _clean(headline) or None,
        **parsed,
        "raw_text": raw[:8000],  # cap stored text
        "content_hash": _hash(parsed, raw),
    }
    return row, "ok", detail


def scrape_all():
    results = []
    for name, cfg in COMPETITORS.items():
        try:
            row, status, detail = scrape_one(name, cfg)
        except Exception as e:
            row, status, detail = None, "error", f"unhandled: {e}"
        results.append((name, row, status, detail))
    return results


# ---- Medium-tier scraping: product launches + prices ------------------------

def _fetch_text_and_links(url, browser):
    """Load a page and return (visible_text, [(anchor_text, href), ...])."""
    page = browser.new_page(
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"))
    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(2500)
        text = page.inner_text("body")
        links = page.eval_on_selector_all(
            "a", "els => els.map(e => [e.innerText.trim(), e.href])")
    finally:
        page.close()
    return text, links


def scrape_new_products(name, cfg):
    """Scrape a competitor's new-arrivals page; return list of product names.
    Heuristic: product links on these pages usually point at /product or
    contain 'seeds' and have a short title. We keep distinct, plausible names."""
    new_url = cfg.get("new_url")
    if not new_url:
        return [], "skip", "no new_url configured"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            text, links = _fetch_text_and_links(new_url, browser)
        except Exception as e:
            browser.close()
            return [], "error", f"new-products fetch failed: {e}"
        browser.close()

    names = []
    seen = set()
    for anchor, href in links:
        a = _clean(anchor)
        if not a or len(a) < 3 or len(a) > 60:
            continue
        h = (href or "").lower()
        looks_product = any(k in h for k in ["/product", "/seeds", "-seeds", "/strain"])
        if not looks_product:
            continue
        # skip nav/category junk
        if any(j in a.lower() for j in
               ["view all", "shop", "category", "filter", "sort", "login",
                "account", "cart", "menu", "home", "next", "previous"]):
            continue
        if a.lower() in seen:
            continue
        seen.add(a.lower())
        names.append(a)
    if not names:
        return [], "warning", "no product names parsed (page layout may have changed)"
    return names[:60], "ok", ""


def scrape_strain_prices(name, cfg):
    """For each tracked strain, find the product on the competitor's site and
    record price + pack size so a per-seed price can be computed.

    Strategy: load the search page, follow the first plausible product link,
    then parse price and pack size from the product page (where both appear
    together). Falls back to the search-results text if no product link found.
    Layout-agnostic by design; logs a warning if nothing parses."""
    base = cfg["url"].split("/", 3)
    origin = "/".join(base[:3]) if len(base) >= 3 else cfg["url"]
    observations = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            for strain in TRACKED_STRAINS:
                q = strain.replace(" ", "+")
                search_url = f"{origin}/search?q={q}"
                try:
                    text, links = _fetch_text_and_links(search_url, browser)
                except Exception:
                    continue

                # Try to follow the first product link whose anchor mentions the strain
                product_url = None
                for anchor, href in links:
                    a = _clean(anchor).lower()
                    h = (href or "").lower()
                    if strain.split()[0].lower() in a and any(
                            k in h for k in ["/product", "/seeds", "-seeds", "/strain"]):
                        product_url = href
                        break

                page_text = text
                src = search_url
                if product_url:
                    try:
                        page_text, _ = _fetch_text_and_links(product_url, browser)
                        src = product_url
                    except Exception:
                        pass

                cur, price, pack = _parse_price_and_pack(page_text)
                if price is None:
                    continue
                in_stock = "out of stock" not in (page_text or "").lower()
                per_seed = round(price / pack, 2) if (pack and pack > 0) else None
                observations.append({
                    "competitor": name, "strain": strain,
                    "product_name": strain, "price": price, "currency": cur,
                    "pack_size": (f"{pack} seeds" if pack else None),
                    "per_seed": per_seed,
                    "in_stock": in_stock, "source_url": src,
                })
        finally:
            browser.close()
    status = "ok" if observations else "warning"
    detail = "" if observations else "no prices parsed for tracked strains"
    return observations, status, detail


# ---- Barney's Farm own special-offers page (weekly Wednesday scrape) --------

SPECIAL_OFFERS_URL = "https://www.barneysfarm.com/us/special-offer-seeds"

# offer phrases we recognize in product cards / labels
OFFER_PATTERNS = [
    (re.compile(r"buy\s*\d+\s*get\s*\d+\s*free", re.I), None),
    (re.compile(r"\bbogo\b", re.I), "Buy 1 Get 1 Free"),
    (re.compile(r"\d{1,2}\s*%\s*off", re.I), None),
    (re.compile(r"double\s*free\s*seeds?", re.I), "Double Free Seeds"),
    (re.compile(r"free\s*seeds?", re.I), "Free Seeds"),
]


def _detect_offer(text):
    t = text or ""
    for pat, label in OFFER_PATTERNS:
        m = pat.search(t)
        if m:
            return label or m.group(0).strip()
    return None


def scrape_special_offers():
    """Scrape the Barney's Farm US special-offers page and extract, per product:
    strain name, the offer (BOGO / % off / free seeds), and price — flagging a
    discount when a struck-through 'was' price is present alongside a lower price.

    Strategy: pull the accessibility tree of product cards. Each card typically
    has a product link (the strain name), price text, and sometimes a sale badge.
    Layout-agnostic: we walk anchor elements that look like products and read the
    surrounding text for offer + price. Logs a warning if nothing parses."""
    offers = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36"))
        try:
            page.goto(SPECIAL_OFFERS_URL, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3000)
            # Try to read product cards via JS: gather blocks that have a product
            # link plus price text. This selector list is broad on purpose.
            cards = page.evaluate(r"""
                () => {
                  const out = [];
                  // candidate product containers
                  const sel = ['.product', '.product-miniature', '.js-product',
                               '[class*=product]', 'article', 'li'];
                  const seen = new Set();
                  for (const s of sel) {
                    document.querySelectorAll(s).forEach(el => {
                      const link = el.querySelector('a[href*="seeds"], a[href*="product"], a[title]');
                      if (!link) return;
                      const name = (link.getAttribute('title') || link.innerText || '').trim();
                      if (!name || name.length < 2 || name.length > 70) return;
                      const txt = (el.innerText || '').trim();
                      if (!txt) return;
                      const key = name.toLowerCase();
                      if (seen.has(key)) return;
                      seen.add(key);
                      out.push({name, text: txt.slice(0, 400)});
                    });
                  }
                  return out;
                }
            """)
            page_text = page.inner_text("body")
        except Exception as e:
            browser.close()
            return [], "error", f"special-offers fetch failed: {e}"
        browser.close()

    # page-level offer (e.g. a sitewide banner) as a fallback offer per product
    page_offer = _detect_offer(page_text)

    for c in cards:
        name = _clean(c.get("name", ""))
        text = c.get("text", "")
        low = name.lower()
        # skip nav/category junk
        if any(j in low for j in ["view all", "shop", "category", "login", "account",
                                  "cart", "menu", "home", "filter", "sort", "sale",
                                  "special offer", "seeds usa", "feminized seeds"]):
            continue
        # need at least one price to count it as a product offer
        prices = [(_m.group(1), _m.group(2)) for _m in PRICE_RE.finditer(text)]
        prices += [(_m.group(2), _m.group(1)) for _m in PRICE_TRAILING_RE.finditer(text)]
        if not prices:
            continue
        # numeric prices
        vals = []
        cur_sym = None
        for sym, num in prices:
            try:
                vals.append(float(num.replace(",", ".")))
                cur_sym = cur_sym or sym
            except ValueError:
                pass
        if not vals:
            continue
        # if two distinct prices, the higher is 'was', lower is current (discount)
        price = min(vals)
        was = max(vals) if len(set(vals)) > 1 else None
        is_disc = was is not None and was > price
        offer = _detect_offer(text) or page_offer
        offers.append({
            "strain": name,
            "offer": offer,
            "price": price,
            "was_price": was,
            "is_discounted": bool(is_disc),
            "currency": cur_sym,
            "source_url": SPECIAL_OFFERS_URL,
        })

    if not offers:
        return [], "warning", "no special offers parsed (page layout may have changed)"
    return offers, "ok", ""
