"""
Backtest runner for OHLCV strategies.

Usage
-----
python backtest.py --strategy william_fractals --years 2025 2026 --timeframe H1
python backtest.py --strategy william_fractals --years 2025 2026 --timeframe H1 --fractal_period 9
python backtest.py --symbol XAUUSD --strategy william_fractals --years 2025 --timeframe H1
python backtest.py --help
"""

import argparse
import hashlib
import importlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import polars as pl

from strategies.base import BaseStrategy

DATA_DIR   = Path(__file__).parent / "data" / "parquet" / "ohlcv"
TICKS_DIR  = Path(__file__).parent / "data" / "parquet" / "ticks"
RESULT_DIR = Path(__file__).parent / "result"
VALID_TIMEFRAMES = ["M1", "M5", "M15", "H1", "H4"]

# Per-pair instrument specs.  Add new pairs here when data becomes available.
# contract_size : units per standard lot (affects lot size calculation)
# pip_mult      : multiplier to convert raw price diff → pip display value
#                 USD-quoted 4-decimal (EURUSD, GBPUSD): 10_000
#                 JPY-quoted 2-decimal (EURJPY, GBPJPY, CHFJPY, USDJPY, AUDJPY, CADJPY): 100
#                 GBP-quoted 4-decimal (EURGBP): 10_000
#                 XAUUSD (oz): 10
# NOTE: For non-USD-quoted pairs (JPY, GBP crosses), profit_usd is in the
#       quote currency, not USD — it is approximate. Future work: add a
#       quote_to_usd_rate conversion field.
PAIR_CONFIG: dict[str, dict] = {
    # Metals
    "XAUUSD": {"contract_size": 100,     "pip_mult": 10.0},
    # Crypto (USD-quoted, 1 lot = 1 BTC)
    "BTCUSD": {"contract_size": 1,       "pip_mult": 1.0},
    # Indices (USD-quoted, 1 lot = 1 contract, 1 point = $1)
    "USTEC":  {"contract_size": 1,       "pip_mult": 1.0},
    # USD-quoted forex
    "EURUSD": {"contract_size": 100_000, "pip_mult": 10_000.0},
    "GBPUSD": {"contract_size": 100_000, "pip_mult": 10_000.0},
    # JPY-quoted forex
    "EURJPY": {"contract_size": 100_000, "pip_mult": 100.0},
    "GBPJPY": {"contract_size": 100_000, "pip_mult": 100.0},
    "CHFJPY": {"contract_size": 100_000, "pip_mult": 100.0},
    "USDJPY": {"contract_size": 100_000, "pip_mult": 100.0},
    "AUDJPY": {"contract_size": 100_000, "pip_mult": 100.0},
    "CADJPY": {"contract_size": 100_000, "pip_mult": 100.0},
    # GBP-quoted forex
    "EURGBP": {"contract_size": 100_000, "pip_mult": 10_000.0},
}


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    direction: int          # 1 = long, -1 = short
    entry_time: object
    entry_price: float
    sl: float
    tp: float
    exit_time: object = None
    exit_price: float = 0.0
    exit_reason: str = ""   # "tp", "sl", "be", "end_of_data"
    pnl_r: float = 0.0      # P&L in R-multiples
    year: int = 0
    hold_period: float = 0.0  # seconds from entry to exit
    # Internal simulation state (not serialised)
    _initial_sl_dist: float = 0.0
    _be_active: bool = False


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_data(years: list[int], timeframe: str, symbol: str = "XAUUSD") -> pl.DataFrame:
    frames = []
    for year in sorted(years):
        path = DATA_DIR / timeframe / f"{symbol}_{timeframe}_{year}.parquet"
        if not path.exists():
            print(f"[WARN] Missing: {path} — skipping year {year}")
            continue
        frames.append(pl.read_parquet(path).with_columns(pl.lit(year).alias("_year")))

    if not frames:
        sys.exit("[ERROR] No data files found. Run convert_to_parquet.py first.")

    return pl.concat(frames).sort("time")


def load_tick_data(years: list[int], symbol: str = "XAUUSD") -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """
    Load tick data for the given years.
    Returns (timestamps_ms, bids, asks) as int64/float64 numpy arrays, or None if no data.
    """
    frames = []
    for year in sorted(years):
        path = TICKS_DIR / f"{symbol}_ticks_{year}.parquet"
        if not path.exists():
            print(f"[WARN] Missing tick data: {path} — year {year} will fall back to OHLCV resolution")
            continue
        frames.append(pl.read_parquet(path))

    if not frames:
        print("[WARN] No tick data found — simulation will use OHLCV bar heuristics.")
        return None

    ticks = pl.concat(frames).sort("timestamp")
    return (
        ticks["timestamp"].cast(pl.Int64).to_numpy(),
        ticks["bid"].to_numpy(),
        ticks["ask"].to_numpy(),
    )


# ---------------------------------------------------------------------------
# Strategy discovery
# ---------------------------------------------------------------------------

