"""
=============================================================
  NEPSE AI TRADER — app.py  (Streamlit Dashboard)
  Run:  streamlit run app.py
=============================================================
"""
import streamlit as st
import pandas as pd
import json
from datetime import date, timedelta
from db_manager import db

st.set_page_config(page_title="NEPSE AI Trader", page_icon="📈", layout="wide")

st.markdown("""
<style>
.buy-badge  {background:#16a34a;color:#fff;padding:3px 12px;
             border-radius:20px;font-weight:700;font-size:13px}
.sell-badge {background:#dc2626;color:#fff;padding:3px 12px;
             border-radius:20px;font-weight:700;font-size:13px}
.hold-badge {background:#d97706;color:#fff;padding:3px 12px;
             border-radius:20px;font-weight:700;font-size:13px}
</style>
""", unsafe_allow_html=True)

with st.sidebar:
    st.title("📈 NEPSE AI Trader")
    page = st.radio("Navigate", [
        "📡 Today's Signals",
        "📊 Market Overview",
        "🔍 Stock Deep Dive",
        "🏦 Broker Analysis",
        "📰 News & Sentiment",
        "📈 Backtest Results",
    ])
    today = date.today().strftime("%Y-%m-%d")
    st.divider()
    cnt = db.fetchone("SELECT COUNT(*) as c FROM stocks WHERE is_active=1")
    st.caption(f"📋 {cnt['c'] if cnt else 0} stocks tracked")
    st.caption(f"📅 {today}")


# ══════════════════════════════════════════
#  PAGE 1: TODAY'S SIGNALS
# ══════════════════════════════════════════
if page == "📡 Today's Signals":
    st.title("📡 Today's AI Trading Signals")

    stats = db.fetchone("""
        SELECT SUM(signal='BUY') AS buys,
               SUM(signal='SELL') AS sells,
               SUM(signal='HOLD') AS holds,
               ROUND(AVG(confidence_pct),1) AS avg_conf
        FROM ai_signals WHERE signal_date=?
    """, [today])

    c1,c2,c3,c4 = st.columns(4)
    c1.metric("🟢 BUY Signals",  stats["buys"]  or 0)
    c2.metric("🔴 SELL Signals", stats["sells"] or 0)
    c3.metric("🟡 HOLD Signals", stats["holds"] or 0)
    c4.metric("Avg Confidence",  f"{stats['avg_conf'] or 0}%")
    st.divider()

    fc1, fc2 = st.columns(2)
    sig_filter = fc1.selectbox("Signal", ["All","BUY","SELL","HOLD"])
    min_conf   = fc2.slider("Min Confidence %", 50, 95, 60)

    where = f"s.signal_date='{today}' AND s.confidence_pct>={min_conf}"
    if sig_filter != "All":
        where += f" AND s.signal='{sig_filter}'"

    rows = db.fetchall(f"""
        SELECT s.symbol, st.sector, s.signal, s.confidence_pct,
               s.stop_loss_price, s.target_price, s.risk_reward,
               s.key_drivers,
               lm.ltp, lm.pct_change, lm.volume
        FROM ai_signals s
        LEFT JOIN stocks st ON st.symbol=s.symbol
        LEFT JOIN (
            SELECT symbol, ltp, pct_change, volume
            FROM live_market
            WHERE snapshot_time=(SELECT MAX(snapshot_time) FROM live_market)
        ) lm ON lm.symbol=s.symbol
        WHERE {where}
        ORDER BY CASE s.signal WHEN 'BUY' THEN 0 WHEN 'SELL' THEN 1 ELSE 2 END,
                 s.confidence_pct DESC
    """)

    if not rows:
        st.info("No signals yet for today. Generate them with:")
        st.code("python run_scheduler.py --signals")
    else:
        for r in rows:
            badge = f"{r['signal'].lower()}-badge"
            ltp   = r.get("ltp") or 0
            c1,c2,c3,c4,c5,c6 = st.columns([1.5,1,1.2,1.2,1.2,1.2])
            c1.markdown(f"**{r['symbol']}**")
            c1.caption(r.get("sector") or "")
            c2.markdown(f"<span class='{badge}'>{r['signal']}</span>",
                        unsafe_allow_html=True)
            c2.caption(f"{r['confidence_pct']:.1f}% conf")
            c3.metric("LTP",       f"Rs.{ltp:,.2f}",
                      f"{r.get('pct_change',0):+.2f}%" if r.get("pct_change") else None)
            c4.metric("Stop Loss", f"Rs.{r['stop_loss_price']:,.2f}")
            c5.metric("Target",    f"Rs.{r['target_price']:,.2f}")
            rr = r.get("risk_reward") or 0
            c6.metric("Risk:Reward", f"1 : {rr:.1f}")

            try:
                drivers = json.loads(r["key_drivers"]) if r.get("key_drivers") else []
                if drivers:
                    st.caption("💡 " + "  ·  ".join(drivers))
            except Exception:
                pass
            st.divider()


