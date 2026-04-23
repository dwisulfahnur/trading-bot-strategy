# CLAUDE.md — Trading Backtest Platform

## Project Overview

A full-stack backtesting platform for multi-pair trading strategies (XAUUSD, EURUSD, GBPUSD, USDJPY, etc.). A FastAPI backend runs strategy simulations on local Polars parquet data; a React/TypeScript frontend displays results interactively. Per-user results are persisted in MongoDB, with JWT authentication.

---

## Architecture

```
backtest-xauusd/
├── backtest.py              # Core engine + CLI runner
├── convert_to_parquet.py    # Converts tick CSVs → OHLCV parquet files
├── strategies/
│   ├── base.py              # Abstract BaseStrategy class
│   ├── william_fractals.py  # William Fractal Breakout strategy
│   ├── n_structure.py       # N Structure Breakout strategy
│   ├── momentum_candle.py  # Momentum Candle strategy
│   ├── order_block_smc.py   # Order Block (SMC) strategy
│   ├── support_resistance.py # Support & Resistance Bounce strategy
│   └── grid.py              # Grid Trading strategy
├── backend/
│   ├── main.py              # FastAPI app entry point (includes auth routes)
│   ├── models.py            # Pydantic request/response models
│   ├── auth.py              # JWT authentication helpers
│   ├── db.py                # MongoDB client setup
│   └── routers/
│       ├── backtest.py      # POST /backtest/run, GET /backtest/status/{id}
│       ├── data.py          # GET /strategies, /data/available, /ohlcv
│       ├── results.py       # GET /results, /results/{id}, DELETE /results, DELETE /results/{id}
│       └── ea.py            # EA (MetaTrader) integration endpoints
├── frontend/
│   └── src/
│       ├── api/             # Axios client + TypeScript types
│       ├── components/      # BacktestForm, PriceChart, EquityChart, TradeTable, PerMonthTable, ComparePanel, EAModal, ...
│       ├── pages/           # Home.tsx, Results.tsx, ResultDetail.tsx, Login.tsx, Register.tsx
│       └── contexts/        # AuthContext with JWT login/register/logout
├── data/parquet/ohlcv/{TF}/  # {SYMBOL}_{TF}_{year}.parquet — NOT in git (e.g. XAUUSD_H1_2025.parquet, EURUSD_M15_2024.parquet)
├── result/                   # JSON output files from backtests
└── docker-compose.yml        # MongoDB container definition
```

---

## Dev Setup

```bash
# Start MongoDB (required for result persistence)
docker compose up -d

# Start both servers (from project root)
./start.sh

# Or manually:
source venv/bin/activate
uvicorn backend.main:app --reload --port 8000

cd frontend && npm run dev   # http://localhost:5173
```

API docs: http://localhost:8000/docs

---

## Authentication

- **Register**: `POST /auth/register` → `{username, password}` → `{access_token, user_id}`
- **Login**: `POST /auth/login` → `{username, password}` → `{access_token, user_id}`
- All `/results` and `/backtest` endpoints require `Authorization: Bearer <token>` header
- Results are scoped to the authenticated user

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
- **Multiple positions**: `max_positions` param allows simultaneous open trades (default 1)
- **Exit reasons**: `tp` (take-profit), `sl` (stop-loss), `be` (break-even stop), `end_of_data`
- **Break-even stop**: when `breakeven_r` is set, SL moves to entry once price reaches `entry ± initial_sl_dist × breakeven_r`. Second param `breakeven_sl_r` sets where SL moves to (0.0 = entry, 0.5 = lock in 0.5R).
- **`_initial_sl_dist`** is stored on Trade at entry and never changed — used for pnl_r calc even after SL moves
- **Compounding**: optional — either % of current capital or fixed risk amount
- **Lot size**: computed per trade as `risk_amount / sl_distance / 100` (standard lots, 1 lot = 100 oz XAUUSD)
- **Profit USD**: `risk_amount × pnl_r` — saved alongside each trade record
- **Commission**: configurable per trade (default 3.5 USD/pip for XAUUSD)

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

