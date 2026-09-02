"""Shared fixtures.

The symbol specs mirror what a real broker reports, because the numbers that
matter here -- ``digits``, ``point``, ``volume_step``, ``trade_tick_value`` --
are exactly the ones the original script assumed instead of reading.
"""

from __future__ import annotations

import pytest

from mt5bot.models import SymbolSpec


@pytest.fixture
def eurusd() -> SymbolSpec:
    """A 5-digit FX major: a pip is ten points, and a tick is worth $1 a lot."""
    return SymbolSpec(
        symbol="EURUSD",
        digits=5,
        point=0.00001,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        trade_tick_value=1.0,
        trade_tick_size=0.00001,
        trade_stops_level=0,
        contract_size=100_000.0,
    )


@pytest.fixture
def usdjpy() -> SymbolSpec:
    """A 3-digit JPY pair: same fractional-pip convention, different scale."""
    return SymbolSpec(
        symbol="USDJPY",
        digits=3,
        point=0.001,
        volume_min=0.01,
        volume_max=50.0,
        volume_step=0.01,
        trade_tick_value=0.67,
        trade_tick_size=0.001,
        contract_size=100_000.0,
    )


@pytest.fixture
def legacy_four_digit() -> SymbolSpec:
    """A 4-digit quote, where a pip and a point really are the same thing."""
    return SymbolSpec(
        symbol="EURUSD4",
        digits=4,
        point=0.0001,
        volume_min=0.1,
        volume_max=10.0,
        volume_step=0.1,
        trade_tick_value=10.0,
        trade_tick_size=0.0001,
    )
