"""The forms a credential takes in text a task writes, so that each form can be hidden.

The worker hides these in the log it captures from a task; the task's harness hides the same
forms in the texts it writes (`lakana.context.secret_forms`, which the SDK keeps as its own
copy because it cannot import this package; a test compares the two).
"""

from __future__ import annotations

import json
from urllib.parse import quote, quote_plus

MIN_SECRET_CHARS = 4
"""Shortest form that is hidden: a shorter one would hide ordinary text."""


def secret_forms(value: str) -> set[str]:
    """List the forms text can carry a credential in.

    Parameters
    ----------
    value
        The credential.

    Returns
    -------
    set of str
        The value itself; as it appears inside a Python `repr`, a JSON string (with and
        without ``ensure_ascii``), a URL (`quote` with its default ``/`` kept and with nothing
        kept, `quote_plus`) and the `repr` of its UTF-8 bytes. Forms shorter than
        `MIN_SECRET_CHARS` are left out.
    """
    found = {
        value,
        repr(value)[1:-1],
        json.dumps(value)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
        quote(value),
        quote(value, safe=""),
        quote_plus(value),
        repr(value.encode())[2:-1],
    }
    return {form for form in found if len(form) >= MIN_SECRET_CHARS}
