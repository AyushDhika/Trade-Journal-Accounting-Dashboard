"""
utils.py — Broker CSV parsing + performance analytics for the Trade Journal.
Also hosts the XAUUSD market-data layer (live spot + daily history) with
multi-source fallback + bar-frequency validation, and the P/L-vs-Gold
benchmark builder (searchsorted asof lookup — pandas-version-proof).
"""
import io
import json
import urllib.request
from datetime import datetime

import numpy as np
import pandas as pd

from db import get_leverage, get_contract_size

# Columns the broker export uses -> our internal db column names
BROKER_COLUMN_MAP = {
    "Order Number": "broker_order_id",
    "Account": "account",
    "Trading Instrument": "instrument",
    "Order Type": "side",
    "Lot": "lot_size",
    "Opening Price": "entry_price",
    "Opening Time": "entry_time",
    "Closing Price": "exit_price",
    "Closing Time": "exit_time",
    "Fee": "fee",
    "Tax": "tax",
    "Commission": "commission",
    "Swap (Overnight Interest)": "swap",
    "P/L": "pnl",
    "Point": "points",
    "Remarks": "remarks",
    "Archived": "archived",
}

NUMERIC_COLS = ["lot_size", "entry_price", "exit_price", "fee", "tax",
                "commission", "swap", "pnl", "points"]


class CSVFormatError(Exception):
    pass


