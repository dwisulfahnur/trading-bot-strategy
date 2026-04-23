# Trading Backtest Platform

A full-stack backtesting platform for multi-pair trading strategies (XAUUSD, EURUSD, GBPUSD, USDJPY, etc.). Run strategy simulations on historical OHLCV data and explore results through an interactive React dashboard.

---

## Features

- **Multi-pair support**: backtest on XAUUSD, EURUSD, GBPUSD, USDJPY, AUDJPY, EURJPY, GBPJPY, CADJPY, CHFJPY, EURGBP, USTEC, and more
- **6 Strategies**: William Fractal Breakout, N Structure Breakout, Momentum Candle, Order Block (SMC), Support & Resistance Bounce, Grid Trading
- **Multi-timeframe EMA**: apply EMA from a different timeframe (M1–D1) to the chart timeframe
- **Sideways market filters** — choose one per run: ADX, EMA Slope, Choppiness Index, Williams Alligator, or Stochastic RSI
- **Session filtering**: trade only during specific market sessions (Asia, London, New York, or combinations)
- **Momentum candle filter**: require candle body ratio + volume confirmation before entry
- **Multi-position backtesting**: allow up to N simultaneous open positions
- **Break-even stop**: configurable R trigger with optional SL level offset (lock in partial profit)
- Timeframes: M1, M5, M15, H1, H4
- Configurable risk per trade (fixed or compounding)
- Commission-aware profit calculation
- Interactive results dashboard:
  - Equity curve chart with datetime X axis
  - Candlestick price chart with trade entry/exit markers
  - Trade table with lot size and profit (USD) per trade
  - Monthly and yearly performance breakdown
  - Trade statistics (win rate, avg win/loss, etc.)
- Saved results page with filtering, sorting, bulk delete, and side-by-side comparison
- MetaTrader EA code generation
- User authentication with per-user result persistence
- Pluggable strategy architecture — add new strategies without touching the engine

---

## Tech Stack

**Backend:** Python 3.11+, FastAPI, Polars, PyArrow, MongoDB (pymongo), JWT auth

**Frontend:** React 19, TypeScript, Vite, Tailwind CSS, TanStack Query, lightweight-charts, Recharts

---

## Setup

### Prerequisites

- Python 3.11+
- Node.js 18+
- Docker (for MongoDB) or local MongoDB instance
- Historical XAUUSD tick data in `data/parquet/ohlcv/` (see Data section)

### Install

```bash
# Backend
python -m venv venv
source venv/bin/activate
pip install -r backend/requirements.txt

# Frontend
cd frontend && npm install
```

### Start

```bash
# Start MongoDB (required for result persistence)
docker compose up -d

# Start both servers (from project root)
./start.sh
```

Opens:
- Frontend: http://localhost:5173
- Backend API: http://localhost:8000
- API docs: http://localhost:8000/docs

---

## Data

OHLCV parquet files are expected at:

```
data/parquet/ohlcv/{TF}/{SYMBOL}_{TF}_{year}.parquet
```

Examples: `data/parquet/ohlcv/H1/XAUUSD_H1_2025.parquet`, `data/parquet/ohlcv/M15/EURUSD_M15_2024.parquet`

Generate from source tick CSVs:

```bash
source venv/bin/activate
python convert_to_parquet.py
```

---

## CLI Usage

Run backtests directly from the terminal without the web UI:

```bash
source venv/bin/activate

# Basic run (H1, 2025–2026)
python backtest.py --strategy william_fractals --years 2025 2026 --timeframe H1

# Multi-timeframe EMA (D1 EMA on H1 chart)
python backtest.py --strategy william_fractals --years 2024 2025 2026 \
  --timeframe H4 --ema_timeframe D1

# With breakeven stop at 1R, SL moves to lock in 0.5R
python backtest.py --strategy william_fractals --years 2025 --timeframe M15 \
  --breakeven_r 1.0 --breakeven_sl_r 0.5

# Multiple simultaneous positions (up to 3)
python backtest.py --strategy n_structure --years 2025 --timeframe H1 --max_positions 3

# Session-filtered (London + New York overlap only)
python backtest.py --strategy n_structure --years 2025 2026 \
  --timeframe H1 --sessions london_newyork

# Non-compounding, 2% risk
python backtest.py --strategy william_fractals --years 2025 2026 \
  --timeframe H1 --risk 0.02 --no_compound

python backtest.py --help
```

Results print to stdout and are saved to `result/` as JSON.

---

## Strategies

### William Fractal Breakout

Enters long when price breaks above a confirmed fractal top in an uptrend (price > EMA), and short when price breaks below a confirmed fractal bottom in a downtrend.

