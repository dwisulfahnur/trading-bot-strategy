"""
FastAPI entry point for the Strategy Backtest Web Application.

Dev startup:
    uvicorn backend.main:app --reload --port 8000
"""

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from backend.routers import backtest, results, data, ea
from backend.routers import auth

app = FastAPI(title="Strategy Backtest API", version="1.0.0")

# ---------------------------------------------------------------------------
# CORS — allow the Vite dev server
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(auth.router)
app.include_router(backtest.router)
app.include_router(results.router)
app.include_router(data.router)
app.include_router(ea.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
