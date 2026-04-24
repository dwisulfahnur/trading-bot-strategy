"""
Grid-search optimizer for the Range Breakout strategy.

Searches key parameters across all available pairs (H1, 2023-2025 data),
scores each run, and prints the top configurations per pair.

Usage:
    python optimize_range_breakout.py
    python optimize_range_breakout.py --timeframe H4 --years 2022 2023 2024 2025
    python optimize_range_breakout.py --symbol XAUUSD
"""

import argparse
import itertools
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data" / "parquet" / "ohlcv"


# ---------------------------------------------------------------------------
# Search grid
# ---------------------------------------------------------------------------

GRID = {
    "range_lookback":  [5, 10, 20, 30],
    "range_mode":      ["rolling", "fixed"],
    "atr_multiplier":  [0.0, 3.0, 6.0],
    "breakout_type":   ["close", "hl"],
    "allow_reentry":   [False],
    "sl_buffer":       [0.0],
    "rr_ratio":        [1.5, 2.0, 2.5, 3.0],
    "ema_period":      [0, 200],
}

MIN_TRADES = 30  # discard results with fewer trades


# ---------------------------------------------------------------------------
# Scoring — balance profit factor, trade count, and drawdown
# ---------------------------------------------------------------------------

def score(m: dict) -> float:
    if not m or m.get("total_trades", 0) < MIN_TRADES:
        return -1.0
    pf  = m["profit_factor"]
    dd  = m["max_drawdown_pct"]
    n   = m["total_trades"]
    # reward PF and trade count, penalise drawdown
    return pf * (n / 50) ** 0.25 * max(0, 1 - dd / 150)


# ---------------------------------------------------------------------------
# Worker — runs one param combo for one symbol/timeframe/years
# ---------------------------------------------------------------------------

def _run_one(args_tuple):
    symbol, timeframe, years, params = args_tuple
    # Imports inside worker so multiprocessing works cleanly
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent))
    from backtest import load_data, simulate, compute_metrics
    from strategies.range_breakout import RangeBreakoutStrategy

    try:
        s = RangeBreakoutStrategy(symbol=symbol, **params)
        df = load_data(years, timeframe, symbol)
        df_sig = s.generate_signals(df)
        trades = simulate(df_sig)
        m = compute_metrics(trades, 10_000, 0.01, symbol=symbol)
        return params, m
    except Exception as e:
        return params, {"_error": str(e)}


# ---------------------------------------------------------------------------
# Discover available symbols for a given timeframe
# ---------------------------------------------------------------------------

def available_symbols(timeframe: str) -> list[str]:
    tf_dir = DATA_DIR / timeframe
    if not tf_dir.exists():
        return []
    syms = set()
    for f in tf_dir.glob("*.parquet"):
        parts = f.stem.split("_")
        if len(parts) >= 3:
            syms.add("_".join(parts[:-2]))
    return sorted(syms)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeframe", default="H1")
    parser.add_argument("--years",     nargs="+", type=int, default=[2023, 2024, 2025])
    parser.add_argument("--symbol",    default=None, help="Single symbol to test (default: all available)")
    parser.add_argument("--top",       type=int, default=5, help="Top N configs to show per symbol")
    parser.add_argument("--workers",   type=int, default=6)
    args = parser.parse_args()

    symbols = [args.symbol] if args.symbol else available_symbols(args.timeframe)
    if not symbols:
        print(f"No data found for timeframe {args.timeframe}")
        sys.exit(1)

    # Build all (symbol, params) combos
    keys   = list(GRID.keys())
    values = list(GRID.values())
    combos = list(itertools.product(*values))
    param_dicts = [dict(zip(keys, c)) for c in combos]

    tasks = [
        (sym, args.timeframe, args.years, p)
        for sym in symbols
        for p in param_dicts
    ]

    total   = len(tasks)
    done    = 0
    results: dict[str, list] = {sym: [] for sym in symbols}

    print(f"Grid search: {len(param_dicts)} combos × {len(symbols)} symbols = {total} runs")
    print(f"Timeframe: {args.timeframe}  Years: {args.years}  Workers: {args.workers}\n")

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_run_one, t): t[0] for t in tasks}
        for fut in as_completed(futures):
            sym = futures[fut]
            done += 1
            params, m = fut.result()
            s = score(m)
            results[sym].append((s, params, m))
            if done % 100 == 0 or done == total:
                print(f"  {done}/{total} done...", end="\r", flush=True)

    print(f"\nDone. Summary:\n{'='*72}")

    best_per_symbol: dict[str, dict] = {}

    for sym in symbols:
        runs = sorted(results[sym], key=lambda x: x[0], reverse=True)
        valid = [(s, p, m) for s, p, m in runs if s > 0]
        print(f"\n{'─'*72}")
        print(f"  {sym}  ({args.timeframe}, {args.years})")
        print(f"{'─'*72}")
        if not valid:
            print("  No profitable configuration found.")
            continue

        top_score, top_params, top_m = valid[0]
        best_per_symbol[sym] = {**top_params}

        for rank, (s, p, m) in enumerate(valid[:args.top], 1):
            print(
                f"  #{rank:2d}  score={s:.3f}  pf={m['profit_factor']:.3f}  "
                f"ret={m['total_return_pct']:+.1f}%  "
                f"dd={m['max_drawdown_pct']:.1f}%  "
                f"wr={m['win_rate_pct']:.1f}%  "
                f"n={m['total_trades']}"
            )
            print(
                f"       lb={p['range_lookback']}  mode={p['range_mode']}  "
                f"atr_mult={p['atr_multiplier']}  type={p['breakout_type']}  "
                f"rr={p['rr_ratio']}  ema={p['ema_period']}"
            )

    # Summary table
    print(f"\n\n{'='*72}")
    print("BEST PARAMS PER SYMBOL")
    print(f"{'='*72}")
    print(f"  {'Symbol':<12} {'lb':>4} {'mode':<10} {'atr':>5} {'type':<8} {'rr':>5} {'ema':>5}  {'pf':>6}  {'ret':>7}  {'dd':>6}  {'n':>5}")
    print(f"  {'-'*68}")
    for sym in symbols:
        runs = sorted(results[sym], key=lambda x: x[0], reverse=True)
        valid = [(s, p, m) for s, p, m in runs if s > 0]
        if not valid:
            print(f"  {sym:<12}  — no profitable config")
            continue
        _, p, m = valid[0]
        print(
            f"  {sym:<12} {p['range_lookback']:>4} {p['range_mode']:<10} "
            f"{p['atr_multiplier']:>5.1f} {p['breakout_type']:<8} "
            f"{p['rr_ratio']:>5.1f} {p['ema_period']:>5}  "
            f"{m['profit_factor']:>6.3f}  "
            f"{m['total_return_pct']:>+7.1f}%  "
            f"{m['max_drawdown_pct']:>5.1f}%  "
            f"{m['total_trades']:>5}"
        )


if __name__ == "__main__":
    main()
