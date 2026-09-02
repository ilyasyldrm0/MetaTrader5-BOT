"""Simulated execution: a paper broker and a dry-run wrapper.

Two things live here, and they answer different questions.

:class:`PaperBroker` is driven by a bar feed. It is what the backtester runs
on and what ``run --replay`` runs on, so a strategy can be measured on any
machine, with no terminal and no account.

:class:`DryRunBroker` wraps a *real* broker. Prices, spreads and symbol
specifications are genuine; only the orders are withheld. It is the default
for ``run``, so pointing the bot at a live account and forgetting a flag
cannot cost money.

Both model the spread the way MetaTrader does: **bar prices are bid prices**,
and the ask sits one spread above. That asymmetry is not a detail. A long pays
the spread on entry and exits on the bid, a short does the reverse, and a
simulator that ignores it reports profits that vanish the moment the strategy
meets a real quote.
"""

from __future__ import annotations

import itertools
import logging
from datetime import datetime, timedelta

import pandas as pd

from mt5bot.brokers.base import BAR_COLUMNS, Broker
from mt5bot.errors import BrokerError, DataUnavailableError
from mt5bot.models import (
    Account,
    ClosedTrade,
    OrderResult,
    Position,
    Side,
    SymbolSpec,
    Tick,
    Timeframe,
)
from mt5bot.risk import pips_to_price, profit_of

log = logging.getLogger(__name__)


def _reached(side: Side, level: float, reason: str, price: float) -> bool:
    """Whether ``price`` has travelled far enough to trigger ``level``.

    A stop loss is reached by moving against the position, a take profit by
    moving with it, so the only thing that changes between the four cases is
    the sign of the direction. Writing it once removes the four near-identical
    comparisons that are so easy to get subtly backwards -- the original script
    had exactly that kind of mirrored duplication in its buy and sell blocks.
    """
    direction = -side.sign if reason == "sl" else side.sign
    return (price - level) * direction >= 0


