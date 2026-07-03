"""
NEPSE AI — ai_signal_engine.py
XGBoost + LightGBM ensemble trained on ALL historical data.
Outputs: BUY/SELL/HOLD signals, price projections, strategy, Wyckoff phase.

Run:  python ai_signal_engine.py --train
      python ai_signal_engine.py --signals
      python ai_signal_engine.py --backtest
"""
import sys, os, logging, json, joblib
import numpy as np
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("AIEngine")

from db_manager import db
from settings import *

MODELS_PATH = Path(MODELS_DIR)
MODELS_PATH.mkdir(exist_ok=True)

META_COLS = {"symbol","trade_date","label_buy","label_sell","next_day_return",
             "close_price","open_price","high_price","low_price",
             "adj_close","volume","turnover","total_trades"}


# ─────────────────────────────────────────────────────────────
#  FEATURE ENGINEERING
# ─────────────────────────────────────────────────────────────
def build_features(symbol: str, cutoff: str, label: bool = True):
    """Build feature matrix for one stock from all data sources."""
    try:
        import pandas as pd
    except ImportError:
        return None

    # Base OHLCV
    rows = db.all("""
        SELECT trade_date, open_price, high_price, low_price,
               close_price, adj_close, volume, turnover, total_trades
        FROM daily_ohlcv
        WHERE symbol=? AND trade_date>=?
        ORDER BY trade_date ASC
    """, [symbol, cutoff])

    if len(rows) < 30:
        return None

    df = pd.DataFrame(rows)
    df.set_index("trade_date", inplace=True)
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["symbol"] = symbol

    c = df["close_price"]
    v = df["volume"]

    # ── Price return features ─────────────────────────────────
    for n in [1, 3, 5, 10, 20]:
        df[f"ret_{n}d"] = c.pct_change(n)

    df["gap_pct"]       = df["open_price"] / c.shift(1) - 1
    df["day_range_pct"] = (df["high_price"] - df["low_price"]) / c.shift(1).replace(0, np.nan)
    df["upper_wick"]    = df["high_price"] - df[["close_price","open_price"]].max(axis=1)
    df["lower_wick"]    = df[["close_price","open_price"]].min(axis=1) - df["low_price"]
    df["body_pct"]      = (df["close_price"] - df["open_price"]).abs() / (df["high_price"] - df["low_price"] + 1e-9)

    # ── Volume features ───────────────────────────────────────
    df["vol_ma5"]    = v.rolling(5).mean()
    df["vol_ma20"]   = v.rolling(20).mean()
    df["vol_ratio5"] = v / df["vol_ma5"].replace(0, np.nan)
    df["vol_ratio20"]= v / df["vol_ma20"].replace(0, np.nan)
    df["vol_spike"]  = (v > df["vol_ma20"] * 2).astype(int)
    df["turnover_r"] = df["turnover"] / df["turnover"].rolling(20).mean().replace(0, np.nan)

    # ── Price position ────────────────────────────────────────
    df["high_20d"]      = df["high_price"].rolling(20).max()
    df["low_20d"]       = df["low_price"].rolling(20).min()
    df["price_pos_20d"] = (c - df["low_20d"]) / (df["high_20d"] - df["low_20d"] + 1e-9)
    df["pct_from_52h"]  = c / df["high_price"].rolling(252).max() - 1
    df["pct_from_52l"]  = c / df["low_price"].rolling(252).min() - 1

    # Consecutive up/down days
    df["up_day"]    = (df["ret_1d"] > 0).astype(int)
    df["consec_up"] = df["up_day"].groupby(
        (df["up_day"] != df["up_day"].shift()).cumsum()).cumcount() + 1
    df["consec_up"] = df["consec_up"] * df["up_day"]

    # ── Technical indicators from DB ──────────────────────────
    ti_rows = db.all("""
        SELECT calc_date,
               ema9, ema21, ema50, ema200,
               macd_line, macd_signal, macd_histogram, adx,
               rsi14, stoch_k, stoch_d, cci14, williams_r, roc10,
               obv, volume_ratio, cmf, mfi14,
               atr14, atr_pct, bb_upper, bb_middle, bb_lower,
               bb_pct_b, bb_bandwidth, bb_squeeze,
               float_adj_volume, relative_strength,
               dist_from_52w_high, dist_from_52w_low,
               pattern_doji, pattern_hammer, pattern_engulf_bull,
               pattern_engulf_bear, pattern_morning_star, pattern_evening_star,
               pattern_shooting_star, signal_strength
        FROM technical_indicators
        WHERE symbol=? AND calc_date>=?
        ORDER BY calc_date ASC
    """, [symbol, cutoff])

    if ti_rows:
        import pandas as pd
        ti = pd.DataFrame(ti_rows)
        # Normalize both indexes to plain YYYY-MM-DD strings before joining
        ti["calc_date"] = pd.to_datetime(ti["calc_date"]).dt.strftime("%Y-%m-%d")
        ti = ti.set_index("calc_date")
        df.index = pd.to_datetime(df.index).strftime("%Y-%m-%d")
        for col in ti.columns:
            ti[col] = pd.to_numeric(ti[col], errors="coerce")
            df[f"ti_{col}"] = ti[col].reindex(df.index)
        # Forward-fill so any trading day with no indicator row gets the last value
        ti_cols = [c for c in df.columns if c.startswith("ti_")]
        df[ti_cols] = df[ti_cols].ffill()

        # Derived indicator features
        if "ti_ema50" in df.columns:
            df["ema_align"] = (
                (c > df.get("ti_ema9",  0)).astype(int) +
                (c > df.get("ti_ema21", 0)).astype(int) +
                (c > df.get("ti_ema50", 0)).astype(int) +
                (c > df.get("ti_ema200",0)).astype(int)
            )
        if "ti_rsi14" in df.columns:
            df["rsi_oversold"]   = (df["ti_rsi14"] < 30).astype(int)
            df["rsi_overbought"] = (df["ti_rsi14"] > 70).astype(int)
            df["rsi_midzone"]    = ((df["ti_rsi14"] >= 40) & (df["ti_rsi14"] <= 60)).astype(int)
        if "ti_macd_line" in df.columns and "ti_macd_signal" in df.columns:
            df["macd_cross_up"]  = (
                (df["ti_macd_line"] > df["ti_macd_signal"]) &
                (df["ti_macd_line"].shift(1) <= df["ti_macd_signal"].shift(1))
            ).astype(int)
        if "ti_bb_squeeze" in df.columns:
            df["bb_squeeze_breakout"] = (
                (df["ti_bb_squeeze"].shift(1) == 1) & (df["ti_bb_squeeze"] == 0)
            ).astype(int)

    # ── Broker concentration features ─────────────────────────
    bc_rows = db.all("""
        SELECT trade_date, top3_buy_pct, top3_sell_pct,
               unique_buyers, unique_sellers,
               avg_trade_size, large_trades_pct,
               accumulation_score
        FROM broker_concentration
        WHERE symbol=? AND trade_date>=?
        ORDER BY trade_date ASC
    """, [symbol, cutoff])

    if bc_rows:
        import pandas as pd
        bc = pd.DataFrame(bc_rows)
        bc["trade_date"] = pd.to_datetime(bc["trade_date"]).dt.strftime("%Y-%m-%d")
        bc = bc.set_index("trade_date")
        for col in bc.columns:
            bc[col] = pd.to_numeric(bc[col], errors="coerce")
            df[f"broker_{col}"] = bc[col].reindex(df.index)
        # Forward-fill broker scores for days without floorsheet data
        broker_cols = [c for c in df.columns if c.startswith("broker_")]
        df[broker_cols] = df[broker_cols].ffill()

        if "broker_accumulation_score" in df.columns:
            df["broker_accum_5d"] = df["broker_accumulation_score"].rolling(5).mean()
            df["broker_accum_trend"] = (
                df["broker_accumulation_score"] > df["broker_accum_5d"]
            ).astype(int)
            df["broker_buyer_seller_ratio"] = (
                df["broker_unique_buyers"] / (df["broker_unique_sellers"] + 1e-9)
                if "broker_unique_buyers" in df.columns else 0
            )

    # ── Corporate action proximity ────────────────────────────
    actions = db.all("""
        SELECT action_type, ex_date, book_close_date, announcement_date, value
        FROM corporate_actions WHERE symbol=?
    """, [symbol])

    df["days_to_bookclose"] = 999.0
    df["days_to_agm"]       = 999.0
    df["has_bonus_90d"]     = 0
    df["has_dividend_90d"]  = 0

    if actions:
        try:
            import pandas as pd
            idx = pd.to_datetime(df.index)
            for act in actions:
                for col, date_field in [("days_to_bookclose","book_close_date"),
                                         ("days_to_agm","announcement_date")]:
                    dval = act.get(date_field,"")
                    if dval:
                        try:
                            target = pd.Timestamp(dval)
                            diff   = (target - idx).days.clip(lower=0)
                            df[col] = df[col].combine(
                                pd.Series(diff.astype(float), index=df.index), min)
                        except Exception:
                            pass
                # Recent bonus/dividend flag
                atype = act.get("action_type","")
                adate = act.get("announcement_date","")
                if adate:
                    try:
                        age = (date.today() - date.fromisoformat(adate[:10])).days
                        if age < 90:
                            if atype == "bonus":   df["has_bonus_90d"]    = 1
                            if atype == "dividend": df["has_dividend_90d"] = 1
                    except Exception:
                        pass
        except Exception:
            pass

    # ── Fundamentals ──────────────────────────────────────────
    fund = db.one("""
        SELECT eps, pe_ratio, book_value, price_to_book, roe, dividend_yield
        FROM fundamentals WHERE symbol=?
        ORDER BY report_date DESC LIMIT 1
    """, [symbol])

    if fund:
        for k, v2 in fund.items():
            df[f"fund_{k}"] = float(v2 or 0)

    # ── NEPSE index relative return ───────────────────────────
    nepse_rows = db.all("""
        SELECT index_date, close_value, pct_change FROM indices
        WHERE index_name='NEPSE' AND index_date>=?
        ORDER BY index_date ASC
    """, [cutoff])

    if nepse_rows:
        import pandas as pd
        ni = pd.DataFrame(nepse_rows)
        ni["index_date"] = pd.to_datetime(ni["index_date"]).dt.strftime("%Y-%m-%d")
        ni = ni.set_index("index_date")
        ni["pct_change"] = pd.to_numeric(ni["pct_change"], errors="coerce")
        df["nepse_pct"]    = ni["pct_change"].reindex(df.index).ffill()
        df["rel_vs_nepse"] = df["ret_1d"] - df["nepse_pct"] / 100

    # ── Label ─────────────────────────────────────────────────
    if label:
        df["next_day_return"] = c.shift(-1) / c - 1
        df["label_buy"]  = (df["next_day_return"] >= TARGET_MOVE_PCT).astype(int)
        df["label_sell"] = (df["next_day_return"] <= -TARGET_MOVE_PCT).astype(int)
        df.dropna(subset=["label_buy"], inplace=True)

    df.reset_index(inplace=True)
    df.rename(columns={"index":"trade_date","trade_date":"trade_date"}, errors="ignore")
    df.fillna(0, inplace=True)
    return df


