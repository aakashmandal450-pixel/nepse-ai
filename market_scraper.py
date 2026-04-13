"""
=============================================================
  NEPSE AI TRADER — market_scraper.py
  Modules 1 (Live), 2 (OHLCV), 5 (Indices), 6 (Top Movers)
=============================================================
"""
import logging
from datetime import datetime, date, timedelta
from base_scraper import BaseScraper
from db_manager import db
from settings import NEPSE_API_BASE, NEPALSTOCK_API, HISTORY_YEARS

logger = logging.getLogger(__name__)


class MarketDataScraper(BaseScraper):
    def __init__(self):
        super().__init__("MarketData")

    # ── MODULE 0: Stock List ──────────────────────────────────
    def scrape_stock_list(self) -> int:
        logger.info("📋 Scraping stock list…")
        start = self.now_str()

        data = self.get(f"{NEPSE_API_BASE}/CompanyList")
        if not data:
            data = self.get(f"{NEPALSTOCK_API}/securityList")
        if not data:
            logger.error("Could not fetch stock list from any source")
            db.log_scraper_run("stock_list", "failed", 0, "No data", start, self.now_str())
            return 0

        companies = data if isinstance(data, list) else data.get("data", [])
        records = []
        for c in companies:
            symbol = (c.get("symbol") or c.get("stockSymbol") or "").strip().upper()
            if not symbol:
                continue
            now = self.now_str()
            records.append((
                symbol,
                c.get("companyName") or c.get("name") or "",
                c.get("sectorName") or c.get("sector") or "",
                c.get("subSector") or "",
                c.get("listedShares") or c.get("totalListedShares") or 0,
                c.get("paidUpValue") or 100,
                1, now, now,
            ))

        if records:
            db.executemany("""
                INSERT INTO stocks
                  (symbol, company_name, sector, sub_sector, listed_shares,
                   paid_up_value, is_active, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                    company_name=excluded.company_name,
                    sector=excluded.sector,
                    updated_at=excluded.updated_at
            """, records)

        logger.info(f"✅ Stock list: {len(records)} stocks saved")
        db.log_scraper_run("stock_list", "success", len(records), None, start, self.now_str())
        return len(records)

    # ── MODULE 1: Live Market ─────────────────────────────────
    def scrape_live_market(self) -> int:
        logger.info("📡 Scraping live market…")
        start = self.now_str()
        snap  = datetime.now().strftime("%Y-%m-%d %H:%M")

        data = self.get(f"{NEPSE_API_BASE}/LiveMarket")
        if not data:
            db.log_scraper_run("live_market", "failed", 0, "No data", start, self.now_str())
            return 0

        items = data if isinstance(data, list) else data.get("data", [])
        records = []
        now = self.now_str()
        for s in items:
            sym = (s.get("symbol") or s.get("stockSymbol") or "").strip().upper()
            if not sym:
                continue
            records.append((
                sym,
                s.get("lastTradedPrice") or s.get("ltp") or 0,
                s.get("pointChange") or s.get("change") or 0,
                s.get("percentageChange") or s.get("perChange") or 0,
                s.get("openPrice") or s.get("open") or 0,
                s.get("highPrice") or s.get("high") or 0,
                s.get("lowPrice") or s.get("low") or 0,
                s.get("totalTradeQuantity") or s.get("volume") or 0,
                s.get("previousClose") or s.get("prevClose") or 0,
                s.get("totalTurnover") or s.get("turnover") or 0,
                s.get("fiftyTwoWeekHigh") or 0,
                s.get("fiftyTwoWeekLow") or 0,
                s.get("totalTrades") or 0,
                snap, now,
            ))

        if records:
            db.executemany("""
                INSERT INTO live_market
                  (symbol, ltp, point_change, pct_change, open_price,
                   high_price, low_price, volume, prev_close, turnover,
                   week52_high, week52_low, total_trades, snapshot_time, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, records)

        logger.info(f"✅ Live market: {len(records)} stocks at {snap}")
        db.log_scraper_run("live_market", "success", len(records), None, start, self.now_str())
        return len(records)

    # ── MODULE 2: EOD OHLCV ───────────────────────────────────
    def scrape_today_ohlcv(self) -> int:
        logger.info("📊 Scraping EOD OHLCV…")
        start = self.now_str()
        today = self.today_str()

        data = self.get(f"{NEPSE_API_BASE}/PriceVolume")
        if not data:
            db.log_scraper_run("eod_ohlcv", "failed", 0, "No data", start, self.now_str())
            return 0

        items = data if isinstance(data, list) else data.get("data", [])
        records = []
        now = self.now_str()
        for s in items:
            sym = (s.get("symbol") or s.get("stockSymbol") or "").strip().upper()
            if not sym:
                continue
            close = s.get("closingPrice") or s.get("lastTradedPrice") or s.get("ltp") or 0
            records.append((
                sym, today,
                s.get("openPrice") or s.get("open") or 0,
                s.get("highPrice") or s.get("high") or 0,
                s.get("lowPrice") or s.get("low") or 0,
                close, close,
                s.get("totalTradeQuantity") or s.get("volume") or 0,
                s.get("totalTurnover") or s.get("turnover") or 0,
                s.get("totalTrades") or 0,
                now,
            ))

        if records:
            db.executemany("""
                INSERT INTO daily_ohlcv
                  (symbol, trade_date, open_price, high_price, low_price,
                   close_price, adj_close, volume, turnover, total_trades, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, trade_date) DO UPDATE SET
                    close_price=excluded.close_price,
                    volume=excluded.volume,
                    turnover=excluded.turnover
            """, records)

        logger.info(f"✅ EOD OHLCV: {len(records)} stocks for {today}")
        db.log_scraper_run("eod_ohlcv", "success", len(records), None, start, self.now_str())
        return len(records)

    def scrape_ohlcv_history(self, symbol: str, years: int = HISTORY_YEARS) -> int:
        end_date   = date.today()
        start_date = end_date - timedelta(days=years * 365)
        url = (f"{NEPALSTOCK_API}/graphData/historical"
               f"?symbol={symbol}"
               f"&startDate={start_date.strftime('%Y-%m-%d')}"
               f"&endDate={end_date.strftime('%Y-%m-%d')}")

        data = self.get(url)
        if not data:
            return 0

        candles = data if isinstance(data, list) else data.get("data", [])
        records = []
        now = self.now_str()
        for c in candles:
            try:
                trade_date = (c.get("businessDate") or c.get("date") or "")[:10]
                if not trade_date:
                    continue
                close = float(c.get("closingPrice") or c.get("close") or 0)
                records.append((
                    symbol, trade_date,
                    float(c.get("openPrice") or c.get("open") or 0),
                    float(c.get("highPrice") or c.get("high") or 0),
                    float(c.get("lowPrice") or c.get("low") or 0),
                    close, close,
                    int(c.get("totalTradedQuantity") or c.get("volume") or 0),
                    float(c.get("totalTurnover") or c.get("turnover") or 0),
                    int(c.get("totalTransactions") or c.get("trades") or 0),
                    now,
                ))
            except (ValueError, TypeError):
                continue

        if records:
            db.executemany("""
                INSERT INTO daily_ohlcv
                  (symbol, trade_date, open_price, high_price, low_price,
                   close_price, adj_close, volume, turnover, total_trades, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, trade_date) DO NOTHING
            """, records)

        return len(records)

    def scrape_all_history(self) -> int:
        logger.info(f"🕰️  Pulling {HISTORY_YEARS}-year history for all stocks…")
        start = self.now_str()
        stocks = db.fetchall("SELECT symbol FROM stocks WHERE is_active=1")
        if not stocks:
            self.scrape_stock_list()
            stocks = db.fetchall("SELECT symbol FROM stocks WHERE is_active=1")

        total = 0
        for i, row in enumerate(stocks, 1):
            sym = row["symbol"]
            n = self.scrape_ohlcv_history(sym)
            total += n
            logger.info(f"  [{i}/{len(stocks)}] {sym}: {n} candles")
            self.sleep(0.5)

        logger.info(f"✅ History done: {total} total candles")
        db.log_scraper_run("ohlcv_history", "success", total, None, start, self.now_str())
        return total

    # ── MODULE 5: Indices ─────────────────────────────────────
    def scrape_indices(self) -> int:
        logger.info("📈 Scraping indices…")
        start = self.now_str()
        today = self.today_str()
        snap  = datetime.now().strftime("%H:%M")
        now   = self.now_str()
        records = []

        for url in [f"{NEPSE_API_BASE}/NepseIndex", f"{NEPSE_API_BASE}/SubIndices"]:
            data = self.get(url)
            if not data:
                continue
            items = data if isinstance(data, list) else data.get("data", [data])
            for idx in items:
                name = idx.get("indexName") or idx.get("index") or "NEPSE"
                records.append((
                    name, today,
                    idx.get("openValue") or 0,
                    idx.get("highValue") or 0,
                    idx.get("lowValue") or 0,
                    idx.get("currentValue") or idx.get("closeValue") or 0,
                    idx.get("percentageChange") or 0,
                    idx.get("pointChange") or 0,
                    idx.get("turnover") or 0,
                    snap, now,
                ))
            self.sleep(0.5)

        if records:
            db.executemany("""
                INSERT INTO indices
                  (index_name, index_date, open_value, high_value, low_value,
                   close_value, pct_change, point_change, turnover,
                   snapshot_time, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(index_name, index_date, snapshot_time) DO NOTHING
            """, records)

        logger.info(f"✅ Indices: {len(records)} snapshots")
        db.log_scraper_run("indices", "success", len(records), None, start, self.now_str())
        return len(records)

    # ── MODULE 6: Top Movers ──────────────────────────────────
    def scrape_top_movers(self) -> int:
        logger.info("🏆 Scraping top movers…")
        start = self.now_str()
        today = self.today_str()
        snap  = datetime.now().strftime("%H:%M")

        endpoints = {
            "gainer":       f"{NEPSE_API_BASE}/TopGainers",
            "loser":        f"{NEPSE_API_BASE}/TopLosers",
            "volume":       f"{NEPSE_API_BASE}/TopStocksByVolume",
            "turnover":     f"{NEPSE_API_BASE}/TopStocksByTurnover",
            "transactions": f"{NEPSE_API_BASE}/TopStocksByTransactions",
        }

        records = []
        now = self.now_str()
        for cat, url in endpoints.items():
            data = self.get(url)
            if not data:
                continue
            items = data if isinstance(data, list) else data.get("data", [])
            for rank, item in enumerate(items[:20], 1):
                sym = (item.get("symbol") or item.get("stockSymbol") or "").strip().upper()
                if not sym:
                    continue
                val = (item.get("percentageChange") or item.get("volume")
                       or item.get("turnover") or 0)
                records.append((
                    today, snap, cat, rank, sym,
                    float(val),
                    float(item.get("lastTradedPrice") or item.get("ltp") or 0),
                    float(item.get("percentageChange") or 0),
                    int(item.get("totalTradeQuantity") or item.get("volume") or 0),
                    now,
                ))
            self.sleep(0.5)

        if records:
            db.executemany("""
                INSERT INTO top_movers
                  (trade_date, snapshot_time, category, rank_position,
                   symbol, value, ltp, pct_change, volume, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, records)

        logger.info(f"✅ Top movers: {len(records)} entries")
        db.log_scraper_run("top_movers", "success", len(records), None, start, self.now_str())
        return len(records)