def load_strategy(name: str, params: dict) -> BaseStrategy:
    try:
        module = importlib.import_module(f"strategies.{name}")
    except ModuleNotFoundError:
        sys.exit(f"[ERROR] Strategy '{name}' not found in strategies/. "
                 f"Create strategies/{name}.py with a BaseStrategy subclass.")

    cls = next(
        (v for v in vars(module).values()
         if isinstance(v, type) and issubclass(v, BaseStrategy) and v is not BaseStrategy),
        None,
    )
    if cls is None:
        sys.exit(f"[ERROR] No BaseStrategy subclass found in strategies/{name}.py")

    # Pass only the params the constructor actually accepts
    import inspect
    sig = inspect.signature(cls.__init__)
    accepted = {k for k in sig.parameters if k != "self"}
    filtered = {k: v for k, v in params.items() if k in accepted}
    return cls(**filtered)


# ---------------------------------------------------------------------------
# Period SL-limit helper
# ---------------------------------------------------------------------------

def _get_period_key(t, sl_period: str) -> str | None:
    """Return a hashable key for the calendar period that `t` falls in."""
    if sl_period == "day":
        return f"{t.year}-{t.month:02d}-{t.day:02d}"
    if sl_period == "week":
        iso = t.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if sl_period == "month":
        return f"{t.year}-{t.month:02d}"
    return None


# ---------------------------------------------------------------------------
# Tick-resolution helpers
# ---------------------------------------------------------------------------

def _check_sl_tp(direction: int, bids: np.ndarray, sl: float, tp: float) -> str | None:
    """
    Check whether TP or SL is hit first in a bid-price tick slice.
    Returns 'tp', 'sl', or None (neither hit).
    """
    if len(bids) == 0:
        return None
    if direction == 1:          # long: tp = bid high, sl = bid low
        tp_idxs = np.where(bids >= tp)[0]
        sl_idxs = np.where(bids <= sl)[0]
    else:                       # short: tp = bid low, sl = bid high
        tp_idxs = np.where(bids <= tp)[0]
        sl_idxs = np.where(bids >= sl)[0]

    first_tp = int(tp_idxs[0]) if len(tp_idxs) else None
    first_sl = int(sl_idxs[0]) if len(sl_idxs) else None

    if first_tp is not None and first_sl is not None:
        return "tp" if first_tp <= first_sl else "sl"
    if first_tp is not None:
        return "tp"
    if first_sl is not None:
        return "sl"
    return None


