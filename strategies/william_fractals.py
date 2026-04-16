"""
William Fractal Breakout Strategy filtered by 200 EMA.

Rules
-----
- 200 EMA defines trend direction.
- Top fractal    : high[i] > high of fractal_n candles on each side → resistance level.
- Bottom fractal : low[i]  < low  of N candles on each side → support level.
- Fractals are confirmed N bars after they form (no lookahead bias).
- BUY  signal : price above 200 EMA + candle closes above last top fractal.
- SELL signal : price below 200 EMA + candle closes below last bottom fractal.
- Stop-loss   : low of signal candle (buy) / high of signal candle (sell).
- Take-profit : SL distance × rr_ratio.

Sideways Filter (optional)
--------------------------
sideways_filter = "none"        → no filter, all signals pass through
sideways_filter = "adx"         → skip signals when ADX < adx_threshold
sideways_filter = "ema_slope"   → skip signals when |EMA slope| < ema_slope_min
sideways_filter = "choppiness"  → skip signals when Choppiness Index >= choppiness_max
sideways_filter = "alligator"   → skip signals when Alligator lines are tangled
sideways_filter = "stochrsi"    → skip signals when StochRSI is not in the extreme zone
                                   (buys only when StochRSI < oversold,
                                    sells only when StochRSI > overbought)
"""

import math

import polars as pl

from strategies.base import BaseStrategy


