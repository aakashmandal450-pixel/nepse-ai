"""
NEPSE AI — indicator_engine.py
Computes 30+ technical indicators + NEPSE-specific features.
Uses the 'ta' library (Python 3.14 compatible, no numba required).
Run:  python indicator_engine.py
      python indicator_engine.py 2026-06-17   (specific date)
"""
import sys, logging
from datetime import date
import numpy as np

logger = logging.getLogger("Indicators")

from db_manager import db
from settings import *


class IndicatorEngine:

    def __init__(self):
        try:
            import pandas as pd
            import ta
            from ta.trend     import ema_indicator, MACD, ADXIndicator, CCIIndicator
            from ta.momentum  import (RSIIndicator, StochasticOscillator,
                                      WilliamsRIndicator, ROCIndicator)
            from ta.volume    import (OnBalanceVolumeIndicator,
                                      ChaikinMoneyFlowIndicator,
                                      MFIIndicator,
                                      VolumeWeightedAveragePrice)
            from ta.volatility import AverageTrueRange, BollingerBands

            self.pd  = pd
            self.ta  = ta
            self._ready = True
        except ImportError as e:
            logger.error(f"Missing: {e} → pip install pandas ta")
            self._ready = False

    # ── public entry point ─────────────────────────────────────
    def compute_all(self, calc_date=None, symbols=None):
        if not self._ready:
            return 0

        calc_date = calc_date or date.today().strftime("%Y-%m-%d")
        logger.info(f"Computing indicators for {calc_date}...")
        start = db.now()

        if not symbols:
            stocks  = db.all("SELECT symbol FROM stocks WHERE is_active=1")
            symbols = [r["symbol"] for r in stocks]

        ok = err = 0
        for sym in symbols:
            try:
                if self._compute_symbol(sym, calc_date):
                    ok += 1
            except Exception as e:
                err += 1
                logger.debug(f"{sym}: {e}")

        logger.info(f"Indicators: {ok} computed, {err} skipped")
        db.log("indicators", "success", ok, None, start)
        return ok

    # ── per-symbol computation ─────────────────────────────────
    def _compute_symbol(self, symbol, calc_date):
        pd = self.pd

        rows = db.all("""
            SELECT trade_date, open_price, high_price, low_price,
                   close_price, volume, turnover
            FROM daily_ohlcv
            WHERE symbol=? AND trade_date<=?
            ORDER BY trade_date DESC LIMIT 260
        """, [symbol, calc_date])

        if len(rows) < 25:
            return False

        # Oldest first for indicator calculation
        df = pd.DataFrame(rows[::-1])
        df.columns = ["date", "open", "high", "low", "close", "volume", "turnover"]
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df.set_index("date", inplace=True)
        df.dropna(subset=["close"], inplace=True)

        if len(df) < 20:
            return False

        c = df["close"]
        h = df["high"]
        l = df["low"]
        v = df["volume"]

        # ── TREND ─────────────────────────────────────────────
        from ta.trend import ema_indicator, MACD, ADXIndicator, CCIIndicator

        ema9   = ema_indicator(c, window=9)
        ema21  = ema_indicator(c, window=21)
        ema50  = ema_indicator(c, window=50)
        ema200 = ema_indicator(c, window=200)

        _macd  = MACD(c, window_slow=26, window_fast=12, window_sign=9)
        ml     = _macd.macd()           # MACD line
        ms     = _macd.macd_signal()    # signal line
        mh     = _macd.macd_diff()      # histogram

        _adx   = ADXIndicator(h, l, c, window=14)
        adx_v  = _adx.adx()
        adxp   = _adx.adx_pos()         # +DI
        adxm   = _adx.adx_neg()         # -DI

        # ── MOMENTUM ──────────────────────────────────────────
        from ta.momentum import (RSIIndicator, StochasticOscillator,
                                  WilliamsRIndicator, ROCIndicator)

        rsi14  = RSIIndicator(c, window=14).rsi()

        _stoch = StochasticOscillator(h, l, c, window=14, smooth_window=3)
        sk     = _stoch.stoch()
        sd     = _stoch.stoch_signal()

        cci14  = CCIIndicator(h, l, c, window=14).cci()
        willr  = WilliamsRIndicator(h, l, c, lbp=14).williams_r()
        roc10  = ROCIndicator(c, window=10).roc()

        # ── VOLUME ────────────────────────────────────────────
        from ta.volume import (OnBalanceVolumeIndicator,
                                ChaikinMoneyFlowIndicator,
                                MFIIndicator,
                                VolumeWeightedAveragePrice)

        obv      = OnBalanceVolumeIndicator(c, v).on_balance_volume()
        cmf      = ChaikinMoneyFlowIndicator(h, l, c, v, window=20).chaikin_money_flow()
        mfi14    = MFIIndicator(h, l, c, v, window=14).money_flow_index()
        vwap     = VolumeWeightedAveragePrice(h, l, c, v).volume_weighted_average_price()

        vol_ma20 = v.rolling(20).mean()
        vol_rat  = v / vol_ma20.replace(0, np.nan)

        # ── VOLATILITY ────────────────────────────────────────
        from ta.volatility import AverageTrueRange, BollingerBands

        atr14    = AverageTrueRange(h, l, c, window=14).average_true_range()
        atr_pct  = atr14 / c * 100

        _bb      = BollingerBands(c, window=20, window_dev=2)
        bb_u     = _bb.bollinger_hband()
        bb_m     = _bb.bollinger_mavg()
        bb_l     = _bb.bollinger_lband()
        bb_p     = _bb.bollinger_pband()   # %B  (0–1)
        bb_w     = _bb.bollinger_wband()   # bandwidth

        # BB squeeze: bandwidth below 75% of its 20-period average
        bb_sq = 0
        if len(bb_w.dropna()) >= 20:
            bw_ma = bb_w.rolling(20).mean()
            last_bw  = bb_w.iloc[-1]
            last_bwma = bw_ma.iloc[-1]
            if pd.notna(last_bw) and pd.notna(last_bwma) and last_bwma != 0:
                bb_sq = int(last_bw < last_bwma * 0.75)

        # ── NEPSE-SPECIFIC FEATURES ───────────────────────────
        # Float-adjusted volume
        stock = db.one(
            "SELECT listed_shares, free_float_pct FROM stocks WHERE symbol=?",
            [symbol])
        float_shares = 0.0
        if stock:
            ls  = float(stock.get("listed_shares") or 0)
            ffp = float(stock.get("free_float_pct") or 30)
            float_shares = ls * ffp / 100 if ls > 0 else 0.0

        last_vol      = float(v.iloc[-1]) if len(v) > 0 else 0
        float_adj_vol = (last_vol / float_shares * 100) if float_shares > 0 else 0.0

        # Relative strength vs NEPSE index (20-day)
        nepse = db.all("""
            SELECT index_date, close_value FROM indices
            WHERE index_name='NEPSE' AND index_date<=?
            ORDER BY index_date DESC LIMIT 21
        """, [calc_date])

        rel_strength = 0.0
        if nepse and len(nepse) >= 2 and len(df) >= 2:
            try:
                stock_ret = float(c.iloc[-1]) / float(c.iloc[-min(21, len(c)-1)]) - 1
                nepse_ret = (float(nepse[0]["close_value"]) /
                             float(nepse[-1]["close_value"]) - 1)
                rel_strength = round((stock_ret - nepse_ret) * 100, 3)
            except Exception:
                pass

        # 52-week high/low distance
        high52 = float(h.rolling(252).max().iloc[-1]) if len(h) >= 5 else float(h.max())
        low52  = float(l.rolling(252).min().iloc[-1])  if len(l) >= 5 else float(l.min())
        last_c = float(c.iloc[-1])
        dist_high = round((last_c / high52 - 1) * 100, 2) if high52 > 0 else 0.0
        dist_low  = round((last_c / low52  - 1) * 100, 2) if low52  > 0 else 0.0

        # ── CANDLESTICK PATTERNS (pure-Python, no pandas_ta) ──
        patt = self._detect_patterns(df)

        # ── COMPOSITE SIGNAL STRENGTH (−100 to +100) ─────────
        signal_score = self._composite_score(
            c, rsi14, mh, ema50, vol_rat, bb_p, symbol, calc_date)

        # ── SAVE TO DB ────────────────────────────────────────
        def s(series_or_val):
            """Safely extract last scalar from a Series or a plain value."""
            try:
                if series_or_val is None:
                    return None
                if hasattr(series_or_val, "iloc"):
                    val = series_or_val.iloc[-1]
                else:
                    val = series_or_val
                f = float(val)
                return round(f, 4) if not np.isnan(f) else None
            except Exception:
                return None

        db.run("""
            INSERT INTO technical_indicators
              (symbol, calc_date,
               ema9, ema21, ema50, ema200,
               macd_line, macd_signal, macd_histogram,
               adx, adx_plus_di, adx_minus_di,
               rsi14, stoch_k, stoch_d, cci14, williams_r, roc10,
               obv, vwap, volume_ma20, volume_ratio, cmf, mfi14,
               atr14, atr_pct,
               bb_upper, bb_middle, bb_lower, bb_pct_b, bb_bandwidth, bb_squeeze,
               float_adj_volume, relative_strength,
               dist_from_52w_high, dist_from_52w_low,
               pattern_doji, pattern_hammer,
               pattern_engulf_bull, pattern_engulf_bear,
               pattern_morning_star, pattern_evening_star, pattern_shooting_star,
               signal_strength, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol, calc_date) DO UPDATE SET
                rsi14=excluded.rsi14,
                macd_histogram=excluded.macd_histogram,
                signal_strength=excluded.signal_strength,
                bb_squeeze=excluded.bb_squeeze,
                adx=excluded.adx
        """, [
            symbol, calc_date,
            s(ema9),  s(ema21), s(ema50), s(ema200),
            s(ml), s(ms), s(mh),
            s(adx_v), s(adxp), s(adxm),
            s(rsi14), s(sk), s(sd),
            s(cci14), s(willr), s(roc10),
            s(obv), s(vwap),
            s(vol_ma20), s(vol_rat),
            s(cmf), s(mfi14),
            s(atr14), s(atr_pct),
            s(bb_u), s(bb_m), s(bb_l), s(bb_p), s(bb_w), bb_sq,
            round(float_adj_vol, 4), rel_strength,
            dist_high, dist_low,
            patt["doji"],       patt["hammer"],
            patt["engulf_bull"],patt["engulf_bear"],
            patt["morning_star"],patt["evening_star"],patt["shooting_star"],
            round(signal_score, 2),
            db.now(),
        ])
        return True

    # ── CANDLESTICK PATTERN DETECTION (pure Python) ────────────
    def _detect_patterns(self, df) -> dict:
        """Detect common candlestick patterns on the last 3 candles."""
        patt = {k: 0 for k in ["doji","hammer","engulf_bull","engulf_bear",
                                 "morning_star","evening_star","shooting_star"]}
        try:
            if len(df) < 3:
                return patt

            o  = df["open"].values
            h  = df["high"].values
            lo = df["low"].values
            c  = df["close"].values

            i = len(c) - 1   # last candle index

            body      = abs(c[i] - o[i])
            candle_range = h[i] - lo[i]
            if candle_range == 0:
                return patt

            upper_wick = h[i]  - max(c[i], o[i])
            lower_wick = min(c[i], o[i]) - lo[i]
            body_pct   = body / candle_range

            # Doji: body < 10% of range
            if body_pct < 0.1:
                patt["doji"] = 1

            # Hammer: lower wick > 2x body, small upper wick, appears in downtrend
            if lower_wick > 2 * body and upper_wick < body and c[i-1] > c[i]:
                patt["hammer"] = 1

            # Shooting star: upper wick > 2x body, small lower wick, appears in uptrend
            if upper_wick > 2 * body and lower_wick < body and c[i-1] < c[i]:
                patt["shooting_star"] = 1

            # Bullish engulfing
            if (c[i-1] < o[i-1] and        # prev candle bearish
                c[i]   > o[i]   and         # curr candle bullish
                o[i]   < c[i-1] and         # curr open below prev close
                c[i]   > o[i-1]):           # curr close above prev open
                patt["engulf_bull"] = 1

            # Bearish engulfing
            if (c[i-1] > o[i-1] and
                c[i]   < o[i]   and
                o[i]   > c[i-1] and
                c[i]   < o[i-1]):
                patt["engulf_bear"] = 1

            # Morning star (3-candle): bearish, small body, bullish
            if i >= 2:
                b1 = abs(c[i-2] - o[i-2])
                b2 = abs(c[i-1] - o[i-1])
                b3 = abs(c[i]   - o[i])
                if (c[i-2] < o[i-2] and     # first candle bearish
                    b2 < b1 * 0.3  and       # second small body (star)
                    c[i]   > o[i]  and       # third bullish
                    b3 > b1 * 0.5):          # third recovers > 50%
                    patt["morning_star"] = 1

                # Evening star (3-candle): bullish, small body, bearish
                if (c[i-2] > o[i-2] and
                    b2 < b1 * 0.3  and
                    c[i]   < o[i]  and
                    b3 > b1 * 0.5):
                    patt["evening_star"] = 1

        except Exception:
            pass
        return patt

    # ── COMPOSITE SIGNAL SCORE ─────────────────────────────────
    def _composite_score(self, close, rsi14, macd_hist, ema50,
                          vol_rat, bb_pband, symbol, calc_date) -> float:
        """Weighted composite score from −100 (strong sell) to +100 (strong buy)."""
        score = 0.0
        w     = 0.0

        def add(val, weight):
            nonlocal score, w
            try:
                f = float(val)
                if not np.isnan(f):
                    score += f * weight
                    w     += weight
            except Exception:
                pass

        # RSI: oversold → bullish, overbought → bearish (weight 2.0)
        try:
            r = float(rsi14.iloc[-1])
            if not np.isnan(r):
                add((50 - r) / 50 * 100, 2.0)
        except Exception:
            pass

        # MACD histogram: positive → bullish (weight 1.5)
        try:
            h = float(macd_hist.iloc[-1])
            if not np.isnan(h):
                add(min(max(h, -100), 100), 1.5)
        except Exception:
            pass

        # EMA50 alignment: price above → bullish (weight 2.0)
        try:
            e = float(ema50.iloc[-1])
            lc = float(close.iloc[-1])
            if not np.isnan(e) and not np.isnan(lc):
                add(100 if lc > e else -100, 2.0)
        except Exception:
            pass

        # Volume ratio on up/down day (weight 1.0)
        try:
            vr = float(vol_rat.iloc[-1])
            prev = float(close.iloc[-2])
            curr = float(close.iloc[-1])
            direction = 1 if curr >= prev else -1
            if not np.isnan(vr) and vr > 1.5:
                add(50 * direction, 1.0)
        except Exception:
            pass

        # Bollinger %B: <0.2 oversold, >0.8 overbought (weight 1.0)
        try:
            pb = float(bb_pband.iloc[-1])
            if not np.isnan(pb):
                add((0.5 - pb) * 200, 1.0)
        except Exception:
            pass

        # Broker accumulation score (weight 3.0 — biggest NEPSE edge)
        broker = db.one("""
            SELECT accumulation_score FROM broker_concentration
            WHERE symbol=? AND trade_date<=?
            ORDER BY trade_date DESC LIMIT 1
        """, [symbol, calc_date])
        if broker and broker.get("accumulation_score") is not None:
            add(min(max(float(broker["accumulation_score"]), -100), 100), 3.0)

        return max(-100.0, min(100.0, score / w)) if w > 0 else 0.0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    engine = IndicatorEngine()
    if not engine._ready:
        print("Install: pip install pandas ta")
        sys.exit(1)

    if len(sys.argv) > 1:
        engine.compute_all(calc_date=sys.argv[1])
    else:
        engine.compute_all()