# ══════════════════════════════════════════
#  PAGE 2: MARKET OVERVIEW
# ══════════════════════════════════════════
elif page == "📊 Market Overview":
    st.title("📊 Market Overview")

    indices = db.fetchall("""
        SELECT index_name, close_value, pct_change, turnover
        FROM indices WHERE index_date=?
        GROUP BY index_name
        ORDER BY CASE index_name WHEN 'NEPSE' THEN 0 ELSE 1 END, index_name
    """, [today])

    if indices:
        st.subheader("Indices")
        cols = st.columns(min(len(indices), 5))
        for i, idx in enumerate(indices[:5]):
            cols[i].metric(
                idx["index_name"],
                f"{idx['close_value']:,.2f}" if idx["close_value"] else "—",
                f"{idx['pct_change']:+.2f}%" if idx.get("pct_change") else None,
            )
        st.divider()

    g1, g2 = st.columns(2)
    with g1:
        st.subheader("🟢 Top Gainers")
        rows = db.fetchall("""
            SELECT symbol, value AS pct_change, ltp, volume
            FROM top_movers WHERE trade_date=? AND category='gainer'
            ORDER BY value DESC LIMIT 10
        """, [today])
        st.dataframe(pd.DataFrame(rows) if rows else pd.DataFrame(),
                     use_container_width=True, hide_index=True)

    with g2:
        st.subheader("🔴 Top Losers")
        rows = db.fetchall("""
            SELECT symbol, value AS pct_change, ltp, volume
            FROM top_movers WHERE trade_date=? AND category='loser'
            ORDER BY value ASC LIMIT 10
        """, [today])
        st.dataframe(pd.DataFrame(rows) if rows else pd.DataFrame(),
                     use_container_width=True, hide_index=True)

    st.subheader("💰 Top by Volume")
    rows = db.fetchall("""
        SELECT symbol, volume, ltp, pct_change
        FROM top_movers WHERE trade_date=? AND category='volume'
        ORDER BY volume DESC LIMIT 10
    """, [today])
    st.dataframe(pd.DataFrame(rows) if rows else pd.DataFrame(),
                 use_container_width=True, hide_index=True)


# ══════════════════════════════════════════
#  PAGE 3: STOCK DEEP DIVE
# ══════════════════════════════════════════
elif page == "🔍 Stock Deep Dive":
    st.title("🔍 Stock Deep Dive")

    stocks = db.fetchall("SELECT symbol FROM stocks WHERE is_active=1 ORDER BY symbol")
    sym    = st.selectbox("Select Stock", [r["symbol"] for r in stocks])

    if sym:
        ohlcv = db.fetchall("""
            SELECT trade_date, open_price, high_price, low_price,
                   close_price, volume
            FROM daily_ohlcv WHERE symbol=?
            ORDER BY trade_date DESC LIMIT 90
        """, [sym])

        if ohlcv:
            df = pd.DataFrame(ohlcv[::-1])
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            st.subheader(f"{sym} — Last 90 Days")

            tab1, tab2, tab3 = st.tabs(["📈 Price", "📊 Indicators", "🏦 Broker"])
            with tab1:
                st.line_chart(df.set_index("trade_date")["close_price"])
                st.bar_chart(df.set_index("trade_date")["volume"])

            with tab2:
                ti = db.fetchone("""
                    SELECT * FROM technical_indicators
                    WHERE symbol=? ORDER BY calc_date DESC LIMIT 1
                """, [sym])
                if ti:
                    m1,m2,m3,m4 = st.columns(4)
                    m1.metric("RSI (14)",        f"{ti.get('rsi14') or 0:.1f}")
                    m2.metric("Signal Strength",  f"{ti.get('signal_strength') or 0:.1f}")
                    m3.metric("Volume Ratio",     f"{ti.get('volume_ratio') or 0:.2f}×")
                    m4.metric("ADX",              f"{ti.get('adx') or 0:.1f}")
                    st.divider()
                    ca, cb = st.columns(2)
                    with ca:
                        st.markdown("**Moving Averages**")
                        st.write(f"EMA  9:  Rs.{ti.get('ema9') or 0:,.2f}")
                        st.write(f"EMA 21:  Rs.{ti.get('ema21') or 0:,.2f}")
                        st.write(f"EMA 50:  Rs.{ti.get('ema50') or 0:,.2f}")
                        st.write(f"EMA 200: Rs.{ti.get('ema200') or 0:,.2f}")
                    with cb:
                        st.markdown("**Bollinger Bands**")
                        st.write(f"Upper:  Rs.{ti.get('bb_upper') or 0:,.2f}")
                        st.write(f"Middle: Rs.{ti.get('bb_middle') or 0:,.2f}")
                        st.write(f"Lower:  Rs.{ti.get('bb_lower') or 0:,.2f}")
                        sq = "🔴 YES — breakout expected!" if ti.get("bb_squeeze") else "No"
                        st.write(f"Squeeze: {sq}")
                else:
                    st.info("No indicator data yet for this stock.")

            with tab3:
                bc = db.fetchall("""
                    SELECT trade_date, top3_buy_pct, top3_sell_pct,
                           unique_buyers, unique_sellers, accumulation_score
                    FROM broker_concentration
                    WHERE symbol=? ORDER BY trade_date DESC LIMIT 20
                """, [sym])
                if bc:
                    st.dataframe(pd.DataFrame(bc),
                                 use_container_width=True, hide_index=True)
                else:
                    st.info("No broker data yet.")
        else:
            st.info("No price history found. Run --setup to collect data.")


