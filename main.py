#!/usr/bin/env python3
"""Entry point, kept at the path the old script lived at.

The bot itself is the ``mt5bot`` package. This file only forwards, so that
``python main.py`` still works for anyone who bookmarked or cloned the
original single-file version.

Two things have changed that a returning user should know:

* **It no longer trades by default.** ``python main.py run`` reports what it
  would do; ``python main.py run --live`` actually sends orders.
* **Stop distances are pips now.** They always claimed to be, but the old code
  added them straight to the price, so a 50-pip target became a take profit at
  51.08 on EURUSD and every order was rejected. The numbers are unchanged; what
  they mean is finally what the README said.

Start with ``python main.py --help``.
"""

from mt5bot.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
