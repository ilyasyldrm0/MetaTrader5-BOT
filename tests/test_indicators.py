"""Indicator tests.

The RSI here is the one thing in this repository that has a single objectively
correct answer, so it is checked against values derived by hand rather than
against another implementation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mt5bot.indicators import atr, ema, sma, true_range, warmup_bars, wilder_rsi, wilder_smooth

# Wilder's own worked example, the series every RSI tutorial reproduces.
WILDER_CLOSES = [
    44.34,
    44.09,
    44.15,
    43.61,
    44.33,
    44.83,
    45.10,
    45.42,
    45.84,
    46.08,
    45.89,
    46.03,
    45.61,
    46.28,
    46.28,
    46.00,
    46.03,
    46.41,
    46.22,
    45.64,
    46.21,
    46.25,
    45.71,
    46.45,
    45.78,
    45.35,
    44.03,
    44.18,
    44.22,
    44.57,
    43.42,
    42.66,
    43.13,
]


class TestWilderRsi:
    def test_first_value_matches_hand_calculation(self) -> None:
        """Derived from the definition, not copied from a website.

        Over the first 14 deltas the gains sum to 3.34 and the losses to 1.40::

            avg_gain = 3.34 / 14 = 0.23857142...
            avg_loss = 1.40 / 14 = 0.10
            RS       = 2.38571428...
            RSI      = 100 - 100 / (1 + RS) = 70.46414...

        Widely copied tables print 70.53 for this bar. That figure comes from a
        spreadsheet that rounds the running averages at each step; it is the
        approximation, and 70.4641 is the exact answer. Worth knowing before
        concluding this implementation is off by 0.07.
        """
        rsi = wilder_rsi(pd.Series(WILDER_CLOSES), 14)
        assert rsi.iloc[14] == pytest.approx(70.46414, abs=1e-5)

    def test_second_value_follows_the_smoothing_recursion(self) -> None:
        """One more step, to pin the recursion and not just the seed.

        Spelled out as the definition rather than as a constant: the assertion
        then states *why* the number is what it is, and cannot drift from a
        mis-typed decimal. The 16th close is 46.00, a 0.28 loss on no gain.
        """
        period = 14
        avg_gain = 3.34 / period  # the seed, from the test above
        avg_loss = 1.40 / period
        avg_gain = (avg_gain * (period - 1) + 0.00) / period
        avg_loss = (avg_loss * (period - 1) + 0.28) / period
        expected = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

        rsi = wilder_rsi(pd.Series(WILDER_CLOSES), period)
        assert rsi.iloc[15] == pytest.approx(expected, abs=1e-4)

    def test_tracks_the_published_reference_table(self) -> None:
        """Every published value, within the rounding error that table carries."""
        published = [
            70.53,
            66.32,
            66.55,
            69.41,
            66.36,
            57.97,
            62.93,
            63.26,
            56.06,
            62.38,
            54.71,
            50.42,
            39.99,
            41.46,
            41.87,
            45.46,
            37.30,
            33.08,
            37.77,
        ]
        rsi = wilder_rsi(pd.Series(WILDER_CLOSES), 14).dropna()
        assert len(rsi) == len(published)
        np.testing.assert_allclose(rsi.to_numpy(), published, atol=0.08)

    def test_warmup_is_nan_and_first_value_lands_at_index_period(self) -> None:
        rsi = wilder_rsi(pd.Series(WILDER_CLOSES), 14)
        assert rsi.iloc[:14].isna().all()
        assert rsi.iloc[14:].notna().all()

    def test_always_within_bounds(self) -> None:
        rng = np.random.default_rng(20230517)
        walk = pd.Series(1.10 + np.cumsum(rng.normal(0, 0.0004, 500)))
        rsi = wilder_rsi(walk, 14).dropna()
        assert len(rsi) > 0
        assert rsi.between(0.0, 100.0).all()

    def test_uninterrupted_gains_give_100_not_inf(self) -> None:
        """The original divided by an average loss of zero and produced ``inf``.

        ``inf`` then flowed into ``100 - 100/(1+inf)`` as exactly 100 by luck,
        but ``0/0`` on a flat series produced ``NaN`` -- and a ``NaN`` fails
        every comparison, so the bot went quiet instead of reporting no trend.
        """
        rsi = wilder_rsi(pd.Series(np.arange(1.0, 41.0)), 14).dropna()
        assert np.isfinite(rsi).all()
        assert (rsi == 100.0).all()

    def test_uninterrupted_losses_give_zero(self) -> None:
        rsi = wilder_rsi(pd.Series(np.arange(40.0, 0.0, -1.0)), 14).dropna()
        assert (rsi == 0.0).all()

    def test_flat_series_is_neutral_not_nan(self) -> None:
        rsi = wilder_rsi(pd.Series([1.2345] * 40), 14).dropna()
        assert len(rsi) == 26
        assert (rsi == 50.0).all()

    def test_differs_from_a_plain_rolling_mean(self) -> None:
        """Guards the actual bug: a rolling mean is not Wilder smoothing.

        Both are "an average of gains over 14 bars", which is why the original
        looked right. They disagree by whole RSI points on real data.
        """
        close = pd.Series(WILDER_CLOSES)
        delta = close.diff()
        gain = delta.where(delta > 0, 0).fillna(0)
        loss = delta.where(delta < 0, 0).abs().fillna(0)
        legacy = 100 - 100 / (1 + gain.rolling(14).mean() / loss.rolling(14).mean())

        correct = wilder_rsi(close, 14)
        overlap = legacy.notna() & correct.notna()
        assert overlap.sum() > 5
        assert (legacy[overlap] - correct[overlap]).abs().max() > 1.0

    def test_short_input_is_all_nan_rather_than_an_error(self) -> None:
        assert wilder_rsi(pd.Series([1.0, 2.0, 3.0]), 14).isna().all()

    def test_rejects_a_nonsense_period(self) -> None:
        with pytest.raises(ValueError, match="period must be >= 1"):
            wilder_rsi(pd.Series([1.0, 2.0]), 0)


class TestWilderSmooth:
    def test_seed_is_the_mean_of_the_first_period_observations(self) -> None:
        s = pd.Series([2.0, 4.0, 6.0, 8.0])
        out = wilder_smooth(s, 4)
        assert out.iloc[:3].isna().all()
        assert out.iloc[3] == pytest.approx(5.0)

    def test_recursion_matches_the_definition(self) -> None:
        s = pd.Series([2.0, 4.0, 6.0, 8.0, 10.0])
        out = wilder_smooth(s, 4)
        assert out.iloc[4] == pytest.approx((5.0 * 3 + 10.0) / 4)

    def test_skips_leading_nan(self) -> None:
        """Inputs come from ``diff()`` and true range, which both start undefined."""
        s = pd.Series([np.nan, 2.0, 4.0, 6.0, 8.0])
        out = wilder_smooth(s, 4)
        assert out.iloc[:4].isna().all()
        assert out.iloc[4] == pytest.approx(5.0)

    def test_converges_towards_a_constant_input(self) -> None:
        out = wilder_smooth(pd.Series([7.0] * 100), 14)
        assert out.iloc[-1] == pytest.approx(7.0)


class TestMovingAverages:
    def test_sma_needs_a_full_window(self) -> None:
        out = sma(pd.Series([1.0, 2.0, 3.0, 4.0]), 3)
        assert out.iloc[:2].isna().all()
        assert out.iloc[2] == pytest.approx(2.0)
        assert out.iloc[3] == pytest.approx(3.0)

    def test_ema_uses_the_conventional_span(self) -> None:
        """``alpha = 2 / (period + 1)``, recursing from the first observation.

        Note the seeding convention differs from :func:`wilder_smooth`: pandas'
        ``adjust=False`` starts the recursion at the first value, and
        ``min_periods`` only hides the early output rather than changing it.
        Wilder's indicators need the mean-of-first-period seed instead, which is
        exactly why :func:`wilder_smooth` exists rather than a call to ``ewm``.
        """
        out = ema(pd.Series([1.0, 2.0, 3.0, 4.0, 5.0]), 3)
        alpha = 2 / (3 + 1)
        assert out.iloc[:2].isna().all()
        expected_2 = 1.0 + alpha * (2.0 - 1.0)  # 1.5
        expected_2 = expected_2 + alpha * (3.0 - expected_2)  # 2.25
        assert out.iloc[2] == pytest.approx(expected_2)
        assert out.iloc[3] == pytest.approx(expected_2 + alpha * (4.0 - expected_2))


class TestTrueRangeAndAtr:
    def test_true_range_uses_the_previous_close_on_a_gap(self) -> None:
        high = pd.Series([10.0, 20.0])
        low = pd.Series([9.0, 19.0])
        close = pd.Series([9.5, 19.5])
        tr = true_range(high, low, close)
        assert np.isnan(tr.iloc[0])
        # The gap from 9.5 up to 19.0 dwarfs the 1.0 intrabar range.
        assert tr.iloc[1] == pytest.approx(20.0 - 9.5)

    def test_atr_of_constant_range_bars_is_that_range(self) -> None:
        n = 60
        close = pd.Series(np.full(n, 100.0))
        high = close + 1.0
        low = close - 1.0
        assert atr(high, low, close, 14).iloc[-1] == pytest.approx(2.0)


class TestWarmupBars:
    def test_gives_several_periods_of_history(self) -> None:
        assert warmup_bars(14) == 70
        assert warmup_bars(100) == 500

    def test_has_a_floor_for_short_periods(self) -> None:
        assert warmup_bars(2) == 50

    def test_beats_what_the_original_script_requested(self) -> None:
        """The original fetched ``rsi_period + sma_period`` bars: 26 for its defaults.

        That is 12 bars past the seed, at which point the seed still carries
        ``(13/14)**12`` -- about 41% -- of the reading's weight.
        """
        assert warmup_bars(14) > 14 + 12
        assert (13 / 14) ** 12 > 0.4
