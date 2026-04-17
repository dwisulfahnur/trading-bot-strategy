"""
Backtest runner for XAUUSD strategies.

Usage
-----
python backtest.py --strategy william_fractals --years 2025 2026 --timeframe H1
python backtest.py --strategy william_fractals --years 2025 2026 --timeframe H1 --fractal_period 9
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
    # Internal simulation state (not serialised)
    _initial_sl_dist: float = 0.0
    _be_active: bool = False


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_data(years: list[int], timeframe: str) -> pl.DataFrame:
    frames = []
    for year in sorted(years):
        path = DATA_DIR / timeframe / f"XAUUSD_{timeframe}_{year}.parquet"
        if not path.exists():
            print(f"[WARN] Missing: {path} — skipping year {year}")
            continue
        frames.append(pl.read_parquet(path).with_columns(pl.lit(year).alias("_year")))

    if not frames:
        sys.exit("[ERROR] No data files found. Run convert_to_parquet.py first.")

    return pl.concat(frames).sort("time")


def load_tick_data(years: list[int]) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """
    Load tick data for the given years.
    Returns (timestamps_ms, bids, asks) as int64/float64 numpy arrays, or None if no data.
    """
    frames = []
    for year in sorted(years):
        path = TICKS_DIR / f"XAUUSD_ticks_{year}.parquet"
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
) -> str | None:
    """
    Replay bid ticks for one bar to determine the exit event for an open position.
    Handles break-even SL migration mid-bar correctly (two-phase approach).
    Mutates position.sl and position._be_active if break-even activates this bar.
    Returns 'tp', 'sl', or None (position still open at end of bar).
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
            be_idx = int(be_idxs[0])
            # Phase 1: ticks before BE fires — use original SL
            result = _check_sl_tp(direction, bar_bids[:be_idx], position.sl, tp)
            if result is not None:
                return result
            # BE activates — migrate SL to entry price
            position.sl         = position.entry_price
            position._be_active = True
            # Phase 2: from BE tick onward — use new SL
            return _check_sl_tp(direction, bar_bids[be_idx:], position.entry_price, tp)

    # No BE transition this bar — single-phase check
    return _check_sl_tp(direction, bar_bids, position.sl, tp)


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def simulate(
    df: pl.DataFrame,
    tick_data: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None,
    breakeven_r: float | None = None,
    max_pending_bars: int | None = None,
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

    breakeven_r:      move SL to entry price when profit reaches this many R.
    max_pending_bars: cancel limit orders that have not filled after this many bars.

    One-trade-per-fractal rule: once a fractal level triggers an entry that same
    level cannot trigger another entry even if price retraces back through it.
    """
    rows   = df.to_dicts()
    n_bars = len(rows)
    trades:        list[Trade] = []
    position:      Trade | None = None
    pending:       dict  | None = None
    pending_bars:  int = 0
    last_top_used: float | None = None
    last_bot_used: float | None = None

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
        if pending is not None and position is None:
            limit_price = pending.get("entry_limit")

            if limit_price is not None:
                if has_ticks:
                    # Tick-based fill / cancel — resolve ordering precisely
                    if pending["signal"] == 1:
                        # BUY limit: MT5 fills when Ask ≤ limit_price
                        fill_idxs   = np.where(bar_asks <= limit_price)[0]
                        cancel_idxs = np.where(bar_bids >  pending["tp"])[0]
                    else:
                        # SELL limit: MT5 fills when Bid ≥ limit_price
                        fill_idxs   = np.where(bar_bids >= limit_price)[0]
                        cancel_idxs = np.where(bar_bids <  pending["tp"])[0]

                    first_fill   = int(fill_idxs[0])   if len(fill_idxs)   else None
                    first_cancel = int(cancel_idxs[0]) if len(cancel_idxs) else None

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
                        cancelled = bar["high"] >  pending["tp"]
                    else:
                        touched   = bar["high"] >= limit_price
                        cancelled = bar["low"]  <  pending["tp"]

                if touched:
                    entry_price = limit_price
                    sl_dist = abs(entry_price - pending["sl"])
                    if sl_dist > 0:
                        position = Trade(
                            direction=pending["signal"],
                            entry_time=bar["time"],
                            entry_price=entry_price,
                            sl=pending["sl"],
                            tp=pending["tp"],
                            year=bar.get("_year", 0),
                            _initial_sl_dist=sl_dist,
                        )
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
                    position = Trade(
                        direction=pending["signal"],
                        entry_time=bar["time"],
                        entry_price=entry_price,
                        sl=pending["sl"],
                        tp=pending["tp"],
                        year=bar.get("_year", 0),
                        _initial_sl_dist=sl_dist,
                    )
                    if pending["signal"] == 1:
                        last_top_used = pending.get("last_top")
                    else:
                        last_bot_used = pending.get("last_bot")
                pending = None
                pending_bars = 0

        # ----------------------------------------------------------------
        # Manage open position
        # ----------------------------------------------------------------
        if position is not None:
            if has_ticks:
                # Tick-level resolution: exact SL/TP ordering, correct BE sequencing
                exit_reason = _resolve_position_ticks(position, bar_bids, breakeven_r)
                hit_tp = exit_reason == "tp"
                hit_sl = exit_reason == "sl"
            else:
                # OHLCV fallback — legacy bar heuristics
                if breakeven_r is not None and not position._be_active:
                    if position.direction == 1:
                        be_trigger = position.entry_price + position._initial_sl_dist * breakeven_r
                        if bar["high"] >= be_trigger:
                            position.sl = position.entry_price
                            position._be_active = True
                    else:
                        be_trigger = position.entry_price - position._initial_sl_dist * breakeven_r
                        if bar["low"] <= be_trigger:
                            position.sl = position.entry_price
                            position._be_active = True

                if position.direction == 1:
                    hit_tp = bar["high"] >= position.tp
                    hit_sl = bar["low"]  <= position.sl
                else:
                    hit_tp = bar["low"]  <= position.tp
                    hit_sl = bar["high"] >= position.sl

                # Same-bar conflict heuristic (candle direction proxy)
                if hit_tp and hit_sl:
                    bullish_bar = bar["close"] >= bar["open"]
                    if position.direction == 1:
                        hit_tp = bullish_bar
                        hit_sl = not bullish_bar
                    else:
                        hit_tp = not bullish_bar
                        hit_sl = bullish_bar

            if hit_tp:
                position.exit_time   = bar["time"]
                position.exit_price  = position.tp
                position.exit_reason = "tp"
                position.pnl_r       = abs(position.tp - position.entry_price) / position._initial_sl_dist
                trades.append(position)
                position = None
            elif hit_sl:
                position.exit_time  = bar["time"]
                if position._be_active:
                    position.exit_price  = position.entry_price
                    position.exit_reason = "be"
                    position.pnl_r       = 0.0
                else:
                    position.exit_price  = position.sl
                    position.exit_reason = "sl"
                    position.pnl_r       = -1.0
                trades.append(position)
                position = None

        # ----------------------------------------------------------------
        # Queue next entry from this bar's signal (if flat)
        # ----------------------------------------------------------------
        if position is None and pending is None and not cancelled_this_bar and bar["signal"] != 0:
            frac_level = bar.get("last_top") if bar["signal"] == 1 else bar.get("last_bot")
            already_used = (
                (bar["signal"] == 1  and frac_level is not None and frac_level == last_top_used) or
                (bar["signal"] == -1 and frac_level is not None and frac_level == last_bot_used)
            )
            if not already_used:
                pending = bar
                pending_bars = 0

    # Close any open trade at the last bar's close
    if position is not None:
        last = rows[-1]
        position.exit_time   = last["time"]
        position.exit_price  = last["close"]
        position.exit_reason = "end_of_data"
        sl_dist = position._initial_sl_dist
        if sl_dist > 0:
            diff = (last["close"] - position.entry_price) * position.direction
            position.pnl_r = diff / sl_dist
        trades.append(position)

    return trades


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def compute_metrics(trades: list[Trade], initial_capital: float, risk_pct: float, compound: bool = True, commission_per_lot: float = 3.5) -> dict:
    if not trades:
        return {}

    # Equity curve + trade log
    capital      = initial_capital
    risk_amount  = initial_capital * risk_pct   # fixed for non-compound
    peak         = capital
    max_dd       = 0.0
    equity_curve = []
    trade_log    = []
    stopped_out  = False

    for i, t in enumerate(trades):
        risk_at_entry  = capital * risk_pct if compound else risk_amount
        lot_size       = round(risk_at_entry / t._initial_sl_dist / 100, 2) if t._initial_sl_dist > 0 else 0.0
        commission_usd = round(lot_size * commission_per_lot * 2, 2)  # round-trip (entry + exit)
        profit_usd     = round(risk_at_entry * t.pnl_r - commission_usd, 2)
        capital        = max(0.0, round(capital + profit_usd, 2))

        if capital > peak:
            peak = capital
        dd = (peak - capital) / peak * 100 if peak > 0 else 100.0
        if dd > max_dd:
            max_dd = dd

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
            "return_pct":   round(sum(t.pnl_r for t in yt) * risk_pct * 100, 4),
        }

    return {
        "total_trades":       len(executed),
        "win_rate_pct":       win_rate,
        "profit_factor":      profit_factor,
        "total_return_pct":   total_return,
        "initial_capital":    initial_capital,
        "final_capital":      capital,
        "max_drawdown_pct":   round(max_dd, 4),
        "risk_pct":           risk_pct,
        "commission_per_lot": commission_per_lot,
        "avg_win_r":          avg_win,
        "avg_loss_r":         avg_loss,
        "max_consec_wins":    max_consec_wins,
        "max_consec_losses":  max_consec_losses,
        "stopped_out":        stopped_out,
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
    print(f"  Max drawdown      : -{metrics['max_drawdown_pct']:.1f}%")
    compounding_label = "compounding" if metrics.get("compound", True) else "fixed (no compound)"
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
    filename  = f"{strategy_name}_{timeframe}_{year_tag}_{param_tag}.json"

    # OS filename limit is 255 bytes. If we'd exceed a safe threshold, truncate
    # param_tag and append an 8-char hash of the full param string for uniqueness.
    _MAX_FILENAME = 200
    if len(filename) > _MAX_FILENAME:
        _hash     = hashlib.md5(param_tag.encode()).hexdigest()[:8]
        _prefix   = f"{strategy_name}_{timeframe}_{year_tag}_"
        _reserved = len(_prefix) + 1 + len(_hash) + len(".json")  # +1 for "_" separator
        param_tag = param_tag[:_MAX_FILENAME - _reserved]
        filename  = f"{_prefix}{param_tag}_{_hash}.json"

    out_path  = RESULT_DIR / filename

    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "strategy":   strategy_name,
        "parameters": {
            **strategy_params,
            "timeframe":         timeframe,
            "years":             years,
            "initial_capital":   metrics.get("initial_capital"),
            "risk_pct":          metrics.get("risk_pct"),
            "compound":          metrics.get("compound", True),
            "commission_per_lot": metrics.get("commission_per_lot", 3.5),
            "breakeven_r":       metrics.get("breakeven_r"),
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
        description="Backtest a strategy on XAUUSD OHLCV data.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
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

    # William Fractals strategy params
    p.add_argument("--ema_period",     type=int,   default=200,
                   help="EMA period for trend filter")
    p.add_argument("--fractal_period", type=int,   default=9,
                   help="Fractal window size (must be odd: 5, 9, 11 …)")
    p.add_argument("--rr_ratio",       type=float, default=1.5,
                   help="Risk/reward ratio for take-profit")
    p.add_argument("--no_compound",    action="store_true",
                   help="Use fixed risk amount (non-compounding) instead of % of current capital")
    p.add_argument("--breakeven_r",    type=float, default=None,
                   help="Move SL to entry when profit reaches this R multiple (e.g. 1.0, 0.5, 1.3). Omit to disable.")
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
                   help="Cancel limit order after this many bars without fill (default: 5)")
    p.add_argument("--sessions",        type=str,   default="all",
                   help="Trading sessions to allow signals in. "
                        "Options: all, asia, london, newyork, "
                        "asia_london, london_newyork, asia_newyork, asia_london_newyork")

    return p.parse_args()


def main() -> None:
    args = parse_args()

    strategy_params = {
        "ema_period":     args.ema_period,
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
    }

    # Prepend the previous year as EMA warmup so that by the first bar of the
    # test period the indicator has converged to match MT5's fully-seeded value.
    # If the warmup year file is missing the loader prints a warning and skips it.
    warmup_year = min(args.years) - 1
    print(f"Loading data  : {args.timeframe} | years {args.years}  (+{warmup_year} EMA warmup)")
    df = load_data([warmup_year] + sorted(args.years), args.timeframe)
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
    tick_data = load_tick_data([warmup_year] + sorted(args.years))
    if tick_data is not None:
        print(f"  {len(tick_data[0]):,} ticks loaded")

    print("Simulating trades ...")
    breakeven_r = args.breakeven_r
    max_pending_bars = args.max_pending_bars if args.strategy == "momentum_candle" else None
    trades = simulate(df, tick_data=tick_data, breakeven_r=breakeven_r, max_pending_bars=max_pending_bars)
    print(f"  {len(trades)} trades executed")

    compound = not args.no_compound
    metrics = compute_metrics(trades, args.capital, args.risk, compound=compound, commission_per_lot=args.commission)
    metrics["compound"] = compound
    metrics["breakeven_r"] = breakeven_r
    print_report(metrics, strategy.name, args.timeframe, args.years)

    out_path = save_result(metrics, strategy.name, strategy_params, args.timeframe, args.years)
    print(f"Result saved  : {out_path.relative_to(Path(__file__).parent)}")


if __name__ == "__main__":
    main()
