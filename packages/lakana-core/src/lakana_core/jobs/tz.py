"""Time zones, always read from the ``tzdata`` package.

``zoneinfo`` looks in the system's database first, so two machines can know different rules (or a
slim container none at all). A deploy that accepts a zone and a scheduler that then computes with
another version of it would disagree about when a job fires. Reading the files of ``tzdata``
directly, and nowhere else, gives every machine the same answer. The global ``zoneinfo`` search
path is left alone, so other code is not affected.
"""

from __future__ import annotations

import re
from functools import lru_cache
from importlib import resources
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_NAME: Final = re.compile(r"[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+)*")
"""What an IANA zone name looks like: path parts of letters, digits and ``_+-``; no dots, so no
``..`` and no file such as ``zone.tab``."""


@lru_cache(maxsize=1)
def _known_zones() -> frozenset[str]:
    """Read the names of the zones `tzdata` has.

    Returns
    -------
    frozenset of str
        The names, as written (case matters). A missing `tzdata` package raises, so a broken
        install is not reported as an unknown zone.
    """
    text = resources.files("tzdata").joinpath("zones").read_text(encoding="utf-8")
    return frozenset(text.split())


@lru_cache(maxsize=512)
def load_zone(name: str) -> ZoneInfo:
    """Load an IANA time zone from the ``tzdata`` package.

    Parameters
    ----------
    name
        An IANA zone name such as ``Europe/Berlin`` or ``UTC``.

    Returns
    -------
    ZoneInfo
        The zone, with ``name`` as its key. The same object for the same name.

    Raises
    ------
    ZoneInfoNotFoundError
        If the name is not written like a zone name or is not in ``tzdata``'s list of zones (case
        matters, on every machine).
    ModuleNotFoundError
        If the ``tzdata`` package is not installed.
    OSError
        If a listed zone cannot be read: a broken install, not an unknown zone.
    """
    if not _NAME.fullmatch(name) or name not in _known_zones():
        raise ZoneInfoNotFoundError(f"'No time zone found with key {name}'")
    node = resources.files("tzdata.zoneinfo").joinpath(*name.split("/"))
    with node.open("rb") as handle:
        return ZoneInfo.from_file(handle, key=name)
