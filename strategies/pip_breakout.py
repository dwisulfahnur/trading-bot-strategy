"""
Pip Breakout Strategy.

Rules
-----
- entry_mode="close"  (default): signal fires when the bar closes above the
  N-bar rolling high (long) / below the rolling low (short).  Entry is a
  market order at the next bar's open, or a stop order offset pips beyond
  the rolling level when entry_offset_pips > 0.
- entry_mode="touch": a stop order is placed at the rolling high + offset
  (long) / rolling low - offset (short) without waiting for a bar close.
  Fills the moment price touches the level intrabar.  max_pending_bars
  controls how long the order waits before being cancelled.
- EMA trend filter: none | single (price vs slow EMA) | dual (fast vs slow EMA).
  EMA may be sourced from a higher timeframe parquet file via ema_timeframe.
- SL / TP fixed in pips, anchored to the actual entry level.
- Supports session filter and all sideways filters.
"""

from pathlib import Path

import polars as pl

from strategies.base import BaseStrategy

_DATA_DIR = Path(__file__).parent.parent / "data" / "parquet" / "ohlcv"

_PIP_MULT: dict[str, float] = {
    "XAUUSD": 10.0,
    "BTCUSD": 1.0,  "ETHUSD": 1.0,  "USTEC": 1.0,
    "EURUSD": 10_000.0, "GBPUSD": 10_000.0,
    "EURJPY": 100.0, "GBPJPY": 100.0, "USDJPY": 100.0,
    "AUDJPY": 100.0, "CADJPY": 100.0, "CHFJPY": 100.0,
    "EURGBP": 10_000.0,
}


