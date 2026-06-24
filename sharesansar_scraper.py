"""
NEPSE AI — sharesansar_scraper.py
Fetches 5+ years of historical OHLCV data from Sharesansar.com
and writes it to the daily_ohlcv table.

Usage:
    python sharesansar_scraper.py --symbol NABIL --start-year 2019
    python sharesansar_scraper.py --all --start-year 2019
"""
import sys
import re
import json
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

BASE_URL = "https://www.sharesansar.com"

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

MIN_CANDLES_TO_SKIP = 600   # ~2.5 years


class SharesansarScraper:
    def __init__(self, start_year: int = 2019):
        self.session    = requests.Session()
        self.session.headers.update(HEADERS)
        self.start_date = f"{start_year}-01-01"
        self._company_map: dict = {}   # symbol.upper() -> cmpjson id

    def _delay(self, min_s=1.5, max_s=3.5):
        time.sleep(random.uniform(min_s, max_s))

    def _build_company_map(self) -> None:
        """
        Parse var cmpjson from any company page — one request for all symbols.
        cmpjson format: [{"id":16,"symbol":"NABIL","companyname":"..."}, ...]
        """
        url = f"{BASE_URL}/company/nabil"
        try:
            self._delay()
            resp = self.session.get(url, timeout=20)
            if resp.status_code != 200:
                log.warning(f"_build_company_map: HTTP {resp.status_code}")
                return

            m = re.search(r'var\s+cmpjson\s*=\s*(\[.*?\]);', resp.text, re.DOTALL)
            if not m:
                log.warning("_build_company_map: cmpjson not found in page")
                return

            companies = json.loads(m.group(1))
            for entry in companies:
                sym = (entry.get("symbol") or "").strip().upper()
                cid = str(entry.get("id") or "").strip()
                if sym and cid.isdigit():
                    self._company_map[sym] = cid

            log.info(f"Company map built: {len(self._company_map)} entries")

        except Exception as e:
            log.warning(f"_build_company_map error: {e}")

    def _get_company_id(self, symbol: str) -> str | None:
        if not self._company_map:
            self._build_company_map()
        cid = self._company_map.get(symbol.upper())
        if cid:
            log.info(f"{symbol}: company_id={cid}")
            return cid
        log.warning(f"{symbol}: not in company map ({len(self._company_map)} entries)")
        return None

    def _fetch_history(self, company_id: str, symbol: str) -> list:
        """
        Fetch price history from Sharesansar.
        Endpoint: GET /company/history
        Probes two parameter styles on first page, then uses the one that works.
        """
        rows = []
        page = 1
        working_params = None   # will be set after first successful probe

        # Two parameter styles to try on page 1
        param_styles = [
            # Style A: company= (cmpjson numeric id)
            lambda p: {
                "company":     company_id,
                "start":       self.start_date,
                "end":         date.today().strftime("%Y-%m-%d"),
                "draw":        p,
                "length":      500,
                "start_index": (p - 1) * 500,
            },
            # Style B: symbol= (stock ticker)
            lambda p: {
                "symbol":      symbol,
                "start":       self.start_date,
                "end":         date.today().strftime("%Y-%m-%d"),
                "draw":        p,
                "length":      500,
                "start_index": (p - 1) * 500,
            },
            # Style C: id= with symbol
            lambda p: {
                "id":          company_id,
                "symbol":      symbol,
                "start":       self.start_date,
                "end":         date.today().strftime("%Y-%m-%d"),
                "draw":        p,
                "length":      500,
                "start_index": (p - 1) * 500,
            },
        ]

        url = f"{BASE_URL}/company/history"

        while True:
            try:
                self._delay(0.8, 2.0)

                if page == 1 and working_params is None:
                    # Probe each style
                    for style_fn in param_styles:
                        params = style_fn(1)
                        r = self.session.get(url, params=params, timeout=20)
                        log.info(f"{symbol}: probing style {list(params.keys())[:2]} "
                                 f"-> HTTP {r.status_code}")
                        if r.status_code == 200:
                            try:
                                data = r.json()
                                items = data.get("data", [])
                                log.info(f"{symbol}: got {len(items)} items, "
                                         f"keys={list(items[0].keys()) if items else '[]'}")
                                if items is not None:   # even empty list is OK
                                    working_params = style_fn
                                    resp = r
                                    break
                            except Exception as e:
                                log.warning(f"{symbol}: JSON parse failed: {e} — "
                                            f"body: {r.text[:300]}")
                        else:
                            log.warning(f"{symbol}: {r.status_code} — {r.text[:200]}")

                    if working_params is None:
                        log.warning(f"{symbol}: all parameter styles failed")
                        break
                else:
                    params = working_params(page)
                    resp = self.session.get(url, params=params, timeout=20)
                    if resp.status_code != 200:
                        log.warning(f"{symbol} page {page}: HTTP {resp.status_code}")
                        break

                try:
                    data = resp.json()
                except Exception:
                    log.warning(f"{symbol} page {page}: not JSON — {resp.text[:300]}")
                    break

                items = data.get("data", [])
                if not items:
                    log.info(f"{symbol}: no items on page {page} — done ({len(rows)} total)")
                    break

                for item in items:
                    raw_date = (item.get("published_date") or
                                item.get("date") or item.get("d") or "")
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
        existing = db.one(
            "SELECT COUNT(*) AS cnt FROM daily_ohlcv WHERE symbol=?", [symbol])
        count = existing.get("cnt", 0) if existing else 0

        if count >= MIN_CANDLES_TO_SKIP:
            log.info(f"{symbol}: already {count} candles — skipping")
            return 0

        log.info(f"{symbol}: {count} candles — fetching from Sharesansar...")

        company_id = self._get_company_id(symbol)
        if not company_id:
            return 0

        rows = self._fetch_history(company_id, symbol)
        if not rows:
            log.warning(f"{symbol}: no data returned")
            return 0

        now = db.now()
        insert_rows = [
            (symbol, r["trade_date"], r["open_price"], r["high_price"],
             r["low_price"], r["close_price"], r["close_price"],
             r["volume"], None, None, now)
            for r in rows if r["close_price"] and r["trade_date"]
        ]

        if not insert_rows:
            log.warning(f"{symbol}: 0 valid rows from {len(rows)} raw items")
            return 0

        db.runmany("""
            INSERT INTO daily_ohlcv
            (symbol, trade_date, open_price, high_price, low_price,
             close_price, adj_close, volume, turnover, total_trades, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (symbol, trade_date) DO NOTHING
        """, insert_rows)

        log.info(f"{symbol}: inserted {len(insert_rows)} candles")
        return len(insert_rows)

    def run(self) -> None:
        log.info("=" * 55)
        log.info("  SHARESANSAR DEEP HISTORY BACKFILL")
        log.info(f"  Start date: {self.start_date}")
        log.info("=" * 55)

        self._build_company_map()
        if not self._company_map:
            log.error("Could not build company map — aborting")
            return

        stocks = db.all("SELECT symbol FROM stocks WHERE is_active=1")
        if not stocks:
            log.error("No symbols in stocks table. Run --sync-stocks first.")
            return

        log.info(f"Processing {len(stocks)} symbols...")
        total_new = skipped = failed = 0

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
                             f"New: {total_new:,} | Skipped: {skipped} | Failed: {failed}")
                    time.sleep(3)
            except Exception as e:
                log.error(f"{sym}: crashed — {e}")
                failed += 1

        log.info(f"\nComplete — {total_new:,} new candles | "
                 f"{skipped} skipped | {failed} failed")


