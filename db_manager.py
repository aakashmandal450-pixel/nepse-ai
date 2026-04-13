"""
=============================================================
  NEPSE AI TRADER — db_manager.py
  Database schema + query helpers. Flat import version.
=============================================================
"""
import sqlite3, logging, os
from datetime import datetime
from settings import USE_SQLITE, SQLITE_PATH, DATABASE

logger = logging.getLogger(__name__)

TABLES = [
    ("stocks", """CREATE TABLE IF NOT EXISTS stocks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT UNIQUE NOT NULL,
        company_name TEXT, sector TEXT, sub_sector TEXT,
        listed_shares REAL, paid_up_value REAL, market_cap REAL,
        is_active INTEGER DEFAULT 1, created_at TEXT, updated_at TEXT)"""),

    ("live_market", """CREATE TABLE IF NOT EXISTS live_market (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
        ltp REAL, point_change REAL, pct_change REAL,
        open_price REAL, high_price REAL, low_price REAL, volume INTEGER,
        prev_close REAL, turnover REAL, week52_high REAL, week52_low REAL,
        total_trades INTEGER, snapshot_time TEXT NOT NULL, created_at TEXT)"""),

    ("daily_ohlcv", """CREATE TABLE IF NOT EXISTS daily_ohlcv (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
        trade_date TEXT NOT NULL, open_price REAL, high_price REAL,
        low_price REAL, close_price REAL, adj_close REAL, volume INTEGER,
        turnover REAL, total_trades INTEGER, created_at TEXT,
        UNIQUE(symbol, trade_date))"""),

    ("stock_fundamentals", """CREATE TABLE IF NOT EXISTS stock_fundamentals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
        report_date TEXT NOT NULL, eps REAL, pe_ratio REAL, book_value REAL,
        price_to_book REAL, roe REAL, dividend_yield REAL, market_cap REAL,
        free_float_pct REAL, created_at TEXT, UNIQUE(symbol, report_date))"""),

    ("floorsheet", """CREATE TABLE IF NOT EXISTS floorsheet (
        id INTEGER PRIMARY KEY AUTOINCREMENT, transaction_id TEXT UNIQUE,
        trade_date TEXT NOT NULL, symbol TEXT NOT NULL,
        buyer_broker INTEGER, seller_broker INTEGER,
        quantity INTEGER, rate REAL, amount REAL, trade_time TEXT, created_at TEXT)"""),

    ("brokers", """CREATE TABLE IF NOT EXISTS brokers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, broker_number INTEGER UNIQUE NOT NULL,
        broker_name TEXT, location TEXT, created_at TEXT)"""),

    ("broker_daily_activity", """CREATE TABLE IF NOT EXISTS broker_daily_activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT NOT NULL,
        symbol TEXT NOT NULL, broker_number INTEGER NOT NULL,
        buy_quantity INTEGER DEFAULT 0, buy_amount REAL DEFAULT 0,
        sell_quantity INTEGER DEFAULT 0, sell_amount REAL DEFAULT 0,
        net_quantity INTEGER, net_amount REAL,
        buy_trades INTEGER DEFAULT 0, sell_trades INTEGER DEFAULT 0,
        created_at TEXT, UNIQUE(trade_date, symbol, broker_number))"""),

    ("broker_concentration", """CREATE TABLE IF NOT EXISTS broker_concentration (
        id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT NOT NULL,
        symbol TEXT NOT NULL, top3_buy_pct REAL, top3_sell_pct REAL,
        top_buyer_broker INTEGER, top_buyer_qty INTEGER,
        top_seller_broker INTEGER, top_seller_qty INTEGER,
        unique_buyers INTEGER, unique_sellers INTEGER,
        accumulation_score REAL, created_at TEXT, UNIQUE(trade_date, symbol))"""),

    ("indices", """CREATE TABLE IF NOT EXISTS indices (
        id INTEGER PRIMARY KEY AUTOINCREMENT, index_name TEXT NOT NULL,
        index_date TEXT NOT NULL, open_value REAL, high_value REAL,
        low_value REAL, close_value REAL, pct_change REAL, point_change REAL,
        turnover REAL, snapshot_time TEXT, created_at TEXT,
        UNIQUE(index_name, index_date, snapshot_time))"""),

    ("top_movers", """CREATE TABLE IF NOT EXISTS top_movers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT NOT NULL,
        snapshot_time TEXT NOT NULL, category TEXT NOT NULL, rank_position INTEGER,
        symbol TEXT NOT NULL, value REAL, ltp REAL, pct_change REAL,
        volume INTEGER, created_at TEXT)"""),

    ("corporate_actions", """CREATE TABLE IF NOT EXISTS corporate_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
        action_type TEXT NOT NULL, announcement_date TEXT, ex_date TEXT,
        book_close_date TEXT, record_date TEXT, effective_date TEXT,
        value REAL, cash_per_share REAL, remarks TEXT, created_at TEXT,
        UNIQUE(symbol, action_type, announcement_date))"""),

    ("technical_indicators", """CREATE TABLE IF NOT EXISTS technical_indicators (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
        calc_date TEXT NOT NULL, ema9 REAL, ema21 REAL, ema50 REAL, ema200 REAL,
        macd_line REAL, macd_signal REAL, macd_histogram REAL, adx REAL,
        rsi14 REAL, stoch_k REAL, stoch_d REAL, cci14 REAL, williams_r REAL,
        obv REAL, vwap REAL, volume_ma20 REAL, volume_ratio REAL, cmf REAL,
        atr14 REAL, bb_upper REAL, bb_middle REAL, bb_lower REAL,
        bb_pct_b REAL, bb_squeeze INTEGER,
        pattern_doji INTEGER DEFAULT 0, pattern_hammer INTEGER DEFAULT 0,
        pattern_shooting_star INTEGER DEFAULT 0, pattern_engulf_bull INTEGER DEFAULT 0,
        pattern_engulf_bear INTEGER DEFAULT 0, pattern_morning_star INTEGER DEFAULT 0,
        pattern_evening_star INTEGER DEFAULT 0, signal_strength REAL, created_at TEXT,
        UNIQUE(symbol, calc_date))"""),

    ("news", """CREATE TABLE IF NOT EXISTS news (
        id INTEGER PRIMARY KEY AUTOINCREMENT, headline TEXT NOT NULL,
        summary TEXT, url TEXT UNIQUE, source TEXT, published_at TEXT,
        symbols_mentioned TEXT, category TEXT, sentiment_score REAL,
        sentiment_label TEXT, created_at TEXT)"""),

    ("ai_signals", """CREATE TABLE IF NOT EXISTS ai_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, signal_date TEXT NOT NULL,
        target_date TEXT NOT NULL, symbol TEXT NOT NULL, signal TEXT NOT NULL,
        confidence_pct REAL, predicted_move_pct REAL, stop_loss_price REAL,
        target_price REAL, risk_reward REAL, key_drivers TEXT, model_version TEXT,
        actual_close REAL, actual_pct_change REAL, signal_correct INTEGER,
        created_at TEXT, UNIQUE(signal_date, symbol))"""),

    ("scraper_log", """CREATE TABLE IF NOT EXISTS scraper_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, module TEXT NOT NULL,
        status TEXT NOT NULL, records_saved INTEGER DEFAULT 0,
        error_message TEXT, started_at TEXT, finished_at TEXT,
        duration_sec REAL, created_at TEXT)"""),
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_live       ON live_market(symbol, snapshot_time)",
    "CREATE INDEX IF NOT EXISTS idx_ohlcv      ON daily_ohlcv(symbol, trade_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_floor_sym  ON floorsheet(symbol, trade_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_floor_buy  ON floorsheet(buyer_broker, trade_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_broker_act ON broker_daily_activity(trade_date DESC, symbol)",
    "CREATE INDEX IF NOT EXISTS idx_idx        ON indices(index_name, index_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_movers     ON top_movers(trade_date DESC, category)",
    "CREATE INDEX IF NOT EXISTS idx_corp       ON corporate_actions(symbol, ex_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_news       ON news(published_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_signals    ON ai_signals(signal_date DESC, symbol)",
    "CREATE INDEX IF NOT EXISTS idx_ti         ON technical_indicators(symbol, calc_date DESC)",
]