| Parameter              | Default | Description                                             |
|------------------------|---------|---------------------------------------------------------|
| `ema_period`           | 200     | EMA period for trend direction                          |
| `ema_timeframe`        | "same"  | EMA timeframe: "same" or M1/M5/M15/H1/H4/D1            |
| `fractal_n`            | 9       | Candles on each side of the fractal center              |
| `rr_ratio`             | 1.5     | Take-profit as multiple of stop-loss                    |
| `sessions`             | "all"   | Session filter (asia/london/newyork combos)             |
| `momentum_candle_filter` | false | Require momentum candle confirmation                     |
| `sideways_filter`      | none    | Sideways/ranging filter (ADX/EMA Slope/Choppiness/etc.) |

### N Structure Breakout

Structure-based breakout strategy using swing high/low detection with configurable stop-loss modes.

| Parameter          | Default | Description                                             |
|--------------------|---------|---------------------------------------------------------|
| `ema_period`       | 200     | EMA period for trend direction                          |
| `ema_timeframe`    | "same"  | EMA timeframe: "same" or M1/M5/M15/H1/H4/D1            |
| `swing_n`          | 5       | Swing detection lookback                                |
| `rr_ratio`         | 2.0     | Take-profit as multiple of stop-loss                    |
| `sl_mode`          | swing_midpoint | SL mode: swing_midpoint/swing_point/signal_candle |
| `pending_cancel`   | max_bars | Pending order cancellation mode                         |
| `max_pending_bars` | 10      | Max bars to hold pending order                          |
| `sessions`         | "all"   | Session filter                                          |
| `sideways_filter`  | none    | Sideways/ranging filter                                 |

### Sideways Filters

Select one via the `sideways_filter` parameter to skip signals during ranging markets:

| Filter        | Description                                                     |
|---------------|-----------------------------------------------------------------|
| `none`        | No filter — all signals pass through (default)                  |
| `adx`         | ADX (Wilder) — signals blocked when ADX < threshold            |
| `ema_slope`   | EMA Slope — signals blocked when \|slope\| < minimum            |
| `choppiness`  | Choppiness Index — signals blocked when CI ≥ max                |
| `alligator`   | Williams Alligator — longs need lips > teeth > jaw; shorts the reverse |
| `stochrsi`    | Stochastic RSI — longs only when StochRSI < oversold; shorts when > overbought |

---

## Results Page (`/results`)

- **Filter** by timeframe, year, drawdown range, and win rate range
- **Sort** any column
- **Select** individual results or use the header checkbox to select all
- **Compare** up to any number of results side-by-side — see performance metrics, strategy params, active filter details, and configuration; differing values are highlighted in amber
- **Delete** individual results or all at once

## Result Detail Page (`/results/{id}`)

Full interactive view of a single backtest result:
- Equity curve chart
- Candlestick price chart with trade markers
- Trade statistics table
- Trade table (sortable, filterable)
- Monthly performance table
- Yearly performance table

---

## Adding a Strategy

1. Create `strategies/{name}.py` extending `BaseStrategy`:

```python
from strategies.base import BaseStrategy
import polars as pl

class MyStrategy(BaseStrategy):
    name = "my_strategy"

    def generate_signals(self, df: pl.DataFrame) -> pl.DataFrame:
        # Must add columns: signal (1/-1/0), sl, tp
        ...
        return df
```

2. Add parameter specs and display name in `backend/routers/data.py`:

```python
STRATEGY_PARAMS["my_strategy"] = [
    {"name": "my_param", "type": "int", "default": 14, "min": 2, "max": 50},
    # type can be: "int", "float", "bool", "str"
    # "str" type requires: "options": ["opt1", "opt2"]
]
DISPLAY_NAMES["my_strategy"] = "My Strategy"
```

The strategy is automatically discovered by the API and appears in the web UI dropdown.

---

## Project Structure

```
backtest-xauusd/
├── backtest.py              # Simulation engine + CLI
├── convert_to_parquet.py    # Data preparation
├── brute_force.py           # Parameter sweep utility
├── strategies/
│   ├── base.py              # Abstract base class
│   ├── william_fractals.py  # William Fractal Breakout
│   ├── n_structure.py       # N Structure Breakout
│   ├── momentum_candle.py  # Momentum Candle
│   ├── order_block_smc.py   # Order Block (SMC)
│   ├── support_resistance.py # Support & Resistance Bounce
│   └── grid.py              # Grid Trading
├── backend/
│   ├── main.py              # FastAPI app + auth routes
│   ├── models.py            # Pydantic models
│   ├── auth.py              # JWT helpers
│   ├── db.py                # MongoDB client
│   └── routers/             # backtest, data, results, ea
├── frontend/src/
│   ├── api/                 # HTTP client + TypeScript types
│   ├── components/          # UI components
│   ├── pages/               # Home, Results, ResultDetail, Login, Register
│   └── contexts/            # AuthContext
├── data/parquet/ohlcv/      # Input data (not in git)
└── result/                  # Backtest output JSON files
```