"""
Range Breakout Strategy.

Rules
-----
- Define a range (high/low) over the last `range_lookback` bars (no lookahead).
- Buy  when price breaks above the range high.
- Sell when price breaks below the range low.
- Stop-loss : range low  - sl_buffer (long) / range high + sl_buffer (short).
- Take-profit: entry (close) ± rr_ratio × SL distance.

Options
-------
range_mode    : "rolling" — range recalculates every bar.
                "fixed"   — range locks in on breakout; signals suppressed for
                            `range_lookback` bars to allow a new range to form.
breakout_type : "close"   — bar must close outside the range.
                "hl"      — high/low cross of the range boundary is enough.
allow_reentry : False (default) — no new signal in the same direction until price
                returns inside the range.
                True  — fire a signal on every qualifying bar.
atr_multiplier: filter out wide ranges; skip if range height > multiplier × ATR(14).
                Set to 0 to disable the filter.
ema_period    : EMA trend filter (0 = disabled).
ema_timeframe : "same" or a specific TF for higher-timeframe EMA.
sideways_filter: ADX / EMA slope / Choppiness / Alligator / StochRSI.
"""

from pathlib import Path

import polars as pl

from strategies.base import BaseStrategy

_DATA_DIR = Path(__file__).parent.parent / "data" / "parquet" / "ohlcv"


class RangeBreakoutStrategy(BaseStrategy):
    name = "range_breakout"

    def __init__(
        self,
        # Range definition
        range_lookback: int = 20,
        range_mode: str = "rolling",
        atr_period: int = 14,
        atr_multiplier: float = 0.0,
        # Entry
        breakout_type: str = "close",
        allow_reentry: bool = False,
        # Exit
        sl_buffer: float = 0.0,
        rr_ratio: float = 2.0,
        # Trend filter
        ema_period: int = 200,
        ema_timeframe: str = "same",
        symbol: str = "XAUUSD",
        # Session filter
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
        self.range_lookback = range_lookback
        self.range_mode = range_mode
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        self.breakout_type = breakout_type
        self.allow_reentry = allow_reentry
        self.sl_buffer = sl_buffer
        self.rr_ratio = rr_ratio
        self.ema_period = ema_period
        self.ema_timeframe = ema_timeframe
        self.symbol = symbol
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
        needs_ema = self.ema_period > 0 or self.sideways_filter == "ema_slope"
        if needs_ema:
            effective_period = self.ema_period if self.ema_period > 0 else 50
            if self.ema_timeframe == "same":
                df = df.with_columns(
                    pl.col("close").ewm_mean(span=effective_period, adjust=False).alias("ema")
                )
            else:
                df = self._load_htf_ema(df, effective_period)

        if self.atr_multiplier > 0:
            tr = pl.max_horizontal(
                pl.col("high") - pl.col("low"),
                (pl.col("high") - pl.col("close").shift(1)).abs(),
                (pl.col("low")  - pl.col("close").shift(1)).abs(),
            )
            df = df.with_columns(
                tr.ewm_mean(span=self.atr_period, adjust=False).alias("_atr")
            )

        # Precompute rolling range (shifted 1 bar — no lookahead)
        lb = self.range_lookback
        df = df.with_columns([
            pl.col("high").rolling_max(window_size=lb).shift(1).alias("_range_high"),
            pl.col("low").rolling_min(window_size=lb).shift(1).alias("_range_low"),
        ])

        df = self._add_sideways_filter(df)

        signals, sl_prices, tp_prices = self._scan_breakout(df)

        df = df.with_columns([
            pl.Series("signal", signals, dtype=pl.Int8),
            pl.Series("sl",     sl_prices, dtype=pl.Float64),
            pl.Series("tp",     tp_prices, dtype=pl.Float64),
        ])

        drop_cols = ["_range_high", "_range_low", "_trend_ok_long", "_trend_ok_short"]
        if "_atr" in df.columns:
            drop_cols.append("_atr")
        df = df.drop(drop_cols)

        return self._apply_session_filter(df, self.sessions)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_htf_ema(self, df: pl.DataFrame, period: int) -> pl.DataFrame:
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
            .with_columns(
                pl.col("close").ewm_mean(span=period, adjust=False).alias("ema")
            )
            .select(["time", "ema"])
        )
        return df.sort("time").join_asof(htf, on="time", strategy="backward")

    def _scan_breakout(self, df: pl.DataFrame) -> tuple[list, list, list]:
        high           = df["high"].to_list()
        low            = df["low"].to_list()
        close          = df["close"].to_list()
        range_high     = df["_range_high"].to_list()
        range_low      = df["_range_low"].to_list()
        atr            = df["_atr"].to_list() if "_atr" in df.columns else [None] * len(close)
        ema            = df["ema"].to_list() if "ema" in df.columns else [None] * len(close)
        trend_ok_long  = df["_trend_ok_long"].to_list()
        trend_ok_short = df["_trend_ok_short"].to_list()

        n_bars  = len(close)
        signals = [0]    * n_bars
        sl_out  = [None] * n_bars
        tp_out  = [None] * n_bars

        in_breakout_long  = False
        in_breakout_short = False
        cooldown          = 0

        for i in range(n_bars):
            rh = range_high[i]
            rl = range_low[i]
            if rh is None or rl is None:
                continue

            if cooldown > 0:
                cooldown -= 1
                continue

            # ATR tightness filter
            if self.atr_multiplier > 0 and atr[i] is not None:
                if (rh - rl) > self.atr_multiplier * atr[i]:
                    continue

            # EMA trend filter
            ema_ok_long  = True
            ema_ok_short = True
            if self.ema_period > 0 and ema[i] is not None:
                ema_ok_long  = close[i] > ema[i]
                ema_ok_short = close[i] < ema[i]

            # Re-entry reset: price returned inside range
            if not self.allow_reentry:
                if in_breakout_long  and rl <= close[i] <= rh:
                    in_breakout_long  = False
                if in_breakout_short and rl <= close[i] <= rh:
                    in_breakout_short = False

            # Breakout detection
            if self.breakout_type == "close":
                long_break  = close[i] > rh
                short_break = close[i] < rl
            else:  # "hl"
                long_break  = high[i] > rh
                short_break = low[i]  < rl

            # Long signal
            if (long_break
                    and (self.allow_reentry or not in_breakout_long)
                    and ema_ok_long
                    and trend_ok_long[i]):
                sl_price = rl - self.sl_buffer
                sl_dist  = close[i] - sl_price
                if sl_dist > 0:
                    signals[i] = 1
                    sl_out[i]  = sl_price
                    tp_out[i]  = close[i] + self.rr_ratio * sl_dist
                    if not self.allow_reentry:
                        in_breakout_long = True
                    if self.range_mode == "fixed":
                        cooldown = self.range_lookback

            # Short signal
            elif (short_break
                    and (self.allow_reentry or not in_breakout_short)
                    and ema_ok_short
                    and trend_ok_short[i]):
                sl_price = rh + self.sl_buffer
                sl_dist  = sl_price - close[i]
                if sl_dist > 0:
                    signals[i] = -1
                    sl_out[i]  = sl_price
                    tp_out[i]  = close[i] - self.rr_ratio * sl_dist
                    if not self.allow_reentry:
                        in_breakout_short = True
                    if self.range_mode == "fixed":
                        cooldown = self.range_lookback

        return signals, sl_out, tp_out
