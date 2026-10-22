"""The manifest stored in `job_versions.manifest_json`."""

from __future__ import annotations

import dataclasses
import logging

import pytest
from test_projects_dag import make_asset

from lakana_core.projects.dag import build_graph
from lakana_core.projects.frontmatter import AssetMetadata
from lakana_core.projects.manifest import (
    JOBS_VERSION,
    MANIFEST_VERSION,
    VERSION_WITHOUT_CHECKS,
    JobNotify,
    asset_kind,
    build_manifest,
    entry_decimals,
    entry_input_kinds,
    entry_inputs,
    entry_keep_versions,
    entry_output,
    job_notify,
    manifest_artifacts,
    manifest_assets,
    manifest_jobs,
    manifest_lock_sha256,
    manifest_python,
    manifest_tables,
    python_entry,
)
from lakana_core.projects.parse import ParsedAsset
from lakana_core.projects.project_config import ResolvedProjectConfig, parse_project_config


def test_manifest_records_assets_edges_inputs_and_config() -> None:
    """Every asset with its write mode, the DAG, external inputs and the resolved config."""
    assets = [
        make_asset("bronze.raw", ("landing.events",), metadata=AssetMetadata(materialize="view")),
        make_asset(
            "silver.clean",
            ("bronze.raw",),
            metadata=AssetMetadata(materialize="incremental", incremental_key="ts", owner="me"),
        ),
        make_asset("gold.daily", ("silver.clean",)),
    ]
    config = ResolvedProjectConfig(repo="analytics", properties={"team": "x"})
    manifest = build_manifest(assets, build_graph(assets), config)
    assert manifest["version"] == VERSION_WITHOUT_CHECKS
    assert [a["table"] for a in manifest["assets"]] == ["bronze.raw", "gold.daily", "silver.clean"]
    entries = manifest_assets(manifest)
    assert (entries["bronze.raw"]["write_mode"], entries["bronze.raw"]["first_write_mode"]) == (
        "view",
        "view",
    )
    assert (entries["gold.daily"]["write_mode"], entries["gold.daily"]["first_write_mode"]) == (
        "replace",
        "create",
    )
    # An incremental asset's first run creates its table; later runs append.
    assert (entries["silver.clean"]["write_mode"], entries["silver.clean"]["first_write_mode"]) == (
        "append",
        "create",
    )
    assert entries["silver.clean"]["incremental_key"] == "ts"
    assert entries["silver.clean"]["metadata"]["owner"] == "me"
    assert manifest["edges"] == [["bronze.raw", "silver.clean"], ["silver.clean", "gold.daily"]]
    assert manifest["external_inputs"] == ["landing.events"]
    assert manifest["config"]["repo"] == "analytics"
    assert manifest["config"]["properties"] == {"team": "x"}
    assert manifest_tables(manifest) == {"bronze.raw", "silver.clean", "gold.daily"}


def test_manifest_records_each_assets_run_policy() -> None:
    """Retries, delay and timeout are resolved at deploy time, against the caps."""
    from lakana_core.projects.run_policy import RunCaps

    assets = [
        make_asset("gold.a", metadata=AssetMetadata(materialize="table", retries=1)),
        make_asset("gold.b"),
    ]
    manifest = build_manifest(
        assets,
        build_graph(assets),
        ResolvedProjectConfig(),
        caps=RunCaps(max_retries=5, max_timeout_seconds=300),
    )
    entries = manifest_assets(manifest)
    assert (
        entries["gold.a"]["retries"],
        entries["gold.a"]["retry_delay_seconds"],
        entries["gold.a"]["timeout_seconds"],
    ) == (1, 30, 300)
    assert entries["gold.b"]["retries"] == 2


