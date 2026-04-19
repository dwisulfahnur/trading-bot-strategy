"""
Backtest execution endpoints:
  POST /backtest/run
  GET  /backtest/status/{job_id}
"""

import threading

from fastapi import APIRouter, BackgroundTasks, HTTPException

from backend.models import BacktestRequest, JobStatus
from backend.services.runner import create_job, get_job, run_backtest

router = APIRouter(prefix="/backtest")

VALID_TIMEFRAMES = ["M1", "M5", "M15", "H1", "H4"]


@router.post("/run", response_model=JobStatus)
def run(req: BacktestRequest, background_tasks: BackgroundTasks) -> JobStatus:
    if req.timeframe not in VALID_TIMEFRAMES:
        raise HTTPException(400, f"Invalid timeframe. Choose from {VALID_TIMEFRAMES}")
    if not req.symbol:
        raise HTTPException(400, "symbol is required")
    if not req.years:
        raise HTTPException(400, "At least one year must be selected")
    if req.breakeven_r is not None and req.breakeven_sl_r >= req.breakeven_r:
        raise HTTPException(
            400,
            f"breakeven_sl_r ({req.breakeven_sl_r}) must be less than breakeven_r ({req.breakeven_r}). "
            "You can't lock in more profit than the trigger level — the SL would overshoot the current price and exit immediately."
        )

    job_id = create_job()
    # Run in a real thread so it doesn't block the event loop
    thread = threading.Thread(
        target=run_backtest,
        args=(job_id, req.model_dump()),
        daemon=True,
    )
    thread.start()

    return JobStatus(job_id=job_id, status="running")


@router.get("/status/{job_id}", response_model=JobStatus)
def status(job_id: str) -> JobStatus:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, f"Job '{job_id}' not found")
    return JobStatus(
        job_id=job_id,
        status=job["status"],
        result_id=job.get("result_id"),
        error=job.get("error"),
    )
