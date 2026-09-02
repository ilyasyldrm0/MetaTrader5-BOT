"""The trade journal: one CSV row per completed round trip.

Live runs and backtests write the same columns, so a session's results can be
compared with the simulation that justified running it -- which is the whole
point of keeping a journal rather than reading back the log.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from mt5bot.models import ClosedTrade

log = logging.getLogger(__name__)

FIELDNAMES: tuple[str, ...] = (
    "time_open",
    "time_close",
    "ticket",
    "symbol",
    "side",
    "volume",
    "price_open",
    "price_close",
    "profit",
    "exit_reason",
    "entry_reason",
)


class TradeJournal:
    """Appends closed trades to a CSV file.

    ``path=None`` makes every method a no-op, so callers never branch on
    whether journalling is switched on.
    """

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self._started = False

    def record(self, trade: ClosedTrade) -> None:
        if self.path is None:
            return
        try:
            self._ensure_header()
            with self.path.open("a", newline="", encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=FIELDNAMES).writerow(_row(trade))
        except OSError as exc:
            # A journal that cannot be written must not take the bot down with
            # a position open. Complain loudly and keep trading.
            log.error("could not write to the trade journal at %s: %s", self.path, exc)

    def record_all(self, trades: list[ClosedTrade]) -> None:
        for trade in trades:
            self.record(trade)

    def _ensure_header(self) -> None:
        if self._started:
            return
        self._started = True
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() or self.path.stat().st_size == 0:
            with self.path.open("w", newline="", encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=FIELDNAMES).writeheader()


def _row(trade: ClosedTrade) -> dict[str, object]:
    return {
        "time_open": trade.time_open.isoformat(sep=" ", timespec="seconds"),
        "time_close": trade.time_close.isoformat(sep=" ", timespec="seconds"),
        "ticket": trade.ticket,
        "symbol": trade.symbol,
        "side": trade.side.value,
        "volume": f"{trade.volume:.2f}",
        "price_open": trade.price_open,
        "price_close": trade.price_close,
        "profit": f"{trade.profit:.2f}",
        "exit_reason": trade.exit_reason,
        "entry_reason": trade.entry_reason,
    }
