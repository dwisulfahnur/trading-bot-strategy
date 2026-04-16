"""
Backtest runner service — wraps existing backtest.py logic.
"""

import sys
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

# Ensure project root is importable
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import load_data, load_strategy, simulate, compute_metrics, save_result

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
        initial_capital = request_data["initial_capital"]
        risk_pct = request_data["risk_pct"]
        compound = request_data["compound"]
        breakeven_r = request_data.get("breakeven_r", None)
        params = request_data.get("params", {})

        df = load_data(years, timeframe)
        strategy = load_strategy(strategy_name, params)
        df = strategy.generate_signals(df)
        trades = simulate(df, breakeven_r=breakeven_r)

        metrics = compute_metrics(trades, initial_capital, risk_pct, compound=compound)
        metrics["compound"] = compound
        metrics["breakeven_r"] = breakeven_r

        out_path = save_result(metrics, strategy_name, params, timeframe, years)
        result_id = out_path.stem

        _jobs[job_id] = {"status": "done", "result_id": result_id, "error": None}

    except Exception as exc:
        _jobs[job_id] = {"status": "error", "result_id": None, "error": str(exc)}
