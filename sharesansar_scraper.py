"""
NEPSE AI — sharesansar_scraper.py
Backfills 5+ years of OHLCV data from Sharesansar.

Key insight (from production logs):
  - Sharesansar uses Laravel path-based routing
  - Company history needs a URL *slug* (e.g. "nabil-bank-limited"),
    NOT the numeric company ID or stock symbol
  - The slug is extracted from the company listing page HTML
  - History endpoint: GET /company/{slug}/price-history
    with query params: startDate, endDate, page, size

Usage:
  python sharesansar_scraper.py                           # all stocks
  python sharesansar_scraper.py --symbol NABIL            # single stock (test)
  python sharesansar_scraper.py --symbol NABIL --start-year 2019
"""

import sys
import time
import logging
import random
import re
from datetime import date, timedelta, datetime

import requests
from bs4 import BeautifulSoup

from db_manager import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-22s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("SharesansarScraper")

BASE_URL   = "https://www.sharesansar.com"
PAGE_SIZE  = 500   # max rows per request
DELAY_MIN  = 0.8
DELAY_MAX  = 1.8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         BASE_URL,
}


# ─────────────────────────────────────────────────────────────
#  SLUG DISCOVERY
# ─────────────────────────────────────────────────────────────

def _fetch_company_listing_page(session: requests.Session) -> str:
    """Fetch the Sharesansar company listing page HTML."""
    url = f"{BASE_URL}/listed-companies"
    try:
        r = session.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return r.text
    except Exception as e:
        log.warning(f"Company listing page fetch failed: {e}")
        return ""


def build_slug_map(session: requests.Session) -> dict:
    """
    Extract symbol→slug mapping from the company listing page.
    Sharesansar renders rows like:
        <a href="/company/nabil-bank-limited">NABIL</a>
    or the symbol is in a sibling column and href carries the slug.

    Returns: {"NABIL": "nabil-bank-limited", ...}
    """
    log.info("Building symbol→slug map from company listing page...")
    html = _fetch_company_listing_page(session)
    if not html:
        log.warning("Empty listing page — falling back to symbol-derived slugs")
        return {}

    soup = BeautifulSoup(html, "html.parser")
    slug_map = {}

    # Strategy 1: find all <a href="/company/{slug}"> links
    # and associate them with the nearest stock symbol text
    for a in soup.find_all("a", href=re.compile(r"^/company/[a-z0-9-]+")):
        href = a["href"]  # e.g. "/company/nabil-bank-limited"
        slug = href.split("/company/")[-1].strip("/")
        if not slug:
            continue

        # The symbol is usually the text of the link or a sibling <td>
        link_text = a.get_text(strip=True).upper()

        # If link text looks like a stock symbol (2-10 uppercase chars), use it
        if re.match(r"^[A-Z0-9]{2,12}$", link_text):
            slug_map[link_text] = slug
            continue

        # Otherwise, look for a sibling or parent <td> with a symbol-looking string
        parent_row = a.find_parent("tr")
        if parent_row:
            cells = parent_row.find_all("td")
            for cell in cells:
                text = cell.get_text(strip=True).upper()
                if re.match(r"^[A-Z0-9]{2,12}$", text) and text != link_text:
                    slug_map[text] = slug
                    break

    log.info(f"Slug map built: {len(slug_map)} entries")
    return slug_map


def symbol_to_slug(symbol: str, slug_map: dict) -> str:
    """
    Return the best slug for a symbol.
    Priority:
      1. Exact match from scraped slug_map
      2. Lowercase symbol (many Sharesansar slugs ARE just the symbol lowercased)
      3. Fall through to None so caller can skip
    """
    if symbol in slug_map:
        return slug_map[symbol]

    # Sharesansar slugs are often just the lowercase symbol
    # e.g. NABIL → "nabil", ADBL → "adbl"
    return symbol.lower()


# ─────────────────────────────────────────────────────────────
#  HISTORY FETCH
# ─────────────────────────────────────────────────────────────

# Candidate URL patterns to probe, most-likely first.
# %s will be replaced with the slug.
HISTORY_URL_PATTERNS = [
    "{base}/company/{slug}/price-history",   # nested resource (most common Laravel)
    "{base}/company/{slug}/history",          # alternative nested
    "{base}/company/history/{slug}",          # flattened with slug in path
    "{base}/company/{slug}/stock-price",      # another variant seen on NEPSE sites
]


