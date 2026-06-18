"""
NEPSE AI — data_collector.py
Uses nepse-scraper library for authenticated NEPSE API access.
Provides all functions expected by run_scheduler.py.
"""
import time
import random
import logging
import warnings
from datetime import datetime, date, timedelta

import pytz

warnings.filterwarnings("ignore", message="Unverified HTTPS request")
warnings.filterwarnings("ignore", message="SSL certificate verification")

from nepse_scraper import NepseScraper
from db_manager import DB, db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("Collector")

# ── Timezone & market calendar ────────────────────────────────
_NPT = pytz.timezone("Asia/Kathmandu")
_MARKET_OPEN  = (11, 0)   # 11:00 NPT
_MARKET_CLOSE = (15, 0)   # 15:00 NPT
_TRADING_DAYS = {6, 0, 1, 2, 3}  # Sun=6, Mon=0, Tue=1, Wed=2, Thu=3


def is_market_open() -> bool:
    """Return True if NEPSE is currently open (11:00–15:00 NPT, Sun–Thu)."""
    now = datetime.now(_NPT)
    if now.weekday() not in _TRADING_DAYS:
        return False
    t = (now.hour, now.minute)
    return _MARKET_OPEN <= t < _MARKET_CLOSE


def is_trading_day() -> bool:
    """Return True if today is a NEPSE trading day (Sun–Thu)."""
    now = datetime.now(_NPT)
    return now.weekday() in _TRADING_DAYS


# ══════════════════════════════════════════════════════════════
#  COLLECTOR CLASS — wraps nepse-scraper
# ══════════════════════════════════════════════════════════════

