# MetaTrader5-BOT

[![CI](https://github.com/ilyasyldrm0/MetaTrader5-BOT/actions/workflows/ci.yml/badge.svg)](https://github.com/ilyasyldrm0/MetaTrader5-BOT/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE.md)

An RSI + moving-average trading bot for MetaTrader 5, in Python.

It ships with a backtester, a paper-trading mode, a `doctor` command for
checking your broker's contract details, and a test suite that runs without
MetaTrader installed. **It does not trade unless you pass `--live`.**

*[Türkçe README](README.tr.md)*

---

## If you used the earlier version, read this

The 2023 single-file `main.py` could not place an order. Not "traded badly" --
could not place one at all:

```python
trade_profit = 50                       # "in pips", said the README
take_profit  = last_close + trade_profit
```

On EURUSD at 1.08 that asks for a take profit at **51.08** and a stop at
**-23.92**, a negative price. Every order came back `INVALID_STOPS`. The close
handler was malformed too, so no position could be closed either.

Three things changed that will surprise a returning user:

| | Then | Now |
|---|---|---|
| **Stop distances** | added to the price, so 50 meant 50.0 | 50 means 50 pips, as the README always claimed |
| **On startup** | traded immediately | simulates; `--live` is required to send orders |
| **Entry point** | `python main.py` | `python main.py run` (`main.py` still works, and `--help` lists the rest) |

The default numbers are unchanged. What they *mean* is finally what was
documented.

---

## Quick start

```bash
git clone https://github.com/ilyasyldrm0/MetaTrader5-BOT
cd MetaTrader5-BOT
pip install -e .
```

Check the strategy against history first -- this needs no MetaTrader and works
on any operating system:

```bash
python main.py backtest --csv tests/data/eurusd_m15_sample.csv
```

Then, on Windows with the terminal running:

```bash
python main.py doctor       # is everything reachable and configured?
python main.py run          # dry run: real prices, no orders sent
python main.py run --live   # real orders. Read the rest of this file first.
```

---

## Requirements

* **Python 3.11+**
* `pandas` and `numpy` -- installed with the package
* **MetaTrader 5 (Windows only)** for live trading: `pip install MetaTrader5`

The `MetaTrader5` package is Windows-only. That constraint shaped the design:
everything above the broker interface is plain Python, so **backtesting, paper
trading and the entire test suite run on Linux and macOS**. Only `run --live`
needs Windows. On Linux you can also run the terminal under Wine, or use
`run --replay` against exported data.

---

## Commands

### `doctor` -- check before you trade

Validates your configuration, connects, and reports what your broker actually
says about your account and symbol: spread, pip size, lot step, minimum stop
distance and fill policy. It finishes by printing a `[symbol]` block you can
paste into your config so backtests size positions for *your* account rather
than a textbook one.

```bash
python main.py doctor
python main.py --symbol GBPUSD doctor
```

### `backtest` -- measure before you commit

```bash
python main.py backtest --csv data/EURUSD_M15.csv
python main.py backtest --csv data/EURUSD_M15.csv --trend-filter off --commission 7
python main.py backtest --csv data/EURUSD_M15.csv --journal trades.csv
```

Two CSV layouts are read without reformatting: a plain
`time,open,high,low,close,volume` file, and MetaTrader's own tab-separated
export from *Tools → History Centre → Export*.

The backtester runs the **same engine and the same strategy object** as a live
session, with a simulated broker in place of the terminal. There is no second
simulation loop to drift away from the real one.

It also declines to flatter you:

* costs are stated on every run, and a 1-pip spread is charged by default
* fewer than 30 closed trades prints a caution -- that is not a sample
* a bar touching both the stop and the target is settled as a **loss**, since
  bars say nothing about the order prices were visited in
* a bar that gaps past a level fills at the open, not the level
* a position still open when the data ends is excluded, not closed at an
  invented price

### `run` -- trade, or pretend to

```bash
python main.py run                       # dry run (default)
python main.py run --live                # real orders
python main.py run --replay data.csv     # drive the engine from a file, full speed
python main.py run --live --config config.toml
```

Dry run uses real prices, spreads and contract details, and logs every order it
*would* have sent. Nothing reaches the terminal.

---

## The strategy, honestly

The original conditions, preserved as the defaults:

```
RSI(14) <= 30  AND  close > SMA(12)   ->  BUY
RSI(14) >= 70  AND  close < SMA(12)   ->  SELL
```

Read carefully, that is *buy the dip while the trend still points up* -- a
reasonable idea. But the two halves pull against each other. Whatever drives
RSI(14) below 30 on a 15-minute chart has almost always dragged price under a
12-period average as well, so requiring both at once asks for a state that
barely occurs.

How rare? Over the bundled 4,000-bar sample:

| `trend_filter` | Signals | Per 1000 bars |
|---|---|---|
| `aligned` (the original) | **0** | 0.0 |
| `contrarian` | 413 | 105.1 |
| `off` | 413 | 105.1 |

Zero. And note that `contrarian` and `off` are *identical* -- disabling the
moving average changes nothing, because every oversold bar already had price
below the average. That is the same fact seen from the other side.

This is not a bug report. The bot is doing exactly what it is configured to do,
and the condition is rare rather than impossible: a long decline, a pause long
enough for the average to catch down, then a modest recovery will satisfy both
halves at once. There is a test that constructs precisely that shape.

The point is that you can now argue about the configuration with a number
instead of an impression. Change one thing at a time:

| Setting | Effect |
|---|---|
| `trend_filter = "contrarian"` | classic mean reversion: buy oversold *because* price is below the average |
| `trend_filter = "off"` | trade the RSI thresholds alone |
| `oversold` / `overbought` | 35/65 fires considerably more often than 30/70 |
| `require_cross = true` | signal only on the bar a threshold is crossed |

> The bundled `tests/data/eurusd_m15_sample.csv` is a **deterministic random
> walk, not market data**. It has no trend, no session structure and no news,
> so it exercises the machinery and tells you nothing about whether a strategy
> makes money. Backtest your own history before drawing any conclusion. The
> signal-frequency finding above is structural -- it follows from how RSI and a
> moving average relate -- but the profit figures on that file are meaningless.

---

## Configuration

Copy `config.example.toml` to `config.toml` and edit. Every key has a default,
so you can delete whatever you do not care about. Unknown keys are **rejected**
rather than ignored -- a typo that silently leaves a default in place is the
config bug that costs the most.

```toml
[trading]
symbol    = "EURUSD"
timeframe = "M15"

[strategy]
trend_filter = "aligned"     # aligned | contrarian | off

[risk]
sl_pips      = 25.0          # pips. Not price.
tp_pips      = 50.0
sizing       = "risk"        # size each trade to lose risk_percent at the stop
risk_percent = 1.0
max_spread_pips    = 3.0
max_trades_per_day = 10
```

Credentials go in the environment, never the file -- see `.env.example`:

```bash
export MT5_LOGIN=12345678
export MT5_PASSWORD=...
export MT5_SERVER=YourBroker-Demo
```

With none of those set, the bot attaches to whichever account the running
terminal is already logged into.

### Position sizing

`sizing = "risk"` sizes each trade so that hitting the stop costs
`risk_percent` of the balance, converted through the symbol's own tick
economics rather than a hard-coded "$10 a pip" (which is wrong for JPY pairs,
wrong for a non-USD account and wrong for every CFD).

$10,000 at 1% risk over a 25-pip stop gives 0.40 lots. When that stop fills, the
loss is $100 -- exactly the 1% that was asked for. Set `sizing = "fixed"` to
send `fixed_lot` every time instead.

---

## What else was fixed

Beyond the stop-distance bug at the top of this file:

* **RSI was not Wilder's RSI.** It averaged gains and losses with a plain
  rolling mean, so readings disagreed with every chart you could compare
  against. A run of up bars also divided by an average loss of zero, and a flat
  market produced `0/0` -- a `NaN`, which compares `False` against every
  threshold, so the bot went quiet instead of reporting "no trend".
* **26 bars of history** were requested for a 14-period RSI. That is twelve
  bars past the seed, where the seed still carries about 41% of the reading's
  weight. It now takes five periods.
* **Signals came from the candle still forming.** Reading from bar position 0
  and polling every 10 seconds gave ninety chances to trade on a number that
  had not settled. It now evaluates once per *closed* bar.
* **Orders were priced from the last bar close**, up to fifteen minutes stale,
  with no slippage tolerance and a hard-coded IOC fill policy that brokers
  requiring FOK reject outright.
* **`order_send` can return `None`.** Reading `.retcode` off it raised
  `AttributeError` and killed the process with a position open.
* **Position state lived in two variables** that were never cleared after a
  close and could not notice a stop loss filling. The bot would go on believing
  it held a position that no longer existed. State is now re-read from the
  broker every cycle.
* **A failed close still opened the reverse position**, leaving the account
  hedged and double-margined. The close is now verified twice -- from the order
  result, and by re-reading what is open.
* **No error handling at all.** One exception ended the process with money in
  the market. Cycles are now caught and backed off, and Ctrl-C unwinds cleanly.

---

## Project layout

```
main.py                  entry point, forwards to the package
mt5bot/
  cli.py                 run | backtest | doctor
  config.py              TOML + environment, validated
  models.py              Side, Signal, Tick, Position, SymbolSpec, ...
  indicators.py          Wilder RSI, SMA, EMA, ATR -- pure functions
  risk.py                pip arithmetic, stop placement, position sizing
  engine.py              the trading loop
  backtest.py            replay and metrics
  brokers/
    base.py              the interface everything above is written against
    mt5.py               the live adapter -- the only MetaTrader-aware file
    paper.py             simulated execution, for backtests and dry runs
  strategy/
    rsi_sma.py           the strategy, configurable
```

The `brokers/base.py` seam is what makes the rest testable: the MetaTrader
package cannot be installed on a CI runner, and the bugs above are exactly the
kind only a test catches.

---

## Development

```bash
pip install -e ".[dev]"

ruff check . && ruff format --check .
mypy mt5bot
pytest
```

All of it runs without MetaTrader installed. CI runs the same on Python 3.11,
3.12 and 3.13.

To add a strategy, implement `Strategy` in `mt5bot/strategy/base.py`: given a
frame of closed bars, return an `Evaluation`. The indicators in
`mt5bot.indicators` are pure functions you can build on, and anything
implementing the interface can be backtested before it is given money.

---

## Risk

**This is educational software. Trading leveraged instruments loses money.**

* Test on a **demo account** first, for long enough to see it handle a losing
  streak, a weekend gap and a disconnection.
* A backtest is a hypothesis, not a forecast. This one ignores swap, requotes,
  variable spread, and slippage beyond the fixed figure you configure.
* The sample data is synthetic. Results on it mean nothing.
* Nobody has verified this strategy is profitable. The evidence in this README
  points the other way.
* Never risk money you cannot afford to lose.

MIT licensed -- see [LICENSE.md](LICENSE.md). No warranty of any kind.
