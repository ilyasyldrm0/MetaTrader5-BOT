"""Broker adapters.

:mod:`mt5bot.brokers.base` defines the interface; :mod:`mt5bot.brokers.mt5`
talks to a real MetaTrader 5 terminal and :mod:`mt5bot.brokers.paper`
simulates one in memory.
"""

from mt5bot.brokers.base import Broker

__all__ = ["Broker"]
