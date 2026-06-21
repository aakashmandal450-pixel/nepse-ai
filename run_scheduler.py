"""
NEPSE AI — run_scheduler.py
Master scheduler. Run this every trading day.
It calls all other modules automatically at the right times.

Usage:
  python run_scheduler.py            (daily scheduler)
  python run_scheduler.py --now      (fetch + signal right now)
"""
import schedule, time, sys, logging
from datetime import datetime, date

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-12s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("Scheduler")

from settings import *
from db_manager import db
from data_collector import (collect_live_market, collect_indices,
                            collect_floorsheet, compute_broker_analysis,
                            collect_corporate_actions, collect_news,
                            is_market_open, is_trading_day)
from indicator_engine import IndicatorEngine
from ai_signal_engine import NEPSEModel

indicator = IndicatorEngine()
model     = NEPSEModel()


# ── TASKS ─────────────────────────────────────────────────────
def task_live():
    if not is_market_open(): return
    logger.info("[SCHED] Live market + indices")
    collect_live_market()
    collect_indices()

def task_eod():
    if not is_trading_day(): return
    logger.info("[SCHED] EOD: Floorsheet + broker analysis")
    collect_floorsheet()
    compute_broker_analysis()

def task_indicators():
    if not is_trading_day(): return
    logger.info("[SCHED] Computing indicators")
    indicator.compute_all()

def task_signals():
    if not is_trading_day(): return
    logger.info("[SCHED] Generating AI signals")
    sigs = model.generate_signals()
    buys = sum(1 for s in sigs if s["signal"]=="BUY")
    logger.info(f"  Generated: {len(sigs)} signals | {buys} BUY")

def task_backtest():
    logger.info("[SCHED] Updating backtest accuracy")
    model.backtest()

def task_daily_maintenance():
    logger.info("[SCHED] Daily maintenance: corporate actions + news")
    collect_corporate_actions()
    collect_news()


# ── SCHEDULE SETUP ────────────────────────────────────────────
schedule.every(5).minutes.do(task_live)
schedule.every().day.at("15:10").do(task_eod)
schedule.every().day.at("15:45").do(task_indicators)
schedule.every().day.at("16:00").do(task_signals)
schedule.every().day.at("16:30").do(task_backtest)
schedule.every().day.at("06:00").do(task_daily_maintenance)
schedule.every(30).minutes.do(collect_news)


def run_now():
    """Run everything immediately — useful for testing."""
    logger.info("Running all tasks immediately...")
    db.init()   # ensure all tables exist before any task runs
    collect_live_market()
    collect_indices()
    collect_corporate_actions()
    collect_news()
    if is_trading_day():
        collect_floorsheet()
        compute_broker_analysis()
        indicator.compute_all()
        model.generate_signals()
        model.backtest()
    logger.info("Done!")


def main():
    logger.info("=" * 55)
    logger.info("  NEPSE AI TRADER — Scheduler")
    logger.info(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 55)

    db.init()

    logger.info("Schedule:")
    logger.info("  Every 5 min : Live market + indices (market hours)")
    logger.info("  15:10 NPT   : Floorsheet + broker analysis")
    logger.info("  15:45 NPT   : Technical indicators")
    logger.info("  16:00 NPT   : AI signals + price projections")
    logger.info("  16:30 NPT   : Backtest accuracy update")
    logger.info("  06:00 NPT   : Corporate actions + news")
    logger.info("\nRunning... Press Ctrl+C to stop")

    # Run live fetch immediately if market is open
    if is_market_open():
        task_live()

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    if "--now" in sys.argv:
        db.init()
        run_now()
    else:
        main()
