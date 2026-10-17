"""The one message a reader gets when a query under a row filter or column mask cannot compute.

A row filter or column mask expression runs on every stored value, including the ones the policy
hides. When the expression or the reader's own SQL fails on such a value, the query engine's
error text quotes it. On a read under a policy that text is replaced, in full, by the message
built here, so the text of an error never carries a stored value.
"""

from __future__ import annotations

from lakana_core.identifiers import shown

POLICY_READ_FAILED_PREFIX = "The query failed on a value it could not convert or compute"
"""What every form of the message starts with; `is_policy_read_failure` matches on it."""

_UNDER = ", under a row filter or column mask on "
_ADVICE = (
    "; the value is not shown. Check the casts and functions in the query. If the query has "
    "none that could fail, a row filter or column mask on the table fails on some stored "
    "values: ask an administrator to check it (for example with try_cast or TRY)."
)


def policy_read_failure(table: str | None) -> str:
    """Word a failed read under a row filter or column mask without the failing value.

    Parameters
    ----------
    table
        The dotted name of the table the policy applies to, when exactly one table of the
        query has a policy; `None` for the variant that names none.

    Returns
    -------
    str
        The fixed message. A table name is escaped with `shown`.
    """
    where = "one of the tables" if table is None else f"`{shown(table)}`"
    return f"{POLICY_READ_FAILED_PREFIX}{_UNDER}{where}{_ADVICE}"


def is_policy_read_failure(text: str | None) -> bool:
    """Say whether a stored error text is the message of `policy_read_failure`.

    Parameters
    ----------
    text
        A stored error text, if any.

    Returns
    -------
    bool
        `True` for either form of the message.
    """
    return text is not None and text.startswith(POLICY_READ_FAILED_PREFIX)
