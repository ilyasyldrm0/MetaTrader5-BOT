"""Exceptions raised across the bot.

Everything inherits from :class:`BotError` so a caller can catch the whole
family, and the CLI can turn any of them into a one-line message instead of a
traceback.
"""

from __future__ import annotations


class BotError(Exception):
    """Base class for every error this package raises deliberately."""


class ConfigError(BotError):
    """The configuration file or environment is missing or contradictory."""


class BrokerError(BotError):
    """The broker could not be reached, or refused a request outright."""


class DataUnavailableError(BrokerError):
    """Price history or a tick could not be read.

    Raised rather than returning ``None``: the original script fed a ``None``
    rate array straight into ``pandas.DataFrame`` and then indexed ``[-1]`` on
    the empty result, turning a closed market into an ``IndexError``.
    """
