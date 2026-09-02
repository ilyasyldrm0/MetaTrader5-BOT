"""Replaying a strategy over history, and scoring the result.

The backtester runs the *same* :class:`~mt5bot.engine.Engine` over the same
:class:`~mt5bot.strategy.base.Strategy` as a live session, with a
:class:`~mt5bot.brokers.paper.PaperBroker` in place of the terminal. There is
no separate simulation loop to drift out of step with the real one, which is
the usual reason a backtested strategy behaves differently in the market.

One number here is worth more than the profit figure: **signals per bar**. The
strategy's defaults reproduce conditions that are demanding enough to fire only
occasionally, and a report that says so plainly is more useful than a return
percentage computed from three trades.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from mt5bot.brokers.paper import PaperBroker
from mt5bot.config import Config
from mt5bot.engine import Engine, EngineStats
from mt5bot.journal import TradeJournal
from mt5bot.models import ClosedTrade, Position
from mt5bot.strategy.base import Strategy

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Everything a run produced, plus the metrics derived from it."""

    symbol: str
    timeframe: str
    strategy: str
    bars: int
    period_start: datetime
    period_end: datetime
    starting_balance: float
    ending_balance: float
    trades: list[ClosedTrade] = field(default_factory=list)
    stats: EngineStats = field(default_factory=EngineStats)
    still_open: Position | None = None

    # -- headline ----------------------------------------------------------

    @property
    def net_profit(self) -> float:
        return self.ending_balance - self.starting_balance

    @property
    def return_percent(self) -> float:
        if self.starting_balance <= 0:
            return 0.0
        return self.net_profit / self.starting_balance * 100.0

    # -- how often it traded at all ----------------------------------------

    @property
    def signals_per_1000_bars(self) -> float:
        """The metric that decides whether the rest of the report means anything.

        A strategy firing twice in four thousand bars has not been tested by
        those bars, whatever its win rate says.
        """
        if self.stats.bars_processed == 0:
            return 0.0
        return self.stats.signals / self.stats.bars_processed * 1000.0

    # -- trade quality -----------------------------------------------------

    @property
    def wins(self) -> list[ClosedTrade]:
        return [t for t in self.trades if t.profit > 0]

    @property
    def losses(self) -> list[ClosedTrade]:
        return [t for t in self.trades if t.profit <= 0]

    @property
    def win_rate(self) -> float:
        return len(self.wins) / len(self.trades) * 100.0 if self.trades else 0.0

    @property
    def gross_profit(self) -> float:
        return sum(t.profit for t in self.wins)

    @property
    def gross_loss(self) -> float:
        """Total of the losing trades, as a positive number."""
        return -sum(t.profit for t in self.losses)

    @property
    def profit_factor(self) -> float | None:
        """Gross profit over gross loss. ``None`` when nothing was ever lost."""
        return self.gross_profit / self.gross_loss if self.gross_loss > 0 else None

    @property
    def average_win(self) -> float:
        return self.gross_profit / len(self.wins) if self.wins else 0.0

    @property
    def average_loss(self) -> float:
        return -self.gross_loss / len(self.losses) if self.losses else 0.0

    @property
    def expectancy(self) -> float:
        """Average result per trade -- what one more trade is worth."""
        return self.net_profit / len(self.trades) if self.trades else 0.0

    @property
    def exit_breakdown(self) -> dict[str, int]:
        """How the trades ended: at the target, at the stop, or on a signal.

        Revealing when it disagrees with the stop and target that were
        configured -- mostly ``signal`` exits means the strategy is reversing
        out of trades before either level is reached, so the risk/reward the
        settings imply is not the one being traded.
        """
        counts: dict[str, int] = {}
        for trade in self.trades:
            counts[trade.exit_reason] = counts.get(trade.exit_reason, 0) + 1
        return dict(sorted(counts.items()))

    # -- drawdown ----------------------------------------------------------

    @property
    def equity_curve(self) -> list[float]:
        """Balance after each closed trade, starting from the opening balance."""
        equity = [self.starting_balance]
        for trade in self.trades:
            equity.append(equity[-1] + trade.profit)
        return equity

    @property
    def max_drawdown(self) -> float:
        """Largest peak-to-trough fall in the closed-trade equity curve.

        Measured on realised balance, so it understates what an account would
        have shown intrabar. Treat it as a floor on the pain, not a bound.
        """
        peak = self.starting_balance
        worst = 0.0
        for equity in self.equity_curve:
            peak = max(peak, equity)
            worst = max(worst, peak - equity)
        return worst

    @property
    def max_drawdown_percent(self) -> float:
        peak = self.starting_balance
        worst = 0.0
        for equity in self.equity_curve:
            peak = max(peak, equity)
            if peak > 0:
                worst = max(worst, (peak - equity) / peak * 100.0)
        return worst


