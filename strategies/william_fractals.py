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

from pathlib import Path

import polars as pl

from strategies.base import BaseStrategy

_DATA_DIR = Path(__file__).parent.parent / "data" / "parquet" / "ohlcv"

_PIP_MULT: dict[str, float] = {
    "XAUUSD": 10.0,
    "XAGUSD": 100.0,
    "BTCUSD": 1.0,  "ETHUSD": 1.0,  "USTEC": 1.0,
    "EURUSD": 10_000.0, "GBPUSD": 10_000.0,
    "EURJPY": 100.0, "GBPJPY": 100.0, "USDJPY": 100.0,
    "AUDJPY": 100.0, "CADJPY": 100.0, "CHFJPY": 100.0,
    "EURGBP": 10_000.0,
}


class WilliamFractalsStrategy(BaseStrategy):
    name = "william_fractals"

    def __init__(
        self,
        ema_period: int = 200,
        ema_fast_period: int = 50,
        ema_timeframe: str = "same",
        ema_filter_mode: str = "single",
        symbol: str = "XAUUSD",
        fractal_n: int = 9,
        trade_direction: str = "both",
        # Long (buy) SL/TP
        long_sl_tp_mode: str = "rr",
        long_rr_ratio: float = 1.5,
        long_sl_pips: float = 200.0,
        long_tp_pips: float = 400.0,
        # Short (sell) SL/TP
        short_sl_tp_mode: str = "rr",
        short_rr_ratio: float = 1.5,
        short_sl_pips: float = 200.0,
        short_tp_pips: float = 400.0,
        # Market session filter
        sessions: str = "all",
        # Momentum candle filter
        momentum_candle_filter: bool = False,
        mc_body_ratio_min: float = 0.6,
        mc_volume_factor: float = 1.5,
        mc_volume_lookback: int = 20,
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
        self.ema_fast_period = ema_fast_period
        self.ema_timeframe = ema_timeframe
        self.ema_filter_mode = ema_filter_mode
        self.symbol = symbol
        self.pip_mult = _PIP_MULT.get(symbol, 10.0)
        self.fractal_n = fractal_n
        self.trade_direction = trade_direction
        self.long_sl_tp_mode = long_sl_tp_mode
        self.long_rr_ratio = long_rr_ratio
        self.long_sl_pips = long_sl_pips
        self.long_tp_pips = long_tp_pips
        self.short_sl_tp_mode = short_sl_tp_mode
        self.short_rr_ratio = short_rr_ratio
        self.short_sl_pips = short_sl_pips
        self.short_tp_pips = short_tp_pips
        self.sessions = sessions
        self.momentum_candle_filter = momentum_candle_filter
        self.mc_body_ratio_min = mc_body_ratio_min
        self.mc_volume_factor = mc_volume_factor
        self.mc_volume_lookback = mc_volume_lookback
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

        if self.ema_timeframe == "same":
            df = df.with_columns([
                self._ema(self.ema_period),
                pl.col("close").ewm_mean(span=self.ema_fast_period, adjust=False).alias("_ema_fast"),
            ])
        else:
            df = self._load_htf_ema(df)

        df = (
            df
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

        # Add _mc_ok_long / _mc_ok_short for the momentum candle filter
        df = self._add_momentum_candle_filter(df)

        df = (
            df
            .with_columns(self._signals())
            .drop(["_fractal_top", "_fractal_bot", "_trend_ok_long", "_trend_ok_short",
                   "_mc_ok_long", "_mc_ok_short", "_ema_fast"])
        )
        return self._apply_session_filter(df, self.sessions)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ema(period: int) -> pl.Expr:
        return pl.col("close").ewm_mean(span=period, adjust=False).alias("ema")

    def _load_htf_ema(self, df: pl.DataFrame) -> pl.DataFrame:
        """Load a higher-timeframe parquet, compute EMA there, join_asof back onto df."""
        years = sorted(df["_year"].unique().to_list())
        frames = []
        for year in years:
            path = _DATA_DIR / self.ema_timeframe / f"{self.symbol}_{self.ema_timeframe}_{year}.parquet"
            if path.exists():
                frames.append(pl.read_parquet(path, columns=["time", "close"]))

        if not frames:
            raise FileNotFoundError(
                f"No {self.ema_timeframe} data found for {self.symbol} years {years}. "
                f"Check {_DATA_DIR / self.ema_timeframe}/"
            )

        htf = (
            pl.concat(frames)
            .sort("time")
            .with_columns([
                pl.col("close").ewm_mean(span=self.ema_period,      adjust=False).alias("ema"),
                pl.col("close").ewm_mean(span=self.ema_fast_period, adjust=False).alias("_ema_fast"),
            ])
            .select(["time", "ema", "_ema_fast"])
        )

        # For each bar in df, take the EMA value from the most recent HTF bar at or before it
        return df.sort("time").join_asof(htf, on="time", strategy="backward")

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
        if self.ema_filter_mode == "dual":
            in_uptrend   = pl.col("_ema_fast") > pl.col("ema")
            in_downtrend = pl.col("_ema_fast") < pl.col("ema")
        elif self.ema_filter_mode == "single":
            in_uptrend   = pl.col("close") > pl.col("ema")
            in_downtrend = pl.col("close") < pl.col("ema")
        else:  # "none"
            in_uptrend   = pl.lit(True)
            in_downtrend = pl.lit(True)

        # Direction gate — block the disabled side entirely
        if self.trade_direction == "short_only":
            in_uptrend = pl.lit(False)
        elif self.trade_direction == "long_only":
            in_downtrend = pl.lit(False)

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
            in_uptrend & buy_breakout & pl.col("last_top").is_not_null()
            & pl.col("_trend_ok_long") & pl.col("_mc_ok_long")
        )
        sell_cond = (
            in_downtrend & sell_breakout & pl.col("last_bot").is_not_null()
            & pl.col("_trend_ok_short") & pl.col("_mc_ok_short")
        )

        signal = (
            pl.when(buy_cond).then(pl.lit(1))
            .when(sell_cond).then(pl.lit(-1))
            .otherwise(pl.lit(0))
            .cast(pl.Int8)
            .alias("signal")
        )

        # Long SL/TP
        if self.long_sl_tp_mode == "pips":
            sl_buy = pl.col("close") - self.long_sl_pips / self.pip_mult
            tp_buy = pl.col("close") + self.long_tp_pips / self.pip_mult
        else:  # rr
            sl_buy = pl.col("low")
            tp_buy = pl.col("close") + self.long_rr_ratio * (pl.col("close") - pl.col("low"))

        # Short SL/TP
        if self.short_sl_tp_mode == "pips":
            sl_sell = pl.col("close") + self.short_sl_pips / self.pip_mult
            tp_sell = pl.col("close") - self.short_tp_pips / self.pip_mult
        else:  # rr
            sl_sell = pl.col("high")
            tp_sell = pl.col("close") - self.short_rr_ratio * (pl.col("high") - pl.col("close"))

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

    def _add_momentum_candle_filter(self, df: pl.DataFrame) -> pl.DataFrame:
        """
        Momentum candle filter: only allow entry on bars where the signal candle
        qualifies as a momentum candle — a strongly directional bar with a volume spike.

        Long  allowed: close > open (bullish), body ratio >= mc_body_ratio_min,
                       ticks > mc_volume_factor × rolling avg of prior mc_volume_lookback bars
        Short allowed: close < open (bearish), same body/volume conditions
        """
        if not self.momentum_candle_filter:
            return df.with_columns([
                pl.lit(True).alias("_mc_ok_long"),
                pl.lit(True).alias("_mc_ok_short"),
            ])

        n = self.mc_volume_lookback
        avg_ticks = pl.col("ticks").shift(1).rolling_mean(window_size=n)

        body = (pl.col("close") - pl.col("open")).abs()
        candle_range = pl.col("high") - pl.col("low")
        body_ratio = body / (candle_range + 1e-10)

        is_strong_body = body_ratio >= self.mc_body_ratio_min
        has_volume_spike = pl.col("ticks") > self.mc_volume_factor * avg_ticks

        return df.with_columns([
            ((pl.col("close") > pl.col("open")) & is_strong_body & has_volume_spike).alias("_mc_ok_long"),
            ((pl.col("close") < pl.col("open")) & is_strong_body & has_volume_spike).alias("_mc_ok_short"),
        ])
