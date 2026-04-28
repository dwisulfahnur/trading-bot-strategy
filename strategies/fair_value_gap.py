"""
Fair Value Gap (FVG) Strategy.

Rules
-----
1. Read market structure via swing highs/lows (swing_n_before/swing_n_after).
2. A Fair Value Gap is a 3-candle imbalance:
     Bullish FVG : high[i-2] < low[i]  (impulse candle[i-1] gapped up)
     Bearish FVG : low[i-2]  > high[i] (impulse candle[i-1] gapped down)
3. FVGs are tracked for up to `max_fvg_bars` bars.
4. Signal fires when price retraces into the FVG zone:
     Long  : low <= zone_high AND close >= zone_low AND close > EMA
     Short : high >= zone_low AND close <= zone_high AND close < EMA
5. Entry    : next bar's open after signal (no lookahead).
6. Stop-loss: controlled by `sl_mode` —
     swing_hl       : last Higher Low before the FVG impulse (long)
                      last Lower High before the FVG impulse (short)
     fvg_edge       : bottom of FVG zone (long) / top of FVG zone (short)
     signal_candle  : low of signal bar (long) / high of signal bar (short)
     impulse_candle : low of impulse bar (long) / high of impulse bar (short)
7. Take-profit: entry_ref ± rr_ratio × sl_distance

Sideways Filter (optional) — same machinery as other strategies.
"""

from pathlib import Path

import polars as pl

from strategies.base import BaseStrategy

_DATA_DIR = Path(__file__).parent.parent / "data" / "parquet" / "ohlcv"


