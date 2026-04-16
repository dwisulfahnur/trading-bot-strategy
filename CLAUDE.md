# CLAUDE.md — XAUUSD Backtest Framework

## Project Overview

A full-stack backtesting app for XAUUSD (Gold/USD) trading strategies. A FastAPI backend runs strategy simulations on local Polars parquet data; a React/TypeScript frontend displays results interactively.

---

## Architecture

```
backtest-xauusd/
├── backtest.py              # Core engine + CLI runner
├── convert_to_parquet.py    # Converts tick CSVs → OHLCV parquet files
├── strategies/
│   ├── base.py              # Abstract BaseStrategy class
│   └── william_fractals.py  # William Fractal Breakout strategy
├── backend/
│   ├── main.py              # FastAPI app entry point
│   ├── models.py            # Pydantic request/response models
│   ├── routers/
│   │   ├── backtest.py      # POST /backtest/run, GET /backtest/status/{id}
│   │   ├── data.py          # GET /strategies, /data/available, /ohlcv
│   │   └── results.py       # GET /results, /results/{id}, DELETE /results, DELETE /results/{id}
│   └── services/
│       └── runner.py        # Background thread job runner
├── frontend/
│   └── src/
│       ├── api/             # Axios client + TypeScript types
│       ├── components/      # BacktestForm, PriceChart, EquityChart, TradeTable, PerMonthTable, ComparePanel
│       └── pages/           # Home.tsx, Results.tsx
├── data/parquet/ohlcv/{TF}/  # XAUUSD_{TF}_{year}.parquet — NOT in git
└── result/                   # JSON output files from backtests
```

---

## Dev Setup

```bash
# Start both servers (from project root)
./start.sh

# Or manually:
source venv/bin/activate
uvicorn backend.main:app --reload --port 8000

cd frontend && npm run dev   # http://localhost:5173
```

API docs: http://localhost:8000/docs

---

## Supported Timeframes

`M1`, `M5`, `M15`, `H1`, `H4`

**Critical:** The timeframe list is duplicated in three places — keep all in sync when adding new timeframes:
1. `backtest.py` — `VALID_TIMEFRAMES`
2. `backend/routers/data.py` — `VALID_TIMEFRAMES`
3. `backend/routers/backtest.py` — `VALID_TIMEFRAMES`

---

## Adding a New Strategy

1. Create `strategies/{name}.py` with a class that extends `BaseStrategy`
2. Implement `generate_signals(df: pl.DataFrame) -> pl.DataFrame`
   - Input: Polars DataFrame with columns `[time, open, high, low, close, ticks, _year]`
   - Must add columns: `signal` (1=buy, -1=sell, 0=none), `sl` (stop-loss price), `tp` (take-profit price)
3. Add parameter specs to `STRATEGY_PARAMS` in `backend/routers/data.py`
   - Supported types: `"int"`, `"float"`, `"bool"`, `"str"` (enum — requires `options: list[str]`)
4. Add display name to `DISPLAY_NAMES` in `backend/routers/data.py`

Strategy discovery is automatic via `importlib` — no other changes needed.

---

## Simulation Engine (`backtest.py`)

- **No lookahead bias**: signal on bar N → entry at bar N+1's open
- **Spread-aware entry**: longs enter at ask (`open + avg_spread`); shorts enter at bid (`open`). `avg_spread` is the mean tick spread aggregated per bar during OHLCV conversion.
- **One position at a time**
- **Exit reasons**: `tp` (take-profit), `sl` (stop-loss), `be` (break-even stop), `end_of_data`
- **Break-even stop**: when `breakeven_r` is set, SL moves to entry once price reaches `entry ± initial_sl_dist × breakeven_r`
- **`_initial_sl_dist`** is stored on Trade at entry and never changed — used for pnl_r calc even after SL moves
- **Compounding**: optional — either % of current capital or fixed risk amount
- **Lot size**: computed per trade as `risk_amount / sl_distance / 100` (standard lots, 1 lot = 100 oz XAUUSD)
- **Profit USD**: `risk_amount × pnl_r` — saved alongside each trade record

### Result filename convention

Only the active sideways filter's sub-params are included in the filename to avoid hitting the OS 255-char limit. All params are still saved in the JSON payload. The filter sub-param prefix mapping:

| Filter      | Prefix          |
|-------------|-----------------|
| adx         | `adx_`          |
| ema_slope   | `ema_slope_`    |
| choppiness  | `choppiness_`   |
| alligator   | `alligator_`    |
| stochrsi    | `stochrsi_`     |

---

## Data Files

- Location: `data/parquet/ohlcv/{TF}/XAUUSD_{TF}_{year}.parquet`
- Schema: `time` (Datetime ms UTC), `open`, `high`, `low`, `close`, `ticks`
- Generate with `convert_to_parquet.py` from source tick CSVs
- H4 is resampled from tick data using `dt.truncate("4h")`