class PipBreakoutStrategy(BaseStrategy):
    name = "pip_breakout"

    def __init__(
        self,
        symbol: str = "XAUUSD",
        level_detector: str = "rolling",   # rolling | fractal
        lookback_bars: int = 20,
        fractal_n_before: int = 5,
        fractal_n_after: int = 5,
        sl_tp_mode: str = "pips",          # pips | pct | atr
        sl_pips: float = 200.0,
        tp_pips: float = 400.0,
        sl_pct: float = 1.0,
        tp_pct: float = 2.0,
        atr_period: int = 14,
        atr_sl_mult: float = 1.5,
        atr_tp_mult: float = 3.0,
        entry_offset_pips: float = 0.0,
        entry_mode: str = "close",         # close | touch
        pending_cancel: str = "max_bars",  # none | max_bars | sl_break | both
        max_pending_bars: int = 10,
        pending_cancel_buffer_pips: float = 0.0,
        ema_period: int = 200,
        ema_fast_period: int = 50,
        ema_timeframe: str = "same",
        ema_filter_mode: str = "single",   # none | single | dual
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
        self.symbol = symbol
        self.level_detector = level_detector
        self.lookback_bars = lookback_bars
        self.fractal_n_before = fractal_n_before
        self.fractal_n_after = fractal_n_after
        self.sl_tp_mode = sl_tp_mode
        self.sl_pips = sl_pips
        self.tp_pips = tp_pips
        self.sl_pct = sl_pct
        self.tp_pct = tp_pct
        self.atr_period = atr_period
        self.atr_sl_mult = atr_sl_mult
        self.atr_tp_mult = atr_tp_mult
        self.entry_offset_pips = entry_offset_pips
        self.entry_mode = entry_mode
        self.pending_cancel = pending_cancel
        self.max_pending_bars = max_pending_bars
        self.pending_cancel_buffer_pips = pending_cancel_buffer_pips
        self.pip_mult = _PIP_MULT.get(symbol, 10.0)
        self.ema_period = ema_period
        self.ema_fast_period = ema_fast_period
        self.ema_timeframe = ema_timeframe
        self.ema_filter_mode = ema_filter_mode
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

    def generate_signals(self, df: pl.DataFrame) -> pl.DataFrame:
        offset_dist = self.entry_offset_pips / self.pip_mult
        buf_dist    = self.pending_cancel_buffer_pips / self.pip_mult

        # ── Polars: EMA ───────────────────────────────────────────────────
        if self.ema_timeframe == "same":
            df = df.with_columns([
                pl.col("close").ewm_mean(span=self.ema_period,      adjust=False).alias("ema"),
                pl.col("close").ewm_mean(span=self.ema_fast_period, adjust=False).alias("_ema_fast"),
            ])
        else:
            df = self._load_htf_ema(df)

        # ── Polars: level detector ────────────────────────────────────────
        if self.level_detector == "fractal":
            # Confirmed fractal: bar i is higher/lower than fractal_n bars on each side.
            # Result is shifted fractal_n_after bars so confirmation arrives without lookahead.
            df = df.with_columns([
                self._fractal_high(self.fractal_n_before, self.fractal_n_after).alias("_level_high"),
                self._fractal_low(self.fractal_n_before, self.fractal_n_after).alias("_level_low"),
            ])
        else:
            # Rolling N-bar max/min; shift(1) excludes the current bar
            n = self.lookback_bars
            df = df.with_columns([
                pl.col("high").shift(1).rolling_max(window_size=n).alias("_level_high"),
                pl.col("low").shift(1).rolling_min(window_size=n).alias("_level_low"),
            ])

        # ── ATR (Wilder's EWM) ───────────────────────────────────────────
        df = df.with_columns([
            pl.max_horizontal(
                pl.col("high") - pl.col("low"),
                (pl.col("high") - pl.col("close").shift(1)).abs(),
                (pl.col("low")  - pl.col("close").shift(1)).abs(),
            ).alias("_tr")
        ])
        df = df.with_columns(
            pl.col("_tr").ewm_mean(alpha=1.0 / self.atr_period, adjust=False).alias("_atr")
        )

        df = self._add_sideways_filter(df)

        # ── Pull arrays for Python scan ───────────────────────────────────
        level_high     = df["_level_high"].to_list()
        level_low      = df["_level_low"].to_list()
        close_arr      = df["close"].to_list()
        ema_arr        = df["ema"].to_list()
        ema_fast_arr   = df["_ema_fast"].to_list()
        trend_ok_long  = df["_trend_ok_long"].to_list()
        trend_ok_short = df["_trend_ok_short"].to_list()
        atr_arr        = df["_atr"].to_list()
        n_bars         = len(close_arr)

        if self.ema_filter_mode == "dual":
            ema_ok_long_arr  = [ef is not None and es is not None and ef > es
                                for ef, es in zip(ema_fast_arr, ema_arr)]
            ema_ok_short_arr = [ef is not None and es is not None and ef < es
                                for ef, es in zip(ema_fast_arr, ema_arr)]
        elif self.ema_filter_mode == "single":
            ema_ok_long_arr  = [es is not None and c > es for c, es in zip(close_arr, ema_arr)]
            ema_ok_short_arr = [es is not None and c < es for c, es in zip(close_arr, ema_arr)]
        else:
            ema_ok_long_arr  = [True] * n_bars
            ema_ok_short_arr = [True] * n_bars

        # ── Bar-by-bar scan ───────────────────────────────────────────────
        # A signal fires only when the detected level is NEW (different from the
        # one that generated the previous signal).  For fractal mode the "current
        # level" is the most-recently confirmed fractal; it is tracked separately
        # from the "last used" level so that a fractal forming while another signal
        # is still pending does not get silently skipped.
        signals     = [0]    * n_bars
        sl_out      = [None] * n_bars
        tp_out      = [None] * n_bars
        entry_stops = [None] * n_bars
        cancel_lvls = [None] * n_bars

        last_used_high: float | None = None
        last_used_low:  float | None = None
        emit_cancel = self.pending_cancel in ("sl_break", "both")

        # Fractal mode: track the most-recently confirmed fractal level so we can
        # still signal on it even on bars where no NEW fractal is confirmed.
        cur_frac_high: float | None = None
        cur_frac_low:  float | None = None

        for i in range(n_bars):
            c        = close_arr[i]
            long_ok  = ema_ok_long_arr[i]  and trend_ok_long[i]
            short_ok = ema_ok_short_arr[i] and trend_ok_short[i]

            if self.level_detector == "fractal":
                # Update running fractal levels whenever a new one is confirmed
                if level_high[i] is not None:
                    cur_frac_high = level_high[i]
                if level_low[i] is not None:
                    cur_frac_low = level_low[i]
                rh = cur_frac_high
                rl = cur_frac_low
            else:
                rh = level_high[i]
                rl = level_low[i]

            atr_i = atr_arr[i]

            if self.entry_mode == "touch":
                if long_ok and rh is not None and rh != last_used_high:
                    stop = rh + offset_dist
                    sl_p, tp_p = self._sl_tp_scalar(stop, 1, atr_i)
                    signals[i]     = 1
                    entry_stops[i] = stop
                    sl_out[i]      = sl_p
                    tp_out[i]      = tp_p
                    if emit_cancel:
                        cancel_lvls[i] = sl_p - buf_dist
                    last_used_high = rh

                elif short_ok and rl is not None and rl != last_used_low:
                    stop = rl - offset_dist
                    sl_p, tp_p = self._sl_tp_scalar(stop, -1, atr_i)
                    signals[i]     = -1
                    entry_stops[i] = stop
                    sl_out[i]      = sl_p
                    tp_out[i]      = tp_p
                    if emit_cancel:
                        cancel_lvls[i] = sl_p + buf_dist
                    last_used_low = rl

            else:  # close mode
                if (long_ok and rh is not None and c is not None
                        and c > rh and rh != last_used_high):
                    anchor = rh + offset_dist if offset_dist > 0 else c
                    sl_p, tp_p = self._sl_tp_scalar(anchor, 1, atr_i)
                    signals[i] = 1
                    sl_out[i]  = sl_p
                    tp_out[i]  = tp_p
                    if offset_dist > 0:
                        entry_stops[i] = anchor
                        if emit_cancel:
                            cancel_lvls[i] = sl_p - buf_dist
                    last_used_high = rh

                elif (short_ok and rl is not None and c is not None
                        and c < rl and rl != last_used_low):
                    anchor = rl - offset_dist if offset_dist > 0 else c
                    sl_p, tp_p = self._sl_tp_scalar(anchor, -1, atr_i)
                    signals[i] = -1
                    sl_out[i]  = sl_p
                    tp_out[i]  = tp_p
                    if offset_dist > 0:
                        entry_stops[i] = anchor
                        if emit_cancel:
                            cancel_lvls[i] = sl_p + buf_dist
                    last_used_low = rl

        # ── Assign output columns ─────────────────────────────────────────
        df = df.with_columns([
            pl.Series("signal",     signals,     dtype=pl.Int8),
            pl.Series("sl",         sl_out,      dtype=pl.Float64),
            pl.Series("tp",         tp_out,      dtype=pl.Float64),
            pl.Series("entry_stop", entry_stops, dtype=pl.Float64),
        ])
        if emit_cancel:
            df = df.with_columns(pl.Series("cancel_level", cancel_lvls, dtype=pl.Float64))

        df = df.drop(["_level_high", "_level_low", "_ema_fast", "_trend_ok_long", "_trend_ok_short", "_tr", "_atr"])
        return self._apply_session_filter(df, self.sessions)

    @staticmethod
    def _fractal_high(n_before: int, n_after: int) -> pl.Expr:
        """Confirmed fractal high: bar's high > n_before bars left AND > n_after bars right.
        Shifted n_after bars so the result lands on the bar where confirmation is known."""
        mask = pl.lit(True)
        for j in range(1, n_before + 1):
            mask = mask & (pl.col("high") > pl.col("high").shift(j))
        for j in range(1, n_after + 1):
            mask = mask & (pl.col("high") > pl.col("high").shift(-j))
        confirmed = mask.shift(n_after)
        return pl.when(confirmed).then(pl.col("high").shift(n_after)).otherwise(None)

    @staticmethod
    def _fractal_low(n_before: int, n_after: int) -> pl.Expr:
        """Confirmed fractal low: bar's low < n_before bars left AND < n_after bars right.
        Shifted n_after bars so the result lands on the bar where confirmation is known."""
        mask = pl.lit(True)
        for j in range(1, n_before + 1):
            mask = mask & (pl.col("low") < pl.col("low").shift(j))
        for j in range(1, n_after + 1):
            mask = mask & (pl.col("low") < pl.col("low").shift(-j))
        confirmed = mask.shift(n_after)
        return pl.when(confirmed).then(pl.col("low").shift(n_after)).otherwise(None)

    def _sl_tp_scalar(self, anchor: float, direction: int, atr: float | None = None) -> tuple[float, float]:
        """Return (sl_price, tp_price) from a scalar anchor price and direction (+1/-1)."""
        if self.sl_tp_mode == "pct":
            sl = anchor * (1.0 - self.sl_pct / 100.0) if direction == 1 else anchor * (1.0 + self.sl_pct / 100.0)
            tp = anchor * (1.0 + self.tp_pct / 100.0) if direction == 1 else anchor * (1.0 - self.tp_pct / 100.0)
        elif self.sl_tp_mode == "atr" and atr is not None:
            sl_d = atr * self.atr_sl_mult
            tp_d = atr * self.atr_tp_mult
            sl   = anchor - sl_d if direction == 1 else anchor + sl_d
            tp   = anchor + tp_d if direction == 1 else anchor - tp_d
        else:  # pips (default)
            sl_d = self.sl_pips / self.pip_mult
            tp_d = self.tp_pips / self.pip_mult
            sl   = anchor - sl_d if direction == 1 else anchor + sl_d
            tp   = anchor + tp_d if direction == 1 else anchor - tp_d
        return sl, tp

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
