# Trading Backtest Strategy

A full-stack backtesting platform for XAUUSD (Gold/USD) trading strategies. Run strategy simulations on historical OHLCV data and explore results through an interactive React dashboard.

---

## Features

- **William Fractal Breakout** strategy with 200 EMA trend filter
- **Sideways market filters** — choose one per run: ADX, EMA Slope, Choppiness Index, Williams Alligator, or Stochastic RSI
- Timeframes: M1, M5, M15, H1, H4
- Configurable risk per trade (fixed or compounding)
- Break-even stop management (configurable R trigger)
- Interactive results dashboard:
  - Equity curve chart with datetime X axis
  - Candlestick price chart with trade entry/exit markers
  - Trade table with lot size and profit (USD) per trade
  - Monthly performance breakdown
- Saved results page with filtering, sorting, bulk delete, and side-by-side comparison
- Pluggable strategy architecture — add new strategies without touching the engine

---

## Tech Stack

**Backend:** Python 3.11+, FastAPI, Polars, PyArrow

**Frontend:** React 19, TypeScript, Vite, Tailwind CSS, TanStack Query, lightweight-charts, Recharts

---

## Setup

### Prerequisites

- Python 3.11+
- Node.js 18+
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
data/parquet/ohlcv/{TF}/XAUUSD_{TF}_{year}.parquet
```

Example: `data/parquet/ohlcv/H1/XAUUSD_H1_2025.parquet`

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

# H4 with custom fractal and RR
python backtest.py --strategy william_fractals --years 2021 2022 2023 2024 2025 2026 \
  --timeframe H4 --fractal_period 7 --rr_ratio 2.0

# With break-even stop at 1R
python backtest.py --strategy william_fractals --years 2025 --timeframe M15 --breakeven_r 1.0

# Non-compounding, 2% risk
python backtest.py --strategy william_fractals --years 2025 2026 \
  --timeframe H1 --risk 0.02 --no_compound

python backtest.py --help
```

Results print to stdout and are saved to `result/` as JSON.

---

## Strategy: William Fractal Breakout

Enters long when price breaks above a confirmed fractal top in an uptrend (price > 200 EMA), and short when price breaks below a confirmed fractal bottom in a downtrend.

### Base Parameters

| Parameter      | Default | Range   | Description                                      |
|----------------|---------|---------|--------------------------------------------------|
| `ema_period`   | 200     | 10–500  | EMA period for trend direction filter            |
| `fractal_n`    | 9       | 2–20    | Candles on each side of the fractal center       |
| `rr_ratio`     | 1.5     | 0.5–5.0 | Take-profit distance as multiple of stop-loss    |

- **Entry**: next bar's open after signal (no lookahead bias)
- **Stop-loss**: entry candle low (long) or high (short)
- **Take-profit**: `entry ± rr_ratio × sl_distance`
- **Break-even**: optional — moves SL to entry when price reaches a configurable R multiple

### Sideways Filters

Select one via the `sideways_filter` parameter to skip signals during ranging markets:

| Filter        | Description                                                     |
|---------------|-----------------------------------------------------------------|
| `none`        | No filter — all signals pass through (default)                  |
| `adx`         | ADX (Wilder) — signals blocked when ADX < threshold            |
| `ema_slope`   | EMA Slope — signals blocked when \|slope\| < minimum           |
| `choppiness`  | Choppiness Index — signals blocked when CI ≥ max               |
| `alligator`   | Williams Alligator — longs need lips > teeth > jaw; shorts the reverse |
| `stochrsi`    | Stochastic RSI — longs only when StochRSI < 20; shorts when > 80 |

Each filter exposes its own configurable sub-parameters in the UI.

---

## Results Page (`/results`)

- **Filter** by timeframe, year, drawdown range, and win rate range
- **Sort** any column
- **Select** individual results or use the header checkbox to select all
- **Compare** up to any number of results side-by-side — see performance metrics, strategy params, active sideways filter details, and configuration; differing values are highlighted in amber
- **Delete** individual results or all at once

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
├── strategies/
│   ├── base.py
│   └── william_fractals.py
├── backend/
│   ├── main.py
│   ├── models.py
│   ├── routers/             # backtest, data, results
│   └── services/runner.py
├── frontend/src/
│   ├── api/                 # HTTP client + TypeScript types
│   ├── components/          # UI components
│   └── pages/               # Home.tsx, Results.tsx
├── data/parquet/ohlcv/      # Input data (not in git)
└── result/                  # Backtest output JSON files
```
