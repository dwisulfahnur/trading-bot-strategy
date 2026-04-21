"""
N Structure Breakout Strategy.

Rules
-----
- 200 EMA defines trend direction (or a higher-timeframe EMA).
- Swing highs/lows are identified using `swing_n` bars on each side (fractal method).
- Bullish N  : swing high (H1) → pullback to higher low (HL) → close above H1.
- Bearish N  : swing low  (L1) → bounce  to lower  high (LH) → close below L1.
- Entry      : next bar's open after signal (no lookahead).
- Stop-loss  : controlled by `sl_mode` —
    swing_midpoint : (H1 + HL) / 2  or  (LH + L1) / 2
    swing_point    : at HL (long)   or  at LH (short)
    signal_candle  : low of signal bar (long) / high of signal bar (short)
- Take-profit: entry ± rr_ratio × (entry - SL).

Sideways Filter (optional) — same machinery as William Fractals.
"""

from pathlib import Path

import polars as pl

from strategies.base import BaseStrategy

_DATA_DIR = Path(__file__).parent.parent / "data" / "parquet" / "ohlcv"


class NStructureStrategy(BaseStrategy):
    name = "n_structure"

    def __init__(
        self,
        ema_period: int = 200,
        ema_timeframe: str = "same",
        symbol: str = "XAUUSD",
        swing_n: int = 5,
        rr_ratio: float = 2.0,
        sl_mode: str = "swing_midpoint",
        # Market session filter
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
        self.ema_period = ema_period
        self.ema_timeframe = ema_timeframe
        self.symbol = symbol
        self.swing_n = swing_n
        self.rr_ratio = rr_ratio
        self.sl_mode = sl_mode
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

        signals, sl_prices, tp_prices = self._scan_n_structure(df)

        df = df.with_columns([
            pl.Series("signal",  signals,   dtype=pl.Int8),
            pl.Series("sl",      sl_prices, dtype=pl.Float64),
            pl.Series("tp",      tp_prices, dtype=pl.Float64),
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

    def _scan_n_structure(self, df: pl.DataFrame) -> tuple[list, list, list]:
        """
        Iterate bar-by-bar to detect N structure breakouts.

        Bullish N state:
          last_sh — most recent confirmed swing high (H1)
          hl      — most recent swing low AFTER last_sh (the pullback / HL)

        Bearish N state:
          last_sl — most recent confirmed swing low (L1)
          lh      — most recent swing high AFTER last_sl (the bounce / LH)

        When a new SH fires it simultaneously:
          - resets the bullish chain (need a fresh HL after this new H1)
          - supplies the LH for the bearish chain (bounce above L1)

        When a new SL fires it simultaneously:
          - resets the bearish chain (need a fresh LH after this new L1)
          - supplies the HL for the bullish chain (pullback below H1)
        """
        sh_arr         = df["_sh"].to_list()
        sl_arr         = df["_sl"].to_list()
        close          = df["close"].to_list()
        high           = df["high"].to_list()
        low            = df["low"].to_list()
        ema            = df["ema"].to_list()
        trend_ok_long  = df["_trend_ok_long"].to_list()
        trend_ok_short = df["_trend_ok_short"].to_list()

        n_bars   = len(close)
        signals  = [0]    * n_bars
        sl_out   = [None] * n_bars
        tp_out   = [None] * n_bars

        last_sh: float | None = None   # H1 of bullish N
        hl:      float | None = None   # pullback low after H1
        last_sl: float | None = None   # L1 of bearish N
        lh:      float | None = None   # bounce high after L1

        for i in range(n_bars):
            c      = close[i]
            prev_c = close[i - 1] if i > 0 else c

            # ── Update swing-point state ──────────────────────────────────

            if sh_arr[i] is not None:
                sh_p = sh_arr[i]
                last_sh = sh_p
                hl = None                               # need fresh HL after new H1
                if last_sl is not None and sh_p > last_sl:
                    lh = sh_p                           # this SH is the LH for short N

            if sl_arr[i] is not None:
                sl_p = sl_arr[i]
                last_sl = sl_p
                lh = None                               # need fresh LH after new L1
                if last_sh is not None and sl_p < last_sh:
                    hl = sl_p                           # this SL is the HL for long N

            # ── Bullish N breakout ────────────────────────────────────────
            if (last_sh is not None
                    and hl is not None
                    and c > last_sh
                    and prev_c <= last_sh               # first bar to close above H1
                    and c > ema[i]                      # uptrend confirmation
                    and trend_ok_long[i]):

                if self.sl_mode == "swing_midpoint":
                    sl_price = (last_sh + hl) / 2.0
                elif self.sl_mode == "swing_point":
                    sl_price = hl
                else:                                   # signal_candle
                    sl_price = low[i]

                dist = c - sl_price
                if dist > 0:
                    signals[i] = 1
                    sl_out[i]  = sl_price
                    tp_out[i]  = c + self.rr_ratio * dist
                    hl = None                           # reset: need new N before next long

            # ── Bearish N breakout (inverted N) ──────────────────────────
            elif (last_sl is not None
                    and lh is not None
                    and c < last_sl
                    and prev_c >= last_sl               # first bar to close below L1
                    and c < ema[i]                      # downtrend confirmation
                    and trend_ok_short[i]):

                if self.sl_mode == "swing_midpoint":
                    sl_price = (last_sl + lh) / 2.0
                elif self.sl_mode == "swing_point":
                    sl_price = lh
                else:                                   # signal_candle
                    sl_price = high[i]

                dist = sl_price - c
                if dist > 0:
                    signals[i] = -1
                    sl_out[i]  = sl_price
                    tp_out[i]  = c - self.rr_ratio * dist
                    lh = None                           # reset: need new N before next short

        return signals, sl_out, tp_out