def _resolve_position_ticks(
    position: "Trade",
    bar_bids: np.ndarray,
    breakeven_r: float | None,
    breakeven_sl_r: float = 0.0,
) -> str | None:
    """
    Replay bid ticks for one bar to determine the exit event for an open position.
    Handles break-even SL migration mid-bar correctly (two-phase approach).
    Mutates position.sl and position._be_active if break-even activates this bar.
    Returns 'tp', 'sl', or None (position still open at end of bar).

    breakeven_sl_r: R level the SL is moved to when BE fires (0.0 = entry, 0.5 = +0.5R).
    """
    direction = position.direction
    tp        = position.tp

    if not position._be_active and breakeven_r is not None:
        if direction == 1:
            be_trigger = position.entry_price + position._initial_sl_dist * breakeven_r
            be_idxs = np.where(bar_bids >= be_trigger)[0]
        else:
            be_trigger = position.entry_price - position._initial_sl_dist * breakeven_r
            be_idxs = np.where(bar_bids <= be_trigger)[0]

        if len(be_idxs):
            be_idx    = int(be_idxs[0])
            # New SL level: entry ± initial_sl_dist × breakeven_sl_r
            new_be_sl = position.entry_price + position._initial_sl_dist * breakeven_sl_r * direction
            # Phase 1: ticks before BE fires — use original SL
            result = _check_sl_tp(direction, bar_bids[:be_idx], position.sl, tp)
            if result is not None:
                return result
            # BE activates — migrate SL to locked level
            position.sl         = new_be_sl
            position._be_active = True
            # Phase 2: from BE tick onward — use new SL
            return _check_sl_tp(direction, bar_bids[be_idx:], new_be_sl, tp)

    # No BE transition this bar — single-phase check
    return _check_sl_tp(direction, bar_bids, position.sl, tp)


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def simulate(
    df: pl.DataFrame,
    tick_data: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None,
    breakeven_r: float | None = None,
    breakeven_sl_r: float = 0.0,
    max_pending_bars: int | None = None,
    max_sl_per_period: int | None = None,
    sl_period: str = "none",
    max_positions: int = 1,
) -> list[Trade]:
    """
    Bar-by-bar simulation with optional tick-level execution.

    When tick_data=(timestamps_ms, bids, asks) is supplied, the simulation uses
    real tick sequences for:
      - Market order entries (first ask/bid of the bar instead of open + avg_spread)
      - Limit order fill and cancel detection (actual ask/bid vs bar high/low)
      - SL/TP and break-even resolution (exact first-hit ordering, no heuristic)

    Without tick_data the engine falls back to OHLCV bar heuristics (candle
    direction to break same-bar SL/TP ties) — identical to the legacy behaviour.

    breakeven_r:      trigger R at which the SL is moved (e.g. 1.0 = move when up 1R).
    breakeven_sl_r:   R level the SL is moved to (0.0 = entry, 0.5 = lock in 0.5R profit).
    max_pending_bars: cancel limit orders that have not filled after this many bars.
    max_positions:    maximum number of trades that may be open simultaneously.

    One-trade-per-fractal rule: once a fractal level triggers an entry that same
    level cannot trigger another entry even if price retraces back through it.
    """
    rows   = df.to_dicts()
    n_bars = len(rows)
    trades:       list[Trade] = []
    positions:    list[Trade] = []
    pending:      dict | None = None
    pending_bars: int = 0
    tops_used:    set = set()
    bots_used:    set = set()

    # Period SL-limit state
    _sl_limit_active   = max_sl_per_period is not None and sl_period != "none"
    _period_sl_count:  int       = 0
    _current_period_key: str | None = None

    # Prepare tick arrays for O(log N) per-bar lookup
    _use_ticks = tick_data is not None
    if _use_ticks:
        _tick_ts, _tick_bid, _tick_ask = tick_data
        _bar_ts = df["time"].cast(pl.Int64).to_numpy()   # ms since epoch

    for i, bar in enumerate(rows):
        cancelled_this_bar = False

        # Resolve tick slice for this bar (all ticks in [bar_start, next_bar_start))
        if _use_ticks:
            bar_start = int(_bar_ts[i])
            bar_end   = int(_bar_ts[i + 1]) if i + 1 < n_bars else bar_start + 86_400_000
            lo = int(np.searchsorted(_tick_ts, bar_start, side="left"))
            hi = int(np.searchsorted(_tick_ts, bar_end,   side="left"))
            bar_bids = _tick_bid[lo:hi]
            bar_asks = _tick_ask[lo:hi]
            has_ticks = len(bar_bids) > 0
        else:
            bar_bids = bar_asks = None
            has_ticks = False

        # ----------------------------------------------------------------
        # Open a pending trade at this bar
        # ----------------------------------------------------------------
        if pending is not None and len(positions) < max_positions:
            # Block entry for the rest of the period if SL limit is reached
            if _sl_limit_active:
                pk = _get_period_key(bar["time"], sl_period)
                if pk != _current_period_key:
                    _current_period_key = pk
                    _period_sl_count = 0
                if _period_sl_count >= max_sl_per_period:
                    pending = None
                    pending_bars = 0
                    cancelled_this_bar = True

        if pending is not None and len(positions) < max_positions:
            stop_price  = pending.get("entry_stop")
            limit_price = pending.get("entry_limit")

            if stop_price is not None:
                # Stop order: long fills when high >= stop_price, short when low <= stop_price
                cancel_level = pending.get("cancel_level")
                if has_ticks:
                    if pending["signal"] == 1:
                        fill_idxs   = np.where(bar_asks >= stop_price)[0]
                        touched     = len(fill_idxs) > 0
                        entry_price = float(bar_asks[fill_idxs[0]]) if touched else None
                        # Cancel if price breaks below HL (setup invalidated)
                        cancelled   = (cancel_level is not None
                                       and len(np.where(bar_bids < cancel_level)[0]) > 0)
                    else:
                        fill_idxs   = np.where(bar_bids <= stop_price)[0]
                        touched     = len(fill_idxs) > 0
                        entry_price = float(bar_bids[fill_idxs[0]]) if touched else None
                        # Cancel if price breaks above LH (setup invalidated)
                        cancelled   = (cancel_level is not None
                                       and len(np.where(bar_asks > cancel_level)[0]) > 0)
                else:
                    spread = bar.get("avg_spread") or 0.0
                    if pending["signal"] == 1:
                        touched   = bar["high"] >= stop_price
                        cancelled = cancel_level is not None and bar["low"] < cancel_level
                        entry_price = stop_price + spread
                    else:
                        touched   = bar["low"] <= stop_price
                        cancelled = cancel_level is not None and bar["high"] > cancel_level
                        entry_price = stop_price

                if touched:
                    sl_dist = abs(entry_price - pending["sl"])
                    if sl_dist > 0:
                        positions.append(Trade(
                            direction=pending["signal"],
                            entry_time=bar["time"],
                            entry_price=entry_price,
                            sl=pending["sl"],
                            tp=pending["tp"],
                            year=bar.get("_year", 0),
                            _initial_sl_dist=sl_dist,
                        ))
                    pending = None
                    pending_bars = 0
                elif cancelled:
                    pending = None
                    pending_bars = 0
                    cancelled_this_bar = True
                else:
                    pending_bars += 1
                    if max_pending_bars is not None and pending_bars >= max_pending_bars:
                        pending = None
                        pending_bars = 0
                        cancelled_this_bar = True

            elif limit_price is not None:
                cancel_level = pending.get("cancel_level")

                if has_ticks:
                    # Tick-based fill / cancel — resolve ordering precisely
                    if pending["signal"] == 1:
                        # BUY limit: MT5 fills when Ask ≤ limit_price
                        fill_idxs = np.where(bar_asks <= limit_price)[0]
                        # Cancel: TP overshoot (price shoots up past TP without filling)
                        c_tp = np.where(bar_bids > pending["tp"])[0]
                        # Cancel: structure break (price drops below cancel_level / HL)
                        c_brk = (np.where(bar_bids < cancel_level)[0]
                                 if cancel_level is not None else np.empty(0, dtype=np.intp))
                    else:
                        # SELL limit: MT5 fills when Bid ≥ limit_price
                        fill_idxs = np.where(bar_bids >= limit_price)[0]
                        # Cancel: TP undershoot (price drops past TP without filling)
                        c_tp = np.where(bar_bids < pending["tp"])[0]
                        # Cancel: structure break (price rises above cancel_level / LH)
                        c_brk = (np.where(bar_asks > cancel_level)[0]
                                 if cancel_level is not None else np.empty(0, dtype=np.intp))

                    all_cancels  = np.concatenate([c_tp, c_brk])
                    first_fill   = int(fill_idxs[0])    if len(fill_idxs)   else None
                    first_cancel = int(all_cancels.min()) if len(all_cancels) else None

                    if first_fill is not None and first_cancel is not None:
                        touched   = first_fill <= first_cancel
                        cancelled = not touched
                    else:
                        touched   = first_fill   is not None
                        cancelled = first_cancel is not None
                else:
                    # OHLCV fallback
                    spread = bar.get("avg_spread") or 0.0
                    if pending["signal"] == 1:
                        touched   = bar["low"]  <= limit_price - spread
                        cancelled = (bar["high"] > pending["tp"]
                                     or (cancel_level is not None and bar["low"] < cancel_level))
                    else:
                        touched   = bar["high"] >= limit_price
                        cancelled = (bar["low"]  < pending["tp"]
                                     or (cancel_level is not None and bar["high"] > cancel_level))

                if touched:
                    entry_price = limit_price
                    sl_dist = abs(entry_price - pending["sl"])
                    if sl_dist > 0:
                        positions.append(Trade(
                            direction=pending["signal"],
                            entry_time=bar["time"],
                            entry_price=entry_price,
                            sl=pending["sl"],
                            tp=pending["tp"],
                            year=bar.get("_year", 0),
                            _initial_sl_dist=sl_dist,
                        ))
                    pending = None
                    pending_bars = 0
                elif cancelled:
                    pending = None
                    pending_bars = 0
                    cancelled_this_bar = True
                else:
                    pending_bars += 1
                    if max_pending_bars is not None and pending_bars >= max_pending_bars:
                        pending = None
                        pending_bars = 0
                        cancelled_this_bar = True

            else:
                # Market order: enter at the first tick of this bar (or bar open fallback)
                if has_ticks:
                    entry_price = (
                        float(bar_asks[0]) if pending["signal"] == 1 else float(bar_bids[0])
                    )
                else:
                    spread = bar.get("avg_spread") or 0.0
                    entry_price = bar["open"] + spread if pending["signal"] == 1 else bar["open"]

                sl_dist = abs(entry_price - pending["sl"])
                if sl_dist > 0:
                    positions.append(Trade(
                        direction=pending["signal"],
                        entry_time=bar["time"],
                        entry_price=entry_price,
                        sl=pending["sl"],
                        tp=pending["tp"],
                        year=bar.get("_year", 0),
                        _initial_sl_dist=sl_dist,
                    ))
                    if pending["signal"] == 1:
                        if pending.get("last_top") is not None:
                            tops_used.add(pending["last_top"])
                    else:
                        if pending.get("last_bot") is not None:
                            bots_used.add(pending["last_bot"])
                pending = None
                pending_bars = 0

        # ----------------------------------------------------------------
        # Manage open positions
        # ----------------------------------------------------------------
        for pos in list(positions):
            if has_ticks:
                # Tick-level resolution: exact SL/TP ordering, correct BE sequencing
                exit_reason = _resolve_position_ticks(pos, bar_bids, breakeven_r, breakeven_sl_r)
                hit_tp = exit_reason == "tp"
                hit_sl = exit_reason == "sl"
            else:
                # OHLCV fallback — legacy bar heuristics
                if breakeven_r is not None and not pos._be_active:
                    if pos.direction == 1:
                        be_trigger = pos.entry_price + pos._initial_sl_dist * breakeven_r
                        if bar["high"] >= be_trigger:
                            pos.sl = pos.entry_price + pos._initial_sl_dist * breakeven_sl_r
                            pos._be_active = True
                    else:
                        be_trigger = pos.entry_price - pos._initial_sl_dist * breakeven_r
                        if bar["low"] <= be_trigger:
                            pos.sl = pos.entry_price - pos._initial_sl_dist * breakeven_sl_r
                            pos._be_active = True

                if pos.direction == 1:
                    hit_tp = bar["high"] >= pos.tp
                    hit_sl = bar["low"]  <= pos.sl
                else:
                    hit_tp = bar["low"]  <= pos.tp
                    hit_sl = bar["high"] >= pos.sl

                # Same-bar conflict heuristic (candle direction proxy)
                if hit_tp and hit_sl:
                    bullish_bar = bar["close"] >= bar["open"]
                    if pos.direction == 1:
                        hit_tp = bullish_bar
                        hit_sl = not bullish_bar
                    else:
                        hit_tp = not bullish_bar
                        hit_sl = bullish_bar

            if hit_tp:
                pos.exit_time   = bar["time"]
                pos.exit_price  = pos.tp
                pos.exit_reason = "tp"
                pos.pnl_r       = abs(pos.tp - pos.entry_price) / pos._initial_sl_dist
                pos.hold_period = (pos.exit_time - pos.entry_time).total_seconds()
                trades.append(pos)
                positions.remove(pos)
            elif hit_sl:
                pos.exit_time  = bar["time"]
                if pos._be_active:
                    pos.exit_price  = pos.sl
                    pos.exit_reason = "be"
                    pos.pnl_r       = breakeven_sl_r
                else:
                    pos.exit_price  = pos.sl
                    pos.exit_reason = "sl"
                    pos.pnl_r       = -1.0
                    if _sl_limit_active:
                        pk = _get_period_key(bar["time"], sl_period)
                        if pk != _current_period_key:
                            _current_period_key = pk
                            _period_sl_count = 0
                        _period_sl_count += 1
                pos.hold_period = (pos.exit_time - pos.entry_time).total_seconds()
                trades.append(pos)
                positions.remove(pos)

        # ----------------------------------------------------------------
        # Queue next entry from this bar's signal (if below max_positions)
        # ----------------------------------------------------------------
        if len(positions) < max_positions and pending is None and not cancelled_this_bar and bar["signal"] != 0:
            frac_level = bar.get("last_top") if bar["signal"] == 1 else bar.get("last_bot")
            already_used = (
                (bar["signal"] == 1  and frac_level is not None and frac_level in tops_used) or
                (bar["signal"] == -1 and frac_level is not None and frac_level in bots_used)
            )
            if not already_used:
                pending = bar
                pending_bars = 0

    # Close any open trades at the last bar's close
    for pos in positions:
        last = rows[-1]
        pos.exit_time   = last["time"]
        pos.exit_price  = last["close"]
        pos.exit_reason = "end_of_data"
        sl_dist = pos._initial_sl_dist
        if sl_dist > 0:
            diff = (last["close"] - pos.entry_price) * pos.direction
            pos.pnl_r = diff / sl_dist
        pos.hold_period = (pos.exit_time - pos.entry_time).total_seconds()
        trades.append(pos)

    trades.sort(key=lambda t: t.exit_time)
    return trades


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def compute_metrics(trades: list[Trade], initial_capital: float, risk_pct: float, risk_recovery: float = 0.0, compound: bool = True, commission_per_lot: float = 3.5, symbol: str = "XAUUSD", trail_recovery: bool = False, trail_recovery_pct: float = 10.0) -> dict:
    if not trades:
        return {}

    # Equity curve + trade log
    capital      = initial_capital
    risk_amount  = initial_capital * risk_pct   # fixed for non-compound
    risk_amount_recovery = initial_capital * risk_recovery  # reduced when underwater
    peak         = capital
    max_dd       = 0.0
    max_dd_from_initial = 0.0
    equity_curve = []
    trade_log    = []
    stopped_out  = False
    recovery_baseline = initial_capital  # trails up with profit milestones when trail_recovery=True

    for i, t in enumerate(trades):
        if trail_recovery and risk_recovery > 0:
            next_milestone = recovery_baseline * (1 + trail_recovery_pct / 100)
            while capital >= next_milestone:
                recovery_baseline = next_milestone
                next_milestone = recovery_baseline * (1 + trail_recovery_pct / 100)
        applied_risk = risk_pct if capital >= recovery_baseline else (risk_recovery if risk_recovery > 0 else risk_pct)
        risk_at_entry  = capital * applied_risk if compound else (risk_amount_recovery if capital < recovery_baseline and risk_recovery > 0 else risk_amount)
        if symbol not in PAIR_CONFIG:
            raise ValueError(f"Symbol '{symbol}' not found in PAIR_CONFIG. Add it to backtest.py before running.")
        contract_size  = PAIR_CONFIG[symbol]["contract_size"]
        lot_size       = round(risk_at_entry / t._initial_sl_dist / contract_size, 2) if t._initial_sl_dist > 0 else 0.0
        commission_usd = round(lot_size * commission_per_lot * 2, 2)  # round-trip (entry + exit)
        profit_usd     = round(risk_at_entry * t.pnl_r - commission_usd, 2)
        capital        = max(0.0, round(capital + profit_usd, 2))

        if capital > peak:
            peak = capital
        dd = (peak - capital) / peak * 100 if peak > 0 else 100.0
        if dd > max_dd:
            max_dd = dd

        # max drawdown measured from initial capital
        dd_from_initial = (initial_capital - capital) / initial_capital * 100 if initial_capital > 0 else 0.0
        if dd_from_initial > max_dd_from_initial:
            max_dd_from_initial = dd_from_initial

        equity_curve.append({
            "trade":       i + 1,
            "capital":     capital,
            "direction":   "long" if t.direction == 1 else "short",
            "exit_reason": t.exit_reason,
            "pnl_r":       round(t.pnl_r, 4),
            "exit_time":   str(t.exit_time),
        })

        trade_log.append({
            "trade":          i + 1,
            "year":           t.year,
            "direction":      "long" if t.direction == 1 else "short",
            "entry_time":     str(t.entry_time),
            "entry_price":    round(t.entry_price, 3),
            "sl":             round(t.sl, 3),
            "tp":             round(t.tp, 3),
            "exit_time":      str(t.exit_time),
            "exit_price":     round(t.exit_price, 3),
            "exit_reason":    t.exit_reason,
            "hold_period":    round(t.hold_period, 1),
            "pnl_r":          round(t.pnl_r, 4),
            "lot_size":       lot_size,
            "commission_usd": commission_usd,
            "profit_usd":     profit_usd,
            "capital_after":  capital,
        })

        if capital <= 0:
            stopped_out = True
            break

    # Summary stats computed only over executed trades
    executed    = trades[:len(trade_log)]
    wins        = [t for t in executed if t.pnl_r > 0]
    losses      = [t for t in executed if t.pnl_r <= 0]
    win_rate    = round(len(wins) / len(executed) * 100, 2) if executed else 0.0
    gross_profit = sum(t.pnl_r for t in wins)
    gross_loss  = abs(sum(t.pnl_r for t in losses)) or 1e-9
    profit_factor = round(gross_profit / gross_loss, 4)
    avg_win     = round(sum(t.pnl_r for t in wins)   / len(wins),   4) if wins   else 0.0
    avg_loss    = round(sum(t.pnl_r for t in losses) / len(losses), 4) if losses else 0.0
    total_return = -100.0 if stopped_out else round((capital - initial_capital) / initial_capital * 100, 4)

    # Consecutive win/loss streaks
    max_consec_wins = max_consec_losses = 0
    cur_wins = cur_losses = 0
    for t in executed:
        if t.pnl_r > 0:
            cur_wins   += 1
            cur_losses  = 0
            if cur_wins > max_consec_wins:
                max_consec_wins = cur_wins
        else:
            cur_losses += 1
            cur_wins    = 0
            if cur_losses > max_consec_losses:
                max_consec_losses = cur_losses

    # Per-year (only executed trades)
    per_year = {}
    for yr in sorted({t.year for t in executed}):
        yt = [t for t in executed if t.year == yr]
        yw = [t for t in yt if t.pnl_r > 0]
        per_year[str(yr)] = {
            "total_trades": len(yt),
            "win_rate_pct": round(len(yw) / len(yt) * 100, 2) if yt else 0.0,
            "return_pct":   round(sum(
                tl["profit_usd"] for tl in trade_log if tl["year"] == yr
            ) / initial_capital * 100, 4),
        }

    pair_cfg = PAIR_CONFIG[symbol]
    return {
        "total_trades":       len(executed),
        "win_rate_pct":       win_rate,
        "profit_factor":      profit_factor,
        "total_return_pct":   total_return,
        "initial_capital":    initial_capital,
        "final_capital":      capital,
        "max_drawdown_pct":   round(max_dd, 4),
        "max_drawdown_from_initial_pct": round(max_dd_from_initial, 4),
        "risk_pct":           risk_pct,
        "risk_recovery_pct":  risk_recovery,
        "trail_recovery":     trail_recovery,
        "trail_recovery_pct": trail_recovery_pct,
        "commission_per_lot": commission_per_lot,
        "avg_win_r":          avg_win,
        "avg_loss_r":         avg_loss,
        "max_consec_wins":    max_consec_wins,
        "max_consec_losses":  max_consec_losses,
        "stopped_out":        stopped_out,
        "symbol":             symbol,
        "contract_size":      pair_cfg["contract_size"],
        "pip_mult":           pair_cfg["pip_mult"],
        "per_year":           per_year,
        "equity_curve":       equity_curve,
        "trades":             trade_log,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(metrics: dict, strategy_name: str, timeframe: str, years: list[int]) -> None:
    if not metrics:
        print("No trades executed.")
        return

    year_range = f"{min(years)}–{max(years)}" if len(years) > 1 else str(years[0])
    SEP = "─" * 62
    print(f"\nStrategy : {strategy_name}  |  Timeframe: {timeframe}  |  Years: {year_range}")
    print(SEP)
    print(f"  Total trades      : {metrics['total_trades']}")
    print(f"  Win rate          : {metrics['win_rate_pct']:.1f}%")
    print(f"  Profit factor     : {metrics['profit_factor']:.2f}")
    print(f"  Total return      : {metrics['total_return_pct']:+.1f}%  "
          f"(${metrics['initial_capital']:,.0f} → ${metrics['final_capital']:,.0f})")
    print(f"  Max drawdown      : -{metrics['max_drawdown_pct']:.1f}%  (peak-to-trough)")
    print(f"  Max DD from init  : -{metrics.get('max_drawdown_from_initial_pct', 0.0):.1f}%  (vs initial capital)")
    compounding_label = "compounding" if metrics.get("compound", True) else "fixed (no compound)"
    recovery_risk = metrics.get('risk_recovery_pct', 0.0)
    if recovery_risk > 0:
        print(f"  Risk per trade    : {metrics['risk_pct'] * 100:.1f}%  →  {recovery_risk * 100:.1f}% when underwater  [{compounding_label}]")
    else:
        print(f"  Risk per trade    : {metrics['risk_pct'] * 100:.1f}%  [{compounding_label}]")
    print(f"  Avg win / Avg loss: {metrics['avg_win_r']:.2f}R / {metrics['avg_loss_r']:.2f}R")
    print(SEP)

    per_year = metrics.get("per_year", {})
    if len(per_year) > 1:
        print("  Per-year breakdown:")
        for yr, yd in per_year.items():
            print(f"    {yr} : {yd['total_trades']:3d} trades | "
                  f"{yd['win_rate_pct']:.0f}% WR | {yd['return_pct']:+.1f}% return")
        print()


def save_result(
    metrics: dict,
    strategy_name: str,
    strategy_params: dict,
    timeframe: str,
    years: list[int],
    symbol: str = "XAUUSD",
) -> Path:
    RESULT_DIR.mkdir(exist_ok=True)

    year_tag = f"{min(years)}-{max(years)}" if len(years) > 1 else str(years[0])

    # Only include params relevant to the active sideways/momentum filters to
    # keep the filename short. Sub-params use a consistent prefix convention.
    active_filter = str(strategy_params.get("sideways_filter", "none"))
    filter_prefix_map = {
        "adx":        "adx_",
        "ema_slope":  "ema_slope_",
        "choppiness": "choppiness_",
        "alligator":  "alligator_",
        "stochrsi":   "stochrsi_",
    }
    active_prefix = filter_prefix_map.get(active_filter)
    mc_active = bool(strategy_params.get("momentum_candle_filter", False))
    included_params = {
        k: v for k, v in strategy_params.items()
        if (
            not any(k.startswith(pfx) for pfx in filter_prefix_map.values())
            or (active_prefix and k.startswith(active_prefix))
        )
        and not (k.startswith("mc_") and not mc_active)
    }
    param_tag = "_".join(f"{k}{v}" for k, v in included_params.items())
    filename  = f"{symbol}_{strategy_name}_{timeframe}_{year_tag}_{param_tag}.json"

    # OS filename limit is 255 bytes. If we'd exceed a safe threshold, truncate
    # param_tag and append an 8-char hash of the full param string for uniqueness.
    _MAX_FILENAME = 200
    if len(filename) > _MAX_FILENAME:
        _hash     = hashlib.md5(param_tag.encode()).hexdigest()[:8]
        _prefix   = f"{symbol}_{strategy_name}_{timeframe}_{year_tag}_"
        _reserved = len(_prefix) + 1 + len(_hash) + len(".json")  # +1 for "_" separator
        param_tag = param_tag[:_MAX_FILENAME - _reserved]
        filename  = f"{_prefix}{param_tag}_{_hash}.json"

    out_path  = RESULT_DIR / filename

    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "strategy":   strategy_name,
        "symbol":     symbol,
        "parameters": {
            **strategy_params,
            "timeframe":         timeframe,
            "years":             years,
            "symbol":            symbol,
            "initial_capital":    metrics.get("initial_capital"),
            "risk_pct":           metrics.get("risk_pct"),
            "risk_recovery":      metrics.get("risk_recovery_pct", 0.0),
            "trail_recovery":     metrics.get("trail_recovery", False),
            "trail_recovery_pct": metrics.get("trail_recovery_pct", 10.0),
            "compound":           metrics.get("compound", True),
            "commission_per_lot": metrics.get("commission_per_lot", 3.5),
            "breakeven_r":        metrics.get("breakeven_r"),
            "breakeven_sl_r":     metrics.get("breakeven_sl_r", 0.0),
        },
        "results": metrics,
    }

    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)

    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backtest a strategy on OHLCV data.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--symbol",         default="XAUUSD",
                   help=f"Trading pair symbol. Configured pairs: {list(PAIR_CONFIG.keys())}")
    p.add_argument("--strategy",       default="william_fractals",
                   help="Strategy module name inside strategies/")
    p.add_argument("--years",          nargs="+", type=int, default=[2025, 2026],
                   help="Years to backtest")
    p.add_argument("--timeframe",      default="H1", choices=VALID_TIMEFRAMES,
                   help="OHLCV timeframe")
    p.add_argument("--capital",        type=float, default=10_000,
                   help="Initial capital in USD")
    p.add_argument("--risk",           type=float, default=0.01,
                   help="Fraction of capital risked per trade (0.01 = 1%%)")
    p.add_argument("--risk_recovery", type=float, default=0.005,
                   help="Reduced risk %% applied when current capital is below initial capital (0.005 = 0.5%%)")
    p.add_argument("--trail_recovery", action="store_true",
                   help="Trail recovery baseline upward at each profit milestone instead of anchoring to initial capital")
    p.add_argument("--trail_recovery_pct", type=float, default=10.0,
                   help="Profit %% milestone for trailing recovery baseline (default 10). E.g. 10 = locks in every 10%% gain.")

    # William Fractals strategy params
    p.add_argument("--ema_period",     type=int,   default=200,
                   help="EMA period for trend filter")
    p.add_argument("--ema_timeframe",  type=str,   default="same",
                   help="Timeframe for EMA source data (same=use running TF, or M1/M5/M15/H1/H4)")
    p.add_argument("--fractal_period", type=int,   default=9,
                   help="Fractal window size (must be odd: 5, 9, 11 …)")
    p.add_argument("--rr_ratio",       type=float, default=1.5,
                   help="Risk/reward ratio for take-profit")
    p.add_argument("--no_compound",    action="store_true",
                   help="Use fixed risk amount (non-compounding) instead of % of current capital")
    p.add_argument("--breakeven_r",    type=float, default=None,
                   help="Trigger: move SL when profit reaches this R multiple (e.g. 1.0). Omit to disable.")
    p.add_argument("--breakeven_sl_r", type=float, default=0.0,
                   help="Lock SL at this R level when break-even fires (0.0 = entry, 0.5 = lock in 0.5R profit).")
    p.add_argument("--commission",     type=float, default=3.5,
                   help="Commission charged per lot round-trip in USD (default: 3.5)")

    # Momentum Candle strategy params
    p.add_argument("--body_ratio_min",  type=float, default=0.70,
                   help="Minimum body-to-range ratio for momentum candle (default: 0.70)")
    p.add_argument("--volume_factor",   type=float, default=1.5,
                   help="Volume spike multiplier over rolling average (default: 1.5)")
    p.add_argument("--volume_lookback", type=int,   default=23,
                   help="Lookback bars for average volume calculation (default: 23)")
    p.add_argument("--retracement_pct", type=float, default=0.50,
                   help="Retracement fraction for limit order entry (default: 0.50)")
    p.add_argument("--sl_mult",         type=float, default=1.0,
                   help="SL distance as multiple of MC range (default: 1.0 = full range)")
    p.add_argument("--tp_mult",         type=float, default=1.0,
                   help="TP distance as multiple of MC range (default: 1.0 = full range)")
    p.add_argument("--max_pending_bars", type=int,  default=5,
                   help="Cancel limit/stop order after this many bars without fill (default: 5)")
    p.add_argument("--pending_cancel",  type=str,  default="max_bars",
                   choices=["none", "max_bars", "hl_break", "both"],
                   help="N Structure stop-order cancellation mode (default: max_bars)")
    p.add_argument("--sessions",        type=str,   default="all",
                   help="Trading sessions to allow signals in. "
                        "Options: all, asia, london, newyork, "
                        "asia_london, london_newyork, asia_newyork, asia_london_newyork")
    p.add_argument("--max_positions",   type=int,   default=1,
                   help="Maximum number of trades open simultaneously (default: 1)")

    return p.parse_args()


