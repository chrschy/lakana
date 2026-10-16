"""Resolution of the SDK's installed version."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def resolve_version() -> str:
    """Resolve the SDK's installed version.

    Returns
    -------
    str
        The version from the installed package's metadata, or
        `"0+unknown"` when `lakana` is not installed (e.g. run from a source
        checkout with no editable install).
    """
    try:
        return version("lakana")
    except PackageNotFoundError:
        return "0+unknown"