def test_manifest_pins_every_field() -> None:
    """The whole shape M5c reads: a new or renamed key must change this test on purpose."""
    from lakana_core.projects.run_policy import RunCaps

    asset = make_asset(
        "silver.clean",
        ("bronze.raw",),
        metadata=AssetMetadata(
            materialize="incremental",
            incremental_key="ts",
            evolve_schema=True,
            description="d",
            owner="me",
            tags=("a", "b"),
            columns={"a": "the a"},
            retries=1,
            retry_delay=10,
            timeout=60,
        ),
    )
    config = ResolvedProjectConfig(repo="analytics")
    manifest = build_manifest([asset], build_graph([asset]), config, caps=RunCaps())
    assert manifest == {
        "version": VERSION_WITHOUT_CHECKS,
        "assets": [
            {
                "table": "silver.clean",
                "path": "assets/silver/clean.sql",
                "kind": "sql",
                "materialize": "incremental",
                "write_mode": "append",
                "first_write_mode": "create",
                "incremental_key": "ts",
                "evolve_schema": True,
                "retries": 1,
                "retry_delay_seconds": 10,
                "timeout_seconds": 60,
                "metadata": {
                    "description": "d",
                    "owner": "me",
                    "tags": ["a", "b"],
                    "columns": {"a": "the a"},
                },
                "sql": asset.sql,
                "normalized_sql": asset.normalized_sql,
                "references": ["bronze.raw"],
                "columns": ["a"],
                "lineage": {},
            }
        ],
        "edges": [],
        "external_inputs": ["bronze.raw"],
        "config": config.model_dump(mode="json", exclude={"jobs"}),
    }


def _python_asset() -> ParsedAsset:
    """Build a parsed Python asset.

    Returns
    -------
    ParsedAsset
        `gold.scores`, reading `silver.clean` as `obs`.
    """
    return ParsedAsset(
        table="gold.scores",
        path="assets/gold/scores.py",
        block_line=4,
        body_line=5,
        metadata=AssetMetadata(materialize="table", columns={"score": "0-1"}),
        sql="",
        normalized_sql="python:sha256:" + "a" * 64,
        references=("silver.clean",),
        columns=None,
        lineage={},
        kind="python",
        inputs=(("obs", "silver.clean"),),
    )


def test_a_python_entry_records_kind_inputs_and_the_lock() -> None:
    assets = [_python_asset()]
    manifest = build_manifest(
        assets, build_graph(assets), ResolvedProjectConfig(), lock_sha256="b" * 64
    )
    assert manifest["version"] == 3
    assert manifest["lock_sha256"] == "b" * 64
    (entry,) = manifest["assets"]
    assert entry["kind"] == "python"
    assert entry["inputs"] == {"obs": "silver.clean"}
    assert (entry["write_mode"], entry["first_write_mode"]) == ("replace", "create")
    assert entry["sql"] == ""
    assert entry["metadata"]["columns"] == {"score": "0-1"}
    assert asset_kind(entry) == "python"
    assert entry_inputs(entry) == {"obs": "silver.clean"}
    assert manifest_lock_sha256(manifest) == "b" * 64


def test_the_manifest_names_the_registered_indexes_the_lock_uses() -> None:
    assets = [_python_asset()]
    manifest = build_manifest(
        assets,
        build_graph(assets),
        ResolvedProjectConfig(),
        lock_sha256="b" * 64,
        package_indexes=["team", "alpha"],
    )
    assert manifest["version"] == 3
    assert manifest["package_indexes"] == ["alpha", "team"]
    plain = build_manifest(
        assets, build_graph(assets), ResolvedProjectConfig(), lock_sha256="b" * 64
    )
    assert "package_indexes" not in plain


def test_a_sql_only_manifest_has_no_lock_key() -> None:
    assets = [make_asset("silver.a")]
    manifest = build_manifest(assets, build_graph(assets), ResolvedProjectConfig())
    assert "lock_sha256" not in manifest
    assert "inputs" not in manifest["assets"][0]
    assert manifest["assets"][0]["kind"] == "sql"
    assert manifest_lock_sha256(manifest) is None


def test_an_entry_without_kind_reads_as_sql() -> None:
    """A project version deployed by an older release."""
    assert asset_kind({"table": "silver.a", "sql": "SELECT 1"}) == "sql"
    assert asset_kind({"kind": "wasm"}) == "sql"
    assert asset_kind({"kind": 7}) == "sql"


@pytest.mark.parametrize(
    "inputs", [None, "x", ["a"], {"a": 1}, {1: "a.b"}, {"a": "a.b", "b": None}]
)
def test_malformed_inputs_read_back_as_empty(inputs: object) -> None:
    assert entry_inputs({"inputs": inputs}) == {}


