"""
NEPSE AI — dashboard.py
Professional signal dashboard with all indicators.
Run locally:  streamlit run dashboard.py
Deployed on:  Streamlit Cloud (reads DATABASE_URL from st.secrets)
"""
import os
import streamlit as st
import pandas as pd
import json
from datetime import date, timedelta

# ── Inject DATABASE_URL from Streamlit secrets when running on Streamlit Cloud
# This must happen BEFORE db_manager is imported so it sees the env variable
if hasattr(st, "secrets") and "DATABASE_URL" in st.secrets:
    os.environ["DATABASE_URL"] = st.secrets["DATABASE_URL"]

from db_manager import db
from settings import *

st.set_page_config(
    page_title="NEPSE AI Signals",
    page_icon="N",
    layout="wide",
)

# ── Minimal CSS ───────────────────────────────────────────────
st.markdown("""
<style>
.buy   {background:#16a34a;color:#fff;padding:2px 10px;border-radius:4px;
        font-weight:600;font-size:12px;display:inline-block}
.sell  {background:#dc2626;color:#fff;padding:2px 10px;border-radius:4px;
        font-weight:600;font-size:12px;display:inline-block}
.hold  {background:#d97706;color:#fff;padding:2px 10px;border-radius:4px;
        font-weight:600;font-size:12px;display:inline-block}
.phase-mu   {color:#16a34a;font-weight:600}
.phase-dc   {color:#dc2626;font-weight:600}
.phase-acc  {color:#2563eb;font-weight:600}
.phase-dis  {color:#d97706;font-weight:600}
.phase-neu  {color:#6b7280}
.up    {color:#16a34a;font-weight:500}
.dn    {color:#dc2626;font-weight:500}
.bar-wrap {height:6px;background:#e5e7eb;border-radius:3px;width:60px;display:inline-block;vertical-align:middle;margin-left:4px}
.bar-buy  {height:6px;background:#16a34a;border-radius:3px}
.bar-sell {height:6px;background:#dc2626;border-radius:3px}
.bar-hold {height:6px;background:#d97706;border-radius:3px}
.disclaimer {font-size:11px;color:#9ca3af;text-align:center;font-style:italic;margin-bottom:8px}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────
#  DATA HELPERS
# ─────────────────────────────────────────────────────────────
def load_signals(signal_date, min_conf=0, sig_filter="All",
                 strat_filter="All", phase_filter="All", sector_filter="All"):
    where = [f"s.signal_date='{signal_date}'",
             f"s.confidence_pct>={min_conf}"]
    if sig_filter != "All":
        where.append(f"s.signal='{sig_filter}'")
    if strat_filter != "All":
        where.append(f"s.strategy='{strat_filter}'")
    if phase_filter != "All":
        where.append(f"s.wyckoff_phase='{phase_filter}'")
    if sector_filter != "All":
        where.append(f"st.sector='{sector_filter}'")

    return db.all(f"""
        SELECT s.symbol, st.sector,
               s.signal, s.confidence_pct,
               s.ltp, s.stop_loss, s.risk_reward,
               s.target_10d, s.target_20d,
               s.upside_prob_10d, s.downside_prob_10d,
               s.strategy, s.wyckoff_phase, s.time_series_type,
               s.rsi_at_signal, s.macd_at_signal,
               s.volume_ratio_at_signal, s.accumulation_score,
               s.beta_3m, s.pe_ratio, s.atr_pct,
               s.key_drivers, s.signal_date, s.target_date,
               -- latest live price
               lm.ltp AS live_ltp, lm.pct_change AS live_pct,
               -- broker data
               bc.top3_buy_pct, bc.unique_buyers, bc.avg_trade_size,
               bc.large_trades_pct
        FROM ai_signals s
        LEFT JOIN stocks st ON st.symbol=s.symbol
        LEFT JOIN (
            SELECT symbol, ltp, pct_change FROM live_market
            WHERE snapshot_time=(SELECT MAX(snapshot_time) FROM live_market)
        ) lm ON lm.symbol=s.symbol
        LEFT JOIN (
            SELECT symbol, top3_buy_pct, unique_buyers,
                   avg_trade_size, large_trades_pct
            FROM broker_concentration
            WHERE trade_date=(SELECT MAX(trade_date) FROM broker_concentration)
        ) bc ON bc.symbol=s.symbol
        WHERE {' AND '.join(where)}
        ORDER BY
            CASE s.signal WHEN 'BUY' THEN 0 WHEN 'SELL' THEN 1 ELSE 2 END,
            s.confidence_pct DESC
    """)


def fmt_phase(phase):
    cls = {"Mark-up":"phase-mu","Decline":"phase-dc",
           "Accumulation":"phase-acc","Distribution":"phase-dis"}.get(phase,"phase-neu")
    return f'<span class="{cls}">{phase or "—"}</span>'

def fmt_signal(sig):
    cls = {"BUY":"buy","SELL":"sell","HOLD":"hold"}.get(sig,"hold")
    return f'<span class="{cls}">{sig}</span>'

def fmt_conf(conf, sig):
    bar_cls = {"BUY":"bar-buy","SELL":"bar-sell","HOLD":"bar-hold"}.get(sig,"bar-hold")
    pct = int(conf or 0)
    return (f'{pct}% <span class="bar-wrap">'
            f'<div class="{bar_cls}" style="width:{pct}%"></div></span>')

def fmt_pct(v):
    if v is None: return "—"
    v = float(v)
    cls = "up" if v > 0 else "dn"
    return f'<span class="{cls}">{v:+.1f}%</span>'

def fmt_rsi(v):
    if v is None: return "—"
    v = float(v)
    cls = "dn" if v > 65 else ("up" if v < 35 else "")
    return f'<span class="{cls}">{v:.0f}</span>'

def fmt_vol(v):
    if v is None: return "—"
    v = float(v)
    cls = "up" if v > 1.5 else ""
    return f'<span class="{cls}">{v:.1f}x</span>'

def fmt_price(v):
    if v is None or float(v) == 0: return "—"
    return f"Rs.{float(v):,.0f}"


def col(r, k, default=None):
    return r.get(k) if r.get(k) is not None else default


# ─────────────────────────────────────────────────────────────
#  SIDEBAR
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## NEPSE AI Trader")
    st.markdown("---")

    page = st.radio("Navigate", [
        "Signal Dashboard",
        "Market Overview",
        "Stock Deep Dive",
        "Broker Analysis",
        "Backtest Results",
        "News & Events",
    ])

    st.markdown("---")
    today = date.today().strftime("%Y-%m-%d")

    # Available signal dates
    dates = db.all("""
        SELECT DISTINCT signal_date FROM ai_signals
        ORDER BY signal_date DESC LIMIT 30
    """)
    date_list = [r["signal_date"] for r in dates] or [today]
    selected_date = st.selectbox("Signal Date", date_list)

    cnt = db.one("SELECT COUNT(*) as c FROM stocks WHERE is_active=1")
    ohlcv = db.one("SELECT COUNT(*) as c FROM daily_ohlcv")
    sig_cnt = db.one(f"SELECT COUNT(*) as c FROM ai_signals WHERE signal_date='{selected_date}'")

    st.markdown(f"**Stocks tracked:** {cnt['c'] if cnt else 0}")
    st.markdown(f"**OHLCV records:** {ohlcv['c']:,}" if ohlcv and ohlcv['c'] else "**OHLCV:** 0")
    st.markdown(f"**Today's signals:** {sig_cnt['c'] if sig_cnt else 0}")


# ═══════════════════════════════════════════════════════════════
#  PAGE 1: SIGNAL DASHBOARD
# ═══════════════════════════════════════════════════════════════
if page == "Signal Dashboard":
    st.markdown('<div class="disclaimer">Signals are 100% Computer Generated for educational purposes. This is NOT a buy/sell recommendation.</div>',
                unsafe_allow_html=True)

    # ── Summary metrics ───────────────────────────────────────
    stats = db.one(f"""
        SELECT SUM(signal='BUY') AS buys, SUM(signal='SELL') AS sells,
               SUM(signal='HOLD') AS holds,
               ROUND(AVG(confidence_pct),1) AS avg_conf,
               ROUND(AVG(CASE WHEN signal='BUY' THEN confidence_pct END),1) AS buy_conf
        FROM ai_signals WHERE signal_date='{selected_date}'
    """)
    if stats:
        c1,c2,c3,c4,c5 = st.columns(5)
        c1.metric("BUY Signals",  stats.get("buys") or 0)
        c2.metric("SELL Signals", stats.get("sells") or 0)
        c3.metric("HOLD Signals", stats.get("holds") or 0)
        c4.metric("Avg Confidence", f"{stats.get('avg_conf') or 0}%")
        c5.metric("BUY Avg Conf",   f"{stats.get('buy_conf') or 0}%")

    st.divider()

    # ── Filters ───────────────────────────────────────────────
    fc1,fc2,fc3,fc4,fc5 = st.columns(5)
    sig_f   = fc1.selectbox("Signal",   ["All","BUY","SELL","HOLD"])
    min_c   = fc2.slider("Min Conf%",   0, 90, 60)
    strat_f = fc3.selectbox("Strategy", ["All","Momentum","Mean Reversion",
                                          "Support & Resistance","Breakout"])
    phase_f = fc4.selectbox("Phase",    ["All","Mark-up","Accumulation",
                                          "Distribution","Decline","Neutral"])
    sectors = ["All"] + [r["sector"] for r in
                          db.all("SELECT DISTINCT sector FROM stocks WHERE sector IS NOT NULL ORDER BY sector")]
    sec_f   = fc5.selectbox("Sector", sectors)

    rows = load_signals(selected_date, min_c, sig_f, strat_f, phase_f, sec_f)

    if not rows:
        st.info("No signals found. Generate signals: `python ai_signal_engine.py --signals`")
    else:
        # ── MAIN SIGNAL TABLE (matches reference image format) ──
        st.markdown(f"**{len(rows)} signals** matching filters")

        # Build HTML table
        table_html = """
        <style>
        .sig-table{width:100%;border-collapse:collapse;font-size:12px}
        .sig-table th{padding:6px 8px;text-align:left;font-size:11px;
                      color:#6b7280;border-bottom:1px solid #e5e7eb;
                      background:#f9fafb;white-space:nowrap}
        .sig-table td{padding:6px 8px;border-bottom:1px solid #f3f4f6;
                      white-space:nowrap;overflow:hidden}
        .sig-table tr:hover td{background:#f9fafb}
        </style>
        <table class="sig-table">
        <thead><tr>
          <th>Symbol</th>
          <th>LTP (Rs)</th>
          <th>Change%</th>
          <th>Volume Ratio</th>
          <th>RSI</th>
          <th>MACD</th>
          <th>3M Beta</th>
          <th>PE Ratio</th>
          <th>Time Series</th>
          <th>Strategy</th>
          <th>Phase</th>
          <th>Upside 10d (Rs)</th>
          <th>Downside 10d (Rs)</th>
          <th>Signal</th>
          <th>Confidence</th>
        </tr></thead>
        <tbody>
        """

        for r in rows:
            ltp  = col(r,"live_ltp") or col(r,"ltp") or 0
            pct  = col(r,"live_pct") or 0
            upside   = col(r,"target_10d") or 0
            downside = col(r,"stop_loss") or 0

            # Try to get upside/downside from price_projections
            proj = db.one("""
                SELECT upside_price, downside_price FROM price_projections
                WHERE symbol=? AND horizon_days=10
                ORDER BY projection_date DESC LIMIT 1
            """, [r["symbol"]])
            if proj:
                upside   = proj.get("upside_price") or upside
                downside = proj.get("downside_price") or downside

            macd_v = col(r,"macd_at_signal") or 0
            macd_str = f'<span class="{"up" if macd_v > 0 else "dn"}">{macd_v:+.2f}</span>'

            pe = col(r,"pe_ratio") or 0
            pe_str = f'<span class="{"dn" if pe < 0 else ""}">{pe:.1f}</span>' if pe else "—"

            table_html += f"""<tr>
              <td><strong>{r['symbol']}</strong></td>
              <td>{float(ltp):,.1f}</td>
              <td>{fmt_pct(pct)}</td>
              <td>{fmt_vol(col(r,"volume_ratio_at_signal"))}</td>
              <td>{fmt_rsi(col(r,"rsi_at_signal"))}</td>
              <td>{macd_str}</td>
              <td>{float(col(r,'beta_3m') or 1):.2f}</td>
              <td>{pe_str}</td>
              <td>{r.get("time_series_type") or "—"}</td>
              <td>{r.get("strategy") or "—"}</td>
              <td>{fmt_phase(r.get("wyckoff_phase"))}</td>
              <td><span class="up">{fmt_price(upside)}</span></td>
              <td><span class="dn">{fmt_price(downside)}</span></td>
              <td>{fmt_signal(r.get("signal","HOLD"))}</td>
              <td>{fmt_conf(col(r,"confidence_pct",50), r.get("signal","HOLD"))}</td>
            </tr>"""

        table_html += "</tbody></table>"
        st.markdown(table_html, unsafe_allow_html=True)

        st.divider()

        # ── DETAIL CARDS for BUY signals ─────────────────────
        buy_rows = [r for r in rows if r.get("signal")=="BUY"]
        if buy_rows:
            st.subheader("BUY Signal Details")
            for r in buy_rows[:10]:
                ltp    = float(col(r,"live_ltp") or col(r,"ltp") or 0)
                target = float(col(r,"target_10d") or ltp)
                sl     = float(col(r,"stop_loss") or ltp)
                rr     = float(col(r,"risk_reward") or 0)
                conf   = float(col(r,"confidence_pct") or 0)
                rsi    = float(col(r,"rsi_at_signal") or 50)
                accum  = float(col(r,"accumulation_score") or 0)
                up_prob  = float(col(r,"upside_prob_10d") or 50)
                dn_prob  = float(col(r,"downside_prob_10d") or 50)

                proj = db.one("""
                    SELECT upside_price, downside_price FROM price_projections
                    WHERE symbol=? AND horizon_days=10
                    ORDER BY projection_date DESC LIMIT 1
                """, [r["symbol"]])
                upside   = float(proj["upside_price"]   if proj else target)
                downside = float(proj["downside_price"] if proj else sl)

                try:
                    drivers = json.loads(r.get("key_drivers") or "[]")
                except Exception:
                    drivers = []

                with st.expander(
                    f"{r['symbol']} — {r.get('strategy','—')} | "
                    f"Conf: {conf:.0f}% | LTP: Rs.{ltp:,.0f} | "
                    f"Phase: {r.get('wyckoff_phase','—')}",
                    expanded=(conf >= 80)
                ):
                    ca,cb,cc,cd,ce,cf = st.columns(6)
                    ca.metric("LTP",       f"Rs.{ltp:,.0f}")
                    cb.metric("Target 10d",f"Rs.{target:,.0f}",
                              f"+{(target/ltp-1)*100:.1f}%" if ltp else None)
                    cc.metric("Stop Loss", f"Rs.{sl:,.0f}",
                              f"{(sl/ltp-1)*100:.1f}%" if ltp else None)
                    cd.metric("Risk:Reward",f"1:{rr:.1f}")
                    ce.metric("Upside 10d",  f"Rs.{upside:,.0f}")
                    cf.metric("Downside 10d",f"Rs.{downside:,.0f}")

                    c1,c2,c3,c4 = st.columns(4)
                    c1.metric("RSI",         f"{rsi:.0f}")
                    c2.metric("Broker Accum",f"{accum:+.0f}")
                    c3.metric("Up Prob 10d", f"{up_prob:.0f}%")
                    c4.metric("Dn Prob 10d", f"{dn_prob:.0f}%")

                    if drivers:
                        st.caption("Key reasons: " + " | ".join(drivers))

                    # Sector
                    if r.get("sector"):
                        st.caption(f"Sector: {r['sector']}")


# ═══════════════════════════════════════════════════════════════
#  PAGE 2: MARKET OVERVIEW
# ═══════════════════════════════════════════════════════════════
elif page == "Market Overview":
    st.title("Market Overview")

    indices = db.all("""
        SELECT index_name, close_value, pct_change, turnover
        FROM indices WHERE index_date=?
        GROUP BY index_name
        ORDER BY CASE index_name WHEN 'NEPSE' THEN 0 ELSE 1 END
        LIMIT 8
    """, [today])

    if indices:
        st.subheader("Indices")
        cols = st.columns(min(len(indices), 4))
        for i, idx in enumerate(indices[:4]):
            pct = idx.get("pct_change") or 0
            cols[i].metric(
                idx["index_name"],
                f"{float(idx['close_value'] or 0):,.2f}",
                f"{float(pct):+.2f}%",
            )

    st.divider()
    g1, g2 = st.columns(2)

    with g1:
        st.subheader("Top Gainers")
        gainers = db.all("""
            SELECT symbol, value AS pct_change, ltp, volume
            FROM top_movers WHERE trade_date=? AND category='gainer'
            ORDER BY value DESC LIMIT 10
        """, [today])
        if gainers:
            st.dataframe(pd.DataFrame(gainers), use_container_width=True, hide_index=True)
        else:
            st.info("No data yet today")

    with g2:
        st.subheader("Top Losers")
        losers = db.all("""
            SELECT symbol, value AS pct_change, ltp, volume
            FROM top_movers WHERE trade_date=? AND category='loser'
            ORDER BY value ASC LIMIT 10
        """, [today])
        if losers:
            st.dataframe(pd.DataFrame(losers), use_container_width=True, hide_index=True)
        else:
            st.info("No data yet today")

    st.subheader("Top by Volume")
    vol = db.all("""
        SELECT symbol, volume, ltp, pct_change
        FROM top_movers WHERE trade_date=? AND category='volume'
        ORDER BY volume DESC LIMIT 10
    """, [today])
    if vol:
        st.dataframe(pd.DataFrame(vol), use_container_width=True, hide_index=True)


# ═══════════════════════════════════════════════════════════════
#  PAGE 3: STOCK DEEP DIVE
# ═══════════════════════════════════════════════════════════════
elif page == "Stock Deep Dive":
    st.title("Stock Deep Dive")

    stocks = db.all("SELECT symbol, company_name, sector FROM stocks WHERE is_active=1 ORDER BY symbol")
    sym_list = [f"{r['symbol']} — {r.get('company_name','')}" for r in stocks]
    sel = st.selectbox("Select Stock", sym_list)
    sym = sel.split(" — ")[0] if sel else None

    if sym:
        c1, c2 = st.columns(2)

        # Latest price
        live = db.one("""
            SELECT ltp, pct_change, high_price, low_price, volume, turnover
            FROM live_market WHERE symbol=?
            ORDER BY snapshot_time DESC LIMIT 1
        """, [sym])

        ohlcv_last = db.one("""
            SELECT close_price, open_price, high_price, low_price, volume
            FROM daily_ohlcv WHERE symbol=? ORDER BY trade_date DESC LIMIT 1
        """, [sym])

        ltp = float((live or ohlcv_last or {}).get("ltp") or
                    (ohlcv_last or {}).get("close_price") or 0)

        with c1:
            st.metric("LTP", f"Rs.{ltp:,.2f}",
                      f"{float((live or {}).get('pct_change') or 0):+.2f}%")

        # Latest signal for this stock
        sig = db.one("""
            SELECT * FROM ai_signals WHERE symbol=?
            ORDER BY signal_date DESC LIMIT 1
        """, [sym])
        if sig:
            with c2:
                st.metric(f"Signal ({sig['signal_date']})",
                          sig["signal"],
                          f"Conf: {sig['confidence_pct']:.0f}%")

        st.divider()
        tab1, tab2, tab3, tab4 = st.tabs(["Price Chart","Indicators","Broker","Projections"])

        with tab1:
            hist = db.all("""
                SELECT trade_date, open_price, high_price, low_price,
                       close_price, volume
                FROM daily_ohlcv WHERE symbol=?
                ORDER BY trade_date DESC LIMIT 180
            """, [sym])
            if hist:
                df = pd.DataFrame(hist[::-1])
                df["trade_date"] = pd.to_datetime(df["trade_date"])
                st.line_chart(df.set_index("trade_date")["close_price"])
                st.bar_chart(df.set_index("trade_date")["volume"])
            else:
                st.info("No price history. Run data collection first.")

        with tab2:
            ti = db.one("""
                SELECT * FROM technical_indicators WHERE symbol=?
                ORDER BY calc_date DESC LIMIT 1
            """, [sym])
            if ti:
                m1,m2,m3,m4 = st.columns(4)
                m1.metric("RSI (14)",        f"{ti.get('rsi14') or 0:.1f}")
                m2.metric("Signal Score",    f"{ti.get('signal_strength') or 0:.1f}")
                m3.metric("Volume Ratio",    f"{ti.get('volume_ratio') or 0:.2f}x")
                m4.metric("ATR%",            f"{ti.get('atr_pct') or 0:.2f}%")

                ca, cb = st.columns(2)
                with ca:
                    st.markdown("**Moving Averages**")
                    st.write(f"EMA 9:   Rs.{float(ti.get('ema9') or 0):,.2f}")
                    st.write(f"EMA 21:  Rs.{float(ti.get('ema21') or 0):,.2f}")
                    st.write(f"EMA 50:  Rs.{float(ti.get('ema50') or 0):,.2f}")
                    st.write(f"EMA 200: Rs.{float(ti.get('ema200') or 0):,.2f}")
                with cb:
                    st.markdown("**Bollinger Bands**")
                    st.write(f"Upper:   Rs.{float(ti.get('bb_upper') or 0):,.2f}")
                    st.write(f"Middle:  Rs.{float(ti.get('bb_middle') or 0):,.2f}")
                    st.write(f"Lower:   Rs.{float(ti.get('bb_lower') or 0):,.2f}")
                    sq = "YES — breakout pending" if ti.get("bb_squeeze") else "No"
                    st.write(f"Squeeze: {sq}")

                st.markdown("**Detected Patterns**")
                patterns = {
                    "Doji": ti.get("pattern_doji"),
                    "Hammer": ti.get("pattern_hammer"),
                    "Bullish Engulfing": ti.get("pattern_engulf_bull"),
                    "Bearish Engulfing": ti.get("pattern_engulf_bear"),
                    "Morning Star": ti.get("pattern_morning_star"),
                    "Evening Star": ti.get("pattern_evening_star"),
                    "Shooting Star": ti.get("pattern_shooting_star"),
                }
                active = [k for k, v in patterns.items() if v]
                st.write(", ".join(active) if active else "None detected")
            else:
                st.info("No indicator data. Run: python indicator_engine.py")

        with tab3:
            bc = db.all("""
                SELECT trade_date, top3_buy_pct, top3_sell_pct,
                       unique_buyers, unique_sellers, avg_trade_size,
                       large_trades_pct, accumulation_score, wyckoff_phase
                FROM broker_concentration WHERE symbol=?
                ORDER BY trade_date DESC LIMIT 30
            """, [sym])
            if bc:
                st.dataframe(pd.DataFrame(bc), use_container_width=True, hide_index=True)
                latest = bc[0]
                col_a, col_b, col_c = st.columns(3)
                col_a.metric("Accumulation Score", f"{latest.get('accumulation_score',0):+.1f}")
                col_b.metric("Wyckoff Phase",       latest.get("wyckoff_phase","—"))
                col_c.metric("Top-3 Buy%",          f"{latest.get('top3_buy_pct',0):.1f}%")
            else:
                st.info("No broker data. Run: python data_collector.py --floorsheet")

        with tab4:
            projs = db.all("""
                SELECT horizon_days, current_price, target_price,
                       upside_price, downside_price,
                       probability_up, probability_down
                FROM price_projections WHERE symbol=?
                ORDER BY projection_date DESC, horizon_days ASC LIMIT 4
            """, [sym])
            if projs:
                for p in projs:
                    h = p["horizon_days"]
                    st.markdown(f"**{h}-Day Projection**")
                    pc1,pc2,pc3,pc4 = st.columns(4)
                    pc1.metric("Target",   f"Rs.{float(p['target_price'] or 0):,.0f}")
                    pc2.metric("Upside",   f"Rs.{float(p['upside_price'] or 0):,.0f}")
                    pc3.metric("Downside", f"Rs.{float(p['downside_price'] or 0):,.0f}")
                    pc4.metric("Up Prob",  f"{float(p['probability_up'] or 0):.0f}%")
                    st.divider()
            else:
                st.info("No projections. Generate signals first.")


# ═══════════════════════════════════════════════════════════════
#  PAGE 4: BROKER ANALYSIS
# ═══════════════════════════════════════════════════════════════
elif page == "Broker Analysis":
    st.title("Broker Accumulation Analysis")
    st.caption("Stocks where concentrated broker buying has been detected (NEPSE's biggest edge)")

    days = st.slider("Look-back days", 1, 30, 5)
    cutoff = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")

    rows = db.all("""
        SELECT symbol,
               ROUND(AVG(accumulation_score),2)  AS avg_score,
               ROUND(AVG(top3_buy_pct),1)         AS avg_top3_buy,
               ROUND(AVG(top3_sell_pct),1)        AS avg_top3_sell,
               ROUND(AVG(unique_buyers),1)         AS avg_buyers,
               ROUND(AVG(unique_sellers),1)        AS avg_sellers,
               ROUND(AVG(large_trades_pct),1)      AS large_trades_pct,
               MAX(wyckoff_phase)                  AS latest_phase,
               COUNT(*)                             AS days
        FROM broker_concentration
        WHERE trade_date>=?
        GROUP BY symbol HAVING days>=1
        ORDER BY avg_score DESC LIMIT 40
    """, [cutoff])

    if rows:
        df = pd.DataFrame(rows)
        df.columns = ["Symbol","Avg Score","Top3 Buy%","Top3 Sell%",
                      "Avg Buyers","Avg Sellers","Large Trades%","Phase","Days"]
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.markdown("""
        **Score > +30** = Mark-up phase (strong accumulation)  
        **Score > +15** = Accumulation phase  
        **Top3 Buy% > 60** = 3 brokers controlling the buy side  
        **Large Trades% > 30** = Institutional activity detected  
        """)
    else:
        st.info("No broker data. Run: `python data_collector.py --floorsheet YYYY-MM-DD`")


# ═══════════════════════════════════════════════════════════════
#  PAGE 5: BACKTEST RESULTS
# ═══════════════════════════════════════════════════════════════
elif page == "Backtest Results":
    st.title("AI Signal Backtest Results")

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
        m1,m2,m3,m4,m5 = st.columns(5)
        m1.metric("Total Evaluated", stats["total"])
        m2.metric("Overall Accuracy", f"{stats.get('accuracy',0)}%")
        m3.metric("BUY Accuracy",     f"{stats.get('buy_acc',0)}%")
        m4.metric("Avg Win",          f"+{stats.get('avg_win',0)}%")
        m5.metric("Avg Loss",         f"{stats.get('avg_loss',0)}%")

        st.divider()
        recent = db.all("""
            SELECT signal_date, symbol, signal, confidence_pct,
                   strategy, wyckoff_phase, actual_pct_10d, signal_correct
            FROM ai_signals WHERE signal_correct IS NOT NULL
            ORDER BY signal_date DESC LIMIT 100
        """)
        if recent:
            df = pd.DataFrame(recent)
            df["Result"] = df["signal_correct"].map({1:"Correct","0":"Wrong",0:"Wrong"})
            st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No backtest data yet. Signals are evaluated daily automatically.")
        st.code("python ai_signal_engine.py --backtest")


# ═══════════════════════════════════════════════════════════════
#  PAGE 6: NEWS & EVENTS
# ═══════════════════════════════════════════════════════════════
elif page == "News & Events":
    st.title("Market News & Corporate Events")

    c1, c2 = st.columns(2)

    with c1:
        st.subheader("Latest News")
        news = db.all("""
            SELECT headline, source, category, sentiment_score,
                   sentiment_label, published_at
            FROM news ORDER BY published_at DESC LIMIT 30
        """)
        if news:
            for n in news:
                score = n.get("sentiment_score") or 0
                label = n.get("sentiment_label","neutral")
                icon  = "+" if label=="positive" else "-" if label=="negative" else "~"
                st.markdown(f"[{icon}] **{n['headline'][:80]}**")
                st.caption(f"{n.get('source','')} | {n.get('category','')} | Score: {score:+.2f}")
        else:
            st.info("No news. News is collected automatically.")

    with c2:
        st.subheader("Upcoming Corporate Actions")
        actions = db.all("""
            SELECT symbol, action_type, book_close_date, ex_date, value, cash_per_share
            FROM corporate_actions
            WHERE book_close_date >= date('now')
               OR ex_date >= date('now')
            ORDER BY COALESCE(book_close_date, ex_date) ASC LIMIT 20
        """)
        if actions:
            st.dataframe(pd.DataFrame(actions), use_container_width=True, hide_index=True)
        else:
            st.info("No upcoming corporate actions in database.")

        st.subheader("Recent Dividends & Bonus")
        corp = db.all("""
            SELECT symbol, action_type, value, cash_per_share, announcement_date
            FROM corporate_actions
            WHERE action_type IN ('dividend','bonus')
            ORDER BY announcement_date DESC LIMIT 15
        """)
        if corp:
            st.dataframe(pd.DataFrame(corp), use_container_width=True, hide_index=True)
