"""
NEPSE AI — db_manager.py
Supports both PostgreSQL (Supabase) and SQLite.
  - Set DATABASE_URL env variable → uses PostgreSQL (Supabase/production).
  - No DATABASE_URL → falls back to SQLite (local development).
"""
import os, logging, re
from datetime import datetime

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES  = bool(DATABASE_URL)

if not USE_POSTGRES:
    try:
        from settings import DB_PATH
    except ImportError:
        DB_PATH = "nepse_ai.db"


def _to_pg(sql: str) -> str:
    """Convert SQLite SQL syntax to PostgreSQL."""
    sql = sql.replace("?", "%s")
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    sql = re.sub(r'\bREAL\b', "DOUBLE PRECISION", sql)
    # For INSERT OR IGNORE: convert to INSERT ... ON CONFLICT DO NOTHING
    sql = re.sub(r'INSERT OR IGNORE INTO\s+(\w+)',
                 lambda m: f'INSERT INTO {m.group(1)}', sql)
    if re.search(r'INSERT OR IGNORE', sql, re.IGNORECASE):
        sql = re.sub(r'INSERT OR IGNORE', 'INSERT', sql, flags=re.IGNORECASE)
        if 'ON CONFLICT' not in sql.upper():
            sql = sql.rstrip().rstrip(';') + ' ON CONFLICT DO NOTHING'
    sql = re.sub(r'INSERT OR REPLACE INTO', "INSERT INTO", sql)
    return sql


