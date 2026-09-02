"""Reading bar data from CSV.

Two shapes are accepted without the user having to reformat anything: the
plain ``time,open,high,low,close,volume`` layout this project writes, and the
tab-separated export MetaTrader 5 itself produces from
*Tools -> History Centre -> Export*, whose columns look like
``<DATE>  <TIME>  <OPEN>  <HIGH>  <LOW>  <CLOSE>  <TICKVOL>``.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from mt5bot.brokers.base import BAR_COLUMNS
from mt5bot.errors import ConfigError

#: Column names seen in the wild, mapped onto the ones used internally.
_ALIASES: dict[str, str] = {
    "date": "time",
    "datetime": "time",
    "timestamp": "time",
    "tickvol": "volume",
    "tick_volume": "volume",
    "vol": "volume",
    "realvol": "volume",
    "real_volume": "volume",
}


def load_csv(path: str | Path) -> pd.DataFrame:
    """Read a bar file into the frame shape the brokers use.

    Raises :class:`~mt5bot.errors.ConfigError` with something actionable
    rather than letting a pandas parse error surface.
    """
    file = Path(path)
    if not file.is_file():
        raise ConfigError(f"price data file not found: {file}")

    try:
        frame = pd.read_csv(file, sep=None, engine="python", skipinitialspace=True)
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        raise ConfigError(f"could not read {file}: {exc}") from exc

    if frame.empty:
        raise ConfigError(f"{file} contains no rows")

    frame.columns = _normalise_columns([str(c) for c in frame.columns])

    # MetaTrader exports the date and the time as separate columns.
    if "time" in frame.columns and "timeonly" in frame.columns:
        frame["time"] = (
            frame["time"].astype(str).str.strip() + " " + frame["timeonly"].astype(str).str.strip()
        )
        frame = frame.drop(columns=["timeonly"])

    missing = [c for c in ("time", "open", "high", "low", "close") if c not in frame.columns]
    if missing:
        raise ConfigError(
            f"{file} is missing the column(s): {', '.join(missing)}. "
            f"Found: {', '.join(map(str, frame.columns))}"
        )

    if "volume" not in frame.columns:
        frame["volume"] = 0.0

    try:
        frame["time"] = pd.to_datetime(frame["time"], format="mixed")
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"could not parse the time column of {file}: {exc}") from exc

    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame = frame.loc[:, list(BAR_COLUMNS)].dropna(subset=["open", "high", "low", "close"])
    frame = frame.sort_values("time").reset_index(drop=True)

    if len(frame) < 2:
        raise ConfigError(f"{file} has {len(frame)} usable rows; need at least 2")
    return frame


def _normalise_columns(columns: list[str]) -> list[str]:
    """Normalise header cells; ``<TICKVOL>`` and ``Tick Volume`` both mean volume.

    The one stateful case is MetaTrader's export, which splits the stamp over
    ``<DATE>`` and ``<TIME>``. The second of those has to become something
    other than ``time`` or it collides with the first, so the pass remembers
    whether a date column has already gone by -- locally, not in a module
    global that would leak into the next file read.
    """
    out: list[str] = []
    seen_date = False
    for name in columns:
        cleaned = name.strip().strip("<>").strip().lower().replace(" ", "_")
        if cleaned in ("date", "datetime", "timestamp"):
            seen_date = True
            resolved = "time"
        elif cleaned == "time":
            resolved = "timeonly" if seen_date else "time"
        else:
            resolved = _ALIASES.get(cleaned, cleaned)

        # Several source names can collapse onto one of ours -- a MetaTrader
        # export carries both <TICKVOL> and <VOL>, and both mean volume. Left
        # as duplicates, frame["volume"] returns a two-column frame and every
        # later operation on it fails in a way that points nowhere useful.
        # The first occurrence wins, which for that export is tick volume: the
        # only one an FX broker actually populates.
        if resolved in out:
            resolved = f"{resolved}_duplicate_{out.count(resolved)}"
        out.append(resolved)
    return out
