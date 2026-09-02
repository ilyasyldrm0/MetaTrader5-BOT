"""Tests for the live MetaTrader adapter, against a fake terminal.

These matter more than their coverage percentage suggests. The headline bug in
the original -- a close request missing everything except the ticket -- lived
exactly here, in code that cannot run on a CI machine and that nobody can
exercise by hand without risking money. A fake module is the only way to assert
the shape of a request before a broker sees it.

The fake mimics the parts of the MetaTrader5 API the adapter touches, and
records every request so the tests can inspect what would have been sent.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from mt5bot.brokers.mt5 import Mt5Broker
from mt5bot.errors import BrokerError, DataUnavailableError
from mt5bot.models import Position, Side, Timeframe

DONE = 10009
REQUOTE = 10004
NO_MONEY = 10019

SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2


class FakeMt5:
    """Enough of the MetaTrader5 module for the adapter to talk to."""

    # Order type / action constants, with the values the real package uses.
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TIMEFRAME_M15 = 15
    TIMEFRAME_H1 = 16385

    def __init__(self, **overrides: Any) -> None:
        self.requests: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []
        self.selected: list[str] = []
        self.shutdown_calls = 0
        self.initialize_ok = True
        self.select_ok = True
        self.error: tuple[int, str] = (0, "no error")
        self.rates: Any = _rates(120)
        self.tick_bid = 1.08000
        self.tick_ask = 1.08012
        self.filling_mode = SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC
        self.send_results: list[Any] = []
        self.check_result: Any = SimpleNamespace(retcode=0, comment="ok", margin=10.0)
        self.open_positions: list[Any] = []
        self.deals: Any = ()
        for key, value in overrides.items():
            setattr(self, key, value)

    # -- lifecycle ---------------------------------------------------------

    def initialize(self, *args: Any, **kwargs: Any) -> bool:
        self.init_args = (args, kwargs)
        return self.initialize_ok

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def last_error(self) -> tuple[int, str]:
        return self.error

    def terminal_info(self) -> Any:
        return SimpleNamespace(trade_allowed=True)

    def account_info(self) -> Any:
        return SimpleNamespace(
            login=12345678,
            balance=10_000.0,
            equity=10_050.0,
            margin_free=9_000.0,
            currency="USD",
            leverage=100,
            server="Fake-Demo",
        )

    # -- market data -------------------------------------------------------

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        if self.select_ok:
            self.selected.append(symbol)
        return self.select_ok

    def symbol_info(self, symbol: str) -> Any:
        return SimpleNamespace(
            digits=5,
            point=0.00001,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            trade_tick_value=1.0,
            trade_tick_size=0.00001,
            trade_stops_level=0,
            filling_mode=self.filling_mode,
            trade_contract_size=100_000.0,
        )

    def symbol_info_tick(self, symbol: str) -> Any:
        if self.tick_bid is None:
            return None
        return SimpleNamespace(time=1_700_000_000, bid=self.tick_bid, ask=self.tick_ask)

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start: int, count: int) -> Any:
        self.last_rates_call = (symbol, timeframe, start, count)
        return self.rates

    # -- trading -----------------------------------------------------------

    def positions_get(self, **kwargs: Any) -> Any:
        return tuple(self.open_positions)

    def order_check(self, request: dict[str, Any]) -> Any:
        self.checks.append(dict(request))
        return self.check_result

    def order_send(self, request: dict[str, Any]) -> Any:
        self.requests.append(dict(request))
        if self.send_results:
            return self.send_results.pop(0)
        return SimpleNamespace(
            retcode=DONE,
            comment="done",
            order=555,
            price=request["price"],
            volume=request["volume"],
        )

    def history_deals_get(self, **kwargs: Any) -> Any:
        return self.deals


def _rates(count: int) -> Any:
    import numpy as np

    dtype = [
        ("time", "i8"),
        ("open", "f8"),
        ("high", "f8"),
        ("low", "f8"),
        ("close", "f8"),
        ("tick_volume", "i8"),
        ("spread", "i4"),
        ("real_volume", "i8"),
    ]
    rows = [
        (1_700_000_000 + i * 900, 1.08, 1.0805, 1.0795, 1.0802, 100, 10, 0) for i in range(count)
    ]
    return np.array(rows, dtype=dtype)


def result(retcode: int, **kwargs: Any) -> Any:
    base = {"comment": "x", "order": 1, "price": 1.08, "volume": 0.1}
    return SimpleNamespace(retcode=retcode, **{**base, **kwargs})


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeMt5:
    module = FakeMt5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", module)
    return module


@pytest.fixture
def broker(fake: FakeMt5) -> Mt5Broker:
    b = Mt5Broker(magic=234000, deviation=20)
    b.connect()
    return b


class TestConnection:
    def test_a_failed_initialize_raises_with_the_terminal_error(self, fake: FakeMt5) -> None:
        """The original printed the error and called quit(), a REPL builtin."""
        fake.initialize_ok = False
        fake.error = (-6, "Terminal: Authorization failed")
        with pytest.raises(BrokerError, match="Authorization failed"):
            Mt5Broker().connect()

    def test_credentials_are_passed_through(self, fake: FakeMt5) -> None:
        b = Mt5Broker(login=999, password="pw", server="Broker-Live")
        b.connect()
        _args, kwargs = fake.init_args
        assert kwargs["login"] == 999
        assert kwargs["server"] == "Broker-Live"

    def test_the_terminal_path_is_positional(self, fake: FakeMt5) -> None:
        """MetaTrader5.initialize takes the path positionally; None is not the same."""
        b = Mt5Broker(terminal_path="C:/mt5/terminal64.exe")
        b.connect()
        args, _kwargs = fake.init_args
        assert args == ("C:/mt5/terminal64.exe",)

    def test_shutdown_is_safe_to_call_twice(self, broker: Mt5Broker, fake: FakeMt5) -> None:
        broker.shutdown()
        broker.shutdown()
        assert fake.shutdown_calls == 1

    def test_using_the_broker_before_connecting_is_an_error_not_a_crash(self) -> None:
        with pytest.raises(BrokerError, match="not connected"):
            Mt5Broker().positions()


class TestMarketData:
    def test_the_symbol_is_added_to_market_watch_first(
        self, broker: Mt5Broker, fake: FakeMt5
    ) -> None:
        """An unwatched symbol returns None for both rates and ticks."""
        broker.tick("EURUSD")
        assert fake.selected == ["EURUSD"]

    def test_selection_happens_once(self, broker: Mt5Broker, fake: FakeMt5) -> None:
        broker.tick("EURUSD")
        broker.tick("EURUSD")
        assert fake.selected == ["EURUSD"]

    def test_a_symbol_that_cannot_be_selected_raises(
        self, broker: Mt5Broker, fake: FakeMt5
    ) -> None:
        fake.select_ok = False
        fake.error = (4301, "Unknown symbol")
        with pytest.raises(BrokerError, match="cannot select symbol"):
            broker.tick("NOPE")

    def test_bars_skip_the_candle_still_forming(self, broker: Mt5Broker, fake: FakeMt5) -> None:
        """Position 1, not 0. Reading 0 is what made signals repaint."""
        broker.bars("EURUSD", Timeframe.M15, 100)
        _symbol, timeframe, start, count = fake.last_rates_call
        assert start == 1
        assert count == 100
        assert timeframe == FakeMt5.TIMEFRAME_M15

    def test_bars_returns_the_expected_columns(self, broker: Mt5Broker) -> None:
        frame = broker.bars("EURUSD", Timeframe.M15, 100)
        assert list(frame.columns) == ["time", "open", "high", "low", "close", "volume"]
        assert len(frame) == 120

    def test_no_history_raises_instead_of_returning_none(
        self, broker: Mt5Broker, fake: FakeMt5
    ) -> None:
        """The original fed None to DataFrame and then indexed [-1] on it."""
        fake.rates = None
        fake.error = (1, "no data")
        with pytest.raises(DataUnavailableError, match="no M15 history"):
            broker.bars("EURUSD", Timeframe.M15, 100)

    def test_too_few_bars_raises(self, broker: Mt5Broker, fake: FakeMt5) -> None:
        fake.rates = _rates(30)
        with pytest.raises(DataUnavailableError, match="needed 100"):
            broker.bars("EURUSD", Timeframe.M15, 100)

    def test_a_missing_tick_raises(self, broker: Mt5Broker, fake: FakeMt5) -> None:
        fake.tick_bid = None
        with pytest.raises(DataUnavailableError, match="no tick"):
            broker.tick("EURUSD")

    def test_a_zero_quote_is_treated_as_no_market(self, broker: Mt5Broker, fake: FakeMt5) -> None:
        fake.tick_bid, fake.tick_ask = 0.0, 0.0
        with pytest.raises(DataUnavailableError, match="market closed"):
            broker.tick("EURUSD")

    def test_symbol_spec_reads_the_contract_rather_than_assuming_it(
        self, broker: Mt5Broker
    ) -> None:
        spec = broker.symbol_spec("EURUSD")
        assert spec.digits == 5
        assert spec.volume_step == pytest.approx(0.01)
        assert spec.trade_tick_value == pytest.approx(1.0)


class TestOpeningAPosition:
    def test_a_buy_is_priced_at_the_ask(self, broker: Mt5Broker, fake: FakeMt5) -> None:
        """Not the last bar close, which could be fifteen minutes old."""
        broker.open_position("EURUSD", Side.BUY, 0.1)
        assert fake.requests[-1]["price"] == pytest.approx(fake.tick_ask)

    def test_a_sell_is_priced_at_the_bid(self, broker: Mt5Broker, fake: FakeMt5) -> None:
        broker.open_position("EURUSD", Side.SELL, 0.1)
        assert fake.requests[-1]["price"] == pytest.approx(fake.tick_bid)

    def test_the_request_carries_everything_a_deal_needs(
        self, broker: Mt5Broker, fake: FakeMt5
    ) -> None:
        broker.open_position("EURUSD", Side.BUY, 0.25, sl=1.0775, tp=1.0850, comment="why")
        request = fake.requests[-1]
        assert request["action"] == FakeMt5.TRADE_ACTION_DEAL
        assert request["symbol"] == "EURUSD"
        assert request["volume"] == pytest.approx(0.25)
        assert request["type"] == FakeMt5.ORDER_TYPE_BUY
        assert request["sl"] == pytest.approx(1.0775)
        assert request["tp"] == pytest.approx(1.0850)
        assert request["magic"] == 234000
        assert request["deviation"] == 20  # the original sent none, meaning zero

    def test_stops_are_omitted_rather_than_sent_as_zero(
        self, broker: Mt5Broker, fake: FakeMt5
    ) -> None:
        broker.open_position("EURUSD", Side.BUY, 0.1)
        assert "sl" not in fake.requests[-1]
        assert "tp" not in fake.requests[-1]

    def test_the_comment_is_truncated_to_what_metatrader_accepts(
        self, broker: Mt5Broker, fake: FakeMt5
    ) -> None:
        broker.open_position("EURUSD", Side.BUY, 0.1, comment="x" * 100)
        assert len(fake.requests[-1]["comment"]) == 31

    def test_a_successful_send_reports_the_ticket_and_fill(self, broker: Mt5Broker) -> None:
        outcome = broker.open_position("EURUSD", Side.BUY, 0.1)
        assert outcome.ok is True
        assert outcome.ticket == 555


class TestClosingAPosition:
    def test_the_close_request_is_complete(self, broker: Mt5Broker, fake: FakeMt5) -> None:
        """The bug that meant no position could ever be closed.

        The original sent {"action": TRADE_ACTION_DEAL, "position": ticket} and
        nothing else. A deal needs to know what to trade, how much, in which
        direction and at what price.
        """
        position = Position(
            ticket=777, symbol="EURUSD", side=Side.BUY, volume=0.35, price_open=1.0800
        )
        broker.close_position(position)

        request = fake.requests[-1]
        assert request["action"] == FakeMt5.TRADE_ACTION_DEAL
        assert request["position"] == 777
        assert request["symbol"] == "EURUSD"
        assert request["volume"] == pytest.approx(0.35)
        assert request["type"] == FakeMt5.ORDER_TYPE_SELL  # opposite of the long
        assert request["price"] == pytest.approx(fake.tick_bid)  # a long sells at the bid
        assert request["deviation"] == 20
        assert "type_filling" in request

    def test_closing_a_short_buys_at_the_ask(self, broker: Mt5Broker, fake: FakeMt5) -> None:
        position = Position(
            ticket=778, symbol="EURUSD", side=Side.SELL, volume=0.1, price_open=1.08
        )
        broker.close_position(position)
        request = fake.requests[-1]
        assert request["type"] == FakeMt5.ORDER_TYPE_BUY
        assert request["price"] == pytest.approx(fake.tick_ask)


class TestFillPolicy:
    @pytest.mark.parametrize(
        ("mask", "expected"),
        [
            (SYMBOL_FILLING_IOC, FakeMt5.ORDER_FILLING_IOC),
            (SYMBOL_FILLING_FOK, FakeMt5.ORDER_FILLING_FOK),
            (SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC, FakeMt5.ORDER_FILLING_IOC),
            (0, FakeMt5.ORDER_FILLING_RETURN),
        ],
    )
    def test_it_is_read_from_the_symbol_not_hard_coded(
        self, broker: Mt5Broker, fake: FakeMt5, mask: int, expected: int
    ) -> None:
        """The original always sent IOC, which FOK-only brokers reject."""
        fake.filling_mode = mask
        broker.open_position("EURUSD", Side.BUY, 0.1)
        assert fake.requests[-1]["type_filling"] == expected


class TestFailureHandling:
    def test_a_none_result_does_not_raise_attributeerror(
        self, broker: Mt5Broker, fake: FakeMt5
    ) -> None:
        """order_send can return None. Reading .retcode off it killed the bot.

        With a position open, and no handler to catch it.
        """
        fake.send_results = [None, None, None]
        fake.error = (10027, "AutoTrading disabled by client")

        outcome = broker.open_position("EURUSD", Side.BUY, 0.1)
        assert outcome.ok is False
        assert "AutoTrading disabled" in outcome.comment

    def test_a_rejection_is_returned_not_raised(self, broker: Mt5Broker, fake: FakeMt5) -> None:
        fake.send_results = [result(NO_MONEY, comment="No money")]
        outcome = broker.open_position("EURUSD", Side.BUY, 99.0)
        assert outcome.ok is False
        assert outcome.retcode == NO_MONEY

    def test_a_requote_is_retried_with_a_fresh_price(
        self, broker: Mt5Broker, fake: FakeMt5
    ) -> None:
        """Resending a stale price is how one requote becomes a loop of them."""
        fake.send_results = [result(REQUOTE, comment="Requote")]

        def moving_tick(symbol: str) -> Any:
            fake.tick_ask += 0.0001
            return SimpleNamespace(time=1_700_000_000, bid=fake.tick_bid, ask=fake.tick_ask)

        fake.symbol_info_tick = moving_tick  # type: ignore[method-assign]
        outcome = broker.open_position("EURUSD", Side.BUY, 0.1)

        assert outcome.ok is True
        assert len(fake.requests) == 2
        assert fake.requests[1]["price"] > fake.requests[0]["price"]

    def test_a_permanent_rejection_is_not_retried(self, broker: Mt5Broker, fake: FakeMt5) -> None:
        """No amount of resending fixes 'no money'."""
        fake.send_results = [result(NO_MONEY), result(NO_MONEY), result(NO_MONEY)]
        broker.open_position("EURUSD", Side.BUY, 99.0)
        assert len(fake.requests) == 1

    def test_retries_are_bounded(self, broker: Mt5Broker, fake: FakeMt5) -> None:
        fake.send_results = [result(REQUOTE) for _ in range(10)]
        outcome = broker.open_position("EURUSD", Side.BUY, 0.1)
        assert outcome.ok is False
        assert len(fake.requests) == 3  # max_retries

    def test_a_precheck_failure_never_reaches_the_market(
        self, broker: Mt5Broker, fake: FakeMt5
    ) -> None:
        fake.check_result = SimpleNamespace(retcode=NO_MONEY, comment="Not enough money")
        outcome = broker.open_position("EURUSD", Side.BUY, 500.0)
        assert outcome.ok is False
        assert "pre-check" in outcome.comment
        assert fake.requests == []  # nothing was sent


class TestPositions:
    def test_only_this_bots_positions_are_returned(self, broker: Mt5Broker, fake: FakeMt5) -> None:
        """A position opened by hand, or by another strategy, is not ours to manage."""
        fake.open_positions = [
            _raw_position(1, magic=234000),
            _raw_position(2, magic=999999),
        ]
        tickets = [p.ticket for p in broker.positions("EURUSD")]
        assert tickets == [1]

    def test_direction_and_stops_are_translated(self, broker: Mt5Broker, fake: FakeMt5) -> None:
        fake.open_positions = [_raw_position(1, position_type=1, sl=1.085, tp=1.070)]
        position = broker.positions("EURUSD")[0]
        assert position.side is Side.SELL
        assert position.sl == pytest.approx(1.085)

    def test_absent_stops_become_none_not_zero(self, broker: Mt5Broker, fake: FakeMt5) -> None:
        fake.open_positions = [_raw_position(1, sl=0.0, tp=0.0)]
        position = broker.positions("EURUSD")[0]
        assert position.sl is None and position.tp is None

    def test_a_vanished_position_is_recovered_from_deal_history(
        self, broker: Mt5Broker, fake: FakeMt5
    ) -> None:
        """A broker-side stop loss is invisible otherwise.

        This is what the original could not see: it kept the ticket in a
        variable and went on believing it held a position that had gone.
        """
        fake.open_positions = [_raw_position(1)]
        assert len(broker.positions("EURUSD")) == 1

        fake.open_positions = []
        fake.deals = (
            SimpleNamespace(
                entry=0, price=1.0800, time=1_700_000_000, profit=0.0, commission=0.0, swap=0.0
            ),
            SimpleNamespace(
                entry=1, price=1.0775, time=1_700_003_600, profit=-25.0, commission=-0.7, swap=-0.3
            ),
        )
        assert broker.positions("EURUSD") == []

        closed = broker.drain_closed_trades()
        assert len(closed) == 1
        assert closed[0].price_close == pytest.approx(1.0775)
        # Commission and swap are what make it the number that hit the balance.
        assert closed[0].profit == pytest.approx(-26.0)
        assert broker.drain_closed_trades() == []  # drained

    def test_a_vanished_position_with_no_history_is_not_invented(
        self, broker: Mt5Broker, fake: FakeMt5
    ) -> None:
        fake.open_positions = [_raw_position(1)]
        broker.positions("EURUSD")
        fake.open_positions = []
        fake.deals = ()
        broker.positions("EURUSD")
        assert broker.drain_closed_trades() == []


def _raw_position(ticket: int, **overrides: Any) -> Any:
    fields = {
        "ticket": ticket,
        "symbol": "EURUSD",
        "type": overrides.pop("position_type", 0),
        "volume": 0.1,
        "price_open": 1.0800,
        "sl": 1.0775,
        "tp": 1.0850,
        "profit": 1.5,
        "magic": 234000,
        "time": 1_700_000_000,
        "comment": "test",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)
