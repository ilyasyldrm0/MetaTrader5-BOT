"""Backtester tests: the metric arithmetic, and what the sample data shows."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mt5bot.backtest import BacktestResult, format_report, run_backtest
from mt5bot.brokers.paper import synthetic_feed
from mt5bot.config import Config, RiskConfig, StrategyConfig
from mt5bot.data import load_csv
from mt5bot.journal import TradeJournal
from mt5bot.models import ClosedTrade, Side

SAMPLE = Path(__file__).parent / "data" / "eurusd_m15_sample.csv"
START = datetime(2024, 1, 1)


def trade(profit: float, index: int = 0, exit_reason: str = "tp") -> ClosedTrade:
    return ClosedTrade(
        ticket=index + 1,
        symbol="EURUSD",
        side=Side.BUY,
        volume=0.1,
        price_open=1.08,
        price_close=1.08 + profit / 1000,
        time_open=START + timedelta(hours=index),
        time_close=START + timedelta(hours=index, minutes=30),
        profit=profit,
        exit_reason=exit_reason,
    )


def result_of(profits: list[float], *, balance: float = 10_000.0) -> BacktestResult:
    trades = [trade(p, i, "tp" if p > 0 else "sl") for i, p in enumerate(profits)]
    return BacktestResult(
        symbol="EURUSD",
        timeframe="M15",
        strategy="test",
        bars=1000,
        period_start=START,
        period_end=START + timedelta(days=10),
        starting_balance=balance,
        ending_balance=balance + sum(profits),
        trades=trades,
    )


class TestMetrics:
    def test_net_profit_and_return(self) -> None:
        r = result_of([100.0, -40.0, 60.0])
        assert r.net_profit == pytest.approx(120.0)
        assert r.return_percent == pytest.approx(1.2)

    def test_win_rate_counts_a_breakeven_trade_as_a_loss(self) -> None:
        """Zero is not a win: it paid the spread and returned nothing."""
        r = result_of([100.0, 0.0, -50.0, 20.0])
        assert len(r.wins) == 2
        assert r.win_rate == pytest.approx(50.0)

    def test_profit_factor(self) -> None:
        r = result_of([100.0, 50.0, -60.0])
        assert r.gross_profit == pytest.approx(150.0)
        assert r.gross_loss == pytest.approx(60.0)
        assert r.profit_factor == pytest.approx(2.5)

    def test_profit_factor_is_none_when_nothing_was_lost(self) -> None:
        """Dividing by zero would report ``inf``, which reads as a real figure."""
        assert result_of([10.0, 20.0]).profit_factor is None

    def test_averages_and_expectancy(self) -> None:
        r = result_of([100.0, 50.0, -60.0, -40.0])
        assert r.average_win == pytest.approx(75.0)
        assert r.average_loss == pytest.approx(-50.0)
        assert r.expectancy == pytest.approx(12.5)

    def test_max_drawdown_measures_peak_to_trough(self) -> None:
        """Up to 10,300, down to 9,900, back up: the drawdown is 400, not 300."""
        r = result_of([300.0, -200.0, -200.0, 500.0])
        assert r.max_drawdown == pytest.approx(400.0)
        assert r.max_drawdown_percent == pytest.approx(400 / 10_300 * 100)

    def test_drawdown_is_zero_for_a_run_that_only_climbs(self) -> None:
        assert result_of([10.0, 20.0, 30.0]).max_drawdown == pytest.approx(0.0)

    def test_equity_curve_starts_at_the_opening_balance(self) -> None:
        curve = result_of([100.0, -50.0]).equity_curve
        assert curve == pytest.approx([10_000.0, 10_100.0, 10_050.0])

    def test_exit_breakdown_counts_each_reason(self) -> None:
        r = result_of([10.0, -10.0, 20.0])
        assert r.exit_breakdown == {"sl": 1, "tp": 2}

    def test_an_empty_run_reports_zeroes_rather_than_dividing_by_zero(self) -> None:
        r = result_of([])
        assert r.win_rate == 0.0
        assert r.expectancy == 0.0
        assert r.max_drawdown == 0.0
        assert r.profit_factor is None
        assert r.signals_per_1000_bars == 0.0


class TestRunBacktest:
    def test_the_defaults_produce_no_signals_at_all(self) -> None:
        """The finding this whole exercise turns on, pinned as a test.

        Over 4,000 bars the original conditions fire zero times. Not rarely --
        never. Whatever pushes RSI(14) below 30 has already pushed price under
        a 12-period average, so requiring both at once asks for a state that
        essentially does not occur.

        Reading this and concluding the bot is broken would be the wrong
        lesson: it is behaving exactly as configured. The point of the number
        is that the configuration can now be argued about.
        """
        result = run_backtest(load_csv(SAMPLE), Config())
        assert result.stats.bars_processed > 3_000
        assert result.stats.signals == 0
        assert result.trades == []

    def test_relaxing_the_filter_produces_plenty(self) -> None:
        config = replace(Config(), strategy=StrategyConfig(trend_filter="contrarian"))
        result = run_backtest(load_csv(SAMPLE), config)
        assert result.stats.signals > 100
        assert len(result.trades) > 10

    def test_the_two_relaxed_filters_agree_on_this_data(self) -> None:
        """'contrarian' and 'off' being identical *is* the finding.

        If disabling the moving average changes nothing, then every oversold
        bar already had price below the average -- which is the same fact the
        zero-signal default reports, seen from the other side.
        """
        feed = load_csv(SAMPLE)
        contrarian = run_backtest(
            feed, replace(Config(), strategy=StrategyConfig(trend_filter="contrarian"))
        )
        off = run_backtest(feed, replace(Config(), strategy=StrategyConfig(trend_filter="off")))
        assert contrarian.stats.signals == off.stats.signals

    def test_costs_reduce_the_result(self) -> None:
        """A zero-cost backtest is the standard way to flatter a losing strategy."""
        feed = load_csv(SAMPLE)
        base = replace(Config(), strategy=StrategyConfig(trend_filter="off"))
        free = run_backtest(feed, replace(base, backtest=replace(base.backtest, spread_pips=0.0)))
        costly = run_backtest(
            feed,
            replace(base, backtest=replace(base.backtest, spread_pips=2.0, commission_per_lot=7.0)),
        )
        assert costly.net_profit < free.net_profit

    def test_records_the_period_and_bar_count(self) -> None:
        result = run_backtest(load_csv(SAMPLE), Config())
        assert result.bars == 4000
        assert result.period_start < result.period_end

    def test_a_feed_shorter_than_the_warmup_is_refused(self) -> None:
        with pytest.raises(ValueError, match="needs \\d+ bars"):
            run_backtest(synthetic_feed(bars=20), Config())

    def test_it_writes_to_a_journal_when_given_one(self, tmp_path: Path) -> None:
        path = tmp_path / "bt.csv"
        config = replace(Config(), strategy=StrategyConfig(trend_filter="off"))
        result = run_backtest(load_csv(SAMPLE), config, journal=TradeJournal(path))

        rows = path.read_text().splitlines()
        assert len(rows) == len(result.trades) + 1  # plus the header
        assert rows[0].startswith("time_open,time_close,ticket")

    def test_an_unfinished_position_is_reported_not_invented(self) -> None:
        """Closing it at the last price would be making up a fill that never was."""
        config = replace(
            Config(),
            strategy=StrategyConfig(trend_filter="off"),
            risk=RiskConfig(sl_pips=500.0, tp_pips=500.0, sizing="fixed", fixed_lot=0.1),
        )
        result = run_backtest(load_csv(SAMPLE), config)
        assert result.still_open is not None
        assert result.still_open.ticket not in {t.ticket for t in result.trades}


class TestReport:
    def test_a_run_with_no_signals_explains_itself(self) -> None:
        config = Config()
        text = format_report(run_backtest(load_csv(SAMPLE), config), config)
        assert "never signalled" in text
        assert "contrarian" in text  # it names the setting to try

    def test_a_run_with_trades_reports_the_metrics(self) -> None:
        config = replace(Config(), strategy=StrategyConfig(trend_filter="off"))
        text = format_report(run_backtest(load_csv(SAMPLE), config), config)
        for heading in ("ACTIVITY", "RESULT", "TRADE QUALITY"):
            assert heading in text
        assert "Win rate" in text and "Max drawdown" in text and "Profit factor" in text

    def test_it_always_states_the_costs_that_were_assumed(self) -> None:
        config = Config()
        assert "spread" in format_report(run_backtest(load_csv(SAMPLE), config), config)

    def test_a_thin_sample_is_flagged_as_such(self) -> None:
        """Twelve trades is not evidence, and the report must not imply it is."""
        config = replace(Config(), strategy=StrategyConfig(trend_filter="off"))
        result = run_backtest(load_csv(SAMPLE), config)
        thin = replace(result, trades=result.trades[:12])
        assert "CAUTION" in format_report(thin, config)
        assert "too few" in format_report(thin, config)
