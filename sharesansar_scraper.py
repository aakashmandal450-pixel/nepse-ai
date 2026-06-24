"""
NEPSE AI — sharesansar_scraper.py
Fetches 5+ years of historical OHLCV data from Sharesansar.com
and writes it to the daily_ohlcv table.

This is a one-time deep backfill tool. After the initial run,
the daily pipeline handles incremental updates via the NEPSE API.

Usage:
    python sharesansar_scraper.py --symbol NABIL --start-year 2019
    python sharesansar_scraper.py --all --start-year 2019

Works by scraping the price history tables at:
    https://www.sharesansar.com/company/{symbol}
"""
import sys
import time
import random
import logging
import requests
from datetime import datetime, date
from bs4 import BeautifulSoup

from db_manager import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-18s] %(levelname)s: %(message)s",
)
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

# Skip stocks that already have this many candles (skip if well-populated)
MIN_CANDLES_TO_SKIP = 600   # ~2.5 years


class SharesansarScraper:
    def __init__(self, start_year: int = 2019):
        self.session    = requests.Session()
        self.session.headers.update(HEADERS)
        self.start_date = f"{start_year}-01-01"

    def _delay(self, min_s=1.5, max_s=3.5):
        time.sleep(random.uniform(min_s, max_s))

    def _get_company_id(self, symbol: str) -> str | None:
        """
        Load the company page and extract the internal Sharesansar company ID.
        Tries multiple extraction strategies since the page structure varies.
        """
        import re
        url = PRICE_URL.format(symbol=symbol.lower())
        try:
            self._delay()
            resp = self.session.get(url, timeout=20)
            if resp.status_code != 200:
                log.warning(f"{symbol}: company page returned HTTP {resp.status_code}")
                return None

            html  = resp.text
            soup  = BeautifulSoup(html, "html.parser")

            # Strategy 1: data-company_id attribute on any tag
            for tag in soup.find_all(attrs={"data-company_id": True}):
                cid = str(tag["data-company_id"]).strip()
                if cid.isdigit():
                    log.info(f"{symbol}: company_id={cid} (data-company_id attr)")
                    return cid

            # Strategy 2: any script tag containing company_id / cid / companyId
            patterns = [
                r'company_id\s*[=:]\s*["\']?(\d+)',
                r'companyId\s*[=:]\s*["\']?(\d+)',
                r'"cid"\s*:\s*(\d+)',
                r"'cid'\s*:\s*(\d+)",
                r'var\s+cid\s*=\s*(\d+)',
                r'company\s*=\s*(\d+)',
            ]
            for script in soup.find_all("script"):
                text = script.string or ""
                for pat in patterns:
                    m = re.search(pat, text, re.IGNORECASE)
                    if m:
                        cid = m.group(1)
                        log.info(f"{symbol}: company_id={cid} (script pattern: {pat})")
                        return cid

            # Strategy 3: scan ALL raw HTML with the same patterns
            for pat in patterns:
                m = re.search(pat, html, re.IGNORECASE)
                if m:
                    cid = m.group(1)
                    log.info(f"{symbol}: company_id={cid} (raw HTML pattern: {pat})")
                    return cid

            # Strategy 4: any input[type=hidden] with name containing 'company'
            for tag in soup.find_all("input", {"type": "hidden"}):
                name = (tag.get("name") or "").lower()
                if "company" in name or "cid" in name:
                    cid = str(tag.get("value", "")).strip()
                    if cid.isdigit():
                        log.info(f"{symbol}: company_id={cid} (hidden input: {name})")
                        return cid

            # Strategy 5: data-id on any element (last resort)
            for tag in soup.find_all(attrs={"data-id": True}):
                cid = str(tag.get("data-id", "")).strip()
                if cid.isdigit():
                    log.info(f"{symbol}: company_id={cid} (data-id attr)")
                    return cid

            # Nothing worked — log full HTML so we can see what's there
            log.warning(f"{symbol}: could not find company_id — dumping full page (first 5000 chars)")
            log.warning(html[:5000])
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
                        "company":     company_id,
                        "start":       self.start_date,
                        "end":         date.today().strftime("%Y-%m-%d"),
                        "draw":        page,
                        "length":      500,
                        "start_index": (page - 1) * 500,
                    },
                    timeout=20,
                )
                if resp.status_code != 200:
                    log.warning(f"{symbol} page {page}: HTTP {resp.status_code}")
                    break

                try:
                    data = resp.json()
                except Exception:
                    log.warning(f"{symbol} page {page}: response is not JSON — "
                                f"first 500 chars: {resp.text[:500]}")
                    break

                items = data.get("data", [])
                if not items:
                    log.info(f"{symbol}: no items on page {page} — done")
                    break

                log.info(f"{symbol}: page {page} — {len(items)} rows, "
                         f"sample keys: {list(items[0].keys()) if items else []}")

                for item in items:
                    raw_date = (item.get("published_date") or
                                item.get("date") or
                                item.get("d") or "")
                    if not raw_date:
                        continue

                    trade_date = _parse_date(raw_date)
                    if not trade_date:
                        continue

                    rows.append({
                        "trade_date":  trade_date,
                        "open_price":  _f(item.get("open")   or item.get("o")),
                        "high_price":  _f(item.get("high")   or item.get("h")),
                        "low_price":   _f(item.get("low")    or item.get("l")),
                        "close_price": _f(item.get("close")  or item.get("c")),
                        "volume":      _f(item.get("volume") or item.get("v") or 0),
                    })

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
        # Check how many candles we already have — use ? everywhere,
        # db._sql() will convert to %s for postgres automatically
        existing = db.one(
            "SELECT COUNT(*) AS cnt FROM daily_ohlcv WHERE symbol=?",
            [symbol]
        )
        count = existing.get("cnt", 0) if existing else 0

        if count >= MIN_CANDLES_TO_SKIP:
            log.info(f"{symbol}: already {count} candles — skipping")
            return 0

        log.info(f"{symbol}: {count} candles currently — fetching from Sharesansar...")

        company_id = self._get_company_id(symbol)
        if not company_id:
            log.warning(f"{symbol}: could not find Sharesansar company ID — skipping")
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
            log.warning(f"{symbol}: parsed 0 valid rows from {len(rows)} raw items")
            return 0

        # Use INSERT ... ON CONFLICT DO NOTHING (db.runmany handles ? -> %s for PG)
        db.runmany("""
            INSERT INTO daily_ohlcv
            (symbol, trade_date, open_price, high_price, low_price,
             close_price, adj_close, volume, turnover, total_trades, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (symbol, trade_date) DO NOTHING
        """, insert_rows)

        log.info(f"{symbol}: inserted {len(insert_rows)} candles "
                 f"(was {count}, now up to ~{count + len(insert_rows)})")
        return len(insert_rows)

    def run(self):
        """Process all active stocks."""
        log.info("=" * 55)
        log.info("  SHARESANSAR DEEP HISTORY BACKFILL")
        log.info(f"  Start date: {self.start_date}")
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


