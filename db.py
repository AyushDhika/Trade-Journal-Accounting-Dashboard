"""
db.py — Persistence layer for the Trade Journal dashboard.

Backend selection (automatic):
  - If TURSO_DATABASE_URL (+ optionally TURSO_AUTH_TOKEN) is set — via
    environment variables or Streamlit secrets — trades are stored in a
    remote Turso database (libSQL, SQLite-compatible). This is what makes
    data survive Streamlit Community Cloud sleeping/rebooting, since Turso
    is a separate persistent service, not a file inside the app container.
  - Otherwise, falls back to a local SQLite file (trade_journal.db) next to
    this script — fine for local development, but ephemeral on most cloud
    hosts.

Both backends speak the same SQL (libSQL is a SQLite-compatible engine), so
nearly every query below is backend-agnostic. The only real difference is
that raw libSQL rows come back as plain tuples instead of sqlite3.Row, so
`_rows()` below normalizes both into plain dicts before the rest of the
code ever sees them — every other function is unchanged either way.

All trades (manual entries and CSV imports) live in a single `trades` table.
Broker imports carry a `broker_order_id` used as a de-duplication key so the
same CSV can be re-uploaded safely without creating duplicate rows.
"""

"""
db.py — Persistence layer for the Trade Journal dashboard.

Backend selection (automatic):
  - If TURSO_DATABASE_URL (+ TURSO_AUTH_TOKEN) is set — via environment
    variables or Streamlit secrets — trades are stored in a remote Turso
    database over its plain HTTP API (https://docs.turso.tech/sdk/http/reference).
    This is deliberately NOT the libsql-experimental native driver: that
    package requires compiling Rust on install, which fails on Streamlit
    Community Cloud's build image. The HTTP API needs nothing but
    `requests` (already a Streamlit dependency), so there's no native
    build step to ever break.
  - Otherwise, falls back to a local SQLite file (trade_journal.db) next to
    this script — fine for local development, but ephemeral on most cloud
    hosts.

Turso/libSQL speaks the same SQL as SQLite, so nearly every query below is
backend-agnostic. `_TursoHTTPCursor` normalizes its results to look like a
standard DBAPI2 cursor (plain tuples + `.description`), matching sqlite3's
shape closely enough that the rest of this module doesn't need to know or
care which backend is active.

All trades (manual entries and CSV imports) live in a single `trades` table.
Broker imports carry a `broker_order_id` used as a de-duplication key so the
same CSV can be re-uploaded safely without creating duplicate rows.
"""

import base64
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

DB_PATH = Path(__file__).parent / "trade_journal.db"

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS trades (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        broker_order_id     TEXT UNIQUE,
        source              TEXT NOT NULL DEFAULT 'manual',
        account              TEXT,
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
        strategy             TEXT,
        remarks             TEXT,
        tags                TEXT,
        created_at          TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time)",
    "CREATE INDEX IF NOT EXISTS idx_trades_instrument ON trades(instrument)",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)",
    """
    CREATE TABLE IF NOT EXISTS contract_sizes (
        instrument     TEXT PRIMARY KEY,
        contract_size  REAL NOT NULL
    )
    """,
]

DEFAULT_LEVERAGE = 200
DEFAULT_CONTRACT_SIZES = {
    "XAUUSD": 100,      # 1 lot = 100 troy oz (standard for gold CFDs)
    "XAUUSD+": 100,
    "BTCUSD": 1,        # 1 lot = 1 BTC on most crypto-CFD brokers
    "ETHUSD": 1,        # 1 lot = 1 ETH
    "EURUSD": 100000,   # standard forex lot
}


# ---------------------------------------------------------------------------
# Backend connection
# ---------------------------------------------------------------------------

def _turso_creds():
    """Looks for Turso credentials in env vars first, then Streamlit secrets."""
    url = os.environ.get("TURSO_DATABASE_URL")
    token = os.environ.get("TURSO_AUTH_TOKEN")
    if url:
        return url, token
    try:
        import streamlit as st
        url = st.secrets.get("TURSO_DATABASE_URL")
        token = st.secrets.get("TURSO_AUTH_TOKEN")
    except Exception:
        return None, None
    return url, token