# ─────────────────────────────────────────────────────────────
#  STRATEGY DETECTION
# ─────────────────────────────────────────────────────────────
def detect_strategy(row: dict) -> str:
    """Detect trading strategy from indicator values."""
    rsi      = float(row.get("ti_rsi14") or 50)
    macd_h   = float(row.get("ti_macd_histogram") or 0)
    bb_p     = float(row.get("ti_bb_pct_b") or 0.5)
    vol_r    = float(row.get("vol_ratio20") or 1)
    ema_align= int(row.get("ema_align") or 2)
    bb_sq    = int(row.get("ti_bb_squeeze") or 0)
    adx      = float(row.get("ti_adx") or 20)
    ret_1d   = float(row.get("ret_1d") or 0)
    ret_5d   = float(row.get("ret_5d") or 0)

    # Breakout: BB squeeze released + volume spike
    if bb_sq == 0 and vol_r > 2.0 and adx > 25:
        return "Breakout"

    # Momentum: all EMAs aligned + MACD positive + strong ADX
    if ema_align >= 3 and macd_h > 0 and adx > 20 and ret_5d > 0:
        return "Momentum"

    # Mean Reversion: RSI oversold + price near lower BB
    if rsi < 35 and bb_p < 0.25:
        return "Mean Reversion"

    # Support & Resistance: price bouncing near key levels
    if 0.2 < bb_p < 0.8 and abs(macd_h) < 1 and adx < 20:
        return "Support & Resistance"

    # Default
    return "Momentum" if ret_1d > 0 else "Mean Reversion"


