"""Whether this worker can run Python assets, and where it caches."""

from __future__ import annotations

from pathlib import Path

import pytest

import lakana_worker.python_support as support_module
from lakana_core.config import RunConfig
from lakana_worker.python_support import CANNOT_RUN, PythonSupport, detect, worker_cache_dir


def test_reason_names_what_is_missing() -> None:
    assert PythonSupport().reason() == "LAKANA_RUN__SERVER_URL is not set"
    assert (
        PythonSupport(server_url="http://server:8000").reason() == "uv is not on the worker's PATH"
    )
    assert PythonSupport(server_url="http://server:8000", uv="/usr/local/bin/uv").reason() is None
    assert CANNOT_RUN.format(reason="x") == "this instance cannot run Python assets: x"


def test_fix_names_the_next_step_for_each_missing_requirement() -> None:
    assert "LAKANA_RUN__SERVER_URL=http://server:8000" in PythonSupport().fix()
    assert "install uv" in PythonSupport(server_url="http://server:8000").fix()
    assert PythonSupport(server_url="http://server:8000", uv="/usr/local/bin/uv").fix() == ""


def test_detect_reads_the_setting_and_looks_for_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(support_module.shutil, "which", lambda name: f"/opt/bin/{name}")
    found = detect(RunConfig(server_url="http://server:8000"))
    assert found == PythonSupport(server_url="http://server:8000", uv="/opt/bin/uv")
    monkeypatch.setattr(support_module.shutil, "which", lambda _name: None)
    assert detect(RunConfig()) == PythonSupport()


def test_detect_stores_an_absolute_uv_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(support_module.shutil, "which", lambda _name: "bin/uv")
    found = detect(RunConfig(server_url="http://server:8000"))
    assert found.uv == str(tmp_path / "bin" / "uv")


def test_worker_cache_dir(tmp_path: Path) -> None:
    assert worker_cache_dir({"XDG_CACHE_HOME": str(tmp_path)}) == tmp_path / "lakana-worker"
    assert worker_cache_dir({"HOME": "/home/lakana"}) == Path("/home/lakana/.cache/lakana-worker")
    assert worker_cache_dir({"XDG_CACHE_HOME": "relative", "HOME": "/h"}) == Path(
        "/h/.cache/lakana-worker"
    )
    assert worker_cache_dir().name == "lakana-worker"
    assert worker_cache_dir({}).parts[-2:] == (".cache", "lakana-worker")


def test_a_docker_worker_needs_no_uv_of_its_own() -> None:
    """The task image carries uv; only the local runner needs one on the worker's PATH."""
    assert PythonSupport(server_url="http://server:8000", uv=None, needs_uv=False).reason() is None
    assert PythonSupport(server_url="http://server:8000", uv=None, needs_uv=False).fix() == ""
    assert (
        PythonSupport(server_url="http://server:8000", uv=None).reason()
        == "uv is not on the worker's PATH"
    )
    assert detect(RunConfig(server_url="http://server:8000", runner="docker")).needs_uv is False
    assert detect(RunConfig(server_url="http://server:8000")).needs_uv is True
    assert PythonSupport(needs_uv=False).reason() == "LAKANA_RUN__SERVER_URL is not set"
