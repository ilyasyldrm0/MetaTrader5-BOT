"""CSV loading tests -- including MetaTrader's own export layout."""

from __future__ import annotations

from pathlib import Path

import pytest

from mt5bot.data import load_csv
from mt5bot.errors import ConfigError

PLAIN = (
    "time,open,high,low,close,volume\n"
    "2024-01-01 00:00,1.08000,1.08100,1.07900,1.08050,120\n"
    "2024-01-01 00:15,1.08050,1.08200,1.08000,1.08150,90\n"
    "2024-01-01 00:30,1.08150,1.08250,1.08100,1.08200,75\n"
)

#: What *Tools -> History Centre -> Export* writes: tab separated, angle
#: brackets, and the stamp split over two columns.
METATRADER_EXPORT = (
    "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
    "2024.01.01\t00:00:00\t1.08000\t1.08100\t1.07900\t1.08050\t120\t0\t10\n"
    "2024.01.01\t00:15:00\t1.08050\t1.08200\t1.08000\t1.08150\t90\t0\t10\n"
    "2024.01.01\t00:30:00\t1.08150\t1.08250\t1.08100\t1.08200\t75\t0\t10\n"
)


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestLoadCsv:
    def test_reads_the_plain_layout(self, tmp_path: Path) -> None:
        frame = load_csv(write(tmp_path, "plain.csv", PLAIN))
        assert list(frame.columns) == ["time", "open", "high", "low", "close", "volume"]
        assert len(frame) == 3
        assert frame["close"].iloc[-1] == pytest.approx(1.08200)

    def test_reads_the_metatrader_export(self, tmp_path: Path) -> None:
        frame = load_csv(write(tmp_path, "mt5.csv", METATRADER_EXPORT))
        assert list(frame.columns) == ["time", "open", "high", "low", "close", "volume"]
        assert len(frame) == 3
        assert str(frame["time"].iloc[0]) == "2024-01-01 00:00:00"
        assert frame["volume"].iloc[0] == pytest.approx(120)

    def test_both_layouts_produce_the_same_frame(self, tmp_path: Path) -> None:
        import pandas as pd

        a = load_csv(write(tmp_path, "a.csv", PLAIN))
        b = load_csv(write(tmp_path, "b.csv", METATRADER_EXPORT))
        pd.testing.assert_frame_equal(a, b, check_dtype=False)

    def test_reading_two_files_in_a_row_does_not_corrupt_the_second(self, tmp_path: Path) -> None:
        """Header handling must not carry state between calls."""
        load_csv(write(tmp_path, "mt5.csv", METATRADER_EXPORT))
        frame = load_csv(write(tmp_path, "plain.csv", PLAIN))
        assert list(frame.columns) == ["time", "open", "high", "low", "close", "volume"]

    def test_rows_are_sorted_by_time(self, tmp_path: Path) -> None:
        shuffled = (
            "time,open,high,low,close\n"
            "2024-01-01 00:30,3,3,3,3\n"
            "2024-01-01 00:00,1,1,1,1\n"
            "2024-01-01 00:15,2,2,2,2\n"
        )
        frame = load_csv(write(tmp_path, "s.csv", shuffled))
        assert frame["close"].tolist() == [1.0, 2.0, 3.0]

    def test_a_missing_volume_column_defaults_to_zero(self, tmp_path: Path) -> None:
        text = "time,open,high,low,close\n2024-01-01,1,1,1,1\n2024-01-02,2,2,2,2\n"
        assert load_csv(write(tmp_path, "v.csv", text))["volume"].tolist() == [0.0, 0.0]

    def test_rows_with_unusable_prices_are_dropped(self, tmp_path: Path) -> None:
        text = PLAIN + "2024-01-01 00:45,,,,,\n"
        assert len(load_csv(write(tmp_path, "gap.csv", text))) == 3


class TestLoadCsvErrors:
    def test_a_missing_file_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_csv(tmp_path / "nope.csv")

    def test_missing_price_columns_are_named(self, tmp_path: Path) -> None:
        text = "time,open,close\n2024-01-01,1,1\n2024-01-02,2,2\n"
        with pytest.raises(ConfigError, match="missing the column"):
            load_csv(write(tmp_path, "bad.csv", text))

    def test_a_file_with_one_usable_row_is_refused(self, tmp_path: Path) -> None:
        text = "time,open,high,low,close\n2024-01-01,1,1,1,1\n"
        with pytest.raises(ConfigError, match="need at least 2"):
            load_csv(write(tmp_path, "one.csv", text))
