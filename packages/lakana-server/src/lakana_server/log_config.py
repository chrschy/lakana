"""The logging configuration the server hands to uvicorn, so its lines are escaped too."""

from __future__ import annotations

import copy
from typing import Any

from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import AccessFormatter, DefaultFormatter

from lakana_core.log_setup import SafeFormatterMixin


class SafeDefaultFormatter(SafeFormatterMixin, DefaultFormatter):
    """Uvicorn's formatter for its error and start-up lines, with control characters escaped."""


class SafeAccessFormatter(SafeFormatterMixin, AccessFormatter):
    """Uvicorn's formatter for its access lines, with control characters escaped.

    The request line holds the path a client sent, which is attacker text.
    """


def uvicorn_log_config() -> dict[str, Any]:
    """Build uvicorn's own logging configuration, with the escaping formatters.

    Uvicorn's loggers do not propagate to the root handler `configure_logging` installs, so
    they would otherwise print a client's path with its control characters. Everything else
    is uvicorn's default (streams, logger names, levels). Colour is switched off: a colour is
    an escape sequence, which the formatter would write out as text, and the application's own
    lines have none.

    Returns
    -------
    dict
        A `logging.config.dictConfig` dictionary for `uvicorn.run(log_config=...)`; a fresh copy
        on every call.
    """
    config = copy.deepcopy(LOGGING_CONFIG)
    config["formatters"]["default"]["()"] = f"{__name__}.SafeDefaultFormatter"
    config["formatters"]["default"]["use_colors"] = False
    config["formatters"]["access"]["()"] = f"{__name__}.SafeAccessFormatter"
    config["formatters"]["access"]["use_colors"] = False
    return config
