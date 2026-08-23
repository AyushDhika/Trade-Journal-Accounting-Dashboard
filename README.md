# Trade Journal & Accounting Dashboard

A professional, self-hosted trading journal built with Streamlit. Log trades
manually or import your broker's closed-trades CSV, and get instant
performance analytics: equity curve, drawdown, win rate, profit factor,
expectancy, and breakdowns by instrument / weekday / hour.

## Features

- **Dashboard** — KPI cards (net P/L, win rate, profit factor, max drawdown,
  avg win/loss, expectancy, current streak), equity curve, daily P/L,
  P/L by instrument, recent trades table. Filterable by date range,
  instrument, and side.
- **Add Trade** — manual entry form. P/L auto-calculates from entry/exit ×
  lot size × side, or you can override it directly (useful for options,
  futures, or instruments where P/L isn't a simple price-delta calc).
- **Import CSV** — upload your broker's *Closed Trades Report* export.
  Trades are matched on the broker's **Order Number**, so you can re-upload
  the same or an overlapping file at any time without creating duplicates.
- **Trade History** — full editable table (edit any field, delete by ID),
  with the same filters as the dashboard, plus CSV export.
- **Analytics** — deeper breakdowns: P/L & win-rate by instrument, P/L by
  weekday, P/L by entry hour, P/L distribution histogram, gross profit/loss,
  average R:R, total fees paid.
- **Settings** — full backup export, and a confirm-to-wipe danger zone.

Data is stored in SQLite by default (`trade_journal.db`, created automatically
next to `app.py` on first run) — nothing is sent anywhere. **On Streamlit
Community Cloud specifically, this local file is wiped whenever the app
sleeps, reboots, or you push new code**, since the container filesystem
outside your git repo is ephemeral. To keep your trades persistent across
sleeps/reboots, connect a free [Turso](https://turso.tech) database — see
**Persistent storage on Streamlit Cloud** below. The app auto-detects
whether Turso credentials are present and switches backends with zero code
changes on your end.

## Persistent storage on Streamlit Cloud (Turso)

Turso is a free, hosted, SQLite-compatible database — the app already
speaks its exact SQL dialect, so connecting it is just a couple of
credentials, no code changes needed.

1. **Create a Turso account & database** — go to [turso.tech](https://turso.tech),
   sign up (free tier is generous — 500 databases, 9 GB storage), then either:
   - Use the web dashboard: **Create Database** → name it (e.g. `trade-journal`) → pick a region close to you.
   - Or via CLI: `curl -sSfL https://get.tur.so/install.sh | bash`, then
     `turso auth login`, then `turso db create trade-journal`.
2. **Get your database URL**:
   - Dashboard: open the database → copy the **URL** shown (starts with `libsql://...`).
   - CLI: `turso db show trade-journal --url`
3. **Create an auth token**:
   - Dashboard: database page → **Create Token**.
   - CLI: `turso db tokens create trade-journal`
4. **Add both as Secrets in Streamlit Cloud**: open your app → **Manage app**
   (bottom right) → **Settings** → **Secrets**, and paste:
   ```toml
   TURSO_DATABASE_URL = "libsql://your-db-name-yourorg.turso.io"
   TURSO_AUTH_TOKEN = "your-token-here"
   ```
5. **Reboot the app**. Open **⚙️ Settings** in the sidebar — it should now
   show "✅ Connected to Turso (persistent cloud)". Re-import your CSV one
   last time (since the old ephemeral data won't carry over automatically)
   and it'll persist from then on.

For local development, just don't set these two variables (env vars also
work, e.g. `export TURSO_DATABASE_URL=...`) and the app quietly falls back
to the local SQLite file — no other changes needed either way.

## Margin, leverage & % return

The **⚙️ Settings** page lets you set your account leverage (e.g. `200` for
1:200) and a contract size per instrument (defaults: XAUUSD = 100 oz/lot,
BTCUSD/ETHUSD = 1 coin/lot, EURUSD = 100,000 units/lot — edit or add rows
as needed). These feed the **Return on Capital Deployed** section on the
Dashboard and the `return_pct` / `margin_deployed` columns in Trade History:

```
margin_deployed = (lot_size × contract_size × entry_price) ÷ leverage
return_pct      = net_pnl ÷ margin_deployed × 100
```

This intentionally does **not** use account-balance-based % return, since
that conflates trading performance with deposits/withdrawals — margin-based
% stays accurate regardless of how much cash you've added to the account.

## Setup

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then open the URL Streamlit prints (usually `http://localhost:8501`).

## CSV import format

The importer expects your broker's "Closed Trades Report" export with these
columns (a free-text title line before the header is fine — it's detected
automatically):

```
Order Number, Account, Trading Instrument, Order Type, Lot, Opening Price,
Opening Time, Closing Price, Closing Time, Fee, Tax, Commission,
Swap (Overnight Interest), P/L, Point, Remarks, Archived
```

If your broker's export has slightly different column names, edit the
`BROKER_COLUMN_MAP` dictionary at the top of `utils.py` — just point each
of your CSV's column headers to the matching internal field name.

## Project structure

```
trade_journal/
├── app.py                 # Streamlit UI — all pages
├── db.py                  # SQLite persistence layer
├── utils.py                # CSV parsing + analytics functions
├── requirements.txt
├── .streamlit/config.toml  # Dark trading-terminal theme
└── trade_journal.db        # created automatically on first run
```

## Customizing

- **Currency symbol** — change `CURRENCY_SYMBOL` near the top of `app.py`
  (defaults to ₹).
- **Theme colors** — edit `.streamlit/config.toml` and the `GREEN` / `RED` /
  `ACCENT` constants in `app.py`.
- **Extra broker columns** — extend `BROKER_COLUMN_MAP` in `utils.py`.

## Notes

- This is a single-user local app — the SQLite file lives on whatever
  machine runs `streamlit run app.py`. If you want to access it from
  multiple devices, host it on a small VPS or Streamlit Community Cloud
  and treat the `.db` file as data you back up periodically (use the
  **Settings → Download full backup** button regularly).
