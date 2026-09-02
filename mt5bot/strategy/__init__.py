"""Trading strategies.

:class:`~mt5bot.strategy.base.Strategy` is the interface the engine and the
backtester both drive, so anything implementing it can be replayed over
history before it is given money.
"""

from mt5bot.strategy.base import Evaluation, Strategy
from mt5bot.strategy.rsi_sma import RsiSmaStrategy, TrendFilter

__all__ = ["Evaluation", "RsiSmaStrategy", "Strategy", "TrendFilter"]
