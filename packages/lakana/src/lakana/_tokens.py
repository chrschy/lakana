"""Token masking, shared by `Client.__repr__` and `ResolvedConfig.__repr__`.

A single implementation exists so a fix to the masking logic (for example,
widening it to a new token shape) never needs to be made twice.
"""

from __future__ import annotations

import re

_TOKEN_PATTERN = re.compile(r"^(lakana_(?:pat|task)_)([^_]+)_")
"""Matches a `lakana_pat_`/`lakana_task_` token's prefix and key id.

The key id (`[^_]+`) is defined as everything up to the *first* remaining
underscore. The secret segment that follows is deliberately never captured
or inspected: `secrets.token_urlsafe`-style secrets routinely contain their
own underscores and dashes, so masking by a fixed `rsplit("_", 1)` would
leave most of the secret exposed. Anchoring on the key id's own boundary
instead means the secret, whatever it looks like, is never part of the
match.
"""


def mask_token(token: str) -> str:
    """Mask a token's secret part for safe display.

    Parameters
    ----------
    token
        The full token value.

    Returns
    -------
    str
        `<prefix><key_id>_***` for a recognised `lakana_pat_`/`lakana_task_`
        token shape; `"***"` for anything else, including a token that is
        missing its trailing secret segment.
    """
    match = _TOKEN_PATTERN.match(token)
    if match is None:
        return "***"
    prefix, key_id = match.group(1), match.group(2)
    return f"{prefix}{key_id}_***"
