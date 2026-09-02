"""The RSI + moving-average strategy this repository started with.

The original conditions were::

    if last_rsi <= 30 and last_close > moving_avg:   -> BUY
    elif last_rsi >= 70 and last_close < moving_avg: -> SELL

Read carefully, that is "buy the dip, but only while price is still above its
average" -- a pullback-in-an-uptrend filter. It is a legitimate idea, but the
two halves pull against each other: whatever drives RSI(14) below 30 on a
15-minute chart has almost always dragged price under a 12-period average as
well. The combination is rare rather than wrong, and on the original 26-bar
window -- barely past the RSI seed -- it was rarer still.

So the defaults here reproduce the original exactly. What is added is the
ability to change each part deliberately, and a backtester that reports how
often the condition actually fires, so the choice is made against a number
instead of an assumption.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

import pandas as pd

from mt5bot.indicators import warmup_bars, wilder_rsi_array
from mt5bot.models import Side, Signal
from mt5bot.strategy.base import Evaluation, Strategy


class TrendFilter(StrEnum):
    """How the moving average is allowed to veto an RSI signal."""

    #: Buy only above the average, sell only below it. The original behaviour:
    #: an oversold reading is acted on only while the trend still points up.
    ALIGNED = "aligned"
    #: Buy only below the average, sell only above it. Pure mean reversion --
    #: the average confirms the stretch rather than contradicting it, so this
    #: fires far more often than ALIGNED.
    CONTRARIAN = "contrarian"
    #: Ignore the average; trade the RSI thresholds alone.
    OFF = "off"


@dataclass(frozen=True, slots=True)
class RsiSmaStrategy(Strategy):
    """RSI thresholds, optionally filtered by a simple moving average."""

    rsi_period: int = 14
    sma_period: int = 12
    oversold: float = 30.0
    overbought: float = 70.0
    trend_filter: TrendFilter = TrendFilter.ALIGNED
    #: Fire only on the bar the threshold is *crossed*, not on every bar the
    #: reading stays beyond it. With this off, a deeply oversold market
    #: re-signals every bar; the engine ignores a signal matching the position
    #: it already holds, so the practical effect is on re-entry after an exit.
    require_cross: bool = False

    def __post_init__(self) -> None:
        if self.rsi_period < 2:
            raise ValueError(f"rsi_period must be at least 2, got {self.rsi_period}")
        if self.sma_period < 1:
            raise ValueError(f"sma_period must be at least 1, got {self.sma_period}")
        if not 0 <= self.oversold < self.overbought <= 100:
            raise ValueError(
                "thresholds must satisfy 0 <= oversold < overbought <= 100, got "
                f"oversold={self.oversold}, overbought={self.overbought}"
            )

    @property
    def name(self) -> str:
        return "rsi-sma"

    @property
    def min_bars(self) -> int:
        # One extra bar so require_cross can see the previous reading.
        return max(warmup_bars(self.rsi_period), self.sma_period) + 1

    def describe(self) -> str:
        parts = [
            f"RSI({self.rsi_period}) <= {self.oversold:g} buys, >= {self.overbought:g} sells",
            f"SMA({self.sma_period}) filter: {self.trend_filter.value}",
        ]
        if self.require_cross:
            parts.append("on threshold crossings only")
        return f"{self.name} -- " + "; ".join(parts)

    def evaluate(self, bars: pd.DataFrame) -> Evaluation:
        # Arrays, not Series: only the last reading (and the one before it, for
        # require_cross) is ever read, and this runs once per bar over the whole
        # of a backtest. Going through pandas here cost more than the arithmetic.
        close = bars["close"].to_numpy(dtype=float, copy=False)
        rsi = wilder_rsi_array(close, self.rsi_period)

        last_rsi = float(rsi[-1])
        last_sma = (
            float(close[-self.sma_period :].mean()) if close.size >= self.sma_period else math.nan
        )
        last_close = float(close[-1])
        readings = {"rsi": last_rsi, "sma": last_sma, "close": last_close}

        if not math.isfinite(last_rsi) or not math.isfinite(last_sma):
            # Still warming up. Returning no signal beats returning one derived
            # from a NaN, which compares False against every threshold and so
            # looks identical to "the market is quiet".
            return Evaluation(None, readings)

        previous_rsi = float(rsi[-2]) if rsi.size >= 2 else math.nan

        if self._triggered(last_rsi, previous_rsi, Side.BUY) and self._trend_allows(
            Side.BUY, last_close, last_sma
        ):
            return Evaluation(Signal(Side.BUY, self._reason(Side.BUY, last_rsi)), readings)

        if self._triggered(last_rsi, previous_rsi, Side.SELL) and self._trend_allows(
            Side.SELL, last_close, last_sma
        ):
            return Evaluation(Signal(Side.SELL, self._reason(Side.SELL, last_rsi)), readings)

        return Evaluation(None, readings)

    def _triggered(self, current: float, previous: float, side: Side) -> bool:
        """Whether the RSI threshold for ``side`` is met on this bar."""
        if side is Side.BUY:
            beyond = current <= self.oversold
            crossed = math.isfinite(previous) and previous > self.oversold
        else:
            beyond = current >= self.overbought
            crossed = math.isfinite(previous) and previous < self.overbought
        return beyond and crossed if self.require_cross else beyond

    def _trend_allows(self, side: Side, close: float, average: float) -> bool:
        if self.trend_filter is TrendFilter.OFF:
            return True
        above = close > average
        wants_above = (side is Side.BUY) is (self.trend_filter is TrendFilter.ALIGNED)
        return above is wants_above

    def _reason(self, side: Side, rsi_value: float) -> str:
        threshold = self.oversold if side is Side.BUY else self.overbought
        comparison = "<=" if side is Side.BUY else ">="
        reason = f"RSI {rsi_value:.1f}{comparison}{threshold:g}"
        if self.trend_filter is not TrendFilter.OFF:
            reason += f" +{self.trend_filter.value[:4]}SMA"
        return reason
