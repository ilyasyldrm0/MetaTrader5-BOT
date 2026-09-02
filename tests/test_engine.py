"""Engine tests.

The strategy is stubbed here on purpose. What is under test is the loop around
it: when it acts, what it believes is open, and what it refuses to do when a
broker answers badly. Those are where the original's real damage was.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from mt5bot.brokers.paper import PaperBroker
from mt5bot.config import Config, RiskConfig, TradingConfig
from mt5bot.engine import MAX_CONSECUTIVE_ERRORS, Engine
from mt5bot.errors import BrokerError
from mt5bot.journal import TradeJournal
from mt5bot.models import OrderResult, Position, Side, Signal, SymbolSpec
from mt5bot.strategy.base import Evaluation, Strategy

START = datetime(2024, 1, 1)


def flat_feed(bars: int, price: float = 1.08000) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "time": [START + timedelta(minutes=15 * i) for i in range(bars)],
            "open": price,
            "high": price + 0.00010,
            "low": price - 0.00010,
            "close": price,
            "volume": 100.0,
        }
    )


@dataclass
class ScriptedStrategy(Strategy):
    """Emits a prepared list of signals, one per evaluation.

    Lets the loop be tested against an exact sequence of decisions instead of
    whatever an indicator happens to produce.
    """

    script: list[Signal | None] = field(default_factory=list)
    calls: int = 0
    bars_seen: list[int] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def min_bars(self) -> int:
        return 3

    def evaluate(self, bars: pd.DataFrame) -> Evaluation:
        self.bars_seen.append(len(bars))
        step = self.calls
        self.calls += 1
        signal = self.script[step] if step < len(self.script) else None
        return Evaluation(signal, {"rsi": 50.0, "sma": 1.08, "close": 1.08})


BUY = Signal(Side.BUY, "scripted buy")
SELL = Signal(Side.SELL, "scripted sell")


def make_config(**overrides: object) -> Config:
    """Defaults suited to a test: fixed lots, no journal, no log directory."""
    base = Config(
        trading=TradingConfig(symbol="EURUSD", timeframe="M15", poll_seconds=1),
        risk=RiskConfig(sizing="fixed", fixed_lot=0.10, sl_pips=25.0, tp_pips=50.0),
        log_dir=None,
        journal_path=None,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def engine_over(
    feed: pd.DataFrame,
    spec: SymbolSpec,
    script: list[Signal | None],
    *,
    broker: PaperBroker | None = None,
    config: Config | None = None,
    journal: TradeJournal | None = None,
) -> tuple[Engine, PaperBroker, ScriptedStrategy]:
    paper = broker or PaperBroker(feed, spec, spread_pips=1.0, warmup=3)
    strategy = ScriptedStrategy(script)
    engine = Engine(
        broker=paper,
        strategy=strategy,
        config=config or make_config(),
        journal=journal or TradeJournal(None),
        sleep=lambda _seconds: None,
    )
    return engine, paper, strategy


class MisbehavingBroker(PaperBroker):
    """A paper broker that can be told to fail in specific ways."""

    fail_close = False
    close_silently_leaves_it_open = False
    raise_on_bars = 0

    def close_position(self, position: Position, *, comment: str = "") -> OrderResult:
        if self.fail_close:
            return OrderResult.failure("broker said no")
        if self.close_silently_leaves_it_open:
            # Reports success and changes nothing -- the shape of a partial
            # fill, or a close racing a broker-side stop.
            return OrderResult(ok=True, ticket=position.ticket, comment="lying")
        return super().close_position(position, comment=comment)

    def bars(self, symbol: str, timeframe: object, count: int) -> pd.DataFrame:  # type: ignore[override]
        if self.raise_on_bars > 0:
            self.raise_on_bars -= 1
            raise BrokerError("simulated disconnection")
        return super().bars(symbol, timeframe, count)  # type: ignore[arg-type]


class TestBasicLoop:
    def test_a_signal_opens_a_position(self, eurusd: SymbolSpec) -> None:
        engine, paper, _ = engine_over(flat_feed(20), eurusd, [None, BUY])
        stats = engine.run(max_cycles=4)

        assert stats.orders_opened == 1
        assert len(paper.positions()) == 1
        assert paper.positions()[0].side is Side.BUY

    def test_stops_when_the_feed_is_exhausted(self, eurusd: SymbolSpec) -> None:
        engine, _, _ = engine_over(flat_feed(8), eurusd, [])
        stats = engine.run(max_cycles=1000)
        assert stats.cycles < 1000  # it stopped on its own

    def test_request_stop_ends_the_loop(self, eurusd: SymbolSpec) -> None:
        engine, _, _ = engine_over(flat_feed(50), eurusd, [])
        engine.sleep = lambda _s: engine.request_stop()
        stats = engine.run()
        assert stats.cycles == 1

    def test_the_strategy_receives_the_history_it_asked_for(self, eurusd: SymbolSpec) -> None:
        engine, _, strategy = engine_over(flat_feed(20), eurusd, [])
        engine.run(max_cycles=3)
        assert strategy.bars_seen and set(strategy.bars_seen) == {strategy.min_bars}

    def test_the_same_bar_is_never_evaluated_twice(self, eurusd: SymbolSpec) -> None:
        """A poll that finds no new closed bar must decide nothing.

        The original re-evaluated the forming candle every ten seconds, so one
        15-minute bar produced ninety chances to trade on a number that had
        not settled yet.
        """

        class StuckClock(PaperBroker):
            def on_cycle(self) -> bool:
                return True  # never advances

        stuck = StuckClock(flat_feed(20), eurusd, spread_pips=1.0, warmup=5)
        engine, _, strategy = engine_over(flat_feed(20), eurusd, [], broker=stuck)
        stats = engine.run(max_cycles=6)

        assert stats.cycles == 6
        assert stats.bars_processed == 1
        assert strategy.calls == 1


class TestPositionHandling:
    def test_a_repeated_signal_does_not_stack_positions(self, eurusd: SymbolSpec) -> None:
        engine, paper, _ = engine_over(flat_feed(20), eurusd, [BUY, BUY, BUY])
        stats = engine.run(max_cycles=5)

        assert stats.orders_opened == 1
        assert len(paper.positions()) == 1

    def test_an_opposite_signal_closes_then_reverses(self, eurusd: SymbolSpec) -> None:
        engine, paper, _ = engine_over(flat_feed(20), eurusd, [BUY, SELL])
        stats = engine.run(max_cycles=4)

        assert stats.orders_opened == 2
        assert stats.orders_closed == 1
        assert paper.positions()[0].side is Side.SELL

    def test_a_failed_close_does_not_open_the_reverse(self, eurusd: SymbolSpec) -> None:
        """The bug that could leave an account hedged and double-margined.

        The original called close_position(), ignored the outcome -- it had no
        outcome to ignore, since the request was malformed -- and opened the
        opposite position regardless.
        """
        broker = MisbehavingBroker(flat_feed(20), eurusd, spread_pips=1.0, warmup=3)
        broker.fail_close = True
        engine, _, _ = engine_over(flat_feed(20), eurusd, [BUY, SELL], broker=broker)

        stats = engine.run(max_cycles=3)

        assert stats.orders_opened == 1  # the BUY only; the reverse never went out
        assert stats.orders_rejected >= 1
        assert len(broker.positions()) == 1
        assert broker.positions()[0].side is Side.BUY  # still the original

    def test_a_close_that_reports_success_but_changes_nothing_is_caught(
        self, eurusd: SymbolSpec
    ) -> None:
        """Trusting the return value alone is not enough; the state is re-read."""
        broker = MisbehavingBroker(flat_feed(20), eurusd, spread_pips=1.0, warmup=3)
        broker.close_silently_leaves_it_open = True
        engine, _, _ = engine_over(flat_feed(20), eurusd, [BUY, SELL], broker=broker)

        stats = engine.run(max_cycles=3)

        assert stats.orders_opened == 1
        assert len(broker.positions()) == 1
        assert broker.positions()[0].side is Side.BUY

    def test_a_broker_side_stop_is_noticed_and_journalled(
        self, eurusd: SymbolSpec, tmp_path: Path
    ) -> None:
        """The failure the original could not even see.

        It held position_id in a variable. When a stop filled at the broker the
        variable stayed set, so the bot believed it was still long.
        """
        price = 1.08000
        feed = flat_feed(20, price)
        # A bar deep enough to take out a 25-pip stop below the entry.
        feed.loc[8, ["open", "high", "low", "close"]] = [price, price, 1.07500, 1.07600]

        journal_path = tmp_path / "journal.csv"
        engine, paper, _ = engine_over(feed, eurusd, [BUY], journal=TradeJournal(journal_path))
        stats = engine.run(max_cycles=10)

        assert stats.orders_opened == 1
        assert paper.positions() == []
        assert paper.closed_trades[0].exit_reason == "sl"

        rows = journal_path.read_text().splitlines()
        assert len(rows) == 2  # header plus the one trade
        assert "sl" in rows[1] and "BUY" in rows[1]

    def test_it_becomes_tradeable_again_after_a_stop(self, eurusd: SymbolSpec) -> None:
        """Being stopped out must not wedge the bot into thinking it is still in."""
        price = 1.08000
        feed = flat_feed(24, price)
        feed.loc[8, ["open", "high", "low", "close"]] = [price, price, 1.07500, price]

        engine, paper, _ = engine_over(feed, eurusd, [BUY, None, None, None, None, None, BUY])
        stats = engine.run(max_cycles=12)

        assert stats.orders_opened == 2
        assert len(paper.positions()) == 1


class TestGuards:
    def test_a_wide_spread_blocks_entry(self, eurusd: SymbolSpec) -> None:
        broker = PaperBroker(flat_feed(20), eurusd, spread_pips=10.0, warmup=3)
        config = make_config(risk=RiskConfig(sizing="fixed", fixed_lot=0.1, max_spread_pips=3.0))
        engine, _, _ = engine_over(flat_feed(20), eurusd, [BUY], broker=broker, config=config)
        stats = engine.run(max_cycles=4)

        assert stats.signals == 1
        assert stats.orders_opened == 0
        assert stats.skipped_spread == 1

    def test_the_spread_check_can_be_switched_off(self, eurusd: SymbolSpec) -> None:
        broker = PaperBroker(flat_feed(20), eurusd, spread_pips=10.0, warmup=3)
        config = make_config(risk=RiskConfig(sizing="fixed", fixed_lot=0.1, max_spread_pips=None))
        engine, _, _ = engine_over(flat_feed(20), eurusd, [BUY], broker=broker, config=config)
        assert engine.run(max_cycles=4).orders_opened == 1

    def test_the_daily_cap_stops_a_flip_flopping_strategy(self, eurusd: SymbolSpec) -> None:
        config = make_config(risk=RiskConfig(sizing="fixed", fixed_lot=0.1, max_trades_per_day=2))
        engine, _, _ = engine_over(
            flat_feed(40), eurusd, [BUY, SELL, BUY, SELL, BUY], config=config
        )
        stats = engine.run(max_cycles=10)

        assert stats.orders_opened == 2
        assert stats.skipped_daily_cap >= 1

    def test_a_size_below_the_brokers_minimum_is_skipped_not_rounded_up(
        self, eurusd: SymbolSpec
    ) -> None:
        broker = PaperBroker(flat_feed(20), eurusd, balance=10.0, spread_pips=1.0, warmup=3)
        config = make_config(risk=RiskConfig(sizing="risk", risk_percent=0.1, sl_pips=100.0))
        engine, _, _ = engine_over(flat_feed(20), eurusd, [BUY], broker=broker, config=config)
        stats = engine.run(max_cycles=4)

        assert stats.signals == 1
        assert stats.orders_opened == 0

    def test_risk_sizing_uses_the_account_balance(self, eurusd: SymbolSpec) -> None:
        broker = PaperBroker(flat_feed(20), eurusd, balance=10_000.0, spread_pips=1.0, warmup=3)
        config = make_config(risk=RiskConfig(sizing="risk", risk_percent=1.0, sl_pips=25.0))
        engine, _, _ = engine_over(flat_feed(20), eurusd, [BUY], broker=broker, config=config)
        engine.run(max_cycles=4)

        assert broker.positions()[0].volume == pytest.approx(0.40)

    def test_stops_are_placed_the_configured_distance_away(self, eurusd: SymbolSpec) -> None:
        engine, paper, _ = engine_over(flat_feed(20), eurusd, [BUY])
        engine.run(max_cycles=3)

        position = paper.positions()[0]
        assert position.sl is not None and position.tp is not None
        # 25 pips below the entry and 50 above -- not 25 and 50 in raw price.
        assert position.price_open - position.sl == pytest.approx(0.0025)
        assert position.tp - position.price_open == pytest.approx(0.0050)


class TestResilience:
    def test_a_transient_failure_is_retried(self, eurusd: SymbolSpec) -> None:
        broker = MisbehavingBroker(flat_feed(30), eurusd, spread_pips=1.0, warmup=3)
        broker.raise_on_bars = 2
        engine, _, _ = engine_over(flat_feed(30), eurusd, [None, None, BUY], broker=broker)
        stats = engine.run(max_cycles=8)

        assert stats.errors == 2
        assert stats.orders_opened == 1  # it recovered and carried on

    def test_it_gives_up_after_too_many_consecutive_failures(self, eurusd: SymbolSpec) -> None:
        broker = MisbehavingBroker(flat_feed(80), eurusd, spread_pips=1.0, warmup=3)
        broker.raise_on_bars = 999
        engine, _, _ = engine_over(flat_feed(80), eurusd, [], broker=broker)
        stats = engine.run(max_cycles=60)

        assert stats.errors == MAX_CONSECUTIVE_ERRORS
        assert stats.cycles == MAX_CONSECUTIVE_ERRORS

    def test_backoff_grows_between_failures(self, eurusd: SymbolSpec) -> None:
        broker = MisbehavingBroker(flat_feed(40), eurusd, spread_pips=1.0, warmup=3)
        broker.raise_on_bars = 3
        waits: list[float] = []
        engine, _, _ = engine_over(flat_feed(40), eurusd, [], broker=broker)
        engine.sleep = waits.append
        engine.run(max_cycles=5)

        assert waits[:3] == sorted(waits[:3]) and waits[0] < waits[2]

    def test_the_broker_is_shut_down_even_when_a_cycle_explodes(self, eurusd: SymbolSpec) -> None:
        class Exploding(PaperBroker):
            shut_down = False

            def shutdown(self) -> None:
                self.shut_down = True

            def positions(self, symbol: str | None = None) -> list[Position]:
                raise RuntimeError("something unexpected")

        broker = Exploding(flat_feed(20), eurusd, warmup=3)
        engine, _, _ = engine_over(flat_feed(20), eurusd, [], broker=broker)

        with pytest.raises(RuntimeError, match="something unexpected"):
            engine.run(max_cycles=3)
        assert broker.shut_down is True


class TestReporting:
    def test_stats_summarise_the_run(self, eurusd: SymbolSpec) -> None:
        engine, _, _ = engine_over(flat_feed(20), eurusd, [BUY, SELL])
        summary = engine.run(max_cycles=5).summary()
        assert "opened" in summary and "signals" in summary

    def test_an_open_position_is_reported_at_shutdown_not_closed(
        self, eurusd: SymbolSpec, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Liquidating on exit would turn a Ctrl-C into a realised loss."""
        engine, paper, _ = engine_over(flat_feed(20), eurusd, [BUY])
        with caplog.at_level("WARNING"):
            engine.run(max_cycles=3)

        assert len(paper.positions()) == 1
        assert "still open" in caplog.text