def parse_broker_csv(file_bytes: bytes) -> tuple[pd.DataFrame, str]:
    """
    Parses the broker 'Closed Trades Report' CSV export.
    The file has a free-text title line before the real header, so we
    locate the header row dynamically rather than assuming line 2.
    Returns (dataframe ready for db insert, report_title)
    """
    text = file_bytes.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()

    header_idx = None
    for i, line in enumerate(lines):
        if "Order Number" in line and "Closing Time" in line:
            header_idx = i
            break
    if header_idx is None:
        raise CSVFormatError(
            "This doesn't look like the broker's Closed Trades Report — "
            "couldn't find the expected header row (Order Number, Closing Time, ...)."
        )

    report_title = lines[0].strip() if header_idx > 0 else ""
    csv_body = "\n".join(lines[header_idx:])
    raw = pd.read_csv(io.StringIO(csv_body), dtype=str)
    raw.columns = [c.strip() for c in raw.columns]

    missing = [c for c in ["Order Number", "P/L", "Closing Time"] if c not in raw.columns]
    if missing:
        raise CSVFormatError(f"Missing expected column(s): {', '.join(missing)}")

    df = pd.DataFrame()
    for broker_col, our_col in BROKER_COLUMN_MAP.items():
        if broker_col in raw.columns:
            df[our_col] = raw[broker_col]

    # Clean numerics: broker uses "--" for empty tax/commission etc.
    for col in NUMERIC_COLS:
        if col in df.columns:
            df[col] = (
                df[col]
                .astype(str)
                .str.replace("--", "0", regex=False)
                .str.replace(",", "", regex=False)
                .replace("", "0")
                .replace("nan", "0")
            )
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    # Normalise datetimes
    for col in ["entry_time", "exit_time"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")

    df["side"] = df["side"].astype(str).str.strip().str.title()
    df["instrument"] = df["instrument"].astype(str).str.strip()
    df["source"] = "csv"
    df["strategy"] = None
    df["tags"] = None

    # The broker's "P/L" column is GROSS — fee/tax/commission/swap are reported
    # separately (and are already signed, e.g. fee = -3 for a cost). Keep the
    # gross figure for reference, and store the NET figure as `pnl`, which is
    # what all analytics in this app are built on.
    if "pnl" in df.columns:
        df["gross_pnl"] = df["pnl"]
        cost_cols = [c for c in ["fee", "tax", "commission", "swap"] if c in df.columns]
        df["pnl"] = df["gross_pnl"] + df[cost_cols].sum(axis=1)

    if "archived" in df.columns:
        df = df.drop(columns=["archived"])

    df = df.dropna(subset=["broker_order_id", "exit_time"])
    return df.reset_index(drop=True), report_title


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------
def margin_deployed(row: pd.Series) -> float:
    """Compute margin (capital deployed) for a single trade row."""
    leverage = get_leverage()
    instr = row.get("instrument", "").upper()
    contract_size = get_contract_size(instr)
    entry = row.get("entry_price", 0.0)
    lot = row.get("lot_size", 0.0)
    if leverage <= 0 or entry <= 0 or lot <= 0:
        return 0.0
    notional = lot * contract_size * entry
    return notional / leverage


def return_pct(row: pd.Series) -> float:
    """Return percentage for a single trade: pnl / margin * 100."""
    margin = margin_deployed(row)
    pnl = row.get("pnl", 0.0)
    if margin == 0:
        return 0.0
    return (pnl / margin) * 100


def compute_kpis(df: pd.DataFrame) -> dict:
    """Core KPI block for the dashboard header."""
    if df.empty:
        return {
            "total_pnl": 0.0, "total_trades": 0, "win_rate": 0.0,
            "profit_factor": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
            "expectancy": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
            "max_drawdown": 0.0, "total_fees": 0.0, "avg_rr": 0.0,
            "current_streak": 0, "gross_profit": 0.0, "gross_loss": 0.0,
            "avg_return_pct": 0.0, "max_return_pct": 0.0, "total_margin_deployed": 0.0,
        }

    pnl = df["pnl"]
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    gross_profit = wins.sum()
    gross_loss = abs(losses.sum())
    win_rate = (len(wins) / len(pnl) * 100) if len(pnl) else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else np.inf
    avg_win = wins.mean() if len(wins) else 0.0
    avg_loss = losses.mean() if len(losses) else 0.0
    expectancy = pnl.mean() if len(pnl) else 0.0
    avg_rr = abs(avg_win / avg_loss) if avg_loss != 0 else np.inf
    total_fees = (
        df.get("fee", pd.Series(dtype=float)).abs().sum()
        + df.get("commission", pd.Series(dtype=float)).abs().sum()
        + df.get("tax", pd.Series(dtype=float)).abs().sum()
    )

    equity = pnl_sorted_cumsum(df)
    running_max = equity.cummax()
    drawdown = equity - running_max
    max_drawdown = drawdown.min() if len(drawdown) else 0.0

    # Current streak (consecutive wins/losses from most recent trade)
    ordered = df.sort_values("exit_time")["pnl"].tolist()
    streak = 0
    if ordered:
        sign = 1 if ordered[-1] > 0 else (-1 if ordered[-1] < 0 else 0)
        for v in reversed(ordered):
            s = 1 if v > 0 else (-1 if v < 0 else 0)
            if s == sign and s != 0:
                streak += 1
            else:
                break
        streak *= sign

    # Return % statistics
    margins = df.apply(margin_deployed, axis=1)
    returns = df.apply(return_pct, axis=1)
    avg_return = returns.mean() if len(returns) else 0.0
    max_return = returns.max() if len(returns) else 0.0
    total_margin = margins.sum()

    return {
        "total_pnl": float(pnl.sum()),
        "total_trades": int(len(df)),
        "win_rate": float(win_rate),
        "profit_factor": float(profit_factor) if np.isfinite(profit_factor) else float("inf"),
        "avg_win": float(avg_win),
        "avg_loss": float(avg_loss),
        "expectancy": float(expectancy),
        "best_trade": float(pnl.max()),
        "worst_trade": float(pnl.min()),
        "max_drawdown": float(max_drawdown),
        "total_fees": float(total_fees),
        "avg_rr": float(avg_rr) if np.isfinite(avg_rr) else float("inf"),
        "current_streak": int(streak),
        "gross_profit": float(gross_profit),
        "gross_loss": float(gross_loss),
        "avg_return_pct": float(avg_return),
        "max_return_pct": float(max_return),
        "total_margin_deployed": float(total_margin),
    }


def pnl_sorted_cumsum(df: pd.DataFrame) -> pd.Series:
    ordered = df.sort_values("exit_time")
    return ordered["pnl"].cumsum().reset_index(drop=True)


def equity_curve(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["exit_time", "pnl", "equity"])
    ordered = df.sort_values("exit_time").copy()
    ordered["equity"] = ordered["pnl"].cumsum()
    return ordered[["exit_time", "pnl", "equity"]]


def daily_pnl(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["date", "pnl", "trades"])
    d = df.copy()
    d["date"] = pd.to_datetime(d["exit_time"]).dt.date
    grouped = d.groupby("date").agg(pnl=("pnl", "sum"), trades=("pnl", "count")).reset_index()
    return grouped


def pnl_by_instrument(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["instrument", "pnl", "trades", "win_rate"])
    rows = []
    for inst, g in df.groupby("instrument"):
        wr = (g["pnl"] > 0).mean() * 100
        rows.append({"instrument": inst, "pnl": g["pnl"].sum(), "trades": len(g), "win_rate": wr})
    return pd.DataFrame(rows).sort_values("pnl", ascending=False)


def pnl_by_weekday(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["weekday", "pnl", "trades"])
    d = df.copy()
    d["weekday"] = pd.to_datetime(d["exit_time"]).dt.day_name()
    order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    grouped = (
        d.groupby("weekday").agg(pnl=("pnl", "sum"), trades=("pnl", "count"))
        .reindex(order).dropna(how="all").reset_index()
    )
    return grouped


def pnl_by_hour(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["hour", "pnl", "trades"])
    d = df.copy()
    d["hour"] = pd.to_datetime(d["entry_time"]).dt.hour
    grouped = d.groupby("hour").agg(pnl=("pnl", "sum"), trades=("pnl", "count")).reset_index()
    return grouped


def cost_breakdown(df: pd.DataFrame) -> dict:
    """Total fees/tax/commission/swap paid, plus gross vs net P/L."""
    if df.empty:
        return {"fee": 0.0, "tax": 0.0, "commission": 0.0, "swap": 0.0,
                "total_costs": 0.0, "gross_pnl": 0.0, "net_pnl": 0.0}
    fee = df.get("fee", pd.Series(dtype=float)).abs().sum()
    tax = df.get("tax", pd.Series(dtype=float)).abs().sum()
    commission = df.get("commission", pd.Series(dtype=float)).abs().sum()
    swap = df.get("swap", pd.Series(dtype=float)).sum()  # swap can be + or -
    net_pnl = df["pnl"].sum()
    gross_pnl = df["gross_pnl"].sum() if "gross_pnl" in df.columns else net_pnl
    total_costs = fee + tax + commission + abs(min(swap, 0))
    return {
        "fee": float(fee), "tax": float(tax), "commission": float(commission),
        "swap": float(swap), "total_costs": float(total_costs),
        "gross_pnl": float(gross_pnl), "net_pnl": float(net_pnl),
    }


def calendar_month_data(df: pd.DataFrame, year: int, month: int) -> pd.DataFrame:
    """Daily P/L + trade count for every day in a given month."""
    if df.empty:
        return pd.DataFrame(columns=["date", "pnl", "trades"])
    d = df.copy()
    d["exit_dt"] = pd.to_datetime(d["exit_time"], errors="coerce")
    mask = (d["exit_dt"].dt.year == year) & (d["exit_dt"].dt.month == month)
    d = d[mask]
    if d.empty:
        return pd.DataFrame(columns=["date", "pnl", "trades"])
    d["date"] = d["exit_dt"].dt.date
    grouped = d.groupby("date").agg(pnl=("pnl", "sum"), trades=("pnl", "count")).reset_index()
    return grouped


def monthly_summary(df: pd.DataFrame) -> pd.DataFrame:
    """P/L aggregated by calendar month, across all history."""
    if df.empty:
        return pd.DataFrame(columns=["month", "pnl", "trades"])
    d = df.copy()
    d["exit_dt"] = pd.to_datetime(d["exit_time"], errors="coerce")
    d["month"] = d["exit_dt"].dt.to_period("M").astype(str)
    grouped = d.groupby("month").agg(pnl=("pnl", "sum"), trades=("pnl", "count")).reset_index()
    return grouped.sort_values("month")


def format_currency(value: float, symbol: str = "$") -> str:
    if value is None or (isinstance(value, float) and (np.isnan(value) or np.isinf(value))):
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}{symbol}{abs(value):,.2f}"