# ── CLI entry point ────────────────────────────────────────────

if __name__ == "__main__":
    # Parse --start-year
    start_year = 2019
    for arg in sys.argv:
        if arg.startswith("--start-year="):
            start_year = int(arg.split("=")[1])
        elif arg == "--start-year" and sys.argv.index(arg) + 1 < len(sys.argv):
            try:
                start_year = int(sys.argv[sys.argv.index(arg) + 1])
            except (ValueError, IndexError):
                pass

    scraper = SharesansarScraper(start_year=start_year)

    if "--symbol" in sys.argv:
        # Single symbol mode
        idx = sys.argv.index("--symbol")
        if idx + 1 < len(sys.argv):
            symbol = sys.argv[idx + 1].upper()
            log.info(f"Single symbol mode: {symbol} (from {start_year})")
            added = scraper.process_symbol(symbol)
            log.info(f"Done — {added} new candles inserted for {symbol}")
        else:
            log.error("--symbol requires a symbol argument, e.g. --symbol NABIL")
            sys.exit(1)

    elif "--all" in sys.argv:
        # Full run mode
        log.info(f"Full run mode — all active stocks from {start_year}")
        scraper.run()

    else:
        print("Usage:")
        print("  python sharesansar_scraper.py --symbol NABIL --start-year 2019")
        print("  python sharesansar_scraper.py --all --start-year 2019")
        sys.exit(0)
