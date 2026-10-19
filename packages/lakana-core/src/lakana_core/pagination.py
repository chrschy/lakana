"""The cursor of the lists that page newest first: checks, runs, queries and deploys.

A cursor is the position of the last item of a page: its time and its id, and the list it
belongs to. A list resumes strictly after that position, so an item that arrives while a client
pages is never repeated and none is skipped. The kind of the list is part of the cursor, so a
cursor of one list is refused by another instead of being misread.
"""

from __future__ import annotations

import base64
import binascii
import enum
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar

from lakana_core.errors import ValidationError

_E = TypeVar("_E", bound=enum.StrEnum)

_BAD_CURSOR = (
    "The cursor is not valid. Start the listing again without `cursor`; pass the `next_cursor` of "
    "a page only to continue that listing."
)
_OTHER_LIST = (
    "The cursor is not valid for the `{expected}` list: it came from the `{actual}` list. "
    "Start the listing again without `cursor`; pass the `next_cursor` of a page only to "
    "continue that listing."
)


@dataclass(frozen=True)
class Position:
    """Where a page ended.

    Parameters
    ----------
    at
        The time of the last item (UTC).
    id
        Its id.
    part
        Which part of a list made of several the item is in; 0 for a list of one part.
    """

    at: datetime
    id: uuid.UUID
    part: int = 0


def encode_cursor(kind: str, at: datetime, row_id: uuid.UUID, part: int = 0) -> str:
    """Encode the position of a page's last item as an opaque cursor.

    Parameters
    ----------
    kind
        The list the cursor belongs to (``checks``, ``runs``, ``queries`` or ``deploys``).
    at
        The time the list is ordered by, of the last item (timezone-aware).
    row_id
        The item's id.
    part
        The part of the list the item is in, for a list that is read in parts.

    Returns
    -------
    str
        Unpadded base64url of ``{"k": kind, "o": at, "i": id}``, with ``"p"`` when `part` is not 0.
    """
    body: dict[str, str | int] = {"k": kind, "o": at.isoformat(), "i": str(row_id)}
    if part:
        body["p"] = part
    return base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()


def decode_cursor(kind: str, cursor: str) -> Position:
    """Decode a cursor produced by `encode_cursor` for the list `kind`.

    Parameters
    ----------
    kind
        The list being read.
    cursor
        The value a client sent back.

    Returns
    -------
    Position
        The position to resume after.

    Raises
    ------
    ValidationError
        If `cursor` is not one this server could have issued, or belongs to another list.
    """
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not (
            isinstance(data, dict)
            and isinstance(data.get("k"), str)
            and isinstance(data.get("o"), str)
            and isinstance(data.get("i"), str)
            and isinstance(data.get("p", 0), int)
        ):
            raise ValidationError(_BAD_CURSOR)
        at = datetime.fromisoformat(data["o"])
        if at.tzinfo is None:
            raise ValidationError(_BAD_CURSOR)
        at = at.astimezone(UTC)
        row_id = uuid.UUID(data["i"])
    except (binascii.Error, ValueError, OverflowError) as exc:
        raise ValidationError(_BAD_CURSOR) from exc
    if data["k"] != kind:
        raise ValidationError(_OTHER_LIST.format(expected=kind, actual=data["k"]))
    return Position(at=at, id=row_id, part=data.get("p", 0))


def parse_choices(
    name: str, values: Sequence[str], members: type[_E], *, any_case: bool = False
) -> tuple[_E, ...]:
    """Turn the values of a filter into members of an enum, or refuse the first that is none.

    Parameters
    ----------
    name
        The parameter, as the caller wrote it.
    values
        The values sent.
    members
        The enum they must be a value of.
    any_case
        Whether a value may be written in any letter case (``queued`` for ``QUEUED``).

    Returns
    -------
    tuple of enum.StrEnum
        The members, in the order given.

    Raises
    ------
    ValidationError
        If a value is not one of the enum; the message names the parameter and the allowed
        values.
    """
    allowed = {(m.value.casefold() if any_case else m.value): m for m in members}
    chosen: list[_E] = []
    for value in values:
        key = value.casefold() if any_case else value
        if key not in allowed:
            names = ", ".join(m.value for m in members)
            raise ValidationError(f"Unknown {name} {value!r}: `{name}` must be one of {names}.")
        chosen.append(allowed[key])
    return tuple(chosen)
