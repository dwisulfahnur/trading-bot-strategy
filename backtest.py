"""
Backtest runner for XAUUSD strategies.

Usage
-----
python backtest.py --strategy william_fractals --years 2025 2026 --timeframe H1
python backtest.py --strategy william_fractals --years 2025 2026 --timeframe H1 --fractal_period 9
python backtest.py --help
"""

import argparse
import importlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from strategies.base import BaseStrategy

DATA_DIR   = Path(__file__).parent / "data" / "parquet" / "ohlcv"
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
# Simulation
# ---------------------------------------------------------------------------

def simulate(df: pl.DataFrame, breakeven_r: float | None = None) -> list[Trade]:
    """
    Bar-by-bar simulation. Enters at the OPEN of the bar after the signal bar
    to avoid lookahead bias. One position at a time.

    breakeven_r: if set, move SL to entry price when profit reaches this many R.
                 e.g. 1.0 = move at 1:1, 0.5 = move at half-R, 1.3 = move at 1.3R.
                 Subsequent SL hits exit as "be" with pnl_r = 0.

    One-trade-per-fractal rule: once a fractal level triggers an entry, that
    same level cannot trigger another entry even if price oscillates back through it.
    """
    rows = df.to_dicts()
    trades: list[Trade] = []
    position: Trade | None = None
    pending: dict | None = None   # signal bar waiting for next open
    last_top_used: float | None = None   # top fractal price used for last long entry
    last_bot_used: float | None = None   # bot fractal price used for last short entry

    for bar in rows:
        # --- Open a pending trade at this bar's open ---
        if pending is not None and position is None:
            spread = bar.get("avg_spread") or 0.0
            # Longs enter at ask (bid + spread); shorts enter at bid
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
                # Mark this fractal level as consumed so it cannot trigger again
                if pending["signal"] == 1:
                    last_top_used = pending.get("last_top")
                else:
                    last_bot_used = pending.get("last_bot")
            pending = None

        # --- Manage open position ---
        if position is not None:
            # Move SL to break-even once price reaches breakeven_r multiples of risk
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

            # Check SL / TP
            if position.direction == 1:   # long
                hit_tp = bar["high"] >= position.tp
                hit_sl = bar["low"]  <= position.sl
            else:                          # short
                hit_tp = bar["low"]  <= position.tp
                hit_sl = bar["high"] >= position.sl

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

        # --- Queue next entry from this bar's signal (if flat) ---
        if position is None and pending is None and bar["signal"] != 0:
            # One-trade-per-fractal: skip if this fractal level was already used
            frac_level = bar.get("last_top") if bar["signal"] == 1 else bar.get("last_bot")
            already_used = (
                (bar["signal"] == 1 and frac_level is not None and frac_level == last_top_used) or
                (bar["signal"] == -1 and frac_level is not None and frac_level == last_bot_used)
            )
            if not already_used:
                pending = bar

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

