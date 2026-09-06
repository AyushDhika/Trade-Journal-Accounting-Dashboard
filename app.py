"""
Trade Journal & Accounting Dashboard
=====================================
A professional trading journal for manual trade logging + broker CSV imports,
with performance analytics (equity curve, drawdown, win rate, profit factor,
per-instrument / per-weekday / per-hour breakdowns).

Run with:  streamlit run app.py
"""

from datetime import datetime, date, time as dtime, timedelta

import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st
import yfinance as yf

import db
import utils

# ---------------------------------------------------------------------------
# Page config + one-time init
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Trade Journal | Performance Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

@st.cache_resource
def _init_db_once():
    """Run schema creation + one-time migrations exactly once per server
    process, instead of on every widget interaction (Streamlit reruns this
    whole script top-to-bottom on every click/input). Re-running the
    migration UPDATE on every rerun was pure wasted work and made the app
    feel sluggish, which is most noticeable on mobile connections."""
    db.init_db()
    return True


_init_db_once()

CURRENCY_SYMBOL = "$"  # USD


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_gold_history(start_date: str, end_date: str) -> pd.DataFrame:
    """Daily XAUUSD spot close prices from Yahoo Finance, cached for an hour
    so we don't re-fetch on every Streamlit rerun (this app reruns its whole
    script on every click/filter change). Needs internet access at runtime —
    returns an empty DataFrame (never raises) if the fetch fails for any
    reason, so the caller can show a friendly warning instead of crashing."""
    try:
        raw = yf.download("XAUUSD=X", start=start_date, end=end_date,
                           progress=False, auto_adjust=False)
        if raw is None or raw.empty:
            return pd.DataFrame(columns=["date", "close"])
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        out = raw.reset_index()[["Date", "Close"]]
        out.columns = ["date", "close"]
        out["date"] = pd.to_datetime(out["date"])
        return out.dropna(subset=["close"])
    except Exception:
        return pd.DataFrame(columns=["date", "close"])