PG_TABLES = [
    ("stocks", """CREATE TABLE IF NOT EXISTS stocks (
        id SERIAL PRIMARY KEY, symbol TEXT UNIQUE NOT NULL,
        company_name TEXT, sector TEXT, sub_sector TEXT,
        listed_shares DOUBLE PRECISION, public_shares DOUBLE PRECISION,
        promoter_shares DOUBLE PRECISION, free_float_pct DOUBLE PRECISION,
        paid_up_value DOUBLE PRECISION DEFAULT 100, market_cap DOUBLE PRECISION,
        is_active INTEGER DEFAULT 1, created_at TEXT, updated_at TEXT)"""),

    ("daily_ohlcv", """CREATE TABLE IF NOT EXISTS daily_ohlcv (
        id SERIAL PRIMARY KEY, symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
        open_price DOUBLE PRECISION, high_price DOUBLE PRECISION,
        low_price DOUBLE PRECISION, close_price DOUBLE PRECISION,
        adj_close DOUBLE PRECISION, volume BIGINT, turnover DOUBLE PRECISION,
        total_trades INTEGER, created_at TEXT, UNIQUE(symbol, trade_date))"""),

    ("live_market", """CREATE TABLE IF NOT EXISTS live_market (
        id SERIAL PRIMARY KEY, symbol TEXT NOT NULL,
        ltp DOUBLE PRECISION, pct_change DOUBLE PRECISION,
        open_price DOUBLE PRECISION, high_price DOUBLE PRECISION,
        low_price DOUBLE PRECISION, volume BIGINT, turnover DOUBLE PRECISION,
        snapshot_time TEXT NOT NULL, created_at TEXT,
        UNIQUE(symbol, snapshot_time))"""),

    ("indices", """CREATE TABLE IF NOT EXISTS indices (
        id SERIAL PRIMARY KEY, index_name TEXT NOT NULL, index_date TEXT,
        close_value DOUBLE PRECISION, current_value DOUBLE PRECISION,
        change DOUBLE PRECISION, pct_change DOUBLE PRECISION,
        per_change DOUBLE PRECISION, turnover DOUBLE PRECISION,
        created_at TEXT, UNIQUE(index_name, index_date))"""),

    ("floorsheet", """CREATE TABLE IF NOT EXISTS floorsheet (
        id SERIAL PRIMARY KEY, contract_no TEXT UNIQUE, symbol TEXT,
        buyer_broker TEXT, seller_broker TEXT, quantity DOUBLE PRECISION,
        rate DOUBLE PRECISION, amount DOUBLE PRECISION,
        trade_date TEXT, created_at TEXT)"""),

    ("broker_analysis", """CREATE TABLE IF NOT EXISTS broker_analysis (
        id SERIAL PRIMARY KEY, trade_date TEXT NOT NULL, broker_id TEXT NOT NULL,
        total_buy_amount DOUBLE PRECISION DEFAULT 0,
        total_sell_amount DOUBLE PRECISION DEFAULT 0,
        total_buy_qty DOUBLE PRECISION DEFAULT 0,
        total_sell_qty DOUBLE PRECISION DEFAULT 0,
        created_at TEXT, UNIQUE(trade_date, broker_id))"""),

    ("broker_concentration", """CREATE TABLE IF NOT EXISTS broker_concentration (
        id SERIAL PRIMARY KEY, symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
        top3_buy_pct DOUBLE PRECISION, top3_sell_pct DOUBLE PRECISION,
        unique_buyers INTEGER, unique_sellers INTEGER,
        avg_trade_size DOUBLE PRECISION, large_trades_pct DOUBLE PRECISION,
        accumulation_score DOUBLE PRECISION, wyckoff_phase TEXT,
        created_at TEXT, UNIQUE(symbol, trade_date))"""),

    ("technical_indicators", """CREATE TABLE IF NOT EXISTS technical_indicators (
        id SERIAL PRIMARY KEY, symbol TEXT NOT NULL, calc_date TEXT NOT NULL,
        ema9 DOUBLE PRECISION, ema21 DOUBLE PRECISION,
        ema50 DOUBLE PRECISION, ema200 DOUBLE PRECISION,
        macd_line DOUBLE PRECISION, macd_signal DOUBLE PRECISION,
        macd_histogram DOUBLE PRECISION, adx DOUBLE PRECISION,
        adx_plus_di DOUBLE PRECISION, adx_minus_di DOUBLE PRECISION,
        rsi14 DOUBLE PRECISION, stoch_k DOUBLE PRECISION, stoch_d DOUBLE PRECISION,
        cci14 DOUBLE PRECISION, williams_r DOUBLE PRECISION, roc10 DOUBLE PRECISION,
        obv DOUBLE PRECISION, vwap DOUBLE PRECISION,
        volume_ma20 DOUBLE PRECISION, volume_ratio DOUBLE PRECISION,
        cmf DOUBLE PRECISION, mfi14 DOUBLE PRECISION,
        atr14 DOUBLE PRECISION, atr_pct DOUBLE PRECISION,
        bb_upper DOUBLE PRECISION, bb_middle DOUBLE PRECISION,
        bb_lower DOUBLE PRECISION, bb_pct_b DOUBLE PRECISION,
        bb_bandwidth DOUBLE PRECISION, bb_squeeze INTEGER DEFAULT 0,
        float_adj_volume DOUBLE PRECISION, relative_strength DOUBLE PRECISION,
        dist_from_52w_high DOUBLE PRECISION, dist_from_52w_low DOUBLE PRECISION,
        pattern_doji INTEGER DEFAULT 0, pattern_hammer INTEGER DEFAULT 0,
        pattern_engulf_bull INTEGER DEFAULT 0, pattern_engulf_bear INTEGER DEFAULT 0,
        pattern_morning_star INTEGER DEFAULT 0, pattern_evening_star INTEGER DEFAULT 0,
        pattern_shooting_star INTEGER DEFAULT 0,
        signal_strength DOUBLE PRECISION, created_at TEXT,
        UNIQUE(symbol, calc_date))"""),

    ("fundamentals", """CREATE TABLE IF NOT EXISTS fundamentals (
        id SERIAL PRIMARY KEY, symbol TEXT NOT NULL, report_date TEXT NOT NULL,
        eps DOUBLE PRECISION, pe_ratio DOUBLE PRECISION,
        book_value DOUBLE PRECISION, price_to_book DOUBLE PRECISION,
        roe DOUBLE PRECISION, dividend_yield DOUBLE PRECISION,
        created_at TEXT, UNIQUE(symbol, report_date))"""),

    ("corporate_actions", """CREATE TABLE IF NOT EXISTS corporate_actions (
        id SERIAL PRIMARY KEY, symbol TEXT NOT NULL,
        action_type TEXT, action_value TEXT,
        value DOUBLE PRECISION, cash_per_share DOUBLE PRECISION,
        book_close_date TEXT, ex_date TEXT, announcement_date TEXT,
        created_at TEXT)"""),

    ("news", """CREATE TABLE IF NOT EXISTS news (
        id SERIAL PRIMARY KEY, news_id TEXT UNIQUE,
        headline TEXT, title TEXT, source TEXT, category TEXT,
        content TEXT, news_date TEXT, published_at TEXT,
        sentiment_score DOUBLE PRECISION DEFAULT 0,
        sentiment_label TEXT DEFAULT 'neutral', created_at TEXT)"""),

    ("ai_signals", """CREATE TABLE IF NOT EXISTS ai_signals (
        id SERIAL PRIMARY KEY, signal_date TEXT NOT NULL,
        target_date TEXT, symbol TEXT NOT NULL, signal TEXT,
        confidence_pct DOUBLE PRECISION, ltp DOUBLE PRECISION,
        target_10d DOUBLE PRECISION, target_20d DOUBLE PRECISION,
        stop_loss DOUBLE PRECISION, risk_reward DOUBLE PRECISION,
        upside_prob_10d DOUBLE PRECISION, downside_prob_10d DOUBLE PRECISION,
        strategy TEXT, wyckoff_phase TEXT, time_series_type TEXT,
        rsi_at_signal DOUBLE PRECISION, macd_at_signal DOUBLE PRECISION,
        volume_ratio_at_signal DOUBLE PRECISION, accumulation_score DOUBLE PRECISION,
        beta_3m DOUBLE PRECISION, pe_ratio DOUBLE PRECISION, atr_pct DOUBLE PRECISION,
        key_drivers TEXT, model_version TEXT,
        actual_close_10d DOUBLE PRECISION, actual_pct_10d DOUBLE PRECISION,
        signal_correct INTEGER, created_at TEXT,
        UNIQUE(signal_date, symbol))"""),

    ("price_projections", """CREATE TABLE IF NOT EXISTS price_projections (
        id SERIAL PRIMARY KEY, symbol TEXT NOT NULL,
        projection_date TEXT NOT NULL, horizon_days INTEGER NOT NULL,
        current_price DOUBLE PRECISION, target_price DOUBLE PRECISION,
        upside_price DOUBLE PRECISION, downside_price DOUBLE PRECISION,
        probability_up DOUBLE PRECISION, probability_down DOUBLE PRECISION,
        atr_based DOUBLE PRECISION, method TEXT, created_at TEXT,
        UNIQUE(symbol, projection_date, horizon_days))"""),

    ("top_movers", """CREATE TABLE IF NOT EXISTS top_movers (
        id SERIAL PRIMARY KEY, trade_date TEXT NOT NULL,
        symbol TEXT NOT NULL, category TEXT NOT NULL,
        value DOUBLE PRECISION, ltp DOUBLE PRECISION,
        volume BIGINT, pct_change DOUBLE PRECISION,
        created_at TEXT, UNIQUE(trade_date, symbol, category))"""),

    ("scraper_log", """CREATE TABLE IF NOT EXISTS scraper_log (
        id SERIAL PRIMARY KEY, module TEXT, status TEXT,
        records INTEGER DEFAULT 0, error TEXT,
        duration_s DOUBLE PRECISION, created_at TEXT)"""),
]

