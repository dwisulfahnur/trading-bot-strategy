"""
Grid Trading Strategy

Places a dynamic grid of buy/sell zones centered on a moving average, with
grid spacing proportional to ATR.  Designed for ranging/oscillating markets.

Rules
-----
- Grid center  : EMA(center_period)
- Grid step    : ATR(atr_period) × grid_step_mult
- Grid levels  : k = 1 … grid_levels on each side of center

Buy signal
  Price crosses upward through the k-th lower grid line
  (center − k × step), i.e. price was below and is now at or above it.
  Entry (next bar open) is treated as a bounce from that support level.
  SL  = grid line − step  (one grid level below the entry level)
  TP  = entry + rr_ratio × step

Sell signal
  Price crosses downward through the k-th upper grid line
  (center + k × step), i.e. price was above and is now at or below it.
  SL  = grid line + step  (one grid level above the entry level)
  TP  = entry − rr_ratio × step

Level priority
  When multiple levels could trigger on the same bar, the innermost
  level (k = 1, closest to center) takes precedence.

Note: grid lines shift every bar as EMA and ATR update.
"""

import polars as pl

from strategies.base import BaseStrategy


class GridStrategy(BaseStrategy):
    name = "grid"

    def __init__(
        self,
        center_period: int = 50,
        atr_period: int = 14,
        grid_step_mult: float = 0.5,
        grid_levels: int = 3,
        rr_ratio: float = 1.0,
        sessions: str = "all",
    ) -> None:
        self.center_period = center_period
        self.atr_period = atr_period
        self.grid_step_mult = grid_step_mult
        self.grid_levels = grid_levels
        self.rr_ratio = rr_ratio
        self.sessions = sessions

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def generate_signals(self, df: pl.DataFrame) -> pl.DataFrame:
        df = self._compute_indicators(df)

        # Build conditions and SL/TP expressions for each level.
        # Process outermost level first so the innermost (k=1) wins
        # when nested into the when/then chain last.
        buy_conds: list[pl.Expr] = []
        sell_conds: list[pl.Expr] = []
        buy_sl_exprs: list[pl.Expr] = []
        buy_tp_exprs: list[pl.Expr] = []
        sell_sl_exprs: list[pl.Expr] = []
        sell_tp_exprs: list[pl.Expr] = []

        for k in range(1, self.grid_levels + 1):
            buy_line      = pl.col("_center") - k * pl.col("_step")
            buy_line_prev = pl.col("_center").shift(1) - k * pl.col("_step").shift(1)

            # Upward crossover: was below line, now at or above
            buy_cross = (
                (pl.col("close") >= buy_line) &
                (pl.col("close").shift(1) < buy_line_prev)
            )

            sell_line      = pl.col("_center") + k * pl.col("_step")
            sell_line_prev = pl.col("_center").shift(1) + k * pl.col("_step").shift(1)

            # Downward crossover: was above line, now at or below
            sell_cross = (
                (pl.col("close") <= sell_line) &
                (pl.col("close").shift(1) > sell_line_prev)
            )

            buy_conds.append(buy_cross)
            sell_conds.append(sell_cross)

            # SL: one grid step beyond the entry grid line
            buy_sl_exprs.append(buy_line - pl.col("_step"))
            sell_sl_exprs.append(sell_line + pl.col("_step"))

            # TP: rr_ratio grid steps in profit direction from close
            buy_tp_exprs.append(pl.col("close") + self.rr_ratio * pl.col("_step"))
            sell_tp_exprs.append(pl.col("close") - self.rr_ratio * pl.col("_step"))

        # Build nested when/then from outermost level inward.
        # Innermost level (index 0 = k=1) will be applied last and win.
        signal_expr: pl.Expr = pl.lit(0).cast(pl.Int8)
        sl_expr: pl.Expr = pl.lit(None, dtype=pl.Float64)
        tp_expr: pl.Expr = pl.lit(None, dtype=pl.Float64)

        for i in range(len(buy_conds) - 1, -1, -1):
            signal_expr = (
                pl.when(buy_conds[i]).then(pl.lit(1).cast(pl.Int8))
                .when(sell_conds[i]).then(pl.lit(-1).cast(pl.Int8))
                .otherwise(signal_expr)
            )
            sl_expr = (
                pl.when(buy_conds[i]).then(buy_sl_exprs[i])
                .when(sell_conds[i]).then(sell_sl_exprs[i])
                .otherwise(sl_expr)
            )
            tp_expr = (
                pl.when(buy_conds[i]).then(buy_tp_exprs[i])
                .when(sell_conds[i]).then(sell_tp_exprs[i])
                .otherwise(tp_expr)
            )

        df = df.with_columns([
            signal_expr.alias("signal"),
            sl_expr.alias("sl"),
            tp_expr.alias("tp"),
        ])

        df = df.drop(["_center", "_tr", "_atr", "_step"])

        return self._apply_session_filter(df, self.sessions)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _compute_indicators(self, df: pl.DataFrame) -> pl.DataFrame:
        # Grid center: EMA
        df = df.with_columns([
            pl.col("close")
            .ewm_mean(span=self.center_period, adjust=False)
            .alias("_center"),
        ])

        # True Range → ATR
        df = df.with_columns([
            pl.max_horizontal(
                pl.col("high") - pl.col("low"),
                (pl.col("high") - pl.col("close").shift(1)).abs(),
                (pl.col("low") - pl.col("close").shift(1)).abs(),
            ).alias("_tr"),
        ])
        df = df.with_columns([
            pl.col("_tr")
            .ewm_mean(span=self.atr_period, adjust=False)
            .alias("_atr"),
        ])

        # Grid step
        df = df.with_columns([
            (pl.col("_atr") * self.grid_step_mult).alias("_step"),
        ])

        return df
