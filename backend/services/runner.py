"""
Backtest runner service — wraps existing backtest.py logic.
"""

import sys
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import polars as pl

# Ensure project root is importable
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import load_data, load_tick_data, load_strategy, simulate, compute_metrics, save_result

# In-memory job registry  {job_id: {"status": ..., "result_id": ..., "error": ...}}
_jobs: dict[str, dict[str, Any]] = {}


def create_job() -> str:
    job_id = uuid.uuid4().hex[:12]
    _jobs[job_id] = {"status": "running", "result_id": None, "error": None}
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    return _jobs.get(job_id)


def run_backtest(job_id: str, request_data: dict[str, Any]) -> None:
    """Executed in a background thread."""
    try:
        strategy_name = request_data["strategy"]
        years = request_data["years"]
        timeframe = request_data["timeframe"]
        symbol = request_data.get("symbol", "XAUUSD")
        initial_capital = request_data["initial_capital"]
        risk_pct = request_data["risk_pct"]
        risk_recovery = request_data.get("risk_recovery", 0.0)
        compound = request_data["compound"]
        breakeven_r    = request_data.get("breakeven_r", None)
        breakeven_sl_r = request_data.get("breakeven_sl_r", 0.0)
        commission_per_lot = request_data.get("commission_per_lot", 3.5)
        max_sl_per_period = request_data.get("max_sl_per_period", None)
        sl_period = request_data.get("sl_period", "none")
        max_positions = int(request_data.get("max_positions", 1))
        params = request_data.get("params", {})
        user_id = request_data.get("_user_id")

        # Prepend the previous year as EMA warmup so indicator values match
        # MT5's fully-seeded EMA by the first bar of the test period.
        warmup_year = min(years) - 1
        all_years   = [warmup_year] + sorted(years)

        df        = load_data(all_years, timeframe, symbol=symbol)
        tick_data = load_tick_data(all_years, symbol=symbol)
        strategy  = load_strategy(strategy_name, {**params, "symbol": symbol})
        df = strategy.generate_signals(df)

        # Suppress signals from the warmup year — used only for EMA convergence
        df = df.with_columns(
            pl.when(pl.col("_year").is_in(years))
            .then(pl.col("signal"))
            .otherwise(pl.lit(0).cast(pl.Int8))
            .alias("signal")
        )

        if strategy_name == "momentum_candle":
            max_pending_bars = int(params.get("max_pending_bars", 5))
        elif strategy_name == "n_structure":
            pending_cancel = params.get("pending_cancel", "max_bars")
            max_pending_bars = (
                int(params.get("max_pending_bars", 10))
                if pending_cancel in ("max_bars", "both") else None
            )
        else:
            max_pending_bars = None
        trades = simulate(
            df,
            tick_data=tick_data,
            breakeven_r=breakeven_r,
            breakeven_sl_r=breakeven_sl_r,
            max_pending_bars=max_pending_bars,
            max_sl_per_period=max_sl_per_period,
            sl_period=sl_period,
            max_positions=max_positions,
        )

        metrics = compute_metrics(trades, initial_capital, risk_pct, risk_recovery=risk_recovery, compound=compound, commission_per_lot=commission_per_lot, symbol=symbol)
        metrics["compound"]       = compound
        metrics["breakeven_r"]    = breakeven_r
        metrics["breakeven_sl_r"] = breakeven_sl_r

        # Include simulation-level params so EA generator can read them from parameters
        params_with_sim = {
            **params,
            "breakeven_r":    breakeven_r,
            "breakeven_sl_r": breakeven_sl_r,
            "max_sl_per_period": max_sl_per_period,
            "sl_period": sl_period,
            "max_positions": max_positions,
            "risk_recovery": risk_recovery,
        }
        out_path = save_result(metrics, strategy_name, params_with_sim, timeframe, years, symbol=symbol)
        result_id = out_path.stem

        _jobs[job_id] = {"status": "done", "result_id": result_id, "error": None}

    except Exception as exc:
        _jobs[job_id] = {"status": "error", "result_id": None, "error": str(exc)}
