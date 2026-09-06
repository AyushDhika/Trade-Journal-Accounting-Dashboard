"""
utils.py — Broker CSV parsing + performance analytics for the Trade Journal.
"""

import io
import re
from datetime import datetime
from typing import Optional

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
    """
    Compute margin (capital deployed) for a single trade row.
    Uses leverage and contract size from settings.
    """
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

    # ---- New: return % statistics ----
    # Compute margin and return for each row (caching for speed)
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
    grouped = d.groupby("weekday").agg(pnl=("pnl", "sum"), trades=("pnl", "count")).reindex(order).dropna(how="all").reset_index()
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
    swap = df.get("swap", pd.Series(dtype=float)).sum()  # swap can be + or -, don't force abs
    net_pnl = df["pnl"].sum()
    gross_pnl = df["gross_pnl"].sum() if "gross_pnl" in df.columns else net_pnl
    total_costs = fee + tax + commission + abs(min(swap, 0))
    return {
        "fee": float(fee), "tax": float(tax), "commission": float(commission),
        "swap": float(swap), "total_costs": float(total_costs),
        "gross_pnl": float(gross_pnl), "net_pnl": float(net_pnl),
    }


def calendar_month_data(df: pd.DataFrame, year: int, month: int) -> pd.DataFrame:
    """Daily P/L + trade count for every day in a given month (df must have exit_time)."""
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
    """P/L aggregated by calendar month, across all history — for a year-over-year overview."""
    if df.empty:
        return pd.DataFrame(columns=["month", "pnl", "trades"])
    d = df.copy()
    d["exit_dt"] = pd.to_datetime(d["exit_time"], errors="coerce")
    d["month"] = d["exit_dt"].dt.to_period("M").astype(str)
    grouped = d.groupby("month").agg(pnl=("pnl", "sum"), trades=("pnl", "count")).reset_index()
    return grouped.sort_values("month")


def equity_curve_daily(df: pd.DataFrame) -> pd.DataFrame:
    """Cumulative P/L reindexed to every calendar day (not just trading days),
    so it can be plotted against a continuous daily price series like gold."""
    if df.empty:
        return pd.DataFrame(columns=["date", "equity"])
    d = daily_pnl(df)
    d["date"] = pd.to_datetime(d["date"])
    d = d.set_index("date").sort_index()
    full_range = pd.date_range(d.index.min(), d.index.max(), freq="D")
    d = d.reindex(full_range)
    d.index.name = "date"
    d["pnl"] = d["pnl"].fillna(0)
    d["equity"] = d["pnl"].cumsum()
    return d.reset_index()[["date", "equity"]]


