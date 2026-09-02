"""Technical indicators, as pure functions over pandas Series.

Nothing here touches a broker or any global state, which is why these are the
easiest part of the bot to pin down with tests -- and they needed pinning down.
The original ``calculate_RSI`` was not Wilder's RSI: it averaged gains and
losses with a plain rolling mean, so its readings drifted from every charting
platform the user would compare against, and it divided by an average loss that
is zero on any run of consecutive up bars, yielding ``inf`` and then a silent
``NaN``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "atr",
    "ema",
    "sma",
    "true_range",
    "warmup_bars",
    "wilder_rsi",
    "wilder_smooth",
]


def _check_period(period: int) -> None:
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")


def sma(values: pd.Series, period: int) -> pd.Series:
    """Simple moving average. ``NaN`` until ``period`` observations exist."""
    _check_period(period)
    return values.rolling(window=period, min_periods=period).mean()


def ema(values: pd.Series, period: int) -> pd.Series:
    """Exponential moving average using the conventional ``2/(period+1)`` span."""
    _check_period(period)
    return values.ewm(span=period, adjust=False, min_periods=period).mean()


def wilder_smooth(values: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (a.k.a. RMA / SMMA).

    Seeded with the simple mean of the first ``period`` observations, then
    ``avg[i] = (avg[i-1] * (period - 1) + value[i]) / period``.

    This is equivalent to an EMA with ``alpha = 1 / period``, but the seed
    matters: pandas' ``ewm(adjust=False)`` starts from the *first* observation
    rather than the mean of the first ``period`` of them, which leaves a
    visible offset for hundreds of bars on a 14-period indicator. Seeding the
    way Wilder specified is what makes these numbers line up with MetaTrader,
    TradingView and TA-Lib.

    Leading ``NaN`` values are skipped -- convenient, because the inputs are
    typically ``diff()`` or a true range, both of which start undefined.
    """
    _check_period(period)
    arr = values.to_numpy(dtype=float, copy=False)
    out = np.full(arr.shape, np.nan, dtype=float)

    finite = np.flatnonzero(~np.isnan(arr))
    if finite.size < period:
        return pd.Series(out, index=values.index, name=values.name)

    start = int(finite[0])
    seed_end = start + period  # exclusive
    if seed_end > arr.size:
        return pd.Series(out, index=values.index, name=values.name)

    out[seed_end - 1] = arr[start:seed_end].mean()
    for i in range(seed_end, arr.size):
        out[i] = (out[i - 1] * (period - 1) + arr[i]) / period

    return pd.Series(out, index=values.index, name=values.name)


def wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index, as J. Welles Wilder defined it.

    Returns values in ``[0, 100]``, ``NaN`` during the warm-up.

    The two degenerate cases are handled explicitly instead of being left to
    float division:

    * no losses in the window -> ``100`` (``avg_loss`` of zero would give ``inf``)
    * no movement at all -> ``50``, the neutral reading (``0 / 0`` would give
      ``NaN``, and a ``NaN`` compares ``False`` against every threshold, so the
      bot would silently stop signalling instead of reporting "no trend")
    """
    _check_period(period)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = wilder_smooth(gain, period).to_numpy(dtype=float, copy=False)
    avg_loss = wilder_smooth(loss, period).to_numpy(dtype=float, copy=False)

    rsi = np.full(avg_gain.shape, np.nan, dtype=float)
    ready = ~(np.isnan(avg_gain) | np.isnan(avg_loss))

    falling = ready & (avg_loss > 0)
    rsi[falling] = 100.0 - 100.0 / (1.0 + avg_gain[falling] / avg_loss[falling])
    rsi[ready & (avg_loss == 0) & (avg_gain > 0)] = 100.0
    rsi[ready & (avg_loss == 0) & (avg_gain == 0)] = 50.0

    return pd.Series(rsi, index=close.index, name="rsi")


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Wilder's true range: the widest of the three gap-aware ranges."""
    prev_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    )
    tr = ranges.max(axis=1)
    tr.iloc[0] = np.nan  # undefined without a previous close
    return pd.Series(tr, index=close.index, name="true_range")


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range -- Wilder-smoothed true range."""
    return wilder_smooth(true_range(high, low, close), period).rename("atr")


def warmup_bars(period: int, *, factor: int = 5, floor: int = 50) -> int:
    """How much history a Wilder indicator of ``period`` needs to settle.

    Wilder smoothing is recursive, so the seed keeps leaking into the result:
    after ``n`` further bars the seed still carries ``((period-1)/period)**n``
    of the weight. At ``period=14`` five periods of history leaves under 1% of
    it, which is well inside the noise.

    The original script requested ``rsi_period + moving_avg_period`` bars -- 26
    for its defaults. That is barely one period past the seed, so its RSI was
    dominated by whatever the window happened to open on.
    """
    _check_period(period)
    return max(period * factor, floor)