class DatabaseManager:
    def get_connection(self):
        if USE_SQLITE:
            conn = sqlite3.connect(SQLITE_PATH, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            return conn
        import psycopg2
        return psycopg2.connect(**DATABASE)

    def initialize_database(self):
        logger.info("Initializing database…")
        conn = self.get_connection()
        try:
            cur = conn.cursor()
            for _, sql in TABLES:
                cur.execute(sql)
            for idx in INDEXES:
                cur.execute(idx)
            conn.commit()
            logger.info(f"✅ Database ready — {len(TABLES)} tables")
        finally:
            conn.close()

    def execute(self, sql: str, params=None):
        conn = self.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(sql, params or [])
            conn.commit()
            return cur.rowcount
        except Exception as e:
            conn.rollback()
            logger.error(f"DB error: {e} | SQL: {sql[:120]}")
            raise
        finally:
            conn.close()

    def executemany(self, sql: str, data: list):
        if not data:
            return 0
        conn = self.get_connection()
        try:
            cur = conn.cursor()
            cur.executemany(sql, data)
            conn.commit()
            return cur.rowcount
        except Exception as e:
            conn.rollback()
            logger.error(f"DB executemany error: {e}")
            raise
        finally:
            conn.close()

    def fetchall(self, sql: str, params=None) -> list:
        conn = self.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(sql, params or [])
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def fetchone(self, sql: str, params=None):
        conn = self.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(sql, params or [])
            r = cur.fetchone()
            return dict(r) if r else None
        finally:
            conn.close()

    def log_scraper_run(self, module, status, records=0, error=None,
                        started_at=None, finished_at=None, duration=None):
        try:
            self.execute("""
                INSERT INTO scraper_log
                  (module, status, records_saved, error_message,
                   started_at, finished_at, duration_sec, created_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, [module, status, records, error, started_at, finished_at,
                  duration, datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
        except Exception:
            pass


db = DatabaseManager()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    db.initialize_database()
    r = db.fetchone("SELECT COUNT(*) as c FROM sqlite_master WHERE type='table'")
    print(f"✅ {r['c']} tables created in {SQLITE_PATH}")
