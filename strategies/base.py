"""
Abstract base class for all backtest strategies.

Each strategy receives an OHLCV Polars DataFrame and must return it
with three additional columns:
  - signal  : int  →  1 (buy), -1 (sell), 0 (no signal)
  - sl      : f64  →  stop-loss price for the signal bar
  - tp      : f64  →  take-profit price for the signal bar
"""

import math
from abc import ABC, abstractmethod

import polars as pl

# UTC hour sets for each named session
_SESSION_HOURS: dict[str, set[int]] = {
    "asia":    set(range(0, 9)),    # 00:00–08:59 UTC
    "london":  set(range(8, 17)),   # 08:00–16:59 UTC
    "newyork": set(range(13, 22)),  # 13:00–21:59 UTC
}


class BaseStrategy(ABC):
    name: str = "base"

    @abstractmethod
    def generate_signals(self, df: pl.DataFrame) -> pl.DataFrame:
        """
        Parameters
        ----------
        df : pl.DataFrame
            OHLCV data with columns [time, open, high, low, close, ticks].

        Returns
        -------
        pl.DataFrame
            Same dataframe with appended columns: signal, sl, tp.
        """
        ...

    @staticmethod
    def _apply_session_filter(df: pl.DataFrame, sessions: str) -> pl.DataFrame:
        """
        Zero out signal (and sl/tp/entry_limit) for bars that fall outside the
        selected trading session(s).  Pass sessions="all" to disable filtering.

        Session UTC hour ranges
        -----------------------
          asia    : 00:00–08:59
          london  : 08:00–16:59
          newyork : 13:00–21:59

        Combine sessions with underscores, e.g. "london_newyork".
        entry_limit is masked only when the column is present in the DataFrame.
        """
        if sessions == "all":
            return df

        active_hours: set[int] = set()
        for part in sessions.split("_"):
            active_hours |= _SESSION_HOURS.get(part, set())

        in_session = pl.col("time").dt.hour().is_in(sorted(active_hours))
        zero = pl.lit(0).cast(pl.Int8)

        exprs = [
            pl.when(in_session).then(pl.col("signal")).otherwise(zero).alias("signal"),
            pl.when(in_session).then(pl.col("sl")).otherwise(None).alias("sl"),
            pl.when(in_session).then(pl.col("tp")).otherwise(None).alias("tp"),
        ]
        if "entry_limit" in df.columns:
            exprs.append(
                pl.when(in_session).then(pl.col("entry_limit")).otherwise(None).alias("entry_limit")
            )

        return df.with_columns(exprs)

    # ------------------------------------------------------------------
    # Sideways filter machinery (shared by all strategies that opt in)
    #
    # Strategies that use this must set the following attributes in __init__:
    #   sideways_filter, adx_period, adx_threshold,
    #   ema_slope_period, ema_slope_min,
    #   choppiness_period, choppiness_max,
    #   alligator_jaw, alligator_teeth, alligator_lips,
    #   stochrsi_rsi_period, stochrsi_stoch_period,
    #   stochrsi_oversold, stochrsi_overbought
    #
    # Adds two boolean columns to df:
    #   _trend_ok_long  — True = long signal allowed
    #   _trend_ok_short — True = short signal allowed
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

        # Offsets match the TradingView display: jaw +8, teeth +5, lips +3
        df = df.with_columns([
            pl.col("close").ewm_mean(alpha=jaw_alpha,   adjust=False).shift(8).alias("_jaw"),
            pl.col("close").ewm_mean(alpha=teeth_alpha, adjust=False).shift(5).alias("_teeth"),
            pl.col("close").ewm_mean(alpha=lips_alpha,  adjust=False).shift(3).alias("_lips"),
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