@pytest.mark.parametrize("value", [None, 7, "short", "G" * 64, "A" * 64, "a" * 64 + "\n"])
def test_a_malformed_lock_digest_reads_back_as_none(value: object) -> None:
    assert manifest_lock_sha256({"lock_sha256": value}) is None


@pytest.mark.parametrize(
    ("entry", "output", "keep", "kinds"),
    [
        ({"kind": "python", "inputs": {"a": "x.a"}}, "table", None, {"a": "table"}),
        (
            {
                "kind": "python",
                "output": "artifact",
                "keep_versions": 3,
                "inputs": {"m": "models.m", "t": "gold.t"},
                "input_kinds": {"m": "artifact", "t": "table", "ghost": "artifact"},
            },
            "artifact",
            3,
            {"m": "artifact", "t": "table"},
        ),
        ({"kind": "sql", "output": "artifact", "keep_versions": 3}, "table", None, {}),
        (
            {"kind": "python", "inputs": {"m": "models.m"}, "input_kinds": ["artifact"]},
            "table",
            None,
            {"m": "table"},
        ),
        (
            {"kind": "python", "inputs": {"m": "models.m"}, "input_kinds": {"m": "blob"}},
            "table",
            None,
            {"m": "table"},
        ),
        # Untrusted shapes beyond the above: each reads as the safe default.
        (None, "table", None, {}),
        ([], "table", None, {}),
        ("x", "table", None, {}),
        (3, "table", None, {}),
        ({"output": "artifact", "keep_versions": 3}, "table", None, {}),
        ({"kind": "Python", "output": "artifact"}, "table", None, {}),
        ({"kind": "python ", "output": "artifact"}, "table", None, {}),
        ({"kind": ["python"], "output": "artifact"}, "table", None, {}),
        ({"kind": "python", "output": "Artifact"}, "table", None, {}),
        ({"kind": "python", "output": "artifact "}, "table", None, {}),
        ({"kind": "python", "output": None}, "table", None, {}),
        ({"kind": "python", "output": True}, "table", None, {}),
        ({"kind": "python", "output": "artifact", "keep_versions": 0}, "artifact", None, {}),
        ({"kind": "python", "output": "artifact", "keep_versions": -1}, "artifact", None, {}),
        ({"kind": "python", "output": "artifact", "keep_versions": True}, "artifact", None, {}),
        ({"kind": "python", "output": "artifact", "keep_versions": 2.0}, "artifact", None, {}),
        ({"kind": "python", "output": "artifact", "keep_versions": "2"}, "artifact", None, {}),
        ({"kind": "python", "output": "artifact", "keep_versions": None}, "artifact", None, {}),
        ({"kind": "python", "output": "artifact", "keep_versions": 10**30}, "artifact", None, {}),
        (
            {"kind": "python", "output": "artifact", "keep_versions": 2**31 - 1},
            "artifact",
            2**31 - 1,
            {},
        ),
        (
            {"kind": "python", "output": "artifact", "keep_versions": 2**31},
            "artifact",
            None,
            {},
        ),
        (
            {"kind": "python", "inputs": {"m": "models.m"}, "input_kinds": {"m": "Artifact"}},
            "table",
            None,
            {"m": "table"},
        ),
        (
            {"kind": "python", "inputs": {"m": "models.m"}, "input_kinds": {"m": " artifact"}},
            "table",
            None,
            {"m": "table"},
        ),
        (
            {"kind": "python", "inputs": {"m": "models.m"}, "input_kinds": {"m": ["artifact"]}},
            "table",
            None,
            {"m": "table"},
        ),
        (
            {"kind": "python", "inputs": {"m": "models.m"}, "input_kinds": {"m": True}},
            "table",
            None,
            {"m": "table"},
        ),
        (
            {"kind": "python", "inputs": {"m": "models.m"}, "input_kinds": None},
            "table",
            None,
            {"m": "table"},
        ),
        (
            {"kind": "python", "inputs": {"m": "models.m"}, "input_kinds": "artifact"},
            "table",
            None,
            {"m": "table"},
        ),
        (
            {"kind": "python", "inputs": {"m": 7}, "input_kinds": {"m": "artifact"}},
            "table",
            None,
            {},
        ),
        (
            {"kind": "python", "inputs": ["m"], "input_kinds": {"m": "artifact"}},
            "table",
            None,
            {},
        ),
    ],
)
def test_a_stored_entry_is_read_defensively(
    entry: object, output: str, keep: int | None, kinds: dict[str, str]
) -> None:
    assert entry_output(entry) == output
    assert entry_keep_versions(entry) == keep
    assert entry_input_kinds(entry) == kinds


