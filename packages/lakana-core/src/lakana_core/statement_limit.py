"""The longest SQL statement Lakana accepts, and the one message that says so."""

from __future__ import annotations

from lakana_core.errors import ValidationError


def statement_too_long_message(what: str, length: int, limit: int) -> str:
    """Write the refusal of a statement over the limit.

    Parameters
    ----------
    what
        What was too long, in plain words ("The SQL", "The row-filter expression").
    length
        Its length in characters.
    limit
        The limit in characters.

    Returns
    -------
    str
        A message that names the limit and the setting, says what to do next, and quotes none of
        the text.
    """
    return (
        f"{what} is {length} characters long; the most Lakana accepts is {limit} "
        "(LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS). Shorten it (a long query can be split into "
        "smaller steps, for example a view or table per step), or ask the operator to raise the "
        "limit."
    )


def check_statement_length(text: str, limit: int, what: str = "The SQL") -> None:
    """Refuse SQL text longer than `limit` characters before anything parses it.

    Parameters
    ----------
    text
        The SQL, or an expression, as it was submitted.
    limit
        `QueryLimitsConfig.max_statement_chars`.
    what
        What the text is, for the message.

    Raises
    ------
    ValidationError
        If `text` is longer than `limit`.
    """
    if len(text) > limit:
        raise ValidationError(statement_too_long_message(what, len(text), limit))
