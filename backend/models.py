from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

class BacktestRequest(BaseModel):
    strategy: str = "william_fractals"
    years: list[int] = Field(default=[2025, 2026], min_length=1)
    timeframe: str = "H1"
    symbol: str = "XAUUSD"
    initial_capital: float = Field(default=10_000, gt=0)
    risk_pct: float = Field(default=0.02, gt=0, le=1)
    risk_recovery: float = Field(default=0.0, ge=0, le=1)
    trail_recovery: bool = False
    trail_recovery_pct: float = Field(default=10.0, ge=1.0, le=100.0)
    compound: bool = False
    breakeven_r: float | None = None
    breakeven_sl_r: float = 0.0
    commission_per_lot: float = Field(default=3.5, ge=0)
    max_sl_per_period: int | None = None
    sl_period: str = "none"
    max_positions: int = Field(default=1, ge=1, le=10)
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def recovery_must_be_lower_than_risk(self) -> "BacktestRequest":
        if self.risk_recovery > 0 and self.risk_recovery >= self.risk_pct:
            raise ValueError(
                f"risk_recovery ({self.risk_recovery}) must be less than risk_pct ({self.risk_pct}) "
                "— recovery risk is meant to reduce exposure when underwater, not increase it."
            )
        if self.trail_recovery and self.risk_recovery == 0:
            raise ValueError(
                "trail_recovery requires risk_recovery > 0 — enable recovery risk first."
            )
        return self


# ---------------------------------------------------------------------------
# Strategy metadata
# ---------------------------------------------------------------------------

class ParameterSpec(BaseModel):
    name: str
    type: str           # "int" | "float" | "bool" | "str"
    default: Any
    min: float | None = None
    max: float | None = None
    step: float | None = None
    options: list[str] | None = None   # enum choices for type="str"


class StrategyMeta(BaseModel):
    name: str
    display_name: str
    parameters: list[ParameterSpec]


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------

class EquityPoint(BaseModel):
    trade: int
    capital: float
    direction: str
    exit_reason: str
    pnl_r: float
    exit_time: str


class TradeRecord(BaseModel):
    trade: int
    year: int
    direction: str
    entry_time: str
    entry_price: float
    sl: float
    tp: float
    exit_time: str
    exit_price: float
    exit_reason: str
    hold_period: float  # seconds
    pnl_r: float
    lot_size: float
    commission_usd: float
    profit_usd: float
    capital_after: float


class PerYearStats(BaseModel):
    total_trades: int
    win_rate_pct: float
    return_pct: float


class ResultSummary(BaseModel):
    id: str
    name: str | None = None
    created_at: str
    strategy: str
    symbol: str = "XAUUSD"
    timeframe: str
    years: list[int]
    total_return_pct: float
    win_rate_pct: float
    max_drawdown_pct: float
    max_drawdown_from_initial_pct: float
    profit_factor: float
    total_trades: int
    parameters: dict[str, Any] = Field(default_factory=dict)


class BacktestResult(BaseModel):
    id: str
    name: str | None = None
    created_at: str
    strategy: str
    parameters: dict[str, Any]
    results: dict[str, Any]


class SaveResultRequest(BaseModel):
    result_id: str
    name: str


# ---------------------------------------------------------------------------
# Job status
# ---------------------------------------------------------------------------

class JobStatus(BaseModel):
    job_id: str
    status: str          # "running" | "done" | "error"
    result_id: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Data availability
# ---------------------------------------------------------------------------

class DataAvailable(BaseModel):
    symbols: dict[str, dict]  # {symbol: {timeframes: [...], years: [...]}}
