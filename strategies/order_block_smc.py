"""
Order Block (SMC) Strategy

How it works
------------
1. Structure break (BOS): price closes above the rolling N-bar high (bullish) or
   below the rolling N-bar low (bearish).  `structure_period` sets N — it defines
   how far back the strategy looks to establish a significant high/low to break.

2. Order Block: the last opposing candle (bearish for longs, bullish for shorts)
   within `ob_lookback` bars immediately before the BOS candle.  This is where
   institutional orders are assumed to have accumulated before the impulse.

3. Entry: limit order placed at the OB edge — OB high for longs, OB low for
   shorts.  The engine fills on a later bar when price retraces into the zone.
   If no OB candle is found within the lookback window, no signal is emitted.

4. Stop-loss placement (sl_mode):
   - "ob_edge"     : SL at OB low (longs) / OB high (shorts)  [default]
   - "ob_midpoint" : SL at the 50% level of the OB candle (tighter)
   - "structure"   : SL below the BOS swing low / above BOS swing high (wider)
5. Take-profit: entry ± rr_ratio × sl_distance  (scales with chosen SL mode)

Optional filters
----------------
require_fvg  : bullish/bearish Fair Value Gap (3-candle imbalance) must be
               present within ob_lookback bars before the BOS.
require_ote  : the OB entry level must fall within the Fibonacci OTE zone
               (default 61.8%–78.6% of the BOS impulse range).

Session filter
--------------
sessions = "all" → no restriction  |  "london" / "newyork" / etc.
"""

import numpy as np
import polars as pl

from strategies.base import BaseStrategy


