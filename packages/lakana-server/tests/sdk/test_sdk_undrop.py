"""The SDK's drop, `list_dropped` and `undrop` against the real server, in process."""

from __future__ import annotations

from pathlib import Path

import pytest

import lakana


def test_sdk_drop_list_dropped_and_undrop(lakana_client: lakana.Client, tmp_path: Path) -> None:
    """A drop is listed until it is restored; a purged one cannot be restored."""
    lakana_client.environments.create(
        "dev", storage={"scheme": "file", "root": f"file://{tmp_path}", "options": {}}
    )
    created = lakana_client.tables.create(
        "dev", "bronze.sales", [{"name": "id", "type": "long", "nullable": False}]
    )
    lakana_client.tables.delete("dev", "bronze.sales")

    (dropped,) = lakana_client.tables.list_dropped("dev", "bronze")
    assert (dropped.name, dropped.uuid, dropped.restorable) == ("sales", created.uuid, True)
    assert dropped.purge_after > dropped.dropped_at

    restored = lakana_client.tables.undrop("dev", "bronze.sales")
    assert restored.uuid == created.uuid
    assert lakana_client.tables.list_dropped("dev", "bronze") == []

    lakana_client.tables.delete("dev", "bronze.sales", purge=True)
    with pytest.raises(lakana.Conflict, match="undo period ended"):
        lakana_client.tables.undrop("dev", "bronze.sales")
    with pytest.raises(lakana.NotFound, match="--dropped"):
        lakana_client.tables.undrop("dev", "bronze.other")
