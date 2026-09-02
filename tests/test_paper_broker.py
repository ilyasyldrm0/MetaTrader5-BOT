"""Paper broker tests.

The backtester's numbers are only worth as much as this simulation, so the
awkward cases get pinned down here: which side of the spread each direction
pays, what happens when a bar touches both the stop and the target, and what a
gap does to a stop that the market jumped over.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from mt5bot.brokers.paper import DryRunBroker, PaperBroker, infer_timeframe, synthetic_feed
from mt5bot.errors import BrokerError, DataUnavailableError
from mt5bot.models import Side, SymbolSpec, Timeframe

START = datetime(2024, 1, 1, 0, 0)


def feed_from(rows: list[tuple[float, float, float, float]], minutes: int = 15) -> pd.DataFrame:
    """Build a feed from ``(open, high, low, close)`` tuples."""
    return pd.DataFrame(
        {
            "time": [START + timedelta(minutes=minutes * i) for i in range(len(rows))],
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [100.0] * len(rows),
        }
    )


FLAT = (1.08000, 1.08010, 1.07990, 1.08000)


def broker(
    rows: list[tuple[float, float, float, float]], spec: SymbolSpec, **kw: object
) -> PaperBroker:
    return PaperBroker(feed_from(rows), spec, **kw)  # type: ignore[arg-type]


class TestFeedValidation:
    def test_rejects_a_feed_missing_columns(self, eurusd: SymbolSpec) -> None:
        bad = pd.DataFrame({"time": [START], "close": [1.0]})
        with pytest.raises(BrokerError, match="missing required columns"):
            PaperBroker(bad, eurusd)

    def test_rejects_a_feed_too_short_to_step_through(self, eurusd: SymbolSpec) -> None:
        with pytest.raises(BrokerError, match="need at least 2"):
            PaperBroker(feed_from([FLAT]), eurusd)

    def test_rejects_a_symbol_it_does_not_hold(self, eurusd: SymbolSpec) -> None:
        b = broker([FLAT] * 5, eurusd)
        with pytest.raises(BrokerError, match="holds 'EURUSD'"):
            b.tick("GBPUSD")


class TestClock:
    def test_on_cycle_advances_one_bar_and_stops_at_the_end(self, eurusd: SymbolSpec) -> None:
        b = broker([FLAT] * 4, eurusd)
        assert b.index == 0
        assert b.on_cycle() is True
        assert b.index == 1
        assert b.on_cycle() is True and b.on_cycle() is True
        assert b.index == 3
        assert b.finished is True
        assert b.on_cycle() is False

    def test_warmup_starts_the_clock_further_in(self, eurusd: SymbolSpec) -> None:
        b = broker([FLAT] * 10, eurusd, warmup=5)
        assert b.index == 5
        b.on_cycle()
        assert b.index == 6

    def test_bars_returns_the_window_ending_at_the_current_bar(self, eurusd: SymbolSpec) -> None:
        rows = [(1.0 + i / 1000, 1.0 + i / 1000, 1.0 + i / 1000, 1.0 + i / 1000) for i in range(10)]
        b = broker(rows, eurusd, warmup=5)
        window = b.bars("EURUSD", Timeframe.M15, 3)
        assert len(window) == 3
        assert window["close"].iloc[-1] == pytest.approx(1.005)  # bar 5, the current one

    def test_bars_refuses_before_enough_history_exists(self, eurusd: SymbolSpec) -> None:
        b = broker([FLAT] * 10, eurusd)
        with pytest.raises(DataUnavailableError, match="needed 5"):
            b.bars("EURUSD", Timeframe.M15, 5)

    def test_bars_rejects_a_timeframe_the_feed_is_not(self, eurusd: SymbolSpec) -> None:
        """An M15 config against an H1 export would silently mean 4x the span."""
        b = broker([FLAT] * 10, eurusd, warmup=5)
        with pytest.raises(BrokerError, match="M15 data but H1 was requested"):
            b.bars("EURUSD", Timeframe.H1, 3)


class TestSpreadAndFills:
    def test_tick_puts_the_ask_one_spread_above_the_bar(self, eurusd: SymbolSpec) -> None:
        b = broker([FLAT] * 4, eurusd, spread_pips=1.0)
        b.on_cycle()
        tick = b.tick("EURUSD")
        assert tick.bid == pytest.approx(1.08000)
        assert tick.ask == pytest.approx(1.08010)

    def test_a_long_pays_the_ask(self, eurusd: SymbolSpec) -> None:
        b = broker([FLAT] * 4, eurusd, spread_pips=1.0)
        b.on_cycle()
        result = b.open_position("EURUSD", Side.BUY, 0.1)
        assert result.ok
        assert result.price == pytest.approx(1.08010)

    def test_a_short_receives_the_bid(self, eurusd: SymbolSpec) -> None:
        b = broker([FLAT] * 4, eurusd, spread_pips=1.0)
        b.on_cycle()
        result = b.open_position("EURUSD", Side.SELL, 0.1)
        assert result.ok
        assert result.price == pytest.approx(1.08000)

    def test_slippage_always_works_against_the_trader(self, eurusd: SymbolSpec) -> None:
        b = broker([FLAT] * 4, eurusd, spread_pips=1.0, slippage_pips=0.5)
        b.on_cycle()
        assert b.open_position("EURUSD", Side.BUY, 0.1).price == pytest.approx(1.08015)

        b2 = broker([FLAT] * 4, eurusd, spread_pips=1.0, slippage_pips=0.5)
        b2.on_cycle()
        assert b2.open_position("EURUSD", Side.SELL, 0.1).price == pytest.approx(1.07995)

    def test_only_one_position_at_a_time(self, eurusd: SymbolSpec) -> None:
        b = broker([FLAT] * 4, eurusd)
        b.on_cycle()
        assert b.open_position("EURUSD", Side.BUY, 0.1).ok
        second = b.open_position("EURUSD", Side.BUY, 0.1)
        assert second.ok is False
        assert "already open" in second.comment

    def test_zero_volume_is_refused(self, eurusd: SymbolSpec) -> None:
        b = broker([FLAT] * 4, eurusd)
        b.on_cycle()
        assert b.open_position("EURUSD", Side.BUY, 0.0).ok is False


class TestStops:
    def test_a_long_is_stopped_out_at_its_level(self, eurusd: SymbolSpec) -> None:
        rows = [FLAT, FLAT, (1.08000, 1.08010, 1.07700, 1.07750), FLAT]
        b = broker(rows, eurusd, spread_pips=1.0)
        b.on_cycle()  # -> bar 1
        b.open_position("EURUSD", Side.BUY, 0.1, sl=1.07800, tp=1.08500)
        b.on_cycle()  # -> bar 2, low 1.07700 takes out the stop

        assert b.positions() == []
        assert len(b.closed_trades) == 1
        trade = b.closed_trades[0]
        assert trade.exit_reason == "sl"
        assert trade.price_close == pytest.approx(1.07800)
        # Entered at the ask 1.08010, out at 1.07800: 21 pips on 0.1 lots.
        assert trade.profit == pytest.approx(-21.0)

    def test_a_long_takes_profit_at_its_level(self, eurusd: SymbolSpec) -> None:
        rows = [FLAT, FLAT, (1.08000, 1.08600, 1.07990, 1.08550), FLAT]
        b = broker(rows, eurusd, spread_pips=1.0)
        b.on_cycle()
        b.open_position("EURUSD", Side.BUY, 0.1, sl=1.07800, tp=1.08500)
        b.on_cycle()

        trade = b.closed_trades[0]
        assert trade.exit_reason == "tp"
        assert trade.price_close == pytest.approx(1.08500)
        assert trade.profit == pytest.approx(49.0)

    def test_a_short_is_stopped_on_the_ask_not_the_bar_high(self, eurusd: SymbolSpec) -> None:
        """The bar high is a bid. A short is closed at the ask, a spread higher.

        With a 1-pip spread a stop at 1.08150 is reached by a bar whose high is
        only 1.08145 -- ignoring that reports shorts as more profitable than
        they are.
        """
        rows = [FLAT, FLAT, (1.08000, 1.08145, 1.07990, 1.08100), FLAT]
        b = broker(rows, eurusd, spread_pips=1.0)
        b.on_cycle()
        b.open_position("EURUSD", Side.SELL, 0.1, sl=1.08150, tp=1.07500)
        b.on_cycle()

        assert len(b.closed_trades) == 1
        assert b.closed_trades[0].exit_reason == "sl"

    def test_a_short_takes_profit_on_the_ask(self, eurusd: SymbolSpec) -> None:
        """Symmetrically, a short's target needs the bar low one spread lower.

        Closing a short means buying, and you buy at the ask. For a target of
        1.07500 the *bid* -- which is what the bar quotes -- has to reach
        1.07490, exactly one pip further than the level suggests.
        """
        rows = [FLAT, FLAT, (1.08000, 1.08010, 1.07490, 1.07500), FLAT]
        b = broker(rows, eurusd, spread_pips=1.0)
        b.on_cycle()
        b.open_position("EURUSD", Side.SELL, 0.1, sl=1.08500, tp=1.07500)
        b.on_cycle()

        assert b.closed_trades[0].exit_reason == "tp"
        assert b.closed_trades[0].price_close == pytest.approx(1.07500)

    def test_a_short_target_one_tick_short_is_not_filled(self, eurusd: SymbolSpec) -> None:
        """One tick above the previous case, and the fill must not happen.

        Pins the boundary from the other side: a simulator that fills a hair
        early credits profits the market never offered.
        """
        rows = [FLAT, FLAT, (1.08000, 1.08010, 1.07491, 1.07500), FLAT]
        b = broker(rows, eurusd, spread_pips=1.0)
        b.on_cycle()
        b.open_position("EURUSD", Side.SELL, 0.1, sl=1.08500, tp=1.07500)
        b.on_cycle()
        assert b.closed_trades == []

    def test_a_bar_touching_both_levels_takes_the_loss(self, eurusd: SymbolSpec) -> None:
        """Bars say nothing about the order prices were visited in.

        Assuming the target came first is the single easiest way to make a
        backtest report profits the strategy cannot reproduce.
        """
        rows = [FLAT, FLAT, (1.08000, 1.08600, 1.07700, 1.08000), FLAT]
        b = broker(rows, eurusd, spread_pips=1.0)
        b.on_cycle()
        b.open_position("EURUSD", Side.BUY, 0.1, sl=1.07800, tp=1.08500)
        b.on_cycle()

        assert b.closed_trades[0].exit_reason == "sl"

    def test_a_gap_through_the_stop_fills_at_the_open(self, eurusd: SymbolSpec) -> None:
        """A stop does not hold a price the market jumped straight over.

        The bar opens at 1.07500, well below the 1.07800 stop. Filling at the
        stop would pretend an exit that was never available.
        """
        rows = [FLAT, FLAT, (1.07500, 1.07550, 1.07400, 1.07450), FLAT]
        b = broker(rows, eurusd, spread_pips=1.0)
        b.on_cycle()
        b.open_position("EURUSD", Side.BUY, 0.1, sl=1.07800, tp=1.08500)
        b.on_cycle()

        trade = b.closed_trades[0]
        assert trade.exit_reason == "sl"
        assert trade.price_close == pytest.approx(1.07500)
        assert trade.profit < -21.0  # worse than the stop promised

    def test_a_gap_past_the_target_fills_at_the_open(self, eurusd: SymbolSpec) -> None:
        rows = [FLAT, FLAT, (1.08700, 1.08800, 1.08650, 1.08750), FLAT]
        b = broker(rows, eurusd, spread_pips=1.0)
        b.on_cycle()
        b.open_position("EURUSD", Side.BUY, 0.1, sl=1.07800, tp=1.08500)
        b.on_cycle()

        trade = b.closed_trades[0]
        assert trade.exit_reason == "tp"
        assert trade.price_close == pytest.approx(1.08700)

    def test_the_entry_bar_cannot_stop_the_position_out(self, eurusd: SymbolSpec) -> None:
        """No look-ahead: bar *i* is history by the time a signal from it trades.

        Bar 1 has a range wide enough to hit the stop, but the position is
        opened at its close, so the position did not exist while that range
        was being traded.
        """
        wide = (1.08000, 1.09000, 1.07000, 1.08000)
        b = broker([FLAT, wide, FLAT, FLAT], eurusd, spread_pips=1.0)
        b.on_cycle()  # -> bar 1, the wide one
        b.open_position("EURUSD", Side.BUY, 0.1, sl=1.07800, tp=1.08500)
        b.on_cycle()  # -> bar 2, narrow

        assert len(b.positions()) == 1
        assert b.closed_trades == []


class TestAccounting:
    def test_balance_moves_only_when_a_trade_closes(self, eurusd: SymbolSpec) -> None:
        rows = [FLAT, FLAT, (1.08000, 1.08600, 1.07990, 1.08550), FLAT]
        b = broker(rows, eurusd, balance=10_000.0, spread_pips=1.0)
        b.on_cycle()
        b.open_position("EURUSD", Side.BUY, 0.1, sl=1.07800, tp=1.08500)
        assert b.balance == pytest.approx(10_000.0)
        b.on_cycle()
        assert b.balance == pytest.approx(10_049.0)

    def test_equity_reflects_an_open_position(self, eurusd: SymbolSpec) -> None:
        rows = [FLAT, FLAT, (1.08200, 1.08300, 1.08100, 1.08200), FLAT]
        b = broker(rows, eurusd, balance=10_000.0, spread_pips=1.0)
        b.on_cycle()
        b.open_position("EURUSD", Side.BUY, 0.1)
        b.on_cycle()
        account = b.account()
        assert account.balance == pytest.approx(10_000.0)
        assert account.equity > account.balance

    def test_commission_is_charged_once_per_round_turn(self, eurusd: SymbolSpec) -> None:
        rows = [FLAT, FLAT, (1.08000, 1.08600, 1.07990, 1.08550), FLAT]
        b = broker(rows, eurusd, spread_pips=1.0, commission_per_lot=7.0)
        b.on_cycle()
        b.open_position("EURUSD", Side.BUY, 0.5, tp=1.08500)
        b.on_cycle()
        # 0.5 lots * 49 pips = 245.0, less 0.5 * $7 commission.
        assert b.closed_trades[0].profit == pytest.approx(245.0 - 3.5)

    def test_closing_by_signal_is_recorded_as_such(self, eurusd: SymbolSpec) -> None:
        """exit_reason is a category the report groups by, not free text.

        Threading the caller's comment into it gave every strategy-driven exit
        its own bucket, so the breakdown listed one row per RSI reading rather
        than three totals.
        """
        b = broker([FLAT] * 5, eurusd, spread_pips=1.0)
        b.on_cycle()
        b.open_position("EURUSD", Side.BUY, 0.1, comment="rsi flip")
        b.on_cycle()
        position = b.positions()[0]
        assert b.close_position(position, comment="RSI 71.2>=70").ok

        trade = b.closed_trades[0]
        assert trade.exit_reason == "signal"
        assert trade.entry_reason == "rsi flip"
        assert b.positions() == []

    def test_closing_an_unknown_ticket_is_refused(self, eurusd: SymbolSpec) -> None:
        from mt5bot.models import Position

        b = broker([FLAT] * 4, eurusd)
        b.on_cycle()
        ghost = Position(ticket=999, symbol="EURUSD", side=Side.BUY, volume=0.1, price_open=1.0)
        assert b.close_position(ghost).ok is False


class TestInferTimeframe:
    def test_reads_the_period_from_the_timestamps(self) -> None:
        assert infer_timeframe(feed_from([FLAT] * 5, minutes=15)) is Timeframe.M15
        assert infer_timeframe(feed_from([FLAT] * 5, minutes=60)) is Timeframe.H1

    def test_survives_a_weekend_gap(self) -> None:
        """The modal gap, not the mean -- one long break must not skew it."""
        feed = feed_from([FLAT] * 6, minutes=15)
        feed.loc[3:, "time"] = feed.loc[3:, "time"] + timedelta(days=2)
        assert infer_timeframe(feed) is Timeframe.M15

    def test_unknown_spacing_gives_none_rather_than_an_error(self) -> None:
        assert infer_timeframe(feed_from([FLAT] * 5, minutes=7)) is None

    def test_too_short_to_tell(self) -> None:
        assert infer_timeframe(feed_from([FLAT, FLAT])) is None


class TestSyntheticFeed:
    def test_is_deterministic(self) -> None:
        a = synthetic_feed(bars=50)
        b = synthetic_feed(bars=50)
        pd.testing.assert_frame_equal(a, b)

    def test_bars_are_internally_consistent(self) -> None:
        feed = synthetic_feed(bars=500)
        assert (feed["high"] >= feed[["open", "close"]].max(axis=1)).all()
        assert (feed["low"] <= feed[["open", "close"]].min(axis=1)).all()
        assert infer_timeframe(feed) is Timeframe.M15


class TestDryRunBroker:
    def test_reads_pass_through_but_orders_do_not_reach_the_inner_broker(
        self, eurusd: SymbolSpec
    ) -> None:
        inner = broker([FLAT] * 6, eurusd, spread_pips=1.0)
        inner.on_cycle()
        dry = DryRunBroker(inner)

        assert dry.tick("EURUSD").bid == pytest.approx(1.08000)
        assert dry.symbol_spec("EURUSD") is eurusd

        assert dry.open_position("EURUSD", Side.BUY, 0.1).ok
        assert len(dry.positions()) == 1
        assert inner.positions() == []  # the real broker saw nothing

    def test_balance_can_be_overridden_for_sizing(self, eurusd: SymbolSpec) -> None:
        inner = broker([FLAT] * 6, eurusd)
        inner.on_cycle()
        dry = DryRunBroker(inner, balance_override=50_000.0)
        assert dry.account().balance == pytest.approx(50_000.0)
        assert inner.account().balance == pytest.approx(10_000.0)

    def test_a_simulated_stop_fills_from_the_live_quote(self, eurusd: SymbolSpec) -> None:
        rows = [FLAT, FLAT, (1.07700, 1.07750, 1.07650, 1.07700), FLAT]
        inner = broker(rows, eurusd, spread_pips=1.0)
        inner.on_cycle()
        dry = DryRunBroker(inner)
        dry.open_position("EURUSD", Side.BUY, 0.1, sl=1.07800)

        dry.on_cycle()  # inner advances to bar 2, quote is now below the stop
        assert dry.positions() == []
        assert dry.closed_trades[0].exit_reason == "sl"

    def test_it_stops_when_the_inner_feed_runs_out(self, eurusd: SymbolSpec) -> None:
        inner = broker([FLAT] * 3, eurusd)
        dry = DryRunBroker(inner)
        assert dry.on_cycle() is True
        assert dry.on_cycle() is True
        assert dry.on_cycle() is False
