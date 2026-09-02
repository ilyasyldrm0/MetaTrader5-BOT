"""Configuration: a TOML file, with secrets from the environment.

The original kept its settings as eight module-level variables in the middle
of the script, so changing the symbol meant editing the program and there was
nowhere to put a broker password that was not the repository.

TOML is read with the standard library's ``tomllib`` (Python 3.11+), so
configuration costs no dependency.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, TypeVar

from mt5bot.errors import ConfigError
from mt5bot.models import SymbolSpec, Timeframe
from mt5bot.strategy import RsiSmaStrategy, TrendFilter

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class TradingConfig:
    """What to trade and how often to look."""

    symbol: str = "EURUSD"
    timeframe: str = "M15"
    #: Tags this bot's orders and filters its positions, so it never touches a
    #: trade opened by hand or by another strategy on the same account.
    magic: int = 234000
    #: Slippage tolerance in points. The original sent none at all, which means
    #: zero tolerance, so any tick arriving first requoted the order.
    deviation: int = 20
    #: Seconds between polls. Derived from the timeframe when unset. Signals
    #: are only ever acted on once per closed bar, so this controls how
    #: promptly a new bar is noticed, not how often the bot trades.
    poll_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """Settings for the RSI + moving-average strategy."""

    rsi_period: int = 14
    sma_period: int = 12
    oversold: float = 30.0
    overbought: float = 70.0
    trend_filter: str = "aligned"
    require_cross: bool = False


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Stop distances, sizing and the guards around them.

    The defaults are the original's numbers -- a 50-pip target and a 25-pip
    stop -- now meaning what the README always claimed they meant. In the old
    code they were added to the price directly, which is why no order was ever
    accepted.
    """

    sl_pips: float = 25.0
    tp_pips: float = 50.0
    #: ``"risk"`` sizes each trade to lose ``risk_percent`` at its stop;
    #: ``"fixed"`` always sends ``fixed_lot``.
    sizing: str = "risk"
    fixed_lot: float = 0.10
    risk_percent: float = 1.0
    #: Refuse to enter when the spread is wider than this. ``null`` disables.
    max_spread_pips: float | None = 3.0
    #: A circuit breaker. A strategy that starts flip-flopping cannot burn the
    #: account in spread and commission before anyone notices.
    max_trades_per_day: int = 10


@dataclass(frozen=True, slots=True)
class TerminalConfig:
    """How to reach the MetaTrader 5 terminal.

    Credentials belong in the environment, not the file: MT5_LOGIN,
    MT5_PASSWORD, MT5_SERVER and MT5_PATH override anything set here. Leave
    them unset to attach to whatever account the running terminal is already
    logged into.
    """

    path: str | None = None
    login: int | None = None
    password: str | None = None
    server: str | None = None


