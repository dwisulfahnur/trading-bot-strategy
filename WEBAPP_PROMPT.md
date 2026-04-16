# Web App Implementation Prompt
## XAUUSD Backtest Web Application

---

## Project Context

This is an existing Python backtesting system for XAUUSD (Gold/USD) trading strategies. It already has:

- `backtest.py` — engine + CLI runner (simulation loop, metrics, JSON output)
- `strategies/base.py` — abstract `BaseStrategy` class
- `strategies/william_fractals.py` — William Fractal Breakout strategy (200 EMA trend filter, fractal breakout signals, configurable SL/TP)
- `convert_to_parquet.py` — data pipeline (tick CSV/ZIP → Parquet)
- `data/parquet/ohlcv/{M1,M5,M15,H1}/XAUUSD_{TF}_{YEAR}.parquet` — OHLCV data for 2021–2026
- `result/*.json` — saved backtest results

The goal is to wrap this into a **FastAPI + ReactJS web application** that lets users configure and run backtests through a UI, view results, and compare runs — without touching the command line.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend API | FastAPI (Python) |
| Frontend | ReactJS + TypeScript |
| Styling | Tailwind CSS |
| Charts | Recharts or Chart.js |
| State management | React Query (server state) + useState (local) |
| Data format | JSON over REST |

---

## Project Structure

```
backtest-xauusd/
├── backend/
│   ├── main.py               ← FastAPI app entry point
│   ├── routers/
│   │   ├── backtest.py       ← POST /backtest/run, GET /backtest/status/{id}
│   │   └── results.py        ← GET /results, GET /results/{id}
│   ├── services/
│   │   └── runner.py         ← wraps existing backtest logic (imports backtest.py)
│   └── models.py             ← Pydantic request/response schemas
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── BacktestForm.tsx      ← parameter input form
│   │   │   ├── ResultCard.tsx        ← summary metrics card
│   │   │   ├── EquityChart.tsx       ← equity curve chart
│   │   │   ├── TradeTable.tsx        ← trade list table
│   │   │   └── ComparePanel.tsx      ← side-by-side result comparison
│   │   ├── pages/
│   │   │   ├── Home.tsx              ← run new backtest
│   │   │   └── Results.tsx           ← browse saved results
│   │   ├── api/
│   │   │   └── client.ts             ← fetch wrappers for backend API
│   │   └── App.tsx
│   ├── package.json
│   └── vite.config.ts
├── backtest.py               ← existing (unchanged)
├── strategies/               ← existing (unchanged)
└── data/                     ← existing (unchanged)
```

---

## Backend: FastAPI

### Key Design Decisions
- Reuse existing `backtest.py` logic directly — import `load_data`, `load_strategy`, `simulate`, `compute_metrics` as functions (do NOT shell out)
- Run backtests in a **background thread** (FastAPI `BackgroundTasks`) since M1 runs can take a few seconds
- Results are stored as JSON files in `result/` (already the existing pattern)
- Strategy discovery is dynamic — auto-detect all `BaseStrategy` subclasses from `strategies/`

### API Endpoints

#### `GET /strategies`
Returns list of available strategies with their configurable parameters.
```json
[
  {
    "name": "william_fractals",
    "display_name": "William Fractal Breakout",
    "parameters": [
      { "name": "ema_period",     "type": "int",   "default": 200,  "min": 10,  "max": 500 },
      { "name": "fractal_period", "type": "int",   "default": 9,    "min": 5,   "max": 21, "step": 2 },
      { "name": "rr_ratio",       "type": "float", "default": 1.5,  "min": 0.5, "max": 5.0 }
    ]
  }
]
```

#### `POST /backtest/run`
Starts a backtest job. Returns a job ID immediately (non-blocking).
```json
// Request
{
  "strategy": "william_fractals",
  "years": [2025, 2026],
  "timeframe": "H1",
  "initial_capital": 10000,
  "risk_pct": 0.02,
  "compound": false,
  "params": {
    "ema_period": 200,
    "fractal_period": 9,
    "rr_ratio": 1.5
  }
}

// Response
{ "job_id": "abc123", "status": "running" }
```

#### `GET /backtest/status/{job_id}`
Poll for job completion.
```json
{ "job_id": "abc123", "status": "done", "result_id": "william_fractals_H1_2025-2026_..." }
// status: "running" | "done" | "error"
```

