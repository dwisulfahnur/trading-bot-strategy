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

_PIP_MULT: dict[str, float] = {
    "XAUUSD": 10.0,
    "XAGUSD": 100.0,
    "BTCUSD": 1.0,  "ETHUSD": 1.0,  "USTEC": 1.0,
    "EURUSD": 10_000.0, "GBPUSD": 10_000.0,
    "EURJPY": 100.0, "GBPJPY": 100.0, "USDJPY": 100.0,
    "AUDJPY": 100.0, "CADJPY": 100.0, "CHFJPY": 100.0,
    "EURGBP": 10_000.0,
}


class NStructureStrategy(BaseStrategy):
    name = "n_structure"

    def __init__(
        self,
        ema_period: int = 200,
        ema_fast_period: int = 50,
        ema_timeframe: str = "same",
        ema_filter_mode: str = "single",
        symbol: str = "XAUUSD",
        swing_n_before: int = 5,
        swing_n_after: int = 5,
        trade_direction: str = "both",
        # Long (buy) SL/TP
        long_sl_tp_mode: str = "rr",
        long_rr_ratio: float = 2.0,
        long_sl_mode: str = "swing_midpoint",   # swing_midpoint | swing_point | signal_candle
        long_sl_pips: float = 200.0,
        long_tp_pips: float = 400.0,
        # Short (sell) SL/TP
        short_sl_tp_mode: str = "rr",
        short_rr_ratio: float = 2.0,
        short_sl_mode: str = "swing_midpoint",
        short_sl_pips: float = 200.0,
        short_tp_pips: float = 400.0,
        # Pending order cancellation
        pending_cancel: str = "max_bars",
        max_pending_bars: int = 10,
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
        self.ema_fast_period = ema_fast_period
        self.ema_timeframe = ema_timeframe
        self.ema_filter_mode = ema_filter_mode
        self.symbol = symbol
        self.pip_mult = _PIP_MULT.get(symbol, 10.0)
        self.swing_n_before = swing_n_before
        self.swing_n_after = swing_n_after
        self.trade_direction = trade_direction
        self.long_sl_tp_mode = long_sl_tp_mode
        self.long_rr_ratio = long_rr_ratio
        self.long_sl_mode = long_sl_mode
        self.long_sl_pips = long_sl_pips
        self.long_tp_pips = long_tp_pips
        self.short_sl_tp_mode = short_sl_tp_mode
        self.short_rr_ratio = short_rr_ratio
        self.short_sl_mode = short_sl_mode
        self.short_sl_pips = short_sl_pips
        self.short_tp_pips = short_tp_pips
        self.pending_cancel = pending_cancel
        self.max_pending_bars = max_pending_bars
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
        if self.ema_timeframe == "same":
            df = df.with_columns([
                pl.col("close").ewm_mean(span=self.ema_period,      adjust=False).alias("ema"),
                pl.col("close").ewm_mean(span=self.ema_fast_period, adjust=False).alias("_ema_fast"),
            ])
        else:
            df = self._load_htf_ema(df)

        df = df.with_columns([
            self._swing_high_price(self.swing_n_before, self.swing_n_after).alias("_sh"),
            self._swing_low_price(self.swing_n_before, self.swing_n_after).alias("_sl"),
        ])

        df = self._add_sideways_filter(df)

        signals, sl_prices, tp_prices, entry_stops, cancel_levels = self._scan_n_structure(df)

        df = df.with_columns([
            pl.Series("signal",        signals,       dtype=pl.Int8),
            pl.Series("sl",            sl_prices,     dtype=pl.Float64),
            pl.Series("tp",            tp_prices,     dtype=pl.Float64),
            pl.Series("entry_stop",    entry_stops,   dtype=pl.Float64),
            pl.Series("cancel_level",  cancel_levels, dtype=pl.Float64),
        ])

        df = df.drop(["_sh", "_sl", "_ema_fast", "_trend_ok_long", "_trend_ok_short"])

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
            .with_columns([
                pl.col("close").ewm_mean(span=self.ema_period,      adjust=False).alias("ema"),
                pl.col("close").ewm_mean(span=self.ema_fast_period, adjust=False).alias("_ema_fast"),
            ])
            .select(["time", "ema", "_ema_fast"])
        )
        return df.sort("time").join_asof(htf, on="time", strategy="backward")

    @staticmethod
    def _swing_high_price(n_before: int, n_after: int) -> pl.Expr:
        """Confirmed swing high: high[i] > n_before bars to the left and n_after bars to the right.
        Result is shifted n_after bars to avoid lookahead."""
        mask = pl.lit(True)
        for j in range(1, n_before + 1):
            mask = mask & (pl.col("high") > pl.col("high").shift(j))
        for j in range(1, n_after + 1):
            mask = mask & (pl.col("high") > pl.col("high").shift(-j))
        confirmed = mask.shift(n_after)
        return pl.when(confirmed).then(pl.col("high").shift(n_after)).otherwise(None)

    @staticmethod
    def _swing_low_price(n_before: int, n_after: int) -> pl.Expr:
        """Confirmed swing low: low[i] < n_before bars to the left and n_after bars to the right.
        Result is shifted n_after bars to avoid lookahead."""
        mask = pl.lit(True)
        for j in range(1, n_before + 1):
            mask = mask & (pl.col("low") < pl.col("low").shift(j))
        for j in range(1, n_after + 1):
            mask = mask & (pl.col("low") < pl.col("low").shift(-j))
        confirmed = mask.shift(n_after)
        return pl.when(confirmed).then(pl.col("low").shift(n_after)).otherwise(None)

    def _scan_n_structure(self, df: pl.DataFrame) -> tuple[list, list, list, list, list]:
        """
        Iterate bar-by-bar to arm stop-entry orders for N structure breakouts.

        Bullish N state:
          last_sh — most recent confirmed swing high (H1)
          hl      — most recent swing low AFTER last_sh (the pullback / HL)

        Bearish N state:
          last_sl — most recent confirmed swing low (L1)
          lh      — most recent swing high AFTER last_sl (the bounce / LH)

        Signal fires when the N structure becomes ARMED (HL or LH just confirmed),
        placing a stop-entry order AT the breakout level (H1 or L1).
        cancel_level = hl (long) / lh (short): if price breaks this level the setup
        is invalidated and the pending order is cancelled.
        """
        sh_arr         = df["_sh"].to_list()
        sl_arr         = df["_sl"].to_list()
        close          = df["close"].to_list()
        high           = df["high"].to_list()
        low            = df["low"].to_list()
        ema            = df["ema"].to_list()
        ema_fast       = df["_ema_fast"].to_list()
        trend_ok_long  = df["_trend_ok_long"].to_list()
        trend_ok_short = df["_trend_ok_short"].to_list()

        n_bars         = len(close)

        if self.ema_filter_mode == "none":
            ema_ok_long_arr  = [True] * n_bars
            ema_ok_short_arr = [True] * n_bars
        elif self.ema_filter_mode == "dual":
            ema_ok_long_arr  = [ef is not None and es is not None and ef > es for ef, es in zip(ema_fast, ema)]
            ema_ok_short_arr = [ef is not None and es is not None and ef < es for ef, es in zip(ema_fast, ema)]
        else:  # "single"
            ema_ok_long_arr  = [es is not None and c > es for c, es in zip(close, ema)]
            ema_ok_short_arr = [es is not None and c < es for c, es in zip(close, ema)]
        signals        = [0]    * n_bars
        sl_out         = [None] * n_bars
        tp_out         = [None] * n_bars
        entry_stops    = [None] * n_bars
        cancel_levels  = [None] * n_bars

        last_sh: float | None = None   # H1 of bullish N
        hl:      float | None = None   # pullback low after H1
        last_sl: float | None = None   # L1 of bearish N
        lh:      float | None = None   # bounce high after L1

        for i in range(n_bars):
            c = close[i]

            long_just_armed  = False
            short_just_armed = False

            # ── Update swing-point state ──────────────────────────────────

            if sh_arr[i] is not None:
                sh_p    = sh_arr[i]
                last_sh = sh_p
                hl      = None                          # need fresh HL after new H1
                if last_sl is not None and sh_p > last_sl:
                    lh              = sh_p              # this SH is the LH for short N
                    short_just_armed = True

            if sl_arr[i] is not None:
                sl_p    = sl_arr[i]
                last_sl = sl_p
                lh      = None                          # need fresh LH after new L1
                short_just_armed = False                # lh reset, short arm cancelled
                if last_sh is not None and sl_p < last_sh:
                    hl              = sl_p              # this SL is the HL for long N
                    long_just_armed = True

            # ── Arm bullish stop-buy at H1 when HL is confirmed ──────────
            if (long_just_armed
                    and last_sh is not None
                    and hl is not None
                    and c < last_sh                     # breakout hasn't happened yet
                    and ema_ok_long_arr[i]
                    and trend_ok_long[i]
                    and self.trade_direction != "short_only"):

                entry_stop = last_sh

                if self.long_sl_tp_mode == "pips":
                    sl_d = self.long_sl_pips / self.pip_mult
                    tp_d = self.long_tp_pips / self.pip_mult
                    signals[i]     = 1
                    entry_stops[i] = entry_stop
                    sl_out[i]      = entry_stop - sl_d
                    tp_out[i]      = entry_stop + tp_d
                    if self.pending_cancel in ("hl_break", "both"):
                        cancel_levels[i] = hl
                else:
                    if self.long_sl_mode == "swing_midpoint":
                        sl_price = (last_sh + hl) / 2.0
                    elif self.long_sl_mode == "swing_point":
                        sl_price = hl
                    else:                               # signal_candle
                        sl_price = low[i]

                    dist = entry_stop - sl_price
                    if dist > 0:
                        signals[i]     = 1
                        entry_stops[i] = entry_stop
                        sl_out[i]      = sl_price
                        tp_out[i]      = entry_stop + self.long_rr_ratio * dist
                        if self.pending_cancel in ("hl_break", "both"):
                            cancel_levels[i] = hl       # cancel if price breaks below HL

            # ── Arm bearish stop-sell at L1 when LH is confirmed ─────────
            elif (short_just_armed
                    and last_sl is not None
                    and lh is not None
                    and c > last_sl                     # breakdown hasn't happened yet
                    and ema_ok_short_arr[i]
                    and trend_ok_short[i]
                    and self.trade_direction != "long_only"):

                entry_stop = last_sl

                if self.short_sl_tp_mode == "pips":
                    sl_d = self.short_sl_pips / self.pip_mult
                    tp_d = self.short_tp_pips / self.pip_mult
                    signals[i]     = -1
                    entry_stops[i] = entry_stop
                    sl_out[i]      = entry_stop + sl_d
                    tp_out[i]      = entry_stop - tp_d
                    if self.pending_cancel in ("hl_break", "both"):
                        cancel_levels[i] = lh
                else:
                    if self.short_sl_mode == "swing_midpoint":
                        sl_price = (last_sl + lh) / 2.0
                    elif self.short_sl_mode == "swing_point":
                        sl_price = lh
                    else:                               # signal_candle
                        sl_price = high[i]

                    dist = sl_price - entry_stop
                    if dist > 0:
                        signals[i]     = -1
                        entry_stops[i] = entry_stop
                        sl_out[i]      = sl_price
                        tp_out[i]      = entry_stop - self.short_rr_ratio * dist
                        if self.pending_cancel in ("hl_break", "both"):
                            cancel_levels[i] = lh       # cancel if price breaks above LH

        return signals, sl_out, tp_out, entry_stops, cancel_levels
