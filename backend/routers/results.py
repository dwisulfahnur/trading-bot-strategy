"""
Result management endpoints:
  GET    /results
  GET    /results/{id}
  DELETE /results/{id}
"""

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from backend.models import BacktestResult, ResultSummary

router = APIRouter(prefix="/results")

RESULT_DIR = Path(__file__).parent.parent.parent / "result"


def _load_result(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _to_summary(result_id: str, data: dict) -> ResultSummary:
    r = data.get("results", {})
    params = data.get("parameters", {})
    years = params.get("years", [])
    return ResultSummary(
        id=result_id,
        created_at=data.get("created_at", ""),
        strategy=data.get("strategy", ""),
        symbol=data.get("symbol", params.get("symbol", "XAUUSD")),
        timeframe=params.get("timeframe", ""),
        years=years,
        total_return_pct=r.get("total_return_pct", 0.0),
        win_rate_pct=r.get("win_rate_pct", 0.0),
        max_drawdown_pct=r.get("max_drawdown_pct", 0.0),
        profit_factor=r.get("profit_factor", 0.0),
        total_trades=r.get("total_trades", 0),
        parameters=params,
    )


@router.get("", response_model=list[ResultSummary])
def list_results() -> list[ResultSummary]:
    if not RESULT_DIR.exists():
        return []
    summaries = []
    for path in sorted(RESULT_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = _load_result(path)
            summaries.append(_to_summary(path.stem, data))
        except Exception:
            continue
    return summaries


@router.get("/{result_id}", response_model=BacktestResult)
def get_result(result_id: str) -> BacktestResult:
    path = RESULT_DIR / f"{result_id}.json"
    if not path.exists():
        raise HTTPException(404, f"Result '{result_id}' not found")
    data = _load_result(path)
    return BacktestResult(
        id=result_id,
        created_at=data.get("created_at", ""),
        strategy=data.get("strategy", ""),
        parameters=data.get("parameters", {}),
        results=data.get("results", {}),
    )


@router.delete("/{result_id}")
def delete_result(result_id: str) -> dict:
    path = RESULT_DIR / f"{result_id}.json"
    if not path.exists():
        raise HTTPException(404, f"Result '{result_id}' not found")
    path.unlink()
    return {"deleted": result_id}


@router.delete("")
def delete_results(ids: list[str]) -> dict:
    """Delete multiple results by ID. Silently skips missing files."""
    deleted = []
    for result_id in ids:
        path = RESULT_DIR / f"{result_id}.json"
        if path.exists():
            path.unlink()
            deleted.append(result_id)
    return {"deleted": deleted}