PG_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_ohlcv          ON daily_ohlcv(symbol, trade_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_live_market     ON live_market(symbol, snapshot_time DESC)",
    "CREATE INDEX IF NOT EXISTS idx_indices         ON indices(index_name, index_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_floorsheet      ON floorsheet(symbol, trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_broker_conc     ON broker_concentration(symbol, trade_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_tech_ind        ON technical_indicators(symbol, calc_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ai_signals      ON ai_signals(signal_date DESC, signal)",
    "CREATE INDEX IF NOT EXISTS idx_price_proj      ON price_projections(symbol, projection_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_top_movers      ON top_movers(trade_date DESC, category)",
    "CREATE INDEX IF NOT EXISTS idx_news            ON news(published_at DESC)",
]

# SQLite table definitions (kept for local dev)
SQLITE_TABLES = [
    (name, sql
     .replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
     .replace("DOUBLE PRECISION", "REAL")
     .replace("BIGINT", "INTEGER"))
    for name, sql in PG_TABLES
]
SQLITE_INDEXES = [i.replace(" IF NOT EXISTS ", " IF NOT EXISTS ") for i in PG_INDEXES]


class DB:
    def __init__(self):
        self._pg = USE_POSTGRES

    def conn(self):
        if self._pg:
            import psycopg2
            return psycopg2.connect(DATABASE_URL, sslmode="require")
        else:
            import sqlite3
            c = sqlite3.connect(DB_PATH, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=OFF")
            return c

    def _rows(self, cur):
        if self._pg:
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        return [dict(r) for r in cur.fetchall()]

    def _row(self, cur):
        if self._pg:
            if not cur.description:
                return None
            cols = [d[0] for d in cur.description]
            row  = cur.fetchone()
            return dict(zip(cols, row)) if row else None
        r = cur.fetchone()
        return dict(r) if r else None

    def _sql(self, sql):
        return _to_pg(sql) if self._pg else sql

    def init(self):
        tables  = PG_TABLES    if self._pg else SQLITE_TABLES
        indexes = PG_INDEXES   if self._pg else SQLITE_INDEXES
        c = self.conn()
        try:
            cur = c.cursor()
            for _, sql in tables:
                cur.execute(sql)
            for idx in indexes:
                try:
                    cur.execute(idx)
                except Exception:
                    pass
            c.commit()
            backend = "PostgreSQL/Supabase" if self._pg else f"SQLite ({DB_PATH})"
            print(f"DB ready — {len(tables)} tables | {backend}")
        except Exception as e:
            c.rollback()
            raise e
        finally:
            c.close()

    def run(self, sql, params=None):
        sql = self._sql(sql)
        c   = self.conn()
        try:
            cur = c.cursor()
            cur.execute(sql, params or [])
            c.commit()
            return cur.rowcount
        except Exception as e:
            c.rollback()
            raise e
        finally:
            c.close()

    def runmany(self, sql, params_list):
        if not params_list:
            return 0
        sql = self._sql(sql)
        c   = self.conn()
        try:
            cur = c.cursor()
            if self._pg:
                import psycopg2.extras
                psycopg2.extras.execute_batch(cur, sql, params_list, page_size=200)
            else:
                cur.executemany(sql, params_list)
            c.commit()
            return cur.rowcount
        except Exception as e:
            c.rollback()
            raise e
        finally:
            c.close()

    def one(self, sql, params=None):
        sql = self._sql(sql)
        c   = self.conn()
        try:
            cur = c.cursor()
            cur.execute(sql, params or [])
            return self._row(cur)
        finally:
            c.close()

    def all(self, sql, params=None):
        sql = self._sql(sql)
        c   = self.conn()
        try:
            cur = c.cursor()
            cur.execute(sql, params or [])
            return self._rows(cur)
        finally:
            c.close()

    def now(self):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def log(self, module, status, records=0, error=None, start_time=None):
        duration = None
        if start_time:
            try:
                t0 = (datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
                      if isinstance(start_time, str) else start_time)
                duration = round((datetime.now() - t0).total_seconds(), 2)
            except Exception:
                pass
        try:
            self.run(
                """INSERT INTO scraper_log
                   (module, status, records, error, duration_s, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [module, status, records, error, duration, self.now()])
        except Exception:
            pass


db = DB()
Database = DB
