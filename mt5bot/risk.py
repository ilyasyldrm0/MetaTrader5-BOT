"""Pip arithmetic, stop placement and position sizing.

This module exists because of the single worst bug in the original script::

    trade_profit = 50            # "in pips", per the README
    take_profit  = last_close + trade_profit

On EURUSD at 1.08 that asks the broker for a take profit at **51.08**, roughly
fifty times the price of the euro. Every order was rejected with
``TRADE_RETCODE_INVALID_STOPS``. The bot could not have placed a trade in its
entire lifetime, which is also why nobody noticed the other bugs.

The fix is not one multiplication. Getting a stop onto a real broker means
knowing the difference between a point and a pip, rounding the price to the
symbol's digits, keeping clear of the broker's minimum stop distance, and
rounding the volume down onto the lot step. All of that is here, in pure
functions, so all of it is testable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from mt5bot.models import Side, SymbolSpec, Tick

__all__ = [
    "StopLevels",
    "lot_for_risk",
    "normalize_price",
    "normalize_volume",
    "pip_size",
    "pips_to_price",
    "position_size",
    "price_to_pips",
    "spread_pips",
    "stop_levels",
]


def pip_size(spec: SymbolSpec) -> float:
    """The size of one pip in price units.

    A *point* is the last digit the broker quotes; a *pip* is the unit traders
    actually talk in. They are only the same thing on a 4-digit quote.

    Most brokers now quote FX to 5 digits (EURUSD ``1.08123``) or JPY pairs to
    3 (USDJPY ``150.123``), adding a fractional pip -- so a pip is ten points.
    Confusing the two is a factor-of-ten error in every stop the bot places,
    silently ten times too tight or too wide.

    The 3/5-digit rule is the FX convention and is what the defaults assume.
    For metals, indices and CFDs "pip" is not standardised, so check what your
    broker means before trusting a stop distance on those.
    """
    return spec.point * 10 if spec.digits in (3, 5) else spec.point


def pips_to_price(pips: float, spec: SymbolSpec) -> float:
    """Convert a pip distance into a price distance."""
    return pips * pip_size(spec)


def price_to_pips(price_distance: float, spec: SymbolSpec) -> float:
    """Convert a price distance into pips."""
    return price_distance / pip_size(spec)


def _decimals(step: float) -> int:
    """Decimal places in ``step``, via its literal text.

    ``Decimal(str(0.01))`` is exactly ``0.01``; ``Decimal(0.01)`` is the binary
    approximation with fifty-odd digits.
    """
    exponent = Decimal(str(step)).as_tuple().exponent
    return max(0, -int(exponent))


def normalize_price(price: float, spec: SymbolSpec) -> float:
    """Round a price onto the symbol's tick grid."""
    return round(price, spec.digits)


def normalize_volume(volume: float, spec: SymbolSpec) -> float:
    """Snap a lot size onto the broker's volume step.

    Rounds **down**, never up: rounding up can push the order past the free
    margin the size was calculated against, and turns a rejection into the
    caller's problem at the worst possible moment.

    Returns ``0.0`` when the result falls below ``volume_min`` -- the honest
    answer for "this position is too small to trade". Clamping up to the
    minimum instead would quietly take more risk than was asked for, which is
    the opposite of what a sizing function is for.
    """
    if volume <= 0 or spec.volume_step <= 0:
        return 0.0

    capped = min(volume, spec.volume_max)
    # round() before floor() absorbs binary dust: 0.3 / 0.1 is 2.9999999999996,
    # which would floor to 2 and silently shrink the position by a third.
    steps = math.floor(round(capped / spec.volume_step, 9))
    snapped = round(steps * spec.volume_step, _decimals(spec.volume_step))

    if snapped < spec.volume_min:
        return 0.0
    return snapped


@dataclass(frozen=True, slots=True)
class StopLevels:
    """Stop loss and take profit as absolute, broker-ready prices."""

    sl: float | None
    tp: float | None
    #: True when the broker's minimum stop distance forced a level wider than
    #: requested. The engine logs this: a strategy backtested on a 5-pip stop
    #: behaves differently once the broker widens it to 20.
    widened: bool = False