- Location: `data/parquet/ohlcv/{TF}/{SYMBOL}_{TF}_{year}.parquet` (e.g. `XAUUSD_H1_2025.parquet`, `EURUSD_M15_2024.parquet`)
- Schema: `time` (Datetime ms UTC), `open`, `high`, `low`, `close`, `ticks`
- Generate with `convert_to_parquet.py` from source tick CSVs
- H4 is resampled from tick data using `dt.truncate("4h")`

---

## Frontend Notes

- **PriceChart**: uses `lightweight-charts` v5. Trade markers must attach to the candlestick series directly (not a separate line series) for `aboveBar`/`belowBar` positioning to work.
- **PerMonthTable** (titled "Monthly Performance"): computed entirely on the frontend from `trades[].entry_time` and `capital_after` — no backend endpoint needed.
- **PerYearTable**: yearly performance breakdown from trades.
- **PipCurve**: pip-based equity curve chart.
- **TradeStatsTable**: win/loss/break-even statistics.
- **Timeframe buttons**: sourced from `/data/available` response, not hardcoded.
- **Breakeven R**: amber toggle + number input; sends `breakeven_r: null` when disabled. Second input `breakeven_sl_r` controls the SL level after breakeven trigger.
- **EquityChart**: X axis uses `exit_time` (datetime) from equity curve points, formatted as `"Mon DD"` ticks. Tooltip shows full datetime.
- **TradeTable**: columns are — #, Dir, Entry Time, Entry, SL, TP, Exit Time, Exit, Reason, Lot, Profit (USD), Capital. Sortable, filterable by direction and exit reason.
- **Results page** (`/results`):
  - Filters: timeframe toggle, year toggle, drawdown range, win rate range
  - Select-all checkbox in header (supports indeterminate state)
  - Delete All button (bulk `DELETE /results` with body `[id, ...]`)
  - Tooltips on TF and PF column headers (rendered via `createPortal` to avoid `overflow-x-auto` clipping)
- **ResultDetail page** (`/results/{id}`): full equity chart, price chart, trade table, monthly and yearly tables, trade stats.
- **ComparePanel**: shows Performance metrics + Strategy params + Sideways Filter (with active sub-params) + Configuration. Values that differ across compared results are highlighted amber.
- **EAModal**: displays generated MetaTrader EA code for the strategy + timeframe.
- **Auth**: Login and Register pages with JWT-based authentication; protected routes redirect to login.

---

## Strategy Parameters

### William Fractal Breakout

| Parameter              | Default | Description                                             |
|------------------------|---------|---------------------------------------------------------|
| `ema_period`           | 200     | EMA period for trend filter                             |
| `ema_timeframe`        | "same"  | EMA timeframe: "same" or M1/M5/M15/H1/H4/D1             |
| `fractal_n`            | 9       | Candles on **each side** of the fractal center          |
| `rr_ratio`             | 1.5     | Take-profit = `rr_ratio × stop-loss distance`          |
| `sessions`             | "all"   | Trading session filter (asia/london/newyork combos)     |
| `momentum_candle_filter` | false | Require momentum candle confirmation                     |
| `mc_body_ratio_min`    | 0.6     | Min body ratio for momentum candle                       |
| `mc_volume_factor`     | 1.5     | Volume multiplier for momentum candle                    |
| `mc_volume_lookback`   | 20      | Volume lookback period for momentum candle              |
| `sideways_filter`      | none    | Which sideways/ranging filter to apply                  |

### N Structure Breakout

| Parameter              | Default | Description                                             |
|------------------------|---------|---------------------------------------------------------|
| `ema_period`           | 200     | EMA period for trend filter                             |
| `ema_timeframe`        | "same"  | EMA timeframe: "same" or M1/M5/M15/H1/H4/D1             |
| `swing_n`              | 5       | Swing detection lookback                                 |
| `rr_ratio`             | 2.0     | Take-profit = `rr_ratio × stop-loss distance`           |
| `sl_mode`              | swing_midpoint | SL mode: swing_midpoint/swing_point/signal_candle |
| `pending_cancel`       | max_bars | Pending order cancellation: none/max_bars/hl_break/both |
| `max_pending_bars`     | 10      | Max bars to hold pending order before cancellation       |
| `sessions`             | "all"   | Trading session filter                                  |
| `sideways_filter`      | none    | Which sideways/ranging filter to apply                  |