def format_percent(value: float) -> str:
    if value is None or np.isnan(value) or np.isinf(value):
        return "—"
    return f"{value:+.2f}%"


# ===========================================================================
# XAUUSD market data — free, key-less sources with automatic fallback
#
#   Live spot : gold-api.com  ->  Yahoo GC=F (front-month COMEX futures)
#   History   : stooq.com -> stooq.pl -> Yahoo GC=F (10y daily)
#
# Every history source is validated: if the median gap between bars is not
# ~1 day (e.g. a source downgrades to MONTHLY bars), it is rejected and the
# next one is tried.
# ===========================================================================
GOLD_LIVE_URLS = [
    "https://api.gold-api.com/price/XAU",
]
GOLD_YAHOO_LIVE_URLS = [
    "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=1d&interval=1m",
    "https://query2.finance.yahoo.com/v8/finance/chart/GC=F?range=1d&interval=1m",
]
GOLD_HISTORY_STOOQ_URLS = [
    "https://stooq.com/q/d/l/?s=xauusd&i=d",
    "https://stooq.pl/q/d/l/?s=xauusd&i=d",
]
GOLD_HISTORY_YAHOO_URLS = [
    "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=10y&interval=1d",
    "https://query2.finance.yahoo.com/v8/finance/chart/GC=F?range=10y&interval=1d",
]