#### `GET /results`
List all saved result files with summary metadata.
```json
[
  {
    "id": "william_fractals_H1_2025-2026_ema_period200_fractal_period9_rr_ratio1.5",
    "created_at": "2026-04-15T15:10:23Z",
    "strategy": "william_fractals",
    "timeframe": "H1",
    "years": [2025, 2026],
    "total_return_pct": 57.28,
    "win_rate_pct": 47.37,
    "max_drawdown_pct": 14.85,
    "profit_factor": 1.358
  }
]
```

#### `GET /results/{id}`
Full result detail including per-year breakdown and equity curve data points.

#### `DELETE /results/{id}`
Delete a saved result file.

#### `GET /data/available`
Returns which years and timeframes have parquet data available.
```json
{
  "years": [2021, 2022, 2023, 2024, 2025, 2026],
  "timeframes": ["M1", "M5", "M15", "H1"]
}
```

### Pydantic Models (`backend/models.py`)
- `BacktestRequest` — validates the run request
- `BacktestResult` — mirrors the existing JSON result schema
- `ResultSummary` — lightweight version for list view
- `StrategyMeta` — strategy name + parameter specs

---

## Frontend: ReactJS

### Pages

#### Home Page (`/`)
- **Backtest Form** (left panel):
  - Strategy selector dropdown (populated from `GET /strategies`)
  - Year multi-select checkboxes (2021–2026)
  - Timeframe selector (M1 / M5 / M15 / H1)
  - Capital input
  - Risk % slider (0.5% – 5%, step 0.5%)
  - Compound toggle switch
  - Strategy-specific params (dynamic, rendered from strategy metadata)
  - **Run Backtest** button → shows loading spinner → polls status → shows result inline
- **Result Panel** (right panel):
  - Summary metrics cards (Total Return, Win Rate, Profit Factor, Max Drawdown, Risk/Trade)
  - Equity curve chart (line chart, x=trade number or date, y=capital)
  - Per-year breakdown table
  - Trade count by exit reason (TP vs SL) — donut chart

#### Results Page (`/results`)
- Table of all saved results (sortable by return, drawdown, profit factor, date)
- Each row: strategy, timeframe, years, return%, win rate%, drawdown%, profit factor, created date
- **Compare** checkbox on each row → opens side-by-side comparison panel
- **Delete** button per row
- Click row → opens full result detail modal

### Components

#### `BacktestForm.tsx`
- Controlled form with React Hook Form or plain useState
- Dynamically renders strategy parameter inputs based on `GET /strategies` response
- Validates: fractal_period must be odd, years must have at least 1 selected

#### `EquityChart.tsx`
- Line chart of capital over trades using Recharts
- Tooltip shows trade number, capital, trade direction (long/short), exit reason
- Color: green line above initial capital, red below
- Clicking a point on the equity curve highlights the corresponding trade in `TradeTable`

#### `PriceChart.tsx`
- Candlestick-style OHLCV chart (use lightweight-charts by TradingView, free library)
- Overlays each trade as an annotation on the chart:
  - **Entry marker**: up arrow (▲ green) for long, down arrow (▼ red) for short at `entry_price`
  - **Exit marker**: circle at `exit_price`, green if TP hit, red if SL hit
  - **SL line**: dashed red horizontal line from entry to exit at `sl` price
  - **TP line**: dashed green horizontal line from entry to exit at `tp` price
- Clicking a trade marker highlights the row in `TradeTable`
- Time range selector to zoom into a specific date range
- Timeframe matches the backtest timeframe (M1/M5/M15/H1)

#### `TradeTable.tsx`
- Paginated table of all trades with columns:
  `#` | `Direction` | `Entry Time` | `Entry Price` | `SL` | `TP` | `Exit Time` | `Exit Price` | `Exit Reason` | `P&L (R)` | `Capital After`
- Direction shown as colored badge: green LONG / red SHORT
- Exit reason badge: green TP / red SL / grey EOD (end of data)
- P&L column color-coded: green positive, red negative
- Clicking a row scrolls `PriceChart` to that trade's entry time and highlights it
- Sortable columns, filter by direction and exit reason

#### `ResultCard.tsx`
- Grid of metric tiles: Total Return (green/red), Win Rate, Profit Factor, Max Drawdown, Trades, Risk/Trade
- Color-coded: green if positive/good, red if negative/bad

