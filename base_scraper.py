"""
=============================================================
  NEPSE AI TRADER — base_scraper.py
  Shared HTTP utilities: retry, anti-ban, market calendar.
=============================================================
"""
import requests, time, random, logging, json
from datetime import datetime, date
from settings import (
    REQUEST_DELAY_MIN, REQUEST_DELAY_MAX, REQUEST_TIMEOUT,
    MAX_RETRIES, RETRY_DELAY, USER_AGENTS,
    NEPSE_API_BASE, NEPALSTOCK_API, NEPSEALPHA_BASE,
    MARKET_OPEN_HOUR, MARKET_CLOSE_HOUR, MARKET_DAYS, NEPSE_HOLIDAYS,
)

logger = logging.getLogger(__name__)


class BaseScraper:
    def __init__(self, name: str):
        self.name = name
        self.session = requests.Session()
        self._rotate_agent()

    def _rotate_agent(self):
        self.session.headers.update({
            "User-Agent":      random.choice(USER_AGENTS),
            "Accept":          "application/json, text/html, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection":      "keep-alive",
            "Referer":         NEPSEALPHA_BASE,
        })

    def get(self, url: str, params: dict = None,
            json_response: bool = True):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self._rotate_agent()
                resp = self.session.get(url, params=params,
                                        timeout=REQUEST_TIMEOUT)
                if resp.status_code == 200:
                    return resp.json() if json_response else resp.text
                elif resp.status_code == 429:
                    wait = RETRY_DELAY * attempt * 2
                    logger.warning(f"[{self.name}] Rate limited — waiting {wait}s")
                    time.sleep(wait)
                else:
                    logger.warning(f"[{self.name}] HTTP {resp.status_code}: {url}")
            except requests.exceptions.Timeout:
                logger.warning(f"[{self.name}] Timeout attempt {attempt}")
            except requests.exceptions.ConnectionError as e:
                logger.warning(f"[{self.name}] Connection error: {e}")
            except json.JSONDecodeError:
                logger.warning(f"[{self.name}] JSON decode failed")
                return None
            except Exception as e:
                logger.error(f"[{self.name}] Error: {e}")

            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)

        logger.error(f"[{self.name}] All {MAX_RETRIES} attempts failed: {url}")
        return None

    def sleep(self, extra: float = 0):
        time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX) + extra)

    @staticmethod
    def is_market_open() -> bool:
        now = datetime.now()
        if now.weekday() not in MARKET_DAYS:
            return False
        if now.strftime("%Y-%m-%d") in NEPSE_HOLIDAYS:
            return False
        return MARKET_OPEN_HOUR <= now.hour < MARKET_CLOSE_HOUR

    @staticmethod
    def is_trading_day(check_date: date = None) -> bool:
        d = check_date or date.today()
        if d.weekday() not in MARKET_DAYS:
            return False
        return d.strftime("%Y-%m-%d") not in NEPSE_HOLIDAYS

    @staticmethod
    def today_str() -> str:
        return date.today().strftime("%Y-%m-%d")

    @staticmethod
    def now_str() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
