"""
=============================================================
  NEPSE AI TRADER — ai_model.py
  XGBoost + LightGBM ensemble. Flat import version.
  Usage:
    python run_scheduler.py --train
    python run_scheduler.py --signals
    python run_scheduler.py --backtest
=============================================================
"""
import os, sys, logging, json, joblib
import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta
from pathlib import Path

from db_manager import db
from settings import MARKET_DAYS, NEPSE_HOLIDAYS

logger = logging.getLogger(__name__)
MODEL_DIR = Path("saved_models")
MODEL_DIR.mkdir(exist_ok=True)

META = {"symbol","trade_date","label_buy","label_sell",
        "next_day_return","close_price","open_price",
        "high_price","low_price","adj_close","volume","turnover","total_trades"}


# ─────────────────────────────────────────────────────────────
#  FEATURE ENGINEERING  (inline — no separate file needed)
# ─────────────────────────────────────────────────────────────
def build_features_for_symbol(symbol: str, cutoff: str,
                               label: bool = True):
    """Build feature DataFrame for one stock."""
    try:
        import pandas_ta as ta
    except ImportError:
        logger.error("pip install pandas-ta")
        return None

    rows = db.fetchall("""
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
    df = df.astype(float)
    df["symbol"] = symbol

    c = df["close_price"]
    v = df["volume"]

    # ── Price returns ─────────────────────────────────────────
    for n in [1,3,5,10,20]:
        df[f"ret_{n}d"] = c.pct_change(n)

    df["gap_pct"]       = df["open_price"] / c.shift(1) - 1
    df["day_range_pct"] = (df["high_price"] - df["low_price"]) / c.shift(1).replace(0,np.nan)

    # Volume features
    df["vol_ma5"]    = v.rolling(5).mean()
    df["vol_ma20"]   = v.rolling(20).mean()
    df["vol_ratio5"] = v / df["vol_ma5"].replace(0,np.nan)
    df["vol_spike"]  = (v > df["vol_ma20"] * 2).astype(int)

    # Price position
    df["high_20d"]      = df["high_price"].rolling(20).max()
    df["low_20d"]       = df["low_price"].rolling(20).min()
    df["price_pos_20d"] = (c - df["low_20d"]) / (df["high_20d"] - df["low_20d"] + 1e-9)
    df["pct_from_52h"]  = c / df["high_price"].rolling(252).max() - 1

    # Consecutive up days
    df["up_day"] = (df["ret_1d"] > 0).astype(int)

    # ── Technical indicators ──────────────────────────────────
    ti_rows = db.fetchall("""
        SELECT calc_date, ema9, ema21, ema50, ema200,
               macd_line, macd_signal, macd_histogram, adx,
               rsi14, stoch_k, stoch_d, cci14, williams_r,
               obv, volume_ratio, cmf, atr14,
               bb_upper, bb_middle, bb_lower, bb_pct_b, bb_squeeze,
               signal_strength
        FROM technical_indicators
        WHERE symbol=? AND calc_date>=?
        ORDER BY calc_date ASC
    """, [symbol, cutoff])

    if ti_rows:
        ti = pd.DataFrame(ti_rows).set_index("calc_date").astype(float)
        for col in ti.columns:
            df[f"ti_{col}"] = ti[col].reindex(df.index)

        # Derived
        df["ema_align"] = (
            (c > df.get("ti_ema9",  pd.Series(0,index=df.index))).astype(int) +
            (c > df.get("ti_ema21", pd.Series(0,index=df.index))).astype(int) +
            (c > df.get("ti_ema50", pd.Series(0,index=df.index))).astype(int) +
            (c > df.get("ti_ema200",pd.Series(0,index=df.index))).astype(int)
        )
        if "ti_rsi14" in df:
            df["rsi_oversold"]   = (df["ti_rsi14"] < 30).astype(int)
            df["rsi_overbought"] = (df["ti_rsi14"] > 70).astype(int)

    # ── Broker concentration ──────────────────────────────────
    bc_rows = db.fetchall("""
        SELECT trade_date, top3_buy_pct, top3_sell_pct,
               unique_buyers, unique_sellers, accumulation_score
        FROM broker_concentration
        WHERE symbol=? AND trade_date>=?
        ORDER BY trade_date ASC
    """, [symbol, cutoff])

    if bc_rows:
        bc = pd.DataFrame(bc_rows).set_index("trade_date").astype(float)
        for col in bc.columns:
            df[f"broker_{col}"] = bc[col].reindex(df.index)
        if "broker_accumulation_score" in df:
            df["broker_accum_5d"] = df["broker_accumulation_score"].rolling(5).mean()
            df["broker_trend"] = (
                df["broker_accumulation_score"] > df["broker_accum_5d"]).astype(int)

    # ── News sentiment ────────────────────────────────────────
    df["news_sent_3d"]  = 0.0
    df["news_count_3d"] = 0

    # ── Label ─────────────────────────────────────────────────
    if label:
        df["next_day_return"] = c.shift(-1) / c - 1
        df["label_buy"]  = (df["next_day_return"] >= 0.02).astype(int)
        df["label_sell"] = (df["next_day_return"] <= -0.02).astype(int)
        df.dropna(subset=["label_buy"], inplace=True)

    df.reset_index(inplace=True)
    df.rename(columns={"index":"trade_date"}, errors="ignore")
    df.fillna(0, inplace=True)
    return df


# ─────────────────────────────────────────────────────────────
#  AI MODEL CLASS
# ─────────────────────────────────────────────────────────────
class NEPSEAIModel:
    VERSION = "1.0"

    def __init__(self):
        self.xgb = None
        self.lgbm = None
        self.feat_cols = []
        self._load()

    # ── Training ──────────────────────────────────────────────
    def train(self, lookback_days: int = 730):
        logger.info("=" * 50)
        logger.info("  🤖 TRAINING NEPSE AI MODEL")
        logger.info("=" * 50)

        cutoff = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        stocks = db.fetchall("SELECT symbol FROM stocks WHERE is_active=1")
        frames = []

        for i, row in enumerate(stocks):
            sym = row["symbol"]
            df  = build_features_for_symbol(sym, cutoff, label=True)
            if df is not None and len(df) >= 60:
                frames.append(df)
            if i % 30 == 0:
                logger.info(f"  Features built: {i}/{len(stocks)} stocks")

        if not frames:
            logger.error("No data! Run --setup first to collect historical data.")
            return

        data = pd.concat(frames, ignore_index=True)
        self.feat_cols = [c for c in data.columns if c not in META]
        logger.info(f"Dataset: {len(data):,} rows × {len(self.feat_cols)} features")

        # Walk-forward validation
        self._walk_forward(data)

        # Final model on all data
        X = data[self.feat_cols].fillna(0).replace([np.inf,-np.inf], 0)
        y = data["label_buy"]
        self._fit(X, y)
        self._save()
        self._feature_importance()

    def _walk_forward(self, df: pd.DataFrame):
        logger.info("\n📊 Walk-Forward Validation…")
        df = df.copy()
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df.sort_values("trade_date", inplace=True)

        cutoff = df["trade_date"].min() + pd.DateOffset(months=6)
        end    = df["trade_date"].max() - pd.DateOffset(months=3)
        fold   = 0

        while cutoff <= end:
            fold += 1
            te_end   = cutoff + pd.DateOffset(months=3)
            tr = df[df["trade_date"] < cutoff]
            te = df[(df["trade_date"] >= cutoff) & (df["trade_date"] < te_end)]

            if len(tr) < 300 or len(te) < 30:
                cutoff += pd.DateOffset(months=1)
                continue

            X_tr = tr[self.feat_cols].fillna(0).replace([np.inf,-np.inf],0)
            X_te = te[self.feat_cols].fillna(0).replace([np.inf,-np.inf],0)
            y_tr, y_te = tr["label_buy"], te["label_buy"]

            xgb, lgbm = self._fit(X_tr, y_tr, save=False)
            proba = (xgb.predict_proba(X_te)[:,1] * 0.5 +
                     lgbm.predict_proba(X_te)[:,1] * 0.5)
            preds = (proba >= 0.55).astype(int)

            buy_mask = preds == 1
            win_rate = y_te[buy_mask].mean() if buy_mask.sum() > 0 else 0
            rets     = te["next_day_return"].values
            buy_rets = rets[buy_mask]
            gp = buy_rets[buy_rets>0].sum()
            gl = abs(buy_rets[buy_rets<0].sum())
            pf = gp/gl if gl > 0 else gp

            logger.info(
                f"  Fold {fold} [{cutoff.date()} → {te_end.date()}] "
                f"WinRate={win_rate:.1%}  PF={pf:.2f}  Signals={buy_mask.sum()}"
            )
            cutoff += pd.DateOffset(months=1)

    def _fit(self, X, y, save=True):
        from xgboost import XGBClassifier
        from lightgbm import LGBMClassifier

        pos = max(y.sum(), 1)
        neg = len(y) - pos
        scale = neg / pos

        xgb = XGBClassifier(
            n_estimators=300, max_depth=5, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            scale_pos_weight=scale,
            eval_metric="logloss", verbosity=0, random_state=42, n_jobs=-1,
        )
        lgbm = LGBMClassifier(
            n_estimators=300, max_depth=5, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            class_weight="balanced", verbose=-1, random_state=42, n_jobs=-1,
        )
        xgb.fit(X, y)
        lgbm.fit(X, y)

        if save:
            self.xgb  = xgb
            self.lgbm = lgbm
            logger.info(f"✅ Models trained on {len(X):,} samples")

        return xgb, lgbm

    # ── Signal Generation ─────────────────────────────────────
    def generate_signals(self, signal_date: str = None) -> list:
        if not self.xgb:
            logger.error("No trained model — run: python run_scheduler.py --train")
            return []

        signal_date = signal_date or date.today().strftime("%Y-%m-%d")
        target_date = self._next_trading_day(signal_date)
        logger.info(f"🔮 Generating signals for {target_date}…")

        cutoff = (date.fromisoformat(signal_date) -
                  timedelta(days=365)).strftime("%Y-%m-%d")
        stocks = db.fetchall("SELECT symbol FROM stocks WHERE is_active=1")
        frames = []

        for row in stocks:
            df = build_features_for_symbol(row["symbol"], cutoff, label=False)
            if df is not None and len(df) > 0:
                frames.append(df.iloc[[-1]].copy())

        if not frames:
            return []

        feat_df = pd.concat(frames, ignore_index=True)
        avail   = [c for c in self.feat_cols if c in feat_df.columns]
        X = feat_df[avail].fillna(0).replace([np.inf,-np.inf],0)
        for col in [c for c in self.feat_cols if c not in avail]:
            X[col] = 0
        X = X[self.feat_cols]

        proba = (self.xgb.predict_proba(X)[:,1]  * 0.5 +
                 self.lgbm.predict_proba(X)[:,1] * 0.5)

        signals = []
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for i, row in feat_df.iterrows():
            sym  = row["symbol"]
            conf = float(proba[i])
            ltp  = float(row.get("close_price", 0))
            atr  = float(row.get("ti_atr14", ltp * 0.03))

            signal = "BUY" if conf >= 0.60 else "SELL" if conf <= 0.35 else "HOLD"
            sl = round(ltp - 1.5 * atr, 2)
            tp = round(ltp + 3.0 * atr, 2)
            rr = round((tp - ltp) / (ltp - sl + 1e-9), 2)

            drivers = self._drivers(row, conf)
            signals.append({
                "signal_date":     signal_date,
                "target_date":     target_date,
                "symbol":          sym,
                "signal":          signal,
                "confidence_pct":  round(conf * 100, 1),
                "ltp":             ltp,
                "stop_loss_price": sl,
                "target_price":    tp,
                "risk_reward":     rr,
                "key_drivers":     drivers,
                "model_version":   self.VERSION,
            })

        signals.sort(key=lambda x: (x["signal"]=="BUY", x["confidence_pct"]), reverse=True)
        self._save_signals(signals, now)

        buy  = sum(1 for s in signals if s["signal"]=="BUY")
        sell = sum(1 for s in signals if s["signal"]=="SELL")
        logger.info(f"✅ {buy} BUY | {sell} SELL | {len(signals)-buy-sell} HOLD")
        return signals

    def _drivers(self, row, conf) -> list:
        reasons = []
        rsi    = row.get("ti_rsi14", 50)
        accum  = row.get("broker_accumulation_score", 0)
        vol_r  = row.get("vol_ratio5", 1)
        macd_h = row.get("ti_macd_histogram", 0)
        align  = row.get("ema_align", 0)

        if   rsi < 35:  reasons.append(f"RSI oversold ({rsi:.0f})")
        elif rsi > 70:  reasons.append(f"RSI overbought ({rsi:.0f})")
        if accum > 20:  reasons.append(f"Broker accumulation +{accum:.1f}")
        elif accum < -20: reasons.append(f"Broker distribution {accum:.1f}")
        if vol_r > 2:   reasons.append(f"Volume spike {vol_r:.1f}×")
        if macd_h > 0:  reasons.append("MACD bullish histogram")
        elif macd_h < 0: reasons.append("MACD bearish histogram")
        if align == 4:  reasons.append("Price above all EMAs")
        elif align <= 1: reasons.append("Price below most EMAs")
        if row.get("ti_bb_squeeze", 0): reasons.append("BB squeeze — breakout soon")
        return reasons[:5]

    def _save_signals(self, signals, now):
        records = [(
            s["signal_date"], s["target_date"], s["symbol"],
            s["signal"], s["confidence_pct"], None,
            s["stop_loss_price"], s["target_price"], s["risk_reward"],
            json.dumps(s["key_drivers"]), s["model_version"],
            None, None, None, now,
        ) for s in signals]

        if records:
            db.executemany("""
                INSERT INTO ai_signals
                  (signal_date, target_date, symbol, signal,
                   confidence_pct, predicted_move_pct,
                   stop_loss_price, target_price, risk_reward,
                   key_drivers, model_version,
                   actual_close, actual_pct_change, signal_correct, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(signal_date, symbol) DO UPDATE SET
                    signal=excluded.signal,
                    confidence_pct=excluded.confidence_pct
            """, records)

    # ── Backtesting ───────────────────────────────────────────
    def backtest(self, start_date=None, end_date=None):
        end_date   = end_date   or date.today().strftime("%Y-%m-%d")
        start_date = start_date or (date.today()-timedelta(days=90)).strftime("%Y-%m-%d")
        logger.info(f"📊 Backtesting {start_date} → {end_date}")

        pending = db.fetchall("""
            SELECT id, symbol, signal, target_date
            FROM ai_signals
            WHERE signal_date BETWEEN ? AND ?
              AND signal IN ('BUY','SELL')
              AND actual_close IS NULL
        """, [start_date, end_date])

        updated = 0
        for sig in pending:
            actual = db.fetchone(
                "SELECT close_price FROM daily_ohlcv WHERE symbol=? AND trade_date=?",
                [sig["symbol"], sig["target_date"]])
            prev = db.fetchone("""
                SELECT close_price FROM daily_ohlcv
                WHERE symbol=? AND trade_date<? ORDER BY trade_date DESC LIMIT 1
            """, [sig["symbol"], sig["target_date"]])

            if not actual or not prev or not prev["close_price"]:
                continue

            pct = (actual["close_price"] / prev["close_price"] - 1) * 100
            ok  = 1 if (sig["signal"]=="BUY" and pct>=2) or \
                       (sig["signal"]=="SELL" and pct<=-2) else 0

            db.execute("""
                UPDATE ai_signals SET
                    actual_close=?, actual_pct_change=?, signal_correct=?
                WHERE id=?
            """, [actual["close_price"], round(pct,3), ok, sig["id"]])
            updated += 1

        stats = db.fetchone("""
            SELECT COUNT(*) AS total,
                   ROUND(AVG(signal_correct)*100,1) AS accuracy,
                   ROUND(AVG(CASE WHEN signal='BUY' THEN signal_correct END)*100,1) AS buy_acc,
                   ROUND(AVG(CASE WHEN signal_correct=1 THEN actual_pct_change END),2) AS avg_win,
                   ROUND(AVG(CASE WHEN signal_correct=0 THEN actual_pct_change END),2) AS avg_loss
            FROM ai_signals
            WHERE signal_date BETWEEN ? AND ?
              AND signal_correct IS NOT NULL
        """, [start_date, end_date])

        if stats and stats["total"]:
            logger.info(f"📈 Results: {stats['total']} signals | "
                        f"Accuracy={stats['accuracy']}% | "
                        f"BUY={stats['buy_acc']}% | "
                        f"AvgWin=+{stats['avg_win']}% | "
                        f"AvgLoss={stats['avg_loss']}%")
        return dict(stats) if stats else {}

    def _feature_importance(self, top_n=20):
        if not self.xgb or not self.feat_cols:
            return
        avg = (self.xgb.feature_importances_ + self.lgbm.feature_importances_) / 2
        fi  = pd.Series(avg, index=self.feat_cols).sort_values(ascending=False)
        logger.info(f"\n🔑 TOP {top_n} FEATURES:")
        for i, (f, s) in enumerate(fi.head(top_n).items(), 1):
            logger.info(f"  {i:2d}. {f:<40s} {s:.4f}")

    # ── Persistence ───────────────────────────────────────────
    def _save(self):
        joblib.dump(self.xgb,       MODEL_DIR / "xgb.pkl")
        joblib.dump(self.lgbm,      MODEL_DIR / "lgbm.pkl")
        joblib.dump(self.feat_cols, MODEL_DIR / "features.pkl")
        logger.info(f"✅ Models saved to {MODEL_DIR}/")

    def _load(self):
        try:
            self.xgb       = joblib.load(MODEL_DIR / "xgb.pkl")
            self.lgbm      = joblib.load(MODEL_DIR / "lgbm.pkl")
            self.feat_cols = joblib.load(MODEL_DIR / "features.pkl")
            logger.info(f"✅ Loaded models ({len(self.feat_cols)} features)")
        except FileNotFoundError:
            logger.info("No saved model found — run: python run_scheduler.py --train")

    def _next_trading_day(self, from_date: str) -> str:
        d = date.fromisoformat(from_date) + timedelta(days=1)
        for _ in range(14):
            if d.weekday() in MARKET_DAYS and d.strftime("%Y-%m-%d") not in NEPSE_HOLIDAYS:
                return d.strftime("%Y-%m-%d")
            d += timedelta(days=1)
        return d.strftime("%Y-%m-%d")