#### `ComparePanel.tsx`
- Side-by-side display of 2–4 selected results
- Highlights best value in each metric row

---

## Result JSON Schema

`backtest.py` already writes the full result to `result/*.json`. The schema is:

```json
{
  "created_at": "2026-04-15T15:10:23Z",
  "strategy": "william_fractals",
  "parameters": {
    "ema_period": 200, "fractal_period": 9, "rr_ratio": 1.5,
    "timeframe": "H1", "years": [2025, 2026],
    "initial_capital": 10000, "risk_pct": 0.02, "compound": false
  },
  "results": {
    "total_trades": 152,
    "win_rate_pct": 47.37,
    "profit_factor": 1.358,
    "total_return_pct": 57.28,
    "initial_capital": 10000,
    "final_capital": 15728.17,
    "max_drawdown_pct": 14.85,
    "risk_pct": 0.02,
    "avg_win_r": 1.51,
    "avg_loss_r": -1.0,
    "per_year": {
      "2025": { "total_trades": 106, "win_rate_pct": 47.17, "return_pct": 36.68 }
    },
    "equity_curve": [
      { "trade": 1, "capital": 10150.0, "direction": "long", "exit_reason": "tp", "pnl_r": 1.51 }
    ],
    "trades": [
      {
        "trade": 1, "year": 2025, "direction": "long",
        "entry_time": "2025-01-15 08:00:00", "entry_price": 2635.5,
        "sl": 2621.3, "tp": 2656.8,
        "exit_time": "2025-01-15 14:00:00", "exit_price": 2656.8,
        "exit_reason": "tp", "pnl_r": 1.5089, "capital_after": 10150.0
      }
    ]
  }
}
```

### API notes
- `GET /results` returns only summary fields (no `equity_curve` or `trades`) for fast list rendering
- `GET /results/{id}` returns the full payload including `equity_curve` and `trades`
- `trades` array drives both `TradeTable` and trade markers on `PriceChart`
- `equity_curve` array drives `EquityChart`

### OHLCV data for PriceChart
Add one more backend endpoint to serve candlestick data for the chart background:

#### `GET /ohlcv?timeframe=H1&years=2025,2026`
Returns OHLCV bars as a JSON array. Used exclusively by `PriceChart`.
```json
[
  { "time": "2025-01-15T08:00:00Z", "open": 2630.1, "high": 2658.3, "low": 2628.7, "close": 2656.8 }
]
```
Read from existing Parquet files via Polars. Filter to the date range of the result's trades to keep payload small.

---

## Implementation Order

1. **Backend foundation** — FastAPI app, CORS, static file serving, existing strategy imports
2. **`GET /strategies` and `GET /data/available`** — metadata endpoints (no computation)
3. **`POST /backtest/run` + `GET /backtest/status/{id}`** — job runner with background tasks
4. **`GET /results` + `GET /results/{id}` + `DELETE /results/{id}`** — result management
5. **`GET /ohlcv`** — OHLCV data endpoint for price chart
6. **Frontend scaffold** — Vite + React + TypeScript + Tailwind setup
7. **BacktestForm + API client** — form submission and polling
8. **ResultCard + EquityChart** — summary metrics and equity curve
9. **PriceChart** — candlestick chart with trade entry/exit/SL/TP overlays
10. **TradeTable** — paginated trade log with cross-linking to PriceChart
11. **Results page** — list, sort, delete
12. **ComparePanel** — side-by-side comparison view

---

## Environment & Dependencies

### Backend (`backend/requirements.txt`)
```
fastapi
uvicorn[standard]
polars
pyarrow
pydantic
```

### Frontend
```
npm create vite@latest frontend -- --template react-ts
npm install tailwindcss recharts react-query axios
```

### Dev startup
```bash
# Backend
uvicorn backend.main:app --reload --port 8000

# Frontend
cd frontend && npm run dev   # runs on port 5173
```

### CORS
Backend allows `http://localhost:5173` during development.

---

## Notes & Constraints

- Do NOT modify `strategies/` or the core logic in `backtest.py` — only extend/import
- The `result/` directory is the source of truth for saved results — no database needed
- Job state (running/done/error) can be held in memory (dict) — no persistence needed for jobs
- Parquet data files are read-only — no upload functionality needed
- The app is single-user / local — no auth required
