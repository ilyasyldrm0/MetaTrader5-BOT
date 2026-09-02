"""Command line interface: ``run``, ``backtest`` and ``doctor``.

The important decision here is the default. ``run`` does not trade. It
connects, evaluates and logs every order it *would* send, and stops there;
sending them requires ``--live``, typed on purpose. The script this replaces
began trading the moment it was executed, which is a poor property for a
program whose worst-case bug costs money.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import replace

from mt5bot import __version__
from mt5bot.backtest import format_report, run_backtest
from mt5bot.brokers.base import Broker
from mt5bot.brokers.mt5 import Mt5Broker
from mt5bot.brokers.paper import DryRunBroker, PaperBroker
from mt5bot.config import Config
from mt5bot.data import load_csv
from mt5bot.engine import Engine
from mt5bot.errors import BotError, BrokerError, ConfigError
from mt5bot.journal import TradeJournal
from mt5bot.logging_setup import configure_logging
from mt5bot.models import Timeframe

log = logging.getLogger("mt5bot")

LIVE_BANNER = """
################################################################
#  LIVE TRADING -- this session will send real orders.         #
#  Check the account below is the one you meant. Ctrl-C stops. #
################################################################
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mt5bot",
        description="An RSI + moving-average trading bot for MetaTrader 5.",
        epilog=(
            "Start with 'doctor' to check the connection, then 'backtest' to see how "
            "the strategy behaves, and only then 'run --live'."
        ),
    )
    parser.add_argument("--version", action="version", version=f"mt5bot {__version__}")
    parser.add_argument(
        "-c",
        "--config",
        metavar="PATH",
        help="TOML configuration file (defaults are used without one)",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="console verbosity (default: INFO, or whatever the config says)",
    )
    parser.add_argument("--symbol", help="override the configured symbol")
    parser.add_argument("--timeframe", help="override the configured timeframe, e.g. M15")

    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser(
        "run",
        help="run the strategy (simulated by default)",
        description=(
            "Evaluates the strategy against live prices. Without --live no order "
            "leaves the process: it reports what it would have done."
        ),
    )
    run.add_argument(
        "--live",
        action="store_true",
        help="actually send orders. Real money. Backtest first.",
    )
    run.add_argument(
        "--replay",
        metavar="CSV",
        help="drive the loop from a price file instead of a terminal, at full speed",
    )
    run.add_argument("--max-cycles", type=int, help="stop after this many polls")
    run.set_defaults(handler=_run)

    backtest = commands.add_parser(
        "backtest",
        help="replay the strategy over historical prices",
        description="Runs the same engine over a price file and reports what happened.",
    )
    backtest.add_argument("--csv", metavar="PATH", help="price data (see docs for the formats)")
    backtest.add_argument(
        "--trend-filter",
        choices=["aligned", "contrarian", "off"],
        help="override the moving-average filter for this run",
    )
    backtest.add_argument("--balance", type=float, help="starting balance")
    backtest.add_argument("--spread", type=float, metavar="PIPS", help="assumed spread")
    backtest.add_argument(
        "--commission", type=float, metavar="PER_LOT", help="assumed round-turn commission"
    )
    backtest.add_argument("--journal", metavar="PATH", help="write the closed trades here")
    backtest.set_defaults(handler=_backtest)

    doctor = commands.add_parser(
        "doctor",
        help="check the terminal, the account and the symbol",
        description=(
            "Verifies the configuration and, where MetaTrader 5 is available, "
            "reports what the broker says about your account and symbol."
        ),
    )
    doctor.set_defaults(handler=_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = _load_config(args)
    except BotError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    configure_logging(args.log_level or config.log_level, config.log_dir)

    try:
        return int(args.handler(args, config))
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130
    except BotError as exc:
        log.error("%s", exc)
        return 1


def _load_config(args: argparse.Namespace) -> Config:
    config = Config.load(args.config)
    trading = config.trading
    if args.symbol:
        trading = replace(trading, symbol=args.symbol)
    if args.timeframe:
        try:
            Timeframe.parse(args.timeframe)  # fail here, not deep inside the engine
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        trading = replace(trading, timeframe=args.timeframe.upper())
    config = replace(config, trading=trading)
    config.validate()
    return config


# -- run -------------------------------------------------------------------


def _run(args: argparse.Namespace, config: Config) -> int:
    strategy = config.build_strategy()
    # A replay has no reason to wait: its clock is the file, not the wall.
    # Leaving the default in place made --replay sleep a whole poll interval
    # per bar, so "at full speed" took an hour to cross a day of M15 data.
    pace: Callable[[float], None] = (lambda _seconds: None) if args.replay else time.sleep

    if args.replay:
        broker: Broker = PaperBroker(
            load_csv(args.replay),
            config.symbol_spec(),
            balance=config.backtest.balance,
            spread_pips=config.backtest.spread_pips,
            slippage_pips=config.backtest.slippage_pips,
            commission_per_lot=config.backtest.commission_per_lot,
            magic=config.trading.magic,
            warmup=strategy.min_bars - 1,
        )
        log.info("Replaying %s -- no terminal involved, no orders sent.", args.replay)
    else:
        live = Mt5Broker(
            magic=config.trading.magic,
            deviation=config.trading.deviation,
            terminal_path=config.terminal.path,
            login=config.terminal.login,
            password=config.terminal.password,
            server=config.terminal.server,
        )
        if args.live:
            print(LIVE_BANNER, file=sys.stderr)
            broker = live
        else:
            broker = DryRunBroker(live)
            log.info(
                "Dry run: prices are real, orders are not sent. Add --live when you "
                "are ready to trade for real."
            )

    engine = Engine(
        broker=broker,
        strategy=strategy,
        config=config,
        journal=TradeJournal(config.journal_path),
        sleep=pace,
    )
    engine.install_signal_handlers()
    stats = engine.run(max_cycles=args.max_cycles)
    return 0 if stats.errors == 0 else 1


# -- backtest --------------------------------------------------------------


def _backtest(args: argparse.Namespace, config: Config) -> int:
    if args.csv:
        feed = load_csv(args.csv)
    else:
        from mt5bot.brokers.paper import synthetic_feed

        log.warning(
            "No --csv given, so this runs on generated random-walk data. It exercises "
            "the machinery and says nothing whatever about the strategy."
        )
        feed = synthetic_feed(bars=4000, timeframe=config.timeframe)

    settings = config.backtest
    if args.balance is not None:
        settings = replace(settings, balance=args.balance)
    if args.spread is not None:
        settings = replace(settings, spread_pips=args.spread)
    if args.commission is not None:
        settings = replace(settings, commission_per_lot=args.commission)
    config = replace(config, backtest=settings)

    if args.trend_filter:
        config = replace(config, strategy=replace(config.strategy, trend_filter=args.trend_filter))
    config.validate()

    result = run_backtest(feed, config, journal=TradeJournal(args.journal))
    print(format_report(result, config))
    if args.journal:
        print(f"\n  {len(result.trades)} trades written to {args.journal}")
    return 0


# -- doctor ----------------------------------------------------------------


def _doctor(_args: argparse.Namespace, config: Config) -> int:
    # Takes _args only to match the shape every handler is dispatched with.
    symbol = config.trading.symbol
    print(f"mt5bot {__version__} on Python {sys.version.split()[0]} ({sys.platform})")
    print(f"\nConfiguration is valid. Trading {symbol} on {config.trading.timeframe}.")
    print(f"  Strategy: {config.build_strategy().describe()}")
    print(f"  History needed per evaluation: {config.build_strategy().min_bars} bars")

    broker = Mt5Broker(
        magic=config.trading.magic,
        terminal_path=config.terminal.path,
        login=config.terminal.login,
        password=config.terminal.password,
        server=config.terminal.server,
    )
    try:
        broker.connect()
    except BrokerError as exc:
        print(f"\nCannot reach a MetaTrader 5 terminal:\n  {exc}")
        print(
            "\nThat is expected off Windows. Everything except live trading still "
            "works:\n"
            "  mt5bot backtest --csv <file>\n"
            "  mt5bot run --replay <file>"
        )
        return 1

    try:
        account = broker.account()
        print(f"\nConnected to {account.server} as {account.login}")
        print(
            f"  Balance {account.balance:,.2f} {account.currency}   "
            f"equity {account.equity:,.2f}   free margin {account.margin_free:,.2f}   "
            f"leverage 1:{account.leverage}"
        )

        spec = broker.symbol_spec(symbol)
        tick = broker.tick(symbol)
        from mt5bot.risk import pip_size, spread_pips

        print(f"\n{symbol}")
        print(f"  Quote        bid {tick.bid} / ask {tick.ask}")
        print(f"  Spread       {spread_pips(tick, spec):.1f} pips")
        print(f"  Pip size     {pip_size(spec):g} ({spec.digits} digits, point {spec.point:g})")
        print(
            f"  Volume       min {spec.volume_min:g}, max {spec.volume_max:g}, "
            f"step {spec.volume_step:g}"
        )
        print(f"  Stops level  {spec.trade_stops_level} points minimum distance")
        print(f"  Fill policy  bitmask {spec.filling_mode}")

        bars = broker.bars(symbol, config.timeframe, config.build_strategy().min_bars)
        print(f"  History      {len(bars)} closed bars available, latest {bars['time'].iloc[-1]}")

        print("\nPaste this into your config so backtests match this broker:\n")
        print("[symbol]")
        for key, value in (
            ("digits", spec.digits),
            ("point", spec.point),
            ("volume_min", spec.volume_min),
            ("volume_max", spec.volume_max),
            ("volume_step", spec.volume_step),
            ("trade_tick_value", spec.trade_tick_value),
            ("trade_tick_size", spec.trade_tick_size),
            ("trade_stops_level", spec.trade_stops_level),
            ("contract_size", spec.contract_size),
        ):
            print(f"{key} = {value!r}")
    finally:
        broker.shutdown()

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