def detect_time_series_type(df_slice) -> str:
    """ADF-like heuristic: is price stationary or trending?"""
    try:
        closes = list(df_slice)
        if len(closes) < 20:
            return "Non Stationary"
        mean  = sum(closes) / len(closes)
        diffs = [abs(closes[i] - closes[i-1]) for i in range(1, len(closes))]
        avg_diff = sum(diffs) / len(diffs)
        stddev   = (sum((x - mean)**2 for x in closes) / len(closes)) ** 0.5
        coef_var = stddev / mean if mean != 0 else 0
        return "Stationary" if coef_var < 0.08 else "Non Stationary"
    except Exception:
        return "Non Stationary"


# ─────────────────────────────────────────────────────────────
#  PRICE PROJECTION
# ─────────────────────────────────────────────────────────────
def compute_price_projections(symbol: str, ltp: float, atr: float,
                               conf: float, signal: str,
                               signal_date: str) -> dict:
    """
    Compute probabilistic price range for 10 and 20 days.
    Based on ATR, historical volatility, and ML confidence.
    """
    if ltp <= 0 or atr <= 0:
        return {}

    # Historical volatility from last 20 candles
    rows = db.all("""
        SELECT close_price FROM daily_ohlcv
        WHERE symbol=? ORDER BY trade_date DESC LIMIT 22
    """, [symbol])

    hist_vol = atr / ltp  # default: ATR-based
    if len(rows) >= 5:
        try:
            prices  = [float(r["close_price"]) for r in rows]
            returns = [(prices[i] / prices[i+1] - 1) for i in range(len(prices)-1)]
            hist_vol = (sum(r**2 for r in returns) / len(returns)) ** 0.5
        except Exception:
            pass

    # Project using random walk assumption scaled by confidence
    conf_factor = conf / 100
    for horizon in [10, 20]:
        vol_h = hist_vol * (horizon ** 0.5)

        # Directional bias from signal
        if signal == "BUY":
            expected = ltp * (1 + vol_h * conf_factor * 0.5)
            upside   = ltp * (1 + vol_h * 1.5)
            downside = ltp * (1 - vol_h * 0.8)
        elif signal == "SELL":
            expected = ltp * (1 - vol_h * conf_factor * 0.5)
            upside   = ltp * (1 + vol_h * 0.8)
            downside = ltp * (1 - vol_h * 1.5)
        else:
            expected = ltp
            upside   = ltp * (1 + vol_h)
            downside = ltp * (1 - vol_h)

        prob_up   = round(conf_factor * 100 if signal == "BUY" else (1 - conf_factor) * 100, 1)
        prob_down = round(100 - prob_up, 1)

        try:
            db.run("""
                INSERT INTO price_projections
                  (symbol, projection_date, horizon_days,
                   current_price, target_price, upside_price, downside_price,
                   probability_up, probability_down, atr_based, method, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, projection_date, horizon_days) DO UPDATE SET
                    target_price=excluded.target_price,
                    upside_price=excluded.upside_price,
                    downside_price=excluded.downside_price
            """, [
                symbol, signal_date, horizon, ltp,
                round(expected, 2), round(upside, 2), round(downside, 2),
                prob_up, prob_down,
                round(atr, 2), "ATR+HistVol", db.now(),
            ])
        except Exception:
            pass

    # Return 10-day values for the signal table
    vol_10 = hist_vol * (10 ** 0.5)
    return {
        "upside_10d":   round(ltp * (1 + vol_10 * 1.5), 2),
        "downside_10d": round(ltp * (1 - vol_10 * 0.8), 2),
        "target_10d":   round(ltp * (1 + vol_10 * conf_factor * 0.5
                                     if signal=="BUY"
                                     else -vol_10 * conf_factor * 0.5), 2),
    }