# ══════════════════════════════════════════
#  PAGE 4: BROKER ANALYSIS
# ══════════════════════════════════════════
elif page == "🏦 Broker Analysis":
    st.title("🏦 Broker Accumulation Signals")
    st.caption("Stocks where concentrated broker buying has been detected")

    days = st.slider("Look-back days", 1, 30, 5)
    cutoff = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")

    rows = db.fetchall("""
        SELECT symbol,
               ROUND(AVG(accumulation_score),2) AS avg_score,
               ROUND(AVG(top3_buy_pct),1)        AS avg_top3_buy,
               ROUND(AVG(top3_sell_pct),1)       AS avg_top3_sell,
               ROUND(AVG(unique_buyers),1)        AS avg_buyers,
               COUNT(*) AS days
        FROM broker_concentration
        WHERE trade_date>=?
        GROUP BY symbol
        ORDER BY avg_score DESC LIMIT 30
    """, [cutoff])

    if rows:
        df = pd.DataFrame(rows)
        df.columns = ["Symbol","Score","Top3 Buy%","Top3 Sell%","Avg Buyers","Days"]
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.info("**Score > +20** = concentrated buying (potential accumulation)\n\n"
                "**Top3 Buy% > 60** = 3 brokers dominating the buy side")
    else:
        st.info("No broker data yet. The scheduler collects this after market close.")


# ══════════════════════════════════════════
#  PAGE 5: NEWS & SENTIMENT
# ══════════════════════════════════════════
elif page == "📰 News & Sentiment":
    st.title("📰 Market News & Sentiment")

    rows = db.fetchall("""
        SELECT headline, source, category, sentiment_score,
               sentiment_label, published_at
        FROM news ORDER BY published_at DESC LIMIT 50
    """)

    if rows:
        for r in rows:
            score = r.get("sentiment_score") or 0
            label = r.get("sentiment_label","neutral")
            icon  = "🟢" if label=="positive" else "🔴" if label=="negative" else "⚪"
            with st.expander(f"{icon}  {r['headline'][:100]}"):
                c1,c2,c3 = st.columns(3)
                c1.caption(f"📰 {r.get('source','')}")
                c2.caption(f"🏷️ {r.get('category','')}")
                c3.caption(f"Sentiment: {score:+.2f} ({label})")
    else:
        st.info("No news yet. The scheduler collects news every 30 minutes.")


# ══════════════════════════════════════════
#  PAGE 6: BACKTEST RESULTS
# ══════════════════════════════════════════
elif page == "📈 Backtest Results":
    st.title("📈 AI Signal Accuracy")

    stats = db.fetchone("""
        SELECT COUNT(*) AS total,
               ROUND(AVG(signal_correct)*100,1) AS accuracy,
               ROUND(AVG(CASE WHEN signal='BUY' THEN signal_correct END)*100,1) AS buy_acc,
               ROUND(AVG(CASE WHEN signal='SELL' THEN signal_correct END)*100,1) AS sell_acc,
               ROUND(AVG(CASE WHEN signal_correct=1 THEN actual_pct_change END),2) AS avg_win,
               ROUND(AVG(CASE WHEN signal_correct=0 THEN actual_pct_change END),2) AS avg_loss
        FROM ai_signals WHERE signal_correct IS NOT NULL
    """)

    if stats and stats.get("total",0) > 0:
        m1,m2,m3,m4,m5 = st.columns(5)
        m1.metric("Total Evaluated", stats["total"])
        m2.metric("Overall Accuracy", f"{stats.get('accuracy',0)}%")
        m3.metric("BUY Accuracy",     f"{stats.get('buy_acc',0)}%")
        m4.metric("Avg Win",          f"+{stats.get('avg_win',0)}%")
        m5.metric("Avg Loss",         f"{stats.get('avg_loss',0)}%")

        recent = db.fetchall("""
            SELECT signal_date, symbol, signal, confidence_pct,
                   actual_pct_change, signal_correct
            FROM ai_signals
            WHERE signal_correct IS NOT NULL
            ORDER BY signal_date DESC LIMIT 100
        """)
        if recent:
            df = pd.DataFrame(recent)
            df["✅/❌"] = df["signal_correct"].map({1:"✅",0:"❌"})
            st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("Backtest data accumulates automatically each trading day.")
        st.markdown("""
        **How it works:**
        1. AI generates signals at 4:00 PM each day
        2. At 4:30 PM next day, actual price is checked
        3. Signal marked ✅ if stock moved ≥2% in predicted direction
        """)
