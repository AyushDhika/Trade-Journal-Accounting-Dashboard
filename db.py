"""
db.py — SQLite persistence layer for the Trade Journal dashboard.

All trades (manual entries and CSV imports) live in a single `trades` table.
Broker imports carry a `broker_order_id` used as a de-duplication key so the
same CSV can be re-uploaded safely without creating duplicate rows.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

DB_PATH = Path(__file__).parent / "trade_journal.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_order_id     TEXT UNIQUE,          -- NULL for manual trades
    source              TEXT NOT NULL DEFAULT 'manual',   -- 'manual' | 'csv'
    account              TEXT,
    instrument          TEXT NOT NULL,
    side                TEXT NOT NULL,        -- Buy / Sell
    lot_size            REAL NOT NULL DEFAULT 0,
    entry_price         REAL,
    exit_price          REAL,
    entry_time          TEXT,                 -- ISO 8601
    exit_time           TEXT,                 -- ISO 8601
    fee                 REAL DEFAULT 0,
    tax                 REAL DEFAULT 0,
    commission          REAL DEFAULT 0,
    swap                REAL DEFAULT 0,
    pnl                 REAL NOT NULL DEFAULT 0,   -- NET P/L (after fee/tax/commission/swap)
    gross_pnl           REAL,                       -- P/L before costs (as reported by broker, if known)
    points              REAL,
    strategy             TEXT,
    remarks             TEXT,
    tags                TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time);
CREATE INDEX IF NOT EXISTS idx_trades_instrument ON trades(instrument);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Migration: add gross_pnl to dbs created before this column existed
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
        if "gross_pnl" not in cols:
            conn.execute("ALTER TABLE trades ADD COLUMN gross_pnl REAL")
            conn.execute("UPDATE trades SET gross_pnl = pnl WHERE gross_pnl IS NULL")

        # Correction: earlier versions of the CSV importer stored the broker's
        # GROSS P/L directly in `pnl` without subtracting fee/tax/commission/swap,
        # so gross_pnl and pnl ended up identical for rows imported back then.
        # This recompute is idempotent (safe to run on every boot) and only
        # touches broker-imported rows, never manual entries.
        conn.execute("""
            UPDATE trades
            SET pnl = ROUND(
                COALESCE(gross_pnl, pnl)
                + COALESCE(fee, 0) + COALESCE(tax, 0)
                + COALESCE(commission, 0) + COALESCE(swap, 0),
            2)
            WHERE source = 'csv'
        """)


def insert_manual_trade(trade: dict) -> int:
    """Insert a single manually-entered trade. Returns new row id."""
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
    """
    Insert broker-imported trades, skipping ones already present
    (matched on broker_order_id). Returns counts summary.
    """
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
