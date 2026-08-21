"""
db.py — SQLite persistence layer with optional Turso cloud storage.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------
# Turso cloud credentials (read from Streamlit secrets)
# ---------------------------------------------------------------------------
TURSO_URL = st.secrets.get("TURSO_DATABASE_URL")
TURSO_TOKEN = st.secrets.get("TURSO_AUTH_TOKEN")

if TURSO_URL and TURSO_TOKEN:
    import turso
    # Correct signature: turso.connect(remote_url, auth_token)
    # The local cache file is automatically managed by the library.
    _conn = turso.connect(TURSO_URL, TURSO_TOKEN)
    _conn.row_factory = sqlite3.Row
    DB_PATH = None
else:
    _conn = None
    DB_PATH = Path(__file__).parent / "trade_journal.db"

# ---------------------------------------------------------------------------
# Schema (unchanged)
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_order_id     TEXT UNIQUE,
    source              TEXT NOT NULL DEFAULT 'manual',
    account             TEXT,
    instrument          TEXT NOT NULL,
    side                TEXT NOT NULL,
    lot_size            REAL NOT NULL DEFAULT 0,
    entry_price         REAL,
    exit_price          REAL,
    entry_time          TEXT,
    exit_time           TEXT,
    fee                 REAL DEFAULT 0,
    tax                 REAL DEFAULT 0,
    commission          REAL DEFAULT 0,
    swap                REAL DEFAULT 0,
    pnl                 REAL NOT NULL DEFAULT 0,
    gross_pnl           REAL,
    points              REAL,
    strategy            TEXT,
    remarks             TEXT,
    tags                TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time);
CREATE INDEX IF NOT EXISTS idx_trades_instrument ON trades(instrument);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

def init_db():
    """Create tables and apply migrations (works on SQLite and Turso)."""
    if TURSO_URL and TURSO_TOKEN:
        conn = _conn
    else:
        conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row

    try:
        conn.executescript(SCHEMA)
        # Migration: add gross_pnl if missing
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
        if "gross_pnl" not in cols:
            conn.execute("ALTER TABLE trades ADD COLUMN gross_pnl REAL")
            conn.execute("UPDATE trades SET gross_pnl = pnl WHERE gross_pnl IS NULL")

        # Recompute net P/L for CSV imports (idempotent)
        conn.execute("""
            UPDATE trades
            SET pnl = ROUND(
                COALESCE(gross_pnl, pnl)
                + COALESCE(fee, 0) + COALESCE(tax, 0)
                + COALESCE(commission, 0) + COALESCE(swap, 0),
            2)
            WHERE source = 'csv'
        """)

        defaults = [("leverage", "200"), ("contract_size_XAUUSD", "100")]
        for key, val in defaults:
            cur = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
            if cur.fetchone() is None:
                conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, val))
        conn.commit()
    finally:
        if not (TURSO_URL and TURSO_TOKEN):
            conn.close()


@contextmanager
def get_conn():
    if TURSO_URL and TURSO_TOKEN:
        conn = _conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    else:
        conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# All your existing functions remain unchanged.
# ---------------------------------------------------------------------------
def insert_manual_trade(trade: dict) -> int:
    trade = dict(trade)
    trade.setdefault("source", "manual")
    trade.setdefault("created_at", datetime.utcnow().isoformat())
    cols = ", ".join(trade.keys())
    placeholders = ", ".join(["?"] * len(trade))
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
            list(trade.values()),
        )
        return cur.lastrowid


def bulk_upsert_csv_trades(df: pd.DataFrame) -> dict:
    inserted, skipped = 0, 0
    with get_conn() as conn:
        existing_ids = {
            row["broker_order_id"]
            for row in conn.execute(
                "SELECT broker_order_id FROM trades WHERE broker_order_id IS NOT NULL"
            )
        }
        for _, row in df.iterrows():
            oid = str(row["broker_order_id"])
            if oid in existing_ids:
                skipped += 1
                continue
            record = row.to_dict()
            record["created_at"] = datetime.utcnow().isoformat()
            cols = ", ".join(record.keys())
            placeholders = ", ".join(["?"] * len(record))
            conn.execute(
                f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
                list(record.values()),
            )
            existing_ids.add(oid)
            inserted += 1
    return {"inserted": inserted, "skipped": skipped}


def fetch_trades(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    instruments: Optional[list] = None,
) -> pd.DataFrame:
    query = "SELECT * FROM trades WHERE 1=1"
    params: list = []
    if start_date:
        query += " AND date(exit_time) >= date(?)"
        params.append(start_date)
    if end_date:
        query += " AND date(exit_time) <= date(?)"
        params.append(end_date)
    if instruments:
        placeholders = ",".join(["?"] * len(instruments))
        query += f" AND instrument IN ({placeholders})"
        params.extend(instruments)
    query += " ORDER BY exit_time DESC"
    with get_conn() as conn:
        df = pd.read_sql_query(query, conn, params=params)
    return df


def delete_trade(trade_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))


def update_trade(trade_id: int, updates: dict):
    if not updates:
        return
    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    with get_conn() as conn:
        conn.execute(
            f"UPDATE trades SET {set_clause} WHERE id = ?",
            list(updates.values()) + [trade_id],
        )


def distinct_instruments() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT instrument FROM trades ORDER BY instrument"
        ).fetchall()
    return [r["instrument"] for r in rows]


def trade_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"]


def wipe_all():
    with get_conn() as conn:
        conn.execute("DELETE FROM trades")
        conn.execute("DELETE FROM settings")


def get_setting(key: str, default: str = None) -> str:
    with get_conn() as conn:
        cur = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )


def get_leverage() -> float:
    val = get_setting("leverage", "200")
    try:
        return float(val)
    except ValueError:
        return 200.0


def get_contract_size(instrument: str) -> float:
    key = f"contract_size_{instrument.upper()}"
    val = get_setting(key, None)
    if val is None:
        return 100.0 if instrument.upper() == "XAUUSD" else 1.0
    try:
        return float(val)
    except ValueError:
        return 1.0


def set_contract_size(instrument: str, size: float):
    key = f"contract_size_{instrument.upper()}"
    set_setting(key, str(size))
