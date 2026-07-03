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

    # ── BACKFILL: compute indicators for ALL historical dates ──
    def backfill_history(self, symbols=None, start_date=None, min_rows=30):
        """
        One-time (or re-runnable) backfill of technical_indicators across
        a symbol's ENTIRE price history, not just 'today'.

        Unlike _compute_symbol() (which re-fetches the last 260 rows and
        computes only the latest value), this pulls the full OHLCV series
        once per symbol and runs the same `ta` functions across the whole
        series. Since EMA/RSI/MACD/etc. are causal rolling calculations,
        the value at any given date is the same either way — this is just
        far fewer DB round-trips.

        Usage:
            engine.backfill_history()                       # all symbols, all history
            engine.backfill_history(start_date="2020-01-01") # limit range
            engine.backfill_history(symbols=["NABIL"])        # one symbol (testing)
        """
        if not self._ready:
            logger.error("Install: pip install pandas ta")
            return 0

        if not symbols:
            stocks  = db.all("SELECT symbol FROM stocks WHERE is_active=1")
            symbols = [r["symbol"] for r in stocks]

        logger.info(f"Backfilling technical_indicators for {len(symbols)} symbols...")
        start = db.now()
        total_rows = 0
        ok = err = 0

        for i, sym in enumerate(symbols, 1):
            try:
                n = self._backfill_symbol(sym, start_date, min_rows)
                total_rows += n
                if n:
                    ok += 1
                if i % 20 == 0 or i == len(symbols):
                    logger.info(f"  [{i}/{len(symbols)}] {sym}: {n} rows "
                                f"(running total: {total_rows:,})")
            except Exception as e:
                err += 1
                logger.warning(f"{sym}: backfill failed: {e}")

        logger.info(f"Backfill complete: {ok} symbols OK, {err} failed, "
                    f"{total_rows:,} total indicator rows written")
        db.log("indicators_backfill", "success", total_rows, None, start)
        return total_rows

    def _backfill_symbol(self, symbol, start_date, min_rows):
        pd = self.pd

        rows = db.all("""
            SELECT trade_date, open_price, high_price, low_price,
                   close_price, volume, turnover
            FROM daily_ohlcv
            WHERE symbol=?
            ORDER BY trade_date ASC
        """, [symbol])

        if len(rows) < min_rows:
            return 0

        df = pd.DataFrame(rows)
        df.columns = ["date", "open", "high", "low", "close", "volume", "turnover"]
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df.set_index("date", inplace=True)
        df.dropna(subset=["close"], inplace=True)
        df = df[~df.index.duplicated(keep="last")]

        if len(df) < min_rows:
            return 0

        o, h, l, c, v = df["open"], df["high"], df["low"], df["close"], df["volume"]

        # ── TREND ───────────────────────────────────────────────
        from ta.trend import ema_indicator, MACD, ADXIndicator, CCIIndicator
        ema9, ema21, ema50, ema200 = (ema_indicator(c, window=w) for w in (9, 21, 50, 200))
        _macd = MACD(c, window_slow=26, window_fast=12, window_sign=9)
        ml, ms, mh = _macd.macd(), _macd.macd_signal(), _macd.macd_diff()
        _adx = ADXIndicator(h, l, c, window=14)
        adx_v, adxp, adxm = _adx.adx(), _adx.adx_pos(), _adx.adx_neg()

        # ── MOMENTUM ────────────────────────────────────────────
        from ta.momentum import RSIIndicator, StochasticOscillator, WilliamsRIndicator, ROCIndicator
        rsi14  = RSIIndicator(c, window=14).rsi()
        _stoch = StochasticOscillator(h, l, c, window=14, smooth_window=3)
        sk, sd = _stoch.stoch(), _stoch.stoch_signal()
        cci14  = CCIIndicator(h, l, c, window=14).cci()
        willr  = WilliamsRIndicator(h, l, c, lbp=14).williams_r()
        roc10  = ROCIndicator(c, window=10).roc()

        # ── VOLUME ──────────────────────────────────────────────
        from ta.volume import (OnBalanceVolumeIndicator, ChaikinMoneyFlowIndicator,
                               MFIIndicator, VolumeWeightedAveragePrice)
        obv   = OnBalanceVolumeIndicator(c, v).on_balance_volume()
        cmf   = ChaikinMoneyFlowIndicator(h, l, c, v, window=20).chaikin_money_flow()
        mfi14 = MFIIndicator(h, l, c, v, window=14).money_flow_index()
        vwap  = VolumeWeightedAveragePrice(h, l, c, v).volume_weighted_average_price()
        vol_ma20 = v.rolling(20).mean()
        vol_rat  = v / vol_ma20.replace(0, np.nan)

        # ── VOLATILITY ──────────────────────────────────────────
        from ta.volatility import AverageTrueRange, BollingerBands
        atr14   = AverageTrueRange(h, l, c, window=14).average_true_range()
        atr_pct = atr14 / c * 100
        _bb  = BollingerBands(c, window=20, window_dev=2)
        bb_u, bb_m, bb_l = _bb.bollinger_hband(), _bb.bollinger_mavg(), _bb.bollinger_lband()
        bb_p, bb_w       = _bb.bollinger_pband(), _bb.bollinger_wband()
        bw_ma  = bb_w.rolling(20).mean()
        bb_sq  = (bb_w < bw_ma * 0.75).astype("Int64").fillna(0).astype(int)

        # ── NEPSE-SPECIFIC FEATURES ─────────────────────────────
        stock = db.one("SELECT listed_shares, free_float_pct FROM stocks WHERE symbol=?", [symbol])
        float_shares = 0.0
        if stock:
            ls  = float(stock.get("listed_shares") or 0)
            ffp = float(stock.get("free_float_pct") or 30)
            float_shares = ls * ffp / 100 if ls > 0 else 0.0
        float_adj_vol = (v / float_shares * 100) if float_shares > 0 else pd.Series(0.0, index=df.index)

        nepse_rows = db.all("""
            SELECT index_date, close_value FROM indices WHERE index_name='NEPSE' ORDER BY index_date ASC
        """)
        rel_strength = pd.Series(np.nan, index=df.index)
        if nepse_rows:
            ni = pd.DataFrame(nepse_rows).set_index("index_date")
            ni["close_value"] = pd.to_numeric(ni["close_value"], errors="coerce")
            nepse_ret20 = ni["close_value"].pct_change(20).reindex(df.index).ffill()
            stock_ret20 = c.pct_change(20)
            rel_strength = ((stock_ret20 - nepse_ret20) * 100).round(3)

        high252   = h.rolling(252, min_periods=5).max()
        low252    = l.rolling(252, min_periods=5).min()
        dist_high = ((c / high252 - 1) * 100).round(2)
        dist_low  = ((c / low252 - 1) * 100).round(2)

        # ── CANDLESTICK PATTERNS (vectorized) ───────────────────
        body   = (c - o).abs()
        crange = (h - l).replace(0, np.nan)
        upper_wick = h - pd.concat([c, o], axis=1).max(axis=1)
        lower_wick = pd.concat([c, o], axis=1).min(axis=1) - l
        body_pct   = body / crange

        doji = (body_pct < 0.1).fillna(False).astype(int)

        c_prev = c.shift(1)
        hammer = ((lower_wick > 2 * body) & (upper_wick < body) & (c_prev > c)).fillna(False).astype(int)
        shooting_star = ((upper_wick > 2 * body) & (lower_wick < body) & (c_prev < c)).fillna(False).astype(int)

        o_prev = o.shift(1)
        engulf_bull = ((c_prev < o_prev) & (c > o) & (o < c_prev) & (c > o_prev)).fillna(False).astype(int)
        engulf_bear = ((c_prev > o_prev) & (c < o) & (o > c_prev) & (c < o_prev)).fillna(False).astype(int)

        b1 = (c.shift(2) - o.shift(2)).abs()
        b2 = (c.shift(1) - o.shift(1)).abs()
        b3 = body
        morning_star = ((c.shift(2) < o.shift(2)) & (b2 < b1 * 0.3) & (c > o) & (b3 > b1 * 0.5)).fillna(False).astype(int)
        evening_star = ((c.shift(2) > o.shift(2)) & (b2 < b1 * 0.3) & (c < o) & (b3 > b1 * 0.5)).fillna(False).astype(int)

        # ── COMPOSITE SIGNAL SCORE (vectorized) ─────────────────
        bc_rows = db.all("""
            SELECT trade_date, accumulation_score FROM broker_concentration
            WHERE symbol=? ORDER BY trade_date ASC
        """, [symbol])
        accum_series = pd.Series(np.nan, index=df.index)
        if bc_rows:
            bc = pd.DataFrame(bc_rows).set_index("trade_date")
            bc["accumulation_score"] = pd.to_numeric(bc["accumulation_score"], errors="coerce")
            accum_series = bc["accumulation_score"].reindex(df.index).ffill()

        signal_strength = self._composite_score_vectorized(
            c, rsi14, mh, ema50, vol_rat, bb_p, accum_series)

        # ── ASSEMBLE + FILTER ────────────────────────────────────
        out = pd.DataFrame({
            "ema9": ema9, "ema21": ema21, "ema50": ema50, "ema200": ema200,
            "macd_line": ml, "macd_signal": ms, "macd_histogram": mh,
            "adx": adx_v, "adx_plus_di": adxp, "adx_minus_di": adxm,
            "rsi14": rsi14, "stoch_k": sk, "stoch_d": sd,
            "cci14": cci14, "williams_r": willr, "roc10": roc10,
            "obv": obv, "vwap": vwap, "volume_ma20": vol_ma20, "volume_ratio": vol_rat,
            "cmf": cmf, "mfi14": mfi14,
            "atr14": atr14, "atr_pct": atr_pct,
            "bb_upper": bb_u, "bb_middle": bb_m, "bb_lower": bb_l,
            "bb_pct_b": bb_p, "bb_bandwidth": bb_w, "bb_squeeze": bb_sq,
            "float_adj_volume": float_adj_vol, "relative_strength": rel_strength,
            "dist_from_52w_high": dist_high, "dist_from_52w_low": dist_low,
            "pattern_doji": doji, "pattern_hammer": hammer,
            "pattern_engulf_bull": engulf_bull, "pattern_engulf_bear": engulf_bear,
            "pattern_morning_star": morning_star, "pattern_evening_star": evening_star,
            "pattern_shooting_star": shooting_star,
            "signal_strength": signal_strength,
        })

        # Only keep rows once core indicators have warmed up (rsi14 + macd valid)
        out = out[out["rsi14"].notna() & out["macd_line"].notna()]
        if start_date:
            out = out[out.index >= start_date]

        if out.empty:
            return 0

        round_cols = [c2 for c2 in out.columns if c2 not in
                     ("bb_squeeze", "pattern_doji", "pattern_hammer",
                      "pattern_engulf_bull", "pattern_engulf_bear",
                      "pattern_morning_star", "pattern_evening_star",
                      "pattern_shooting_star")]
        out[round_cols] = out[round_cols].round(4)

        now = db.now()

        def none_if_nan(x):
            try:
                return None if (x is None or pd.isna(x)) else float(x)
            except Exception:
                return None

        records = []
        for calc_date, row in out.iterrows():
            records.append((
                symbol, calc_date,
                none_if_nan(row["ema9"]), none_if_nan(row["ema21"]),
                none_if_nan(row["ema50"]), none_if_nan(row["ema200"]),
                none_if_nan(row["macd_line"]), none_if_nan(row["macd_signal"]),
                none_if_nan(row["macd_histogram"]),
                none_if_nan(row["adx"]), none_if_nan(row["adx_plus_di"]), none_if_nan(row["adx_minus_di"]),
                none_if_nan(row["rsi14"]), none_if_nan(row["stoch_k"]), none_if_nan(row["stoch_d"]),
                none_if_nan(row["cci14"]), none_if_nan(row["williams_r"]), none_if_nan(row["roc10"]),
                none_if_nan(row["obv"]), none_if_nan(row["vwap"]),
                none_if_nan(row["volume_ma20"]), none_if_nan(row["volume_ratio"]),
                none_if_nan(row["cmf"]), none_if_nan(row["mfi14"]),
                none_if_nan(row["atr14"]), none_if_nan(row["atr_pct"]),
                none_if_nan(row["bb_upper"]), none_if_nan(row["bb_middle"]), none_if_nan(row["bb_lower"]),
                none_if_nan(row["bb_pct_b"]), none_if_nan(row["bb_bandwidth"]), int(row["bb_squeeze"]),
                none_if_nan(row["float_adj_volume"]), none_if_nan(row["relative_strength"]),
                none_if_nan(row["dist_from_52w_high"]), none_if_nan(row["dist_from_52w_low"]),
                int(row["pattern_doji"]), int(row["pattern_hammer"]),
                int(row["pattern_engulf_bull"]), int(row["pattern_engulf_bear"]),
                int(row["pattern_morning_star"]), int(row["pattern_evening_star"]),
                int(row["pattern_shooting_star"]),
                none_if_nan(row["signal_strength"]),
                now,
            ))

        db.runmany("""
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
                ema9=excluded.ema9, ema21=excluded.ema21, ema50=excluded.ema50, ema200=excluded.ema200,
                macd_line=excluded.macd_line, macd_signal=excluded.macd_signal, macd_histogram=excluded.macd_histogram,
                adx=excluded.adx, rsi14=excluded.rsi14,
                bb_upper=excluded.bb_upper, bb_middle=excluded.bb_middle, bb_lower=excluded.bb_lower,
                bb_pct_b=excluded.bb_pct_b, bb_bandwidth=excluded.bb_bandwidth, bb_squeeze=excluded.bb_squeeze,
                signal_strength=excluded.signal_strength
        """, records)

        return len(records)

    def _composite_score_vectorized(self, c, rsi14, mh, ema50, vol_rat, bb_pband, accum_series):
        """Vectorized equivalent of _composite_score(), run across a whole series."""
        pd = self.pd
        idx = c.index
        scores  = pd.Series(0.0, index=idx)
        weights = pd.Series(0.0, index=idx)

        def add(contrib, weight, mask):
            nonlocal scores, weights
            w = pd.Series(weight, index=idx).where(mask, 0.0)
            contrib = contrib.where(mask, 0.0)
            scores += contrib.fillna(0.0) * w
            weights += w

        add((50 - rsi14) / 50 * 100, 2.0, rsi14.notna())
        add(mh.clip(-100, 100), 1.5, mh.notna())
        add(pd.Series(np.where(c > ema50, 100.0, -100.0), index=idx), 2.0, ema50.notna())

        direction = pd.Series(np.where(c >= c.shift(1), 1, -1), index=idx)
        vol_mask  = vol_rat.notna() & (vol_rat > 1.5)
        add(50 * direction, 1.0, vol_mask)

        add((0.5 - bb_pband) * 200, 1.0, bb_pband.notna())
        add(accum_series.clip(-100, 100), 3.0, accum_series.notna())

        result = (scores / weights.replace(0, np.nan)).clip(-100, 100)
        return result

    # ── per-symbol computation (single date — daily pipeline use) ──
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

    if "--backfill" in sys.argv:
        start_arg = None
        for a in sys.argv[1:]:
            if a.startswith("20") and "-" in a:
                start_arg = a
        engine.backfill_history(start_date=start_arg)
    elif len(sys.argv) > 1 and sys.argv[1].startswith("20"):
        engine.compute_all(calc_date=sys.argv[1])
    else:
        engine.compute_all()
