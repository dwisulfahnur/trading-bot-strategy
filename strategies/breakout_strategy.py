"""
Market Structure Breakout Strategy.

Rules
-----
- Swing highs/lows are identified using swing_n bars on each side (fractal method).
- Bullish market structure : Higher Highs (HH) + Higher Lows (HL).
- Bearish market structure : Lower Lows  (LL) + Lower Highs (LH).
- BUY  signal : close breaks above the last confirmed swing high (Break of Structure up)
                while a valid Higher Low is established as structural support.
- SELL signal : close breaks below the last confirmed swing low (Break of Structure down)
                while a valid Lower High is established as structural resistance.
- EMA trend filter (ema_filter_mode):
    "none"   → no filter, BOS signals in both directions.
    "single" → long only when close > slow EMA, short only when close < slow EMA.
    "dual"   → long only when fast EMA > slow EMA, short only when fast EMA < slow EMA.
- Entry  : next bar's open (no lookahead).
- SL (structure)     : last Higher Low for longs / last Lower High for shorts.
- SL (signal_candle) : low of signal candle for longs / high for shorts.
- TP : entry ± rr_ratio × (entry − SL).

Sideways Filter (optional) — same machinery as other strategies.
The slow EMA (ema_period) column is also used by the ema_slope sideways filter.
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


class MarketStructureBreakoutStrategy(BaseStrategy):
    name = "breakout_strategy"

    def __init__(
        self,
        ema_period: int = 200,
        ema_fast_period: int = 50,
        ema_timeframe: str = "same",
        symbol: str = "XAUUSD",
        swing_n_before: int = 5,
        swing_n_after: int = 5,
        trade_direction: str = "both",
        # Long (buy) SL/TP
        long_sl_tp_mode: str = "rr",
        long_rr_ratio: float = 2.0,
        long_sl_mode: str = "structure",    # structure | signal_candle
        long_sl_pips: float = 200.0,
        long_tp_pips: float = 400.0,
        # Short (sell) SL/TP
        short_sl_tp_mode: str = "rr",
        short_rr_ratio: float = 2.0,
        short_sl_mode: str = "structure",
        short_sl_pips: float = 200.0,
        short_tp_pips: float = 400.0,
        ema_filter_mode: str = "dual",
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
        self.symbol = symbol
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
        self.pip_mult = _PIP_MULT.get(symbol, 10.0)
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

        signals, sl_prices, tp_prices = self._scan_bos(df)

        df = df.with_columns([
            pl.Series("signal", signals,   dtype=pl.Int8),
            pl.Series("sl",     sl_prices, dtype=pl.Float64),
            pl.Series("tp",     tp_prices, dtype=pl.Float64),
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
        """Confirmed swing high shifted n_after bars to avoid lookahead."""
        mask = pl.lit(True)
        for j in range(1, n_before + 1):
            mask = mask & (pl.col("high") > pl.col("high").shift(j))
        for j in range(1, n_after + 1):
            mask = mask & (pl.col("high") > pl.col("high").shift(-j))
        confirmed = mask.shift(n_after)
        return pl.when(confirmed).then(pl.col("high").shift(n_after)).otherwise(None)

    @staticmethod
    def _swing_low_price(n_before: int, n_after: int) -> pl.Expr:
        """Confirmed swing low shifted n_after bars to avoid lookahead."""
        mask = pl.lit(True)
        for j in range(1, n_before + 1):
            mask = mask & (pl.col("low") < pl.col("low").shift(j))
        for j in range(1, n_after + 1):
            mask = mask & (pl.col("low") < pl.col("low").shift(-j))
        confirmed = mask.shift(n_after)
        return pl.when(confirmed).then(pl.col("low").shift(n_after)).otherwise(None)

    def _scan_bos(self, df: pl.DataFrame) -> tuple[list, list, list]:
        """
        Bar-by-bar scan for Break of Structure signals.

        Bullish BOS : close crosses above the last confirmed swing high
                      while a Higher Low (hl_level) is established.
        Bearish BOS : close crosses below the last confirmed swing low
                      while a Lower High (lh_level) is established.

        hl_level is consumed after a long BOS fires (requires new HH+HL to rearm).
        lh_level is consumed after a short BOS fires (requires new LL+LH to rearm).
        """
        sh_arr         = df["_sh"].to_list()
        sl_arr         = df["_sl"].to_list()
        close          = df["close"].to_list()
        high           = df["high"].to_list()
        low            = df["low"].to_list()
        ema_slow       = df["ema"].to_list()
        ema_fast       = df["_ema_fast"].to_list()
        trend_ok_long  = df["_trend_ok_long"].to_list()
        trend_ok_short = df["_trend_ok_short"].to_list()

        n_bars  = len(close)
        signals = [0]    * n_bars
        sl_out  = [None] * n_bars
        tp_out  = [None] * n_bars

        last_sh: float | None = None   # most recent confirmed swing high
        last_sl: float | None = None   # most recent confirmed swing low
        hl_level: float | None = None  # last Higher Low → SL for long BOS
        lh_level: float | None = None  # last Lower High → SL for short BOS
        prev_close: float | None = None

        for i in range(n_bars):
            c = close[i]

            # ── Update market structure from newly confirmed swing points ──

            if sh_arr[i] is not None:
                sh_p = sh_arr[i]
                # Higher High → record the last swing low as the Higher Low
                if last_sh is not None and sh_p > last_sh and last_sl is not None:
                    hl_level = last_sl
                last_sh = sh_p

            if sl_arr[i] is not None:
                sl_p = sl_arr[i]
                # Lower Low → record the last swing high as the Lower High
                if last_sl is not None and sl_p < last_sl and last_sh is not None:
                    lh_level = last_sh
                last_sl = sl_p

            # ── EMA trend filter ──────────────────────────────────────────

            eslow = ema_slow[i]
            efast = ema_fast[i]
            if self.ema_filter_mode == "single":
                ema_ok_long  = eslow is not None and c > eslow
                ema_ok_short = eslow is not None and c < eslow
            elif self.ema_filter_mode == "dual":
                ema_ok_long  = eslow is not None and efast is not None and efast > eslow
                ema_ok_short = eslow is not None and efast is not None and efast < eslow
            else:  # "none"
                ema_ok_long  = True
                ema_ok_short = True

            # ── Break of Structure — Long ─────────────────────────────────

            if (last_sh is not None
                    and hl_level is not None
                    and c > last_sh
                    and (prev_close is None or prev_close <= last_sh)
                    and ema_ok_long
                    and trend_ok_long[i]
                    and self.trade_direction != "short_only"):

                if self.long_sl_tp_mode == "pips":
                    sl_d = self.long_sl_pips / self.pip_mult
                    tp_d = self.long_tp_pips / self.pip_mult
                    signals[i] = 1
                    sl_out[i]  = c - sl_d
                    tp_out[i]  = c + tp_d
                    hl_level   = None
                else:
                    sl_price = hl_level if self.long_sl_mode == "structure" else low[i]
                    dist = c - sl_price
                    if dist > 0:
                        signals[i] = 1
                        sl_out[i]  = sl_price
                        tp_out[i]  = c + self.long_rr_ratio * dist
                        hl_level   = None  # consumed — rearms on next HH+HL

            # ── Break of Structure — Short ────────────────────────────────

            elif (last_sl is not None
                    and lh_level is not None
                    and c < last_sl
                    and (prev_close is None or prev_close >= last_sl)
                    and ema_ok_short
                    and trend_ok_short[i]
                    and self.trade_direction != "long_only"):

                if self.short_sl_tp_mode == "pips":
                    sl_d = self.short_sl_pips / self.pip_mult
                    tp_d = self.short_tp_pips / self.pip_mult
                    signals[i] = -1
                    sl_out[i]  = c + sl_d
                    tp_out[i]  = c - tp_d
                    lh_level   = None
                else:
                    sl_price = lh_level if self.short_sl_mode == "structure" else high[i]
                    dist = sl_price - c
                    if dist > 0:
                        signals[i] = -1
                        sl_out[i]  = sl_price
                        tp_out[i]  = c - self.short_rr_ratio * dist
                        lh_level   = None  # consumed — rearms on next LL+LH

            prev_close = c

        return signals, sl_out, tp_out
