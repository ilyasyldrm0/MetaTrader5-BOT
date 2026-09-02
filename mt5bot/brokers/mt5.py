"""Adapter for a live MetaTrader 5 terminal.

Every MetaTrader-specific quirk the bot has to respect lives in this file and
nowhere else, so the strategy and the engine stay importable on a machine that
cannot install the package at all.

Most of what follows is a correction of the original script's order handling:

* prices came from the last *bar close* rather than the current tick, so a
  market order was priced against data up to fifteen minutes stale
* no ``deviation`` was sent, so the request tolerated zero slippage and was
  requoted by any tick that arrived first
* the fill policy was hard-coded to IOC, which brokers that only accept FOK
  reject outright
* ``order_send`` can return ``None``; reading ``.retcode`` off it raised
  ``AttributeError`` and killed the process with a position open
* the close request carried only ``action`` and ``position``, omitting the
  symbol, volume, direction and price that ``TRADE_ACTION_DEAL`` requires, so
  no position was ever closed by the bot
"""

from __future__ import annotations

import logging
from datetime import datetime
from types import ModuleType
from typing import Any, Final, cast

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

log = logging.getLogger(__name__)

#: Bits of ``symbol_info.filling_mode``. Note these are *not* the same numbers
#: as the ``ORDER_FILLING_*`` constants that go into a request.
_SYMBOL_FILLING_FOK: Final = 1
_SYMBOL_FILLING_IOC: Final = 2

#: Rejections worth re-sending: the price moved between building the request
#: and the server reading it. Anything else (invalid volume, no money, market
#: closed) will fail identically however many times it is retried.
_RETRYABLE_RETCODES: Final = frozenset({10004, 10012, 10020, 10021})

_DONE: Final = 10009
_DONE_PARTIAL: Final = 10010


def _import_mt5() -> ModuleType:
    """Import MetaTrader5, or explain why it is missing.

    Imported lazily and never at module scope. The package is Windows-only, so
    a top-level import would make this module -- and anything that imports the
    broker registry -- unimportable on Linux and macOS, taking the backtester
    and the whole test suite down with it.
    """
    try:
        import MetaTrader5
    except ImportError as exc:  # pragma: no cover - platform dependent
        raise BrokerError(
            "The MetaTrader5 package is not installed.\n"
            "  Windows:       pip install MetaTrader5\n"
            "  Linux / macOS: it is Windows-only. Use 'backtest' or "
            "'run --replay' instead, or run the terminal under Wine."
        ) from exc
    return cast(ModuleType, MetaTrader5)


