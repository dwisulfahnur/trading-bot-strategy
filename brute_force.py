#!/usr/bin/env python3
"""
Brute-force parameter search for MomentumCandleStrategy on M15.

OHLCV + tick data are pre-loaded once; only signal-gen + simulation
runs for each combination (no repeated I/O).

Usage:
    source venv/bin/activate
    python brute_force.py

Results are printed (top 30) and saved to result/bruteforce_momentum_candle_M15.csv.
"""
import csv
import itertools
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import polars as pl

from backtest import load_data, load_tick_data, simulate, compute_metrics
from strategies.momentum_candle import MomentumCandleStrategy

# ── Configuration ─────────────────────────────────────────────────────────────
TIMEFRAME    = "M15"
YEARS        = [2024, 2025]           # test period
WARMUP_YEAR  = min(YEARS) - 1         # EMA warm-up (signals suppressed)
CAPITAL      = 10_000
RISK_PCT     = 0.01
COMPOUND     = True
COMMISSION   = 3.5                    # USD per lot round-trip
MIN_TRADES   = 50                     # discard combos with too few trades

# Parameter grid — add/remove values to widen/narrow the search
GRID = dict(
    ema_period       = [200],
    body_ratio_min   = [0.60, 0.65, 0.70, 0.75, 0.80, 0.85],
    volume_factor    = [1.2, 1.5, 2.0, 2.5, 3.0],
    volume_lookback  = [14, 23],
    retracement_pct  = [0.30, 0.40, 0.50, 0.60],
    sl_mult          = [1.0],
    tp_mult          = [1.0, 1.5, 2.0],
    max_pending_bars = [3, 5],
    sessions         = ["all", "london", "newyork", "london_newyork"],
)
# ──────────────────────────────────────────────────────────────────────────────


def run_combo(
    params: dict,
    df_base: pl.DataFrame,
    tick_data,
    years: list[int],
) -> dict | None:
    """Run one parameter combination. Returns metrics dict or None."""
    # max_pending_bars is a simulate-level param, not a strategy constructor param
    max_pb        = int(params["max_pending_bars"])
    strategy_params = {k: v for k, v in params.items() if k != "max_pending_bars"}

    strategy = MomentumCandleStrategy(**strategy_params)
    df = strategy.generate_signals(df_base)

    # Suppress signals from warmup year
    df = df.with_columns(
        pl.when(pl.col("_year").is_in(years))
        .then(pl.col("signal"))
        .otherwise(pl.lit(0).cast(pl.Int8))
        .alias("signal")
    )

    trades = simulate(df, tick_data=tick_data, max_pending_bars=max_pb)

    if len(trades) < MIN_TRADES:
        return None

    m = compute_metrics(
        trades, CAPITAL, RISK_PCT,
        compound=COMPOUND, commission_per_lot=COMMISSION,
    )
    if not m:
        return None

    return {
        **params,
        "trades":     m["total_trades"],
        "win_rate":   round(m["win_rate_pct"], 2),
        "pf":         round(m["profit_factor"], 4),
        "return_pct": round(m["total_return_pct"], 2),
        "max_dd":     round(m["max_drawdown_pct"], 2),
        "avg_win_r":  round(m["avg_win_r"], 4),
        "avg_loss_r": round(m["avg_loss_r"], 4),
    }


def main() -> None:
    all_years = [WARMUP_YEAR] + sorted(YEARS)

    print(f"Loading M15 OHLCV  {all_years} ...", flush=True)
    df_base = load_data(all_years, TIMEFRAME)
    print(f"  {len(df_base):,} bars loaded")

    print("Loading tick data ...", flush=True)
    tick_data = load_tick_data(all_years)
    if tick_data is not None:
        print(f"  {len(tick_data[0]):,} ticks loaded")
    else:
        print("  [WARN] No tick data — falling back to OHLCV heuristics")

    keys   = list(GRID.keys())
    combos = list(itertools.product(*GRID.values()))
    total  = len(combos)
    print(f"\nGrid: {total:,} combinations  |  min trades to qualify: {MIN_TRADES}\n")

    results: list[dict] = []
    t0 = time.time()

    for idx, vals in enumerate(combos, 1):
        params = dict(zip(keys, vals))
        r = run_combo(params, df_base, tick_data, YEARS)
        if r:
            results.append(r)

        if idx % 200 == 0 or idx == total:
            elapsed = time.time() - t0
            rate    = idx / elapsed
            eta     = (total - idx) / rate if rate else 0
            print(
                f"  {idx:>5}/{total}  {idx/total*100:>5.1f}%  "
                f"elapsed {elapsed:>6.0f}s  ETA ~{eta:>5.0f}s  "
                f"qualified: {len(results)}",
                flush=True,
            )

    elapsed = time.time() - t0
    results.sort(key=lambda r: r["pf"], reverse=True)

    # ── Print top 30 ──────────────────────────────────────────────────────────
    SEP = "─" * 115
    print(f"\n{SEP}")
    print(
        f"Done in {elapsed:.0f}s  |  "
        f"{len(results):,} combos qualified / {total:,} total"
    )
    print(SEP)

    if not results:
        print("No combinations met the minimum trade threshold.")
        return

    HDR = (
        f"{'#':<4} {'PF':>5} {'WR%':>6} {'Ret%':>8} {'DD%':>6} {'Trades':>7}  "
        f"{'body':>5} {'vfact':>6} {'vlook':>5} {'retr':>5} "
        f"{'sl':>4} {'tp':>4} {'pend':>4}  sessions"
    )
    print(f"\n{HDR}")
    print(SEP)
    for i, r in enumerate(results[:30], 1):
        print(
            f"{i:<4} {r['pf']:>5.2f} {r['win_rate']:>5.1f}% "
            f"{r['return_pct']:>+8.1f}% {r['max_dd']:>5.1f}%  {r['trades']:>6}  "
            f"{r['body_ratio_min']:>5.2f} {r['volume_factor']:>6.1f} {r['volume_lookback']:>5} "
            f"{r['retracement_pct']:>5.2f} {r['sl_mult']:>4.1f} {r['tp_mult']:>4.1f} "
            f"{r['max_pending_bars']:>4}  {r['sessions']}"
        )
    print(SEP)

    # ── Save CSV ──────────────────────────────────────────────────────────────
    out = ROOT / "result" / f"bruteforce_momentum_candle_{TIMEFRAME}.csv"
    out.parent.mkdir(exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)
    print(f"\nAll {len(results)} qualified results saved → {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
