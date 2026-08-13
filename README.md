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

Data is stored locally in a SQLite file (`trade_journal.db`) that's created
automatically next to `app.py` the first time you run the app — nothing is
sent anywhere.

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
