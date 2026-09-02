"""Strategy tests.

The interesting question is not whether the conditions are coded correctly --
it is what they mean. The defaults reproduce the original script, so the tests
here also document how demanding those defaults are, and show what each knob
changes.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from mt5bot.indicators import sma, wilder_rsi
from mt5bot.models import Side
from mt5bot.strategy import RsiSmaStrategy, TrendFilter

START = datetime(2024, 1, 1)


def bars_from(closes: list[float]) -> pd.DataFrame:
    """A minimal OHLC frame; only ``close`` matters to this strategy."""
    series = pd.Series(closes, dtype=float)
    return pd.DataFrame(
        {
            "time": [START + timedelta(minutes=15 * i) for i in range(len(closes))],
            "open": series,
            "high": series,
            "low": series,
            "close": series,
            "volume": 100.0,
        }
    )


def oscillating_warmup(bars: int = 40, amplitude: float = 0.5) -> list[float]:
    """Small alternating moves, leaving RSI near 50 before the real move.

    A flat warm-up will not do: with no up bars at all the average gain is
    exactly zero, so RSI is pinned to 0 and no fixture can land on an
    intermediate reading.
    """
    return [100.0 + (amplitude if i % 2 else -amplitude) for i in range(bars)]


def declining_from_warmup(step: float, count: int) -> list[float]:
    """A warm-up followed by ``count`` bars falling ``step`` each."""
    base = oscillating_warmup()
    return base + [base[-1] - i * step for i in range(1, count + 1)]


#: A steady decline: RSI is on the floor and price is far below its average.
#: This is what an oversold market almost always looks like.
STEADY_DECLINE = [100.0 - i for i in range(60)]

#: A decline, a pause, then a modest recovery -- the shape that satisfies both
#: halves of the original condition at once. RSI is still 17 while the last
#: close has just climbed back above the 12-bar average.
DIP_THEN_RECOVERY = [100.0 - i for i in range(40)] + [60.0] * 8 + [60.5, 61.0, 61.5]

STEADY_RALLY = [100.0 + i for i in range(60)]
RALLY_THEN_FADE = [100.0 + i for i in range(40)] + [140.0] * 8 + [139.5, 139.0, 138.5]


class TestConfiguration:
    def test_defaults_reproduce_the_original_script(self) -> None:
        s = RsiSmaStrategy()
        assert (s.rsi_period, s.sma_period) == (14, 12)
        assert (s.oversold, s.overbought) == (30.0, 70.0)
        assert s.trend_filter is TrendFilter.ALIGNED
        assert s.require_cross is False

    def test_min_bars_far_exceeds_the_original_window(self) -> None:
        """The original fetched rsi_period + sma_period, which is 26 bars."""
        assert RsiSmaStrategy().min_bars > 26

    def test_min_bars_grows_with_the_periods(self) -> None:
        assert RsiSmaStrategy(rsi_period=50).min_bars > RsiSmaStrategy(rsi_period=14).min_bars

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"rsi_period": 1}, "rsi_period must be at least 2"),
            ({"sma_period": 0}, "sma_period must be at least 1"),
            ({"oversold": 70.0, "overbought": 30.0}, "oversold < overbought"),
            ({"overbought": 101.0}, "overbought <= 100"),
        ],
    )
    def test_rejects_incoherent_settings(self, kwargs: dict[str, float], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            RsiSmaStrategy(**kwargs)  # type: ignore[arg-type]

    def test_describe_mentions_the_settings_that_matter(self) -> None:
        text = RsiSmaStrategy(require_cross=True).describe()
        assert "RSI(14)" in text and "SMA(12)" in text and "aligned" in text
        assert "crossings" in text


class TestWarmup:
    def test_no_signal_while_the_indicators_are_still_nan(self) -> None:
        result = RsiSmaStrategy().evaluate(bars_from([1.0 + i / 100 for i in range(10)]))
        assert result.signal is None

    def test_readings_are_reported_even_with_no_signal(self) -> None:
        result = RsiSmaStrategy().evaluate(bars_from(STEADY_RALLY))
        assert result.signal is None
        assert set(result.indicators) == {"rsi", "sma", "close"}
        assert result.indicators["rsi"] > 70  # a rally is overbought...


class TestTheOriginalConditions:
    def test_a_steady_decline_produces_no_buy(self) -> None:
        """Deeply oversold, and the default filter still refuses.

        This is the case that makes the original defaults so quiet: whatever
        drives RSI under 30 has almost always pushed price under its average
        too, and ALIGNED requires the opposite.
        """
        frame = bars_from(STEADY_DECLINE)
        result = RsiSmaStrategy().evaluate(frame)

        assert result.indicators["rsi"] <= 30  # the RSI half is satisfied
        assert result.indicators["close"] < result.indicators["sma"]  # the filter is not
        assert result.signal is None

    def test_a_dip_then_recovery_does_produce_a_buy(self) -> None:
        """Rare is not impossible -- and this is the shape that qualifies.

        A long decline leaves RSI at 17; eight flat bars let the 12-bar average
        catch down to price; a three-bar recovery lifts the close back above
        it while RSI is still nowhere near 30.
        """
        result = RsiSmaStrategy().evaluate(bars_from(DIP_THEN_RECOVERY))

        assert result.signal is not None
        assert result.signal.side is Side.BUY
        assert result.indicators["rsi"] <= 30
        assert result.indicators["close"] > result.indicators["sma"]

    def test_the_sell_side_is_the_mirror_image(self) -> None:
        assert RsiSmaStrategy().evaluate(bars_from(STEADY_RALLY)).signal is None

        result = RsiSmaStrategy().evaluate(bars_from(RALLY_THEN_FADE))
        assert result.signal is not None
        assert result.signal.side is Side.SELL

    def test_the_reason_names_the_reading_that_fired(self) -> None:
        signal = RsiSmaStrategy().evaluate(bars_from(DIP_THEN_RECOVERY)).signal
        assert signal is not None
        assert "RSI" in signal.reason and "<=30" in signal.reason
        # MetaTrader truncates order comments at 31 characters.
        assert len(signal.reason) <= 31


class TestTrendFilter:
    def test_contrarian_fires_where_aligned_stays_silent(self) -> None:
        """The same bars, the opposite filter, and a signal appears.

        Classic mean reversion: buy the oversold market precisely because it
        is below its average. Far more frequent than the default, and a
        different strategy -- which is why it is opt-in rather than a fix.
        """
        frame = bars_from(STEADY_DECLINE)
        assert RsiSmaStrategy().evaluate(frame).signal is None

        result = RsiSmaStrategy(trend_filter=TrendFilter.CONTRARIAN).evaluate(frame)
        assert result.signal is not None
        assert result.signal.side is Side.BUY

    def test_off_trades_the_thresholds_alone(self) -> None:
        for closes, side in ((STEADY_DECLINE, Side.BUY), (STEADY_RALLY, Side.SELL)):
            result = RsiSmaStrategy(trend_filter=TrendFilter.OFF).evaluate(bars_from(closes))
            assert result.signal is not None
            assert result.signal.side is side

    def test_contrarian_blocks_what_aligned_permits(self) -> None:
        frame = bars_from(DIP_THEN_RECOVERY)
        assert RsiSmaStrategy().evaluate(frame).signal is not None
        assert RsiSmaStrategy(trend_filter=TrendFilter.CONTRARIAN).evaluate(frame).signal is None

    def test_the_reason_records_which_filter_was_applied(self) -> None:
        signal = (
            RsiSmaStrategy(trend_filter=TrendFilter.OFF).evaluate(bars_from(STEADY_DECLINE)).signal
        )
        assert signal is not None and "SMA" not in signal.reason


class TestThresholds:
    def test_a_looser_threshold_fires_earlier(self) -> None:
        """A shallow dip that 30 ignores and 45 acts on."""
        frame = bars_from(declining_from_warmup(step=0.5, count=6))
        rsi = float(wilder_rsi(frame["close"], 14).iloc[-1])
        assert 30 < rsi < 45, f"fixture drifted: RSI is {rsi:.1f}"

        assert RsiSmaStrategy(trend_filter=TrendFilter.CONTRARIAN).evaluate(frame).signal is None
        loose = RsiSmaStrategy(oversold=45.0, trend_filter=TrendFilter.CONTRARIAN)
        assert loose.evaluate(frame).signal is not None


class TestRequireCross:
    def test_off_by_default_so_a_reading_keeps_signalling(self) -> None:
        frame = bars_from(STEADY_DECLINE)
        strategy = RsiSmaStrategy(trend_filter=TrendFilter.OFF)
        assert strategy.evaluate(frame).signal is not None
        assert strategy.evaluate(frame.iloc[:-1]).signal is not None

    def test_on_it_fires_only_as_the_threshold_is_crossed(self) -> None:
        """Deep inside oversold territory, with no crossing on this bar."""
        frame = bars_from(STEADY_DECLINE)
        strategy = RsiSmaStrategy(trend_filter=TrendFilter.OFF, require_cross=True)

        previous = float(wilder_rsi(frame["close"], 14).iloc[-2])
        assert previous <= 30, "fixture must already be oversold on the prior bar"
        assert strategy.evaluate(frame).signal is None

    def test_it_does_fire_on_the_bar_that_crosses(self) -> None:
        frame = bars_from(declining_from_warmup(step=1.0, count=8))
        rsi = wilder_rsi(frame["close"], 14)
        assert float(rsi.iloc[-2]) > 30 >= float(rsi.iloc[-1]), "fixture must cross on the last bar"

        strategy = RsiSmaStrategy(trend_filter=TrendFilter.OFF, require_cross=True)
        result = strategy.evaluate(frame)
        assert result.signal is not None
        assert result.signal.side is Side.BUY


class TestNoLookAhead:
    def test_the_verdict_depends_only_on_bars_up_to_the_last_row(self) -> None:
        """Appending future bars must not change what was decided earlier.

        Cheap to state, easy to break: one ``iloc[-1]`` written against the
        wrong frame turns a backtest into a fiction.
        """
        strategy = RsiSmaStrategy(trend_filter=TrendFilter.CONTRARIAN)
        history = bars_from(STEADY_DECLINE)
        with_future = bars_from(STEADY_DECLINE + [200.0, 300.0, 400.0])

        assert (
            strategy.evaluate(history).signal
            == strategy.evaluate(with_future.iloc[: len(history)]).signal
        )

    def test_readings_match_the_indicators_computed_independently(self) -> None:
        frame = bars_from(STEADY_DECLINE)
        result = RsiSmaStrategy().evaluate(frame)
        assert result.indicators["rsi"] == pytest.approx(
            float(wilder_rsi(frame["close"], 14).iloc[-1])
        )
        assert result.indicators["sma"] == pytest.approx(float(sma(frame["close"], 12).iloc[-1]))