class OrderBlockSMCStrategy(BaseStrategy):
    name = "order_block_smc"

    def __init__(
        self,
        structure_period: int = 20,    # rolling lookback to define the high/low that gets broken
        ob_lookback: int = 5,          # bars before BOS to search for the OB candle
        rr_ratio: float = 2.0,         # take-profit = rr_ratio × OB height
        require_fvg: bool = False,     # require Fair Value Gap confluence
        require_ote: bool = False,     # require OB entry within OTE Fibonacci zone
        ote_fib_low: float = 0.618,    # OTE zone lower boundary (shown when require_ote=True)
        ote_fib_high: float = 0.786,   # OTE zone upper boundary (shown when require_ote=True)
        sl_mode: str = "ob_edge",      # "ob_edge" | "ob_midpoint" | "structure"
        sessions: str = "all",
    ) -> None:
        self.structure_period = structure_period
        self.ob_lookback = ob_lookback
        self.rr_ratio = rr_ratio
        self.require_fvg = require_fvg
        self.require_ote = require_ote
        self.ote_fib_low = ote_fib_low
        self.ote_fib_high = ote_fib_high
        self.sl_mode = sl_mode
        self.sessions = sessions

    # ──────────────────────────────────────────────────────────────────────
    # Public
    # ──────────────────────────────────────────────────────────────────────

    def generate_signals(self, df: pl.DataFrame) -> pl.DataFrame:
        p = self.structure_period

        # ── Step 1: rolling structure levels (vectorized) ──────────────────
        # recent_high / recent_low = highest high / lowest low over the prior p bars
        # .shift(1) ensures we only use completed bars → no lookahead bias
        df = df.with_columns([
            pl.col("high").shift(1).rolling_max(window_size=p).alias("_recent_high"),
            pl.col("low").shift(1).rolling_min(window_size=p).alias("_recent_low"),
        ])

        # ── Step 2: BOS detection (vectorized) ────────────────────────────
        # Bullish BOS: close breaks above rolling N-bar high
        # Bearish BOS: close breaks below rolling N-bar low
        df = df.with_columns([
            (
                pl.col("_recent_high").is_not_null()
                & (pl.col("close") > pl.col("_recent_high"))
                & (pl.col("close").shift(1) <= pl.col("_recent_high").shift(1))
            ).fill_null(False).alias("_bos_long"),
            (
                pl.col("_recent_low").is_not_null()
                & (pl.col("close") < pl.col("_recent_low"))
                & (pl.col("close").shift(1) >= pl.col("_recent_low").shift(1))
            ).fill_null(False).alias("_bos_short"),
        ])

        # ── Step 3: Fair Value Gap detection (vectorized) ─────────────────
        # Bullish FVG: low[i] > high[i-2]  (upward 3-candle gap)
        # Bearish FVG: high[i] < low[i-2]  (downward 3-candle gap)
        df = df.with_columns([
            (pl.col("low") > pl.col("high").shift(2)).fill_null(False).alias("_fvg_bull"),
            (pl.col("high") < pl.col("low").shift(2)).fill_null(False).alias("_fvg_bear"),
        ])

        # ── Step 4: state machine (numpy) ─────────────────────────────────
        signals, sl_arr, tp_arr, entry_limit_arr = self._run_state_machine(df)

        # ── Step 5: attach output columns ─────────────────────────────────
        df = df.with_columns([
            pl.Series("signal",      signals,         dtype=pl.Int8),
            pl.Series("sl",          sl_arr,          dtype=pl.Float64),
            pl.Series("tp",          tp_arr,          dtype=pl.Float64),
            pl.Series("entry_limit", entry_limit_arr, dtype=pl.Float64),
        ])

        # ── Step 6: drop temp columns ──────────────────────────────────────
        df = df.drop(["_recent_high", "_recent_low", "_bos_long", "_bos_short",
                      "_fvg_bull", "_fvg_bear"])

        return self._apply_session_filter(df, self.sessions)

    # ──────────────────────────────────────────────────────────────────────
    # State machine
    # ──────────────────────────────────────────────────────────────────────

    def _run_state_machine(
        self, df: pl.DataFrame
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        hi = df["high"].to_numpy(allow_copy=True).astype(np.float64)
        lo = df["low"].to_numpy(allow_copy=True).astype(np.float64)
        op = df["open"].to_numpy(allow_copy=True).astype(np.float64)
        cl = df["close"].to_numpy(allow_copy=True).astype(np.float64)

        bos_long  = df["_bos_long"].to_numpy(allow_copy=True).astype(bool)
        bos_short = df["_bos_short"].to_numpy(allow_copy=True).astype(bool)
        fvg_bull  = df["_fvg_bull"].to_numpy(allow_copy=True).astype(bool)
        fvg_bear  = df["_fvg_bear"].to_numpy(allow_copy=True).astype(bool)

        # Rolling structure levels as float64 (NaN before enough history)
        rh_raw = df["_recent_high"].to_numpy(allow_copy=True)
        rl_raw = df["_recent_low"].to_numpy(allow_copy=True)
        recent_high = np.where(rh_raw == None, np.nan, rh_raw).astype(np.float64)  # noqa: E711
        recent_low  = np.where(rl_raw == None, np.nan, rl_raw).astype(np.float64)  # noqa: E711

        n = len(hi)
        signals     = np.zeros(n, dtype=np.int8)
        sl_arr      = np.full(n, np.nan, dtype=np.float64)
        tp_arr      = np.full(n, np.nan, dtype=np.float64)
        entry_limit = np.full(n, np.nan, dtype=np.float64)

        for i in range(2, n):
            if bos_long[i]:
                result = self._eval_long(
                    i, hi, lo, op, cl, fvg_bull,
                    recent_high[i], recent_low[i],
                )
                if result is not None:
                    el, s, t = result
                    signals[i]     = 1
                    entry_limit[i] = el
                    sl_arr[i]      = s
                    tp_arr[i]      = t

            elif bos_short[i]:
                result = self._eval_short(
                    i, hi, lo, op, cl, fvg_bear,
                    recent_high[i], recent_low[i],
                )
                if result is not None:
                    el, s, t = result
                    signals[i]     = -1
                    entry_limit[i] = el
                    sl_arr[i]      = s
                    tp_arr[i]      = t

        return signals, sl_arr, tp_arr, entry_limit

    # ──────────────────────────────────────────────────────────────────────
    # Per-BOS evaluators
    # ──────────────────────────────────────────────────────────────────────

    def _eval_long(self, i, hi, lo, op, cl, fvg_bull, recent_high, recent_low):
        """
        Bullish BOS at bar i.
        Find the last bearish OB candle; apply optional FVG / OTE filters.
        Returns (entry_limit, sl, tp) or None.
        """
        ob_high, ob_low = self._find_ob_long(i, hi, lo, op, cl)
        if ob_high is None:
            return None

        if ob_high - ob_low <= 0:
            return None

        if self.require_fvg:
            look_from = max(0, i - self.ob_lookback)
            if not any(fvg_bull[look_from:i + 1]):
                return None

        if self.require_ote:
            if not self._in_ote_zone_long(ob_high, recent_low, cl[i]):
                return None

        entry = ob_high
        sl = self._calc_sl_long(ob_high, ob_low, recent_low)
        if sl is None or entry <= sl:
            return None
        sl_dist = entry - sl
        tp = entry + self.rr_ratio * sl_dist
        return entry, sl, tp

    def _eval_short(self, i, hi, lo, op, cl, fvg_bear, recent_high, recent_low):
        """
        Bearish BOS at bar i.
        Find the last bullish OB candle; apply optional FVG / OTE filters.
        Returns (entry_limit, sl, tp) or None.
        """
        ob_high, ob_low = self._find_ob_short(i, hi, lo, op, cl)
        if ob_high is None:
            return None

        if ob_high - ob_low <= 0:
            return None

        if self.require_fvg:
            look_from = max(0, i - self.ob_lookback)
            if not any(fvg_bear[look_from:i + 1]):
                return None

        if self.require_ote:
            if not self._in_ote_zone_short(ob_low, recent_high, cl[i]):
                return None

        entry = ob_low
        sl = self._calc_sl_short(ob_high, ob_low, recent_high)
        if sl is None or entry >= sl:
            return None
        sl_dist = sl - entry
        tp = entry - self.rr_ratio * sl_dist
        return entry, sl, tp

    # ──────────────────────────────────────────────────────────────────────
    # SL placement helpers
    # ──────────────────────────────────────────────────────────────────────

    def _calc_sl_long(self, ob_high, ob_low, recent_low) -> float | None:
        """Return stop-loss price for a long entry, based on sl_mode."""
        if self.sl_mode == "ob_edge":
            return ob_low
        if self.sl_mode == "ob_midpoint":
            return (ob_high + ob_low) / 2.0
        if self.sl_mode == "structure":
            if np.isnan(recent_low):
                return None
            return recent_low
        return ob_low  # fallback

    def _calc_sl_short(self, ob_high, ob_low, recent_high) -> float | None:
        """Return stop-loss price for a short entry, based on sl_mode."""
        if self.sl_mode == "ob_edge":
            return ob_high
        if self.sl_mode == "ob_midpoint":
            return (ob_high + ob_low) / 2.0
        if self.sl_mode == "structure":
            if np.isnan(recent_high):
                return None
            return recent_high
        return ob_high  # fallback

    # ──────────────────────────────────────────────────────────────────────
    # Order Block finders
    # ──────────────────────────────────────────────────────────────────────

    def _find_ob_long(self, bos_idx, hi, lo, op, cl):
        """Last bearish candle (close < open) within ob_lookback bars before bos_idx."""
        look_from = max(0, bos_idx - self.ob_lookback)
        for j in range(bos_idx - 1, look_from - 1, -1):
            if cl[j] < op[j]:
                return hi[j], lo[j]
        return None, None

    def _find_ob_short(self, bos_idx, hi, lo, op, cl):
        """Last bullish candle (close > open) within ob_lookback bars before bos_idx."""
        look_from = max(0, bos_idx - self.ob_lookback)
        for j in range(bos_idx - 1, look_from - 1, -1):
            if cl[j] > op[j]:
                return hi[j], lo[j]
        return None, None

    # ──────────────────────────────────────────────────────────────────────
    # OTE (Fibonacci) zone checks
    # ──────────────────────────────────────────────────────────────────────

    def _in_ote_zone_long(self, ob_high, recent_low, bos_close):
        """
        Bullish OTE: OB entry (ob_high) must be in the Fibonacci retracement
        of the impulse leg from recent_low up to the BOS close.
        OTE zone = [recent_low + fib_low×range, recent_low + fib_high×range].
        """
        if np.isnan(recent_low):
            return False
        impulse = bos_close - recent_low
        if impulse <= 0:
            return False
        lo_level = recent_low + self.ote_fib_low  * impulse
        hi_level = recent_low + self.ote_fib_high * impulse
        return lo_level <= ob_high <= hi_level

    def _in_ote_zone_short(self, ob_low, recent_high, bos_close):
        """
        Bearish OTE: OB entry (ob_low) must be in the Fibonacci retracement
        of the impulse leg from recent_high down to the BOS close.
        OTE zone = [recent_high - fib_high×range, recent_high - fib_low×range].
        """
        if np.isnan(recent_high):
            return False
        impulse = recent_high - bos_close
        if impulse <= 0:
            return False
        lo_level = recent_high - self.ote_fib_high * impulse
        hi_level = recent_high - self.ote_fib_low  * impulse
        return lo_level <= ob_low <= hi_level
