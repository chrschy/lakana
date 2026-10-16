"""lakana-core's runtime dependencies include a Postgres driver."""

from importlib.metadata import requires


def test_lakana_core_requires_pg8000() -> None:
    """Team mode connects with `postgresql+pg8000://`, so the driver is a runtime dependency."""
    reqs = requires("lakana-core") or []
    assert any(r.split(";")[0].replace(" ", "").startswith("pg8000>=") for r in reqs), reqs


def test_lakana_core_requires_pyyaml() -> None:
    """M5b loads `lakana.yaml` and front-matter in core, so PyYAML is a runtime dependency."""
    reqs = requires("lakana-core") or []
    assert any(r.split(";")[0].replace(" ", "").lower().startswith("pyyaml>=") for r in reqs), reqs
