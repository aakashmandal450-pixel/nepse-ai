"""
=============================================================
  NEPSE AI TRADER — floorsheet_scraper.py
  Modules 3 (Floorsheet) + 4 (Broker Activity & Concentration)
=============================================================
"""
import logging
from datetime import datetime
from collections import defaultdict
from base_scraper import BaseScraper
from db_manager import db
from settings import NEPSE_API_BASE, NEPALSTOCK_API

logger = logging.getLogger(__name__)


class FloorsheetScraper(BaseScraper):
    def __init__(self):
        super().__init__("Floorsheet")

    # ── MODULE 3: Floorsheet ──────────────────────────────────
    def scrape_floorsheet(self, trade_date: str = None) -> int:
        trade_date = trade_date or self.today_str()
        logger.info(f"📋 Scraping floorsheet for {trade_date}…")
        start = self.now_str()

        all_records = []
        page = 1

        while True:
            url    = f"{NEPALSTOCK_API}/floorsheet"
            params = {
                "page": page, "size": 2000,
                "businessDate": trade_date, "sort": "contractId,desc",
            }
            data = self.get(url, params=params)

            # Fallback to NepseAPI
            if not data:
                data = self.get(f"{NEPSE_API_BASE}/Floorsheet",
                                params={"page": page})
            if not data:
                break

            content = (data.get("floorsheets", {}).get("content")
                       or data.get("data")
                       or (data if isinstance(data, list) else []))
            if not content:
                break

            now = self.now_str()
            for row in content:
                tid = str(row.get("contractId") or row.get("transactionId") or "")
                sym = (row.get("stockSymbol") or row.get("symbol") or "").strip().upper()
                if not sym:
                    continue
                tid = tid or f"{trade_date}_{sym}_{len(all_records)}"
                all_records.append((
                    tid, trade_date, sym,
                    int(row.get("buyerMemberId") or row.get("buyerBroker") or 0),
                    int(row.get("sellerMemberId") or row.get("sellerBroker") or 0),
                    int(row.get("contractQuantity") or row.get("quantity") or 0),
                    float(row.get("contractRate") or row.get("rate") or 0),
                    float(row.get("contractAmount") or row.get("amount") or 0),
                    now,
                ))

            total_pages = (data.get("floorsheets", {}).get("totalPages")
                           or data.get("totalPages") or 1)
            logger.info(f"   Page {page}/{total_pages} — {len(content)} rows")
            if page >= total_pages:
                break
            page += 1
            self.sleep(1)

        if all_records:
            db.executemany("""
                INSERT INTO floorsheet
                  (transaction_id, trade_date, symbol, buyer_broker,
                   seller_broker, quantity, rate, amount, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(transaction_id) DO NOTHING
            """, all_records)

        logger.info(f"✅ Floorsheet: {len(all_records)} trades for {trade_date}")
        db.log_scraper_run("floorsheet",
                           "success" if all_records else "failed",
                           len(all_records), None, start, self.now_str())
        return len(all_records)

    # ── MODULE 4: Broker Activity ─────────────────────────────
    def compute_broker_activity(self, trade_date: str = None) -> int:
        trade_date = trade_date or self.today_str()
        logger.info(f"🔍 Computing broker activity for {trade_date}…")
        start = self.now_str()

        rows = db.fetchall("""
            SELECT symbol, buyer_broker, seller_broker, quantity, amount
            FROM floorsheet WHERE trade_date=?
        """, [trade_date])

        if not rows:
            logger.warning(f"No floorsheet data for {trade_date}")
            return 0

        buy_data  = defaultdict(lambda: defaultdict(lambda: [0, 0.0, 0]))
        sell_data = defaultdict(lambda: defaultdict(lambda: [0, 0.0, 0]))

        for r in rows:
            sym = r["symbol"]
            qty = r["quantity"] or 0
            amt = r["amount"] or 0
            if r["buyer_broker"]:
                b = r["buyer_broker"]
                buy_data[sym][b][0] += qty
                buy_data[sym][b][1] += amt
                buy_data[sym][b][2] += 1
            if r["seller_broker"]:
                s = r["seller_broker"]
                sell_data[sym][s][0] += qty
                sell_data[sym][s][1] += amt
                sell_data[sym][s][2] += 1

        all_syms = set(list(buy_data) + list(sell_data))
        activity = []
        now = self.now_str()

        for sym in all_syms:
            brokers = set(list(buy_data[sym]) + list(sell_data[sym]))
            for br in brokers:
                bq, ba, bt = buy_data[sym].get(br, [0, 0.0, 0])
                sq, sa, st = sell_data[sym].get(br, [0, 0.0, 0])
                activity.append((
                    trade_date, sym, br,
                    bq, ba, sq, sa,
                    bq - sq, ba - sa,
                    bt, st, now,
                ))

        if activity:
            db.executemany("""
                INSERT INTO broker_daily_activity
                  (trade_date, symbol, broker_number,
                   buy_quantity, buy_amount, sell_quantity, sell_amount,
                   net_quantity, net_amount, buy_trades, sell_trades, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(trade_date, symbol, broker_number) DO UPDATE SET
                    buy_quantity=excluded.buy_quantity,
                    sell_quantity=excluded.sell_quantity,
                    net_quantity=excluded.net_quantity
            """, activity)

        logger.info(f"✅ Broker activity: {len(activity)} rows")
        self._compute_concentration(trade_date, buy_data, sell_data)
        db.log_scraper_run("broker_activity", "success", len(activity),
                           None, start, self.now_str())
        return len(activity)

    def _compute_concentration(self, trade_date, buy_data, sell_data):
        all_syms = set(list(buy_data) + list(sell_data))
        records  = []
        now = self.now_str()

        for sym in all_syms:
            buys  = sorted(buy_data[sym].items(),  key=lambda x: x[1][0], reverse=True)
            sells = sorted(sell_data[sym].items(), key=lambda x: x[1][0], reverse=True)

            total_buy  = sum(v[0] for v in buy_data[sym].values())
            total_sell = sum(v[0] for v in sell_data[sym].values())

            top3_buy_qty  = sum(v[0] for _, v in buys[:3])
            top3_sell_qty = sum(v[0] for _, v in sells[:3])

            top3_buy_pct  = (top3_buy_qty  / total_buy  * 100) if total_buy  else 0
            top3_sell_pct = (top3_sell_qty / total_sell * 100) if total_sell else 0

            score = top3_buy_pct - top3_sell_pct

            records.append((
                trade_date, sym,
                round(top3_buy_pct, 2),  round(top3_sell_pct, 2),
                buys[0][0]  if buys  else None,
                buys[0][1][0] if buys else 0,
                sells[0][0] if sells else None,
                sells[0][1][0] if sells else 0,
                len(buy_data[sym]), len(sell_data[sym]),
                round(score, 2), now,
            ))

        if records:
            db.executemany("""
                INSERT INTO broker_concentration
                  (trade_date, symbol, top3_buy_pct, top3_sell_pct,
                   top_buyer_broker, top_buyer_qty,
                   top_seller_broker, top_seller_qty,
                   unique_buyers, unique_sellers, accumulation_score, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(trade_date, symbol) DO UPDATE SET
                    accumulation_score=excluded.accumulation_score
            """, records)
            logger.info(f"✅ Broker concentration: {len(records)} stocks")
