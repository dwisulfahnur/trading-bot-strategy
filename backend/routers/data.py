"""
Data-related endpoints:
  GET /strategies
  GET /data/available
  GET /ohlcv
"""

import importlib
import inspect
from pathlib import Path

import polars as pl
from fastapi import APIRouter, HTTPException, Query

from backend.models import DataAvailable, StrategyMeta, ParameterSpec
from strategies.base import BaseStrategy

router = APIRouter()

STRATEGIES_DIR = Path(__file__).parent.parent.parent / "strategies"
DATA_DIR = Path(__file__).parent.parent.parent / "data" / "parquet" / "ohlcv"
VALID_TIMEFRAMES = ["M1", "M5", "M15", "H1", "H4"]

# ---------------------------------------------------------------------------
# Hard-coded parameter specs per strategy (extend as new strategies added)
# ---------------------------------------------------------------------------
STRATEGY_PARAMS: dict[str, list[dict]] = {
    "momentum_candle": [
        {"name": "ema_period",        "type": "int",   "default": 200,  "min": 10,  "max": 500},
        {"name": "body_ratio_min",    "type": "float", "default": 0.70, "min": 0.5, "max": 0.95, "step": 0.01},
        {"name": "volume_factor",     "type": "float", "default": 1.5,  "min": 1.0, "max": 5.0,  "step": 0.1},
        {"name": "volume_lookback",   "type": "int",   "default": 23,   "min": 5,   "max": 100},
        {"name": "retracement_pct",   "type": "float", "default": 0.50, "min": 0.1, "max": 0.9,  "step": 0.05},
        {"name": "sl_mult",           "type": "float", "default": 1.0,  "min": 0.5, "max": 3.0,  "step": 0.1},
        {"name": "tp_mult",           "type": "float", "default": 1.0,  "min": 0.5, "max": 3.0,  "step": 0.1},
        {"name": "max_pending_bars",  "type": "int",   "default": 5,    "min": 1,   "max": 20},
        {"name": "sessions",          "type": "str",   "default": "all",
         "options": ["all", "asia", "london", "newyork",
                     "asia_london", "london_newyork", "asia_newyork",
                     "asia_london_newyork"]},
        # Sideways / ranging filter
        {"name": "sideways_filter",   "type": "str",   "default": "none",
         "options": ["none", "adx", "ema_slope", "choppiness", "alligator", "stochrsi"]},
        {"name": "adx_period",        "type": "int",   "default": 14,    "min": 5,    "max": 50},
        {"name": "adx_threshold",     "type": "float", "default": 25.0,  "min": 10.0, "max": 50.0, "step": 1.0},
        {"name": "ema_slope_period",  "type": "int",   "default": 10,    "min": 2,    "max": 50},
        {"name": "ema_slope_min",     "type": "float", "default": 0.5,   "min": 0.1,  "max": 10.0, "step": 0.1},
        {"name": "choppiness_period", "type": "int",   "default": 14,    "min": 5,    "max": 50},
        {"name": "choppiness_max",    "type": "float", "default": 61.8,  "min": 50.0, "max": 80.0, "step": 0.1},
        # Alligator sub-params
        {"name": "alligator_jaw",     "type": "int",   "default": 13,    "min": 5,    "max": 50},
        {"name": "alligator_teeth",   "type": "int",   "default": 8,     "min": 3,    "max": 30},
        {"name": "alligator_lips",    "type": "int",   "default": 5,     "min": 2,    "max": 20},
        # StochRSI sub-params
        {"name": "stochrsi_rsi_period",   "type": "int",   "default": 14,   "min": 5,    "max": 50},
        {"name": "stochrsi_stoch_period", "type": "int",   "default": 14,   "min": 3,    "max": 50},
        {"name": "stochrsi_oversold",     "type": "float", "default": 20.0, "min": 5.0,  "max": 40.0, "step": 1.0},
        {"name": "stochrsi_overbought",   "type": "float", "default": 80.0, "min": 60.0, "max": 95.0, "step": 1.0},
    ],
    "william_fractals": [
        {"name": "ema_period",        "type": "int",   "default": 200,   "min": 10,   "max": 500},
        {"name": "ema_timeframe",     "type": "str",   "default": "same",
         "options": ["same", "M1", "M5", "M15", "H1", "H4", "D1"]},
        {"name": "fractal_n",         "type": "int",   "default": 9,     "min": 2,    "max": 20},
        {"name": "rr_ratio",          "type": "float", "default": 1.5,   "min": 0.5,  "max": 5.0,  "step": 0.1},
        # Market session filter
        {"name": "sessions",          "type": "str",   "default": "all",
         "options": ["all", "asia", "london", "newyork",
                     "asia_london", "london_newyork", "asia_newyork",
                     "asia_london_newyork"]},
        # Momentum candle filter
        {"name": "momentum_candle_filter", "type": "bool",  "default": False},
        {"name": "mc_body_ratio_min",      "type": "float", "default": 0.6,  "min": 0.3, "max": 0.95, "step": 0.05},
        {"name": "mc_volume_factor",       "type": "float", "default": 1.5,  "min": 1.0, "max": 5.0,  "step": 0.1},
        {"name": "mc_volume_lookback",     "type": "int",   "default": 20,   "min": 5,   "max": 100},
        # Sideways / ranging filter
        {"name": "sideways_filter",   "type": "str",   "default": "none",
         "options": ["none", "adx", "ema_slope", "choppiness", "alligator", "stochrsi"]},
        {"name": "adx_period",        "type": "int",   "default": 14,    "min": 5,    "max": 50},
        {"name": "adx_threshold",     "type": "float", "default": 25.0,  "min": 10.0, "max": 50.0, "step": 1.0},
        {"name": "ema_slope_period",  "type": "int",   "default": 10,    "min": 2,    "max": 50},
        {"name": "ema_slope_min",     "type": "float", "default": 0.5,   "min": 0.1,  "max": 10.0, "step": 0.1},
        {"name": "choppiness_period", "type": "int",   "default": 14,    "min": 5,    "max": 50},
        {"name": "choppiness_max",    "type": "float", "default": 61.8,  "min": 50.0, "max": 80.0, "step": 0.1},
        # Alligator sub-params
        {"name": "alligator_jaw",     "type": "int",   "default": 13,    "min": 5,    "max": 50},
        {"name": "alligator_teeth",   "type": "int",   "default": 8,     "min": 3,    "max": 30},
        {"name": "alligator_lips",    "type": "int",   "default": 5,     "min": 2,    "max": 20},
        # StochRSI sub-params
        {"name": "stochrsi_rsi_period",   "type": "int",   "default": 14,   "min": 5,    "max": 50},
        {"name": "stochrsi_stoch_period", "type": "int",   "default": 14,   "min": 3,    "max": 50},
        {"name": "stochrsi_oversold",     "type": "float", "default": 20.0, "min": 5.0,  "max": 40.0, "step": 1.0},
        {"name": "stochrsi_overbought",   "type": "float", "default": 80.0, "min": 60.0, "max": 95.0, "step": 1.0},
    ],
    "support_resistance": [
        {"name": "pivot_n",           "type": "int",   "default": 5,    "min": 2,   "max": 20},
        {"name": "zone_tolerance",    "type": "float", "default": 0.5,  "min": 0.0, "max": 10.0, "step": 0.1},
        {"name": "rr_ratio",          "type": "float", "default": 2.0,  "min": 0.5, "max": 5.0,  "step": 0.1},
        {"name": "ema_period",        "type": "int",   "default": 200,  "min": 10,  "max": 500},
        {"name": "use_ema_filter",    "type": "bool",  "default": True},
        {"name": "sessions",          "type": "str",   "default": "all",
         "options": ["all", "asia", "london", "newyork",
                     "asia_london", "london_newyork", "asia_newyork",
                     "asia_london_newyork"]},
        # Sideways / ranging filter
        {"name": "sideways_filter",   "type": "str",   "default": "none",
         "options": ["none", "adx", "ema_slope", "choppiness", "alligator", "stochrsi"]},
        {"name": "adx_period",        "type": "int",   "default": 14,    "min": 5,    "max": 50},
        {"name": "adx_threshold",     "type": "float", "default": 25.0,  "min": 10.0, "max": 50.0, "step": 1.0},
        {"name": "ema_slope_period",  "type": "int",   "default": 10,    "min": 2,    "max": 50},
        {"name": "ema_slope_min",     "type": "float", "default": 0.5,   "min": 0.1,  "max": 10.0, "step": 0.1},
        {"name": "choppiness_period", "type": "int",   "default": 14,    "min": 5,    "max": 50},
        {"name": "choppiness_max",    "type": "float", "default": 61.8,  "min": 50.0, "max": 80.0, "step": 0.1},
        {"name": "alligator_jaw",     "type": "int",   "default": 13,    "min": 5,    "max": 50},
        {"name": "alligator_teeth",   "type": "int",   "default": 8,     "min": 3,    "max": 30},
        {"name": "alligator_lips",    "type": "int",   "default": 5,     "min": 2,    "max": 20},
        {"name": "stochrsi_rsi_period",   "type": "int",   "default": 14,   "min": 5,    "max": 50},
        {"name": "stochrsi_stoch_period", "type": "int",   "default": 14,   "min": 3,    "max": 50},
        {"name": "stochrsi_oversold",     "type": "float", "default": 20.0, "min": 5.0,  "max": 40.0, "step": 1.0},
        {"name": "stochrsi_overbought",   "type": "float", "default": 80.0, "min": 60.0, "max": 95.0, "step": 1.0},
    ],
    "grid": [
        {"name": "center_period",    "type": "int",   "default": 50,   "min": 10,  "max": 300},
        {"name": "atr_period",       "type": "int",   "default": 14,   "min": 5,   "max": 100},
        {"name": "grid_step_mult",   "type": "float", "default": 0.5,  "min": 0.1, "max": 5.0,  "step": 0.1},
        {"name": "grid_levels",      "type": "int",   "default": 3,    "min": 1,   "max": 5},
        {"name": "rr_ratio",         "type": "float", "default": 1.0,  "min": 0.5, "max": 5.0,  "step": 0.1},
        {"name": "sessions",         "type": "str",   "default": "all",
         "options": ["all", "asia", "london", "newyork",
                     "asia_london", "london_newyork", "asia_newyork",
                     "asia_london_newyork"]},
    ],
    "order_block_smc": [
        # ── Structure & OB detection ───────────────────────────────────────
        {"name": "structure_period", "type": "int",   "default": 20,   "min": 5,   "max": 100},
        {"name": "ob_lookback",      "type": "int",   "default": 5,    "min": 1,   "max": 20},
        # ── Entry & Exit ───────────────────────────────────────────────────
        {"name": "rr_ratio",         "type": "float", "default": 2.0,  "min": 0.5, "max": 5.0, "step": 0.1},
        {"name": "sl_mode",          "type": "str",   "default": "ob_edge",
         "options": ["ob_edge", "ob_midpoint", "structure"]},
        # ── Optional filters ───────────────────────────────────────────────
        {"name": "require_fvg",  "type": "bool", "default": False},
        {"name": "require_ote",  "type": "bool", "default": False},
        {"name": "ote_fib_low",  "type": "float", "default": 0.618, "min": 0.382, "max": 0.786, "step": 0.001},
        {"name": "ote_fib_high", "type": "float", "default": 0.786, "min": 0.500, "max": 0.886, "step": 0.001},
        # ── Session filter ─────────────────────────────────────────────────
        {"name": "sessions", "type": "str", "default": "all",
         "options": ["all", "asia", "london", "newyork",
                     "asia_london", "london_newyork", "asia_newyork",
                     "asia_london_newyork"]},
    ],
}