def run_backtest(
    feed: pd.DataFrame,
    config: Config,
    strategy: Strategy | None = None,
    *,
    journal: TradeJournal | None = None,
) -> BacktestResult:
    """Replay ``feed`` through the engine and score what happened."""
    strategy = strategy or config.build_strategy()
    spec = config.symbol_spec()
    settings = config.backtest

    if len(feed) <= strategy.min_bars:
        raise ValueError(
            f"the feed has {len(feed)} bars but the strategy needs {strategy.min_bars} bars "
            "of warm-up before it can evaluate anything"
        )

    broker = PaperBroker(
        feed,
        spec,
        balance=settings.balance,
        spread_pips=settings.spread_pips,
        slippage_pips=settings.slippage_pips,
        commission_per_lot=settings.commission_per_lot,
        magic=config.trading.magic,
        # Start where the indicators are already warm, so the first evaluated
        # bar is a real decision rather than a frame full of NaN.
        warmup=strategy.min_bars - 1,
    )

    engine = Engine(
        broker=broker,
        strategy=strategy,
        config=config,
        journal=journal or TradeJournal(None),
        sleep=lambda _seconds: None,
    )
    stats = engine.run()

    open_positions = broker.positions()
    return BacktestResult(
        symbol=config.trading.symbol,
        timeframe=config.trading.timeframe,
        strategy=strategy.describe(),
        bars=len(feed),
        period_start=pd.Timestamp(feed["time"].iloc[0]).to_pydatetime(),
        period_end=pd.Timestamp(feed["time"].iloc[-1]).to_pydatetime(),
        starting_balance=broker.starting_balance,
        ending_balance=broker.balance,
        trades=list(broker.closed_trades),
        stats=stats,
        still_open=open_positions[0] if open_positions else None,
    )


def format_report(result: BacktestResult, config: Config) -> str:
    """Render the result as a plain-text report."""
    money = f"{_currency(config)}"
    lines = [
        "=" * 64,
        f"  BACKTEST  {result.symbol} {result.timeframe}",
        "=" * 64,
        f"  Strategy        {result.strategy}",
        f"  Period          {result.period_start:%Y-%m-%d %H:%M} to "
        f"{result.period_end:%Y-%m-%d %H:%M}",
        f"  Bars            {result.bars:,} ({result.stats.bars_processed:,} evaluated)",
        f"  Costs           {config.backtest.spread_pips:g} pip spread, "
        f"{config.backtest.slippage_pips:g} pip slippage, "
        f"{money}{config.backtest.commission_per_lot:g}/lot commission",
        "",
        "  ACTIVITY",
        f"    Signals              {result.stats.signals:,}"
        f"   ({result.signals_per_1000_bars:.1f} per 1000 bars)",
        f"    Trades opened        {result.stats.orders_opened:,}",
        f"    Trades closed        {len(result.trades):,}",
    ]

    if result.stats.skipped_spread:
        lines.append(f"    Skipped, spread      {result.stats.skipped_spread:,}")
    if result.stats.skipped_daily_cap:
        lines.append(f"    Skipped, daily cap   {result.stats.skipped_daily_cap:,}")
    if result.stats.orders_rejected:
        lines.append(f"    Rejected             {result.stats.orders_rejected:,}")

    if not result.trades:
        lines += [
            "",
            "  No trades were closed, so there is nothing to score.",
            "",
            _why_nothing_happened(result),
            "=" * 64,
        ]
        return "\n".join(lines)

    exits = ", ".join(f"{k}={v}" for k, v in result.exit_breakdown.items())
    profit_factor = (
        f"{result.profit_factor:.2f}" if result.profit_factor is not None else "no losses"
    )

    lines += [
        f"    Exits                {exits}",
        "",
        "  RESULT",
        f"    Starting balance     {money}{result.starting_balance:,.2f}",
        f"    Ending balance       {money}{result.ending_balance:,.2f}",
        f"    Net profit           {money}{result.net_profit:+,.2f} "
        f"({result.return_percent:+.2f}%)",
        f"    Max drawdown         {money}{result.max_drawdown:,.2f} "
        f"({result.max_drawdown_percent:.2f}%)",
        "",
        "  TRADE QUALITY",
        f"    Win rate             {result.win_rate:.1f}%  "
        f"({len(result.wins)} won, {len(result.losses)} lost)",
        f"    Profit factor        {profit_factor}",
        f"    Average win          {money}{result.average_win:+,.2f}",
        f"    Average loss         {money}{result.average_loss:+,.2f}",
        f"    Expectancy per trade {money}{result.expectancy:+,.2f}",
    ]

    if result.still_open is not None:
        lines += [
            "",
            f"  Note: a {result.still_open.side} position was still open when the data ran",
            "        out. It is excluded above rather than closed at an invented price.",
        ]

    lines += ["", _health_warning(result), "=" * 64]
    return "\n".join([line for line in lines if line is not None])


def _currency(config: Config) -> str:
    return "$" if config.backtest.balance else ""


def _why_nothing_happened(result: BacktestResult) -> str:
    if result.stats.signals == 0:
        return (
            "  The strategy never signalled. With the default filter a buy needs RSI\n"
            "  oversold *and* price above its moving average at the same time, which is\n"
            '  an uncommon combination. Try trend_filter = "contrarian" or "off" in the\n'
            "  config to see how much of the silence comes from the filter."
        )
    return (
        "  Signals fired but no trade closed. Check the spread limit, the daily cap\n"
        "  and whether the lot size fell below the broker's minimum."
    )


def _health_warning(result: BacktestResult) -> str:
    """Say plainly when the sample is too thin to conclude anything."""
    if len(result.trades) < 30:
        return (
            f"  CAUTION: {len(result.trades)} closed trades is far too few to judge a\n"
            "  strategy. Results this size are dominated by luck; treat them as a check\n"
            "  that the plumbing works, not as evidence of an edge."
        )
    return (
        "  Past results do not predict future ones, and this simulation ignores\n"
        "  swap, requotes, variable spread and slippage beyond the fixed figure above."
    )
