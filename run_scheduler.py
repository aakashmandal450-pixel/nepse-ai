"""
=============================================================
 NEPSE AI TRADER — CLEAN NEPSEALPHA ONLY VERSION
=============================================================
"""

import time
import schedule
import logging
import os
import sys
import io
from datetime import datetime
import cloudscraper

from db_manager import db


# ─────────────────────────────────────────────
# UTF-8 FIX (Windows safe)
# ─────────────────────────────────────────────
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


# ─────────────────────────────────────────────
# LOGGING (NO EMOJIS, STABLE)
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger("NEPSE")


# ─────────────────────────────────────────────
# NEPSEALPHA CLIENT (ONLY DATA SOURCE)
# ─────────────────────────────────────────────
class NEPSEAlphaClient:
    def __init__(self):
        self.scraper = cloudscraper.create_scraper()
        self.url = "https://nepsealpha.com/live/stocks"

    def fetch_live(self):
        try:
            params = {
                "fsk": str(int(time.time() * 1000)),
                "selectedSector": "NEPSE",
                "sector": "",
                "fs": "123100"
            }

            res = self.scraper.get(self.url, params=params, timeout=15)

            if res.status_code != 200:
                logger.warning(f"HTTP Error: {res.status_code}")
                return []

            data = res.json()
            return data.get("stock_live", {}).get("prices", [])

        except Exception as e:
            logger.error(f"Fetch failed: {e}")
            return []


# ─────────────────────────────────────────────
# MARKET DATA LAYER
# ─────────────────────────────────────────────
class MarketData:
    def __init__(self):
        self.client = NEPSEAlphaClient()

    def get_live_market(self):
        raw = self.client.fetch_live()

        if not raw:
            logger.warning("No market data received")
            return []

        cleaned = []

        for item in raw:
            try:
                cleaned.append({
                    "symbol": item.get("symbol"),
                    "open": item.get("open"),
                    "high": item.get("high"),
                    "low": item.get("low"),
                    "close": item.get("close"),
                    "volume": item.get("volume"),
                    "turnover": item.get("turn_over"),
                    "percent_change": item.get("percent_change"),
                    "point_change": item.get("point_change"),
                    "prev_close": item.get("prev_close"),
                    "week52_high": item.get("week52_high"),
                    "week52_low": item.get("week52_low"),
                    "total_trades": item.get("total_trades"),
                })
            except:
                continue

        return cleaned


# ─────────────────────────────────────────────
# STORAGE (MEMORY + SQLITE)
# ─────────────────────────────────────────────
class DataStore:
    def __init__(self):
        self.latest_data = []

    def save(self, data):
        """Keep in memory for signal generation."""
        self.latest_data = data

    def get(self):
        return self.latest_data

    def persist_live_market(self, data):
        """Write live market snapshot to SQLite."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        live_rows = []
        stock_rows = []

        for d in data:
            sym = d.get("symbol")
            if not sym:
                continue
            live_rows.append((
                sym, d.get("close"), d.get("point_change"),
                d.get("percent_change"), d.get("open"), d.get("high"),
                d.get("low"), d.get("volume"), d.get("prev_close"),
                d.get("turnover"), d.get("week52_high"), d.get("week52_low"),
                d.get("total_trades"), now, now
            ))
            stock_rows.append((sym, now, now))

        try:
            db.executemany("""
                INSERT INTO live_market
                  (symbol, ltp, point_change, pct_change, open_price,
                   high_price, low_price, volume, prev_close, turnover,
                   week52_high, week52_low, total_trades,
                   snapshot_time, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, live_rows)
            logger.info(f"DB: {len(live_rows)} rows -> live_market")
        except Exception as e:
            logger.error(f"DB live_market write failed: {e}")

        try:
            db.executemany("""
                INSERT OR IGNORE INTO stocks (symbol, created_at, updated_at)
                VALUES (?,?,?)
            """, stock_rows)
        except Exception as e:
            logger.error(f"DB stocks write failed: {e}")

    def persist_signals(self, signals):
        """Write AI signals to SQLite."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        today = datetime.now().strftime("%Y-%m-%d")
        rows = []

        for s in signals:
            sym = s.get("symbol")
            if not sym:
                continue
            rows.append((
                today, today, sym, s.get("signal", "HOLD"),
                None, s.get("change"), None, None, None,
                None, "v0.1", None, None, None, now
            ))

        try:
            db.executemany("""
                INSERT OR REPLACE INTO ai_signals
                  (signal_date, target_date, symbol, signal,
                   confidence_pct, predicted_move_pct, stop_loss_price,
                   target_price, risk_reward, key_drivers, model_version,
                   actual_close, actual_pct_change, signal_correct, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, rows)
            logger.info(f"DB: {len(rows)} rows -> ai_signals")
        except Exception as e:
            logger.error(f"DB ai_signals write failed: {e}")