DISPLAY_NAMES: dict[str, str] = {
    "william_fractals":      "William Fractal Breakout",
    "momentum_candle":       "Momentum Candle",
    "order_block_smc":       "Order Block (SMC)",
    "support_resistance":    "Support & Resistance Bounce",
    "grid":                  "Grid Trading",
}


def _discover_strategies() -> list[str]:
    """Return module names (file stems) of all BaseStrategy subclasses in strategies/."""
    names = []
    for path in sorted(STRATEGIES_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module_name = f"strategies.{path.stem}"
        try:
            mod = importlib.import_module(module_name)
        except Exception:
            continue
        has_strategy = any(
            isinstance(v, type) and issubclass(v, BaseStrategy) and v is not BaseStrategy
            for v in vars(mod).values()
        )
        if has_strategy:
            names.append(path.stem)
    return names


@router.get("/strategies", response_model=list[StrategyMeta])
def get_strategies() -> list[StrategyMeta]:
    result = []
    for name in _discover_strategies():
        params_raw = STRATEGY_PARAMS.get(name, [])
        params = [ParameterSpec(**p) for p in params_raw]
        result.append(StrategyMeta(
            name=name,
            display_name=DISPLAY_NAMES.get(name, name.replace("_", " ").title()),
            parameters=params,
        ))
    return result


@router.get("/data/available", response_model=DataAvailable)
def get_data_available() -> DataAvailable:
    # symbols: {symbol: {timeframes: [...], years: [...]}}
    symbols: dict[str, dict] = {}
    for tf in VALID_TIMEFRAMES:
        tf_dir = DATA_DIR / tf
        if not tf_dir.exists():
            continue
        for f in tf_dir.glob("*.parquet"):
            # filename: {SYMBOL}_{TF}_{YEAR}.parquet  e.g. XAUUSD_H1_2025.parquet
            parts = f.stem.split("_")
            if len(parts) < 3:
                continue
            try:
                year = int(parts[-1])
            except ValueError:
                continue
            # Symbol is everything before the last two segments (TF + year)
            sym = "_".join(parts[:-2])
            if not sym:
                continue
            if sym not in symbols:
                symbols[sym] = {"timeframes": set(), "years": set()}
            symbols[sym]["timeframes"].add(tf)
            symbols[sym]["years"].add(year)

    # Convert sets to sorted lists; preserve VALID_TIMEFRAMES order
    result = {
        sym: {
            "timeframes": [tf for tf in VALID_TIMEFRAMES if tf in data["timeframes"]],
            "years": sorted(data["years"]),
        }
        for sym, data in sorted(symbols.items())
    }
    return DataAvailable(symbols=result)


@router.get("/ohlcv")
def get_ohlcv(
    timeframe: str = Query(...),
    years: str = Query(..., description="Comma-separated years, e.g. 2025,2026"),
    symbol: str = Query("XAUUSD", description="Trading pair symbol, e.g. XAUUSD"),
    date_from: str | None = Query(None, description="ISO date filter start (inclusive)"),
    date_to: str | None = Query(None, description="ISO date filter end (inclusive)"),
) -> list[dict]:
    if timeframe not in VALID_TIMEFRAMES:
        raise HTTPException(400, f"Invalid timeframe. Choose from {VALID_TIMEFRAMES}")

    year_list = []
    for y in years.split(","):
        try:
            year_list.append(int(y.strip()))
        except ValueError:
            raise HTTPException(400, f"Invalid year: {y}")

    frames = []
    for year in sorted(year_list):
        path = DATA_DIR / timeframe / f"{symbol}_{timeframe}_{year}.parquet"
        if not path.exists():
            continue
        frames.append(pl.read_parquet(path))

    if not frames:
        raise HTTPException(404, "No OHLCV data found for requested years/timeframe")

    df = pl.concat(frames).sort("time")

    if date_from:
        dt_from = pl.lit(date_from).str.to_datetime(format="%Y-%m-%d", time_unit="ms").dt.replace_time_zone("UTC")
        df = df.filter(pl.col("time") >= dt_from)
    if date_to:
        dt_to = pl.lit(date_to).str.to_datetime(format="%Y-%m-%d", time_unit="ms").dt.replace_time_zone("UTC")
        df = df.filter(pl.col("time") <= dt_to)

    return [
        {
            "time": row["time"].isoformat() if hasattr(row["time"], "isoformat") else str(row["time"]),
            "open":  round(row["open"],  3),
            "high":  round(row["high"],  3),
            "low":   round(row["low"],   3),
            "close": round(row["close"], 3),
        }
        for row in df.select(["time", "open", "high", "low", "close"]).to_dicts()
    ]
