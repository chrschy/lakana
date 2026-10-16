"""Importing the platform schemas emits no warnings."""

import subprocess
import sys


def test_importing_schemas_emits_no_warnings() -> None:
    """`ExternalTableOut.schema` used to shadow `BaseModel.schema` and warn on every start.

    Runs the import in a subprocess rather than popping and
    reimporting `lakana_server.api.platform.schemas` in this process: that
    approach permanently swapped the module object in `sys.modules` with no
    restore, which broke `test_tables_append.py`'s own `importlib.reload` of
    the same module whenever it ran after this test. A subprocess needs no
    such global-state mutation and no restoration to get right.
    """
    result = subprocess.run(
        [sys.executable, "-W", "error", "-c", "import lakana_server.api.platform.schemas"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_external_table_out_still_serializes_schema_key() -> None:
    """The wire format keeps the `schema` key."""
    from lakana_server.api.platform.schemas import ExternalTableOut

    fields = ExternalTableOut.model_json_schema()["properties"]
    assert "schema" in fields