# ---------------------------------------------------------------------------
# Global CSS — professional trading-terminal look, Apple-grade motion & depth
# ---------------------------------------------------------------------------
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600;700&display=swap');

    :root {
        --ease: cubic-bezier(0.16, 1, 0.3, 1);
        --ease-soft: cubic-bezier(0.4, 0, 0.2, 1);
        --bg: #0A0D12;
        --card: #141B22;
        --card-2: #10151B;
        --border: #232B33;
        --text: #E6EDF3;
        --text-dim: #8B98A5;
        --text-faint: #6B7885;
        --green: #22C55E;
        --red: #EF4444;
        --blue: #3B82F6;
    }

    @media (prefers-reduced-motion: reduce) {
        *, *::before, *::after { animation-duration: 0.001ms !important; transition-duration: 0.001ms !important; }
    }

    html, body, [class*="css"]  {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
        -webkit-font-smoothing: antialiased;
        -moz-osx-font-smoothing: grayscale;
    }

    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}

    /* IMPORTANT: do NOT hide <header> or its toolbar. The sidebar's
       open/close button lives somewhere inside that region, and its exact
       DOM testid varies by Streamlit version — hiding the container risks
       hiding that button with no way to ever reopen the sidebar again.
       Only cosmetic change: make the header bar blend into the background. */
    header[data-testid="stHeader"] { background: transparent; }

    html { scroll-behavior: smooth; }

    /* Prevent the page from ever scrolling sideways on small screens —
       a common cause of an "unreadable"/broken-looking layout on phones. */
    html, body { overflow-x: hidden; }

    /* Ambient background glow — fixed, subtle, non-interactive depth */
    .stApp {
        background:
            radial-gradient(1100px 520px at 8% -8%, rgba(34,197,94,0.055), transparent 60%),
            radial-gradient(900px 480px at 96% 6%, rgba(59,130,246,0.05), transparent 55%),
            radial-gradient(800px 500px at 50% 105%, rgba(239,68,68,0.03), transparent 60%),
            var(--bg);
    }

    ::-webkit-scrollbar { width: 10px; height: 10px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: #232B33; border-radius: 20px; border: 2px solid var(--bg); }
    ::-webkit-scrollbar-thumb:hover { background: #2D3742; }

    .block-container {
        padding-top: 1.8rem;
        padding-bottom: 3rem;
        max-width: 1400px;
    }

    @keyframes pageIn {
        from { opacity: 0; transform: translateY(6px); }
        to   { opacity: 1; transform: translateY(0); }
    }
    @keyframes fadeInUp {
        from { opacity: 0; transform: translateY(14px); }
        to   { opacity: 1; transform: translateY(0); }
    }
    @keyframes fadeIn {
        from { opacity: 0; }
        to   { opacity: 1; }
    }

    /* KPI cards */
    .kpi-card {
        background: linear-gradient(155deg, var(--card) 0%, var(--card-2) 100%);
        border: 1px solid var(--border);
        border-radius: 16px;
        padding: 18px 20px;
        height: 100%;
        transition: transform 0.35s var(--ease), box-shadow 0.35s var(--ease), border-color 0.35s var(--ease);
    }
    .kpi-card:hover {
        transform: translateY(-4px);
        border-color: #2E3946;
        box-shadow: 0 16px 40px -12px rgba(0,0,0,0.55), 0 0 0 1px rgba(255,255,255,0.02);
    }
    .kpi-label {
        font-size: 11.5px;
        font-weight: 600;
        letter-spacing: 0.07em;
        text-transform: uppercase;
        color: var(--text-dim);
        margin-bottom: 7px;
    }
    .kpi-value {
        font-family: 'JetBrains Mono', monospace;
        font-variant-numeric: tabular-nums;
        font-size: 27px;
        font-weight: 700;
        color: var(--text);
        line-height: 1.2;
        letter-spacing: -0.01em;
        transition: color 0.35s var(--ease-soft);
    }
    .kpi-sub {
        font-size: 12px;
        color: var(--text-faint);
        margin-top: 5px;
    }
    .kpi-positive { color: var(--green) !important; }
    .kpi-negative { color: var(--red) !important; }

    .section-title {
        font-size: 18px;
        font-weight: 700;
        color: var(--text);
        margin: 6px 0 14px 0;
        padding-left: 10px;
        border-left: 3px solid var(--green);
        letter-spacing: -0.01em;
    }

    .app-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding-bottom: 10px;
        margin-bottom: 20px;
        border-bottom: 1px solid var(--border);
    }
    .app-title {
        font-size: 30px;
        font-weight: 800;
        color: var(--text);
        letter-spacing: -0.025em;
    }
    .app-subtitle { font-size: 13.5px; color: var(--text-dim); margin-top: 2px; }

    /* Tabs */
    .stTabs [data-baseweb="tab-list"] { gap: 4px; }
    .stTabs [data-baseweb="tab"] {
        background-color: var(--card);
        border-radius: 10px 10px 0 0;
        padding: 9px 18px;
        color: var(--text-dim);
        transition: all 0.3s var(--ease-soft);
    }
    .stTabs [data-baseweb="tab"]:hover { color: var(--text); background-color: #1A222B; }
    .stTabs [aria-selected="true"] {
        background-color: #1B232B;
        color: var(--green) !important;
        font-weight: 600;
    }
    .stTabs [data-baseweb="tab-highlight"] { background-color: var(--green); transition: all 0.3s var(--ease); }

    div[data-testid="stMetric"] {
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 14px;
        padding: 14px 16px;
        transition: transform 0.3s var(--ease), box-shadow 0.3s var(--ease);
    }
    div[data-testid="stMetric"]:hover { transform: translateY(-2px); box-shadow: 0 10px 28px -10px rgba(0,0,0,0.5); }
    div[data-testid="stMetricValue"] { font-variant-numeric: tabular-nums; }

    .badge-buy { background:#0F2A1B; color:var(--green); padding:3px 10px; border-radius:20px; font-size:12px; font-weight:600; transition: transform 0.2s var(--ease); }
    .badge-sell { background:#2A0F13; color:var(--red); padding:3px 10px; border-radius:20px; font-size:12px; font-weight:600; transition: transform 0.2s var(--ease); }
    .badge-buy:hover, .badge-sell:hover { transform: scale(1.05); }

    /* Sidebar — glassy depth */
    section[data-testid="stSidebar"] {
        background-color: rgba(13,18,24,0.85);
        backdrop-filter: blur(20px);
        -webkit-backdrop-filter: blur(20px);
        border-right: 1px solid #1D242C;
    }
    section[data-testid="stSidebar"] div[data-testid="stRadio"] label {
        border-radius: 10px;
        padding: 9px 12px !important;
        margin-bottom: 2px;
        transition: background-color 0.25s var(--ease-soft), transform 0.25s var(--ease-soft), color 0.25s var(--ease-soft);
    }
    section[data-testid="stSidebar"] div[data-testid="stRadio"] label:hover {
        background-color: rgba(34,197,94,0.07);
        transform: translateX(3px);
    }
    section[data-testid="stSidebar"] div[data-testid="stRadio"] label[data-checked="true"],
    section[data-testid="stSidebar"] div[data-testid="stRadio"] label:has(input:checked) {
        background-color: rgba(34,197,94,0.1);
        border-left: 2.5px solid var(--green);
    }

    /* Buttons — tactile press feedback */
    .stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {
        border-radius: 10px !important;
        transition: transform 0.18s var(--ease-soft), box-shadow 0.25s var(--ease-soft), border-color 0.25s var(--ease-soft), background-color 0.25s var(--ease-soft) !important;
        font-weight: 600 !important;
    }
    .stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {
        transform: translateY(-1px);
        box-shadow: 0 8px 20px -8px rgba(0,0,0,0.5);
        border-color: #2E3946 !important;
    }
    .stButton > button:active, .stDownloadButton > button:active, .stFormSubmitButton > button:active {
        transform: translateY(0) scale(0.97);
    }
    .stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {
        background: linear-gradient(155deg, #22C55E, #16A34A) !important;
        border: none !important;
    }
    .stButton > button[kind="primary"]:hover, .stFormSubmitButton > button[kind="primary"]:hover {
        box-shadow: 0 10px 26px -8px rgba(34,197,94,0.45);
    }

    /* Inputs — smooth focus rings */
    .stTextInput input, .stNumberInput input, .stDateInput input, .stTextArea textarea,
    div[data-baseweb="select"] > div {
        border-radius: 10px !important;
        transition: border-color 0.25s var(--ease-soft), box-shadow 0.25s var(--ease-soft) !important;
    }
    .stTextInput input:focus, .stNumberInput input:focus, .stDateInput input:focus, .stTextArea textarea:focus {
        border-color: var(--green) !important;
        box-shadow: 0 0 0 3px rgba(34,197,94,0.15) !important;
    }

    /* Expanders */
    div[data-testid="stExpander"] {
        border-radius: 12px !important;
        border-color: var(--border) !important;
        transition: border-color 0.3s var(--ease-soft);
        overflow: hidden;
    }
    div[data-testid="stExpander"] details[open] summary ~ div { animation: fadeIn 0.35s var(--ease) both; }
    div[data-testid="stExpander"]:hover { border-color: #2E3946 !important; }

    /* Dataframes */
    div[data-testid="stDataFrame"], div[data-testid="stDataEditor"] {
        border-radius: 12px;
        overflow: hidden;
    }

    /* Plotly charts — gentle reveal */
    div[data-testid="stPlotlyChart"] {
        border-radius: 12px;
        overflow: hidden;
    }

    /* Radio (side/view toggles outside sidebar) */
    div[data-testid="stRadio"] > div { gap: 4px; }

    hr { border-color: #1D242C !important; }

    /* ---------------------------------------------------------------
       Calendar grid — classes instead of inline styles so it can be
       restyled per breakpoint below.
    --------------------------------------------------------------- */
    .cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; }
    .cal-grid-header { margin-bottom: 2px; }
    .cal-header {
        text-align: center; font-size: 11px; font-weight: 600;
        color: var(--text-faint); padding: 4px;
    }
    .cal-cell {
        background: var(--card); border: 1px solid var(--border);
        border-radius: 8px; min-height: 74px; padding: 8px;
        overflow: hidden;
    }
    .cal-cell-empty { background: transparent; border-color: transparent; }
    .cal-cell-win { background: rgba(34,197,94,0.14); border-color: rgba(34,197,94,0.4); }
    .cal-cell-loss { background: rgba(239,68,68,0.14); border-color: rgba(239,68,68,0.4); }
    .cal-cell-today { box-shadow: inset 0 0 0 1.5px var(--blue); }
    .cal-day-num { font-size: 12px; color: var(--text-dim); font-weight: 600; }
    .cal-pnl {
        font-family: 'JetBrains Mono', monospace; font-size: 12px; font-weight: 700;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .cal-pnl-win { color: var(--green); }
    .cal-pnl-loss { color: var(--red); }
    .cal-pnl-flat { color: var(--text-dim); font-weight: 400; }
    .cal-trades { font-size: 10px; color: var(--text-faint); margin-top: 2px; }

    /* ---------------------------------------------------------------
       RESPONSIVE — tablets and small desktop windows
    --------------------------------------------------------------- */
    @media (max-width: 900px) {
        .block-container { padding-left: 1.2rem !important; padding-right: 1.2rem !important; }
        .app-title { font-size: 24px; }
        .kpi-value { font-size: 22px; }
    }

    /* ---------------------------------------------------------------
       RESPONSIVE — phones / narrow windows
    --------------------------------------------------------------- */
    @media (max-width: 640px) {
        .block-container {
            padding-left: 0.75rem !important;
            padding-right: 0.75rem !important;
            padding-top: 1rem !important;
        }

        /* Header */
        .app-header { flex-wrap: wrap; gap: 4px; padding-bottom: 8px; margin-bottom: 14px; }
        .app-title { font-size: 20px; letter-spacing: -0.01em; }
        .app-subtitle { font-size: 12px; line-height: 1.4; }

        /* KPI cards: keep them compact and never let long numbers overflow */
        .kpi-card { padding: 12px 14px; border-radius: 12px; }
        .kpi-label { font-size: 10px; letter-spacing: 0.05em; margin-bottom: 4px; }
        .kpi-value { font-size: 19px; word-break: break-word; }
        .kpi-sub { font-size: 10.5px; }

        .section-title { font-size: 14.5px; margin: 4px 0 10px 0; padding-left: 8px; }

        /* Tabs: allow horizontal scroll instead of squashing labels */
        .stTabs [data-baseweb="tab-list"] {
            overflow-x: auto; flex-wrap: nowrap; -webkit-overflow-scrolling: touch;
        }
        .stTabs [data-baseweb="tab"] { padding: 7px 12px; font-size: 12.5px; white-space: nowrap; }

        /* Metrics */
        div[data-testid="stMetric"] { padding: 10px 12px; }
        div[data-testid="stMetricValue"] { font-size: 20px; }

        /* Calendar: shrink cells so the 7-column grid still fits a phone */
        .cal-grid { gap: 3px; }
        .cal-cell { min-height: 52px; padding: 4px; border-radius: 6px; }
        .cal-header { font-size: 8.5px; padding: 2px; }
        .cal-day-num { font-size: 9.5px; }
        .cal-pnl { font-size: 8.5px; }
        .cal-trades { display: none; }  /* too cramped at this width, P/L color still tells the story */

        /* Sidebar */
        section[data-testid="stSidebar"] div[data-testid="stRadio"] label { font-size: 13.5px; }
    }

    @media (max-width: 400px) {
        .app-title { font-size: 18px; }
        .kpi-value { font-size: 17px; }
        .cal-cell { min-height: 44px; }
        .cal-pnl { font-size: 7.5px; }
    }
</style>
""", unsafe_allow_html=True)

PLOTLY_TEMPLATE = go.layout.Template(
    layout=dict(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, sans-serif", color="#C9D2DA", size=12),
        xaxis=dict(gridcolor="#1D242C", zerolinecolor="#1D242C"),
        yaxis=dict(gridcolor="#1D242C", zerolinecolor="#1D242C"),
        legend=dict(bgcolor="rgba(0,0,0,0)"),
        margin=dict(l=10, r=10, t=40, b=10),
        transition=dict(duration=450, easing="cubic-in-out"),
    )
)
GREEN = "#22C55E"
RED = "#EF4444"
ACCENT = "#3B82F6"

_kpi_anim_counter = {"i": 0}


def kpi_card(label, value, sub=None, positive=None):
    cls = ""
    if positive is True:
        cls = "kpi-positive"
    elif positive is False:
        cls = "kpi-negative"
    sub_html = f'<div class="kpi-sub">{sub}</div>' if sub else ""
    st.markdown(f"""
        <div class="kpi-card">
            <div class="kpi-label">{label}</div>
            <div class="kpi-value {cls}">{value}</div>
            {sub_html}
        </div>
    """, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Sidebar navigation
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("""
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px;">
            <div style="font-size:28px;">📈</div>
            <div>
                <div style="font-size:18px;font-weight:800;color:#E6EDF3;">Trade Journal</div>
                <div style="font-size:11px;color:#6B7885;">Performance Dashboard</div>
            </div>
        </div>
        <hr style="border-color:#1D242C;margin:12px 0 18px 0;">
    """, unsafe_allow_html=True)

    page = st.radio(
        "Navigate",
        ["📊 Dashboard", "📅 Calendar", "➕ Add Trade", "📥 Import CSV", "💰 Capital", "📜 Trade History", "🔍 Analytics", "⚙️ Settings"],
        label_visibility="collapsed",
    )

    st.markdown("<hr style='border-color:#1D242C;margin:18px 0;'>", unsafe_allow_html=True)
    total_trades_sidebar = db.trade_count()
    st.caption(f"Total trades logged: **{total_trades_sidebar}**")
    st.caption(f"Today • {date.today().strftime('%d %b %Y')}")


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
PAGE_TITLES = {
    "📊 Dashboard": ("Performance Dashboard", "Overview of your trading performance across all logged trades"),
    "📅 Calendar": ("Trading Calendar", "Daily P/L at a glance — navigate month to month or pick a date range"),
    "➕ Add Trade": ("Add Trade", "Manually log a closed trade"),
    "📥 Import CSV": ("Import Broker CSV", "Upload your broker's Closed Trades Report to sync trades automatically"),
    "💰 Capital": ("Capital Ledger", "Log deposits and withdrawals so your returns can be measured as a %, not just $"),
    "📜 Trade History": ("Trade History", "Browse, filter, edit and export every logged trade"),
    "🔍 Analytics": ("Deep Analytics", "Break down performance by instrument, weekday, hour and streaks"),
    "⚙️ Settings": ("Settings", "Preferences and data management"),
}
title, subtitle = PAGE_TITLES[page]
st.markdown(f"""
    <div class="app-header">
        <div>
            <div class="app-title">{title}</div>
            <div class="app-subtitle">{subtitle}</div>
        </div>
    </div>
""", unsafe_allow_html=True)


def apply_filters_ui(df: pd.DataFrame, key_prefix: str) -> pd.DataFrame:
    """Renders a compact filter bar and returns the filtered dataframe."""
    if df.empty:
        return df
    df = df.copy()
    df["exit_time_dt"] = pd.to_datetime(df["exit_time"], errors="coerce")
    min_d = df["exit_time_dt"].min().date()
    max_d = df["exit_time_dt"].max().date()

    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        dr = st.date_input(
            "Date range", value=(min_d, max_d), min_value=min_d, max_value=max_d,
            key=f"{key_prefix}_daterange",
        )
    with c2:
        insts = sorted(df["instrument"].dropna().unique().tolist())
        chosen = st.multiselect("Instrument", insts, default=insts, key=f"{key_prefix}_inst")
    with c3:
        side_choice = st.selectbox("Side", ["All", "Buy", "Sell"], key=f"{key_prefix}_side")

    if isinstance(dr, tuple) and len(dr) == 2:
        start_d, end_d = dr
    else:
        start_d, end_d = min_d, max_d

    mask = (
        (df["exit_time_dt"].dt.date >= start_d)
        & (df["exit_time_dt"].dt.date <= end_d)
        & (df["instrument"].isin(chosen))
    )
    if side_choice != "All":
        mask &= (df["side"] == side_choice)

    return df[mask].drop(columns=["exit_time_dt"])


# ---------------------------------------------------------------------------
# PAGE: Dashboard
# ---------------------------------------------------------------------------
if page == "📊 Dashboard":
    all_df = db.fetch_trades()

    if all_df.empty:
        st.info("No trades logged yet. Head to **➕ Add Trade** to log one manually, or **📥 Import CSV** to upload your broker report.")
    else:
        with st.expander("🔎 Filters", expanded=False):
            filtered = apply_filters_ui(all_df, "dash")

        k = utils.compute_kpis(filtered)

        r1c1, r1c2, r1c3, r1c4 = st.columns(4)
        with r1c1:
            kpi_card("Net P/L", utils.format_currency(k["total_pnl"], CURRENCY_SYMBOL),
                      sub=f"{k['total_trades']} trades", positive=(k["total_pnl"] >= 0))
        with r1c2:
            kpi_card("Win Rate", f"{k['win_rate']:.1f}%",
                      sub=f"{int(round(k['win_rate']/100*k['total_trades'])) if k['total_trades'] else 0} wins")
        with r1c3:
            pf_display = "∞" if k["profit_factor"] == float("inf") else f"{k['profit_factor']:.2f}"
            kpi_card("Profit Factor", pf_display, sub="Gross profit ÷ gross loss",
                      positive=(k["profit_factor"] >= 1.5) if k["profit_factor"] != float("inf") else True)
        with r1c4:
            kpi_card("Max Drawdown", utils.format_currency(k["max_drawdown"], CURRENCY_SYMBOL),
                      sub="Peak-to-trough equity", positive=False if k["max_drawdown"] < 0 else None)

        st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)
        r2c1, r2c2, r2c3, r2c4 = st.columns(4)
        with r2c1:
            kpi_card("Avg Win", utils.format_currency(k["avg_win"], CURRENCY_SYMBOL), positive=True)
        with r2c2:
            kpi_card("Avg Loss", utils.format_currency(k["avg_loss"], CURRENCY_SYMBOL), positive=False)
        with r2c3:
            kpi_card("Expectancy / Trade", utils.format_currency(k["expectancy"], CURRENCY_SYMBOL),
                      positive=(k["expectancy"] >= 0))
        with r2c4:
            streak = k["current_streak"]
            streak_txt = f"{abs(streak)} {'win' if streak >= 0 else 'loss'}{'es' if abs(streak)!=1 and streak<0 else ('s' if abs(streak)!=1 else '')}"
            kpi_card("Current Streak", streak_txt, positive=(streak >= 0))

        cap_df_dash = db.fetch_capital_transactions()
        if not cap_df_dash.empty:
            st.markdown("<div style='height:22px'></div>", unsafe_allow_html=True)
            st.markdown('<div class="section-title">Capital & Return</div>', unsafe_allow_html=True)
            first_dt = pd.to_datetime(filtered["exit_time"]).min()
            last_dt = max(pd.to_datetime(filtered["exit_time"]).max(), pd.Timestamp.now())
            overall_ret = utils.overall_return_pct(k["total_pnl"], first_dt, last_dt, cap_df_dash)
            capc1, capc2, capc3 = st.columns(3)
            with capc1:
                kpi_card("Current Capital", utils.format_currency(db.current_capital_balance(), CURRENCY_SYMBOL))
            with capc2:
                kpi_card("Overall Return %",
                          utils.format_percent(overall_ret) if overall_ret is not None else "—",
                          sub="Modified Dietz, since inception", positive=(overall_ret or 0) >= 0)
            with capc3:
                net_flow = cap_df_dash["amount"].sum()
                kpi_card("Net Deposits", utils.format_currency(net_flow, CURRENCY_SYMBOL),
                          sub="Total deposited minus withdrawn")

        st.markdown("<div style='height:22px'></div>", unsafe_allow_html=True)
        st.markdown('<div class="section-title">Costs Breakdown</div>', unsafe_allow_html=True)
        costs = utils.cost_breakdown(filtered)
        cc1, cc2, cc3, cc4, cc5 = st.columns(5)
        with cc1:
            kpi_card("Gross P/L", utils.format_currency(costs["gross_pnl"], CURRENCY_SYMBOL),
                      sub="Before costs", positive=(costs["gross_pnl"] >= 0))
        with cc2:
            kpi_card("Total Fees", utils.format_currency(costs["fee"], CURRENCY_SYMBOL), positive=False)
        with cc3:
            kpi_card("Commission", utils.format_currency(costs["commission"], CURRENCY_SYMBOL), positive=False)
        with cc4:
            kpi_card("Tax", utils.format_currency(costs["tax"], CURRENCY_SYMBOL), positive=False)
        with cc5:
            kpi_card("Net P/L", utils.format_currency(costs["net_pnl"], CURRENCY_SYMBOL),
                      sub="After all costs", positive=(costs["net_pnl"] >= 0))
        cost_pct = (costs["total_costs"] / abs(costs["gross_pnl"]) * 100) if costs["gross_pnl"] else 0
        st.caption(f"Total costs paid: **{utils.format_currency(costs['total_costs'], CURRENCY_SYMBOL)}** "
                    f"({cost_pct:.1f}% of gross P/L)" + (f" · Swap/overnight interest: {utils.format_currency(costs['swap'], CURRENCY_SYMBOL)}" if costs["swap"] else ""))

        st.markdown("<div style='height:22px'></div>", unsafe_allow_html=True)
        st.markdown('<div class="section-title">Equity Curve</div>', unsafe_allow_html=True)
        eq = utils.equity_curve(filtered)
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=list(range(1, len(eq) + 1)), y=eq["equity"],
            mode="lines", line=dict(color=GREEN, width=2.4),
            fill="tozeroy", fillcolor="rgba(34,197,94,0.08)",
            hovertemplate="Trade #%{x}<br>Equity: " + CURRENCY_SYMBOL + "%{y:,.2f}<extra></extra>",
        ))
        fig.update_layout(template=PLOTLY_TEMPLATE, height=340,
                           xaxis_title="Trade #", yaxis_title=f"Cumulative P/L ({CURRENCY_SYMBOL})")
        st.plotly_chart(fig, width="stretch")

        cL, cR = st.columns(2)
        with cL:
            st.markdown('<div class="section-title">Daily P/L</div>', unsafe_allow_html=True)
            dpnl = utils.daily_pnl(filtered)
            colors = [GREEN if v >= 0 else RED for v in dpnl["pnl"]]
            fig2 = go.Figure(go.Bar(x=dpnl["date"].astype(str), y=dpnl["pnl"], marker_color=colors,
                                     hovertemplate="%{x}<br>P/L: " + CURRENCY_SYMBOL + "%{y:,.2f}<extra></extra>"))
            fig2.update_layout(template=PLOTLY_TEMPLATE, height=300)
            st.plotly_chart(fig2, width="stretch")
        with cR:
            st.markdown('<div class="section-title">P/L by Instrument</div>', unsafe_allow_html=True)
            byinst = utils.pnl_by_instrument(filtered)
            colors2 = [GREEN if v >= 0 else RED for v in byinst["pnl"]]
            fig3 = go.Figure(go.Bar(x=byinst["instrument"], y=byinst["pnl"], marker_color=colors2,
                                     hovertemplate="%{x}<br>P/L: " + CURRENCY_SYMBOL + "%{y:,.2f}<extra></extra>"))
            fig3.update_layout(template=PLOTLY_TEMPLATE, height=300)
            st.plotly_chart(fig3, width="stretch")

        # --- NEW: Return on Capital KPI row ---
        st.markdown("<div style='height:22px'></div>", unsafe_allow_html=True)
        st.markdown('<div class="section-title">Return on Capital (margin-based)</div>', unsafe_allow_html=True)
        r3c1, r3c2, r3c3 = st.columns(3)
        with r3c1:
            kpi_card("Avg Return / Trade", utils.format_percent(k["avg_return_pct"]),
                     sub="Average % profit/loss per trade on margin deployed")
        with r3c2:
            kpi_card("Best Return", utils.format_percent(k["max_return_pct"]),
                     sub="Highest single-trade % return")
        with r3c3:
            kpi_card("Total Margin Deployed", utils.format_currency(k["total_margin_deployed"], CURRENCY_SYMBOL),
                     sub="Sum of margin locked across all filtered trades")

        st.markdown("<div style='height:22px'></div>", unsafe_allow_html=True)
        st.markdown('<div class="section-title">Recent Trades</div>', unsafe_allow_html=True)
        recent = filtered.sort_values("exit_time", ascending=False).head(10)
        show_cols = ["exit_time", "instrument", "side", "lot_size", "entry_price", "exit_price", "pnl", "remarks"]
        show_cols = [c for c in show_cols if c in recent.columns]
        st.dataframe(
            recent[show_cols].rename(columns={
                "exit_time": "Closed", "instrument": "Instrument", "side": "Side",
                "lot_size": "Lot", "entry_price": "Entry", "exit_price": "Exit",
                "pnl": "P/L", "remarks": "Remarks",
            }),
            width="stretch", hide_index=True,
        )


# ---------------------------------------------------------------------------
# PAGE: Calendar
# ---------------------------------------------------------------------------
elif page == "📅 Calendar":
    all_df = db.fetch_trades()

    if all_df.empty:
        st.info("No trades logged yet.")
    else:
        all_df["exit_dt"] = pd.to_datetime(all_df["exit_time"], errors="coerce")
        min_dt = all_df["exit_dt"].min()
        max_dt = all_df["exit_dt"].max()

        view_mode = st.radio("View", ["Month grid", "Custom date range"], horizontal=True, label_visibility="collapsed")

        if view_mode == "Month grid":
            if "cal_year" not in st.session_state:
                st.session_state.cal_year = max_dt.year
                st.session_state.cal_month = max_dt.month

            nav1, nav2, nav3 = st.columns([1, 3, 1])
            with nav1:
                if st.button("← Prev", width="stretch"):
                    m, y = st.session_state.cal_month - 1, st.session_state.cal_year
                    if m == 0:
                        m, y = 12, y - 1
                    st.session_state.cal_month, st.session_state.cal_year = m, y
            with nav3:
                if st.button("Next →", width="stretch"):
                    m, y = st.session_state.cal_month + 1, st.session_state.cal_year
                    if m == 13:
                        m, y = 1, y + 1
                    st.session_state.cal_month, st.session_state.cal_year = m, y

            year, month = st.session_state.cal_year, st.session_state.cal_month
            with nav2:
                st.markdown(f"<h3 style='text-align:center;margin:0;'>{date(year, month, 1).strftime('%B %Y')}</h3>", unsafe_allow_html=True)

            cal_data = utils.calendar_month_data(all_df, year, month)
            pnl_by_day = {row["date"]: (row["pnl"], row["trades"]) for _, row in cal_data.iterrows()}

            month_pnl = cal_data["pnl"].sum() if not cal_data.empty else 0.0
            month_trades = int(cal_data["trades"].sum()) if not cal_data.empty else 0
            trading_days = len(cal_data)
            green_days = int((cal_data["pnl"] > 0).sum()) if not cal_data.empty else 0

            s1, s2, s3, s4 = st.columns(4)
            s1.metric("Month P/L", utils.format_currency(month_pnl, CURRENCY_SYMBOL))
            s2.metric("Trades", month_trades)
            s3.metric("Trading Days", trading_days)
            s4.metric("Green Days", f"{green_days}/{trading_days}" if trading_days else "0/0")

            st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

            import calendar as cal_module
            cal_module.setfirstweekday(cal_module.MONDAY)
            month_matrix = cal_module.monthcalendar(year, month)

            weekday_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            header_html = "".join(f"<div class='cal-header'>{w}</div>" for w in weekday_labels)

            rows_html = ""
            for week in month_matrix:
                for day in week:
                    if day == 0:
                        rows_html += "<div class='cal-cell cal-cell-empty'></div>"
                        continue
                    d = date(year, month, day)
                    pnl_val, trades_val = pnl_by_day.get(d, (None, 0))
                    if pnl_val is None:
                        cell_cls, pnl_txt = "", ""
                    elif pnl_val > 0:
                        cell_cls = "cal-cell-win"
                        pnl_txt = f"<div class='cal-pnl cal-pnl-win'>+{utils.format_currency(pnl_val, CURRENCY_SYMBOL)}</div>"
                    elif pnl_val < 0:
                        cell_cls = "cal-cell-loss"
                        pnl_txt = f"<div class='cal-pnl cal-pnl-loss'>{utils.format_currency(pnl_val, CURRENCY_SYMBOL)}</div>"
                    else:
                        cell_cls, pnl_txt = "", "<div class='cal-pnl cal-pnl-flat'>—</div>"
                    trades_txt = f"<div class='cal-trades'>{trades_val} trade{'s' if trades_val != 1 else ''}</div>" if trades_val else ""
                    today_cls = " cal-cell-today" if d == date.today() else ""
                    rows_html += (
                        f'<div class="cal-cell {cell_cls}{today_cls}">'
                        f"<div class='cal-day-num'>{day}</div>"
                        f"{pnl_txt}{trades_txt}</div>"
                    )

            calendar_html = (
                '<div class="cal-grid cal-grid-header">'
                f'{header_html}</div>'
                '<div class="cal-grid">'
                f'{rows_html}</div>'
            )
            st.markdown(calendar_html, unsafe_allow_html=True)

            st.markdown("<div style='height:26px'></div>", unsafe_allow_html=True)
            st.markdown('<div class="section-title">Monthly Overview (all-time)</div>', unsafe_allow_html=True)
            msum = utils.monthly_summary(all_df)
            colors = [GREEN if v >= 0 else RED for v in msum["pnl"]]
            fig = go.Figure(go.Bar(x=msum["month"], y=msum["pnl"], marker_color=colors,
                                    hovertemplate="%{x}<br>P/L: " + CURRENCY_SYMBOL + "%{y:,.2f}<extra></extra>"))
            fig.update_layout(template=PLOTLY_TEMPLATE, height=280)
            st.plotly_chart(fig, width="stretch")

        else:
            c1, c2 = st.columns(2)
            with c1:
                start_d = st.date_input("From", value=min_dt.date(), min_value=min_dt.date(), max_value=max_dt.date())
            with c2:
                end_d = st.date_input("To", value=max_dt.date(), min_value=min_dt.date(), max_value=max_dt.date())

            mask = (all_df["exit_dt"].dt.date >= start_d) & (all_df["exit_dt"].dt.date <= end_d)
            ranged = all_df[mask]

            k = utils.compute_kpis(ranged)
            costs = utils.cost_breakdown(ranged)
            r1, r2, r3, r4 = st.columns(4)
            r1.metric("Net P/L", utils.format_currency(k["total_pnl"], CURRENCY_SYMBOL))
            r2.metric("Trades", k["total_trades"])
            r3.metric("Win Rate", f"{k['win_rate']:.1f}%")
            r4.metric("Total Costs", utils.format_currency(costs["total_costs"], CURRENCY_SYMBOL))

            dpnl = utils.daily_pnl(ranged)
            colors = [GREEN if v >= 0 else RED for v in dpnl["pnl"]]
            fig = go.Figure(go.Bar(x=dpnl["date"].astype(str), y=dpnl["pnl"], marker_color=colors,
                                    hovertemplate="%{x}<br>P/L: " + CURRENCY_SYMBOL + "%{y:,.2f}<extra></extra>"))
            fig.update_layout(template=PLOTLY_TEMPLATE, height=340, title="P/L by Day")
            st.plotly_chart(fig, width="stretch")

            st.dataframe(
                dpnl.rename(columns={"date": "Date", "pnl": "P/L", "trades": "Trades"}),
                width="stretch", hide_index=True,
            )


# ---------------------------------------------------------------------------
# PAGE: Add Trade
# ---------------------------------------------------------------------------
elif page == "➕ Add Trade":
    st.markdown("Log a completed trade manually. P/L auto-calculates from entry/exit if you leave it at 0, "
                 "or override it directly (useful for options/futures where lot math differs).")

    with st.form("add_trade_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            instrument = st.text_input("Instrument *", placeholder="e.g. XAUUSD, NIFTY24AUGFUT")
            side = st.selectbox("Side *", ["Buy", "Sell"])
            lot_size = st.number_input("Lot / Quantity *", min_value=0.0, value=1.0, step=0.01, format="%.4f")
        with c2:
            entry_price = st.number_input("Entry Price", min_value=0.0, value=0.0, step=0.01, format="%.4f")
            exit_price = st.number_input("Exit Price", min_value=0.0, value=0.0, step=0.01, format="%.4f")
            strategy = st.text_input("Strategy / Setup", placeholder="e.g. ORB, ICT FVG, Breakout")
        with c3:
            entry_date = st.date_input("Entry Date", value=date.today())
            entry_time_val = st.time_input("Entry Time", value=dtime(9, 15))
            exit_date = st.date_input("Exit Date", value=date.today())
            exit_time_val = st.time_input("Exit Time", value=dtime(9, 30))

        st.markdown("**Costs & Manual P/L**")
        c4, c5, c6, c7 = st.columns(4)
        with c4:
            fee = st.number_input("Fee", value=0.0, step=0.01, format="%.2f")
        with c5:
            tax = st.number_input("Tax", value=0.0, step=0.01, format="%.2f")
        with c6:
            commission = st.number_input("Commission", value=0.0, step=0.01, format="%.2f")
        with c7:
            manual_pnl = st.number_input("P/L (override, optional)", value=0.0, step=0.01, format="%.2f",
                                          help="Leave as 0 to auto-calculate from entry/exit × lot size × side.")

        remarks = st.text_area("Remarks / Notes", placeholder="Trade rationale, mistakes, lessons...")
        submitted = st.form_submit_button("💾 Save Trade", width="stretch", type="primary")

        if submitted:
            if not instrument.strip():
                st.error("Instrument is required.")
            else:
                if manual_pnl != 0:
                    gross_pnl = manual_pnl
                elif entry_price and exit_price:
                    direction = 1 if side == "Buy" else -1
                    gross_pnl = (exit_price - entry_price) * lot_size * direction
                else:
                    gross_pnl = 0.0
                pnl = gross_pnl - (abs(fee) + abs(tax) + abs(commission))

                entry_dt = datetime.combine(entry_date, entry_time_val)
                exit_dt = datetime.combine(exit_date, exit_time_val)

                trade = {
                    "instrument": instrument.strip().upper(),
                    "side": side,
                    "lot_size": lot_size,
                    "entry_price": entry_price or None,
                    "exit_price": exit_price or None,
                    "entry_time": entry_dt.isoformat(sep=" "),
                    "exit_time": exit_dt.isoformat(sep=" "),
                    "fee": fee,
                    "tax": tax,
                    "commission": commission,
                    "swap": 0.0,
                    "pnl": round(pnl, 2),
                    "gross_pnl": round(gross_pnl, 2),
                    "points": (exit_price - entry_price) if (entry_price and exit_price) else None,
                    "strategy": strategy.strip() or None,
                    "remarks": remarks.strip() or None,
                    "source": "manual",
                }
                db.insert_manual_trade(trade)
                st.success(f"Trade saved — net P/L: {utils.format_currency(pnl, CURRENCY_SYMBOL)}")
                st.balloons()


# ---------------------------------------------------------------------------
# PAGE: Import CSV
# ---------------------------------------------------------------------------
elif page == "📥 Import CSV":
    st.markdown(
        "Upload the **Closed Trades Report** CSV exported by your broker. "
        "Trades are matched on their broker **Order Number**, so re-uploading the same "
        "or an overlapping file will never create duplicates."
    )

    uploaded = st.file_uploader("Drop your broker CSV here", type=["csv"])

    if uploaded is not None:
        try:
            parsed_df, report_title = utils.parse_broker_csv(uploaded.getvalue())
        except utils.CSVFormatError as e:
            st.error(str(e))
            parsed_df = None

        if parsed_df is not None:
            if report_title:
                st.caption(f"📄 {report_title}")

            st.markdown(f"**{len(parsed_df)} trades** found in file.")
            preview_cols = ["broker_order_id", "instrument", "side", "lot_size",
                             "entry_price", "exit_price", "exit_time", "pnl", "remarks"]
            preview_cols = [c for c in preview_cols if c in parsed_df.columns]
            st.dataframe(parsed_df[preview_cols].head(20), width="stretch", hide_index=True)

            total_pnl_preview = parsed_df["pnl"].sum()
            m1, m2, m3 = st.columns(3)
            m1.metric("Trades in file", len(parsed_df))
            m2.metric("Net P/L in file", utils.format_currency(total_pnl_preview, CURRENCY_SYMBOL))
            m3.metric("Instruments", parsed_df["instrument"].nunique())

            if st.button("📥 Import into Journal", type="primary", width="stretch"):
                result = db.bulk_upsert_csv_trades(parsed_df)
                st.success(f"Imported **{result['inserted']}** new trades. "
                           f"Skipped **{result['skipped']}** already in your journal.")
                if result["inserted"] > 0:
                    st.balloons()
    else:
        st.info("Expected columns: Order Number, Account, Trading Instrument, Order Type, Lot, "
                "Opening Price, Opening Time, Closing Price, Closing Time, Fee, Tax, Commission, "
                "Swap (Overnight Interest), P/L, Point, Remarks, Archived.")


# ---------------------------------------------------------------------------
# PAGE: Capital
# ---------------------------------------------------------------------------
elif page == "💰 Capital":
    st.caption(
        "Log every deposit or withdrawal here. This lets your P/L be measured as a "
        "**% return** (via the Modified Dietz method, which accounts for exactly when "
        "money moved) instead of just a raw $ number — useful since your trading "
        "capital changes often."
    )

    bal = db.current_capital_balance()
    st.markdown(f"""
        <div class="kpi-card" style="max-width:340px;margin-bottom:20px;">
            <div class="kpi-label">CURRENT CAPITAL BALANCE</div>
            <div class="kpi-value">{utils.format_currency(bal, CURRENCY_SYMBOL)}</div>
            <div class="kpi-sub">Sum of all deposits minus withdrawals logged below</div>
        </div>
    """, unsafe_allow_html=True)

    with st.form("capital_tx_form", clear_on_submit=True):
        st.markdown('<div class="section-title">Log a Deposit or Withdrawal</div>', unsafe_allow_html=True)
        fc1, fc2, fc3 = st.columns([1, 1, 2])
        tx_date = fc1.date_input("Date", value=date.today())
        tx_type = fc2.radio("Type", ["Deposit", "Withdrawal"], horizontal=True)
        tx_note = fc3.text_input("Note (optional)", placeholder="e.g. moved profits to broker B")
        tx_amount = st.number_input("Amount", min_value=0.0, step=100.0, format="%.2f")
        submitted = st.form_submit_button("Add Entry", type="primary")
        if submitted:
            if tx_amount <= 0:
                st.error("Enter an amount greater than 0.")
            else:
                signed = tx_amount if tx_type == "Deposit" else -tx_amount
                db.insert_capital_transaction(
                    date=datetime.combine(tx_date, dtime.min).isoformat(),
                    amount=signed,
                    note=tx_note,
                )
                st.success(f"Logged {tx_type.lower()} of {utils.format_currency(tx_amount, CURRENCY_SYMBOL)}.")
                st.rerun()

    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
    st.markdown('<div class="section-title">History</div>', unsafe_allow_html=True)
    cap_df = db.fetch_capital_transactions()
    if cap_df.empty:
        st.info("No capital transactions logged yet.")
    else:
        display_df = cap_df.copy()
        display_df["date"] = pd.to_datetime(display_df["date"]).dt.strftime("%Y-%m-%d")
        display_df["Type"] = display_df["amount"].apply(lambda a: "Deposit" if a >= 0 else "Withdrawal")
        display_df["Amount"] = display_df["amount"].apply(lambda a: utils.format_currency(abs(a), CURRENCY_SYMBOL))
        display_df = display_df.rename(columns={"date": "Date", "note": "Note"})
        st.dataframe(
            display_df[["id", "Date", "Type", "Amount", "Note"]].sort_values("id", ascending=False),
            width="stretch", hide_index=True,
            column_config={"id": "ID"},
        )

        with st.expander("✏️ Edit or delete an entry"):
            tx_ids = cap_df["id"].tolist()
            if tx_ids:
                sel_id = st.selectbox("Select entry ID", tx_ids)
                sel_row = cap_df[cap_df["id"] == sel_id].iloc[0]
                ec1, ec2, ec3 = st.columns([1, 1, 2])
                new_date = ec1.date_input("Date", value=pd.to_datetime(sel_row["date"]).date(), key="edit_cap_date")
                new_type = ec2.radio("Type", ["Deposit", "Withdrawal"],
                                      index=0 if sel_row["amount"] >= 0 else 1,
                                      horizontal=True, key="edit_cap_type")
                new_note = ec3.text_input("Note", value=sel_row.get("note", "") or "", key="edit_cap_note")
                new_amount = st.number_input("Amount", min_value=0.0, value=abs(float(sel_row["amount"])),
                                              step=100.0, format="%.2f", key="edit_cap_amount")
                b1, b2 = st.columns(2)
                if b1.button("Save Changes", type="primary", key="save_cap_edit"):
                    signed = new_amount if new_type == "Deposit" else -new_amount
                    db.update_capital_transaction(
                        int(sel_id), datetime.combine(new_date, dtime.min).isoformat(), signed, new_note
                    )
                    st.success("Updated.")
                    st.rerun()
                if b2.button("🗑️ Delete Entry", key="delete_cap_entry"):
                    db.delete_capital_transaction(int(sel_id))
                    st.success("Deleted.")
                    st.rerun()


# ---------------------------------------------------------------------------
# PAGE: Trade History
# ---------------------------------------------------------------------------
elif page == "📜 Trade History":
    all_df = db.fetch_trades()

    if all_df.empty:
        st.info("No trades logged yet.")
    else:
        with st.expander("🔎 Filters", expanded=True):
            filtered = apply_filters_ui(all_df, "hist")

        st.caption(f"Showing {len(filtered)} of {len(all_df)} trades")

        # Add computed columns for margin and return %
        filtered["margin_deployed"] = filtered.apply(utils.margin_deployed, axis=1)
        filtered["return_pct"] = filtered.apply(utils.return_pct, axis=1)

        display_cols = ["id", "exit_time", "instrument", "side", "lot_size", "entry_price",
                         "exit_price", "fee", "tax", "commission", "swap", "pnl",
                         "margin_deployed", "return_pct", "strategy", "remarks", "source"]
        display_cols = [c for c in display_cols if c in filtered.columns]
        edit_df = filtered[display_cols].sort_values("exit_time", ascending=False).reset_index(drop=True)

        edited = st.data_editor(
            edit_df,
            width="stretch",
            hide_index=True,
            num_rows="fixed",
            disabled=["id", "source", "margin_deployed", "return_pct"],
            column_config={
                "id": st.column_config.NumberColumn("ID", width="small"),
                "pnl": st.column_config.NumberColumn("P/L", format="%.2f"),
                "lot_size": st.column_config.NumberColumn("Lot", format="%.4f"),
                "entry_price": st.column_config.NumberColumn("Entry", format="%.4f"),
                "exit_price": st.column_config.NumberColumn("Exit", format="%.4f"),
                "margin_deployed": st.column_config.NumberColumn("Margin ($)", format="%.2f"),
                "return_pct": st.column_config.NumberColumn("Return %", format="%.2f%%"),
                "source": st.column_config.TextColumn("Source", width="small"),
            },
            key="trade_history_editor",
        )

        c1, c2, c3 = st.columns([1, 1, 3])
        with c1:
            if st.button("💾 Save edits", width="stretch"):
                changes = 0
                orig_indexed = edit_df.set_index("id")
                for _, row in edited.iterrows():
                    rid = int(row["id"])
                    updates = {}
                    for col in edit_df.columns:
                        if col == "id" or col in ["margin_deployed", "return_pct", "source"]:
                            continue
                        if row[col] != orig_indexed.loc[rid, col]:
                            updates[col] = row[col]
                    if updates:
                        db.update_trade(rid, updates)
                        changes += 1
                st.success(f"Updated {changes} trade(s).")
                st.rerun()
        with c2:
            del_id = st.number_input("Delete ID", min_value=0, step=1, label_visibility="collapsed",
                                      placeholder="Trade ID to delete")
        with c3:
            if st.button("🗑️ Delete trade by ID", width="content"):
                if del_id:
                    db.delete_trade(int(del_id))
                    st.success(f"Deleted trade #{int(del_id)}.")
                    st.rerun()

        st.download_button(
            "⬇️ Export filtered trades to CSV",
            data=filtered.to_csv(index=False).encode("utf-8"),
            file_name=f"trade_journal_export_{date.today().isoformat()}.csv",
            mime="text/csv",
            width="stretch",
        )


# ---------------------------------------------------------------------------
# PAGE: Analytics
# ---------------------------------------------------------------------------
elif page == "🔍 Analytics":
    all_df = db.fetch_trades()

    if all_df.empty:
        st.info("No trades logged yet.")
    else:
        with st.expander("🔎 Filters", expanded=False):
            filtered = apply_filters_ui(all_df, "an")

        tab1, tab2, tab3, tab4, tab5 = st.tabs(
            ["By Instrument", "By Weekday", "By Hour", "Distribution", "🪙 vs Gold"]
        )

        with tab1:
            byinst = utils.pnl_by_instrument(filtered)
            c1, c2 = st.columns(2)
            with c1:
                colors = [GREEN if v >= 0 else RED for v in byinst["pnl"]]
                fig = go.Figure(go.Bar(x=byinst["instrument"], y=byinst["pnl"], marker_color=colors))
                fig.update_layout(template=PLOTLY_TEMPLATE, height=340, title="Net P/L by Instrument")
                st.plotly_chart(fig, width="stretch")
            with c2:
                fig = go.Figure(go.Bar(x=byinst["instrument"], y=byinst["win_rate"], marker_color=ACCENT))
                fig.update_layout(template=PLOTLY_TEMPLATE, height=340, title="Win Rate % by Instrument",
                                   yaxis_range=[0, 100])
                st.plotly_chart(fig, width="stretch")
            st.dataframe(
                byinst.rename(columns={"instrument": "Instrument", "pnl": "Net P/L",
                                        "trades": "Trades", "win_rate": "Win Rate %"}),
                width="stretch", hide_index=True,
            )

        with tab2:
            bywd = utils.pnl_by_weekday(filtered)
            colors = [GREEN if v >= 0 else RED for v in bywd["pnl"]]
            fig = go.Figure(go.Bar(x=bywd["weekday"], y=bywd["pnl"], marker_color=colors))
            fig.update_layout(template=PLOTLY_TEMPLATE, height=360, title="Net P/L by Weekday")
            st.plotly_chart(fig, width="stretch")

        with tab3:
            byhr = utils.pnl_by_hour(filtered)
            colors = [GREEN if v >= 0 else RED for v in byhr["pnl"]]
            fig = go.Figure(go.Bar(x=byhr["hour"], y=byhr["pnl"], marker_color=colors))
            fig.update_layout(template=PLOTLY_TEMPLATE, height=360, title="Net P/L by Entry Hour",
                               xaxis_title="Hour of Day (Entry Time)")
            st.plotly_chart(fig, width="stretch")

        with tab4:
            fig = go.Figure(go.Histogram(x=filtered["pnl"], nbinsx=30, marker_color=ACCENT))
            fig.update_layout(template=PLOTLY_TEMPLATE, height=360, title="P/L Distribution",
                               xaxis_title=f"P/L ({CURRENCY_SYMBOL})", yaxis_title="Trade count")
            st.plotly_chart(fig, width="stretch")

            k = utils.compute_kpis(filtered)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Gross Profit", utils.format_currency(k["gross_profit"], CURRENCY_SYMBOL))
            c2.metric("Gross Loss", utils.format_currency(-k["gross_loss"], CURRENCY_SYMBOL))
            c3.metric("Avg R:R", "∞" if k["avg_rr"] == float("inf") else f"{k['avg_rr']:.2f}")
            c4.metric("Total Fees Paid", utils.format_currency(k["total_fees"], CURRENCY_SYMBOL))

        with tab5:
            st.caption(
                "Benchmarked against live XAUUSD spot price (Yahoo Finance). "
                "Your P/L is in raw $ (no starting-capital tracking, since your "
                "trading capital changes frequently) — gold's move is shown as a %, "
                "side by side rather than on one shared scale."
            )
            bench_source = filtered if not filtered.empty else all_df
            freq_choice = st.radio(
                "Period", ["Weekly", "Monthly", "Yearly"], horizontal=True, key="gold_bench_freq"
            )
            freq_code = {"Weekly": "W", "Monthly": "M", "Yearly": "Y"}[freq_choice]

            start_d = pd.to_datetime(bench_source["exit_time"]).min().date()
            end_d = date.today() + timedelta(days=1)
            gold_df = fetch_gold_history(str(start_d), str(end_d))

            if gold_df.empty:
                st.warning(
                    "Couldn't fetch live gold price data right now — this needs internet "
                    "access to Yahoo Finance at runtime. If you're running locally without "
                    "internet, or Yahoo is temporarily unreachable, try again shortly."
                )
            else:
                pnl_periods = utils.pnl_by_period(bench_source, freq_code)
                gold_periods = utils.gold_pct_by_period(gold_df, freq_code)
                bench = utils.merge_benchmark(pnl_periods, gold_periods, freq_code)

                capital_df = db.fetch_capital_transactions()
                has_capital = not capital_df.empty
                if has_capital:
                    bench = utils.attach_capital_returns(bench, capital_df, freq_code)
                else:
                    st.info(
                        "Log deposits/withdrawals in **💰 Capital** to also see your "
                        "**% return** here, computed properly even when your capital changes "
                        "mid-period — right now this only compares raw $ P/L against gold's % move."
                    )

                fig = go.Figure()
                bar_colors = [GREEN if v >= 0 else RED for v in bench["pnl"]]
                fig.add_trace(go.Bar(
                    x=bench["period_label"], y=bench["pnl"],
                    name=f"Your P/L ({CURRENCY_SYMBOL})", marker_color=bar_colors, yaxis="y1",
                ))
                fig.add_trace(go.Scatter(
                    x=bench["period_label"], y=bench["gold_pct"],
                    name="Gold % Move", mode="lines+markers",
                    line=dict(color=ACCENT, width=2), yaxis="y2",
                ))
                if has_capital:
                    fig.add_trace(go.Scatter(
                        x=bench["period_label"], y=bench["return_pct"],
                        name="Your Return % (Modified Dietz)", mode="lines+markers",
                        line=dict(color="#F59E0B", width=2, dash="dash"), yaxis="y2",
                    ))
                fig.update_layout(
                    template=PLOTLY_TEMPLATE, height=380,
                    title=f"Your P/L vs Gold Move — {freq_choice}",
                    yaxis=dict(title=f"Your P/L ({CURRENCY_SYMBOL})"),
                    yaxis2=dict(title="% Move / Return", overlaying="y", side="right", showgrid=False),
                    legend=dict(orientation="h", y=1.15, x=0),
                    barmode="relative",
                )
                st.plotly_chart(fig, width="stretch")

                eq = utils.equity_curve_daily(bench_source)
                gold_cum = gold_df.sort_values("date").copy()
                gold_cum["cum_pct"] = (gold_cum["close"] / gold_cum["close"].iloc[0] - 1) * 100
                fig2 = go.Figure()
                fig2.add_trace(go.Scatter(
                    x=eq["date"], y=eq["equity"], name=f"Your Cumulative P/L ({CURRENCY_SYMBOL})",
                    line=dict(color=GREEN, width=2.2), yaxis="y1",
                ))
                fig2.add_trace(go.Scatter(
                    x=gold_cum["date"], y=gold_cum["cum_pct"], name="Gold Cumulative % Return",
                    line=dict(color=ACCENT, width=2, dash="dot"), yaxis="y2",
                ))
                fig2.update_layout(
                    template=PLOTLY_TEMPLATE, height=380,
                    title="Cumulative: Your Equity vs Gold",
                    yaxis=dict(title=f"Your Equity ({CURRENCY_SYMBOL})"),
                    yaxis2=dict(title="Gold Cumulative % Return", overlaying="y", side="right", showgrid=False),
                    legend=dict(orientation="h", y=1.15, x=0),
                )
                st.plotly_chart(fig2, width="stretch")

                table = bench.copy()
                table["Your P/L"] = table["pnl"].apply(lambda v: utils.format_currency(v, CURRENCY_SYMBOL))
                table["Gold Move %"] = table["gold_pct"].apply(
                    lambda v: utils.format_percent(v) if pd.notna(v) else "—"
                )
                table = table.rename(columns={"period_label": "Period", "trades": "Trades"})
                show_cols = ["Period", "Your P/L", "Trades", "Gold Move %"]
                if has_capital:
                    table["Your Return %"] = table["return_pct"].apply(
                        lambda v: utils.format_percent(v) if pd.notna(v) else "—"
                    )
                    show_cols.append("Your Return %")
                st.dataframe(table[show_cols], width="stretch", hide_index=True)


# ---------------------------------------------------------------------------
# PAGE: Settings
# ---------------------------------------------------------------------------
elif page == "⚙️ Settings":
    st.markdown('<div class="section-title">Data Management</div>', unsafe_allow_html=True)

    all_df = db.fetch_trades()
    c1, c2 = st.columns(2)
    with c1:
        st.metric("Total trades in database", len(all_df))
    with c2:
        st.metric("Database file", str(db.DB_PATH.name))

    if not all_df.empty:
        st.download_button(
            "⬇️ Download full backup (CSV)",
            data=all_df.to_csv(index=False).encode("utf-8"),
            file_name=f"trade_journal_backup_{date.today().isoformat()}.csv",
            mime="text/csv",
            width="stretch",
        )

    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

    # ---- NEW: Leverage & Contract Size Settings ----
    st.markdown('<div class="section-title">Margin & Return Calculations</div>', unsafe_allow_html=True)
    with st.form("settings_form"):
        current_leverage = db.get_leverage()
        new_leverage = st.number_input("Leverage", min_value=1.0, value=float(current_leverage), step=0.5, format="%.1f")

        st.markdown("**Contract sizes (lot = 1)**")
        st.caption("Defaults: XAUUSD=100, all others=1")
        # Show all instruments currently in the database plus a blank for adding new
        instruments = db.distinct_instruments()
        if not instruments:
            instruments = ["XAUUSD"]  # fallback
        contract_sizes = {}
        for inst in instruments:
            current_size = db.get_contract_size(inst)
            new_size = st.number_input(
                f"Contract size for {inst}",
                min_value=0.0, value=float(current_size), step=0.1, format="%.2f",
                key=f"cs_{inst}"
            )
            contract_sizes[inst] = new_size

        # Allow adding a new instrument contract size
        new_instr = st.text_input("Add contract size for a new instrument (optional)", placeholder="e.g. BTCUSD")
        if new_instr.strip():
            new_instr = new_instr.strip().upper()
            default_size = st.number_input(f"Contract size for {new_instr}", min_value=0.0, value=1.0, step=0.1, format="%.2f")
        else:
            new_instr = None
            default_size = None

        submitted_settings = st.form_submit_button("Save Settings", type="primary")
        if submitted_settings:
            db.set_setting("leverage", str(new_leverage))
            for inst, size in contract_sizes.items():
                db.set_contract_size(inst, size)
            if new_instr and default_size is not None:
                db.set_contract_size(new_instr, default_size)
            st.success("Settings saved.")
            st.rerun()

    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
    st.markdown('<div class="section-title">⚠️ Danger Zone</div>', unsafe_allow_html=True)
    with st.expander("Wipe all trade data"):
        st.warning("This permanently deletes every trade in the journal. This cannot be undone.")
        confirm = st.text_input("Type DELETE to confirm")
        if st.button("Wipe database", type="primary"):
            if confirm == "DELETE":
                db.wipe_all()
                st.success("All trade data wiped.")
                st.rerun()
            else:
                st.error("Type DELETE exactly to confirm.")

    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
    st.markdown('<div class="section-title">About</div>', unsafe_allow_html=True)
    st.caption(
        "Trade Journal & Accounting Dashboard — logs manual trades and broker CSV imports into a local "
        "SQLite database (`trade_journal.db`), matches broker imports on Order Number to prevent duplicates, "
        "and surfaces performance analytics: equity curve, drawdown, win rate, profit factor, expectancy, "
        "and breakdowns by instrument / weekday / hour."
    )