### Sideways Filter Internals

The filter system uses two boolean columns `_trend_ok_long` and `_trend_ok_short` (replacing the old single `_is_trending`). This allows direction-aware filters (Alligator, StochRSI) to gate long and short signals independently.

| Filter       | `_trend_ok_long`              | `_trend_ok_short`             | Sub-params                                        |
|--------------|-------------------------------|-------------------------------|---------------------------------------------------|
| `none`       | always True                   | always True                   | —                                                 |
| `adx`        | ADX ≥ threshold               | ADX ≥ threshold               | `adx_period`, `adx_threshold`                     |
| `ema_slope`  | \|slope\| ≥ min               | \|slope\| ≥ min               | `ema_slope_period`, `ema_slope_min`               |
| `choppiness` | CI < max                      | CI < max                      | `choppiness_period`, `choppiness_max`             |
| `alligator`  | lips > teeth > jaw            | jaw > teeth > lips            | `alligator_jaw`, `alligator_teeth`, `alligator_lips` |
| `stochrsi`   | StochRSI < oversold           | StochRSI > overbought         | `stochrsi_rsi_period`, `stochrsi_stoch_period`, `stochrsi_oversold`, `stochrsi_overbought` |

### Frontend Conditional Param Visibility

`BacktestForm.tsx` hides sub-params that don't belong to the active filter using prefix matching:
- `adx_*` → shown only when filter = `adx`
- `ema_slope_*` → shown only when filter = `ema_slope`
- `choppiness_*` → shown only when filter = `choppiness`
- `alligator_*` → shown only when filter = `alligator`
- `stochrsi_*` → shown only when filter = `stochrsi`
- `mc_*` → shown only when `momentum_candle_filter` = true

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/auth/register` | Register new user → `{access_token, user_id}` |
| POST | `/auth/login` | Login → `{access_token, user_id}` |
| GET | `/strategies` | List strategies with parameter specs |
| GET | `/data/available` | Available years and timeframes |
| GET | `/ohlcv` | OHLCV bars (query: timeframe, years, date_from, date_to) |
| POST | `/backtest/run` | Start backtest job → returns `{job_id}` |
| GET | `/backtest/status/{job_id}` | Poll job status |
| GET | `/results` | List all saved results for authenticated user |
| GET | `/results/{id}` | Full result with trades and equity curve |
| DELETE | `/results/{id}` | Delete a single result |
| DELETE | `/results` | Bulk delete — body: `["id1", "id2", ...]` |
| GET | `/ea/generate` | Generate MetaTrader EA code for strategy/timeframe |

---

## CLI Usage

```bash
source venv/bin/activate

# Basic run on XAUUSD H1 (2025–2026)
python backtest.py --strategy william_fractals --years 2025 2026 --timeframe H1

# EURUSD on H4
python backtest.py --strategy n_structure --symbol EURUSD --years 2024 2025 2026 --timeframe H4

# USDJPY M15 with multi-timeframe EMA
python backtest.py --strategy william_fractals --symbol USDJPY --years 2024 2025 2026 \
  --timeframe M15 --ema_timeframe H1 --fractal_period 7 --rr_ratio 2.0

# With breakeven stop (trigger at 1R, SL moves to entry+0.5R)
python backtest.py --strategy william_fractals --years 2025 --timeframe M15 \
  --breakeven_r 1.0 --breakeven_sl_r 0.5

# Multiple positions (up to 3 simultaneous)
python backtest.py --strategy n_structure --years 2025 --timeframe H1 --max_positions 3

# Non-compounding, 2% risk
python backtest.py --strategy william_fractals --years 2025 2026 \
  --timeframe H1 --risk 0.02 --no_compound

# Session-filtered (London only)
python backtest.py --strategy n_structure --years 2025 --timeframe H1 --sessions london

python backtest.py --help
```

Results are saved to `result/` as JSON files named by strategy + timeframe + years + active params.