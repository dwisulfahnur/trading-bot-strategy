"""
Momentum Candle Scalping Strategy.

Rules (from transcript)
-----------------------
A "momentum candle" has two defining characteristics:
  1. Large body  : |close - open| / (high - low) >= body_ratio_min
                   (typically 0.70–0.80 — price moved strongly in one direction)
  2. Volume spike: bar's tick count > rolling average × volume_factor
                   (unusual buying/selling pressure confirms the move)

Entry logic (limit order)
-------------------------
- Bullish MC : close > open + body large + volume spike + close above EMA → BUY limit signal
- Bearish MC : close < open + body large + volume spike + close below EMA → SELL limit signal
- Limit entry = retracement_pct × (high - low) into the candle from the extreme:
    BUY  limit : high − retracement_pct × (high − low)   — price must pull back to here
    SELL limit : low  + retracement_pct × (high − low)   — price must retrace up to here
- If the next bar never touches the limit price → order cancelled, wait for next MC.

Stop-loss / Take-profit
-----------------------
BUY:
  SL = low  of the momentum candle   — candle's lowest price
  TP = high of the momentum candle   — candle's highest price

SELL:
  SL = high of the momentum candle   — candle's highest price
  TP = low  of the momentum candle   — candle's lowest price

Market session filter (UTC hours)
----------------------------------
  asia    : 00:00–08:59 UTC  (Tokyo / Sydney)
  london  : 08:00–16:59 UTC  (European session)
  newyork : 13:00–21:59 UTC  (US session)
Sessions may be combined; overlapping hours are included once.
Use "all" to disable session filtering.

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

import polars as pl

from strategies.base import BaseStrategy


class MomentumCandleStrategy(BaseStrategy):
    name = "momentum_candle"

    def __init__(
        self,
        ema_period: int = 200,
        body_ratio_min: float = 0.70,
        volume_factor: float = 1.5,
        volume_lookback: int = 23,
        retracement_pct: float = 0.50,
        sl_mult: float = 1.0,
        tp_mult: float = 1.0,
        sessions: str = "all",
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
        self.body_ratio_min = body_ratio_min
        self.volume_factor = volume_factor
        self.volume_lookback = volume_lookback
        self.retracement_pct = retracement_pct
        self.sl_mult = sl_mult
        self.tp_mult = tp_mult
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
        # EMA trend filter
        df = df.with_columns(
            pl.col("close").ewm_mean(span=self.ema_period, adjust=False).alias("ema")
        )

        # Body ratio: |close - open| / (high - low)
        df = df.with_columns(
            (
                (pl.col("close") - pl.col("open")).abs()
                / (pl.col("high") - pl.col("low") + 1e-10)
            ).alias("_body_ratio")
        )

        # Rolling average volume over the PREVIOUS volume_lookback bars (excludes the signal bar
        # itself — comparing bar N's volume against bars N-lookback .. N-1 matches MT5 EA logic)
        df = df.with_columns(
            pl.col("ticks")
            .rolling_mean(window_size=self.volume_lookback)
            .shift(1)
            .alias("_avg_ticks")
        )

        # Add _trend_ok_long / _trend_ok_short columns based on selected filter
        df = self._add_sideways_filter(df)

        # Momentum candle flag (direction-agnostic)
        is_mc = (
            (pl.col("_body_ratio") >= self.body_ratio_min)
            & (pl.col("ticks") > pl.col("_avg_ticks") * self.volume_factor)
        )

        bullish = pl.col("close") > pl.col("open")
        bearish = pl.col("close") < pl.col("open")

        buy_cond  = is_mc & bullish & (pl.col("close") > pl.col("ema")) & pl.col("_trend_ok_long")
        sell_cond = is_mc & bearish & (pl.col("close") < pl.col("ema")) & pl.col("_trend_ok_short")

        candle_range = pl.col("high") - pl.col("low")
        r = self.retracement_pct

        signal = (
            pl.when(buy_cond).then(pl.lit(1))
            .when(sell_cond).then(pl.lit(-1))
            .otherwise(pl.lit(0))
            .cast(pl.Int8)
            .alias("signal")
        )
        # Limit order price — next bar must reach this level or the order is cancelled
        entry_limit = (
            pl.when(buy_cond).then(pl.col("high") - r * candle_range)
            .when(sell_cond).then(pl.col("low")   + r * candle_range)
            .otherwise(None)
            .alias("entry_limit")
        )
        # SL: sl_mult × range from the opposite extreme in signal direction
        #   BUY:  mc_high - sl_mult * range  (default 1.0 → mc_low)
        #   SELL: mc_low  + sl_mult * range  (default 1.0 → mc_high)
        sl = (
            pl.when(buy_cond).then(pl.col("high") - self.sl_mult * candle_range)
            .when(sell_cond).then(pl.col("low")   + self.sl_mult * candle_range)
            .otherwise(None)
            .alias("sl")
        )
        # TP: tp_mult × range from the near extreme in signal direction
        #   BUY:  mc_low  + tp_mult * range  (default 1.0 → mc_high)
        #   SELL: mc_high - tp_mult * range  (default 1.0 → mc_low)
        tp = (
            pl.when(buy_cond).then(pl.col("low")  + self.tp_mult * candle_range)
            .when(sell_cond).then(pl.col("high")  - self.tp_mult * candle_range)
            .otherwise(None)
            .alias("tp")
        )

        df = df.with_columns([signal, entry_limit, sl, tp])
        df = df.drop(["_body_ratio", "_avg_ticks", "_trend_ok_long", "_trend_ok_short"])
        return self._apply_session_filter(df, self.sessions)
