"""The SDK's table and environment retention against the real server, in process."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import lakana


def test_sdk_sets_shows_and_lists_retention(lakana_client: lakana.Client, tmp_path: Path) -> None:
    """Days, a day, never; the environment's default; the expiring listing and the warning."""
    lakana_client.environments.create(
        "dev", storage={"scheme": "file", "root": f"file://{tmp_path}", "options": {}}
    )
    lakana_client.tables.create(
        "dev", "bronze.sales", [{"name": "id", "type": "long", "nullable": False}]
    )
    assert lakana_client.tables.retention("dev", "bronze.sales").source is None

    shown = lakana_client.tables.set_retention("dev", "bronze.sales", days=3)
    assert (shown.source, shown.days, shown.set_by) == ("table", 3, "admin")
    assert shown.expires_at is not None
    assert shown.expires_at - datetime.now(UTC) < timedelta(days=3, minutes=1)
    table = lakana_client.tables.get("dev", "bronze.sales")
    assert table.expires_soon is True and table.expires_at == shown.expires_at
    (entry,) = lakana_client.tables.expiring("dev")
    assert (entry.namespace, entry.name) == ("bronze", "sales")
    assert lakana_client.tables.expiring("dev", within_days=1) == []

    day = datetime.now(UTC).date() + timedelta(days=40)
    assert lakana_client.tables.set_retention("dev", "bronze.sales", on=day).on == day
    kept = lakana_client.tables.set_retention("dev", "bronze.sales", never=True)
    assert (kept.never, kept.expires_at) == (True, None)

    env = lakana_client.environments.set_retention("dev", 30)
    assert env.retention_days == 30
    assert lakana_client.environments.get("dev").retention_days == 30
    assert lakana_client.environments.set_retention("dev", None).retention_days is None
    with pytest.raises(lakana.ValidationError, match="--days"):
        lakana_client.tables.set_retention("dev", "bronze.sales")