class Collector:
    def __init__(self):
        self.db = db
        log.info("Initializing NepseScraper...")
        self.scraper = NepseScraper(verify_ssl=False)
        # Build symbol→id map once at startup so process_stock() can use it
        # without hitting _resolve_ticker_ids (which has the stockSymbol bug)
        self._symbol_id_map = {}
        log.info("NepseScraper ready [OK]")

    # ── helpers ────────────────────────────────────────────────
    def _force_reauth(self):
        """
        Force the scraper session to discard the cached token and re-authenticate.
        The library's _get_access_token() has an early return if a token exists,
        so we clear it manually to trigger a fresh login.
        """
        try:
            self.scraper.session.access_token = None
            self.scraper.session.token_details = None
            self.scraper.session._market_open_id = None
            self.scraper.session._get_access_token()
            log.info("Token refreshed successfully")
        except Exception as e:
            log.warning(f"Re-auth failed: {e}")

    def _safe(self, func, *args, **kwargs):
        """Call a scraper method with retry + delay. Re-authenticates on 401."""
        for attempt in range(3):
            try:
                time.sleep(random.uniform(0.5, 1.5))
                return func(*args, **kwargs)
            except Exception as e:
                err_str = str(e)
                log.warning(f"Attempt {attempt+1} failed: {e}")
                # 401 = token expired — force re-auth before next attempt
                if "401" in err_str and attempt < 2:
                    log.info("401 detected — refreshing token...")
                    self._force_reauth()
                elif attempt < 2:
                    time.sleep(2 * (attempt + 1))
        return None

    def _build_symbol_id_map(self):
        """
        Build symbol→id map directly from the securities list.
        The library's internal _get_security_map() uses item.get('symbol') but
        the API actually returns 'stockSymbol', causing silent lookup failures.
        We bypass that by calling get_all_securities() ourselves.
        """
        log.info("Building symbol→id map from securities list...")
        secs = self._safe(self.scraper.get_all_securities)
        if not secs:
            log.error("Could not fetch securities list for id map")
            return
        items = secs if isinstance(secs, list) else secs.get("content", [])
        for item in items:
            sym = item.get("stockSymbol") or item.get("symbol")
            sid = item.get("id")
            if sym and sid:
                self._symbol_id_map[sym.upper()] = sid
        log.info(f"Symbol→id map built: {len(self._symbol_id_map)} entries")

    def _get_ticker_history(self, symbol: str, start_date: str, end_date: str):
        """
        Fetch price history bypassing the broken _resolve_ticker_ids().
        Uses our own _symbol_id_map instead. Retries with token refresh on 401.
        """
        if not self._symbol_id_map:
            self._build_symbol_id_map()

        security_id = self._symbol_id_map.get(symbol.upper())
        if not security_id:
            log.warning(f"{symbol}: not found in symbol→id map, skipping")
            return None

        endpoint = self.scraper.endpoints["ticker_price_api"]
        path = f"{endpoint['api']}/{security_id}"
        params = {
            "startDate": start_date,
            "endDate":   end_date,
            "page":      0,
            "size":      500,
        }

        for attempt in range(3):
            try:
                time.sleep(random.uniform(0.3, 0.8))
                response = self.scraper.session.get(path, params=params)
                return response.json()
            except Exception as e:
                err_str = str(e)
                if "401" in err_str:
                    log.info(f"{symbol}: 401 on attempt {attempt+1} — refreshing token...")
                    self._force_reauth()
                else:
                    log.warning(f"{symbol}: history fetch failed: {e}")
                    if attempt < 2:
                        time.sleep(2 * (attempt + 1))
        log.warning(f"{symbol}: all attempts failed, skipping")
        return None

    # ── process one stock (history) ───────────────────────────
    def process_stock(self, symbol: str):
        end   = datetime.today().strftime("%Y-%m-%d")
        start = (datetime.today() - timedelta(days=365 * 3)).strftime("%Y-%m-%d")

        log.info(f"Processing {symbol}")
        data = self._get_ticker_history(symbol, start, end)

        if not data:
            log.warning(f"{symbol} skipped (no data)")
            return

        rows = []
        try:
            # API returns a paginated dict with 'content', or a plain list
            items = data.get("content", []) if isinstance(data, dict) else data
            for item in items:
                trade_date  = item.get("businessDate")
                open_price  = item.get("openPrice")
                high_price  = item.get("highPrice")
                low_price   = item.get("lowPrice")
                close_price = item.get("closePrice")
                volume      = item.get("totalTradedQuantity")
                turnover    = item.get("totalTradedValue")
                trades      = item.get("totalTransactions")

                if not trade_date or close_price is None:
                    continue

                rows.append((
                    symbol,
                    trade_date,
                    open_price,
                    high_price,
                    low_price,
                    close_price,
                    close_price,        # adj_close — same as close until splits handled
                    volume,
                    turnover,
                    trades,
                    db.now(),
                ))

            if rows:
                self.db.runmany("""
                    INSERT OR IGNORE INTO daily_ohlcv
                    (symbol, trade_date, open_price, high_price, low_price,
                     close_price, adj_close, volume, turnover, total_trades, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, rows)
                log.info(f"{symbol} → {len(rows)} candles saved")
            else:
                log.warning(f"{symbol}: parsed 0 valid rows")
        except Exception as e:
            log.error(f"{symbol} parse error: {e}")

    # ── batch run (all stocks) ────────────────────────────────
    def run(self):
        log.info("Collector started — full history run")
        # Build map once before the loop
        self._build_symbol_id_map()

        stocks = self.db.all("SELECT symbol FROM stocks WHERE is_active=1")
        if not stocks:
            log.error("No symbols in stocks table. Run --sync-stocks first.")
            return
        log.info(f"Found {len(stocks)} active symbols")

        ok = err = 0
        for i, s in enumerate(stocks):
            symbol = s["symbol"]
            try:
                self.process_stock(symbol)
                ok += 1
                if (i + 1) % 10 == 0:
                    log.info(f"  [{i+1}/{len(stocks)}] batch pause...")
                    time.sleep(5)
                # Proactively refresh token every 50 stocks (~5 min of requests)
                if (i + 1) % 50 == 0:
                    log.info("Proactive token refresh...")
                    self._force_reauth()
            except Exception as e:
                log.error(f"{symbol} crashed: {e}")
                err += 1
                continue

        log.info(f"History run complete — {ok} OK, {err} failed")


# ══════════════════════════════════════════════════════════════
#  MODULE-LEVEL SINGLETON
# ══════════════════════════════════════════════════════════════
_collector = Collector()


# ══════════════════════════════════════════════════════════════
#  PUBLIC FUNCTIONS (imported by run_scheduler.py)
# ══════════════════════════════════════════════════════════════

def collect_live_market():
    """
    Fetch today's prices for all securities.
    Writes to BOTH daily_ohlcv (for history) AND live_market (for dashboard).
    """
    log.info("collect_live_market: fetching today's prices")
    start = db.now()
    try:
        data = _collector._safe(_collector.scraper.get_today_price)
        if not data:
            log.warning("collect_live_market: no data returned")
            return

        today = datetime.now(_NPT).strftime("%Y-%m-%d")
        snapshot_time = db.now()

        ohlcv_rows      = []
        live_market_rows = []
        top_mover_rows  = []

        for item in data:
            sym         = item.get("stockSymbol") or item.get("symbol")
            bdate       = item.get("businessDate") or today
            open_p      = item.get("openPrice")
            high_p      = item.get("highPrice")
            low_p       = item.get("lowPrice")
            close_p     = item.get("closePrice") or item.get("lastTradedPrice") or item.get("lastUpdatedPrice")
            volume      = item.get("totalTradedQuantity")
            turnover    = item.get("totalTradedValue")
            trades      = item.get("totalTransactions")
            pct_change  = item.get("percentageChange") or item.get("priceChange")
            ltp         = item.get("lastTradedPrice") or close_p

            if not sym:
                continue

            ohlcv_rows.append((
                sym, bdate, open_p, high_p, low_p,
                close_p, close_p,   # adj_close same as close
                volume, turnover, trades,
                snapshot_time,
            ))

            live_market_rows.append((
                sym, ltp, pct_change,
                open_p, high_p, low_p,
                volume, turnover,
                snapshot_time, snapshot_time,
            ))

        # ── Write to daily_ohlcv ──────────────────────────────
        if ohlcv_rows:
            _collector.db.runmany("""
                INSERT OR REPLACE INTO daily_ohlcv
                (symbol, trade_date, open_price, high_price, low_price,
                 close_price, adj_close, volume, turnover, total_trades, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, ohlcv_rows)

        # ── Write to live_market ──────────────────────────────
        if live_market_rows:
            _collector.db.runmany("""
                INSERT OR REPLACE INTO live_market
                (symbol, ltp, pct_change, open_price, high_price, low_price,
                 volume, turnover, snapshot_time, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, live_market_rows)

        # ── Populate top_movers from today's data ─────────────
        _update_top_movers(data, today)

        log.info(f"collect_live_market: {len(ohlcv_rows)} stocks updated")
        db.log("live_market", "success", len(ohlcv_rows), None, start)

    except Exception as e:
        log.error(f"collect_live_market error: {e}")
        db.log("live_market", "error", 0, str(e), start)


def _update_top_movers(data: list, trade_date: str):
    """Derive top gainers, losers, and volume leaders from live price data."""
    try:
        rows = []
        for item in data:
            sym    = item.get("stockSymbol") or item.get("symbol")
            ltp    = item.get("lastTradedPrice") or item.get("closePrice") or 0
            volume = item.get("totalTradedQuantity") or 0
            pct    = item.get("percentageChange") or 0

            if not sym:
                continue

            try:
                pct_f = float(pct)
                vol_f = float(volume)
                ltp_f = float(ltp)
            except (TypeError, ValueError):
                continue

            # gainer / loser / volume — one row per category
            if pct_f > 0:
                rows.append((trade_date, sym, "gainer", pct_f, ltp_f, int(vol_f), pct_f, db.now()))
            elif pct_f < 0:
                rows.append((trade_date, sym, "loser",  pct_f, ltp_f, int(vol_f), pct_f, db.now()))

            rows.append((trade_date, sym, "volume", vol_f, ltp_f, int(vol_f), pct_f, db.now()))

        if rows:
            _collector.db.runmany("""
                INSERT OR REPLACE INTO top_movers
                (trade_date, symbol, category, value, ltp, volume, pct_change, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
    except Exception as e:
        log.warning(f"_update_top_movers error: {e}")


def collect_indices():
    """
    Fetch all NEPSE sub-indices using get_nepse_index() (returns all indices)
    and also get_sector_indices() for sector breakdown.
    Stores index_date so ai_signal_engine can JOIN on it.
    """
    log.info("collect_indices: fetching index data")
    start = db.now()
    try:
        today = datetime.now(_NPT).strftime("%Y-%m-%d")
        rows  = []

        # ── Main NEPSE indices ────────────────────────────────
        data = _collector._safe(_collector.scraper.get_nepse_index)
        if data:
            items = data if isinstance(data, list) else [data]
            for item in items:
                name  = (item.get("index") or item.get("indexName")
                         or item.get("name") or "NEPSE")
                close = item.get("currentValue") or item.get("close") or item.get("value")
                chg   = item.get("change") or item.get("pointChange") or 0
                pct   = item.get("perChange") or item.get("percentageChange") or 0
                rows.append((name, today, close, close, chg, pct, pct, None, db.now()))

        # ── Sector indices ────────────────────────────────────
        sector_data = _collector._safe(_collector.scraper.get_sector_indices)
        if sector_data:
            items = sector_data if isinstance(sector_data, list) else [sector_data]
            for item in items:
                name  = item.get("sectorName") or item.get("index") or item.get("indexName")
                close = item.get("currentValue") or item.get("close")
                chg   = item.get("change") or 0
                pct   = item.get("perChange") or item.get("percentageChange") or 0
                if name:
                    rows.append((name, today, close, close, chg, pct, pct, None, db.now()))

        if rows:
            _collector.db.runmany("""
                INSERT OR REPLACE INTO indices
                (index_name, index_date, close_value, current_value,
                 change, pct_change, per_change, turnover, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
        log.info(f"collect_indices: {len(rows)} index records saved")
        db.log("indices", "success", len(rows), None, start)

    except Exception as e:
        log.error(f"collect_indices error: {e}")
        db.log("indices", "error", 0, str(e), start)


def collect_floorsheet():
    """Fetch today's live trades (floorsheet)."""
    log.info("collect_floorsheet: fetching live trades")
    start = db.now()
    try:
        data = _collector._safe(_collector.scraper.get_live_trades)
        if not data:
            log.warning("collect_floorsheet: no data returned (market may be closed)")
            return

        today = datetime.now(_NPT).strftime("%Y-%m-%d")
        rows  = []
        items = data if isinstance(data, list) else data.get("content", [])

        for item in items:
            rows.append((
                str(item.get("contractNo") or item.get("id") or ""),
                item.get("stockSymbol") or item.get("symbol"),
                str(item.get("buyerMemberId") or item.get("buyerBroker") or ""),
                str(item.get("sellerMemberId") or item.get("sellerBroker") or ""),
                item.get("contractQuantity") or item.get("quantity"),
                item.get("contractRate") or item.get("rate"),
                item.get("contractAmount") or item.get("amount"),
                today,
                db.now(),
            ))

        if rows:
            _collector.db.runmany("""
                INSERT OR IGNORE INTO floorsheet
                (contract_no, symbol, buyer_broker, seller_broker,
                 quantity, rate, amount, trade_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
        log.info(f"collect_floorsheet: {len(rows)} trades saved")
        db.log("floorsheet", "success", len(rows), None, start)

    except Exception as e:
        log.error(f"collect_floorsheet error: {e}")
        db.log("floorsheet", "error", 0, str(e), start)


def compute_broker_analysis():
    """
    Aggregate broker buy/sell stats from today's floorsheet into broker_analysis,
    then compute per-symbol broker concentration scores into broker_concentration.
    """
    log.info("compute_broker_analysis: aggregating broker data")
    start = db.now()
    today = datetime.now(_NPT).strftime("%Y-%m-%d")

    try:
        # ── broker_analysis: daily broker totals ──────────────
        _collector.db.run("""
            INSERT OR REPLACE INTO broker_analysis
                (trade_date, broker_id,
                 total_buy_amount, total_sell_amount,
                 total_buy_qty, total_sell_qty,
                 created_at)
            SELECT
                trade_date,
                buyer_broker        AS broker_id,
                SUM(amount)         AS total_buy_amount,
                0                   AS total_sell_amount,
                SUM(quantity)       AS total_buy_qty,
                0                   AS total_sell_qty,
                ?
            FROM floorsheet
            WHERE trade_date = ?
            GROUP BY buyer_broker
        """, (db.now(), today))

        # Add sell side
        _collector.db.run("""
            INSERT OR REPLACE INTO broker_analysis
                (trade_date, broker_id,
                 total_buy_amount, total_sell_amount,
                 total_buy_qty, total_sell_qty,
                 created_at)
            SELECT
                trade_date,
                seller_broker       AS broker_id,
                0,
                SUM(amount),
                0,
                SUM(quantity),
                ?
            FROM floorsheet
            WHERE trade_date = ?
            GROUP BY seller_broker
        """, (db.now(), today))

        # ── broker_concentration: per-symbol scores ───────────
        _compute_broker_concentration(today)

        log.info("compute_broker_analysis: done")
        db.log("broker_analysis", "success", 0, None, start)

    except Exception as e:
        log.error(f"compute_broker_analysis error: {e}")
        db.log("broker_analysis", "error", 0, str(e), start)


def _compute_broker_concentration(trade_date: str):
    """
    For each symbol traded today, compute:
      - top3_buy_pct: % of buy volume held by top-3 buying brokers
      - top3_sell_pct: same for sell side
      - unique_buyers / unique_sellers
      - avg_trade_size
      - large_trades_pct: % of trades above 10x avg size
      - accumulation_score: net buy pressure (−100 to +100)
      - wyckoff_phase: derived from accumulation_score
    """
    from settings import (WYCKOFF_ACCUM_SCORE, WYCKOFF_MARKUP_SCORE,
                           WYCKOFF_DIST_SCORE, WYCKOFF_DECLINE_SCORE)

    symbols = db.all("""
        SELECT DISTINCT symbol FROM floorsheet WHERE trade_date=?
    """, [trade_date])

    rows = []
    for s in symbols:
        sym = s["symbol"]
        if not sym:
            continue

        trades = db.all("""
            SELECT buyer_broker, seller_broker, quantity, amount
            FROM floorsheet WHERE symbol=? AND trade_date=?
        """, [sym, trade_date])

        if not trades:
            continue

        total_buy_vol  = sum(float(t["quantity"] or 0) for t in trades)
        total_sell_vol = total_buy_vol  # symmetric in floorsheet
        total_trades_n = len(trades)
        avg_size       = total_buy_vol / total_trades_n if total_trades_n else 0
        large_threshold = avg_size * 10

        # Broker buy volumes
        broker_buys  = {}
        broker_sells = {}
        large_count  = 0

        for t in trades:
            qty = float(t["quantity"] or 0)
            bb  = str(t["buyer_broker"] or "")
            sb  = str(t["seller_broker"] or "")
            broker_buys[bb]  = broker_buys.get(bb, 0)  + qty
            broker_sells[sb] = broker_sells.get(sb, 0) + qty
            if qty >= large_threshold:
                large_count += 1

        top3_buy  = sum(sorted(broker_buys.values(),  reverse=True)[:3])
        top3_sell = sum(sorted(broker_sells.values(), reverse=True)[:3])

        top3_buy_pct  = (top3_buy  / total_buy_vol  * 100) if total_buy_vol  else 0
        top3_sell_pct = (top3_sell / total_sell_vol * 100) if total_sell_vol else 0
        unique_buyers  = len(broker_buys)
        unique_sellers = len(broker_sells)
        large_pct      = (large_count / total_trades_n * 100) if total_trades_n else 0

        # Accumulation score: concentrated buying vs concentrated selling
        # Positive = accumulation, Negative = distribution
        accum_score = round((top3_buy_pct - top3_sell_pct) * (unique_sellers / max(unique_buyers, 1)), 2)
        accum_score = max(-100, min(100, accum_score))

        # Wyckoff phase
        if accum_score >= WYCKOFF_MARKUP_SCORE:
            phase = "Mark-up"
        elif accum_score >= WYCKOFF_ACCUM_SCORE:
            phase = "Accumulation"
        elif accum_score <= WYCKOFF_DECLINE_SCORE:
            phase = "Decline"
        elif accum_score <= WYCKOFF_DIST_SCORE:
            phase = "Distribution"
        else:
            phase = "Neutral"

        rows.append((
            sym, trade_date,
            round(top3_buy_pct, 2), round(top3_sell_pct, 2),
            unique_buyers, unique_sellers,
            round(avg_size, 2), round(large_pct, 2),
            accum_score, phase,
            db.now(),
        ))

    if rows:
        db.runmany("""
            INSERT OR REPLACE INTO broker_concentration
            (symbol, trade_date,
             top3_buy_pct, top3_sell_pct,
             unique_buyers, unique_sellers,
             avg_trade_size, large_trades_pct,
             accumulation_score, wyckoff_phase,
             created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        log.info(f"broker_concentration: {len(rows)} symbols updated")


def collect_corporate_actions():
    """Fetch corporate disclosures (dividends, rights, bonuses)."""
    log.info("collect_corporate_actions: fetching disclosures")
    start = db.now()
    try:
        data = _collector._safe(_collector.scraper.get_company_disclosures)
        if not data:
            log.warning("collect_corporate_actions: no data returned")
            return

        # get_company_disclosures returns response.json().get('news', [])
        items = data if isinstance(data, list) else data.get("news", data.get("content", []))
        rows  = []

        for item in items:
            sym         = item.get("symbol") or item.get("companySymbol") or item.get("stockSymbol")
            action_type = item.get("actionType") or item.get("disclosureType") or item.get("category", "")
            action_val  = item.get("actionValue") or item.get("description") or item.get("subject", "")
            book_close  = item.get("bookCloseDate") or item.get("publishedDate", "")
            ex_date     = item.get("exDate") or ""
            ann_date    = item.get("announcementDate") or item.get("publishedDate") or ""
            value       = None
            cash_ps     = None
            try:
                value  = float(item.get("value") or item.get("bonusPercent") or 0) or None
                cash_ps = float(item.get("cashDividend") or item.get("cashPerShare") or 0) or None
            except (TypeError, ValueError):
                pass

            if sym:
                rows.append((
                    sym, action_type, action_val,
                    value, cash_ps,
                    book_close, ex_date, ann_date,
                    db.now(),
                ))

        if rows:
            _collector.db.runmany("""
                INSERT OR IGNORE INTO corporate_actions
                (symbol, action_type, action_value,
                 value, cash_per_share,
                 book_close_date, ex_date, announcement_date,
                 created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
        log.info(f"collect_corporate_actions: {len(rows)} records saved")
        db.log("corporate_actions", "success", len(rows), None, start)

    except Exception as e:
        log.error(f"collect_corporate_actions error: {e}")
        db.log("corporate_actions", "error", 0, str(e), start)


def collect_news():
    """Fetch latest NEPSE notices / news."""
    log.info("collect_news: fetching notices")
    start = db.now()
    try:
        data = _collector._safe(_collector.scraper.get_notices)
        if not data:
            log.warning("collect_news: no data returned")
            return

        # get_notices() returns response.json() directly (a list)
        items = data if isinstance(data, list) else data.get("news", data.get("content", []))
        rows  = []

        for item in items:
            news_id   = str(item.get("newsId") or item.get("id") or "")
            title     = item.get("title") or item.get("subject") or item.get("newsTitle", "")
            headline  = title  # store in both columns for dashboard compatibility
            source    = item.get("source") or item.get("postedBy") or "NEPSE"
            category  = item.get("newsType") or item.get("category") or ""
            content   = item.get("content") or item.get("description") or ""
            news_date = item.get("newsDate") or item.get("publishedDate") or item.get("createdDate", "")
            pub_at    = news_date

            rows.append((
                news_id or None,
                headline,
                title,
                source,
                category,
                content,
                news_date,
                pub_at,
                db.now(),
            ))

        if rows:
            _collector.db.runmany("""
                INSERT OR IGNORE INTO news
                (news_id, headline, title, source, category, content,
                 news_date, published_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
        log.info(f"collect_news: {len(rows)} news items saved")
        db.log("news", "success", len(rows), None, start)

    except Exception as e:
        log.error(f"collect_news error: {e}")
        db.log("news", "error", 0, str(e), start)


# ══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys

    if "--sync-stocks" in sys.argv:
        # ── Sync the stock list from NEPSE into the stocks table ──
        log.info("Syncing stock list from NEPSE...")
        secs = _collector._safe(_collector.scraper.get_all_securities)
        if secs:
            items = secs if isinstance(secs, list) else secs.get("content", [])
            rows  = []
            for s in items:
                # API returns stockSymbol, not symbol
                sym  = s.get("stockSymbol") or s.get("symbol")
                name = s.get("securityName") or s.get("companyName") or ""
                sec  = s.get("sectorName") or s.get("sector") or ""
                if sym:
                    rows.append((sym, name, sec, db.now()))

            if rows:
                _collector.db.runmany("""
                    INSERT INTO stocks (symbol, company_name, sector, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (symbol) DO UPDATE SET
                        company_name = EXCLUDED.company_name,
                        sector       = EXCLUDED.sector,
                        updated_at   = EXCLUDED.created_at
                """, rows)
            log.info(f"Synced {len(rows)} securities")
        else:
            log.error("Could not fetch securities list")

    elif "--live" in sys.argv:
        collect_live_market()
        collect_indices()

    elif "--floorsheet" in sys.argv:
        collect_floorsheet()
        compute_broker_analysis()

    elif "--history" in sys.argv:
        collector = Collector()
        collector._build_symbol_id_map()
        collector.run()

    elif "--news" in sys.argv:
        collect_news()
        collect_corporate_actions()

    else:
        log.info("Usage:")
        log.info("  python data_collector.py --sync-stocks   Sync stock list from NEPSE")
        log.info("  python data_collector.py --live           Fetch live prices + indices")
        log.info("  python data_collector.py --floorsheet     Fetch trades + broker analysis")
        log.info("  python data_collector.py --history        Fetch 3yr price history")
        log.info("  python data_collector.py --news           Fetch news + corporate actions")