def stop_levels(
    entry: float,
    side: Side,
    spec: SymbolSpec,
    *,
    sl_pips: float | None,
    tp_pips: float | None,
) -> StopLevels:
    """Turn pip distances into the prices an order request actually carries.

    Distances are unsigned; direction comes from ``side``. A long stops out
    below entry and targets above it, a short does the reverse -- expressed
    once via :attr:`~mt5bot.models.Side.sign` rather than as two mirrored
    branches, which is how the original ended up with its buy and sell blocks
    duplicated and free to drift apart.

    Levels closer than ``spec.trade_stops_level`` points are pushed out to that
    distance rather than sent and rejected.
    """
    if sl_pips is not None and sl_pips <= 0:
        raise ValueError(f"sl_pips must be positive, got {sl_pips}")
    if tp_pips is not None and tp_pips <= 0:
        raise ValueError(f"tp_pips must be positive, got {tp_pips}")

    min_distance = spec.trade_stops_level * spec.point
    widened = False

    sl: float | None = None
    if sl_pips is not None:
        distance = pips_to_price(sl_pips, spec)
        if distance < min_distance:
            distance, widened = min_distance, True
        sl = normalize_price(entry - side.sign * distance, spec)

    tp: float | None = None
    if tp_pips is not None:
        distance = pips_to_price(tp_pips, spec)
        if distance < min_distance:
            distance, widened = min_distance, True
        tp = normalize_price(entry + side.sign * distance, spec)

    return StopLevels(sl=sl, tp=tp, widened=widened)


def lot_for_risk(
    balance: float,
    risk_percent: float,
    sl_pips: float,
    spec: SymbolSpec,
) -> float:
    """Lot size such that hitting the stop costs ``risk_percent`` of ``balance``.

    Converted through the symbol's own tick economics
    (``trade_tick_value`` per ``trade_tick_size`` of price movement, per lot)
    rather than through a hard-coded "$10 a pip". That constant is only true
    for a 100k contract quoted in the account currency; it is wrong for JPY
    pairs, wrong for a EUR-denominated account, and wrong for every CFD.

    Worked example -- EURUSD, $10,000 balance, 1% risk, 25-pip stop::

        stop distance = 25 pips              = 0.0025 in price
                      = 0.0025 / 0.00001     = 250 ticks
        loss per lot  = 250 * $1.00          = $250
        lots          = $100 / $250          = 0.40

    Returns ``0.0`` when the answer rounds below the broker's minimum lot.
    """
    if sl_pips <= 0:
        raise ValueError(f"sl_pips must be positive, got {sl_pips}")
    if risk_percent <= 0:
        raise ValueError(f"risk_percent must be positive, got {risk_percent}")
    if spec.trade_tick_size <= 0 or spec.trade_tick_value <= 0:
        raise ValueError(f"{spec.symbol} reports no usable tick value; cannot size by risk")
    if balance <= 0:
        return 0.0

    money_at_risk = balance * risk_percent / 100.0
    ticks_to_stop = pips_to_price(sl_pips, spec) / spec.trade_tick_size
    loss_per_lot = ticks_to_stop * spec.trade_tick_value
    if loss_per_lot <= 0:
        return 0.0

    return normalize_volume(money_at_risk / loss_per_lot, spec)


def position_size(
    *,
    mode: str,
    fixed_lot: float,
    risk_percent: float,
    balance: float,
    sl_pips: float | None,
    spec: SymbolSpec,
) -> float:
    """Resolve the configured sizing mode into a broker-ready lot size.

    ``mode="risk"`` sizes off the stop distance; ``mode="fixed"`` uses
    ``fixed_lot``. Risk sizing needs a stop -- without one there is no defined
    loss to size against, so that combination is refused rather than guessed at.
    """
    if mode == "fixed":
        return normalize_volume(fixed_lot, spec)
    if mode == "risk":
        if sl_pips is None:
            raise ValueError("sizing mode 'risk' requires a stop loss; set sl_pips or use 'fixed'")
        return lot_for_risk(balance, risk_percent, sl_pips, spec)
    raise ValueError(f"unknown sizing mode {mode!r}; expected 'fixed' or 'risk'")


def spread_pips(tick: Tick, spec: SymbolSpec) -> float:
    """Current bid/ask spread, in pips."""
    return price_to_pips(tick.spread, spec)


def spread_is_acceptable(tick: Tick, spec: SymbolSpec, max_pips: float | None) -> bool:
    """Whether the spread is tight enough to enter.

    Spreads blow out around news and at the session rollover. Entering then
    pays the widened spread on the way in *and* moves the effective stop
    closer, which is a reliable way to turn a tested strategy into a losing one.
    ``None`` disables the check.
    """
    if max_pips is None:
        return True
    return spread_pips(tick, spec) <= max_pips