# ─────────────────────────────────────────────────────────────
#  ML MODEL CLASS
# ─────────────────────────────────────────────────────────────
class NEPSEModel:
    VERSION = "2.0"

    def __init__(self):
        self.xgb       = None
        self.lgbm      = None
        self.feat_cols = []
        self._load()

    # ── TRAINING ──────────────────────────────────────────────
    def train(self, lookback_days=730):
        try:
            import pandas as pd
            from xgboost import XGBClassifier
            from lightgbm import LGBMClassifier
            from sklearn.metrics import precision_score, recall_score
        except ImportError as e:
            logger.error(f"Missing: {e}")
            return

        logger.info("=" * 55)
        logger.info("  TRAINING NEPSE AI MODEL")
        logger.info(f"  Data: {lookback_days} days lookback")
        logger.info("=" * 55)

        cutoff  = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        stocks  = db.all("SELECT symbol FROM stocks WHERE is_active=1")
        frames  = []

        for i, row in enumerate(stocks, 1):
            sym = row["symbol"]
            df  = build_features(sym, cutoff, label=True)
            if df is not None and len(df) >= 60:
                frames.append(df)
            if i % 30 == 0:
                logger.info(f"  Features built: {i}/{len(stocks)}")

        if not frames:
            logger.error("No training data. Run data_collector.py --history first.")
            return

        data = pd.concat(frames, ignore_index=True)
        self.feat_cols = [c for c in data.columns if c not in META_COLS
                          and c != "label_sell" and c != "next_day_return"]

        logger.info(f"Dataset: {len(data):,} rows x {len(self.feat_cols)} features")

        # Walk-forward validation
        self._walk_forward(data)

        # Final model on all data
        logger.info("\nTraining final model on all data...")
        X = data[self.feat_cols].fillna(0).replace([np.inf, -np.inf], 0)
        y = data["label_buy"]

        pos = max(y.sum(), 1); neg = len(y) - pos
        scale = neg / pos

        self.xgb = XGBClassifier(
            n_estimators=400, max_depth=5, learning_rate=0.04,
            subsample=0.8, colsample_bytree=0.75,
            scale_pos_weight=scale, eval_metric="logloss",
            verbosity=0, random_state=42, n_jobs=-1,
        )
        self.lgbm = LGBMClassifier(
            n_estimators=400, max_depth=5, learning_rate=0.04,
            subsample=0.8, colsample_bytree=0.75,
            class_weight="balanced", verbose=-1,
            random_state=42, n_jobs=-1,
        )
        self.xgb.fit(X, y)
        self.lgbm.fit(X, y)

        logger.info(f"Models trained on {len(X):,} samples")
        self._save()
        self._feature_importance(top_n=25)

    def _walk_forward(self, df):
        import pandas as pd
        logger.info("\nWalk-Forward Validation...")
        df = df.copy()
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df.sort_values("trade_date", inplace=True)

        cutoff = df["trade_date"].min() + pd.DateOffset(months=6)
        end    = df["trade_date"].max() - pd.DateOffset(months=3)
        fold   = 0
        wins   = []

        while cutoff <= end:
            fold += 1
            te_end = cutoff + pd.DateOffset(months=3)
            tr = df[df["trade_date"] < cutoff]
            te = df[(df["trade_date"] >= cutoff) & (df["trade_date"] < te_end)]

            if len(tr) < 300 or len(te) < 30:
                cutoff += pd.DateOffset(months=1)
                continue

            from xgboost import XGBClassifier
            from lightgbm import LGBMClassifier

            X_tr = tr[self.feat_cols].fillna(0).replace([np.inf,-np.inf],0)
            y_tr = tr["label_buy"]
            X_te = te[self.feat_cols].fillna(0).replace([np.inf,-np.inf],0)
            y_te = te["label_buy"]

            pos = max(y_tr.sum(),1); neg = len(y_tr)-pos
            xgb  = XGBClassifier(n_estimators=200, max_depth=5, learning_rate=0.05,
                                  scale_pos_weight=neg/pos, verbosity=0,
                                  eval_metric="logloss", random_state=42, n_jobs=-1)
            lgbm = LGBMClassifier(n_estimators=200, max_depth=5, learning_rate=0.05,
                                   class_weight="balanced", verbose=-1,
                                   random_state=42, n_jobs=-1)
            xgb.fit(X_tr, y_tr)
            lgbm.fit(X_tr, y_tr)

            proba = (xgb.predict_proba(X_te)[:,1] * 0.5 +
                     lgbm.predict_proba(X_te)[:,1] * 0.5)
            preds = (proba >= BUY_THRESHOLD).astype(int)

            buy_mask = preds == 1
            wr = float(y_te[buy_mask].mean()) if buy_mask.sum() > 0 else 0
            wins.append(wr)

            rets = te["next_day_return"].values
            brets = rets[buy_mask]
            gp = brets[brets > 0].sum()
            gl = abs(brets[brets < 0].sum())
            pf = gp / gl if gl > 0 else gp

            # Account for transaction costs
            net_wr = wr - TOTAL_COST_PCT * 2

            logger.info(
                f"  Fold {fold} [{cutoff.date()} → {te_end.date()}] "
                f"WinRate={wr:.1%} NetWinRate={net_wr:.1%} "
                f"PF={pf:.2f} Signals={buy_mask.sum()}"
            )
            cutoff += pd.DateOffset(months=1)

        if wins:
            logger.info(f"\nAverage win rate: {sum(wins)/len(wins):.1%} across {fold} folds")

    # ── SIGNAL GENERATION ─────────────────────────────────────
    def generate_signals(self, signal_date=None):
        if not self.xgb:
            logger.error("No trained model. Run: python ai_signal_engine.py --train")
            return []

        import pandas as pd

        signal_date = signal_date or date.today().strftime("%Y-%m-%d")
        target_date = self._next_trading_day(signal_date)

        logger.info(f"Generating signals for {target_date} (predicted from {signal_date})...")
        start  = db.now()

        cutoff = (date.fromisoformat(signal_date) - timedelta(days=365)).strftime("%Y-%m-%d")
        stocks = db.all("SELECT symbol FROM stocks WHERE is_active=1")
        frames = []

        for row in stocks:
            sym = row["symbol"]
            df  = build_features(sym, cutoff, label=False)
            if df is not None and len(df) > 0:
                last = df.iloc[[-1]].copy()
                frames.append(last)

        if not frames:
            logger.error("No features. Scrape data first.")
            return []

        feat_df = pd.concat(frames, ignore_index=True)

        # Align columns
        avail = [c for c in self.feat_cols if c in feat_df.columns]
        X = feat_df[avail].fillna(0).replace([np.inf,-np.inf],0)
        for col in [c for c in self.feat_cols if c not in avail]:
            X[col] = 0
        X = X[self.feat_cols]

        proba = (self.xgb.predict_proba(X)[:,1]  * 0.5 +
                 self.lgbm.predict_proba(X)[:,1] * 0.5)

        signals = []
        now     = db.now()

        for i, row in feat_df.iterrows():
            sym  = row.get("symbol","")
            conf = float(proba[i])
            ltp  = float(row.get("close_price", 0))
            atr  = float(row.get("ti_atr14", ltp * 0.025))
            atr_pct = float(row.get("ti_atr_pct", 2.5))
            rsi  = float(row.get("ti_rsi14", 50))
            macd_h = float(row.get("ti_macd_histogram", 0))
            vol_r  = float(row.get("vol_ratio20", 1))
            accum  = float(row.get("broker_accumulation_score", 0))
            beta   = self._compute_beta(sym, signal_date)

            # Fundamentals
            pe = float(row.get("fund_pe_ratio", 0))

            # Signal classification
            if conf >= BUY_THRESHOLD:
                signal = "BUY"
            elif conf <= SELL_THRESHOLD:
                signal = "SELL"
            else:
                signal = "HOLD"

            # Strategy & Wyckoff phase
            strategy = detect_strategy(dict(row))
            phase    = db.one("""
                SELECT wyckoff_phase FROM broker_concentration
                WHERE symbol=? AND trade_date<=?
                ORDER BY trade_date DESC LIMIT 1
            """, [sym, signal_date])
            phase_str = (phase.get("wyckoff_phase") if phase else None) or "Neutral"

            # Time series type
            close_hist = db.all("""
                SELECT close_price FROM daily_ohlcv
                WHERE symbol=? ORDER BY trade_date DESC LIMIT 30
            """, [sym])
            ts_type = detect_time_series_type(
                [float(r["close_price"]) for r in close_hist])

            # ATR-based stop loss
            sl = round(ltp - 1.5 * atr, 2)
            tp = round(ltp + 3.0 * atr, 2)
            rr = round((tp - ltp) / (ltp - sl + 1e-9), 2)

            # Price projections
            projections = compute_price_projections(
                sym, ltp, atr, conf * 100, signal, signal_date)
            upside_10d  = projections.get("upside_10d", tp)
            downside_10d= projections.get("downside_10d", sl)
            target_10d  = projections.get("target_10d", tp)

            # Key drivers
            drivers = self._explain(row, conf, signal)

            # Upside/downside probability
            up_prob   = round(conf * 100 if signal == "BUY" else (1-conf)*100, 1)
            down_prob = round(100 - up_prob, 1)

            signals.append({
                "signal_date":  signal_date,
                "target_date":  target_date,
                "symbol":       sym,
                "signal":       signal,
                "confidence_pct": round(conf * 100, 1),
                "ltp":          ltp,
                "target_10d":   target_10d,
                "target_20d":   projections.get("target_10d", tp),
                "stop_loss":    sl,
                "risk_reward":  rr,
                "upside_10d":   upside_10d,
                "downside_10d": downside_10d,
                "upside_prob_10d":   up_prob,
                "downside_prob_10d": down_prob,
                "strategy":     strategy,
                "wyckoff_phase":phase_str,
                "time_series":  ts_type,
                "rsi":          round(rsi, 1),
                "macd":         round(macd_h, 2),
                "volume_ratio": round(vol_r, 2),
                "accum_score":  round(accum, 1),
                "beta_3m":      round(beta, 2),
                "pe_ratio":     round(pe, 1),
                "atr_pct":      round(atr_pct, 2),
                "key_drivers":  drivers,
                "model_version":self.VERSION,
            })

        signals.sort(
            key=lambda x: (x["signal"]=="BUY", x["confidence_pct"]),
            reverse=True
        )

        self._save_signals(signals, now)
        self._export_signals(signals, signal_date)

        buy  = sum(1 for s in signals if s["signal"]=="BUY")
        sell = sum(1 for s in signals if s["signal"]=="SELL")
        logger.info(f"Signals: {buy} BUY | {sell} SELL | {len(signals)-buy-sell} HOLD")
        db.log("signals", "success", len(signals), None, start)
        return signals

    def _explain(self, row, conf, signal):
        reasons = []
        rsi   = float(row.get("ti_rsi14") or 50)
        accum = float(row.get("broker_accumulation_score") or 0)
        vol_r = float(row.get("vol_ratio20") or 1)
        macd_h= float(row.get("ti_macd_histogram") or 0)
        align = int(row.get("ema_align") or 2)
        bb_sq = int(row.get("ti_bb_squeeze") or 0)
        top3  = float(row.get("broker_top3_buy_pct") or 0)
        atr_p = float(row.get("ti_atr_pct") or 0)

        if rsi < 30:   reasons.append(f"RSI oversold ({rsi:.0f})")
        elif rsi < 40: reasons.append(f"RSI low ({rsi:.0f})")
        elif rsi > 70: reasons.append(f"RSI overbought ({rsi:.0f})")

        if macd_h > 0: reasons.append("MACD bullish")
        elif macd_h < 0: reasons.append("MACD bearish")

        if accum > 25:  reasons.append(f"Broker accumulation +{accum:.0f}")
        elif accum < -25: reasons.append(f"Broker distribution {accum:.0f}")

        if vol_r > 2.0: reasons.append(f"Volume spike {vol_r:.1f}x avg")

        if align == 4:  reasons.append("Price above all EMAs")
        elif align <= 1: reasons.append("Price below most EMAs")

        if bb_sq: reasons.append("BB squeeze — breakout loading")
        if top3 > 65: reasons.append(f"Top-3 brokers: {top3:.0f}% of buy volume")
        if atr_p > 3: reasons.append(f"High volatility (ATR {atr_p:.1f}%)")

        return reasons[:5]

    def _compute_beta(self, symbol, signal_date):
        """3-month beta vs NEPSE index."""
        cutoff = (date.fromisoformat(signal_date) - timedelta(days=90)).strftime("%Y-%m-%d")
        stock_rows = db.all("""
            SELECT close_price FROM daily_ohlcv
            WHERE symbol=? AND trade_date>=?
            ORDER BY trade_date ASC
        """, [symbol, cutoff])
        nepse_rows = db.all("""
            SELECT close_value FROM indices
            WHERE index_name='NEPSE' AND index_date>=?
            ORDER BY index_date ASC
        """, [cutoff])

        try:
            if len(stock_rows) < 5 or len(nepse_rows) < 5:
                return 1.0
            n = min(len(stock_rows), len(nepse_rows)) - 1
            sr = [(float(stock_rows[i+1]["close_price"]) /
                   float(stock_rows[i]["close_price"]) - 1) for i in range(n)]
            nr = [(float(nepse_rows[i+1]["close_value"]) /
                   float(nepse_rows[i]["close_value"]) - 1) for i in range(n)]
            cov  = sum((sr[i]-sum(sr)/n) * (nr[i]-sum(nr)/n) for i in range(n)) / n
            varn = sum((nr[i]-sum(nr)/n)**2 for i in range(n)) / n
            return round(cov / varn, 2) if varn > 0 else 1.0
        except Exception:
            return 1.0

    def _save_signals(self, signals, now):
        records = [(
            s["signal_date"], s["target_date"], s["symbol"],
            s["signal"], s["confidence_pct"], s["ltp"],
            s["target_10d"], s["target_20d"],
            s["stop_loss"], s["risk_reward"],
            s["upside_prob_10d"], s["downside_prob_10d"],
            s["strategy"], s["wyckoff_phase"], s["time_series"],
            s["rsi"], s["macd"], s["volume_ratio"],
            s["accum_score"], s["beta_3m"], s["pe_ratio"],
            json.dumps(s["key_drivers"]), s["model_version"],
            None, None, None, now,
        ) for s in signals]

        if records:
            db.runmany("""
                INSERT INTO ai_signals
                  (signal_date, target_date, symbol, signal,
                   confidence_pct, ltp, target_10d, target_20d,
                   stop_loss, risk_reward, upside_prob_10d, downside_prob_10d,
                   strategy, wyckoff_phase, time_series_type,
                   rsi_at_signal, macd_at_signal, volume_ratio_at_signal,
                   accumulation_score, beta_3m, pe_ratio,
                   key_drivers, model_version,
                   actual_close_10d, actual_pct_10d, signal_correct, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(signal_date, symbol) DO UPDATE SET
                    signal=excluded.signal,
                    confidence_pct=excluded.confidence_pct,
                    target_10d=excluded.target_10d
            """, records)

    def _export_signals(self, signals, signal_date):
        """Export to CSV in exports folder."""
        import csv, os
        path = os.path.join(EXPORTS_DIR, f"signals_{signal_date}.csv")
        if not signals:
            return
        fieldnames = list(signals[0].keys())
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                for s in signals:
                    row = {k: (", ".join(v) if isinstance(v, list) else v)
                           for k, v in s.items()}
                    w.writerow(row)
            logger.info(f"Signals exported: {path}")
        except Exception as e:
            logger.warning(f"Export error: {e}")

    # ── BACKTESTING ───────────────────────────────────────────
    def backtest(self, start_date=None, end_date=None):
        end_date   = end_date   or date.today().strftime("%Y-%m-%d")
        start_date = start_date or (date.today()-timedelta(days=90)).strftime("%Y-%m-%d")
        logger.info(f"Backtesting {start_date} → {end_date}...")

        pending = db.all("""
            SELECT id, symbol, signal, signal_date, target_date
            FROM ai_signals
            WHERE signal_date BETWEEN ? AND ?
              AND signal IN ('BUY','SELL') AND signal_correct IS NULL
        """, [start_date, end_date])

        updated = 0
        for sig in pending:
            actual = db.one("""
                SELECT close_price FROM daily_ohlcv
                WHERE symbol=? AND trade_date=?
            """, [sig["symbol"], sig["target_date"]])

            prev = db.one("""
                SELECT close_price FROM daily_ohlcv
                WHERE symbol=? AND trade_date<? ORDER BY trade_date DESC LIMIT 1
            """, [sig["symbol"], sig["target_date"]])

            if not actual or not prev or not prev.get("close_price"):
                continue

            pct = (actual["close_price"] / prev["close_price"] - 1) * 100
            # Account for transaction costs
            net_pct = pct - TOTAL_COST_PCT * 100 * 2
            ok = 1 if ((sig["signal"]=="BUY"  and net_pct >= 0) or
                       (sig["signal"]=="SELL" and net_pct <= 0)) else 0

            db.run("""
                UPDATE ai_signals SET
                    actual_close_10d=?, actual_pct_10d=?, signal_correct=?
                WHERE id=?
            """, [actual["close_price"], round(pct,3), ok, sig["id"]])
            updated += 1

        # Print summary
        stats = db.one("""
            SELECT COUNT(*) AS total,
                   SUM(signal='BUY')  AS buys,
                   SUM(signal='SELL') AS sells,
                   ROUND(AVG(signal_correct)*100,1) AS accuracy,
                   ROUND(AVG(CASE WHEN signal='BUY'  THEN signal_correct END)*100,1) AS buy_acc,
                   ROUND(AVG(CASE WHEN signal='SELL' THEN signal_correct END)*100,1) AS sell_acc,
                   ROUND(AVG(CASE WHEN signal_correct=1 THEN actual_pct_10d END),2) AS avg_win,
                   ROUND(AVG(CASE WHEN signal_correct=0 THEN actual_pct_10d END),2) AS avg_loss
            FROM ai_signals WHERE signal_correct IS NOT NULL
        """)

        if stats and stats.get("total",0) > 0:
            logger.info("\n=== BACKTEST RESULTS ===")
            logger.info(f"Total signals:   {stats['total']}")
            logger.info(f"Overall accuracy:{stats['accuracy']}%")
            logger.info(f"BUY accuracy:    {stats['buy_acc']}%")
            logger.info(f"SELL accuracy:   {stats['sell_acc']}%")
            logger.info(f"Avg win:         +{stats['avg_win']}%")
            logger.info(f"Avg loss:         {stats['avg_loss']}%")

        return dict(stats) if stats else {}

    # ── MODEL PERSISTENCE ─────────────────────────────────────
    def _save(self):
        joblib.dump(self.xgb,       MODELS_PATH / "xgb.pkl")
        joblib.dump(self.lgbm,      MODELS_PATH / "lgbm.pkl")
        joblib.dump(self.feat_cols, MODELS_PATH / "features.pkl")
        logger.info(f"Models saved to {MODELS_PATH}/")

    def _load(self):
        try:
            self.xgb       = joblib.load(MODELS_PATH / "xgb.pkl")
            self.lgbm      = joblib.load(MODELS_PATH / "lgbm.pkl")
            self.feat_cols = joblib.load(MODELS_PATH / "features.pkl")
            logger.info(f"Models loaded ({len(self.feat_cols)} features)")
        except FileNotFoundError:
            logger.info("No saved model — run: python ai_signal_engine.py --train")

    def _feature_importance(self, top_n=25):
        if not self.xgb or not self.feat_cols:
            return
        import pandas as pd
        avg = (self.xgb.feature_importances_ + self.lgbm.feature_importances_) / 2
        fi  = pd.Series(avg, index=self.feat_cols).sort_values(ascending=False)
        logger.info(f"\nTOP {top_n} FEATURES:")
        for i, (feat, score) in enumerate(fi.head(top_n).items(), 1):
            logger.info(f"  {i:2d}. {feat:<45s} {score:.4f}")

    def _next_trading_day(self, from_date):
        d = date.fromisoformat(from_date) + timedelta(days=1)
        for _ in range(14):
            if d.weekday() in MARKET_DAYS and d.strftime("%Y-%m-%d") not in NEPSE_HOLIDAYS:
                return d.strftime("%Y-%m-%d")
            d += timedelta(days=1)
        return d.strftime("%Y-%m-%d")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    model = NEPSEModel()

    if "--train" in sys.argv:
        lookback = 730
        for a in sys.argv:
            if a.startswith("--lookback="):
                lookback = int(a.split("=", 1)[1])
        model.train(lookback_days=lookback)

    elif "--signals" in sys.argv:
        date_arg = None
        for a in sys.argv:
            if a.startswith("2"):
                date_arg = a
        sigs = model.generate_signals(date_arg)
        buys = [s for s in sigs if s["signal"]=="BUY"]
        print(f"\n{'='*80}")
        print(f"{'Symbol':8} {'Signal':5} {'Conf':6} {'LTP':8} {'Target10d':10} {'SL':8} {'Strategy':20} {'Phase':15}")
        print(f"{'='*80}")
        for s in buys[:20]:
            print(f"{s['symbol']:8} {s['signal']:5} {s['confidence_pct']:5.1f}% "
                  f"Rs.{s['ltp']:7,.0f} Rs.{s['target_10d']:8,.0f} "
                  f"Rs.{s['stop_loss']:7,.0f} {s['strategy']:20} {s['wyckoff_phase']:15}")

    elif "--backtest" in sys.argv:
        model.backtest()

    else:
        print("Usage:")
        print("  python ai_signal_engine.py --train [--lookback=2190]")
        print("  python ai_signal_engine.py --signals")
        print("  python ai_signal_engine.py --backtest")