def backend_name() -> str:
    url, _ = _turso_creds()
    return "Turso (persistent cloud)" if url else "Local SQLite (ephemeral)"


class TursoError(Exception):
    pass


class _TursoHTTPCursor:
    """Mimics just enough of the DBAPI2 cursor surface (.description,
    .fetchall(), .lastrowid) for the rest of this module to treat it like
    a normal sqlite3 cursor."""

    def __init__(self, result: dict):
        cols = result.get("cols") or []
        self.description = [(c.get("name"),) for c in cols]
        self.lastrowid = result.get("last_insert_rowid")
        self._rows_raw = result.get("rows") or []

    @staticmethod
    def _decode_cell(cell: dict):
        t = cell.get("type")
        if t == "null":
            return None
        if t == "integer":
            return int(cell["value"])
        if t == "float":
            return float(cell["value"])
        if t == "text":
            return cell["value"]
        if t == "blob":
            return base64.b64decode(cell.get("base64", ""))
        return cell.get("value")

    def fetchall(self):
        return [tuple(self._decode_cell(c) for c in row) for row in self._rows_raw]

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None

    def __iter__(self):
        return iter(self.fetchall())


class _TursoHTTPConnection:
    """
    Talks to Turso's /v2/pipeline HTTP endpoint directly via `requests` —
    no native/compiled driver involved. Each execute() is a self-contained
    request+close pipeline (we don't need multi-statement transactions
    here, since every write in this module is already a single statement).
    """

    def __init__(self, url: str, token: str):
        base = url.replace("libsql://", "https://").replace("turso://", "https://")
        self._base = base.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token or ''}",
            "Content-Type": "application/json",
        }
        self._session = requests.Session()

    @staticmethod
    def _to_arg(v):
        if v is None:
            return {"type": "null", "value": None}
        if isinstance(v, bool):
            return {"type": "integer", "value": str(int(v))}
        if isinstance(v, int):
            return {"type": "integer", "value": str(v)}
        if isinstance(v, float):
            return {"type": "float", "value": v}
        if isinstance(v, (bytes, bytearray)):
            return {"type": "blob", "base64": base64.b64encode(v).decode("ascii")}
        return {"type": "text", "value": str(v)}

    def _pipeline(self, statements: list) -> list:
        requests_payload = [{"type": "execute", "stmt": s} for s in statements]
        requests_payload.append({"type": "close"})
        try:
            resp = self._session.post(
                f"{self._base}/v2/pipeline",
                json={"requests": requests_payload},
                headers=self._headers,
                timeout=20,
            )
        except requests.RequestException as e:
            raise TursoError(f"Could not reach Turso database: {e}") from e

        if resp.status_code == 401:
            raise TursoError("Turso auth token was rejected (401). Check TURSO_AUTH_TOKEN.")
        if resp.status_code >= 400:
            raise TursoError(f"Turso HTTP error {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        results = []
        for entry in data.get("results", []):
            if entry.get("type") == "error":
                msg = entry.get("error", {}).get("message", "unknown Turso error")
                raise TursoError(msg)
            if entry.get("response", {}).get("type") == "execute":
                results.append(entry["response"]["result"])
        return results

    def execute(self, sql, params=None):
        stmt = {"sql": sql}
        if params:
            stmt["args"] = [self._to_arg(p) for p in params]
        results = self._pipeline([stmt])
        return _TursoHTTPCursor(results[0] if results else {})

    def executescript(self, script: str):
        statements = [{"sql": s.strip()} for s in script.split(";") if s.strip()]
        for s in statements:
            self._pipeline([s])

    def commit(self):
        pass  # each request already auto-commits server-side; no-op for interface parity

    def close(self):
        self._session.close()


def _raw_connect():
    url, token = _turso_creds()
    if url:
        return _TursoHTTPConnection(url, token)
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_conn():
    raw_conn = _raw_connect()
    conn = _ExecuteTupleWrapper(raw_conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


class _ExecuteTupleWrapper:
    """
    Thin pass-through wrapper that guarantees query parameters are always a
    tuple, since it's cheap insurance against any backend that's stricter
    than sqlite3 about accepted parameter types.
    """
    def __init__(self, raw_conn):
        self._conn = raw_conn

    def execute(self, sql, params=None):
        if params is None:
            return self._conn.execute(sql)
        return self._conn.execute(sql, tuple(params))

    def executescript(self, script):
        return self._conn.executescript(script)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def _rows(cursor) -> list:
    """Normalizes a cursor's fetchall() into a list of plain dicts,
    regardless of backend (sqlite3.Row vs plain tuples)."""
    raw_rows = cursor.fetchall()
    if not raw_rows:
        return []
    if isinstance(raw_rows[0], sqlite3.Row):
        return [dict(r) for r in raw_rows]
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in raw_rows]


# ---------------------------------------------------------------------------
# Schema init + migrations
# ---------------------------------------------------------------------------

def init_db():
    with get_conn() as conn:
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)

        # Migration: add gross_pnl to dbs created before this column existed.
        # Uses try/except instead of PRAGMA table_info so it works identically
        # whether the SQL is executed locally (sqlite3) or over Turso's HTTP
        # API — both understand ALTER TABLE, but PRAGMA introspection syntax
        # support is less certain across backends, so we just attempt the
        # ALTER and treat "already exists" as success.
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN gross_pnl REAL")
            conn.execute("UPDATE trades SET gross_pnl = pnl WHERE gross_pnl IS NULL")
        except Exception as e:
            if "duplicate column" not in str(e).lower() and "already exists" not in str(e).lower():
                raise

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
        id_cur = conn.execute(
            "SELECT broker_order_id FROM trades WHERE broker_order_id IS NOT NULL"
        )
        existing_ids = {r["broker_order_id"] for r in _rows(id_cur)}
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
        cur = conn.execute(query, tuple(params))
        raw_rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
    normalized_rows = [tuple(r) for r in raw_rows]
    return pd.DataFrame(normalized_rows, columns=cols)