def main() -> None:
    args = parse_args()

    strategy_params = {
        "ema_period":     args.ema_period,
        "ema_timeframe":  args.ema_timeframe,
        "fractal_period": args.fractal_period,
        "rr_ratio":       args.rr_ratio,
        # Momentum Candle params (ignored by other strategies via load_strategy filtering)
        "body_ratio_min":   args.body_ratio_min,
        "volume_factor":    args.volume_factor,
        "volume_lookback":  args.volume_lookback,
        "retracement_pct":  args.retracement_pct,
        "sl_mult":          args.sl_mult,
        "tp_mult":          args.tp_mult,
        "max_pending_bars": args.max_pending_bars,
        "sessions":         args.sessions,
        "symbol":           symbol,
    }

    # Prepend the previous year as EMA warmup so that by the first bar of the
    # test period the indicator has converged to match MT5's fully-seeded value.
    # If the warmup year file is missing the loader prints a warning and skips it.
    symbol = args.symbol
    warmup_year = min(args.years) - 1
    print(f"Symbol        : {symbol}")
    print(f"Loading data  : {args.timeframe} | years {args.years}  (+{warmup_year} EMA warmup)")
    df = load_data([warmup_year] + sorted(args.years), args.timeframe, symbol=symbol)
    print(f"  {len(df):,} bars loaded")

    print(f"Strategy      : {args.strategy}  params={strategy_params}")
    strategy = load_strategy(args.strategy, strategy_params)

    print("Generating signals ...")
    df = strategy.generate_signals(df)

    # Zero-out any signals that fall inside the warmup year — the warmup bars
    # exist solely to seed the EMA; no trades should start during that period.
    df = df.with_columns(
        pl.when(pl.col("_year").is_in(args.years))
        .then(pl.col("signal"))
        .otherwise(pl.lit(0).cast(pl.Int8))
        .alias("signal")
    )
    print(f"  {(df['signal'] != 0).sum()} signals found")

    print("Loading tick data ...")
    tick_data = load_tick_data([warmup_year] + sorted(args.years), symbol=symbol)
    if tick_data is not None:
        print(f"  {len(tick_data[0]):,} ticks loaded")

    print("Simulating trades ...")
    breakeven_r    = args.breakeven_r
    breakeven_sl_r = args.breakeven_sl_r if breakeven_r is not None else 0.0
    if args.strategy == "momentum_candle":
        max_pending_bars = args.max_pending_bars
    elif args.strategy == "n_structure":
        pending_cancel = args.pending_cancel
        max_pending_bars = (
            args.max_pending_bars if pending_cancel in ("max_bars", "both") else None
        )
    else:
        max_pending_bars = None
    trades = simulate(df, tick_data=tick_data, breakeven_r=breakeven_r, breakeven_sl_r=breakeven_sl_r, max_pending_bars=max_pending_bars, max_positions=args.max_positions)
    print(f"  {len(trades)} trades executed")

    compound = not args.no_compound
    metrics = compute_metrics(trades, args.capital, args.risk, risk_recovery=args.risk_recovery, compound=compound, commission_per_lot=args.commission, symbol=symbol, trail_recovery=args.trail_recovery, trail_recovery_pct=args.trail_recovery_pct)
    metrics["compound"]           = compound
    metrics["breakeven_r"]        = breakeven_r
    metrics["breakeven_sl_r"]     = breakeven_sl_r
    metrics["risk_recovery"]      = args.risk_recovery
    metrics["trail_recovery"]     = args.trail_recovery
    metrics["trail_recovery_pct"] = args.trail_recovery_pct
    print_report(metrics, strategy.name, args.timeframe, args.years)

    out_path = save_result(metrics, strategy.name, strategy_params, args.timeframe, args.years, symbol=symbol)
    print(f"Result saved  : {out_path.relative_to(Path(__file__).parent)}")


if __name__ == "__main__":
    main()
