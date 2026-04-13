"""
=============================================================
  NEPSE AI TRADER — corporate_news_scraper.py
  Module 7 (Corporate Actions) + Module 9 (News & Sentiment)
=============================================================
"""
import logging, re, json
from datetime import datetime, date
from base_scraper import BaseScraper
from db_manager import db
from settings import NEPALSTOCK_API, SHARESANSAR_BASE

logger = logging.getLogger(__name__)

# ── Simple rule-based sentiment ───────────────────────────────
POS = ["profit","dividend","bonus","growth","record","approve","increase",
       "highest","strong","launch","expansion","surplus","gain","rise","good",
       "excellent","improve","beat","declare","award"]
NEG = ["loss","decline","drop","fall","decrease","closure","suspend",
       "investigation","fine","penalty","fraud","lawsuit","bankrupt",
       "default","bear","negative","below","concern","risk","warning","delist"]

def sentiment(text: str):
    t = text.lower()
    pos = sum(1 for w in POS if w in t)
    neg = sum(1 for w in NEG if w in t)
    total = pos + neg
    if total == 0:
        return 0.0, "neutral"
    score = (pos - neg) / total
    label = "positive" if score > 0.1 else "negative" if score < -0.1 else "neutral"
    return round(score, 3), label


class CorporateActionsScraper(BaseScraper):
    def __init__(self):
        super().__init__("CorporateActions")

    def scrape_corporate_actions(self) -> int:
        logger.info("📢 Scraping corporate actions…")
        start = self.now_str()
        total = 0

        for method in [self._dividends, self._bonus, self._agm, self._bookclose]:
            try:
                total += method()
                self.sleep(0.5)
            except Exception as e:
                logger.warning(f"Corporate action scrape error: {e}")

        logger.info(f"✅ Corporate actions: {total} records")
        db.log_scraper_run("corporate_actions", "success", total, None, start, self.now_str())
        return total

    def _insert(self, records):
        if not records:
            return 0
        return db.executemany("""
            INSERT INTO corporate_actions
              (symbol, action_type, announcement_date, ex_date,
               book_close_date, record_date, effective_date,
               value, cash_per_share, remarks, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol, action_type, announcement_date) DO NOTHING
        """, records)

    def _dividends(self):
        data = self.get(f"{NEPALSTOCK_API}/dividendList")
        if not data:
            return 0
        items = data if isinstance(data, list) else data.get("data", [])
        now = self.now_str()
        recs = []
        for i in items:
            sym = (i.get("symbol") or "").strip().upper()
            if sym:
                recs.append((sym, "dividend",
                    i.get("announcementDate",""), i.get("exDate",""),
                    i.get("bookCloseDate",""), i.get("recordDate",""), "",
                    float(i.get("dividendPercentage") or 0),
                    float(i.get("cashDividendPerUnit") or 0),
                    i.get("remarks",""), now))
        logger.info(f"   Dividends: {len(recs)}")
        return self._insert(recs)

    def _bonus(self):
        data = self.get(f"{NEPALSTOCK_API}/bonusList")
        if not data:
            return 0
        items = data if isinstance(data, list) else data.get("data", [])
        now = self.now_str()
        recs = []
        for i in items:
            sym = (i.get("symbol") or "").strip().upper()
            if sym:
                recs.append((sym, "bonus",
                    i.get("announcementDate",""), i.get("exDate",""),
                    i.get("bookCloseDate",""), i.get("recordDate",""),
                    i.get("distributionDate",""),
                    float(i.get("bonusPercentage") or 0), 0,
                    i.get("remarks",""), now))
        logger.info(f"   Bonus: {len(recs)}")
        return self._insert(recs)

    def _agm(self):
        data = self.get(f"{NEPALSTOCK_API}/agmList")
        if not data:
            return 0
        items = data if isinstance(data, list) else data.get("data", [])
        now = self.now_str()
        recs = []
        for i in items:
            sym = (i.get("symbol") or "").strip().upper()
            if sym:
                recs.append((sym, "agm",
                    i.get("announcementDate",""), "", i.get("bookCloseDate",""),
                    "", i.get("agmDate",""), 0, 0,
                    i.get("venue") or i.get("remarks",""), now))
        logger.info(f"   AGMs: {len(recs)}")
        return self._insert(recs)

    def _bookclose(self):
        data = self.get(f"{NEPALSTOCK_API}/bookCloseList")
        if not data:
            return 0
        items = data if isinstance(data, list) else data.get("data", [])
        now = self.now_str()
        recs = []
        for i in items:
            sym = (i.get("symbol") or "").strip().upper()
            if sym:
                recs.append((sym, "bookclose",
                    i.get("announcementDate",""),
                    i.get("fromDate",""), i.get("toDate",""),
                    "", "", 0, 0,
                    i.get("remarks") or i.get("purpose",""), now))
        logger.info(f"   Book closes: {len(recs)}")
        return self._insert(recs)


class NewsScraper(BaseScraper):
    def __init__(self):
        super().__init__("News")

    def scrape_news(self) -> int:
        logger.info("📰 Scraping news…")
        start = self.now_str()
        total = self._sharesansar()
        logger.info(f"✅ News: {total} articles saved")
        db.log_scraper_run("news", "success" if total else "partial",
                           total, None, start, self.now_str())
        return total

    def _sharesansar(self) -> int:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.warning("BeautifulSoup not installed — run: pip install beautifulsoup4")
            return 0

        resp = self.get(f"{SHARESANSAR_BASE}/category/stock-market",
                        json_response=False)
        if not resp:
            return 0

        try:
            soup = BeautifulSoup(resp, "html.parser")
        except Exception:
            return 0

        stocks_set = {r["symbol"] for r in
                      db.fetchall("SELECT symbol FROM stocks WHERE is_active=1")}

        articles = soup.find_all(
            "div", class_=re.compile("post|article|news|item", re.I))
        records = []
        now = self.now_str()

        for art in articles[:30]:
            try:
                title_tag = (art.find("h1") or art.find("h2") or
                             art.find("h3") or art.find("a"))
                if not title_tag:
                    continue
                headline = title_tag.get_text(strip=True)
                if not headline or len(headline) < 10:
                    continue

                link = art.find("a", href=True)
                url  = ""
                if link:
                    href = link["href"]
                    url  = href if href.startswith("http") else f"{SHARESANSAR_BASE}{href}"

                time_tag = art.find("time") or art.find(
                    class_=re.compile("date|time", re.I))
                pub = (time_tag.get("datetime") or
                       time_tag.get_text(strip=True) if time_tag else now)

                mentioned = [s for s in stocks_set if s in headline.upper()]
                cat = "general"
                for kw, lbl in [("dividend","dividend"),("bonus","bonus"),
                                 ("ipo","ipo"),("fpo","ipo"),("agm","agm"),
                                 ("right","rights"),("sebon","regulatory"),
                                 ("nrb","regulatory")]:
                    if kw in headline.lower():
                        cat = lbl
                        break

                score, label = sentiment(headline)
                records.append((
                    headline, "", url or None, "sharesansar",
                    str(pub)[:19], json.dumps(mentioned),
                    cat, score, label, now,
                ))
            except Exception:
                continue

        saved = 0
        if records:
            try:
                saved = db.executemany("""
                    INSERT INTO news
                      (headline, summary, url, source, published_at,
                       symbols_mentioned, category, sentiment_score,
                       sentiment_label, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(url) DO NOTHING
                """, records)
            except Exception as e:
                logger.error(f"News insert error: {e}")

        return len(records)