def test_python_entry_reads_only_the_first_entry_of_a_name() -> None:
    sql = {"table": "m.c", "kind": "sql"}
    python = {"table": "m.c", "kind": "python", "output": "artifact"}
    assert python_entry({"assets": [sql, python]}, "m.c") is None
    assert python_entry({"assets": [python, sql]}, "m.c") == python
    assert python_entry({"assets": [3, python]}, "m.c") == python
    assert python_entry({"assets": [python]}, "m.other") is None
    for manifest in (None, [], "x", {}, {"assets": "x"}):
        assert python_entry(manifest, "m.c") is None


def _artifact_asset() -> ParsedAsset:
    """Build a parsed artifact asset.

    Returns
    -------
    ParsedAsset
        `models.churn`, reading `gold.scores` and `models.base`.
    """
    return dataclasses.replace(
        _python_asset(),
        table="models.churn",
        path="assets/models/churn.py",
        metadata=AssetMetadata(materialize="table"),
        references=("gold.scores", "models.base"),
        inputs=(("scores", "gold.scores"), ("base", "models.base")),
        output="artifact",
        keep_versions=2,
    )


def test_manifest_version_3_records_output_retention_and_input_kinds() -> None:
    consumer = dataclasses.replace(
        _python_asset(),
        table="gold.predictions",
        references=("models.churn", "silver.clean"),
        inputs=(("model", "models.churn"), ("rows", "silver.clean")),
    )
    assets = [_python_asset(), _artifact_asset(), consumer]
    manifest = build_manifest(
        assets,
        build_graph(assets),
        ResolvedProjectConfig(),
        artifacts=frozenset({"models.base"}),
    )
    assert manifest["version"] == 3
    entries = manifest_assets(manifest)
    churn = entries["models.churn"]
    assert (churn["output"], churn["keep_versions"]) == ("artifact", 2)
    assert (churn["materialize"], churn["write_mode"], churn["first_write_mode"]) == (
        "artifact",
        "artifact",
        "artifact",
    )
    assert churn["input_kinds"] == {"scores": "table", "base": "artifact"}
    assert entries["gold.predictions"]["input_kinds"] == {"model": "artifact", "rows": "table"}
    scores = entries["gold.scores"]
    assert (scores["output"], scores["keep_versions"], scores["input_kinds"]) == (
        "table",
        None,
        {"obs": "table"},
    )
    assert (scores["write_mode"], scores["first_write_mode"]) == ("replace", "create")
    assert manifest_artifacts(manifest) == {"models.churn"}
    assert manifest_tables(manifest) == {"gold.scores", "models.churn", "gold.predictions"}
    assert entry_output(churn) == "artifact"
    assert entry_keep_versions(churn) == 2
    assert entry_input_kinds(churn) == {"scores": "table", "base": "artifact"}
    assert ("models.churn", "gold.predictions") in [tuple(edge) for edge in manifest["edges"]]


def test_a_table_asset_never_records_retention() -> None:
    asset = dataclasses.replace(_python_asset(), keep_versions=4)
    manifest = build_manifest([asset], build_graph([asset]), ResolvedProjectConfig())
    assert manifest["assets"][0]["keep_versions"] is None