def delete_trade(trade_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM trades WHERE id = ?", (int(trade_id),))


def update_trade(trade_id: int, updates: dict):
    if not updates:
        return
    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    with get_conn() as conn:
        conn.execute(
            f"UPDATE trades SET {set_clause} WHERE id = ?",
            list(updates.values()) + [int(trade_id)],
        )


def distinct_instruments() -> list:
    with get_conn() as conn:
        cur = conn.execute("SELECT DISTINCT instrument FROM trades ORDER BY instrument")
        rows = _rows(cur)
    return [r["instrument"] for r in rows]


def trade_count() -> int:
    with get_conn() as conn:
        cur = conn.execute("SELECT COUNT(*) c FROM trades")
        rows = _rows(cur)
    return rows[0]["c"] if rows else 0


def wipe_all():
    with get_conn() as conn:
        conn.execute("DELETE FROM trades")


# ---------------------------------------------------------------------------
# Settings: leverage + per-instrument contract sizes (for margin/% return calc)
# ---------------------------------------------------------------------------

def get_leverage() -> int:
    with get_conn() as conn:
        cur = conn.execute("SELECT value FROM settings WHERE key = 'leverage'")
        rows = _rows(cur)
    return int(rows[0]["value"]) if rows else DEFAULT_LEVERAGE


def set_leverage(leverage: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('leverage', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(leverage),),
        )


def get_contract_sizes() -> dict:
    """Returns {instrument: contract_size}, seeded with sane defaults on first call."""
    with get_conn() as conn:
        cur = conn.execute("SELECT instrument, contract_size FROM contract_sizes")
        rows = _rows(cur)
    sizes = {r["instrument"]: r["contract_size"] for r in rows}
    if not sizes:
        set_contract_sizes(DEFAULT_CONTRACT_SIZES)
        return dict(DEFAULT_CONTRACT_SIZES)
    return sizes


def set_contract_sizes(mapping: dict):
    with get_conn() as conn:
        for instrument, size in mapping.items():
            conn.execute(
                "INSERT INTO contract_sizes (instrument, contract_size) VALUES (?, ?) "
                "ON CONFLICT(instrument) DO UPDATE SET contract_size = excluded.contract_size",
                (instrument, float(size)),
            )


def set_contract_size(instrument: str, size: float):
    set_contract_sizes({instrument: size})


def delete_contract_size(instrument: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM contract_sizes WHERE instrument = ?", (instrument,))
