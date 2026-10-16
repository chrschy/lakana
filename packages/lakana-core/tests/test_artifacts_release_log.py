"""The INFO line of a cut release names the artifact escaped and bounded."""

from __future__ import annotations

import pytest

from lakana_core.artifacts.service import ArtifactService
from lakana_core.identifiers import NamespacePath, TableRef


def test_the_release_cut_line_escapes_and_bounds_the_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A table name with a newline and a great length cannot forge or flood a log line."""
    ref = TableRef("dev", NamespacePath.from_parts(["gold"]), "x\n2026 INFO forged " + "y" * 500)
    caplog.set_level("INFO", logger="lakana_core.artifacts.service")
    ArtifactService._release_cut(ref)
    [record] = caplog.records
    message = record.getMessage()
    assert "\n" not in message
    assert "\\n2026 INFO forged" in message
    assert len(message) < 400