def _f(val):
    try:
        return float(str(val).replace(",", "")) if val is not None else None
    except (ValueError, TypeError):
        return None


def _parse_date(raw: str):
    raw = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d",
                "%B %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


if __name__ == "__main__":
    start_year = 2019
    for i, arg in enumerate(sys.argv):
        if arg.startswith("--start-year="):
            start_year = int(arg.split("=")[1])
        elif arg == "--start-year" and i + 1 < len(sys.argv):
            try:
                start_year = int(sys.argv[i + 1])
            except (ValueError, IndexError):
                pass

    scraper = SharesansarScraper(start_year=start_year)

    if "--symbol" in sys.argv:
        idx = sys.argv.index("--symbol")
        if idx + 1 < len(sys.argv):
            symbol = sys.argv[idx + 1].upper()
            log.info(f"Single symbol mode: {symbol} (from {start_year})")
            added = scraper.process_symbol(symbol)
            log.info(f"Done — {added} new candles inserted for {symbol}")
        else:
            log.error("--symbol requires an argument, e.g. --symbol NABIL")
            sys.exit(1)

    elif "--all" in sys.argv:
        log.info(f"Full run mode — all active stocks from {start_year}")
        scraper.run()

    else:
        print("Usage:")
        print("  python sharesansar_scraper.py --symbol NABIL --start-year 2019")
        print("  python sharesansar_scraper.py --all --start-year 2019")
        sys.exit(0)