def pnl_by_period(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Net P/L bucketed into weekly ('W'), monthly ('M') or yearly ('Y')
    periods, keyed by each period's end date so it can be merged against a
    benchmark series bucketed the same way."""
    if df.empty:
        return pd.DataFrame(columns=["period_end", "pnl", "trades"])
    d = df.copy()
    d["exit_dt"] = pd.to_datetime(d["exit_time"], errors="coerce")
    d = d.dropna(subset=["exit_dt"])
    if d.empty:
        return pd.DataFrame(columns=["period_end", "pnl", "trades"])
    grouped = d.groupby(pd.Grouper(key="exit_dt", freq=freq)).agg(
        pnl=("pnl", "sum"), trades=("pnl", "count")
    ).reset_index().rename(columns={"exit_dt": "period_end"})
    return grouped


def gold_pct_by_period(gold_df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Gold's % price change within each period, using the first and last
    close price available inside that bucket."""
    if gold_df.empty:
        return pd.DataFrame(columns=["period_end", "gold_start", "gold_end", "gold_pct"])
    g = gold_df.copy().sort_values("date")
    grouped = g.groupby(pd.Grouper(key="date", freq=freq))["close"].agg(["first", "last"]).reset_index()
    grouped = grouped.rename(columns={"date": "period_end", "first": "gold_start", "last": "gold_end"})
    grouped = grouped.dropna(subset=["gold_start", "gold_end"])
    grouped["gold_pct"] = (grouped["gold_end"] - grouped["gold_start"]) / grouped["gold_start"] * 100
    return grouped


def merge_benchmark(pnl_periods: pd.DataFrame, gold_periods: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Combine your period P/L with gold's period % move into one table,
    labeled for display."""
    merged = pd.merge(pnl_periods, gold_periods, on="period_end", how="outer").sort_values("period_end")
    merged["pnl"] = merged["pnl"].fillna(0.0)
    merged["trades"] = merged["trades"].fillna(0).astype(int)
    if freq == "W":
        merged["period_label"] = merged["period_end"].dt.strftime("Week of %d %b %Y")
    elif freq == "M":
        merged["period_label"] = merged["period_end"].dt.strftime("%b %Y")
    else:
        merged["period_label"] = merged["period_end"].dt.strftime("%Y")
    return merged.reset_index(drop=True)


def period_bounds(period_end: pd.Timestamp, freq: str) -> tuple:
    """Given a period-end timestamp from pnl_by_period/gold_pct_by_period,
    return (period_start, period_end) as full-day-inclusive timestamps."""
    period_end = pd.Timestamp(period_end).normalize()
    if freq == "W":
        start = period_end - pd.Timedelta(days=6)
    elif freq == "M":
        start = period_end.replace(day=1)
    else:  # "Y"
        start = period_end.replace(month=1, day=1)
    end = period_end + pd.Timedelta(hours=23, minutes=59, seconds=59)
    return start, end


def modified_dietz_return(pnl: float, period_start: pd.Timestamp, period_end: pd.Timestamp,
                           capital_df: pd.DataFrame) -> Optional[float]:
    """% return for a period accounting for exactly when capital moved in/out
    during it (Modified Dietz method), so a deposit or withdrawal mid-period
    doesn't distort the return the way a flat start/end balance would.

    capital_df must have 'date' (datetime) and 'amount' (float, +deposit/-withdrawal).
    Returns None if there's no capital base to divide by (e.g. nothing logged yet).
    """
    if capital_df.empty:
        return None
    c = capital_df.copy()
    c["date"] = pd.to_datetime(c["date"])

    bmv = c.loc[c["date"] < period_start, "amount"].sum()
    flows = c.loc[(c["date"] >= period_start) & (c["date"] <= period_end)]

    total_seconds = (period_end - period_start).total_seconds()
    if total_seconds <= 0 or flows.empty:
        weighted_cf = flows["amount"].sum() if not flows.empty else 0.0
    else:
        weights = (period_end - flows["date"]).dt.total_seconds() / total_seconds
        weighted_cf = (flows["amount"] * weights).sum()

    denom = bmv + weighted_cf
    if denom == 0:
        return None
    return pnl / denom * 100


def attach_capital_returns(bench: pd.DataFrame, capital_df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Add a 'return_pct' column to a benchmark table (from merge_benchmark),
    computed via Modified Dietz for each period."""
    out = bench.copy()
    if capital_df.empty:
        out["return_pct"] = None
        return out
    returns = []
    for _, row in out.iterrows():
        p_start, p_end = period_bounds(row["period_end"], freq)
        returns.append(modified_dietz_return(row["pnl"], p_start, p_end, capital_df))
    out["return_pct"] = returns
    return out


def overall_return_pct(total_pnl: float, first_date: pd.Timestamp, last_date: pd.Timestamp,
                        capital_df: pd.DataFrame) -> Optional[float]:
    """Since-inception % return via Modified Dietz, for a single headline KPI."""
    if capital_df.empty or first_date is None or last_date is None:
        return None
    return modified_dietz_return(total_pnl, pd.Timestamp(first_date), pd.Timestamp(last_date), capital_df)


def format_currency(value: float, symbol: str = "$") -> str:
    if value is None or (isinstance(value, float) and (np.isnan(value) or np.isinf(value))):
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}{symbol}{abs(value):,.2f}"


def format_percent(value: float) -> str:
    if value is None or np.isnan(value) or np.isinf(value):
        return "—"
    return f"{value:+.2f}%"
