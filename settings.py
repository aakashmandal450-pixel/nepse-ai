"""
NEPSE AI — settings.py
Edit this file to configure paths and keys.
"""
import os

# ── Paths ─────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DB_PATH     = os.path.join(BASE_DIR, "nepse_ai.db")
MODELS_DIR  = os.path.join(BASE_DIR, "models")
EXPORTS_DIR = os.path.join(BASE_DIR, "exports")
LOG_DIR     = os.path.join(BASE_DIR, "logs")

for d in [MODELS_DIR, EXPORTS_DIR, LOG_DIR]:
    os.makedirs(d, exist_ok=True)

# ── Data sources ──────────────────────────────────────────────
NEPSEALPHA_LIVE   = "https://nepsealpha.com/live/stocks"
NEPALSTOCK_API    = "https://www.nepalstock.com.np/api/nots"
NEPSE_UNOFF_API   = "https://nepseapi.surajrimal.dev"
SHARESANSAR_BASE  = "https://www.sharesansar.com"
MEROLAGANI_BASE   = "https://merolagani.com"

# ── Scraping ──────────────────────────────────────────────────
REQUEST_DELAY     = 2.5
REQUEST_TIMEOUT   = 25
MAX_RETRIES       = 3
HISTORY_YEARS     = 3

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/121.0 Safari/537.36",
]

# ── Market calendar ───────────────────────────────────────────
MARKET_DAYS       = [6, 0, 1, 2, 3]   # Sun=6 Mon=0 Tue=1 Wed=2 Thu=3
MARKET_OPEN_HOUR  = 11
MARKET_CLOSE_HOUR = 15
NEPSE_HOLIDAYS    = [
    "2025-01-11","2025-02-19","2025-04-14","2025-04-15","2025-05-01",
    "2025-08-21","2025-09-19","2025-10-02","2025-10-20","2025-11-05",
    "2026-01-11","2026-02-19","2026-04-14","2026-04-15","2026-05-01",
]

# ── Brokerage costs (for backtesting) ─────────────────────────
BROKERAGE_PCT    = 0.004   # 0.4% brokerage
SEBON_PCT        = 0.00015 # 0.015% SEBON fee
DP_CHARGE        = 25      # Rs. 25 flat DP charge per trade
TOTAL_COST_PCT   = BROKERAGE_PCT + SEBON_PCT  # ~0.415% one way

# ── ML Model ──────────────────────────────────────────────────
BUY_THRESHOLD    = 0.60    # confidence above this → BUY signal
SELL_THRESHOLD   = 0.35    # confidence below this → SELL signal
MIN_CONF_DISPLAY = 50      # hide signals below this confidence
TARGET_MOVE_PCT  = 0.02    # 2% move = successful signal

# ── Wyckoff phase thresholds ──────────────────────────────────
WYCKOFF_ACCUM_SCORE  =  15   # broker accum score for "Accumulation"
WYCKOFF_MARKUP_SCORE =  30   # higher score = "Mark-up"
WYCKOFF_DIST_SCORE   = -15   # for "Distribution"
WYCKOFF_DECLINE_SCORE= -30   # for "Decline"