def compute_metrics(trades: list[Trade], initial_capital: float, risk_pct: float, compound: bool = True) -> dict:
    if not trades:
        return {}

    wins   = [t for t in trades if t.pnl_r > 0]
    losses = [t for t in trades if t.pnl_r <= 0]

    win_rate      = round(len(wins) / len(trades) * 100, 2)
    gross_profit  = sum(t.pnl_r for t in wins)
    gross_loss    = abs(sum(t.pnl_r for t in losses)) or 1e-9
    profit_factor = round(gross_profit / gross_loss, 4)
    avg_win       = round(sum(t.pnl_r for t in wins)   / len(wins),   4) if wins   else 0.0
    avg_loss      = round(sum(t.pnl_r for t in losses) / len(losses), 4) if losses else 0.0

    # Equity curve + trade log
    capital      = initial_capital
    risk_amount  = initial_capital * risk_pct   # fixed for non-compound
    peak         = capital
    max_dd       = 0.0
    equity_curve = []
    trade_log    = []

    for i, t in enumerate(trades):
        risk_at_entry = capital * risk_pct if compound else risk_amount
        capital += risk_at_entry * t.pnl_r
        if capital > peak:
            peak = capital
        dd = (peak - capital) / peak * 100
        if dd > max_dd:
            max_dd = dd

        # Lot size in standard lots (1 lot = 100 oz for XAUUSD)
        lot_size = round(risk_at_entry / t._initial_sl_dist / 100, 2) if t._initial_sl_dist > 0 else 0.0
        profit_usd = round(risk_at_entry * t.pnl_r, 2)

        equity_curve.append({
            "trade":       i + 1,
            "capital":     round(capital, 2),
            "direction":   "long" if t.direction == 1 else "short",
            "exit_reason": t.exit_reason,
            "pnl_r":       round(t.pnl_r, 4),
            "exit_time":   str(t.exit_time),
        })

        trade_log.append({
            "trade":        i + 1,
            "year":         t.year,
            "direction":    "long" if t.direction == 1 else "short",
            "entry_time":   str(t.entry_time),
            "entry_price":  round(t.entry_price, 3),
            "sl":           round(t.sl, 3),
            "tp":           round(t.tp, 3),
            "exit_time":    str(t.exit_time),
            "exit_price":   round(t.exit_price, 3),
            "exit_reason":  t.exit_reason,
            "pnl_r":        round(t.pnl_r, 4),
            "lot_size":     lot_size,
            "profit_usd":   profit_usd,
            "capital_after": round(capital, 2),
        })

    total_return = round((capital - initial_capital) / initial_capital * 100, 4)

    # Per-year
    per_year = {}
    for yr in sorted({t.year for t in trades}):
        yt = [t for t in trades if t.year == yr]
        yw = [t for t in yt if t.pnl_r > 0]
        per_year[str(yr)] = {
            "total_trades": len(yt),
            "win_rate_pct": round(len(yw) / len(yt) * 100, 2) if yt else 0.0,
            "return_pct":   round(sum(t.pnl_r for t in yt) * risk_pct * 100, 4),
        }

    return {
        "total_trades":     len(trades),
        "win_rate_pct":     win_rate,
        "profit_factor":    profit_factor,
        "total_return_pct": total_return,
        "initial_capital":  initial_capital,
        "final_capital":    round(capital, 2),
        "max_drawdown_pct": round(max_dd, 4),
        "risk_pct":         risk_pct,
        "avg_win_r":        avg_win,
        "avg_loss_r":       avg_loss,
        "per_year":         per_year,
        "equity_curve":     equity_curve,
        "trades":           trade_log,
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

    # Only include params relevant to the active sideways filter to keep the
    # filename short. Filter sub-params use a consistent prefix convention.
    active_filter = str(strategy_params.get("sideways_filter", "none"))
    filter_prefix_map = {
        "adx":        "adx_",
        "ema_slope":  "ema_slope_",
        "choppiness": "choppiness_",
        "alligator":  "alligator_",
        "stochrsi":   "stochrsi_",
    }
    active_prefix = filter_prefix_map.get(active_filter)
    included_params = {
        k: v for k, v in strategy_params.items()
        if not any(k.startswith(pfx) for pfx in filter_prefix_map.values())
        or (active_prefix and k.startswith(active_prefix))
    }
    param_tag = "_".join(f"{k}{v}" for k, v in included_params.items())
    filename  = f"{strategy_name}_{timeframe}_{year_tag}_{param_tag}.json"
    out_path  = RESULT_DIR / filename

    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "strategy":   strategy_name,
        "parameters": {
            **strategy_params,
            "timeframe":       timeframe,
            "years":           years,
            "initial_capital": metrics.get("initial_capital"),
            "risk_pct":        metrics.get("risk_pct"),
            "compound":        metrics.get("compound", True),
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

    return p.parse_args()


def main() -> None:
    args = parse_args()

    strategy_params = {
        "ema_period":     args.ema_period,
        "fractal_period": args.fractal_period,
        "rr_ratio":       args.rr_ratio,
    }

    print(f"Loading data  : {args.timeframe} | years {args.years}")
    df = load_data(args.years, args.timeframe)
    print(f"  {len(df):,} bars loaded")

    print(f"Strategy      : {args.strategy}  params={strategy_params}")
    strategy = load_strategy(args.strategy, strategy_params)

    print("Generating signals ...")
    df = strategy.generate_signals(df)
    print(f"  {(df['signal'] != 0).sum()} signals found")

    print("Simulating trades ...")
    breakeven_r = args.breakeven_r
    trades = simulate(df, breakeven_r=breakeven_r)
    print(f"  {len(trades)} trades executed")

    compound = not args.no_compound
    metrics = compute_metrics(trades, args.capital, args.risk, compound=compound)
    metrics["compound"] = compound
    metrics["breakeven_r"] = breakeven_r
    print_report(metrics, strategy.name, args.timeframe, args.years)

    out_path = save_result(metrics, strategy.name, strategy_params, args.timeframe, args.years)
    print(f"Result saved  : {out_path.relative_to(Path(__file__).parent)}")


if __name__ == "__main__":
    main()
