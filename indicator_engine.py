"""
=============================================================
  NEPSE AI TRADER — indicator_engine.py
  Module 8: Computes all technical indicators from DB OHLCV
=============================================================
"""
import logging
from datetime import datetime, date
from db_manager import db

logger = logging.getLogger(__name__)


class TechnicalIndicatorEngine:
    def __init__(self):
        try:
            import pandas as pd
            import pandas_ta as ta
            self.pd = pd
            self.ta = ta
            self._ready = True
        except ImportError as e:
            logger.error(f"Missing: {e}  →  pip install pandas pandas-ta")
            self._ready = False

    def compute_all(self, calc_date: str = None) -> int:
        if not self._ready:
            return 0

        calc_date = calc_date or date.today().strftime("%Y-%m-%d")
        logger.info(f"⚙️  Computing indicators for {calc_date}…")
        start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        stocks = db.fetchall("SELECT symbol FROM stocks WHERE is_active=1")
        total, errors = 0, 0

        for row in stocks:
            try:
                if self._compute_symbol(row["symbol"], calc_date):
                    total += 1
            except Exception as e:
                errors += 1
                logger.debug(f"{row['symbol']} indicator error: {e}")

        logger.info(f"✅ Indicators: {total} stocks computed, {errors} errors")
        db.log_scraper_run("compute_indicators", "success", total, None,
                           start, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        return total

    def _compute_symbol(self, symbol: str, calc_date: str) -> bool:
        pd = self.pd
        ta = self.ta

        rows = db.fetchall("""
            SELECT trade_date, open_price, high_price, low_price,
                   close_price, volume
            FROM daily_ohlcv
            WHERE symbol=? AND trade_date<=?
            ORDER BY trade_date DESC LIMIT 250
        """, [symbol, calc_date])

        if len(rows) < 20:
            return False

        df = pd.DataFrame(rows[::-1])
        df.columns = ["date","open","high","low","close","volume"]
        df = df.astype({"open":float,"high":float,"low":float,
                        "close":float,"volume":float})
        df.set_index("date", inplace=True)

        # Trend
        df["ema9"]   = ta.ema(df["close"], length=9)
        df["ema21"]  = ta.ema(df["close"], length=21)
        df["ema50"]  = ta.ema(df["close"], length=50)
        df["ema200"] = ta.ema(df["close"], length=200)

        macd = ta.macd(df["close"])
        if macd is not None and not macd.empty:
            df["macd"]   = macd.iloc[:,0]
            df["macd_s"] = macd.iloc[:,1]
            df["macd_h"] = macd.iloc[:,2]

        adx_df = ta.adx(df["high"], df["low"], df["close"])
        if adx_df is not None and not adx_df.empty:
            df["adx"] = adx_df.iloc[:,0]

        # Momentum
        df["rsi"]     = ta.rsi(df["close"], length=14)
        stoch = ta.stoch(df["high"], df["low"], df["close"])
        if stoch is not None and not stoch.empty:
            df["stoch_k"] = stoch.iloc[:,0]
            df["stoch_d"] = stoch.iloc[:,1]
        df["cci"]     = ta.cci(df["high"], df["low"], df["close"])
        df["willr"]   = ta.willr(df["high"], df["low"], df["close"])

        # Volume
        df["obv"]      = ta.obv(df["close"], df["volume"])
        try:
            df["vwap"] = ta.vwap(df["high"], df["low"], df["close"], df["volume"])
        except Exception:
            df["vwap"] = df["close"]
        df["vol_ma20"] = ta.sma(df["volume"], length=20)
        df["vol_rat"]  = df["volume"] / df["vol_ma20"].replace(0, float("nan"))
        df["cmf"]      = ta.cmf(df["high"], df["low"], df["close"], df["volume"])

        # Volatility
        df["atr"] = ta.atr(df["high"], df["low"], df["close"])
        bb = ta.bbands(df["close"])
        bb_u = bb_m = bb_l = bb_p = None
        bb_sq = 0
        if bb is not None and not bb.empty:
            bb_l = bb.iloc[:,0]; bb_m = bb.iloc[:,1]
            bb_u = bb.iloc[:,2]; bb_p = bb.iloc[:,4]
            bw   = (bb_u - bb_l) / bb_m.replace(0, float("nan"))
            bb_sq = int((bw.iloc[-1] < bw.rolling(20).mean().iloc[-1] * 0.75)
                        if len(bw) >= 20 else 0)

        last = df.iloc[-1]

        def s(v):
            try:
                f = float(v)
                return round(f,4) if f==f else None
            except Exception:
                return None

        signal_score = self._signal_score(last, df)

        db.execute("""
            INSERT INTO technical_indicators
              (symbol, calc_date, ema9, ema21, ema50, ema200,
               macd_line, macd_signal, macd_histogram, adx,
               rsi14, stoch_k, stoch_d, cci14, williams_r,
               obv, vwap, volume_ma20, volume_ratio, cmf,
               atr14, bb_upper, bb_middle, bb_lower, bb_pct_b, bb_squeeze,
               signal_strength, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol, calc_date) DO UPDATE SET
                rsi14=excluded.rsi14,
                signal_strength=excluded.signal_strength
        """, [
            symbol, calc_date,
            s(last.get("ema9")),  s(last.get("ema21")),
            s(last.get("ema50")), s(last.get("ema200")),
            s(last.get("macd")),  s(last.get("macd_s")),
            s(last.get("macd_h")), s(last.get("adx")),
            s(last.get("rsi")),   s(last.get("stoch_k")),
            s(last.get("stoch_d")), s(last.get("cci")),
            s(last.get("willr")),
            s(last.get("obv")),   s(last.get("vwap")),
            s(last.get("vol_ma20")), s(last.get("vol_rat")),
            s(last.get("cmf")),   s(last.get("atr")),
            s(bb_u.iloc[-1] if bb_u is not None else None),
            s(bb_m.iloc[-1] if bb_m is not None else None),
            s(bb_l.iloc[-1] if bb_l is not None else None),
            s(bb_p.iloc[-1] if bb_p is not None else None),
            bb_sq,
            round(signal_score, 2),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ])
        return True

    def _signal_score(self, last, df) -> float:
        score = 0.0
        w = 0

        def add(val, weight):
            nonlocal score, w
            try:
                f = float(val)
                if f == f:
                    score += f * weight
                    w += weight
            except Exception:
                pass

        rsi = last.get("rsi")
        if rsi:
            add((50 - float(rsi)) / 50 * 100, 2.0)

        hist = last.get("macd_h")
        if hist:
            add(min(max(float(hist), -100), 100), 1.5)

        close = float(df["close"].iloc[-1])
        ema50 = last.get("ema50")
        if ema50 and float(ema50) > 0:
            add(100 if close > float(ema50) else -100, 2.0)

        vol_r = last.get("vol_rat")
        if vol_r and len(df) > 1:
            prev = float(df["close"].iloc[-2])
            direction = 1 if close >= prev else -1
            if float(vol_r) > 1.5:
                add(50 * direction, 1.0)

        bb_p = last.get("bb_p")
        if bb_p:
            add((0.5 - float(bb_p)) * 200, 1.0)

        return max(-100, min(100, score / w)) if w > 0 else 0.0
