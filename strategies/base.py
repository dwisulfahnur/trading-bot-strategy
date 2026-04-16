"""
Abstract base class for all backtest strategies.

Each strategy receives an OHLCV Polars DataFrame and must return it
with three additional columns:
  - signal  : int  →  1 (buy), -1 (sell), 0 (no signal)
  - sl      : f64  →  stop-loss price for the signal bar
  - tp      : f64  →  take-profit price for the signal bar
"""

from abc import ABC, abstractmethod

import polars as pl


class BaseStrategy(ABC):
    name: str = "base"

    @abstractmethod
    def generate_signals(self, df: pl.DataFrame) -> pl.DataFrame:
        """
        Parameters
        ----------
        df : pl.DataFrame
            OHLCV data with columns [time, open, high, low, close, ticks].

        Returns
        -------
        pl.DataFrame
            Same dataframe with appended columns: signal, sl, tp.
        """
        ...
