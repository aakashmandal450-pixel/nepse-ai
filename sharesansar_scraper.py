"""
NEPSE AI — sharesansar_scraper.py
Fetches 5+ years of historical OHLCV data from Sharesansar.com
and writes it to the daily_ohlcv table.

This is a one-time deep backfill tool. After the initial run,
the daily pipeline handles incremental updates via the NEPSE API.

Usage:
    python data_collector.py --history-deep

Works by scraping the price history tables at:
    https://www.sharesansar.com/company/{symbol}
"""
import time
import random
import logging
import requests
from datetime import datetime, date
from bs4 import BeautifulSoup

from db_manager import db

log = logging.getLogger("SharesansarScraper")

BASE_URL    = "https://www.sharesansar.com"
PRICE_URL   = f"{BASE_URL}/company/{{symbol}}"
HISTORY_API = f"{BASE_URL}/ajax/company_price_history"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": BASE_URL,
}

# How many candles we already need before skipping a stock
MIN_CANDLES_TO_SKIP = 600   # ~2.5 years — skip if already well-populated


class SharesansarScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _delay(self, min_s=1.5, max_s=3.5):
        time.sleep(random.uniform(min_s, max_s))

    def _get_company_id(self, symbol: str) -> str | None:
        """
        Load the company page and extract the internal Sharesansar company ID
        needed for the AJAX price history endpoint.
        """
        url = PRICE_URL.format(symbol=symbol.lower())
        try:
            self._delay()
            resp = self.session.get(url, timeout=20)
            if resp.status_code != 200:
                return None
            soup = BeautifulSoup(resp.text, "html.parser")

            # The price history tab has a data attribute with the company id
            # Pattern: data-company_id="123" or similar
            for tag in soup.find_all(attrs={"data-company_id": True}):
                return tag["data-company_id"]

            # Fallback: look for it in script tags
            for script in soup.find_all("script"):
                text = script.string or ""
                if "company_id" in text:
                    import re
                    m = re.search(r'company_id["\s:=]+(\d+)', text)
                    if m:
                        return m.group(1)

            return None
        except Exception as e:
            log.warning(f"{symbol}: failed to get company ID — {e}")
            return None

    def _fetch_history(self, company_id: str, symbol: str) -> list[dict]:
        """
        Fetch all available price history from the Sharesansar AJAX endpoint.
        Returns a list of row dicts with date, open, high, low, close, volume.
        """
        rows = []
        page = 1

        while True:
            try:
                self._delay(0.8, 2.0)
                resp = self.session.post(
                    HISTORY_API,
                    data={
                        "company": company_id,
                        "start": "2010-01-01",
                        "end": date.today().strftime("%Y-%m-%d"),
                        "draw": page,
                        "length": 500,
                        "start_index": (page - 1) * 500,
                    },
                    timeout=20,
                )
                if resp.status_code != 200:
                    break

                data = resp.json()
                items = data.get("data", [])
                if not items:
                    break

                for item in items:
                    # Sharesansar returns date as "2024-01-15" or "15-01-2024"
                    raw_date = (item.get("published_date") or
                                item.get("date") or
                                item.get("d") or "")
                    if not raw_date:
                        continue

                    # Normalise date format to YYYY-MM-DD
                    trade_date = _parse_date(raw_date)
                    if not trade_date:
                        continue

                    rows.append({
                        "trade_date":  trade_date,
                        "open_price":  _f(item.get("open") or item.get("o")),
                        "high_price":  _f(item.get("high") or item.get("h")),
                        "low_price":   _f(item.get("low")  or item.get("l")),
                        "close_price": _f(item.get("close")or item.get("c")),
                        "volume":      _f(item.get("volume")or item.get("v") or 0),
                    })

                # If we got fewer than 500, we've hit the last page
                if len(items) < 500:
                    break
                page += 1

            except Exception as e:
                log.warning(f"{symbol} page {page}: {e}")
                break

        return rows

    def process_symbol(self, symbol: str) -> int:
        """
        Fetch and store history for one symbol.
        Returns number of new rows inserted.
        """
        # Check how many candles we already have
        existing = db.one(
            "SELECT COUNT(*) AS cnt FROM daily_ohlcv WHERE symbol=%s",
            [symbol]
        ) if db._pg else db.one(
            "SELECT COUNT(*) AS cnt FROM daily_ohlcv WHERE symbol=?",
            [symbol]
        )
        count = existing.get("cnt", 0) if existing else 0

        if count >= MIN_CANDLES_TO_SKIP:
            log.info(f"{symbol}: already {count} candles — skipping")
            return 0

        company_id = self._get_company_id(symbol)
        if not company_id:
            log.warning(f"{symbol}: could not find Sharesansar company ID")
            return 0

        rows = self._fetch_history(company_id, symbol)
        if not rows:
            log.warning(f"{symbol}: no data returned from Sharesansar")
            return 0

        now = db.now()
        insert_rows = [
            (
                symbol,
                r["trade_date"],
                r["open_price"],
                r["high_price"],
                r["low_price"],
                r["close_price"],
                r["close_price"],   # adj_close = close until splits handled
                r["volume"],
                None,               # turnover — not available from Sharesansar
                None,               # total_trades — not available
                now,
            )
            for r in rows
            if r["close_price"] and r["trade_date"]
        ]

        if not insert_rows:
            return 0

        db.runmany("""
            INSERT OR IGNORE INTO daily_ohlcv
            (symbol, trade_date, open_price, high_price, low_price,
             close_price, adj_close, volume, turnover, total_trades, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, insert_rows)

        log.info(f"{symbol}: {len(insert_rows)} candles saved "
                 f"(was {count}, now up to {count + len(insert_rows)})")
        return len(insert_rows)

    def run(self):
        """Process all active stocks."""
        log.info("=" * 55)
        log.info("  SHARESANSAR DEEP HISTORY BACKFILL")
        log.info("=" * 55)

        stocks = db.all("SELECT symbol FROM stocks WHERE is_active=1")
        if not stocks:
            log.error("No symbols in stocks table. Run --sync-stocks first.")
            return

        log.info(f"Processing {len(stocks)} symbols...")
        total_new = 0
        skipped   = 0
        failed    = 0

        for i, row in enumerate(stocks, 1):
            sym = row["symbol"]
            try:
                added = self.process_symbol(sym)
                if added == 0:
                    skipped += 1
                else:
                    total_new += added

                if i % 20 == 0:
                    log.info(f"  Progress: {i}/{len(stocks)} | "
                             f"New candles: {total_new:,} | "
                             f"Skipped: {skipped} | Failed: {failed}")
                    time.sleep(3)   # batch pause

            except Exception as e:
                log.error(f"{sym}: crashed — {e}")
                failed += 1
                continue

        log.info(f"\nComplete — {total_new:,} new candles | "
                 f"{skipped} skipped | {failed} failed")


# ── helpers ────────────────────────────────────────────────────

def _f(val) -> float | None:
    """Safe float conversion, returns None on failure."""
    try:
        return float(str(val).replace(",", "")) if val is not None else None
    except (ValueError, TypeError):
        return None


def _parse_date(raw: str) -> str | None:
    """
    Try to parse a date string into YYYY-MM-DD format.
    Handles common Sharesansar formats.
    """
    raw = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d",
                "%B %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None