def test_a_sql_entry_has_no_artifact_fields_and_older_manifests_have_no_artifacts() -> None:
    assets = [make_asset("silver.a", ("bronze.events",))]
    manifest = build_manifest(assets, build_graph(assets), ResolvedProjectConfig())
    (entry,) = manifest["assets"]
    assert not {"output", "keep_versions", "input_kinds"} & set(entry)
    assert manifest_artifacts(manifest) == set()
    assert (
        manifest_artifacts({"version": 2, "assets": [{"table": "gold.t", "kind": "python"}]})
        == set()
    )
    assert manifest_artifacts({}) == set()
    assert manifest_artifacts({"assets": "x"}) == set()
    assert manifest_artifacts({"assets": [3, {"table": 4, "output": "artifact"}]}) == set()


@pytest.mark.parametrize(
    "damaged",
    [{"assets": [3]}, {"assets": 3}, {"assets": None}, {"assets": "x"}, {}, [1], None, 7],
)
def test_a_damaged_manifest_has_no_assets(damaged: object) -> None:
    """A stored manifest is untrusted: it indexes to nothing and never raises."""
    assert manifest_assets(damaged) == {}
    assert manifest_tables(damaged) == set()
    assert manifest_artifacts(damaged) == set()


def test_damaged_entries_are_skipped_and_the_rest_is_read() -> None:
    good = {"table": "m.a", "kind": "python", "output": "artifact"}
    manifest = {"assets": [3, good, {"kind": "python"}, {"table": 4}, "s", None, ["m.b"]]}
    assert manifest_assets(manifest) == {"m.a": good}
    assert manifest_tables(manifest) == {"m.a"}
    assert manifest_artifacts(manifest) == {"m.a"}


def test_skipped_entries_of_a_stored_manifest_are_counted_in_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """m5: a damaged entry is skipped, and a person can learn that it was, once per manifest."""
    manifest = {"assets": [3, "x", {"kind": "python"}, {"table": "a.b"}, None]}
    with caplog.at_level(logging.WARNING, logger="lakana_core.projects.manifest"):
        assert manifest_assets(manifest) == {"a.b": {"table": "a.b"}}
    (record,) = caplog.records
    assert record.getMessage() == (
        "A stored deploy manifest has 4 malformed asset entries, which are skipped; deploy the "
        "repository again to replace it."
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="lakana_core.projects.manifest"):
        assert manifest_assets({"assets": [{"table": "a.b"}]}) == {"a.b": {"table": "a.b"}}
        assert manifest_assets(None) == {}
    assert caplog.records == []


@pytest.mark.parametrize(
    ("entry", "decimals"),
    [
        ({"kind": "python", "decimals": "exact"}, "exact"),
        ({"kind": "python", "decimals": "float"}, "float"),
        # an entry deployed before the option existed means the default
        ({"kind": "python"}, "float"),
        ({"kind": "python", "decimals": None}, "float"),
        ({"kind": "python", "decimals": "EXACT"}, "float"),
        ({"kind": "python", "decimals": ["exact"]}, "float"),
        ({"kind": "python", "decimals": True}, "float"),
        ({"kind": "sql", "decimals": "exact"}, "float"),
        (None, "float"),
        ("exact", "float"),
    ],
)
def test_a_stored_entry_reads_decimals_defensively(entry: object, decimals: str) -> None:
    assert entry_decimals(entry) == decimals


@pytest.mark.parametrize("decimals", ["float", "exact"])
def test_a_python_entry_records_decimals_and_the_manifest_version_is_unchanged(
    decimals: str,
) -> None:
    """The key is optional and read with a default, so version 3 stays version 3."""
    asset = dataclasses.replace(_python_asset(), decimals=decimals)
    manifest = build_manifest([asset], build_graph([asset]), ResolvedProjectConfig())
    assert manifest["version"] == VERSION_WITHOUT_CHECKS == 3
    assert MANIFEST_VERSION == 6
    assert manifest["assets"][0]["decimals"] == decimals


def test_a_sql_entry_has_no_decimals_key() -> None:
    assets = [make_asset("silver.a", ("bronze.events",))]
    manifest = build_manifest(assets, build_graph(assets), ResolvedProjectConfig())
    assert "decimals" not in manifest["assets"][0]


_JOBS_YAML = """\
jobs:
  daily:
    select: ["silver.*"]
    upstream: true
    schedule: { cron: "0 6 * * *", timezone: Europe/Berlin }
  by_hand:
    select: ["gold.*"]
environments:
  dev:
    jobs:
      daily: { schedule: null }
"""


