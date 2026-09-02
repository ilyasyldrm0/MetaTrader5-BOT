"""CLI tests.

Chiefly: that `run` does not trade unless told to. The rest of this project can
be correct and still lose money if that default is wrong.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mt5bot.brokers.base import Broker
from mt5bot.brokers.paper import DryRunBroker, PaperBroker
from mt5bot.cli import main
from mt5bot.engine import Engine, EngineStats

SAMPLE = str(Path(__file__).parent / "data" / "eurusd_m15_sample.csv")


@pytest.fixture(autouse=True)
def quiet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep test runs from writing logs and journals into the repository."""
    monkeypatch.chdir(tmp_path)


class TestArgumentHandling:
    def test_help_exits_cleanly(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exit_info:
            main(["--help"])
        assert exit_info.value.code == 0
        assert "backtest" in capsys.readouterr().out

    def test_version(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exit_info:
            main(["--version"])
        assert exit_info.value.code == 0
        assert "mt5bot" in capsys.readouterr().out

    def test_a_command_is_required(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            main([])
        assert exit_info.value.code == 2

    def test_an_unknown_timeframe_is_reported_not_raised(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--timeframe", "M7", "doctor"]) == 1
        assert "M7" in capsys.readouterr().err

    def test_a_missing_config_file_is_reported(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--config", "nope.toml", "doctor"]) == 1
        assert "not found" in capsys.readouterr().err

    def test_an_invalid_config_value_is_reported(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text('[risk]\nsizing = "martingale"\n')
        assert main(["--config", str(bad), "doctor"]) == 1
        assert "sizing" in capsys.readouterr().err

    def test_an_unknown_config_key_is_reported(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A typo that silently leaves a default in place is the worst config bug."""
        bad = tmp_path / "typo.toml"
        bad.write_text("[risk]\nstop_loss = 40\n")
        assert main(["--config", str(bad), "doctor"]) == 1
        assert "unknown" in capsys.readouterr().err


class TestBacktestCommand:
    def test_it_reports_on_the_sample_data(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["backtest", "--csv", SAMPLE]) == 0
        assert "BACKTEST" in capsys.readouterr().out

    def test_the_trend_filter_can_be_overridden(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["backtest", "--csv", SAMPLE, "--trend-filter", "off"]) == 0
        out = capsys.readouterr().out
        assert "Win rate" in out and "filter: off" in out

    def test_costs_can_be_overridden(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["backtest", "--csv", SAMPLE, "--spread", "2.5", "--commission", "7"]) == 0
        assert "2.5 pip spread" in capsys.readouterr().out

    def test_without_a_csv_it_warns_that_the_data_is_generated(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Nobody should mistake a random walk for a strategy evaluation."""
        assert main(["backtest"]) == 0
        assert "random-walk" in capsys.readouterr().err

    def test_it_writes_a_journal_when_asked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        journal = tmp_path / "trades.csv"
        assert (
            main(["backtest", "--csv", SAMPLE, "--trend-filter", "off", "--journal", str(journal)])
            == 0
        )
        assert journal.exists()
        assert "trades written to" in capsys.readouterr().out

    def test_a_missing_price_file_is_reported(self) -> None:
        assert main(["backtest", "--csv", "nope.csv"]) == 1

    def test_an_unknown_trend_filter_is_rejected_by_argparse(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            main(["backtest", "--csv", SAMPLE, "--trend-filter", "sideways"])
        assert exit_info.value.code == 2


class TestRunCommand:
    def test_replay_drives_the_whole_engine_without_a_terminal(self) -> None:
        assert main(["run", "--replay", SAMPLE, "--max-cycles", "120"]) == 0

    def test_replay_does_not_wait_between_bars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`--replay` is documented as running at full speed, so it must not sleep.

        The engine's default is time.sleep; forgetting to override it here made
        a replay take a poll interval per bar.
        """
        import mt5bot.cli

        slept: list[float] = []
        monkeypatch.setattr(mt5bot.cli.time, "sleep", lambda s: slept.append(s))
        assert main(["run", "--replay", SAMPLE, "--max-cycles", "50"]) == 0
        assert slept == []

    def test_without_live_no_order_can_reach_the_broker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default that matters: `run` simulates unless --live is given."""
        broker = _broker_the_cli_would_use(monkeypatch, [])
        assert isinstance(broker, DryRunBroker)

    def test_live_reaches_the_real_broker_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        broker = _broker_the_cli_would_use(monkeypatch, ["--live"])
        assert isinstance(broker, _FakeMt5Broker)
        assert "LIVE TRADING" in capsys.readouterr().err


class TestDoctorCommand:
    def test_it_explains_itself_when_metatrader_is_absent(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """On Linux and macOS this is the expected path, and must not traceback."""
        assert main(["doctor"]) == 1
        out = capsys.readouterr().out
        assert "Configuration is valid" in out
        assert "Windows-only" in out
        assert "backtest" in out  # it points at what does work

    def test_it_reports_the_configured_symbol(self, capsys: pytest.CaptureFixture[str]) -> None:
        main(["--symbol", "GBPUSD", "doctor"])
        assert "GBPUSD" in capsys.readouterr().out


def _broker_the_cli_would_use(monkeypatch: pytest.MonkeyPatch, extra_args: list[str]) -> Broker:
    """Run `run` with the engine stubbed out, and report the broker it built."""
    import mt5bot.cli

    captured: dict[str, Broker] = {}

    class RecordingEngine(Engine):
        def run(self, *, max_cycles: int | None = None) -> EngineStats:
            captured["broker"] = self.broker
            return EngineStats()

    monkeypatch.setattr(mt5bot.cli, "Mt5Broker", _FakeMt5Broker)
    monkeypatch.setattr(mt5bot.cli, "Engine", RecordingEngine)
    assert main(["run", "--max-cycles", "1", *extra_args]) == 0
    return captured["broker"]


class _FakeMt5Broker(PaperBroker):
    """Stands in for the live adapter so the CLI's wiring can be tested."""

    def __init__(self, **kwargs: Any) -> None:
        from mt5bot.brokers.paper import synthetic_feed
        from mt5bot.config import Config

        super().__init__(
            synthetic_feed(bars=200),
            Config().symbol_spec(),
            magic=kwargs.get("magic", 234000),
            warmup=100,
        )

    @property
    def name(self) -> str:
        return "fake-live"
