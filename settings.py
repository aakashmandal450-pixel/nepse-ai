"""
=============================================================
  NEPSE AI TRADER — settings.py
  All configuration lives here. Edit this file only.
=============================================================
"""

# ── DATABASE ──────────────────────────────────────────────────
DATABASE = {
    "host":     "localhost",
    "port":     5432,
    "name":     "nepse_ai",
    "user":     "postgres",
    "password": "your_password_here",   # only used if USE_SQLITE=False
}

USE_SQLITE   = True                     # True = SQLite (no setup needed)
SQLITE_PATH  = "nepse_ai.db"           # saved right in D:\Stockpile\

# ── DATA SOURCES ──────────────────────────────────────────────
NEPSEALPHA_BASE  = "https://nepsealpha.com"
NEPSE_API_BASE   = "https://nepseapi.surajrimal.dev"
NEPALSTOCK_API   = "https://www.nepalstock.com.np/api/nots"
SHARESANSAR_BASE = "https://www.sharesansar.com"

# ── SCRAPING BEHAVIOUR ────────────────────────────────────────
REQUEST_DELAY_MIN  = 2.0
REQUEST_DELAY_MAX  = 5.0
REQUEST_TIMEOUT    = 30
MAX_RETRIES        = 3
RETRY_DELAY        = 10

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

# ── NEPSE MARKET CALENDAR ─────────────────────────────────────
MARKET_OPEN_HOUR  = 11          # 11:00 AM NPT
MARKET_CLOSE_HOUR = 15          # 3:00 PM  NPT
# Python weekday: Mon=0 Tue=1 Wed=2 Thu=3 Fri=4 Sat=5 Sun=6
# NEPSE trades Sun–Thu → 6,0,1,2,3
MARKET_DAYS = [6, 0, 1, 2, 3]

NEPSE_HOLIDAYS = [
    "2025-01-11","2025-02-19","2025-03-08","2025-04-14","2025-04-15",
    "2025-05-01","2025-08-21","2025-09-19","2025-10-02","2025-10-20",
    "2025-11-05","2025-12-25",
    "2026-01-11","2026-02-19","2026-04-14","2026-04-15",
]

# ── SCHEDULER TIMING ─────────────────────────────────────────
HISTORY_YEARS = 3

# ── LOGGING ───────────────────────────────────────────────────
LOG_LEVEL        = "INFO"
LOG_DIR          = "logs"
LOG_MAX_MB       = 10
LOG_BACKUP_COUNT = 5
