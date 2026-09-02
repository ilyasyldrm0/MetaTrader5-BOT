"""The strategy interface.

A strategy is a pure function of recent bars: given history, say whether to be
long, short, or out. It knows nothing about brokers, order types, lot sizes or
money, which is what lets the same object run in a backtest and against a live
account without a line of difference between the two.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field

import pandas as pd

from mt5bot.models import Signal


@dataclass(frozen=True, slots=True)
class Evaluation:
    """What a strategy concluded from one closed bar.

    ``indicators`` carries the readings behind the decision. The engine logs
    them every bar and the journal stores them alongside the trade, so a
    surprising entry can be explained afterwards instead of re-derived from
    a chart -- the original printed the same three numbers and then threw
    them away.
    """

    signal: Signal | None
    indicators: Mapping[str, float] = field(default_factory=dict)


class Strategy(ABC):
    """Turns recent price history into a directional opinion."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short label for logs and reports."""

    @property
    @abstractmethod
    def min_bars(self) -> int:
        """Bars of history :meth:`evaluate` needs before it can decide.

        The engine requests exactly this many, so a strategy that lengthens
        its indicator periods automatically gets the longer warm-up it needs.
        Getting this wrong is quiet: too little history does not raise, it just
        returns a number computed mostly from the seed.
        """

    @abstractmethod
    def evaluate(self, bars: pd.DataFrame) -> Evaluation:
        """Assess the market as of the last row of ``bars``.

        ``bars`` holds at least :attr:`min_bars` **closed** candles, oldest
        first. Implementations must not look beyond the final row -- there is
        no future data in a live run, and a backtest that peeks is fiction.
        """

    def describe(self) -> str:
        """One line of configuration, logged at start-up."""
        return self.name
