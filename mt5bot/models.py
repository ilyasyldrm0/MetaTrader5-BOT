"""Plain data types shared by the strategies, the brokers and the engine.

These are deliberately broker-agnostic: nothing here imports MetaTrader5, which
is what lets the strategy, the risk maths and the engine be tested on any
operating system.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum, StrEnum


class Timeframe(Enum):
    """Chart period, expressed as its length in minutes.

    The bot never touches ``mt5.TIMEFRAME_*`` outside the live adapter, which
    maps these members onto the MetaTrader constants by name. That is what
    keeps the engine, the strategy and the backtester importable without the
    Windows-only MetaTrader5 package installed.
    """

    M1 = 1
    M5 = 5
    M15 = 15
    M30 = 30
    H1 = 60
    H4 = 240
    D1 = 1440

    @property
    def minutes(self) -> int:
        return self.value

    @property
    def seconds(self) -> int:
        return self.value * 60

    @classmethod
    def parse(cls, value: str) -> Timeframe:
        """Look a timeframe up by name, case-insensitively (``"m15"``)."""
        try:
            return cls[value.strip().upper()]
        except KeyError:
            valid = ", ".join(m.name for m in cls)
            raise ValueError(f"unknown timeframe {value!r}; expected one of: {valid}") from None


class Side(StrEnum):
    """Direction of a position."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        """``+1`` for a long, ``-1`` for a short.

        Lets price maths be written once instead of branching on direction:
        a take profit is always ``entry + sign * distance``.
        """
        return 1 if self is Side.BUY else -1


@dataclass(frozen=True, slots=True)
class Signal:
    """A strategy's decision for one closed bar.

    ``reason`` is carried all the way into the trade journal so a row in the
    CSV can be traced back to the exact condition that produced it.
    """

    side: Side
    reason: str


@dataclass(frozen=True, slots=True)
class Bar:
    """One completed OHLC candle."""

    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True, slots=True)
class Tick:
    """The current best bid/ask."""

    time: datetime
    bid: float
    ask: float

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def price_for(self, side: Side) -> float:
        """The price a market order of ``side`` would actually be filled at.

        You buy at the ask and sell at the bid. The original script sent the
        last *bar close* as the order price for both directions, which a live
        broker answers with a requote or an invalid-price rejection.
        """
        return self.ask if side is Side.BUY else self.bid


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    """Everything about a symbol that affects order construction.

    Read from the broker rather than assumed. Hard-coding any of these is how
    orders get rejected on a broker whose contract differs from the one the
    author happened to test against.
    """

    symbol: str
    digits: int
    point: float
    volume_min: float
    volume_max: float
    volume_step: float
    trade_tick_value: float
    trade_tick_size: float
    #: Minimum distance in points that a stop loss / take profit must keep from
    #: the current price. Zero on many brokers, but not all.
    trade_stops_level: int = 0
    #: Bitmask of the fill policies the broker accepts for this symbol.
    filling_mode: int = 0
    contract_size: float = 100_000.0


@dataclass(frozen=True, slots=True)
class Position:
    """An open position, as reported by the broker."""

    ticket: int
    symbol: str
    side: Side
    volume: float
    price_open: float
    sl: float | None = None
    tp: float | None = None
    profit: float = 0.0
    magic: int = 0
    time: datetime | None = None
    comment: str = ""


@dataclass(frozen=True, slots=True)
class Account:
    """Account state used for position sizing and reporting."""

    login: int
    balance: float
    equity: float
    margin_free: float
    currency: str = "USD"
    leverage: int = 0
    server: str = ""


@dataclass(frozen=True, slots=True)
class OrderResult:
    """Uniform outcome of an order request, from any broker.

    The live adapter maps MetaTrader's ``retcode`` onto this; the paper broker
    fills it in directly. Callers check :attr:`ok` and never a raw retcode, so
    the engine has no MetaTrader constants in it.
    """

    ok: bool
    comment: str = ""
    retcode: int | None = None
    ticket: int | None = None
    price: float | None = None
    volume: float | None = None

    @classmethod
    def failure(cls, comment: str, retcode: int | None = None) -> OrderResult:
        return cls(ok=False, comment=comment, retcode=retcode)


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """A round trip, recorded once the position is gone.

    Produced by the paper broker and by the engine's reconciliation step, so a
    backtest and a live run write the same journal schema.
    """

    ticket: int
    symbol: str
    side: Side
    volume: float
    price_open: float
    price_close: float
    time_open: datetime
    time_close: datetime
    profit: float
    #: ``"tp"``, ``"sl"``, ``"signal"`` or ``"shutdown"``.
    exit_reason: str
    entry_reason: str = ""
