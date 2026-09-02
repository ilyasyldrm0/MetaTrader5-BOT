"""Risk and pip-arithmetic tests.

These cover the bug that stopped the original bot from ever placing an order,
plus the neighbouring mistakes that would have surfaced the moment it did.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from mt5bot.models import Side, SymbolSpec, Tick
from mt5bot.risk import (
    lot_for_risk,
    normalize_price,
    normalize_volume,
    pip_size,
    pips_to_price,
    position_size,
    price_to_pips,
    spread_is_acceptable,
    spread_pips,
    stop_levels,
)

NOW = datetime(2026, 9, 2, 12, 0, 0)


def vars_of(spec: SymbolSpec) -> dict[str, object]:
    """Field values of a slotted frozen dataclass, for building a variant of it."""
    return {f: getattr(spec, f) for f in spec.__slots__}


class TestPipSize:
    def test_five_digit_quote_has_a_fractional_pip(self, eurusd: SymbolSpec) -> None:
        assert pip_size(eurusd) == pytest.approx(0.0001)
        assert pip_size(eurusd) == pytest.approx(eurusd.point * 10)

    def test_three_digit_jpy_quote_has_a_fractional_pip(self, usdjpy: SymbolSpec) -> None:
        assert pip_size(usdjpy) == pytest.approx(0.01)

    def test_four_digit_quote_has_pip_equal_to_point(self, legacy_four_digit: SymbolSpec) -> None:
        assert pip_size(legacy_four_digit) == pytest.approx(legacy_four_digit.point)

    def test_round_trips_through_pips_and_back(self, eurusd: SymbolSpec) -> None:
        assert price_to_pips(pips_to_price(37.5, eurusd), eurusd) == pytest.approx(37.5)


class TestStopLevels:
    def test_buy_stops_below_and_targets_above(self, eurusd: SymbolSpec) -> None:
        levels = stop_levels(1.08000, Side.BUY, eurusd, sl_pips=25, tp_pips=50)
        assert levels.sl == pytest.approx(1.07750)
        assert levels.tp == pytest.approx(1.08500)

    def test_sell_is_the_mirror_image(self, eurusd: SymbolSpec) -> None:
        levels = stop_levels(1.08000, Side.SELL, eurusd, sl_pips=25, tp_pips=50)
        assert levels.sl == pytest.approx(1.08250)
        assert levels.tp == pytest.approx(1.07500)

    def test_the_original_bug_is_gone(self, eurusd: SymbolSpec) -> None:
        """Regression for the defect that made the bot untradeable.

        ``take_profit = last_close + trade_profit`` with ``trade_profit = 50``
        asked for a take profit at 51.08 and a stop at -23.92 -- a negative
        price. Fifty pips is 0.0050, and the levels must stay in the
        neighbourhood of the entry.
        """
        entry = 1.08000
        levels = stop_levels(entry, Side.BUY, eurusd, sl_pips=25, tp_pips=50)
        assert levels.tp is not None and levels.sl is not None
        assert abs(levels.tp - entry) < 0.01
        assert abs(levels.sl - entry) < 0.01
        assert levels.sl > 0
        assert levels.tp != pytest.approx(entry + 50)

    def test_levels_are_rounded_to_the_symbol_digits(self, usdjpy: SymbolSpec) -> None:
        levels = stop_levels(150.123, Side.BUY, usdjpy, sl_pips=13.7, tp_pips=None)
        assert levels.sl is not None
        assert levels.sl == round(levels.sl, usdjpy.digits)
        assert levels.tp is None

    def test_broker_minimum_distance_widens_a_tight_stop(self, eurusd: SymbolSpec) -> None:
        """A 2-pip stop under a 50-point (5-pip) minimum must be pushed out."""
        strict = SymbolSpec(**{**vars_of(eurusd), "trade_stops_level": 50})
        levels = stop_levels(1.08000, Side.BUY, strict, sl_pips=2, tp_pips=50)
        assert levels.widened is True
        assert levels.sl == pytest.approx(1.07950)  # 50 points, not 2 pips
        assert levels.tp == pytest.approx(1.08500)  # comfortably clear, untouched

    def test_a_comfortable_stop_is_not_flagged(self, eurusd: SymbolSpec) -> None:
        strict = SymbolSpec(**{**vars_of(eurusd), "trade_stops_level": 50})
        assert stop_levels(1.08, Side.BUY, strict, sl_pips=25, tp_pips=50).widened is False

    @pytest.mark.parametrize("bad", [0, -5])
    def test_rejects_a_nonpositive_distance(self, eurusd: SymbolSpec, bad: float) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            stop_levels(1.08, Side.BUY, eurusd, sl_pips=bad, tp_pips=50)


class TestNormalizeVolume:
    def test_snaps_down_onto_the_step(self, eurusd: SymbolSpec) -> None:
        assert normalize_volume(0.157, eurusd) == pytest.approx(0.15)

    def test_rounds_down_never_up(self, eurusd: SymbolSpec) -> None:
        """Rounding up can exceed the free margin the size was computed against."""
        assert normalize_volume(0.199, eurusd) == pytest.approx(0.19)

    def test_survives_binary_dust(self, eurusd: SymbolSpec) -> None:
        """``0.3 / 0.01`` is 29.999999999999996 in binary floating point.

        A naive ``floor`` turns a 0.30 lot order into 0.29.
        """
        assert normalize_volume(0.3, eurusd) == pytest.approx(0.30)
        assert normalize_volume(0.7, eurusd) == pytest.approx(0.70)
        assert normalize_volume(1.1, eurusd) == pytest.approx(1.10)

    def test_caps_at_the_broker_maximum(self, usdjpy: SymbolSpec) -> None:
        assert normalize_volume(999.0, usdjpy) == pytest.approx(usdjpy.volume_max)

    def test_below_the_minimum_returns_zero_rather_than_clamping_up(
        self, eurusd: SymbolSpec
    ) -> None:
        """Clamping up would take more risk than the caller asked for."""
        assert normalize_volume(0.004, eurusd) == 0.0

    def test_zero_and_negative_are_zero(self, eurusd: SymbolSpec) -> None:
        assert normalize_volume(0.0, eurusd) == 0.0
        assert normalize_volume(-1.0, eurusd) == 0.0

    def test_coarse_step_broker(self, legacy_four_digit: SymbolSpec) -> None:
        assert normalize_volume(0.44, legacy_four_digit) == pytest.approx(0.4)
        assert normalize_volume(0.05, legacy_four_digit) == 0.0


class TestNormalizePrice:
    def test_rounds_to_symbol_digits(self, eurusd: SymbolSpec) -> None:
        assert normalize_price(1.0812345678, eurusd) == pytest.approx(1.08123)


class TestLotForRisk:
    def test_worked_example(self, eurusd: SymbolSpec) -> None:
        """$10,000 at 1% over a 25-pip stop is 0.40 lots.

        0.40 lots * 25 pips * $10 per pip per lot = $100 = 1% of the balance.
        """
        assert lot_for_risk(10_000, 1.0, 25, eurusd) == pytest.approx(0.40)

    def test_risk_scales_linearly(self, eurusd: SymbolSpec) -> None:
        assert lot_for_risk(10_000, 2.0, 25, eurusd) == pytest.approx(0.80)

    def test_a_wider_stop_buys_fewer_lots(self, eurusd: SymbolSpec) -> None:
        assert lot_for_risk(10_000, 1.0, 50, eurusd) == pytest.approx(0.20)

    def test_uses_the_symbols_own_tick_value_not_a_hardcoded_ten_dollars(
        self, usdjpy: SymbolSpec
    ) -> None:
        """A JPY pair is not $10 a pip, which is why tick economics are read.

        stop = 25 pips = 0.25 in price = 250 ticks; 250 * $0.67 = $167.50 a lot.
        $100 of risk therefore buys 0.59 lots, not 0.40.
        """
        assert lot_for_risk(10_000, 1.0, 25, usdjpy) == pytest.approx(0.59)

    def test_tiny_account_gets_zero_rather_than_the_minimum_lot(self, eurusd: SymbolSpec) -> None:
        assert lot_for_risk(50, 1.0, 100, eurusd) == 0.0

    def test_empty_account_is_zero(self, eurusd: SymbolSpec) -> None:
        assert lot_for_risk(0.0, 1.0, 25, eurusd) == 0.0

    @pytest.mark.parametrize(("risk", "stop"), [(0, 25), (-1, 25), (1, 0), (1, -5)])
    def test_rejects_nonsense_inputs(self, eurusd: SymbolSpec, risk: float, stop: float) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            lot_for_risk(10_000, risk, stop, eurusd)

    def test_refuses_a_symbol_with_no_tick_economics(self, eurusd: SymbolSpec) -> None:
        broken = SymbolSpec(**{**vars_of(eurusd), "trade_tick_value": 0.0})
        with pytest.raises(ValueError, match="no usable tick value"):
            lot_for_risk(10_000, 1.0, 25, broken)


class TestPositionSize:
    def test_fixed_mode_normalises_the_configured_lot(self, eurusd: SymbolSpec) -> None:
        got = position_size(
            mode="fixed",
            fixed_lot=0.517,
            risk_percent=1.0,
            balance=10_000,
            sl_pips=25,
            spec=eurusd,
        )
        assert got == pytest.approx(0.51)

    def test_fixed_mode_ignores_the_balance(self, eurusd: SymbolSpec) -> None:
        got = position_size(
            mode="fixed",
            fixed_lot=0.5,
            risk_percent=1.0,
            balance=100.0,
            sl_pips=25,
            spec=eurusd,
        )
        assert got == pytest.approx(0.5)

    def test_risk_mode_sizes_off_the_stop(self, eurusd: SymbolSpec) -> None:
        got = position_size(
            mode="risk",
            fixed_lot=0.5,
            risk_percent=1.0,
            balance=10_000,
            sl_pips=25,
            spec=eurusd,
        )
        assert got == pytest.approx(0.40)

    def test_risk_mode_without_a_stop_is_refused(self, eurusd: SymbolSpec) -> None:
        """There is no defined loss to size against, so guessing is not an option."""
        with pytest.raises(ValueError, match="requires a stop loss"):
            position_size(
                mode="risk",
                fixed_lot=0.5,
                risk_percent=1.0,
                balance=10_000,
                sl_pips=None,
                spec=eurusd,
            )

    def test_unknown_mode_is_refused(self, eurusd: SymbolSpec) -> None:
        with pytest.raises(ValueError, match="unknown sizing mode"):
            position_size(
                mode="martingale",
                fixed_lot=0.5,
                risk_percent=1.0,
                balance=10_000,
                sl_pips=25,
                spec=eurusd,
            )


class TestSpread:
    def test_spread_is_reported_in_pips(self, eurusd: SymbolSpec) -> None:
        tick = Tick(time=NOW, bid=1.08000, ask=1.08012)
        assert spread_pips(tick, eurusd) == pytest.approx(1.2)

    def test_a_tight_spread_is_acceptable(self, eurusd: SymbolSpec) -> None:
        tick = Tick(time=NOW, bid=1.08000, ask=1.08012)
        assert spread_is_acceptable(tick, eurusd, 2.0) is True

    def test_a_blown_out_spread_is_not(self, eurusd: SymbolSpec) -> None:
        tick = Tick(time=NOW, bid=1.08000, ask=1.08090)
        assert spread_is_acceptable(tick, eurusd, 2.0) is False

    def test_none_disables_the_check(self, eurusd: SymbolSpec) -> None:
        tick = Tick(time=NOW, bid=1.08000, ask=1.09000)
        assert spread_is_acceptable(tick, eurusd, None) is True