class Mt5Broker(Broker):
    """Live trading against a running MetaTrader 5 terminal."""

    def __init__(
        self,
        *,
        magic: int = 234000,
        deviation: int = 20,
        terminal_path: str | None = None,
        login: int | None = None,
        password: str | None = None,
        server: str | None = None,
        timeout_ms: int = 60_000,
        max_retries: int = 3,
    ) -> None:
        self.magic = magic
        self.deviation = deviation
        self._terminal_path = terminal_path
        self._login = login
        self._password = password
        self._server = server
        self._timeout_ms = timeout_ms
        self._max_retries = max(1, max_retries)
        self._mt5: ModuleType | None = None
        self._selected: set[str] = set()
        self._seen: dict[int, Position] = {}
        self._undrained: list[ClosedTrade] = []

    @property
    def name(self) -> str:
        return "MetaTrader5"

    @property
    def mt5(self) -> ModuleType:
        if self._mt5 is None:
            raise BrokerError("broker is not connected; call connect() first")
        return self._mt5

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        mt5 = _import_mt5()

        kwargs: dict[str, Any] = {"timeout": self._timeout_ms}
        if self._login is not None:
            kwargs |= {"login": self._login, "password": self._password, "server": self._server}

        ok = (
            mt5.initialize(self._terminal_path, **kwargs)
            if self._terminal_path
            else mt5.initialize(**kwargs)
        )
        if not ok:
            code, description = mt5.last_error()
            # The original called quit(), which only exists because the site
            # module injects it for the REPL, and printed the error to stdout.
            raise BrokerError(f"MetaTrader5 initialize() failed: {description} (code {code})")

        self._mt5 = mt5
        info = mt5.terminal_info()
        if info is not None and not bool(info.trade_allowed):
            log.warning(
                "Algo trading is disabled in the terminal. Enable the 'Algo Trading' "
                "button, or every order will come back as TRADE_DISABLED."
            )

    def shutdown(self) -> None:
        if self._mt5 is not None:
            self._mt5.shutdown()
            self._mt5 = None
            self._selected.clear()
            self._seen.clear()

    # -- market data -------------------------------------------------------

    def _select(self, symbol: str) -> None:
        """Add the symbol to Market Watch.

        A symbol the terminal is not watching returns ``None`` for both rates
        and ticks. The original never selected one, so it worked only when the
        chart happened to be open already.
        """
        if symbol in self._selected:
            return
        if not self.mt5.symbol_select(symbol, True):
            code, description = self.mt5.last_error()
            raise BrokerError(f"cannot select symbol {symbol!r}: {description} (code {code})")
        self._selected.add(symbol)

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        self._select(symbol)
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise BrokerError(f"symbol {symbol!r} is not available on this account")
        return SymbolSpec(
            symbol=symbol,
            digits=int(info.digits),
            point=float(info.point),
            volume_min=float(info.volume_min),
            volume_max=float(info.volume_max),
            volume_step=float(info.volume_step),
            trade_tick_value=float(info.trade_tick_value),
            trade_tick_size=float(info.trade_tick_size),
            trade_stops_level=int(info.trade_stops_level),
            filling_mode=int(info.filling_mode),
            contract_size=float(info.trade_contract_size),
        )

    def bars(self, symbol: str, timeframe: Timeframe, count: int) -> pd.DataFrame:
        self._select(symbol)
        mt5_timeframe = getattr(self.mt5, f"TIMEFRAME_{timeframe.name}", None)
        if mt5_timeframe is None:  # pragma: no cover - guards a typo, not user input
            raise BrokerError(f"MetaTrader5 has no timeframe {timeframe.name}")

        # start_pos=1 skips the candle still forming. Reading from 0, as the
        # original did, means the indicators are recomputed from a partial bar
        # on every poll and a signal can appear and disappear within one bar.
        rates = self.mt5.copy_rates_from_pos(symbol, mt5_timeframe, 1, count)
        if rates is None or len(rates) == 0:
            code, description = self.mt5.last_error()
            raise DataUnavailableError(
                f"no {timeframe.name} history for {symbol}: {description} (code {code})"
            )
        if len(rates) < count:
            raise DataUnavailableError(
                f"{symbol} returned {len(rates)} {timeframe.name} bars, needed {count}. "
                "The symbol's history may not be downloaded yet."
            )

        frame = pd.DataFrame(rates)
        frame = frame.rename(columns={"tick_volume": "volume"})
        # Broker server time, not UTC -- MetaTrader reports whatever the server
        # runs on, commonly UTC+2/+3. Left naive rather than mislabelled.
        frame["time"] = pd.to_datetime(frame["time"], unit="s")
        return frame.loc[:, list(BAR_COLUMNS)].reset_index(drop=True)

    def tick(self, symbol: str) -> Tick:
        self._select(symbol)
        raw = self.mt5.symbol_info_tick(symbol)
        if raw is None:
            code, description = self.mt5.last_error()
            raise DataUnavailableError(f"no tick for {symbol}: {description} (code {code})")
        if raw.bid <= 0 or raw.ask <= 0:
            raise DataUnavailableError(
                f"{symbol} quoted bid={raw.bid} ask={raw.ask}; market closed?"
            )
        return Tick(
            time=datetime.fromtimestamp(raw.time),
            bid=float(raw.bid),
            ask=float(raw.ask),
        )

    def account(self) -> Account:
        info = self.mt5.account_info()
        if info is None:
            code, description = self.mt5.last_error()
            raise BrokerError(f"cannot read account: {description} (code {code})")
        return Account(
            login=int(info.login),
            balance=float(info.balance),
            equity=float(info.equity),
            margin_free=float(info.margin_free),
            currency=str(info.currency),
            leverage=int(info.leverage),
            server=str(info.server),
        )

    # -- trading -----------------------------------------------------------

    def positions(self, symbol: str | None = None) -> list[Position]:
        raw = self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get()
        if raw is None:
            # An empty account and a failed call both look like this; treat it
            # as "nothing open" but say so, since it decides whether the engine
            # opens a second position.
            code, description = self.mt5.last_error()
            if code != 1:  # 1 == RES_S_OK, i.e. genuinely no positions
                log.warning("positions_get failed: %s (code %s)", description, code)
            return []
        found = [self._to_position(p) for p in raw if int(p.magic) == self.magic]
        self._note_closures(found, symbol)
        return found

    def _note_closures(self, found: list[Position], symbol: str | None) -> None:
        """Queue a journal entry for any position that has gone since last time.

        A stop loss filling at the broker is invisible to the bot otherwise --
        which is precisely how the original ended up holding a position id
        that no longer referred to anything.

        Comparison is scoped to the same symbol filter as the query, or
        positions on other symbols would look as though they had vanished.
        """
        current = {p.ticket: p for p in found}
        watched = {
            ticket: position
            for ticket, position in self._seen.items()
            if symbol is None or position.symbol == symbol
        }
        for ticket, position in watched.items():
            if ticket in current:
                continue
            del self._seen[ticket]
            trade = self._closed_trade(position)
            if trade is not None:
                self._undrained.append(trade)
            else:
                log.warning("position #%d is gone but no closing deal was found in history", ticket)
        self._seen.update(current)

    def _closed_trade(self, position: Position) -> ClosedTrade | None:
        """Rebuild a finished round trip from the terminal's deal history."""
        deals = self.mt5.history_deals_get(position=position.ticket)
        if not deals:
            return None
        # DEAL_ENTRY_OUT and DEAL_ENTRY_INOUT are the deals that reduce or
        # reverse a position; there can be several if it closed in parts.
        closing = [d for d in deals if int(d.entry) in (1, 2)]
        if not closing:
            return None

        last = closing[-1]
        # Commission and swap are what turn a nominal profit into the number
        # that actually reached the balance.
        profit = sum(float(d.profit) + float(d.commission) + float(d.swap) for d in closing)
        return ClosedTrade(
            ticket=position.ticket,
            symbol=position.symbol,
            side=position.side,
            volume=position.volume,
            price_open=position.price_open,
            price_close=float(last.price),
            time_open=position.time or datetime.fromtimestamp(int(last.time)),
            time_close=datetime.fromtimestamp(int(last.time)),
            profit=profit,
            exit_reason="broker",
            entry_reason=position.comment,
        )

    def drain_closed_trades(self) -> list[ClosedTrade]:
        finished, self._undrained = self._undrained, []
        return finished

    @staticmethod
    def _to_position(raw: Any) -> Position:
        return Position(
            ticket=int(raw.ticket),
            symbol=str(raw.symbol),
            side=Side.BUY if int(raw.type) == 0 else Side.SELL,
            volume=float(raw.volume),
            price_open=float(raw.price_open),
            sl=float(raw.sl) or None,
            tp=float(raw.tp) or None,
            profit=float(raw.profit),
            magic=int(raw.magic),
            time=datetime.fromtimestamp(int(raw.time)),
            comment=str(raw.comment),
        )

    def _fill_policy(self, spec: SymbolSpec) -> int:
        """Pick a fill policy the broker actually accepts for this symbol.

        IOC first: a partial fill leaves a smaller position than intended,
        which under-risks rather than over-risks, and is preferable to an
        outright rejection in a fast market. FOK next, and RETURN as the
        fallback for exchange-execution symbols that support neither.
        """
        mt5 = self.mt5
        if spec.filling_mode & _SYMBOL_FILLING_IOC:
            return int(mt5.ORDER_FILLING_IOC)
        if spec.filling_mode & _SYMBOL_FILLING_FOK:
            return int(mt5.ORDER_FILLING_FOK)
        return int(mt5.ORDER_FILLING_RETURN)

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
        mt5 = self.mt5
        spec = self.symbol_spec(symbol)
        order_type = mt5.ORDER_TYPE_BUY if side is Side.BUY else mt5.ORDER_TYPE_SELL

        def build(price: float) -> dict[str, Any]:
            request: dict[str, Any] = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": order_type,
                "price": price,
                "deviation": self.deviation,
                "magic": self.magic,
                "comment": comment[:31],  # MetaTrader truncates past 31 chars
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._fill_policy(spec),
            }
            if sl is not None:
                request["sl"] = sl
            if tp is not None:
                request["tp"] = tp
            return request

        return self._send(build, side=side, symbol=symbol, what="open")

    def close_position(self, position: Position, *, comment: str = "") -> OrderResult:
        """Close at market with a complete request.

        The original sent ``{"position": id, "action": TRADE_ACTION_DEAL}`` and
        nothing else. A deal needs to know what to trade, how much, in which
        direction and at what price -- so the request either failed or, worse,
        returned None and crashed the caller on ``.retcode``.
        """
        mt5 = self.mt5
        closing_side = position.side.opposite
        order_type = mt5.ORDER_TYPE_BUY if closing_side is Side.BUY else mt5.ORDER_TYPE_SELL
        spec = self.symbol_spec(position.symbol)

        def build(price: float) -> dict[str, Any]:
            return {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": position.symbol,
                "volume": position.volume,
                "type": order_type,
                "position": position.ticket,
                "price": price,
                "deviation": self.deviation,
                "magic": self.magic,
                "comment": (comment or "close")[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._fill_policy(spec),
            }

        return self._send(build, side=closing_side, symbol=position.symbol, what="close")

    # -- request plumbing --------------------------------------------------

    def _send(
        self,
        build: Any,
        *,
        side: Side,
        symbol: str,
        what: str,
    ) -> OrderResult:
        """Price, validate and send a request, retrying only price rejections.

        The price is re-read from the current tick on every attempt: resending
        a stale price is what turns one requote into a loop of them.
        """
        mt5 = self.mt5
        last: OrderResult = OrderResult.failure(f"{what} was never attempted")

        for attempt in range(1, self._max_retries + 1):
            try:
                price = self.tick(symbol).price_for(side)
            except DataUnavailableError as exc:
                return OrderResult.failure(str(exc))

            request = build(price)

            check = mt5.order_check(request)
            if check is not None and int(check.retcode) not in (0, _DONE):
                # Margin and volume problems are settled here, before anything
                # reaches the market.
                return OrderResult.failure(
                    f"{what} rejected in pre-check: {check.comment}", int(check.retcode)
                )

            raw = mt5.order_send(request)
            if raw is None:
                code, description = mt5.last_error()
                last = OrderResult.failure(f"{what} returned no result: {description}", code)
                log.warning("order_send returned None on attempt %d: %s", attempt, description)
                continue

            retcode = int(raw.retcode)
            if retcode in (_DONE, _DONE_PARTIAL):
                if retcode == _DONE_PARTIAL:
                    log.warning(
                        "%s partially filled: %s of %s lots", what, raw.volume, request["volume"]
                    )
                return OrderResult(
                    ok=True,
                    comment=str(raw.comment),
                    retcode=retcode,
                    ticket=int(raw.order) or None,
                    price=float(raw.price) or None,
                    volume=float(raw.volume) or None,
                )

            last = OrderResult.failure(f"{what} failed: {raw.comment}", retcode)
            if retcode not in _RETRYABLE_RETCODES:
                return last
            log.info(
                "%s requoted (retcode %d), attempt %d of %d",
                what,
                retcode,
                attempt,
                self._max_retries,
            )

        return last
