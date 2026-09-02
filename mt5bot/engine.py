"""The trading loop.

Three things separate this from the ``while True`` it replaces.

**It acts once per closed bar.** The original recomputed its indicators every
ten seconds against the candle still forming, so a signal could appear, be
traded, and then not exist any more when the bar finally closed.

**The broker is the only source of truth about what is open.** The original
kept ``position_id`` and ``position_type`` in two module-level variables, never
cleared them after a close, and had no way of noticing a stop loss filling. It
would then "close" a position that was already gone and open another on top.
Here every cycle begins by asking the broker.

**It survives its own mistakes.** Any error inside a cycle is caught and backed
off from, and a signal handler unwinds cleanly, so a transient disconnection
does not leave the process dead with money in the market.
"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from mt5bot.brokers.base import Broker
from mt5bot.config import Config
from mt5bot.errors import BrokerError
from mt5bot.journal import TradeJournal
from mt5bot.models import Position, Signal, SymbolSpec
from mt5bot.risk import position_size, spread_is_acceptable, spread_pips, stop_levels
from mt5bot.strategy.base import Strategy

log = logging.getLogger(__name__)

#: Give up after this many cycles in a row fail. Something is wrong that
#: retrying will not fix, and a bot flailing against a broken connection is
#: worse than a bot that has stopped and said so.
MAX_CONSECUTIVE_ERRORS = 10

#: Ceiling on the exponential backoff between failed cycles.
MAX_BACKOFF_SECONDS = 300


@dataclass(slots=True)
class EngineStats:
    """A tally of what a run did, for the closing summary and for tests."""

    cycles: int = 0
    bars_processed: int = 0
    signals: int = 0
    orders_opened: int = 0
    orders_closed: int = 0
    orders_rejected: int = 0
    skipped_spread: int = 0
    skipped_daily_cap: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (
            f"{self.bars_processed} bars, {self.signals} signals, "
            f"{self.orders_opened} opened, {self.orders_closed} closed, "
            f"{self.orders_rejected} rejected, {self.errors} errors"
        )


@dataclass(slots=True)
class _DailyCap:
    """Counts entries per calendar day, using bar time rather than the clock.

    Bar time is what makes the cap behave identically in a replay and in a
    live run.
    """

    limit: int
    day: date | None = None
    count: int = 0

    def allows(self, when: date) -> bool:
        return not (self.day == when and self.count >= self.limit)

    def record(self, when: date) -> None:
        if self.day != when:
            self.day, self.count = when, 0
        self.count += 1


@dataclass(slots=True)
class Engine:
    """Drives a strategy against a broker."""

    broker: Broker
    strategy: Strategy
    config: Config
    journal: TradeJournal = field(default_factory=lambda: TradeJournal(None))
    #: Injected so replays and tests run at full speed.
    sleep: Callable[[float], None] = time.sleep

    stats: EngineStats = field(default_factory=EngineStats)
    _stop: bool = False
    _position: Position | None = None
    _last_bar_time: pd.Timestamp | None = None
    _cap: _DailyCap | None = None

    # -- lifecycle ---------------------------------------------------------

    def request_stop(self) -> None:
        """Ask the loop to finish the current cycle and return."""
        self._stop = True

    def install_signal_handlers(self) -> None:
        """Turn Ctrl-C and SIGTERM into a clean exit rather than a traceback."""

        def handler(signum: int, _frame: object) -> None:
            log.warning("received signal %s, finishing the current cycle", signum)
            self.request_stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except ValueError:  # pragma: no cover - not the main thread
                log.debug("cannot install a handler for %s from this thread", sig)

    def run(self, *, max_cycles: int | None = None) -> EngineStats:
        """Poll until stopped, the feed ends, or ``max_cycles`` is reached."""
        symbol = self.config.trading.symbol
        self._cap = _DailyCap(self.config.risk.max_trades_per_day)

        self.broker.connect()
        try:
            spec = self.broker.symbol_spec(symbol)
            self._announce(spec)
            self._loop(spec, max_cycles)
        finally:
            self._farewell()
            self.broker.shutdown()

        log.info("Run finished: %s", self.stats.summary())
        return self.stats

    def _announce(self, spec: SymbolSpec) -> None:
        account = self.broker.account()
        log.info("=" * 62)
        log.info("Broker    : %s", self.broker.name)
        log.info(
            "Account   : %s %s, balance %.2f", account.login, account.currency, account.balance
        )
        log.info(
            "Market    : %s %s, %d digits, lots %.2f-%.2f step %.2f",
            spec.symbol,
            self.config.trading.timeframe,
            spec.digits,
            spec.volume_min,
            spec.volume_max,
            spec.volume_step,
        )
        log.info("Strategy  : %s", self.strategy.describe())
        log.info(
            "Risk      : %.1f pip stop / %.1f pip target, sizing=%s, max %d trades a day",
            self.config.risk.sl_pips,
            self.config.risk.tp_pips,
            self.config.risk.sizing,
            self.config.risk.max_trades_per_day,
        )
        log.info(
            "History   : %d bars per evaluation, polling every %ds",
            self.strategy.min_bars,
            self.config.poll_seconds,
        )
        log.info("=" * 62)

    def _farewell(self) -> None:
        """Say what is being left behind.

        Deliberately does *not* close anything. Liquidating on shutdown would
        turn a Ctrl-C, a lost connection or a machine reboot into a realised
        loss at whatever price happened to be showing, which is a far more
        expensive surprise than an open position with a stop already on it.
        """
        if self._position is None:
            return
        log.warning(
            "Exiting with %s %.2f %s still open (ticket #%d, sl=%s tp=%s). "
            "The broker's stop and target remain in force; close it yourself if "
            "that is not what you want.",
            self._position.side,
            self._position.volume,
            self._position.symbol,
            self._position.ticket,
            self._position.sl,
            self._position.tp,
        )

    # -- the loop ----------------------------------------------------------

    def _loop(self, spec: SymbolSpec, max_cycles: int | None) -> None:
        consecutive_errors = 0

        while not self._stop:
            if max_cycles is not None and self.stats.cycles >= max_cycles:
                log.info("reached the %d cycle limit", max_cycles)
                return
            if not self.broker.on_cycle():
                log.info("the broker's feed is exhausted")
                return

            self.stats.cycles += 1
            try:
                self._cycle(spec)
            except BrokerError as exc:
                self.stats.errors += 1
                consecutive_errors += 1
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    log.error(
                        "giving up after %d consecutive failures: %s", consecutive_errors, exc
                    )
                    return
                backoff = min(self.config.poll_seconds * 2**consecutive_errors, MAX_BACKOFF_SECONDS)
                log.error(
                    "cycle failed (%d in a row): %s -- retrying in %ds",
                    consecutive_errors,
                    exc,
                    backoff,
                )
                self.sleep(backoff)
                continue

            consecutive_errors = 0
            self.sleep(self.config.poll_seconds)

    def _cycle(self, spec: SymbolSpec) -> None:
        symbol = self.config.trading.symbol
        self._reconcile(symbol)

        bars = self.broker.bars(symbol, self.config.timeframe, self.strategy.min_bars)
        bar_time = pd.Timestamp(bars["time"].iloc[-1])
        if bar_time == self._last_bar_time:
            return  # the same closed bar as last time; nothing to decide
        self._last_bar_time = bar_time
        self.stats.bars_processed += 1

        evaluation = self.strategy.evaluate(bars)
        readings = " ".join(f"{k}={v:.5f}" for k, v in evaluation.indicators.items())
        log.info("%s | %s", bar_time, readings)

        if evaluation.signal is None:
            return
        self.stats.signals += 1
        log.info("SIGNAL %s -- %s", evaluation.signal.side, evaluation.signal.reason)
        self._act(evaluation.signal, spec)

    def _reconcile(self, symbol: str) -> None:
        """Re-read what is open, and journal anything that closed elsewhere."""
        for trade in self.broker.drain_closed_trades():
            self.journal.record(trade)
            log.info(
                "CLOSED #%d %s %.2f at %.5f -> %+.2f (%s)",
                trade.ticket,
                trade.side,
                trade.volume,
                trade.price_close,
                trade.profit,
                trade.exit_reason,
            )

        open_positions = self.broker.positions(symbol)
        if len(open_positions) > 1:
            log.warning(
                "%d positions are open on %s with this magic number; managing the first",
                len(open_positions),
                symbol,
            )
        self._position = open_positions[0] if open_positions else None

    # -- acting on a signal ------------------------------------------------

    def _act(self, sig: Signal, spec: SymbolSpec) -> None:
        if self._position is not None:
            if self._position.side is sig.side:
                log.info("already %s; holding", self._position.side)
                return

            log.info("reversing: closing #%d first", self._position.ticket)
            result = self.broker.close_position(self._position, comment=sig.reason)
            if not result.ok:
                # The original ignored this and opened the opposite position
                # anyway, ending up hedged, double-margined, and wrong about
                # both.
                self.stats.orders_rejected += 1
                log.error("close failed (%s); not opening the reverse", result.comment)
                return
            self.stats.orders_closed += 1

            self._reconcile(self.config.trading.symbol)
            if self._position is not None:
                log.error(
                    "#%d is still open after the close reported success; standing down",
                    self._position.ticket,
                )
                return

        self._open(sig, spec)

    def _open(self, sig: Signal, spec: SymbolSpec) -> None:
        risk = self.config.risk
        symbol = self.config.trading.symbol

        today = self._last_bar_time.date() if self._last_bar_time is not None else date.today()
        assert self._cap is not None
        if not self._cap.allows(today):
            self.stats.skipped_daily_cap += 1
            log.warning("daily limit of %d trades reached; skipping", self._cap.limit)
            return

        tick = self.broker.tick(symbol)
        if not spread_is_acceptable(tick, spec, risk.max_spread_pips):
            self.stats.skipped_spread += 1
            log.info(
                "skipping: spread is %.1f pips, limit is %.1f",
                spread_pips(tick, spec),
                risk.max_spread_pips,
            )
            return

        account = self.broker.account()
        volume = position_size(
            mode=risk.sizing,
            fixed_lot=risk.fixed_lot,
            risk_percent=risk.risk_percent,
            balance=account.balance,
            sl_pips=risk.sl_pips,
            spec=spec,
        )
        if volume <= 0:
            log.warning(
                "computed size is below %s's minimum lot of %.2f; skipping",
                symbol,
                spec.volume_min,
            )
            return

        entry = tick.price_for(sig.side)
        levels = stop_levels(entry, sig.side, spec, sl_pips=risk.sl_pips, tp_pips=risk.tp_pips)
        if levels.widened:
            log.warning(
                "the broker's minimum stop distance (%d points) is wider than the "
                "configured levels; they have been pushed out",
                spec.trade_stops_level,
            )

        result = self.broker.open_position(
            symbol, sig.side, volume, sl=levels.sl, tp=levels.tp, comment=sig.reason
        )
        if not result.ok:
            self.stats.orders_rejected += 1
            log.error("open failed: %s", result.comment)
            return

        self.stats.orders_opened += 1
        self._cap.record(today)
        log.info(
            "OPENED %s %.2f %s at %.5f (sl=%.5f tp=%.5f, ticket #%s)",
            sig.side,
            volume,
            symbol,
            result.price or entry,
            levels.sl or 0.0,
            levels.tp or 0.0,
            result.ticket,
        )
