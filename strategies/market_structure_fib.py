"""
EMA Trend + Fibonacci Retracement Strategy.

Rules
-----
- Trend direction determined by EMA (same or higher timeframe):
    Uptrend  : close > EMA  → look for long entries
    Downtrend: close < EMA  → look for short entries
- When a new swing high is confirmed in an uptrend:
    Impulse leg = last swing low → new swing high
    BUY LIMIT   = swing_high - impulse × fib_entry
- When a new swing low is confirmed in a downtrend:
    Impulse leg = last swing high → new swing low
    SELL LIMIT  = swing_low + impulse × fib_entry
- Stop-loss (sl_mode):
    swing_point  : last swing low (long) / last swing high (short)
    signal_candle: low of the signal bar (long) / high (short)
- Take-profit: entry ± rr_ratio × |entry - SL|
- Pending order cancellation (pending_cancel):
    none     : only TP-overshoot auto-cancel
    max_bars : expire after max_pending_bars bars
    hl_break : cancel if price breaks below the swing low (long) or above swing high (short)
    both     : max_bars + hl_break
"""

from pathlib import Path

import polars as pl

from strategies.base import BaseStrategy

_DATA_DIR = Path(__file__).parent.parent / "data" / "parquet" / "ohlcv"


class MarketStructureFibStrategy(BaseStrategy):
    name = "market_structure_fib"

    def __init__(
        self,
        swing_n: int = 5,
        fib_entry: float = 0.618,
        sl_mode: str = "swing_point",
        rr_ratio: float = 2.0,
        pending_cancel: str = "both",
        max_pending_bars: int = 20,
        ema_period: int = 200,
        ema_timeframe: str = "same",
        symbol: str = "XAUUSD",
        sessions: str = "all",
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
        self.swing_n = swing_n
        self.fib_entry = fib_entry
        self.sl_mode = sl_mode
        self.rr_ratio = rr_ratio
        self.pending_cancel = pending_cancel
        self.max_pending_bars = max_pending_bars
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
        n = self.swing_n

        if self.ema_timeframe == "same":
            df = df.with_columns(
                pl.col("close").ewm_mean(span=self.ema_period, adjust=False).alias("ema")
            )
        else:
            df = self._load_htf_ema(df)

        df = df.with_columns([
            self._swing_high_price(n).alias("_sh"),
            self._swing_low_price(n).alias("_sl"),
        ])

        df = self._add_sideways_filter(df)

        signals, sl_prices, tp_prices, entry_limits, cancel_levels = self._scan(df)

        df = df.with_columns([
            pl.Series("signal",       signals,       dtype=pl.Int8),
            pl.Series("sl",           sl_prices,     dtype=pl.Float64),
            pl.Series("tp",           tp_prices,     dtype=pl.Float64),
            pl.Series("entry_limit",  entry_limits,  dtype=pl.Float64),
            pl.Series("cancel_level", cancel_levels, dtype=pl.Float64),
        ])

        df = df.drop(["_sh", "_sl", "_trend_ok_long", "_trend_ok_short"])

        return self._apply_session_filter(df, self.sessions)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_htf_ema(self, df: pl.DataFrame) -> pl.DataFrame:
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
                pl.col("close").ewm_mean(span=self.ema_period, adjust=False).alias("ema")
            )
            .select(["time", "ema"])
        )
        return df.sort("time").join_asof(htf, on="time", strategy="backward")

    @staticmethod
    def _swing_high_price(n: int) -> pl.Expr:
        """Confirmed swing high: high[i] > n bars each side. Result shifted n bars to avoid lookahead."""
        mask = pl.lit(True)
        for j in range(1, n + 1):
            mask = mask & (pl.col("high") > pl.col("high").shift(j))
            mask = mask & (pl.col("high") > pl.col("high").shift(-j))
        confirmed = mask.shift(n)
        return pl.when(confirmed).then(pl.col("high").shift(n)).otherwise(None)

    @staticmethod
    def _swing_low_price(n: int) -> pl.Expr:
        """Confirmed swing low: low[i] < n bars each side. Result shifted n bars to avoid lookahead."""
        mask = pl.lit(True)
        for j in range(1, n + 1):
            mask = mask & (pl.col("low") < pl.col("low").shift(j))
            mask = mask & (pl.col("low") < pl.col("low").shift(-j))
        confirmed = mask.shift(n)
        return pl.when(confirmed).then(pl.col("low").shift(n)).otherwise(None)

    def _scan(self, df: pl.DataFrame) -> tuple[list, list, list, list, list]:
        """
        Bar-by-bar scan.

        Long  : new swing high confirmed + close > EMA
                impulse = last_sl → new_sh
                entry_limit = new_sh - impulse × fib_entry

        Short : new swing low confirmed + close < EMA
                impulse = last_sh → new_sl
                entry_limit = new_sl + impulse × fib_entry
        """
        sh_arr         = df["_sh"].to_list()
        sl_arr         = df["_sl"].to_list()
        close          = df["close"].to_list()
        high           = df["high"].to_list()
        low            = df["low"].to_list()
        ema            = df["ema"].to_list()
        trend_ok_long  = df["_trend_ok_long"].to_list()
        trend_ok_short = df["_trend_ok_short"].to_list()

        n_bars        = len(close)
        signals       = [0]    * n_bars
        sl_out        = [None] * n_bars
        tp_out        = [None] * n_bars
        entry_lims    = [None] * n_bars
        cancel_levels = [None] * n_bars

        last_sh: float | None = None
        last_sl: float | None = None

        for i in range(n_bars):
            c = close[i]

            # ── New swing high → potential long entry ─────────────────────
            if sh_arr[i] is not None:
                sh_p = sh_arr[i]

                if (last_sl is not None
                        and c > ema[i]
                        and trend_ok_long[i]
                        and ema[i] < last_sl):  # EMA must be below the entire structure

                    impulse = sh_p - last_sl
                    if impulse > 0:
                        entry    = sh_p - impulse * self.fib_entry
                        sl_price = last_sl if self.sl_mode == "swing_point" else low[i]
                        sl_dist  = entry - sl_price

                        if sl_dist > 0:
                            signals[i]    = 1
                            entry_lims[i] = entry
                            sl_out[i]     = sl_price
                            tp_out[i]     = entry + self.rr_ratio * sl_dist
                            if self.pending_cancel in ("hl_break", "both"):
                                cancel_levels[i] = last_sl

                last_sh = sh_p

            # ── New swing low → potential short entry ─────────────────────
            if sl_arr[i] is not None:
                sl_p = sl_arr[i]

                if (last_sh is not None
                        and c < ema[i]
                        and trend_ok_short[i]
                        and ema[i] > last_sh):  # EMA must be above the entire structure

                    impulse = last_sh - sl_p
                    if impulse > 0:
                        entry    = sl_p + impulse * self.fib_entry
                        sl_price = last_sh if self.sl_mode == "swing_point" else high[i]
                        sl_dist  = sl_price - entry

                        if sl_dist > 0:
                            signals[i]    = -1
                            entry_lims[i] = entry
                            sl_out[i]     = sl_price
                            tp_out[i]     = entry - self.rr_ratio * sl_dist
                            if self.pending_cancel in ("hl_break", "both"):
                                cancel_levels[i] = last_sh

                last_sl = sl_p

        return signals, sl_out, tp_out, entry_lims, cancel_levels
