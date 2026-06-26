"""
NEPSE AI — sharesansar_scraper.py
Backfills 5+ years of OHLCV data from Sharesansar.

Key findings (from production log analysis):
  - /company/{slug}/... → HTTP 404, "No query results for model [App\\Model\\Company]"
    (Laravel slug lookup fails — Sharesansar doesn't route by text slug)
  - /company/{id}       → HTTP 403 (route EXISTS, blocked by middleware)
    (Numeric ID IS the correct identifier)
  - The cmpjson JS variable on the main page gives us symbol→numeric_id mapping
  - History endpoint: GET /company/{id}/price-history (or similar numeric-ID patterns)

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
import json
from datetime import date, timedelta, datetime

import requests

from db_manager import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-22s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("SharesansarScraper")

BASE_URL  = "https://www.sharesansar.com"
PAGE_SIZE = 500
DELAY_MIN = 0.8
DELAY_MAX = 1.8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         BASE_URL,
    "X-Requested-With": "XMLHttpRequest",
}

# URL patterns to probe — {id} is the numeric company ID from cmpjson
HISTORY_URL_PATTERNS = [
    "{base}/company/{id}/price-history",   # nested resource, numeric PK
    "{base}/company/{id}/history",
    "{base}/company/history/{id}",
    "{base}/company/{id}/stock-price",
    "{base}/stockdetail/company/{id}",     # sometimes used for stock detail pages
]


# ─────────────────────────────────────────────────────────────
#  COMPANY ID MAP  (symbol → numeric id via cmpjson)
# ─────────────────────────────────────────────────────────────

def build_company_id_map(session: requests.Session) -> dict:
    """
    Fetch the cmpjson JavaScript variable from Sharesansar's main page.
    Returns {"NABIL": 16, "ADBL": 25, ...}
    """
    log.info("Building symbol→id map from Sharesansar cmpjson...")
    try:
        r = session.get(BASE_URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        # Extract: var cmpjson = {...};
        m = re.search(r'var\s+cmpjson\s*=\s*(\{[^;]+\})', r.text, re.DOTALL)
        if not m:
            log.warning("cmpjson not found on main page")
            return {}
        raw = m.group(1).strip().rstrip(';')
        data = json.loads(raw)
        # data is {"NABIL": 16, "ADBL": 25, ...}
        id_map = {sym.upper(): int(cid) for sym, cid in data.items()}
        log.info(f"Company ID map built: {len(id_map)} entries")
        return id_map
    except Exception as e:
        log.warning(f"Failed to build company ID map: {e}")
        return {}


# ─────────────────────────────────────────────────────────────
#  URL PROBING
# ─────────────────────────────────────────────────────────────

def _probe_history_url(session: requests.Session, symbol: str, company_id: int,
                        start_date: str, end_date: str) -> tuple:
    """
    Try each URL pattern to find the working history endpoint.
    Returns (working_url, first_page_data) or (None, []).
    """
    probe_start = (date.fromisoformat(end_date) - timedelta(days=30)).strftime("%Y-%m-%d")

    for pattern in HISTORY_URL_PATTERNS:
        url = pattern.format(base=BASE_URL, id=company_id)
        params = {
            "startDate": probe_start,
            "endDate":   end_date,
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
                    if isinstance(data, list):
                        return url, data
                    if isinstance(data, dict):
                        content = data.get("content") or data.get("data") or []
                        return url, content
                except Exception:
                    log.warning(f"{symbol}: {url} returned non-JSON 200, skipping")
                    continue

            elif r.status_code == 404:
                log.warning(f"{symbol}: 404 — {r.text[:200].strip()}")
            elif r.status_code == 403:
                log.warning(f"{symbol}: 403 on {url} — may need session cookie")
            else:
                log.warning(f"{symbol}: HTTP {r.status_code} on {url}")

        except Exception as e:
            log.warning(f"{symbol}: request error on {url}: {e}")

    # All patterns failed — try the AJAX endpoint as last resort
    return _try_ajax_fallback(session, symbol, company_id, probe_start, end_date)


def _try_ajax_fallback(session: requests.Session, symbol: str, company_id: int,
                        start_date: str, end_date: str) -> tuple:
    """
    Try known Sharesansar AJAX endpoints for price history.
    These are called by their chart JavaScript.
    """
    ajax_urls = [
        f"{BASE_URL}/company/companydetail",
        f"{BASE_URL}/ajax/company_price_history",
        f"{BASE_URL}/company/ajaxpricehistory",
    ]
    for url in ajax_urls:
        try:
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
            # Try as POST with form data
            payload = {
                "id":        company_id,
                "startDate": start_date,
                "endDate":   end_date,
            }
            r = session.post(url, data=payload, headers=HEADERS, timeout=25)
            log.info(f"{symbol}: AJAX POST {url} → HTTP {r.status_code}")
            if r.status_code == 200:
                try:
                    data = r.json()
                    content = (data if isinstance(data, list)
                               else data.get("data") or data.get("content") or [])
                    if content:
                        return url, content
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"{symbol}: AJAX error on {url}: {e}")

    log.warning(f"{symbol}: all URL patterns and AJAX fallbacks failed")
    return None, []


# ─────────────────────────────────────────────────────────────
#  FULL HISTORY FETCH
# ─────────────────────────────────────────────────────────────

def fetch_symbol_history(session: requests.Session, symbol: str, company_id: int,
                          start_date: str, end_date: str) -> list:
    """Fetch all paginated history for one symbol."""
    working_url, first_page = _probe_history_url(
        session, symbol, company_id, start_date, end_date)

    if working_url is None:
        return []

    all_rows = list(first_page)
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
                rows     = data.get("content") or data.get("data") or []
                total    = data.get("totalElements") or data.get("total") or 0
                has_more = (page + 1) * PAGE_SIZE < int(total) if total else len(rows) == PAGE_SIZE
            else:
                break

            if not rows:
                break

            all_rows.extend(rows)
            log.info(f"{symbol}: page {page} → {len(rows)} rows (total: {len(all_rows)})")

            if not has_more:
                break
            page += 1

        except Exception as e:
            log.error(f"{symbol}: page {page} error: {e}")
            break

    return all_rows


# ─────────────────────────────────────────────────────────────
#  ROW PARSING
# ─────────────────────────────────────────────────────────────

def _parse_float(val):
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def parse_history_row(symbol: str, row: dict):
    trade_date = (
        row.get("date") or row.get("businessDate") or
        row.get("tradeDate") or row.get("published_date") or row.get("Date")
    )
    if not trade_date:
        return None

    if isinstance(trade_date, str):
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%Y"):
            try:
                trade_date = datetime.strptime(trade_date, fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue

    open_p   = _parse_float(row.get("open")  or row.get("openPrice")  or row.get("Open"))
    high_p   = _parse_float(row.get("high")  or row.get("highPrice")  or row.get("High"))
    low_p    = _parse_float(row.get("low")   or row.get("lowPrice")   or row.get("Low"))
    close_p  = _parse_float(
        row.get("close") or row.get("closePrice") or row.get("Close") or
        row.get("lastTradedPrice") or row.get("ltp")
    )
    volume   = _parse_float(row.get("volume") or row.get("totalTradedQuantity") or row.get("Volume"))
    turnover = _parse_float(row.get("turnover") or row.get("totalTradedValue") or row.get("Turnover"))
    trades   = _parse_float(row.get("totalTransactions") or row.get("transactions") or row.get("noOfTransactions"))

    if close_p is None:
        return None

    return (
        symbol, trade_date,
        open_p, high_p, low_p, close_p, close_p,
        int(volume) if volume is not None else None,
        turnover,
        int(trades) if trades is not None else None,
        db.now(),
    )


# ─────────────────────────────────────────────────────────────
#  DB INSERT
# ─────────────────────────────────────────────────────────────

def insert_ohlcv(rows: list) -> int:
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


def existing_candle_count(symbol: str) -> int:
    r = db.one(
        "SELECT COUNT(*) AS cnt FROM daily_ohlcv WHERE symbol=%s" if db._pg
        else "SELECT COUNT(*) AS cnt FROM daily_ohlcv WHERE symbol=?",
        [symbol]
    )
    return int(r["cnt"]) if r else 0


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────

def scrape_symbol(session, symbol: str, company_id: int,
                   start_date: str, end_date: str) -> int:
    count = existing_candle_count(symbol)
    log.info(f"{symbol}: {count} candles in DB — fetching (id={company_id})...")

    raw_rows = fetch_symbol_history(session, symbol, company_id, start_date, end_date)
    if not raw_rows:
        log.warning(f"{symbol}: no data returned")
        return 0

    parsed = [t for row in raw_rows for t in [parse_history_row(symbol, row)] if t]
    if not parsed:
        log.warning(f"{symbol}: parsed 0 valid rows from {len(raw_rows)} raw")
        return 0

    inserted = insert_ohlcv(parsed)
    log.info(f"{symbol}: {inserted} new candles inserted")
    return inserted


def run_backfill(symbols: list, start_year: int = 2019):
    session    = requests.Session()
    id_map     = build_company_id_map(session)
    start_date = f"{start_year}-01-01"
    end_date   = date.today().strftime("%Y-%m-%d")

    log.info(f"Starting backfill: {len(symbols)} symbols | {start_date} → {end_date}")

    total_inserted = 0
    failed         = []
    no_id          = []

    for i, symbol in enumerate(symbols, 1):
        company_id = id_map.get(symbol.upper())
        if not company_id:
            log.warning(f"{symbol}: not found in cmpjson map, skipping")
            no_id.append(symbol)
            continue

        try:
            n = scrape_symbol(session, symbol, company_id, start_date, end_date)
            total_inserted += n
            if n == 0:
                failed.append(symbol)
        except Exception as e:
            log.error(f"{symbol}: unexpected error: {e}")
            failed.append(symbol)

        if i % 20 == 0:
            log.info(f"[{i}/{len(symbols)}] pausing 3s...")
            time.sleep(3)

    log.info("=" * 60)
    log.info(f"Backfill complete: {total_inserted} new candles")
    if no_id:
        log.warning(f"No ID found ({len(no_id)}): {', '.join(no_id[:20])}")
    if failed:
        log.warning(f"Zero-data ({len(failed)}): {', '.join(failed[:20])}")
    log.info("=" * 60)
    return total_inserted


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol",     help="Single symbol (test mode)")
    parser.add_argument("--start-year", type=int, default=2019)
    parser.add_argument("--limit",      type=int, default=0)
    args = parser.parse_args()

    if args.symbol:
        log.info(f"Single symbol mode: {args.symbol} (from {args.start_year})")
        run_backfill([args.symbol.upper()], start_year=args.start_year)
    else:
        stocks  = db.all("SELECT symbol FROM stocks WHERE is_active=1 ORDER BY symbol")
        symbols = [s["symbol"] for s in stocks]
        if args.limit > 0:
            symbols = symbols[:args.limit]
        log.info(f"Full backfill mode: {len(symbols)} symbols")
        run_backfill(symbols, start_year=args.start_year)