# ─────────────────────────────────────────────
# AI ENGINE (PLACEHOLDER READY)
# ─────────────────────────────────────────────
class AIEngine:
    def generate_signals(self, data):
        signals = []

        for d in data:
            try:
                change = float(d.get("percent_change") or 0)

                if change > 2:
                    signal = "BUY"
                elif change < -2:
                    signal = "SELL"
                else:
                    signal = "HOLD"

                signals.append({
                    "symbol": d["symbol"],
                    "signal": signal,
                    "change": change
                })

            except:
                continue

        return signals


# ─────────────────────────────────────────────
# MAIN SYSTEM
# ─────────────────────────────────────────────
market = MarketData()
store = DataStore()
ai = AIEngine()


# ─────────────────────────────────────────────
# TASKS
# ─────────────────────────────────────────────
def task_fetch_market():
    logger.info("Fetching live market data...")
    started = datetime.now()

    data = market.get_live_market()

    if data:
        store.save(data)
        store.persist_live_market(data)
        logger.info(f"Saved {len(data)} stocks (memory + DB)")
        db.log_scraper_run(
            "live_market", "success", records=len(data),
            started_at=started.strftime("%Y-%m-%d %H:%M:%S"),
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            duration=(datetime.now() - started).total_seconds()
        )
    else:
        logger.warning("No data fetched")
        db.log_scraper_run(
            "live_market", "empty",
            started_at=started.strftime("%Y-%m-%d %H:%M:%S"),
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            duration=(datetime.now() - started).total_seconds()
        )


def task_generate_signals():
    logger.info("Generating AI signals...")

    data = store.get()

    if not data:
        logger.warning("No data for signals")
        return

    signals = ai.generate_signals(data)

    buy_list  = sorted([s for s in signals if s["signal"] == "BUY"],
                       key=lambda x: x["change"], reverse=True)
    sell_list = sorted([s for s in signals if s["signal"] == "SELL"],
                       key=lambda x: x["change"])
    hold_list = [s for s in signals if s["signal"] == "HOLD"]

    logger.info(f"Signals -> BUY: {len(buy_list)}, SELL: {len(sell_list)}, "
                f"HOLD: {len(hold_list)}, TOTAL: {len(signals)}")

    if buy_list:
        buy_names = ", ".join(f"{s['symbol']}({s['change']:+.2f}%)" for s in buy_list)
        logger.info(f"BUY  stocks: {buy_names}")

    if sell_list:
        sell_names = ", ".join(f"{s['symbol']}({s['change']:+.2f}%)" for s in sell_list)
        logger.info(f"SELL stocks: {sell_names}")

    if hold_list:
        hold_names = ", ".join(s["symbol"] for s in hold_list)
        logger.info(f"HOLD stocks: {hold_names}")

    store.persist_signals(signals)


# ─────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────
schedule.every(5).minutes.do(task_fetch_market)
schedule.every(10).minutes.do(task_generate_signals)


# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────
def main():
    logger.info("NEPSE AI SYSTEM STARTED")
    logger.info(str(datetime.now()))

    task_fetch_market()

    while True:
        schedule.run_pending()
        time.sleep(5)


# ─────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    main()