def _probe_history_url(session: requests.Session, symbol: str, slug: str,
                        start_date: str, end_date: str) -> tuple[str | None, list]:
    """
    Try each URL pattern with a small date window to find the working endpoint.
    Returns (working_url_template, first_page_data) or (None, []).
    """
    # Use a tiny window just to probe (1 month of data)
    probe_end   = end_date
    probe_start = (date.fromisoformat(end_date) - timedelta(days=30)).strftime("%Y-%m-%d")

    for pattern in HISTORY_URL_PATTERNS:
        url = pattern.format(base=BASE_URL, slug=slug)
        params = {
            "startDate": probe_start,
            "endDate":   probe_end,
            "page":      0,
            "size":      10,
        }
        try:
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
            r = session.get(url, headers=HEADERS, params=params, timeout=25)

            log.info(f"{symbol}: probing {url} → HTTP {r.status_code}")

            if r.status_code == 200:
                try:
                    data = r.json()
                    # Valid response: either a list or a dict with 'content' key
                    if isinstance(data, list) and len(data) > 0:
                        return url, data
                    if isinstance(data, dict):
                        content = data.get("content") or data.get("data") or []
                        if content:
                            return url, content
                        # Empty but 200 — endpoint exists, just no data in window
                        # Still count this as working
                        return url, []
                except Exception:
                    # Non-JSON 200 (HTML page) — not the right endpoint
                    log.warning(f"{symbol}: {url} returned non-JSON 200, skipping")
                    continue

            elif r.status_code == 404:
                body_preview = r.text[:200].strip()
                log.warning(f"{symbol}: 404 — {body_preview}")
                continue

            elif r.status_code in (405, 403):
                log.warning(f"{symbol}: HTTP {r.status_code} on {url}")
                continue

        except Exception as e:
            log.warning(f"{symbol}: request error on {url}: {e}")
            continue

    log.warning(f"{symbol}: all URL patterns failed for slug '{slug}'")
    return None, []


def fetch_symbol_history(session: requests.Session, symbol: str, slug: str,
                          start_date: str, end_date: str) -> list[dict]:
    """
    Fetch all pages of price history for one symbol from Sharesansar.
    Returns list of raw row dicts.
    """
    working_url, first_page = _probe_history_url(
        session, symbol, slug, start_date, end_date)

    if working_url is None:
        return []

    all_rows = list(first_page)  # may be empty if probe window had no data

    # Now fetch all pages for the full date range
    page = 0
    while True:
        params = {
            "startDate": start_date,
            "endDate":   end_date,
            "page":      page,
            "size":      PAGE_SIZE,
        }
        try:
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
            r = session.get(working_url, headers=HEADERS, params=params, timeout=30)

            if r.status_code != 200:
                log.warning(f"{symbol}: HTTP {r.status_code} on page {page}, stopping")
                break

            data = r.json()

            if isinstance(data, list):
                rows = data
                has_more = len(rows) == PAGE_SIZE
            elif isinstance(data, dict):
                rows    = data.get("content") or data.get("data") or []
                total   = data.get("totalElements") or data.get("total") or 0
                has_more = (page + 1) * PAGE_SIZE < int(total) if total else (len(rows) == PAGE_SIZE)
            else:
                break

            if not rows:
                break

            all_rows.extend(rows)
            log.info(f"{symbol}: page {page} → {len(rows)} rows (total so far: {len(all_rows)})")

            if not has_more:
                break

            page += 1

        except Exception as e:
            log.error(f"{symbol}: page {page} fetch error: {e}")
            break

    return all_rows


# ─────────────────────────────────────────────────────────────
#  ROW PARSING
# ─────────────────────────────────────────────────────────────

