"""
NEPSE AI — merolagani_scraper.py
Backfills 5+ years of OHLCV data from MeroLagani's chart API.

Endpoint (confirmed by multiple open-source NEPSE projects):
  GET https://merolagani.com/handlers/TechnicalChartHandler.ashx
      ?type=get_price_history
      &symbol=NABIL
      &resolution=D          # D = daily candles
      &from=<unix_timestamp>
      &to=<unix_timestamp>

Response (TradingView UDF format):
  {
    "s": "ok",
    "t": [1546300800, ...],   # timestamps
    "o": [900.0, ...],
    "h": [950.0, ...],
    "l": [880.0, ...],
    "c": [920.0, ...],
    "v": [12345, ...]
  }

No authentication required. Symbol is the NEPSE stock symbol directly.

Usage:
  python merolagani_scraper.py --symbol NABIL            # test
  python merolagani_scraper.py --symbol NABIL --start-year 2019
  python merolagani_scraper.py                           # all stocks
  python merolagani_scraper.py --start-year 2019 --limit 10
"""

import sys
import time
import logging
import random
from datetime import date, datetime

import requests

from db_manager import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-22s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("MerolaganiScraper")

BASE_URL  = "https://merolagani.com/handlers/TechnicalChartHandler.ashx"
DELAY_MIN = 0.5
DELAY_MAX = 1.2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://merolagani.com/",
    "Accept":  "application/json, text/plain, */*",
}


# ─────────────────────────────────────────────────────────────
#  FETCH
# ─────────────────────────────────────────────────────────────

def fetch_history(session: requests.Session, symbol: str,
                  start_date: str, end_date: str):
    """
    Fetch daily OHLCV from MeroLagani for one symbol.
    Returns list of (trade_date, open, high, low, close, volume) tuples,
    or [] on failure.
    """
    from_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp())
    to_ts   = int(datetime.strptime(end_date,   "%Y-%m-%d").timestamp())

    params = {
        "type":       "get_price_history",
        "symbol":     symbol,
        "resolution": "D",
        "from":       from_ts,
        "to":         to_ts,
    }

    for attempt in range(3):
        try:
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
            r = session.get(BASE_URL, params=params, headers=HEADERS, timeout=30)
            log.info(f"{symbol}: HTTP {r.status_code}")

            if r.status_code != 200:
                log.warning(f"{symbol}: non-200 on attempt {attempt+1}")
                time.sleep(2 * (attempt + 1))
                continue

            data = r.json()

            if data.get("s") != "ok":
                log.warning(f"{symbol}: API status={data.get('s')} — {data}")
                return []

            timestamps = data.get("t", [])
            opens      = data.get("o", [])
            highs      = data.get("h", [])
            lows       = data.get("l", [])
            closes     = data.get("c", [])
            volumes    = data.get("v", [])

            if not timestamps:
                log.warning(f"{symbol}: empty response (no candles)")
                return []

            rows = []
            for i, ts in enumerate(timestamps):
                trade_date = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                try:
                    row = (
                        trade_date,
                        float(opens[i])   if i < len(opens)   else None,
                        float(highs[i])   if i < len(highs)   else None,
                        float(lows[i])    if i < len(lows)    else None,
                        float(closes[i])  if i < len(closes)  else None,
                        int(volumes[i])   if i < len(volumes)  else None,
                    )
                    if row[4] is not None:  # close required
                        rows.append(row)
                except (ValueError, TypeError):
                    continue

            log.info(f"{symbol}: {len(rows)} candles received")
            return rows

        except Exception as e:
            log.warning(f"{symbol}: attempt {attempt+1} error: {e}")
            time.sleep(2 * (attempt + 1))

    log.error(f"{symbol}: all attempts failed")
    return []


# ─────────────────────────────────────────────────────────────
#  DB INSERT
# ─────────────────────────────────────────────────────────────

def insert_ohlcv(symbol: str, rows: list) -> int:
    """Insert parsed rows into daily_ohlcv, skipping duplicates."""
    if not rows:
        return 0

    db_rows = [
        (
            symbol,
            trade_date,
            open_p, high_p, low_p, close_p, close_p,  # adj_close = close
            volume, None, None,                          # no turnover/trades from ML
            db.now(),
        )
        for trade_date, open_p, high_p, low_p, close_p, volume in rows
    ]

    try:
        db.runmany("""
            INSERT INTO daily_ohlcv
                (symbol, trade_date, open_price, high_price, low_price,
                 close_price, adj_close, volume, turnover, total_trades, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (symbol, trade_date) DO NOTHING
        """, db_rows)
        return len(db_rows)
    except Exception as e:
        log.error(f"{symbol}: DB insert error: {e}")
        return 0


def existing_count(symbol: str) -> int:
    r = db.one(
        "SELECT COUNT(*) AS cnt FROM daily_ohlcv WHERE symbol=%s" if db._pg
        else "SELECT COUNT(*) AS cnt FROM daily_ohlcv WHERE symbol=?",
        [symbol]
    )
    return int(r["cnt"]) if r else 0


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────

def scrape_symbol(session, symbol: str, start_date: str, end_date: str) -> int:
    count = existing_count(symbol)
    log.info(f"{symbol}: {count} candles in DB — fetching from MeroLagani...")
    rows = fetch_history(session, symbol, start_date, end_date)
    if not rows:
        return 0
    inserted = insert_ohlcv(symbol, rows)
    log.info(f"{symbol}: {inserted} new candles inserted")
    return inserted


def run_backfill(symbols: list, start_year: int = 2019):
    session    = requests.Session()
    start_date = f"{start_year}-01-01"
    end_date   = date.today().strftime("%Y-%m-%d")

    log.info(f"Starting backfill: {len(symbols)} symbols | {start_date} → {end_date}")

    total    = 0
    failed   = []

    for i, symbol in enumerate(symbols, 1):
        try:
            n = scrape_symbol(session, symbol, start_date, end_date)
            total += n
            if n == 0:
                failed.append(symbol)
        except Exception as e:
            log.error(f"{symbol}: crashed: {e}")
            failed.append(symbol)

        if i % 50 == 0:
            log.info(f"[{i}/{len(symbols)}] pausing 5s...")
            time.sleep(5)

    log.info("=" * 60)
    log.info(f"Done: {total:,} new candles across {len(symbols)} symbols")
    if failed:
        log.warning(f"Zero-data ({len(failed)}): {', '.join(failed[:30])}")
    log.info("=" * 60)
    return total


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
        log.info(f"Full backfill: {len(symbols)} symbols")
        run_backfill(symbols, start_year=args.start_year)
