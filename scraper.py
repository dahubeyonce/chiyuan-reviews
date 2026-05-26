"""
Google Maps Reviews Scraper for 季緣 CHIYUAN brand.

How it works
------------
1. Reads `stores.json` for the list of stores + Google Maps URLs.
2. For each store, opens the Google Maps page in a headless Chromium
   (Playwright), navigates to the /reviews/ URL variant, switches the
   reviews panel to "newest first", scrolls until we have at least
   `MAX_REVIEWS_PER_STORE` reviews loaded, then parses out:
   author, rating (1-5), date (ISO YYYY-MM-DD), text.
3. Writes three files into `data/`:
     - reviews.json         (canonical: list of {store, rating, date, author, text})
     - store_ratings.json   (per-store {store, rating, count})
     - reviews_data.js      (paste-ready JS for index.html dashboard)

Run it
------
    python scraper.py                    # default: 200 reviews / store, headless
    python scraper.py --max 100          # cap reviews per store
    python scraper.py --headed           # show the browser (useful when debugging)
    python scraper.py --store 小巨蛋     # only scrape one store
    python scraper.py --verbose

Known Google Maps quirks (handled here)
---------------------------------------
- Short URLs (maps.app.goo.gl) redirect to long URLs but land on the
  "Overview" tab. We inject `/reviews/` into the path so the Reviews
  tab opens directly without needing to click it.
- `div[data-review-id]` matches BOTH the outer wrapper AND inner content
  of each review (nested DOM), so we dedupe by the id attribute.
- The "撰寫評論 / Write a review" button also matches `aria-label*="評論"`.
  We restrict to `role="tab"` to avoid clicking it.
- Playwright is detected via `navigator.webdriver`, which makes Google
  serve a simplified mobile-ish layout. We mask it via init script.

If a future Google change breaks the scraper, the most likely culprits
are the selectors in `_REVIEW_SELECTORS` and `_extract_review`. See
README "Debug: scraper stopped working" for how to update them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeoutError,
    async_playwright,
)

# Local classifier — see classify.py. Maps each review to NPS 6 領域
# and a promoter/passive/detractor sentiment group.
from classify import classify_topics, sentiment_group, NPS_CATEGORIES_ORDERED

# --------------------------------------------------------------------------
# CONFIG  (tweak here, or override via CLI flags)
# --------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
STORES_PATH = PROJECT_ROOT / "stores.json"
DATA_DIR = PROJECT_ROOT / "data"

DEFAULT_MAX_REVIEWS_PER_STORE = 200
SCROLL_PAUSE_MS = 1200          # how long to wait after each scroll for more reviews to load
SCROLL_NO_PROGRESS_LIMIT = 6    # stop after this many scrolls without new reviews
PAGE_LOAD_TIMEOUT_MS = 45_000
LOCALE = "zh-TW"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Selectors that find the review elements once the reviews panel is open.
# Listed in priority order – the first one to return matches wins.
_REVIEW_SELECTORS = [
    "div[data-review-id]",        # stable when present
    "div.jftiEf",                 # legacy class
]

logger = logging.getLogger("scraper")


# --------------------------------------------------------------------------
# Date parsing
# --------------------------------------------------------------------------
# Google Maps shows dates as relative ("3 個月前" / "3 months ago" / "a year ago")
# We translate these to an absolute YYYY-MM-DD using "today" as the reference.
_REL_DATE_PATTERNS = [
    # English
    (re.compile(r"(\d+|a|an)\s+(minute|hour|day|week|month|year)s?\s+ago", re.I), "en"),
    # Traditional/Simplified Chinese
    (re.compile(r"(\d+|一)\s*(分鐘|小時|天|週|周|個月|年)前"), "zh"),
]

_UNIT_DAYS = {
    "minute": 0, "分鐘": 0,
    "hour": 0, "小時": 0,
    "day": 1, "天": 1,
    "week": 7, "週": 7, "周": 7,
    "month": 30, "個月": 30,
    "year": 365, "年": 365,
}


def parse_relative_date(text: str, today: Optional[date] = None) -> Optional[str]:
    """Convert "3 個月前" or "3 months ago" → ISO date. Returns None if unparseable."""
    if not text:
        return None
    text = text.strip()
    today = today or date.today()

    # Some dates come pre-rendered in YYYY/MM/DD form for older reviews.
    iso_match = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", text)
    if iso_match:
        y, m, d = map(int, iso_match.groups())
        try:
            return date(y, m, d).isoformat()
        except ValueError:
            pass

    for pattern, _lang in _REL_DATE_PATTERNS:
        m = pattern.search(text)
        if m:
            qty_str, unit = m.group(1), m.group(2)
            qty = 1 if qty_str.lower() in ("a", "an", "一") else int(qty_str)
            days = qty * _UNIT_DAYS.get(unit, 0)
            return (today - timedelta(days=days)).isoformat()

    return None


# --------------------------------------------------------------------------
# Scraping primitives
# --------------------------------------------------------------------------
async def _accept_consent(page: Page) -> None:
    """Click consent / cookies banners if Google shows them."""
    for selector in [
        'button[aria-label*="Accept"]',
        'button[aria-label*="同意"]',
        'form[action*="consent"] button',
    ]:
        try:
            btn = await page.query_selector(selector)
            if btn:
                await btn.click()
                await page.wait_for_timeout(500)
                logger.debug("Clicked consent button via %s", selector)
                return
        except Exception:
            continue


async def _open_reviews_tab(page: Page) -> None:
    """Click the 'Reviews' (評論) tab so we get the full review feed.

    Restricted to role="tab" so we don't accidentally click 撰寫評論 (Write a review),
    which also has aria-label containing "評論".
    """
    selectors = [
        'button[role="tab"][aria-label*="評論"]',
        'button[role="tab"][aria-label*="Reviews"]',
        'button[role="tab"][aria-label*="的評論"]',   # "對…的評論" format
    ]
    for selector in selectors:
        try:
            await page.wait_for_selector(selector, timeout=10_000)
            tab = await page.query_selector(selector)
            if tab:
                await tab.click()
                await page.wait_for_timeout(2000)
                logger.debug("Opened reviews tab via %s", selector)
                return
        except Exception:
            continue
    # Fallback: find tab by text (role=tab prevents matching 撰寫評論)
    for text in ["評論", "Reviews"]:
        try:
            btn = page.get_by_role("tab", name=re.compile(text))
            if await btn.count() > 0:
                await btn.first.click()
                await page.wait_for_timeout(2000)
                logger.debug("Opened reviews tab via role=tab text=%s", text)
                return
        except Exception:
            continue
    logger.warning("Could not find Reviews tab — page may already be on reviews view")


async def _sort_by_newest(page: Page) -> None:
    """Click sort dropdown → 'Newest' so we always get recent reviews first."""
    for selector in [
        'button[aria-label*="排序"]',
        'button[aria-label*="Sort"]',
        'button[data-value="Sort"]',
        'button[jsaction*="sortBy"]',
    ]:
        try:
            await page.wait_for_selector(selector, timeout=8_000)
            btn = await page.query_selector(selector)
            if btn:
                await btn.click()
                await page.wait_for_timeout(800)
                # Pick "Newest" / "最新" — usually data-index="1" in the menu
                for option in [
                    '[role="menuitemradio"][data-index="1"]',
                    'div[role="menuitem"]:has-text("最新")',
                    'div[role="menuitem"]:has-text("Newest")',
                ]:
                    try:
                        item = await page.query_selector(option)
                        if item:
                            await item.click()
                            await page.wait_for_timeout(2000)
                            logger.debug("Sorted by newest via %s", option)
                            return
                    except Exception:
                        continue
        except Exception:
            continue
    logger.warning("Could not switch to 'Newest' sort — using default order")


async def _find_feed(page: Page):
    """Locate the scrollable reviews container."""
    selectors = [
        'div[role="feed"]',
        'div.m6QErb.DxyBCb',         # legacy
        'div.review-dialog-list',    # very legacy
    ]
    for selector in selectors:
        try:
            await page.wait_for_selector(selector, timeout=15_000)
            el = await page.query_selector(selector)
            if el:
                logger.debug("Found feed via %s", selector)
                return el
        except Exception:
            continue
    return None


async def _count_reviews(page: Page) -> int:
    """Count unique visible review cards (deduplicated by data-review-id).

    Google Maps nests the same data-review-id on both outer and inner divs,
    so a naive `query_selector_all` returns 2x the real count.
    """
    for sel in _REVIEW_SELECTORS:
        items = await page.query_selector_all(sel)
        if items:
            seen: set[str] = set()
            unique = 0
            for item in items:
                rid = await item.get_attribute("data-review-id") or ""
                if rid not in seen:
                    seen.add(rid)
                    unique += 1
            return unique
    return 0


async def _expand_long_reviews(page: Page) -> None:
    """Click every '更多 / More' button so long reviews are fully captured."""
    for selector in [
        'button[aria-label*="顯示更多"]',
        'button[aria-label*="Show more"]',
        'button.w8nwRe',
    ]:
        buttons = await page.query_selector_all(selector)
        for b in buttons:
            try:
                await b.click(timeout=500)
            except Exception:
                continue
    await page.wait_for_timeout(300)


async def _scroll_feed(page: Page, feed, target: int) -> None:
    """Scroll the feed until we have `target` reviews or progress stalls."""
    seen = 0
    no_progress = 0
    while True:
        count = await _count_reviews(page)
        logger.info("    loaded %d / %d reviews", count, target)
        if count >= target:
            return
        if count == seen:
            no_progress += 1
            if no_progress >= SCROLL_NO_PROGRESS_LIMIT:
                logger.info("    no more reviews available (stopped at %d)", count)
                return
        else:
            no_progress = 0
            seen = count
        # Scroll the feed container by its full height
        await page.evaluate("(el) => el.scrollBy(0, el.scrollHeight)", feed)
        await page.wait_for_timeout(SCROLL_PAUSE_MS)


async def _extract_review(card) -> Optional[dict]:
    """Pull author / rating / date / text out of a single review card."""
    try:
        # Author
        author = ""
        for sel in ["div.d4r55", "button[jsaction*='reviewerLink'] div", "[class*='d4r55']"]:
            el = await card.query_selector(sel)
            if el:
                author = (await el.inner_text()).strip()
                if author:
                    break

        # Rating — read aria-label that contains a star count
        rating = 0
        for sel in [
            "[role='img'][aria-label*='星']",
            "[role='img'][aria-label*='star']",
            "span.kvMYJc[aria-label]",
        ]:
            el = await card.query_selector(sel)
            if el:
                label = (await el.get_attribute("aria-label")) or ""
                m = re.search(r"(\d+(?:\.\d+)?)", label)
                if m:
                    rating = int(round(float(m.group(1))))
                    break

        # Date (relative)
        date_text = ""
        for sel in ["span.rsqaWe", "span.xRkPPb", "[class*='rsqaWe']"]:
            el = await card.query_selector(sel)
            if el:
                date_text = (await el.inner_text()).strip()
                if date_text:
                    break
        iso_date = parse_relative_date(date_text) or ""

        # Body text
        text = ""
        for sel in [
            "span.wiI7pd",
            "div.MyEned span",
            "[data-expandable-section] span",
            "span[jsname]",
        ]:
            el = await card.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if text:
                    break

        if not author and not text and rating == 0:
            return None  # empty card, skip

        return {
            "rating": rating,
            "date": iso_date,
            "author": author,
            "text": text,
        }
    except Exception as e:
        logger.debug("    failed to parse a card: %s", e)
        return None


async def _extract_overall_rating(page: Page) -> tuple[float, int]:
    """Read the big star rating and total review count from the place header."""
    rating, count = 0.0, 0
    for sel in ["div.F7nice span[aria-hidden='true']", "div.fontDisplayLarge"]:
        el = await page.query_selector(sel)
        if el:
            try:
                rating = float((await el.inner_text()).strip().replace(",", "."))
                break
            except ValueError:
                continue
    for sel in [
        "div.F7nice span[aria-label*='評論']",
        "div.F7nice span[aria-label*='review']",
        "button[jsaction*='pane.rating'] span",
    ]:
        el = await page.query_selector(sel)
        if el:
            text = (await el.get_attribute("aria-label")) or (await el.inner_text())
            m = re.search(r"([\d,]+)", text or "")
            if m:
                count = int(m.group(1).replace(",", ""))
                break
    return rating, count


# --------------------------------------------------------------------------
# Per-store driver
# --------------------------------------------------------------------------
def _build_reviews_url(full_url: str) -> str:
    """Insert /reviews/ into a Google Maps place URL so the Reviews tab loads directly."""
    # Pattern: /place/NAME/  →  /place/NAME/reviews/
    modified = re.sub(r'(/place/[^/]+/)', r'\1reviews/', full_url)
    return modified if modified != full_url else full_url


async def scrape_store(
    browser: Browser,
    name: str,
    url: str,
    max_reviews: int,
) -> dict:
    """Returns {'store': name, 'overall_rating': float, 'overall_count': int, 'reviews': [...]}."""
    logger.info("→ %s: opening %s", name, url)
    ctx: BrowserContext = await browser.new_context(
        locale=LOCALE,
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
    )
    # Mask the webdriver flag — Google Maps serves a simplified layout otherwise.
    await ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    page = await ctx.new_page()
    try:
        # Step 1: load the short URL to resolve to the canonical /place/ URL
        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        await page.wait_for_load_state("networkidle", timeout=20_000)
        await page.wait_for_timeout(2000)
        await _accept_consent(page)

        # Step 2: rewrite to the /reviews/ variant so the Reviews tab is the landing
        reviews_url = _build_reviews_url(page.url)
        if reviews_url != page.url:
            logger.debug("Re-navigating to reviews URL: %s", reviews_url[:80])
            await page.goto(reviews_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            await page.wait_for_load_state("networkidle", timeout=20_000)
            await page.wait_for_timeout(2000)

        overall_rating, overall_count = await _extract_overall_rating(page)
        logger.info("    overall: ★%.1f (%d reviews on Google)", overall_rating, overall_count)

        await _open_reviews_tab(page)
        await _sort_by_newest(page)
        feed = await _find_feed(page)
        if not feed:
            logger.error("    could not find reviews feed — aborting this store")
            return {"store": name, "overall_rating": overall_rating, "overall_count": overall_count, "reviews": []}

        await _scroll_feed(page, feed, max_reviews)
        await _expand_long_reviews(page)

        # Parse — deduplicate cards that share the same data-review-id.
        # Google Maps DOM nests the same id on outer and inner divs, so
        # query_selector_all returns ~2x duplicates.
        cards = []
        for sel in _REVIEW_SELECTORS:
            cards = await page.query_selector_all(sel)
            if cards:
                break

        seen_ids: set[str] = set()
        unique_cards = []
        for card in cards:
            rid = await card.get_attribute("data-review-id") or ""
            if rid and rid in seen_ids:
                continue
            seen_ids.add(rid)
            unique_cards.append(card)

        reviews = []
        for card in unique_cards[:max_reviews]:
            r = await _extract_review(card)
            if r:
                r["store"] = name
                # Auto-tag with NPS 6 領域 + sentiment group
                r["topics"] = classify_topics(r.get("text", ""))
                r["sentiment_group"] = sentiment_group(r["rating"])
                reviews.append(r)
        logger.info("    extracted %d reviews", len(reviews))
        return {
            "store": name,
            "overall_rating": overall_rating,
            "overall_count": overall_count,
            "reviews": reviews,
        }
    except PWTimeoutError as e:
        logger.error("    timeout for %s: %s", name, e)
        return {"store": name, "overall_rating": 0.0, "overall_count": 0, "reviews": []}
    finally:
        await ctx.close()


# --------------------------------------------------------------------------
# Output writers
# --------------------------------------------------------------------------
def _write_reviews_json(all_reviews: list[dict]) -> None:
    out = DATA_DIR / "reviews.json"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(all_reviews),
        "classifier_version": "nps-aligned-v1",
        "categories": NPS_CATEGORIES_ORDERED,
        "reviews": all_reviews,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Wrote %s (%d reviews)", out, len(all_reviews))


def _write_store_ratings_json(store_results: list[dict]) -> None:
    out = DATA_DIR / "store_ratings.json"
    rows = [
        {"store": r["store"], "rating": r["overall_rating"], "count": r["overall_count"]}
        for r in store_results
    ]
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Wrote %s", out)


def _write_reviews_js(all_reviews: list[dict], store_results: list[dict]) -> None:
    """Paste-ready JS that mirrors the REVIEWS_DATA / STORE_RATINGS arrays in index.html."""
    out = DATA_DIR / "reviews_data.js"

    def js_str(s: str) -> str:
        return json.dumps(s, ensure_ascii=False)

    lines = ["// Auto-generated by scraper.py — do not edit by hand.",
             f"// Generated: {datetime.now().isoformat(timespec='seconds')}",
             f"// Categories (NPS-aligned): {', '.join(NPS_CATEGORIES_ORDERED)}",
             "const REVIEWS_DATA = ["]
    for r in all_reviews:
        topics_arr = "[" + ",".join(js_str(t) for t in r.get("topics", [])) + "]"
        lines.append(
            "  {{store:{store}, rating:{rating}, date:{date}, author:{author}, text:{text}, "
            "topics:{topics}, sentiment_group:{sg}}},".format(
                store=js_str(r["store"]),
                rating=r["rating"],
                date=js_str(r["date"]),
                author=js_str(r["author"]),
                text=js_str(r["text"]),
                topics=topics_arr,
                sg=js_str(r.get("sentiment_group", "")),
            )
        )
    lines.append("];\n")
    lines.append("const STORE_RATINGS = [")
    for r in store_results:
        lines.append(
            "  {{ store:{store}, rating:{rating}, count:{count} }},".format(
                store=js_str(r["store"]),
                rating=r["overall_rating"],
                count=r["overall_count"],
            )
        )
    lines.append("];\n")
    out.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Wrote %s", out)


def _write_last_updated() -> None:
    (DATA_DIR / "last_updated.txt").write_text(
        datetime.now().isoformat(timespec="seconds"), encoding="utf-8"
    )


# NPS reference numbers from internal NPS questionnaire (1,052 留言, see README).
# Used only to enrich nps_comparison.json with side-by-side reference values.
# If your NPS analysis updates, edit these.
_NPS_REFERENCE = {
    "產品品質": {"promoter_pct": 72.0, "detractor_pct": 3.3},
    "服務體驗": {"promoter_pct": 34.0, "detractor_pct": 2.0},
    "品牌形象": {"promoter_pct": 14.0, "detractor_pct": 0.4},
    "定價價格": {"promoter_pct":  6.0, "detractor_pct": 1.4},
    "營運體驗": {"promoter_pct": 19.0, "detractor_pct": 1.4},
    "行銷吸引": {"promoter_pct":  5.0, "detractor_pct": 0.4},
}


def _write_nps_comparison(all_reviews: list[dict]) -> None:
    """Produce Google-Maps-vs-NPS comparison data for the dashboard."""
    out = DATA_DIR / "nps_comparison.json"
    promoters  = [r for r in all_reviews if r.get("sentiment_group") == "promoter"]
    detractors = [r for r in all_reviews if r.get("sentiment_group") == "detractor"]
    rows = []
    for cat in NPS_CATEGORIES_ORDERED:
        pro_n = sum(1 for r in promoters  if cat in r.get("topics", []))
        det_n = sum(1 for r in detractors if cat in r.get("topics", []))
        gm_pro_pct = round(pro_n / len(promoters)  * 100, 1) if promoters  else 0.0
        gm_det_pct = round(det_n / len(detractors) * 100, 1) if detractors else 0.0
        rows.append({
            "category": cat,
            "gm_promoter_pct":  gm_pro_pct,
            "gm_detractor_pct": gm_det_pct,
            "gm_promoter_n":  pro_n,
            "gm_detractor_n": det_n,
            "nps_promoter_pct":  _NPS_REFERENCE[cat]["promoter_pct"],
            "nps_detractor_pct": _NPS_REFERENCE[cat]["detractor_pct"],
        })
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "gm_sample": {
            "promoter":  len(promoters),
            "detractor": len(detractors),
        },
        "nps_sample_size": 1052,
        "comparison": rows,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Wrote %s", out)


def _write_store_category_breakdown(all_reviews: list[dict]) -> None:
    """Per-store % of promoters mentioning each NPS 領域 — for store-level NPS chart."""
    out = DATA_DIR / "store_category_breakdown.json"
    from collections import defaultdict
    by_store = defaultdict(list)
    for r in all_reviews:
        if r.get("sentiment_group") == "promoter":
            by_store[r["store"]].append(r)
    breakdown = {}
    for store, prs in by_store.items():
        n = len(prs)
        cat_pcts = {}
        for cat in NPS_CATEGORIES_ORDERED:
            cnt = sum(1 for r in prs if cat in r.get("topics", []))
            cat_pcts[cat] = round(cnt / n * 100, 1) if n else 0.0
        breakdown[store] = {"promoter_n": n, "pct_by_category": cat_pcts}
    out.write_text(json.dumps(breakdown, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Wrote %s", out)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
async def main(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    stores_cfg = json.loads(STORES_PATH.read_text(encoding="utf-8"))
    stores = stores_cfg["stores"]
    if args.store:
        stores = [s for s in stores if s["name"] == args.store]
        if not stores:
            logger.error("No store named %r in stores.json", args.store)
            return 1

    pw: Playwright = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=not args.headed)
        store_results = []
        all_reviews = []
        for s in stores:
            res = await scrape_store(browser, s["name"], s["url"], args.max)
            store_results.append(res)
            all_reviews.extend(res["reviews"])
        await browser.close()
    finally:
        await pw.stop()

    _write_reviews_json(all_reviews)
    _write_store_ratings_json(store_results)
    _write_reviews_js(all_reviews, store_results)
    _write_nps_comparison(all_reviews)
    _write_store_category_breakdown(all_reviews)
    _write_last_updated()

    # Summary
    print("\n=== SUMMARY ===")
    for r in store_results:
        print(f"  {r['store']:6s}  ★{r['overall_rating']:.1f}  "
              f"total={r['overall_count']}  scraped={len(r['reviews'])}")
    print(f"  TOTAL scraped reviews: {len(all_reviews)}")
    return 0


def cli() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--max", type=int, default=DEFAULT_MAX_REVIEWS_PER_STORE,
                   help=f"Max reviews to scrape per store (default: {DEFAULT_MAX_REVIEWS_PER_STORE})")
    p.add_argument("--headed", action="store_true",
                   help="Show the browser window (useful for debugging)")
    p.add_argument("--store", type=str, default=None,
                   help="Only scrape one store by name (e.g. --store 小巨蛋)")
    p.add_argument("--verbose", action="store_true", help="Verbose logging")
    return asyncio.run(main(p.parse_args()))


if __name__ == "__main__":
    sys.exit(cli())