PERIOD_FREQ = {"Weekly": "W", "Monthly": "M", "Yearly": "Y"}


class GoldDataError(Exception):
    pass


def _http_get(url: str, timeout: int = 12) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _source_name(url: str) -> str:
    try:
        return url.split("//", 1)[1].split("/", 1)[0]
    except Exception:
        return url


def _validate_daily(df: pd.DataFrame) -> pd.DataFrame:
    """Reject payloads that are not genuinely daily bars."""
    if df is None or len(df) < 30:
        raise ValueError("too few rows to be a daily history")
    recent = df.tail(500)
    gaps = recent["date"].diff().dt.days.dropna()
    if gaps.empty:
        raise ValueError("cannot determine bar frequency")
    median_gap = float(gaps.median())
    if median_gap > 3.5:
        raise ValueError(
            f"coarse interval detected (median gap {median_gap:.0f}d — expected ~1d); "
            "source downgraded to non-daily bars, rejecting"
        )
    return df


# ----- live price sources --------------------------------------------------
def _gold_live_goldapi(url: str) -> dict:
    payload = json.loads(_http_get(url).decode("utf-8"))
    price = float(payload.get("price"))
    if price <= 0:
        raise ValueError("bad price payload")
    return {
        "price": price,
        "updated_at": payload.get("updatedAt") or payload.get("createdAt") or "",
    }


def _gold_live_yahoo(url: str) -> dict:
    payload = json.loads(_http_get(url).decode("utf-8"))
    results = (payload.get("chart") or {}).get("result") or []
    if not results:
        raise ValueError("empty chart result")
    meta = results[0].get("meta") or {}
    price = float(meta.get("regularMarketPrice") or 0)
    if price <= 0:
        raise ValueError("no regularMarketPrice in meta")
    ts = meta.get("regularMarketTime")
    updated = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M UTC") if ts else ""
    return {"price": price, "updated_at": updated}


def fetch_gold_live() -> dict:
    """Live XAUUSD price, trying each source in order. {'price','updated_at'}"""
    errors = []
    sources = ([(u, _gold_live_goldapi) for u in GOLD_LIVE_URLS]
               + [(u, _gold_live_yahoo) for u in GOLD_YAHOO_LIVE_URLS])
    for url, fetcher in sources:
        try:
            return fetcher(url)
        except Exception as exc:
            errors.append(f"{_source_name(url)}: {exc}")
    raise GoldDataError("live gold price unavailable — " + " | ".join(errors))


# ----- history sources -------------------------------------------------------
def _gold_history_stooq(url: str) -> pd.DataFrame:
    text = _http_get(url).decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not lines or "Date" not in lines[0]:
        raise ValueError("unexpected payload (rate-limited or blocked)")
    df = pd.read_csv(io.StringIO(text))
    df.columns = [str(c).strip().lower() for c in df.columns]
    required = {"date", "open", "high", "low", "close"}
    if not required.issubset(df.columns):
        raise ValueError(f"missing columns: {sorted(required - set(df.columns))}")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    if df.empty:
        raise ValueError("empty history")
    return _validate_daily(df)


def _gold_history_yahoo(url: str) -> pd.DataFrame:
    payload = json.loads(_http_get(url).decode("utf-8"))
    chart = payload.get("chart") or {}
    results = chart.get("result") or []
    if not results:
        err = chart.get("error") or {}
        raise ValueError(err.get("description", "empty chart result"))
    res = results[0]
    ts = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    if len(ts) == 0 or len(closes) == 0:
        raise ValueError("no rows in chart result")
    df = pd.DataFrame({
        "date": pd.to_datetime(ts, unit="s", utc=True).tz_localize(None).normalize(),
        "open": quote.get("open"),
        "high": quote.get("high"),
        "low": quote.get("low"),
        "close": closes,
    })
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = (df.dropna(subset=["date", "close"])
            .drop_duplicates(subset=["date"], keep="last")
            .sort_values("date").reset_index(drop=True))
    if df.empty:
        raise ValueError("all rows dropped")
    return _validate_daily(df)