class PaperBroker(Broker):
    """A broker backed by a table of historical bars.

    The feed is consumed one bar per :meth:`on_cycle`, which is also where
    stops are filled -- before the engine sees the bar. A position opened at
    the close of bar *i* is first tested against bar *i+1*, so the simulation
    can never act on a price the strategy had not yet been shown.
    """

    def __init__(
        self,
        feed: pd.DataFrame,
        spec: SymbolSpec,
        *,
        balance: float = 10_000.0,
        spread_pips: float = 1.0,
        slippage_pips: float = 0.0,
        commission_per_lot: float = 0.0,
        magic: int = 234000,
        warmup: int = 0,
    ) -> None:
        missing = [c for c in BAR_COLUMNS if c not in feed.columns]
        if missing:
            raise BrokerError(f"feed is missing required columns: {', '.join(missing)}")
        if len(feed) < 2:
            raise BrokerError(f"feed has {len(feed)} bars; need at least 2")

        # Normalised once, so every later access is a plain slice: selecting
        # columns and rebuilding a row Series on each of several thousand
        # cycles was a large share of a backtest's runtime.
        self._feed = feed.loc[:, list(BAR_COLUMNS)].reset_index(drop=True)
        self.timeframe = infer_timeframe(self._feed)
        self._times = self._feed["time"].to_numpy()
        self._opens = self._feed["open"].to_numpy(dtype=float)
        self._highs = self._feed["high"].to_numpy(dtype=float)
        self._lows = self._feed["low"].to_numpy(dtype=float)
        self._closes = self._feed["close"].to_numpy(dtype=float)
        self.spec = spec
        self.magic = magic
        self.starting_balance = balance
        self.balance = balance
        self._spread = pips_to_price(spread_pips, spec)
        self._slippage = pips_to_price(slippage_pips, spec)
        self._commission_per_lot = commission_per_lot

        # Start with enough history for the indicators to have warmed up,
        # rather than handing the strategy a frame full of NaN.
        self._i = max(0, min(warmup, len(self._feed) - 1))
        self._open: Position | None = None
        self._entry_time: datetime | None = None
        self._entry_reason = ""
        self._tickets = itertools.count(1)
        #: Every trade, kept for reporting at the end of a run.
        self.closed_trades: list[ClosedTrade] = []
        #: The same trades, but cleared each time the engine collects them.
        self._undrained: list[ClosedTrade] = []

    @property
    def name(self) -> str:
        return "paper"

    def connect(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    # -- simulated clock ---------------------------------------------------

    @property
    def index(self) -> int:
        """Position in the feed of the most recently closed bar."""
        return self._i

    @property
    def finished(self) -> bool:
        return self._i >= len(self._feed) - 1

    def on_cycle(self) -> bool:
        """Close the next bar, filling any stop its range touched."""
        if self.finished:
            return False
        self._i += 1
        self._apply_stops(self._i)
        return True

    def _apply_stops(self, index: int) -> None:
        """Fill a stop loss or take profit that this bar's range reached.

        The stop is checked first, so a bar that touches both takes the loss.
        Bars carry no information about the order prices were visited in, and
        assuming the profitable one came first is how a backtest flatters a
        strategy into looking tradeable.
        """
        position = self._open
        if position is None:
            return

        # A long closes at the bid, which is what the bars quote; a short
        # closes at the ask, one spread above every bar price.
        offset = 0.0 if position.side is Side.BUY else self._spread
        bar_open = self._opens[index] + offset
        high = self._highs[index] + offset
        low = self._lows[index] + offset

        for level, reason in ((position.sl, "sl"), (position.tp, "tp")):
            if level is None:
                continue
            direction = -position.side.sign if reason == "sl" else position.side.sign
            extreme = high if direction > 0 else low
            if not _reached(position.side, level, reason, extreme):
                continue
            # If the bar opened beyond the level, the gap is the fill. A stop
            # does not hold a price the market jumped straight over.
            gapped = _reached(position.side, level, reason, bar_open)
            fill = bar_open if gapped else level
            self._settle(position, fill, self._time_at(index), reason)
            return

    def drain_closed_trades(self) -> list[ClosedTrade]:
        finished, self._undrained = self._undrained, []
        return finished

    # -- market data -------------------------------------------------------

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        self._require_symbol(symbol)
        return self.spec

    def _require_symbol(self, symbol: str) -> None:
        if symbol != self.spec.symbol:
            raise BrokerError(f"paper broker holds {self.spec.symbol!r}, not {symbol!r}")

    def bars(self, symbol: str, timeframe: Timeframe, count: int) -> pd.DataFrame:
        self._require_symbol(symbol)
        if self.timeframe is not None and timeframe is not self.timeframe:
            # Catches the quiet mistake of backtesting an M15 configuration
            # against an H1 export: every indicator period would then mean
            # four times the wall-clock span the strategy was tuned for.
            raise BrokerError(
                f"feed is {self.timeframe.name} data but {timeframe.name} was requested; "
                "set timeframe in the config to match the file"
            )
        if self._i + 1 < count:
            raise DataUnavailableError(
                f"replay has only reached bar {self._i + 1} of the feed, needed {count}"
            )
        return self._feed.iloc[self._i + 1 - count : self._i + 1]

    def tick(self, symbol: str) -> Tick:
        self._require_symbol(symbol)
        bid = float(self._closes[self._i])
        return Tick(time=self._time_at(self._i), bid=bid, ask=bid + self._spread)

    def _time_at(self, index: int) -> datetime:
        return pd.Timestamp(self._times[index]).to_pydatetime()

    def account(self) -> Account:
        return Account(
            login=0,
            balance=self.balance,
            equity=self.balance + self._unrealized(),
            margin_free=self.balance,
            currency="USD",
            server="paper",
        )

    def _unrealized(self) -> float:
        if self._open is None:
            return 0.0
        tick = self.tick(self.spec.symbol)
        exit_price = tick.bid if self._open.side is Side.BUY else tick.ask
        return profit_of(
            self.spec, self._open.side, self._open.volume, self._open.price_open, exit_price
        )

    # -- trading -----------------------------------------------------------

    def positions(self, symbol: str | None = None) -> list[Position]:
        if self._open is None:
            return []
        if symbol is not None and symbol != self._open.symbol:
            return []
        # Refresh the floating P&L so callers see the same number a terminal would.
        return [
            Position(
                ticket=self._open.ticket,
                symbol=self._open.symbol,
                side=self._open.side,
                volume=self._open.volume,
                price_open=self._open.price_open,
                sl=self._open.sl,
                tp=self._open.tp,
                profit=self._unrealized(),
                magic=self._open.magic,
                time=self._open.time,
                comment=self._open.comment,
            )
        ]

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
        self._require_symbol(symbol)
        if self._open is not None:
            return OrderResult.failure("a position is already open (paper broker holds one)")
        if volume <= 0:
            return OrderResult.failure(f"volume must be positive, got {volume}")

        tick = self.tick(symbol)
        # Buy at the ask, sell at the bid; slippage always works against you.
        fill = tick.price_for(side) + side.sign * self._slippage

        ticket = next(self._tickets)
        self._open = Position(
            ticket=ticket,
            symbol=symbol,
            side=side,
            volume=volume,
            price_open=fill,
            sl=sl,
            tp=tp,
            magic=self.magic,
            time=tick.time,
            comment=comment,
        )
        self._entry_time = tick.time
        self._entry_reason = comment
        return OrderResult(ok=True, ticket=ticket, price=fill, volume=volume, comment="paper fill")

    def close_position(self, position: Position, *, comment: str = "") -> OrderResult:
        """Close at market. ``comment`` is accepted for interface parity; a
        simulated venue has nowhere to record it."""
        del comment
        if self._open is None or self._open.ticket != position.ticket:
            return OrderResult.failure(f"position {position.ticket} is not open")

        tick = self.tick(self._open.symbol)
        closing_side = self._open.side.opposite
        fill = tick.price_for(closing_side) + closing_side.sign * self._slippage
        # Always "signal": this is the category the report groups by, not a
        # free-text note. Threading the caller's comment through here gave
        # every exit its own bucket and made the breakdown unreadable.
        self._settle(self._open, fill, tick.time, "signal")
        return OrderResult(ok=True, ticket=position.ticket, price=fill, comment="paper close")

    def _settle(self, position: Position, fill: float, when: datetime, exit_reason: str) -> None:
        gross = profit_of(self.spec, position.side, position.volume, position.price_open, fill)
        # One round-turn commission, charged on the way out.
        net = gross - self._commission_per_lot * position.volume
        self.balance += net

        self.closed_trades.append(
            ClosedTrade(
                ticket=position.ticket,
                symbol=position.symbol,
                side=position.side,
                volume=position.volume,
                price_open=position.price_open,
                price_close=fill,
                time_open=self._entry_time or when,
                time_close=when,
                profit=net,
                exit_reason=exit_reason,
                entry_reason=self._entry_reason,
            )
        )
        self._undrained.append(self.closed_trades[-1])
        self._open = None
        self._entry_time = None
        self._entry_reason = ""


class DryRunBroker(Broker):
    """Reads a real broker, refuses to send it anything.

    The default for ``run``. Market data, spreads and contract details are the
    live ones, so what the log shows is genuinely what the bot would have done
    -- but ``open_position`` and ``close_position`` never reach the terminal.

    Stops are simulated against real ticks as they arrive, which is close but
    not exact: a tick between two polls can breach a level and come back
    without the simulation noticing. Dry-run answers "what would this bot do
    right now"; for performance figures, use the backtester, which sees every
    bar's full range.
    """

    def __init__(self, inner: Broker, *, balance_override: float | None = None) -> None:
        self._inner = inner
        self.magic = inner.magic
        self._balance_override = balance_override
        self._open: Position | None = None
        self._tickets = itertools.count(1)
        #: Every trade, kept for reporting at the end of a run.
        self.closed_trades: list[ClosedTrade] = []
        #: The same trades, but cleared each time the engine collects them.
        self._undrained: list[ClosedTrade] = []

    @property
    def name(self) -> str:
        return f"{self._inner.name} (dry run)"

    def connect(self) -> None:
        self._inner.connect()

    def shutdown(self) -> None:
        self._inner.shutdown()

    def on_cycle(self) -> bool:
        """Fill a simulated stop if the current quote has passed it."""
        if self._inner.on_cycle() is False:
            return False
        if self._open is not None:
            tick = self._inner.tick(self._open.symbol)
            exit_price = tick.bid if self._open.side is Side.BUY else tick.ask
            for level, reason in ((self._open.sl, "sl"), (self._open.tp, "tp")):
                if level is not None and _reached(self._open.side, level, reason, exit_price):
                    self._settle(self._open, exit_price, tick.time, reason)
                    break
        return True

    def drain_closed_trades(self) -> list[ClosedTrade]:
        finished, self._undrained = self._undrained, []
        return finished

    # -- reads pass straight through --------------------------------------

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        return self._inner.symbol_spec(symbol)

    def bars(self, symbol: str, timeframe: Timeframe, count: int) -> pd.DataFrame:
        return self._inner.bars(symbol, timeframe, count)

    def tick(self, symbol: str) -> Tick:
        return self._inner.tick(symbol)

    def account(self) -> Account:
        live = self._inner.account()
        if self._balance_override is None:
            return live
        return Account(
            login=live.login,
            balance=self._balance_override,
            equity=self._balance_override,
            margin_free=self._balance_override,
            currency=live.currency,
            leverage=live.leverage,
            server=live.server,
        )

    # -- writes are simulated ---------------------------------------------

    def positions(self, symbol: str | None = None) -> list[Position]:
        if self._open is None:
            return []
        if symbol is not None and symbol != self._open.symbol:
            return []
        return [self._open]

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
        if self._open is not None:
            return OrderResult.failure("a position is already open")
        tick = self._inner.tick(symbol)
        fill = tick.price_for(side)
        ticket = next(self._tickets)
        self._open = Position(
            ticket=ticket,
            symbol=symbol,
            side=side,
            volume=volume,
            price_open=fill,
            sl=sl,
            tp=tp,
            magic=self.magic,
            time=tick.time,
            comment=comment,
        )
        log.info(
            "DRY RUN would open %s %.2f %s at %s (sl=%s tp=%s)",
            side,
            volume,
            symbol,
            fill,
            sl,
            tp,
        )
        return OrderResult(ok=True, ticket=ticket, price=fill, volume=volume, comment="dry run")

    def close_position(self, position: Position, *, comment: str = "") -> OrderResult:
        del comment  # nothing is sent, so there is nowhere to attach it
        if self._open is None or self._open.ticket != position.ticket:
            return OrderResult.failure(f"position {position.ticket} is not open")
        tick = self._inner.tick(self._open.symbol)
        fill = tick.price_for(self._open.side.opposite)
        log.info("DRY RUN would close #%d at %s", position.ticket, fill)
        self._settle(self._open, fill, tick.time, "signal")
        return OrderResult(ok=True, ticket=position.ticket, price=fill, comment="dry run")

    def _settle(self, position: Position, fill: float, when: datetime, exit_reason: str) -> None:
        spec = self._inner.symbol_spec(position.symbol)
        self.closed_trades.append(
            ClosedTrade(
                ticket=position.ticket,
                symbol=position.symbol,
                side=position.side,
                volume=position.volume,
                price_open=position.price_open,
                price_close=fill,
                time_open=position.time or when,
                time_close=when,
                profit=profit_of(spec, position.side, position.volume, position.price_open, fill),
                exit_reason=exit_reason,
                entry_reason=position.comment,
            )
        )
        self._undrained.append(self.closed_trades[-1])
        self._open = None


def infer_timeframe(feed: pd.DataFrame) -> Timeframe | None:
    """Work out a feed's bar period from its timestamps.

    Uses the modal gap rather than the mean, so weekend breaks and the odd
    missing bar do not skew the answer. Returns ``None`` when the spacing does
    not match a known timeframe, which leaves the caller free to proceed
    without the cross-check rather than refusing an unusual but valid file.
    """
    if len(feed) < 3:
        return None
    gaps = pd.to_datetime(feed["time"]).diff().dropna()
    if gaps.empty:
        return None
    minutes = round(gaps.mode().iloc[0].total_seconds() / 60)
    try:
        return Timeframe(minutes)
    except ValueError:
        return None


def synthetic_feed(
    *,
    bars: int = 2000,
    start: datetime | None = None,
    timeframe: Timeframe = Timeframe.M15,
    start_price: float = 1.08000,
    seed: int = 20230517,
) -> pd.DataFrame:
    """A deterministic random-walk feed, for tests and examples.

    This is **not** market data and must never be presented as such. It has no
    trend, no session structure, no news and no fat tails, so a strategy's
    results on it say nothing about how it would trade. What it is good for is
    exercising the machinery: the engine, the stop handling and the metrics all
    need a feed that behaves the same on every machine and every run.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    start = start or datetime(2024, 1, 1, 0, 0)

    steps = rng.normal(0.0, 0.00035, bars)
    closes = start_price + np.cumsum(steps)
    opens = np.concatenate([[start_price], closes[:-1]])
    wick = np.abs(rng.normal(0.0, 0.00025, bars))

    return pd.DataFrame(
        {
            "time": [start + timedelta(minutes=timeframe.minutes * i) for i in range(bars)],
            "open": opens,
            "high": np.maximum(opens, closes) + wick,
            "low": np.minimum(opens, closes) - wick,
            "close": closes,
            "volume": rng.integers(50, 500, bars).astype(float),
        }
    )