class WilliamFractalsStrategy(BaseStrategy):
    name = "william_fractals"

    def __init__(
        self,
        ema_period: int = 200,
        fractal_n: int = 9,
        rr_ratio: float = 1.5,
        # Sideways filter
        sideways_filter: str = "none",
        # ADX params
        adx_period: int = 14,
        adx_threshold: float = 25.0,
        # EMA slope params
        ema_slope_period: int = 10,
        ema_slope_min: float = 0.5,
        # Choppiness params
        choppiness_period: int = 14,
        choppiness_max: float = 61.8,
        # Alligator params
        alligator_jaw: int = 13,
        alligator_teeth: int = 8,
        alligator_lips: int = 5,
        # StochRSI params
        stochrsi_rsi_period: int = 14,
        stochrsi_stoch_period: int = 14,
        stochrsi_oversold: float = 20.0,
        stochrsi_overbought: float = 80.0,
    ) -> None:
        self.ema_period = ema_period
        self.fractal_n = fractal_n
        self.rr_ratio = rr_ratio
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
        n = self.fractal_n

        df = (
            df
            .with_columns(self._ema(self.ema_period))
            .with_columns([
                self._fractal_top_price(n).alias("_fractal_top"),
                self._fractal_bot_price(n).alias("_fractal_bot"),
            ])
            .with_columns([
                pl.col("_fractal_top").forward_fill().alias("last_top"),
                pl.col("_fractal_bot").forward_fill().alias("last_bot"),
            ])
        )

        # Add _trend_ok_long / _trend_ok_short columns based on selected filter
        df = self._add_sideways_filter(df)

        df = (
            df
            .with_columns(self._signals())
            .drop(["_fractal_top", "_fractal_bot", "_trend_ok_long", "_trend_ok_short"])
        )
        return df

    # ------------------------------------------------------------------
    # Sideways filter dispatcher
    # Adds two boolean columns:
    #   _trend_ok_long  — True = long signal allowed
    #   _trend_ok_short — True = short signal allowed
    # Direction-agnostic filters set both columns to the same value.
    # Direction-aware filters (alligator, stochrsi) set them independently.
    # ------------------------------------------------------------------

    def _add_sideways_filter(self, df: pl.DataFrame) -> pl.DataFrame:
        f = self.sideways_filter

        if f == "adx":
            return self._filter_adx(df)
        elif f == "ema_slope":
            return self._filter_ema_slope(df)
        elif f == "choppiness":
            return self._filter_choppiness(df)
        elif f == "alligator":
            return self._filter_alligator(df)
        elif f == "stochrsi":
            return self._filter_stochrsi(df)
        else:
            # "none" or unrecognised → always allow
            return df.with_columns([
                pl.lit(True).alias("_trend_ok_long"),
                pl.lit(True).alias("_trend_ok_short"),
            ])

    def _filter_adx(self, df: pl.DataFrame) -> pl.DataFrame:
        """
        ADX (Average Directional Index) — Wilder's method.
        Market is trending when ADX >= adx_threshold.
        """
        alpha = 1.0 / self.adx_period

        df = df.with_columns([
            pl.max_horizontal(
                pl.col("high") - pl.col("low"),
                (pl.col("high") - pl.col("close").shift(1)).abs(),
                (pl.col("low")  - pl.col("close").shift(1)).abs(),
            ).alias("_tr"),
            pl.when(
                (pl.col("high") - pl.col("high").shift(1) > pl.col("low").shift(1) - pl.col("low")) &
                (pl.col("high") - pl.col("high").shift(1) > 0)
            ).then(pl.col("high") - pl.col("high").shift(1)).otherwise(0.0).alias("_plus_dm"),
            pl.when(
                (pl.col("low").shift(1) - pl.col("low") > pl.col("high") - pl.col("high").shift(1)) &
                (pl.col("low").shift(1) - pl.col("low") > 0)
            ).then(pl.col("low").shift(1) - pl.col("low")).otherwise(0.0).alias("_minus_dm"),
        ])

        df = df.with_columns([
            pl.col("_tr").ewm_mean(alpha=alpha, adjust=False).alias("_atr_s"),
            pl.col("_plus_dm").ewm_mean(alpha=alpha, adjust=False).alias("_plus_dm_s"),
            pl.col("_minus_dm").ewm_mean(alpha=alpha, adjust=False).alias("_minus_dm_s"),
        ])

        df = df.with_columns([
            (100 * pl.col("_plus_dm_s")  / (pl.col("_atr_s") + 1e-10)).alias("_plus_di"),
            (100 * pl.col("_minus_dm_s") / (pl.col("_atr_s") + 1e-10)).alias("_minus_di"),
        ])

        df = df.with_columns([
            (100 * (pl.col("_plus_di") - pl.col("_minus_di")).abs() /
             (pl.col("_plus_di") + pl.col("_minus_di") + 1e-10)).alias("_dx"),
        ])

        df = df.with_columns([
            pl.col("_dx").ewm_mean(alpha=alpha, adjust=False).alias("_adx"),
        ])

        is_trending = pl.col("_adx") >= self.adx_threshold
        df = df.with_columns([
            is_trending.alias("_trend_ok_long"),
            is_trending.alias("_trend_ok_short"),
        ])

        return df.drop(["_tr", "_plus_dm", "_minus_dm", "_atr_s",
                         "_plus_dm_s", "_minus_dm_s", "_plus_di", "_minus_di", "_dx", "_adx"])

    def _filter_ema_slope(self, df: pl.DataFrame) -> pl.DataFrame:
        """
        EMA Slope — measures how steeply the trend EMA is rising/falling.
        slope = (ema[i] - ema[i - period]) / period  (price per bar)
        Market is trending when |slope| >= ema_slope_min.
        """
        period = self.ema_slope_period
        slope = (pl.col("ema") - pl.col("ema").shift(period)) / period
        is_trending = slope.abs() >= self.ema_slope_min
        return df.with_columns([
            is_trending.alias("_trend_ok_long"),
            is_trending.alias("_trend_ok_short"),
        ])

    def _filter_choppiness(self, df: pl.DataFrame) -> pl.DataFrame:
        """
        Choppiness Index (CI).
        CI = 100 × log10(Σ ATR(1) over N) / (Highest High(N) - Lowest Low(N)) / log10(N)
        Range: 0–100.  CI < 38.2 = strongly trending,  CI > 61.8 = ranging.
        Market is trending when CI < choppiness_max.
        """
        period = self.choppiness_period
        log_n = math.log10(period)

        df = df.with_columns([
            pl.max_horizontal(
                pl.col("high") - pl.col("low"),
                (pl.col("high") - pl.col("close").shift(1)).abs(),
                (pl.col("low")  - pl.col("close").shift(1)).abs(),
            ).alias("_tr"),
        ])

        df = df.with_columns([
            pl.col("_tr").rolling_sum(window_size=period).alias("_tr_sum"),
            pl.col("high").rolling_max(window_size=period).alias("_hh"),
            pl.col("low").rolling_min(window_size=period).alias("_ll"),
        ])

        df = df.with_columns([
            (100.0 * (pl.col("_tr_sum") / (pl.col("_hh") - pl.col("_ll") + 1e-10)).log(10) / log_n
             ).alias("_ci"),
        ])

        is_trending = pl.col("_ci") < self.choppiness_max
        df = df.with_columns([
            is_trending.alias("_trend_ok_long"),
            is_trending.alias("_trend_ok_short"),
        ])

        return df.drop(["_tr", "_tr_sum", "_hh", "_ll", "_ci"])

    def _filter_alligator(self, df: pl.DataFrame) -> pl.DataFrame:
        """
        Williams Alligator — three SMAs (Smoothed Moving Averages).
          Jaw   : SMMA(alligator_jaw)   — slowest
          Teeth : SMMA(alligator_teeth) — medium
          Lips  : SMMA(alligator_lips)  — fastest

        Trending up   : lips > teeth > jaw  → long signals allowed
        Trending down : jaw  > teeth > lips → short signals allowed
        Sideways      : lines tangled/crossing → both blocked
        """
        jaw_alpha   = 1.0 / self.alligator_jaw
        teeth_alpha = 1.0 / self.alligator_teeth
        lips_alpha  = 1.0 / self.alligator_lips

        df = df.with_columns([
            pl.col("close").ewm_mean(alpha=jaw_alpha,   adjust=False).alias("_jaw"),
            pl.col("close").ewm_mean(alpha=teeth_alpha, adjust=False).alias("_teeth"),
            pl.col("close").ewm_mean(alpha=lips_alpha,  adjust=False).alias("_lips"),
        ])

        df = df.with_columns([
            # Uptrend alignment: lips > teeth > jaw
            ((pl.col("_lips") > pl.col("_teeth")) & (pl.col("_teeth") > pl.col("_jaw")))
            .alias("_trend_ok_long"),
            # Downtrend alignment: jaw > teeth > lips
            ((pl.col("_jaw") > pl.col("_teeth")) & (pl.col("_teeth") > pl.col("_lips")))
            .alias("_trend_ok_short"),
        ])

        return df.drop(["_jaw", "_teeth", "_lips"])

    def _filter_stochrsi(self, df: pl.DataFrame) -> pl.DataFrame:
        """
        Stochastic RSI — Stochastic oscillator applied to RSI values.
          StochRSI = 100 × (RSI - lowest RSI over N) / (highest RSI - lowest RSI over N)

        Long  signals allowed when StochRSI < stochrsi_oversold  (pullback oversold)
        Short signals allowed when StochRSI > stochrsi_overbought (pullback overbought)
        """
        rsi_alpha = 1.0 / self.stochrsi_rsi_period
        stoch_n   = self.stochrsi_stoch_period

        delta = pl.col("close") - pl.col("close").shift(1)
        gain  = pl.when(delta > 0).then(delta).otherwise(0.0)
        loss  = pl.when(delta < 0).then(-delta).otherwise(0.0)

        df = df.with_columns([
            gain.ewm_mean(alpha=rsi_alpha, adjust=False).alias("_avg_gain"),
            loss.ewm_mean(alpha=rsi_alpha, adjust=False).alias("_avg_loss"),
        ])

        df = df.with_columns([
            (100.0 - 100.0 / (1.0 + pl.col("_avg_gain") / (pl.col("_avg_loss") + 1e-10)))
            .alias("_rsi"),
        ])

        df = df.with_columns([
            pl.col("_rsi").rolling_min(window_size=stoch_n).alias("_rsi_low"),
            pl.col("_rsi").rolling_max(window_size=stoch_n).alias("_rsi_high"),
        ])

        df = df.with_columns([
            (100.0 * (pl.col("_rsi") - pl.col("_rsi_low")) /
             (pl.col("_rsi_high") - pl.col("_rsi_low") + 1e-10))
            .alias("_stochrsi"),
        ])

        df = df.with_columns([
            (pl.col("_stochrsi") < self.stochrsi_oversold).alias("_trend_ok_long"),
            (pl.col("_stochrsi") > self.stochrsi_overbought).alias("_trend_ok_short"),
        ])

        return df.drop(["_avg_gain", "_avg_loss", "_rsi", "_rsi_low", "_rsi_high", "_stochrsi"])

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ema(period: int) -> pl.Expr:
        return pl.col("close").ewm_mean(span=period, adjust=False).alias("ema")

    @staticmethod
    def _fractal_top_price(n: int) -> pl.Expr:
        """Price of confirmed top fractal, null otherwise."""
        mask = pl.lit(True)
        for j in range(1, n + 1):
            mask = mask & (pl.col("high") > pl.col("high").shift(j))
            mask = mask & (pl.col("high") > pl.col("high").shift(-j))
        confirmed = mask.shift(n)
        return pl.when(confirmed).then(pl.col("high").shift(n)).otherwise(None)

    @staticmethod
    def _fractal_bot_price(n: int) -> pl.Expr:
        """Price of confirmed bottom fractal, null otherwise."""
        mask = pl.lit(True)
        for j in range(1, n + 1):
            mask = mask & (pl.col("low") < pl.col("low").shift(j))
            mask = mask & (pl.col("low") < pl.col("low").shift(-j))
        confirmed = mask.shift(n)
        return pl.when(confirmed).then(pl.col("low").shift(n)).otherwise(None)

    def _signals(self) -> list[pl.Expr]:
        rr = self.rr_ratio

        in_uptrend   = pl.col("close") > pl.col("ema")
        in_downtrend = pl.col("close") < pl.col("ema")

        buy_breakout = (
            pl.col("close") > pl.col("last_top")
        ) & (
            pl.col("close").shift(1) <= pl.col("last_top").shift(1)
        )
        sell_breakout = (
            pl.col("close") < pl.col("last_bot")
        ) & (
            pl.col("close").shift(1) >= pl.col("last_bot").shift(1)
        )

        buy_cond  = (
            in_uptrend & buy_breakout & pl.col("last_top").is_not_null() & pl.col("_trend_ok_long")
        )
        sell_cond = (
            in_downtrend & sell_breakout & pl.col("last_bot").is_not_null() & pl.col("_trend_ok_short")
        )

        signal = (
            pl.when(buy_cond).then(pl.lit(1))
            .when(sell_cond).then(pl.lit(-1))
            .otherwise(pl.lit(0))
            .cast(pl.Int8)
            .alias("signal")
        )

        sl_buy = pl.col("low")
        tp_buy = pl.col("close") + rr * (pl.col("close") - pl.col("low"))

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