def fetch_gold_history() -> pd.DataFrame:
    """
    Daily XAUUSD OHLC history. Tries every source in order and returns the
    first one that yields valid DAILY rows. Columns: date, open, high, low, close.
    """
    errors = []
    sources = ([(u, _gold_history_stooq) for u in GOLD_HISTORY_STOOQ_URLS]
               + [(u, _gold_history_yahoo) for u in GOLD_HISTORY_YAHOO_URLS])
    for url, fetcher in sources:
        try:
            df = fetcher(url)
            if df is not None and not df.empty:
                return df
            errors.append(f"{_source_name(url)}: empty result")
        except Exception as exc:
            errors.append(f"{_source_name(url)}: {exc}")
    raise GoldDataError("all history sources failed — " + " | ".join(errors))


# ----- benchmark builder ------------------------------------------------------
def _period_label(period, freq: str) -> str:
    if freq == "W":
        return period.start_time.strftime("%d %b %y")
    if freq == "M":
        return period.strftime("%b %Y")
    if freq == "Y":
        return str(period.year)
    return str(period)


def build_benchmark(trades_df: pd.DataFrame, gold_df: pd.DataFrame,
                    freq: str = "M") -> pd.DataFrame:
    """
    Align trade P/L with gold price movement in Weekly/Monthly/Yearly buckets.
    One row per period that contains at least one trade:
      period, period_label, trades, net_pnl, gold_close, gold_pct

    gold_close = last daily gold close on/before the period's end date
    gold_pct   = that close vs the close on/before the PREVIOUS period's end

    Implemented with a binary search (np.searchsorted) over epoch floats —
    deliberately NOT pd.merge_asof, whose strict dtype checks break across
    pandas versions (datetime64[ns] vs [s]/[us] MergeError on Python 3.14).
    """
    cols = ["period", "period_label", "trades", "net_pnl", "gold_close", "gold_pct"]
    if trades_df is None or trades_df.empty or gold_df is None or gold_df.empty:
        return pd.DataFrame(columns=cols)

    t = trades_df.copy()
    t["exit_dt"] = pd.to_datetime(t["exit_time"], errors="coerce")
    t = t.dropna(subset=["exit_dt"])
    if t.empty:
        return pd.DataFrame(columns=cols)

    t["period"] = t["exit_dt"].dt.to_period(freq)
    agg = t.groupby("period").agg(net_pnl=("pnl", "sum"), trades=("pnl", "count"))

    g = gold_df[["date", "close"]].copy()
    g["date"] = pd.to_datetime(g["date"], errors="coerce")
    g["close"] = pd.to_numeric(g["close"], errors="coerce")
    g = g.dropna(subset=["date", "close"]).sort_values("date")
    if g.empty:
        return pd.DataFrame(columns=cols)

    # Epoch-second float arrays — immune to pandas datetime64 resolution quirks
    g_epoch = np.array([pd.Timestamp(d).timestamp() for d in g["date"]], dtype="float64")
    g_close = g["close"].to_numpy(dtype="float64")
    TOL_SECONDS = 45.0 * 86400.0  # don't use stale closes older than ~45 days

    def close_at(ts) -> float | None:
        te = pd.Timestamp(ts).timestamp()
        i = int(np.searchsorted(g_epoch, te, side="right")) - 1
        if i < 0:
            return None
        if (te - g_epoch[i]) > TOL_SECONDS:
            return None
        return float(g_close[i])

    out = agg.reset_index()
    ends = [p.end_time for p in out["period"]]
    prev_ends = [(p - 1).end_time for p in out["period"]]
    gold_close = [close_at(e) for e in ends]
    prev_close = [close_at(e) for e in prev_ends]

    def _pct(c, p):
        if c is None or p is None or not p:
            return None
        return (c / p - 1) * 100

    out["gold_close"] = gold_close
    out["gold_pct"] = [_pct(c, p) for c, p in zip(gold_close, prev_close)]
    out["period_label"] = out["period"].map(lambda p: _period_label(p, freq))
    return out[cols]