class FairValueGapStrategy(BaseStrategy):
    name = "fair_value_gap"

    def __init__(
        self,
        ema_period: int = 200,
        ema_timeframe: str = "same",
        symbol: str = "XAUUSD",
        rr_ratio: float = 2.0,
        fvg_min_size: float = 0.0,
        min_sl_pips: float = 5.0,
        entry_mode: str = "zone_mid",
        sl_mode: str = "swing_hl",
        max_fvg_bars: int = 20,
        swing_n_before: int = 5,
        swing_n_after: int = 5,
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
        self.rr_ratio = rr_ratio
        self.fvg_min_size = fvg_min_size
        self.min_sl_pips = min_sl_pips
        self.entry_mode = entry_mode
        self.sl_mode = sl_mode
        self.max_fvg_bars = max_fvg_bars
        self.swing_n_before = swing_n_before
        self.swing_n_after = swing_n_after
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
            df = df.with_columns(
                pl.col("close").ewm_mean(span=self.ema_period, adjust=False).alias("ema")
            )
        else:
            df = self._load_htf_ema(df)

        df = self._add_sideways_filter(df)

        signals, sl_prices, tp_prices = self._scan_fvg(df)

        df = df.with_columns([
            pl.Series("signal", signals,   dtype=pl.Int8),
            pl.Series("sl",     sl_prices, dtype=pl.Float64),
            pl.Series("tp",     tp_prices, dtype=pl.Float64),
        ])

        df = df.drop(["_trend_ok_long", "_trend_ok_short"])

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

    def _compute_swings(self, df: pl.DataFrame) -> tuple[list, list]:
        """
        Returns (swing_highs, swing_lows).
        swing_highs[i] = high[i] if bar i is a confirmed pivot high, else None.
        swing_lows[i]  = low[i]  if bar i is a confirmed pivot low,  else None.
        """
        highs = df["high"].to_list()
        lows  = df["low"].to_list()
        n     = len(highs)
        nb    = self.swing_n_before
        na    = self.swing_n_after

        swing_highs: list = [None] * n
        swing_lows:  list = [None] * n

        for i in range(nb, n - na):
            if (all(highs[i] > highs[i - k] for k in range(1, nb + 1)) and
                    all(highs[i] > highs[i + k] for k in range(1, na + 1))):
                swing_highs[i] = highs[i]
            if (all(lows[i] < lows[i - k] for k in range(1, nb + 1)) and
                    all(lows[i] < lows[i + k] for k in range(1, na + 1))):
                swing_lows[i] = lows[i]

        return swing_highs, swing_lows

    def _scan_fvg(self, df: pl.DataFrame) -> tuple[list, list, list]:
        """
        Bar-by-bar scan for FVG entries.

        Each active FVG zone stores:
          direction    — 1 (bullish) or -1 (bearish)
          zone_low     — lower bound of the gap
          zone_high    — upper bound of the gap
          impulse_low  — low of the impulse (middle) candle
          impulse_high — high of the impulse (middle) candle
          age          — bars since FVG was detected
          created_at   — bar index where FVG was detected (skip on same bar)
          sl_anchor    — last swing low (bull) / last swing high (bear) at
                         the moment the FVG formed — used as structural SL
        """
        highs          = df["high"].to_list()
        lows           = df["low"].to_list()
        closes         = df["close"].to_list()
        emas           = df["ema"].to_list()
        trend_ok_long  = df["_trend_ok_long"].to_list()
        trend_ok_short = df["_trend_ok_short"].to_list()

        n_bars  = len(closes)
        signals = [0]    * n_bars
        sl_out  = [None] * n_bars
        tp_out  = [None] * n_bars

        swing_highs, swing_lows = self._compute_swings(df)
        last_swing_low  = None  # most recently confirmed swing low price
        last_swing_high = None  # most recently confirmed swing high price

        active_fvgs: list[dict] = []

        for i in range(2, n_bars):
            # Update structural swing trackers
            if swing_lows[i] is not None:
                last_swing_low = swing_lows[i]
            if swing_highs[i] is not None:
                last_swing_high = swing_highs[i]

            # ── Detect new FVGs formed by 3-candle pattern ending at bar i ──

            # Bullish FVG: high[i-2] < low[i]  (impulse = candle[i-1] gapped up)
            bull_gap = lows[i] - highs[i - 2]
            if bull_gap >= self.fvg_min_size and highs[i - 2] < lows[i]:
                active_fvgs.append({
                    "direction":    1,
                    "zone_low":     highs[i - 2],
                    "zone_high":    lows[i],
                    "impulse_low":  lows[i - 1],
                    "impulse_high": highs[i - 1],
                    "age":          0,
                    "created_at":   i,
                    "sl_anchor":    last_swing_low,   # last HL = structural SL for longs
                })

            # Bearish FVG: low[i-2] > high[i]  (impulse = candle[i-1] gapped down)
            bear_gap = lows[i - 2] - highs[i]
            if bear_gap >= self.fvg_min_size and lows[i - 2] > highs[i]:
                active_fvgs.append({
                    "direction":    -1,
                    "zone_low":     highs[i],
                    "zone_high":    lows[i - 2],
                    "impulse_low":  lows[i - 1],
                    "impulse_high": highs[i - 1],
                    "age":          0,
                    "created_at":   i,
                    "sl_anchor":    last_swing_high,  # last LH = structural SL for shorts
                })

            # ── Check active FVGs for price entry ──────────────────────────
            signal_fired = False
            to_remove: list[dict] = []

            for fvg in active_fvgs:
                if fvg["created_at"] == i:
                    continue  # wait at least 1 bar after FVG forms

                fvg["age"] += 1

                if fvg["age"] > self.max_fvg_bars:
                    to_remove.append(fvg)
                    continue

                if signal_fired:
                    continue

                zone_low  = fvg["zone_low"]
                zone_high = fvg["zone_high"]
                zone_mid  = (zone_low + zone_high) / 2.0

                if fvg["direction"] == 1:
                    # Price retraces into bullish FVG
                    entered = lows[i] <= zone_high and closes[i] >= zone_low
                    if entered and closes[i] > emas[i] and trend_ok_long[i]:
                        if self.entry_mode == "zone_top":
                            entry_ref = zone_high
                        elif self.entry_mode == "zone_bottom":
                            entry_ref = zone_low
                        else:
                            entry_ref = zone_mid

                        if self.sl_mode == "swing_hl":
                            # SL = last Higher Low (structural swing low before the FVG impulse)
                            sl = fvg["sl_anchor"]
                            if sl is None or sl >= entry_ref:
                                sl = fvg["impulse_low"]  # fallback: impulse candle low
                        elif self.sl_mode == "signal_candle":
                            sl = lows[i]
                        elif self.sl_mode == "impulse_candle":
                            sl = fvg["impulse_low"]
                        else:  # fvg_edge
                            sl = zone_low

                        sl_dist = entry_ref - sl
                        if sl_dist > 0 and sl_dist >= self.min_sl_pips:
                            signals[i] = 1
                            sl_out[i]  = sl
                            tp_out[i]  = entry_ref + self.rr_ratio * sl_dist
                            signal_fired = True
                            to_remove.append(fvg)

                elif fvg["direction"] == -1:
                    # Price retraces into bearish FVG
                    entered = highs[i] >= zone_low and closes[i] <= zone_high
                    if entered and closes[i] < emas[i] and trend_ok_short[i]:
                        if self.entry_mode == "zone_top":
                            entry_ref = zone_high
                        elif self.entry_mode == "zone_bottom":
                            entry_ref = zone_low
                        else:
                            entry_ref = zone_mid

                        if self.sl_mode == "swing_hl":
                            # SL = last Lower High (structural swing high before the FVG impulse)
                            sl = fvg["sl_anchor"]
                            if sl is None or sl <= entry_ref:
                                sl = fvg["impulse_high"]  # fallback: impulse candle high
                        elif self.sl_mode == "signal_candle":
                            sl = highs[i]
                        elif self.sl_mode == "impulse_candle":
                            sl = fvg["impulse_high"]
                        else:  # fvg_edge
                            sl = zone_high

                        sl_dist = sl - entry_ref
                        if sl_dist > 0 and sl_dist >= self.min_sl_pips:
                            signals[i] = -1
                            sl_out[i]  = sl
                            tp_out[i]  = entry_ref - self.rr_ratio * sl_dist
                            signal_fired = True
                            to_remove.append(fvg)

            for fvg in to_remove:
                if fvg in active_fvgs:
                    active_fvgs.remove(fvg)

        return signals, sl_out, tp_out