@dataclass(frozen=True, slots=True)
class SymbolConfig:
    """Contract details used when there is no broker to ask.

    A backtest still has to know what a pip is worth and what lot sizes are
    legal. Live runs read all of this from the terminal; a CSV cannot supply
    it, so it is configured here.

    The defaults describe a standard 5-digit EURUSD on a 100k contract. They
    are a reasonable starting point and a poor substitute for the real thing:
    run ``mt5bot doctor`` against your own broker and paste the block it
    prints, or the backtest will size positions for somebody else's account.
    """

    digits: int = 5
    point: float = 0.00001
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    trade_tick_value: float = 1.0
    trade_tick_size: float = 0.00001
    trade_stops_level: int = 0
    contract_size: float = 100_000.0


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Execution assumptions for simulated fills.

    Defaults are deliberately not free: a zero-spread, zero-commission
    backtest is the standard way to make a losing strategy look profitable.
    """

    balance: float = 10_000.0
    spread_pips: float = 1.0
    slippage_pips: float = 0.0
    commission_per_lot: float = 0.0


@dataclass(frozen=True, slots=True)
class Config:
    """The whole configuration."""

    trading: TradingConfig = TradingConfig()
    strategy: StrategyConfig = StrategyConfig()
    risk: RiskConfig = RiskConfig()
    terminal: TerminalConfig = TerminalConfig()
    symbol: SymbolConfig = SymbolConfig()
    backtest: BacktestConfig = BacktestConfig()
    log_level: str = "INFO"
    log_dir: str | None = "logs"
    journal_path: str | None = "journal.csv"

    # -- derived ----------------------------------------------------------

    @property
    def timeframe(self) -> Timeframe:
        return Timeframe.parse(self.trading.timeframe)

    @property
    def poll_seconds(self) -> int:
        """How long to wait between polls.

        Defaults to a fifteenth of the bar length, bounded to 5..60 seconds:
        often enough that a new bar is picked up promptly, rarely enough not
        to hammer the terminal. The original slept a flat 10 seconds and
        re-evaluated the *forming* bar each time.
        """
        if self.trading.poll_seconds is not None:
            return max(1, self.trading.poll_seconds)
        return max(5, min(60, self.timeframe.seconds // 15))

    def symbol_spec(self) -> SymbolSpec:
        """The configured contract, for use when no broker can be queried."""
        c = self.symbol
        return SymbolSpec(
            symbol=self.trading.symbol,
            digits=c.digits,
            point=c.point,
            volume_min=c.volume_min,
            volume_max=c.volume_max,
            volume_step=c.volume_step,
            trade_tick_value=c.trade_tick_value,
            trade_tick_size=c.trade_tick_size,
            trade_stops_level=c.trade_stops_level,
            contract_size=c.contract_size,
        )

    def build_strategy(self) -> RsiSmaStrategy:
        s = self.strategy
        try:
            trend_filter = TrendFilter(s.trend_filter)
        except ValueError:
            valid = ", ".join(f.value for f in TrendFilter)
            raise ConfigError(
                f"unknown trend_filter {s.trend_filter!r}; expected one of: {valid}"
            ) from None
        try:
            return RsiSmaStrategy(
                rsi_period=s.rsi_period,
                sma_period=s.sma_period,
                oversold=s.oversold,
                overbought=s.overbought,
                trend_filter=trend_filter,
                require_cross=s.require_cross,
            )
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

    def validate(self) -> None:
        """Check everything that cannot be expressed in the types themselves."""
        try:
            self.timeframe
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

        if self.risk.sizing not in ("risk", "fixed"):
            raise ConfigError(f"risk.sizing must be 'risk' or 'fixed', got {self.risk.sizing!r}")
        if self.risk.sizing == "risk" and self.risk.sl_pips <= 0:
            raise ConfigError("risk.sizing = 'risk' needs a positive risk.sl_pips to size against")
        if self.risk.sl_pips <= 0:
            raise ConfigError(f"risk.sl_pips must be positive, got {self.risk.sl_pips}")
        if self.risk.tp_pips <= 0:
            raise ConfigError(f"risk.tp_pips must be positive, got {self.risk.tp_pips}")
        if not 0 < self.risk.risk_percent <= 100:
            raise ConfigError(
                f"risk.risk_percent must be in (0, 100], got {self.risk.risk_percent}"
            )
        if self.risk.max_trades_per_day < 1:
            raise ConfigError("risk.max_trades_per_day must be at least 1")
        if self.backtest.spread_pips < 0 or self.backtest.slippage_pips < 0:
            raise ConfigError("backtest spread and slippage cannot be negative")
        self.build_strategy()

    # -- loading ----------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        """Read a TOML file, then let the environment override the secrets.

        A missing path yields the defaults, so the bot runs out of the box.
        A path that was asked for and does not exist is an error, because
        silently ignoring it would run live with settings nobody chose.
        """
        data: dict[str, Any] = {}
        if path is not None:
            file = Path(path)
            if not file.is_file():
                raise ConfigError(f"config file not found: {file}")
            try:
                data = tomllib.loads(file.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{file} is not valid TOML: {exc}") from exc

        config = _build(cls, data, where="config")
        return config._with_environment()

    def _with_environment(self) -> Config:
        env = {
            "path": os.environ.get("MT5_PATH"),
            "login": os.environ.get("MT5_LOGIN"),
            "password": os.environ.get("MT5_PASSWORD"),
            "server": os.environ.get("MT5_SERVER"),
        }
        if not any(env.values()):
            return self

        login: int | None = self.terminal.login
        if env["login"]:
            try:
                login = int(env["login"])
            except ValueError:
                raise ConfigError(f"MT5_LOGIN must be a number, got {env['login']!r}") from None

        from dataclasses import replace

        return replace(
            self,
            terminal=TerminalConfig(
                path=env["path"] or self.terminal.path,
                login=login,
                password=env["password"] or self.terminal.password,
                server=env["server"] or self.terminal.server,
            ),
        )


def _build(target: type[T], data: dict[str, Any], *, where: str) -> T:
    """Construct a (possibly nested) dataclass from parsed TOML.

    Unknown keys are an error rather than being ignored. A typo in a config
    file that silently does nothing is the worst kind: the bot runs, appears
    configured, and trades on a default the user thought they had replaced.
    """
    assert is_dataclass(target)
    known = {f.name: f for f in fields(target)}

    unknown = set(data) - set(known)
    if unknown:
        suggestions = ", ".join(sorted(known))
        raise ConfigError(
            f"unknown {where} key(s): {', '.join(sorted(unknown))}. Valid keys: {suggestions}"
        )

    kwargs: dict[str, Any] = {}
    for name, value in data.items():
        field_type = known[name].type
        nested = _nested_dataclass(field_type)
        if nested is not None:
            if not isinstance(value, dict):
                raise ConfigError(f"{where}.{name} must be a table, got {type(value).__name__}")
            kwargs[name] = _build(nested, value, where=f"{where}.{name}")
        else:
            kwargs[name] = value

    try:
        return target(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"invalid {where}: {exc}") from exc


_SECTIONS: dict[str, type] = {
    "TradingConfig": TradingConfig,
    "StrategyConfig": StrategyConfig,
    "RiskConfig": RiskConfig,
    "TerminalConfig": TerminalConfig,
    "BacktestConfig": BacktestConfig,
    "SymbolConfig": SymbolConfig,
}


def _nested_dataclass(annotation: Any) -> type | None:
    """Resolve a field annotation to a config section, if it names one.

    ``from __future__ import annotations`` makes every annotation a string, so
    the name is looked up rather than inspected.
    """
    return _SECTIONS.get(
        annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")
    )