---

## Frontend Notes

- **PriceChart**: uses `lightweight-charts` v5. Trade markers must attach to the candlestick series directly (not a separate line series) for `aboveBar`/`belowBar` positioning to work.
- **PerMonthTable** (titled "Monthly Performance"): computed entirely on the frontend from `trades[].entry_time` and `capital_after` — no backend endpoint needed.
- **Timeframe buttons**: sourced from `/data/available` response, not hardcoded.
- **Breakeven R**: amber toggle + number input; sends `breakeven_r: null` when disabled.
- **EquityChart**: X axis uses `exit_time` (datetime) from equity curve points, formatted as `"Mon DD"` ticks. Tooltip shows full datetime.
- **TradeTable**: columns are — #, Dir, Entry Time, Entry, SL, TP, Exit Time, Exit, Reason, Lot, Profit (USD), Capital. Sortable, filterable by direction and exit reason.
- **Results page** (`/results`):
  - Filters: timeframe toggle, year toggle, drawdown range, win rate range
  - Select-all checkbox in header (supports indeterminate state)
  - Delete All button (bulk `DELETE /results` with body `[id, ...]`)
  - Tooltips on TF and PF column headers (rendered via `createPortal` to avoid `overflow-x-auto` clipping)
- **ComparePanel**: shows Performance metrics + Strategy params + Sideways Filter (with active sub-params) + Configuration. Values that differ across compared results are highlighted amber.

---

## William Fractal Strategy Parameters

### Base parameters

| Parameter      | Default | Description                                    |
|----------------|---------|------------------------------------------------|
| `ema_period`   | 200     | EMA period for trend filter                    |
| `fractal_n`    | 9       | Candles on **each side** of the fractal center |
| `rr_ratio`     | 1.5     | Take-profit = `rr_ratio × stop-loss distance`  |
| `sideways_filter` | none | Which sideways/ranging filter to apply         |

Note: `fractal_n` means candles on each side directly (n=9 = 9 left + center + 9 right). It is **not** a total window divided by 2.

### Sideways filter internals

The filter system uses two boolean columns `_trend_ok_long` and `_trend_ok_short` (replacing the old single `_is_trending`). This allows direction-aware filters (Alligator, StochRSI) to gate long and short signals independently.

| Filter       | `_trend_ok_long`              | `_trend_ok_short`             | Sub-params                                        |
|--------------|-------------------------------|-------------------------------|---------------------------------------------------|
| `none`       | always True                   | always True                   | —                                                 |
| `adx`        | ADX ≥ threshold               | ADX ≥ threshold               | `adx_period`, `adx_threshold`                     |
| `ema_slope`  | \|slope\| ≥ min               | \|slope\| ≥ min               | `ema_slope_period`, `ema_slope_min`               |
| `choppiness` | CI < max                      | CI < max                      | `choppiness_period`, `choppiness_max`             |
| `alligator`  | lips > teeth > jaw            | jaw > teeth > lips            | `alligator_jaw`, `alligator_teeth`, `alligator_lips` |
| `stochrsi`   | StochRSI < oversold           | StochRSI > overbought         | `stochrsi_rsi_period`, `stochrsi_stoch_period`, `stochrsi_oversold`, `stochrsi_overbought` |

### Frontend conditional param visibility

`BacktestForm.tsx` hides sub-params that don't belong to the active filter using prefix matching:
- `adx_*` → shown only when filter = `adx`
- `ema_slope_*` → shown only when filter = `ema_slope`
- `choppiness_*` → shown only when filter = `choppiness`
- `alligator_*` → shown only when filter = `alligator`
- `stochrsi_*` → shown only when filter = `stochrsi`

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/strategies` | List strategies with parameter specs |
| GET | `/data/available` | Available years and timeframes |
| GET | `/ohlcv` | OHLCV bars (query: timeframe, years, date_from, date_to) |
| POST | `/backtest/run` | Start backtest job → returns `{job_id}` |
| GET | `/backtest/status/{job_id}` | Poll job status |
| GET | `/results` | List all saved results (includes `parameters` field) |
| GET | `/results/{id}` | Full result with trades and equity curve |
| DELETE | `/results/{id}` | Delete a single result |
| DELETE | `/results` | Bulk delete — body: `["id1", "id2", ...]` |

---

## CLI Usage

```bash
source venv/bin/activate

python backtest.py --strategy william_fractals --years 2025 2026 --timeframe H1
python backtest.py --strategy william_fractals --years 2024 2025 2026 --timeframe H4 --fractal_period 7 --rr_ratio 2.0
python backtest.py --strategy william_fractals --years 2025 --timeframe M15 --breakeven_r 1.0
python backtest.py --help
```

Results are saved to `result/` as JSON files named by strategy + timeframe + years + active params.