def _parse_float(val) -> float | None:
    """Parse a value to float, handling strings with commas."""
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def parse_history_row(symbol: str, row: dict) -> tuple | None:
    """
    Parse one Sharesansar history row into a DB tuple.
    Handles multiple field name variations Sharesansar may use.
    Returns None if essential fields are missing.
    """
    # Date field — Sharesansar uses various formats
    trade_date = (
        row.get("date") or row.get("businessDate") or
        row.get("tradeDate") or row.get("published_date") or
        row.get("Date")
    )
    if not trade_date:
        return None

    # Normalize date to YYYY-MM-DD
    try:
        if isinstance(trade_date, str):
            # Handle formats like "2019-01-15", "15-01-2019", "Jan 15, 2019"
            for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%Y"):
                try:
                    trade_date = datetime.strptime(trade_date, fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue
    except Exception:
        return None

    open_p  = _parse_float(row.get("open")  or row.get("openPrice")  or row.get("Open"))
    high_p  = _parse_float(row.get("high")  or row.get("highPrice")  or row.get("High"))
    low_p   = _parse_float(row.get("low")   or row.get("lowPrice")   or row.get("Low"))
    close_p = _parse_float(
        row.get("close") or row.get("closePrice") or row.get("Close") or
        row.get("lastTradedPrice") or row.get("ltp")
    )
    volume  = _parse_float(
        row.get("volume") or row.get("totalTradedQuantity") or
        row.get("Volume") or row.get("qty")
    )
    turnover = _parse_float(
        row.get("turnover") or row.get("totalTradedValue") or
        row.get("Turnover") or row.get("amount")
    )
    trades   = _parse_float(
        row.get("totalTransactions") or row.get("transactions") or
        row.get("noOfTransactions") or row.get("trades")
    )

    if close_p is None:
        return None

    # Use close as adj_close (splits not yet handled)
    return (
        symbol,
        trade_date,
        open_p,
        high_p,
        low_p,
        close_p,
        close_p,   # adj_close
        int(volume) if volume is not None else None,
        turnover,
        int(trades) if trades is not None else None,
        db.now(),
    )


# ─────────────────────────────────────────────────────────────
#  DB INSERT
# ─────────────────────────────────────────────────────────────

def insert_ohlcv(rows: list[tuple]) -> int:
    """Insert parsed OHLCV rows into daily_ohlcv, skipping duplicates."""
    if not rows:
        return 0
    try:
        db.runmany("""
            INSERT INTO daily_ohlcv
                (symbol, trade_date, open_price, high_price, low_price,
                 close_price, adj_close, volume, turnover, total_trades, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (symbol, trade_date) DO NOTHING
        """, rows)
        return len(rows)
    except Exception as e:
        log.error(f"DB insert error: {e}")
        return 0


# ─────────────────────────────────────────────────────────────
#  EXISTING DATA CHECK
# ─────────────────────────────────────────────────────────────

def existing_candle_count(symbol: str) -> int:
    """Return how many OHLCV rows we already have for this symbol."""
    r = db.one(
        "SELECT COUNT(*) AS cnt FROM daily_ohlcv WHERE symbol=%s" if db._pg
        else "SELECT COUNT(*) AS cnt FROM daily_ohlcv WHERE symbol=?",
        [symbol]
    )
    return int(r["cnt"]) if r else 0


def earliest_candle_date(symbol: str) -> str | None:
    """Return the earliest trade_date we have for this symbol."""
    r = db.one(
        "SELECT MIN(trade_date) AS d FROM daily_ohlcv WHERE symbol=%s" if db._pg
        else "SELECT MIN(trade_date) AS d FROM daily_ohlcv WHERE symbol=?",
        [symbol]
    )
    return r["d"] if r else None


# ─────────────────────────────────────────────────────────────
#  MAIN SCRAPER LOGIC
# ─────────────────────────────────────────────────────────────

def scrape_symbol(session: requests.Session, symbol: str, slug: str,
                   start_date: str, end_date: str) -> int:
    """
    Fetch and store all history for one symbol.
    Returns number of new rows inserted.
    """
    count = existing_candle_count(symbol)
    log.info(f"{symbol}: {count} candles in DB — fetching from Sharesansar (slug='{slug}')...")

    raw_rows = fetch_symbol_history(session, symbol, slug, start_date, end_date)
    if not raw_rows:
        log.warning(f"{symbol}: no data returned")
        return 0

    parsed = []
    for row in raw_rows:
        t = parse_history_row(symbol, row)
        if t:
            parsed.append(t)

    if not parsed:
        log.warning(f"{symbol}: parsed 0 valid rows from {len(raw_rows)} raw rows")
        return 0

    inserted = insert_ohlcv(parsed)
    log.info(f"{symbol}: {inserted} new candles inserted (from {len(raw_rows)} raw rows)")
    return inserted


def run_backfill(symbols: list[str], start_year: int = 2019):
    """
    Main entry point: backfill history for a list of symbols.
    """
    session    = requests.Session()
    slug_map   = build_slug_map(session)
    start_date = f"{start_year}-01-01"
    end_date   = date.today().strftime("%Y-%m-%d")

    log.info(f"Starting backfill: {len(symbols)} symbols | {start_date} → {end_date}")

    total_inserted = 0
    failed         = []

    for i, symbol in enumerate(symbols, 1):
        slug = symbol_to_slug(symbol, slug_map)
        try:
            n = scrape_symbol(session, symbol, slug, start_date, end_date)
            total_inserted += n
            if n == 0:
                failed.append(symbol)
        except Exception as e:
            log.error(f"{symbol}: unexpected error: {e}")
            failed.append(symbol)

        # Batch pause every 20 symbols
        if i % 20 == 0:
            log.info(f"[{i}/{len(symbols)}] pausing 3s...")
            time.sleep(3)

    log.info("=" * 60)
    log.info(f"Backfill complete: {total_inserted} new candles across {len(symbols)} symbols")
    if failed:
        log.warning(f"Zero-data symbols ({len(failed)}): {', '.join(failed[:30])}")
    log.info("=" * 60)

    return total_inserted


# ─────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sharesansar historical data backfill")
    parser.add_argument("--symbol",     help="Single symbol to process (test mode)")
    parser.add_argument("--start-year", type=int, default=2019,
                        help="Start year for backfill (default: 2019)")
    parser.add_argument("--limit",      type=int, default=0,
                        help="Limit number of symbols (0 = all)")
    args = parser.parse_args()

    if args.symbol:
        # ── Single symbol mode (for testing) ─────────────────
        log.info(f"Single symbol mode: {args.symbol} (from {args.start_year})")
        run_backfill([args.symbol.upper()], start_year=args.start_year)
    else:
        # ── Full backfill mode ────────────────────────────────
        stocks = db.all("SELECT symbol FROM stocks WHERE is_active=1 ORDER BY symbol")
        symbols = [s["symbol"] for s in stocks]
        if args.limit > 0:
            symbols = symbols[: args.limit]
        log.info(f"Full backfill mode: {len(symbols)} symbols")
        run_backfill(symbols, start_year=args.start_year)
