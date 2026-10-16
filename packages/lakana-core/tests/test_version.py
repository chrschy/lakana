"""Tests that the package metadata is wired up."""

from importlib.metadata import version


def test_lakana_core_has_a_version() -> None:
    """The installed package reports a non-empty version."""
    assert version("lakana-core")
