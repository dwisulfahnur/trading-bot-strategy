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


class MomentumCandleStrategy(BaseStrategy):
    name = "momentum_candle"

    def __init__(
        self,
        symbol: str = "XAUUSD",
        ema_period: int = 200,
        ema_fast_period: int = 50,
        ema_filter_mode: str = "single",
        ema_timeframe: str = "same",
        body_ratio_min: float = 0.70,
        volume_factor: float = 1.5,
        volume_lookback: int = 23,
        retracement_pct: float = 0.50,
        trade_direction: str = "both",
        # Long (buy) SL/TP
        long_sl_tp_mode: str = "candle",   # candle | pips
        long_sl_mult: float = 1.0,
        long_tp_mult: float = 1.0,
        long_sl_pips: float = 200.0,
        long_tp_pips: float = 400.0,
        # Short (sell) SL/TP
        short_sl_tp_mode: str = "candle",
        short_sl_mult: float = 1.0,
        short_tp_mult: float = 1.0,
        short_sl_pips: float = 200.0,
        short_tp_pips: float = 400.0,
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
        self.symbol = symbol
        self.ema_period = ema_period
        self.ema_fast_period = ema_fast_period
        self.ema_filter_mode = ema_filter_mode
        self.ema_timeframe = ema_timeframe
        self.body_ratio_min = body_ratio_min
        self.volume_factor = volume_factor
        self.volume_lookback = volume_lookback
        self.retracement_pct = retracement_pct
        self.pip_mult = _PIP_MULT.get(symbol, 10.0)
        self.trade_direction = trade_direction
        self.long_sl_tp_mode = long_sl_tp_mode
        self.long_sl_mult = long_sl_mult
        self.long_tp_mult = long_tp_mult
        self.long_sl_pips = long_sl_pips
        self.long_tp_pips = long_tp_pips
        self.short_sl_tp_mode = short_sl_tp_mode
        self.short_sl_mult = short_sl_mult
        self.short_tp_mult = short_tp_mult
        self.short_sl_pips = short_sl_pips
        self.short_tp_pips = short_tp_pips
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
        # EMA trend filter (same TF or higher TF via join_asof)
        if self.ema_timeframe == "same":
            df = df.with_columns([
                pl.col("close").ewm_mean(span=self.ema_period,      adjust=False).alias("ema"),
                pl.col("close").ewm_mean(span=self.ema_fast_period, adjust=False).alias("_ema_fast"),
            ])
        else:
            df = self._load_htf_ema(df)

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

        if self.ema_filter_mode == "dual":
            ema_ok_long  = pl.col("_ema_fast") > pl.col("ema")
            ema_ok_short = pl.col("_ema_fast") < pl.col("ema")
        elif self.ema_filter_mode == "single":
            ema_ok_long  = pl.col("close") > pl.col("ema")
            ema_ok_short = pl.col("close") < pl.col("ema")
        else:  # "none"
            ema_ok_long  = pl.lit(True)
            ema_ok_short = pl.lit(True)

        if self.trade_direction == "short_only":
            ema_ok_long = pl.lit(False)
        elif self.trade_direction == "long_only":
            ema_ok_short = pl.lit(False)

        # Momentum candle flag (direction-agnostic)
        is_mc = (
            (pl.col("_body_ratio") >= self.body_ratio_min)
            & (pl.col("ticks") > pl.col("_avg_ticks") * self.volume_factor)
        )

        bullish = pl.col("close") > pl.col("open")
        bearish = pl.col("close") < pl.col("open")

        buy_cond  = is_mc & bullish & ema_ok_long  & pl.col("_trend_ok_long")
        sell_cond = is_mc & bearish & ema_ok_short & pl.col("_trend_ok_short")

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

        # Long SL/TP
        if self.long_sl_tp_mode == "pips":
            sl_buy = (pl.col("high") - r * candle_range) - self.long_sl_pips / self.pip_mult
            tp_buy = (pl.col("high") - r * candle_range) + self.long_tp_pips / self.pip_mult
        else:  # candle
            sl_buy = pl.col("high") - self.long_sl_mult * candle_range
            tp_buy = pl.col("low")  + self.long_tp_mult * candle_range

        # Short SL/TP
        if self.short_sl_tp_mode == "pips":
            sl_sell = (pl.col("low") + r * candle_range) + self.short_sl_pips / self.pip_mult
            tp_sell = (pl.col("low") + r * candle_range) - self.short_tp_pips / self.pip_mult
        else:  # candle
            sl_sell = pl.col("low")  + self.short_sl_mult * candle_range
            tp_sell = pl.col("high") - self.short_tp_mult * candle_range

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

        df = df.with_columns([signal, entry_limit, sl, tp])
        df = df.drop(["_body_ratio", "_avg_ticks", "_ema_fast", "_trend_ok_long", "_trend_ok_short"])
        return self._apply_session_filter(df, self.sessions)

    def _load_htf_ema(self, df: pl.DataFrame) -> pl.DataFrame:
        """Load a higher-timeframe parquet, compute both EMAs there, join_asof back."""
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

        return df.sort("time").join_asof(htf, on="time", strategy="backward")