def _jobs_manifest(environment: str) -> dict:
    config = parse_project_config(_JOBS_YAML).resolved_for(environment)
    assets = [make_asset("silver.a"), make_asset("gold.b", ("silver.a",))]
    return build_manifest(assets, build_graph(assets), config)


def test_a_manifest_with_jobs_and_no_notify_is_version_5_and_resolved_per_environment() -> None:
    prod = _jobs_manifest("prod")
    assert prod["version"] == JOBS_VERSION == 5
    assert prod["jobs"] == {
        "by_hand": {
            "select": ["gold.*"],
            "upstream": False,
            "downstream": False,
            "schedule": None,
        },
        "daily": {
            "select": ["silver.*"],
            "upstream": True,
            "downstream": False,
            "schedule": {"cron": "0 6 * * *", "timezone": "Europe/Berlin"},
        },
    }
    assert _jobs_manifest("dev")["jobs"]["daily"]["schedule"] is None
    assert "jobs" not in prod["config"]


def test_a_manifest_without_jobs_has_no_jobs_key_and_keeps_its_version() -> None:
    assets = [make_asset("silver.a")]
    manifest = build_manifest(assets, build_graph(assets), ResolvedProjectConfig())
    assert "jobs" not in manifest
    assert manifest["version"] == VERSION_WITHOUT_CHECKS


def test_manifest_jobs_reads_a_damaged_value_as_no_jobs() -> None:
    assert manifest_jobs(_jobs_manifest("prod"))["by_hand"]["select"] == ["gold.*"]
    for damaged in (None, [], "x", {"jobs": []}, {"jobs": {"a": 1}}, {"jobs": {1: {}}}):
        assert manifest_jobs(damaged) == {}


_NOTIFY_YAML = """\
jobs:
  daily:
    select: ["silver.*"]
    notify: { webhook: team-alerts, format: slack }
  by_hand:
    select: ["gold.*"]
environments:
  dev:
    jobs:
      daily: { notify: null }
"""


def _notify_manifest(environment: str) -> dict:
    config = parse_project_config(_NOTIFY_YAML).resolved_for(environment)
    assets = [make_asset("silver.a"), make_asset("gold.b", ("silver.a",))]
    return build_manifest(assets, build_graph(assets), config)


def test_a_manifest_with_notify_is_version_6_and_has_it_resolved_for_the_environment() -> None:
    prod = _notify_manifest("prod")
    assert prod["version"] == MANIFEST_VERSION == 6
    assert prod["jobs"]["daily"]["notify"] == {"webhook": "team-alerts", "format": "slack"}
    assert "notify" not in prod["jobs"]["by_hand"]
    dev = _notify_manifest("dev")
    assert dev["version"] == 5  # no job notifies there, so an older worker can read it
    assert "notify" not in dev["jobs"]["daily"]


def test_job_notify_reads_a_manifest_entry_and_a_damaged_one_as_none() -> None:
    manifest = _notify_manifest("prod")
    assert job_notify(manifest, "daily") == JobNotify("team-alerts", "slack")
    assert job_notify(manifest, "by_hand") is None
    assert job_notify(manifest, "missing") is None
    for damaged in (None, [], {"jobs": {"a": 1}}, {"jobs": {"a": {"notify": "x"}}}):
        assert job_notify(damaged, "a") is None
    for bad in (
        {"webhook": 1, "format": "json"},
        {"webhook": "w", "format": "email"},
        {"webhook": "w"},
        {"format": "json"},
    ):
        assert job_notify({"jobs": {"a": {"notify": bad}}}, "a") is None


def test_the_manifest_records_python_only_when_the_project_names_one() -> None:
    """An older worker reads no `python` key and runs on its own interpreter, as before."""
    config = ResolvedProjectConfig()
    graph = build_graph([])

    assert "python" not in build_manifest([], graph, config)
    named = build_manifest([], graph, config, python="3.12")
    assert named["python"] == "3.12"
    assert manifest_python(named) == "3.12"
    assert manifest_python({"python": "3.9"}) is None
    assert manifest_python({}) is None
