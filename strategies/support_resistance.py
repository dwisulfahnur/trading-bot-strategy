"""
Support and Resistance Bounce Strategy.

Rules
-----
- Pivot highs form resistance zones; pivot lows form support zones.
- A pivot is confirmed after `pivot_n` candles on each side have printed.
- BUY  signal: candle low touches support (within zone_tolerance) AND close > support
               → price bounced off support.
- SELL signal: candle high touches resistance (within zone_tolerance) AND close < resistance
               → price rejected at resistance.
- Optional 200 EMA trend filter: buy only above EMA, sell only below EMA.
- Stop-loss  : low of signal candle (buy) / high of signal candle (sell).
- Take-profit: SL distance × rr_ratio.

Sideways Filter (optional)
--------------------------
Same set of filters as the other strategies — "none", "adx", "ema_slope",
"choppiness", "alligator", "stochrsi".
"""

import polars as pl

from strategies.base import BaseStrategy


class SupportResistanceStrategy(BaseStrategy):
    name = "support_resistance"

    def __init__(
        self,
        pivot_n: int = 5,
        zone_tolerance: float = 0.5,
        rr_ratio: float = 2.0,
        ema_period: int = 200,
        use_ema_filter: bool = True,
        sessions: str = "all",
        # Sideways filter
        sideways_filter: str = "none",
        adx_period: int = 14,
        adx_threshold: float = 25.0,
        ema_slope_period: int = 10,
        ema_slope_min: float = 0.5,
        choppiness_period: int = 14,
        choppiness_max: float = 61.8,
        alligator_jaw: int = 13,
        alligator_teeth: int = 8,
        alligator_lips: int = 5,
        stochrsi_rsi_period: int = 14,
        stochrsi_stoch_period: int = 14,
        stochrsi_oversold: float = 20.0,
        stochrsi_overbought: float = 80.0,
    ) -> None:
        self.pivot_n = pivot_n
        self.zone_tolerance = zone_tolerance
        self.rr_ratio = rr_ratio
        self.ema_period = ema_period
        self.use_ema_filter = use_ema_filter
        self.sessions = sessions
        self.sideways_filter = sideways_filter
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.ema_slope_period = ema_slope_period
        self.ema_slope_min = ema_slope_min
        self.choppiness_period = choppiness_period
        self.choppiness_max = choppiness_max
        self.alligator_jaw = alligator_jaw
        self.alligator_teeth = alligator_teeth
        self.alligator_lips = alligator_lips
        self.stochrsi_rsi_period = stochrsi_rsi_period
        self.stochrsi_stoch_period = stochrsi_stoch_period
        self.stochrsi_oversold = stochrsi_oversold
        self.stochrsi_overbought = stochrsi_overbought

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def generate_signals(self, df: pl.DataFrame) -> pl.DataFrame:
        n = self.pivot_n

        # EMA for trend filter and ema_slope sideways filter
        df = df.with_columns(
            pl.col("close").ewm_mean(span=self.ema_period, adjust=False).alias("ema")
        )

        # Detect pivot highs (resistance) and pivot lows (support), confirmed n bars later
        df = df.with_columns([
            self._pivot_high(n).alias("_pivot_high"),
            self._pivot_low(n).alias("_pivot_low"),
        ])

        # Forward-fill so every bar has the most recent known level
        df = df.with_columns([
            pl.col("_pivot_high").forward_fill().alias("last_resistance"),
            pl.col("_pivot_low").forward_fill().alias("last_support"),
        ])

        # Add _trend_ok_long / _trend_ok_short columns
        df = self._add_sideways_filter(df)

        df = df.with_columns(self._signals())

        df = df.drop([
            "_pivot_high", "_pivot_low",
            "_trend_ok_long", "_trend_ok_short",
        ])

        return self._apply_session_filter(df, self.sessions)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pivot_high(n: int) -> pl.Expr:
        """Price of a confirmed pivot high (resistance), null otherwise."""
        mask = pl.lit(True)
        for j in range(1, n + 1):
            mask = mask & (pl.col("high") > pl.col("high").shift(j))
            mask = mask & (pl.col("high") > pl.col("high").shift(-j))
        confirmed = mask.shift(n)
        return pl.when(confirmed).then(pl.col("high").shift(n)).otherwise(None)

    @staticmethod
    def _pivot_low(n: int) -> pl.Expr:
        """Price of a confirmed pivot low (support), null otherwise."""
        mask = pl.lit(True)
        for j in range(1, n + 1):
            mask = mask & (pl.col("low") < pl.col("low").shift(j))
            mask = mask & (pl.col("low") < pl.col("low").shift(-j))
        confirmed = mask.shift(n)
        return pl.when(confirmed).then(pl.col("low").shift(n)).otherwise(None)

    def _signals(self) -> list[pl.Expr]:
        rr  = self.rr_ratio
        tol = self.zone_tolerance

        # EMA trend direction
        if self.use_ema_filter:
            in_uptrend   = pl.col("close") > pl.col("ema")
            in_downtrend = pl.col("close") < pl.col("ema")
        else:
            in_uptrend   = pl.lit(True)
            in_downtrend = pl.lit(True)

        # Bounce off support: low touches (or slightly penetrates) the level, close is above it
        touched_support  = pl.col("low")  <= pl.col("last_support")  + tol
        bounced_support  = pl.col("close") > pl.col("last_support")

        # Rejection at resistance: high touches (or slightly penetrates) the level, close below it
        touched_resist   = pl.col("high") >= pl.col("last_resistance") - tol
        rejected_resist  = pl.col("close") < pl.col("last_resistance")

        buy_cond = (
            in_uptrend
            & touched_support
            & bounced_support
            & pl.col("last_support").is_not_null()
            & pl.col("_trend_ok_long")
        )
        sell_cond = (
            in_downtrend
            & touched_resist
            & rejected_resist
            & pl.col("last_resistance").is_not_null()
            & pl.col("_trend_ok_short")
        )

        signal = (
            pl.when(buy_cond).then(pl.lit(1))
            .when(sell_cond).then(pl.lit(-1))
            .otherwise(pl.lit(0))
            .cast(pl.Int8)
            .alias("signal")
        )

        sl_buy  = pl.col("low")
        tp_buy  = pl.col("close") + rr * (pl.col("close") - pl.col("low"))

        sl_sell = pl.col("high")
        tp_sell = pl.col("close") - rr * (pl.col("high") - pl.col("close"))

        sl = (
            pl.when(buy_cond).then(sl_buy)
            .when(sell_cond).then(sl_sell)
            .otherwise(None)
            .alias("sl")
        )
        tp = (
            pl.when(buy_cond).then(tp_buy)
            .when(sell_cond).then(tp_sell)
            .otherwise(None)
            .alias("tp")
        )

        return [signal, sl, tp]
