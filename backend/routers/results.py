"""
Result management endpoints:
  POST   /results/save
  GET    /results
  GET    /results/{id}
  DELETE /results/{id}
  DELETE /results         (bulk)
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import get_current_user
from backend.db import get_results
from backend.models import BacktestResult, ResultSummary, SaveResultRequest

RESULT_DIR = Path(__file__).parent.parent.parent / "result"

router = APIRouter(prefix="/results")

# Fields excluded from listing to keep payloads small
_LIST_PROJECTION = {"results.trades": 0, "results.equity_curve": 0}


@router.post("/save", response_model=BacktestResult)
def save_result(
    req: SaveResultRequest, user_id: str = Depends(get_current_user)
) -> BacktestResult:
    """Explicitly save a completed backtest result with a user-given name."""
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Name cannot be empty")

    path = RESULT_DIR / f"{req.result_id}.json"
    if not path.exists():
        raise HTTPException(404, f"Result '{req.result_id}' not found on disk")

    with open(path) as f:
        data = json.load(f)

    params = data.get("parameters", {})
    metrics = data.get("results", {})

    doc = {
        "_id": req.result_id,
        "user_id": user_id,
        "name": name,
        "strategy": data.get("strategy", ""),
        "created_at": data.get("created_at", datetime.now(timezone.utc).isoformat()),
        "symbol": data.get("symbol", params.get("symbol", "XAUUSD")),
        "parameters": params,
        "results": metrics,
    }
    col = get_results()
    existing = col.find_one({"_id": req.result_id})
    if existing and existing.get("user_id") != user_id:
        raise HTTPException(403, "Result belongs to another user")
    col.replace_one({"_id": req.result_id}, doc, upsert=True)

    return BacktestResult(
        id=req.result_id,
        name=name,
        created_at=doc["created_at"],
        strategy=doc["strategy"],
        parameters=params,
        results=metrics,
    )


def _doc_to_summary(doc: dict) -> ResultSummary:
    r = doc.get("results", {})
    params = doc.get("parameters", {})
    years = params.get("years", [])
    return ResultSummary(
        id=doc["_id"],
        name=doc.get("name"),
        created_at=doc.get("created_at", ""),
        strategy=doc.get("strategy", ""),
        symbol=doc.get("symbol", params.get("symbol", "XAUUSD")),
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
def list_results(user_id: str = Depends(get_current_user)) -> list[ResultSummary]:
    col = get_results()
    docs = col.find({"user_id": user_id}, _LIST_PROJECTION).sort("created_at", -1)
    summaries = []
    for doc in docs:
        try:
            summaries.append(_doc_to_summary(doc))
        except Exception:
            continue
    return summaries


@router.get("/{result_id}", response_model=BacktestResult)
def get_result(
    result_id: str, user_id: str = Depends(get_current_user)
) -> BacktestResult:
    col = get_results()
    doc = col.find_one({"_id": result_id, "user_id": user_id})
    if not doc:
        raise HTTPException(404, f"Result '{result_id}' not found")
    return BacktestResult(
        id=doc["_id"],
        name=doc.get("name"),
        created_at=doc.get("created_at", ""),
        strategy=doc.get("strategy", ""),
        parameters=doc.get("parameters", {}),
        results=doc.get("results", {}),
    )


@router.delete("/{result_id}")
def delete_result(
    result_id: str, user_id: str = Depends(get_current_user)
) -> dict:
    col = get_results()
    res = col.delete_one({"_id": result_id, "user_id": user_id})
    if res.deleted_count == 0:
        raise HTTPException(404, f"Result '{result_id}' not found")
    return {"deleted": result_id}


@router.delete("")
def delete_results(ids: list[str], user_id: str = Depends(get_current_user)) -> dict:
    """Bulk delete — silently skips IDs that don't belong to the user."""
    col = get_results()
    res = col.delete_many({"_id": {"$in": ids}, "user_id": user_id})
    return {"deleted": res.deleted_count}
