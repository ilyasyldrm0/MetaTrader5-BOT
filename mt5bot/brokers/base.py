"""The seam between the trading logic and a real broker.

Everything above this interface -- the strategy, the risk maths, the engine,
the backtester -- is pure Python and runs anywhere. Everything MetaTrader
specific lives below it, in :mod:`mt5bot.brokers.mt5`.

That split is not decoration. The ``MetaTrader5`` package is Windows-only, so
without it there would be no way to run a test, and the bugs this rewrite fixes
(a malformed close request, an order priced off a stale bar) are exactly the
kind that only a test catches.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from mt5bot.models import (
    Account,
    OrderResult,
    Position,
    Side,
    SymbolSpec,
    Tick,
    Timeframe,
)

#: Columns every :meth:`Broker.bars` implementation returns, in this order.
BAR_COLUMNS: tuple[str, ...] = ("time", "open", "high", "low", "close", "volume")


class Broker(ABC):
    """A venue the bot can read prices from and send orders to."""

    #: Identifies this bot's orders. Implementations tag every order with it
    #: and filter :meth:`positions` by it, so a bot never touches a position
    #: opened by hand or by another strategy on the same account.
    magic: int

    @property
    @abstractmethod
    def name(self) -> str:
        """Short label for logs, e.g. ``"MetaTrader5"`` or ``"paper"``."""

    # -- lifecycle ---------------------------------------------------------

    @abstractmethod
    def connect(self) -> None:
        """Establish the connection. Raises :class:`~mt5bot.errors.BrokerError`."""

    @abstractmethod
    def shutdown(self) -> None:
        """Release the connection. Must be safe to call twice."""

    def __enter__(self) -> Broker:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()

    # -- market data -------------------------------------------------------

    @abstractmethod
    def symbol_spec(self, symbol: str) -> SymbolSpec:
        """Contract details for ``symbol``."""

    @abstractmethod
    def bars(self, symbol: str, timeframe: Timeframe, count: int) -> pd.DataFrame:
        """The last ``count`` **closed** bars, oldest first.

        Excluding the forming bar is part of the contract, not a caller's
        responsibility. The original script read from position 0, which is the
        candle still being built, so its RSI and SMA changed on every poll and
        a signal could appear and vanish within the same 15 minutes.

        Returns a frame with :data:`BAR_COLUMNS`. Raises
        :class:`~mt5bot.errors.DataUnavailableError` if fewer than ``count`` bars
        are available.
        """

    @abstractmethod
    def tick(self, symbol: str) -> Tick:
        """The current bid/ask. Raises :class:`~mt5bot.errors.DataUnavailableError`."""

    @abstractmethod
    def account(self) -> Account:
        """Balance, equity and free margin."""

    # -- trading -----------------------------------------------------------

    @abstractmethod
    def positions(self, symbol: str | None = None) -> list[Position]:
        """This bot's open positions, filtered by :attr:`magic`.

        The engine calls this every cycle and treats the answer as the only
        truth about what is open. Keeping the position id in a variable, as the
        original did, goes stale the moment a stop loss fires.
        """

    @abstractmethod
    def open_position(
        self,
        symbol: str,
        side: Side,
        volume: float,
        *,
        sl: float | None = None,
        tp: float | None = None,
        comment: str = "",
    ) -> OrderResult:
        """Send a market order.

        ``sl`` and ``tp`` are absolute prices, already converted from pips and
        normalised by :mod:`mt5bot.risk`. Implementations never raise on a
        rejection -- they return an :class:`~mt5bot.models.OrderResult` with
        ``ok=False`` so the caller can log it and carry on.
        """

    @abstractmethod
    def close_position(self, position: Position, *, comment: str = "") -> OrderResult:
        """Close ``position`` at market."""
