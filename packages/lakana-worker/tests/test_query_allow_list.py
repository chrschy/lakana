"""Allow-list mode, end to end: what a real query process opens, over HTTPS and in local mode.

The HTTPS cases run against `tls_fixture` servers in the test process (a test authority, a
leaf for ``127.0.0.1``), which exercise exactly what broke on DuckDB 2.0 (httpfs loading the
CA bundle under the restriction) and can play what a store cannot: a second host, a redirect.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tls_fixture import Authority, Serving, make_authority, serving

from lakana_core.errors import ValidationError
from lakana_worker.executor import DuckDBInternalError, execute_query
from lakana_worker.query_child import OUTSIDE_PREFIXES, REFUSED_LOCATION


def _parquet(path: Path, value: int) -> None:
    """Write a one-row Parquet file with column ``a``.

    Parameters
    ----------
    path
        The file; its directory is created.
    value
        The cell.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"a": [value]}), path)


@pytest.fixture
def authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Authority:
    """Make a test authority and give its certificate to every query process as the bundle.

    Parameters
    ----------
    tmp_path
        Holds the certificate files.
    monkeypatch
        Replaces the worker's resolved bundle for the test.

    Returns
    -------
    Authority
        The authority whose certificate the query process trusts.
    """
    (tmp_path / "certs").mkdir()
    made = make_authority(tmp_path / "certs")
    monkeypatch.setattr("lakana_worker.executor.query_ca_bundle", lambda: str(made.ca_file))
    return made


@pytest.fixture
def store(tmp_path: Path, authority: Authority) -> Iterator[Serving]:
    """Serve a store over HTTPS: ``t1/a.parquet`` (1), ``t2/a.parquet`` (2), ``t10/a.parquet`` (10).

    Parameters
    ----------
    tmp_path
        Holds the served files.
    authority
        Signs the server's certificate.

    Yields
    ------
    Serving
        The running server.
    """
    for table, value in (("t1", 1), ("t2", 2), ("t10", 10)):
        _parquet(tmp_path / "store" / table / "a.parquet", value)
    with serving(tmp_path / "store", authority, tmp_path / "certs") as server:
        yield server


@pytest.fixture
def other(tmp_path: Path, authority: Authority) -> Iterator[Serving]:
    """Serve a second host over HTTPS: ``t9/a.parquet`` (9), never one of the query's tables.

    Parameters
    ----------
    tmp_path
        Holds the served files.
    authority
        Signs the server's certificate (the same authority: only the allow-list refuses it).

    Yields
    ------
    Serving
        The running server.
    """
    _parquet(tmp_path / "other" / "t9" / "a.parquet", 9)
    with serving(tmp_path / "other", authority, tmp_path / "certs") as server:
        yield server


def _read(url: str, prefixes: tuple[str, ...]) -> list[int]:
    """Read one Parquet file by its literal URL, as SQL that got past the guard would.

    Parameters
    ----------
    url
        The file.
    prefixes
        The locations the query process may open.

    Returns
    -------
    list of int
        Column ``a``.
    """
    result = execute_query(f"SELECT a FROM read_parquet('{url}')", read_prefixes=prefixes)
    return result.column("a").to_pylist()


def test_an_https_read_under_its_prefix_returns_rows(store: Serving) -> None:
    """Red on DuckDB 2.0 before allow-list mode: ``Cannot read a CA certificate bundle``."""
    assert _read(store.url("t1/a.parquet"), (store.url("t1/"),)) == [1]


def test_an_https_read_through_a_variable_returns_rows(store: Serving) -> None:
    """The way every production read goes: the file list is a variable, checked by the child."""
    result = execute_query(
        "SELECT a FROM read_parquet(getvariable('v_0_files'))",
        {"v_0_files": [store.url("t1/a.parquet") + "?X-Amz-Signature=abc"]},
        read_prefixes=(store.url("t1/"),),
    )
    assert result.column("a").to_pylist() == [1]


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("sibling", id="sibling t10 against t1/"),
        pytest.param("other", id="a second server"),
        pytest.param("http", id="http against https of the same host"),
        pytest.param("http://169.254.169.254/latest/meta-data/", id="cloud metadata"),
        pytest.param("s3://bucket/t1/a.parquet", id="s3 scheme"),
        pytest.param("https://127.0.0.1:9/t1/a.parquet", id="another port"),
    ],
)
def test_an_address_outside_the_prefixes_is_refused_without_naming_it(
    store: Serving, other: Serving, target: str
) -> None:
    """Red on DuckDB 1.5 before allow-list mode: each of these was read (or tried)."""
    url = {
        "sibling": store.url("t10/a.parquet"),
        "other": other.url("t9/a.parquet"),
        "http": store.url("t1/a.parquet").replace("https://", "http://"),
    }.get(target, target)
    with pytest.raises(ValidationError) as caught:
        _read(url, (store.url("t1/"),))
    assert type(caught.value) is ValidationError
    assert str(caught.value) == REFUSED_LOCATION
    assert other.seen == []
    assert not [line for line in store.seen if "t10" in line]


def test_a_variable_that_leaves_its_prefix_is_refused_before_anything_runs(
    store: Serving,
) -> None:
    with pytest.raises(DuckDBInternalError) as caught:
        execute_query(
            "SELECT a FROM read_parquet(getvariable('v_0_files'))",
            {"v_0_files": [store.url("t1/../t2/a.parquet")]},
            read_prefixes=(store.url("t1/"),),
        )
    assert str(caught.value).startswith(OUTSIDE_PREFIXES)
    assert store.seen == []


def test_known_residual_a_literal_dotdot_reaches_the_same_server(store: Serving) -> None:
    """Known residual (#14): DuckDB compares the URL string; the request leaves the prefix.

    Production file lists are variables, which the child checks; only SQL that got past the
    guard can write a literal. What it reaches is what the same host serves to a request
    without a valid signature. Pinned so that a DuckDB change shows.
    """
    assert _read(store.url("t1/../t2/a.parquet"), (store.url("t1/"),)) == [2]


def test_known_residual_a_redirect_from_the_store_is_followed_to_another_host(
    tmp_path: Path, authority: Authority, other: Serving
) -> None:
    """Known residual (#14): httpfs follows a redirect without checking the allow-list.

    Observed on DuckDB 1.5.5 and 2.0.0. The reach is what the store itself redirects to;
    the query cannot choose the target. Pinned so that a DuckDB change shows.
    """
    _parquet(tmp_path / "redirecting" / "t1" / "a.parquet", 1)
    redirects = {"/t1/r.parquet": other.url("t9/a.parquet")}
    with serving(tmp_path / "redirecting", authority, tmp_path / "certs", redirects) as redirecting:
        assert _read(redirecting.url("t1/r.parquet"), (redirecting.url("t1/"),)) == [9]
    assert other.seen and all(line.endswith("/t9/a.parquet") for line in other.seen)


@pytest.fixture
def local_store(tmp_path: Path) -> Path:
    """Lay out a local store: two tables, a secret beside them, a symlink out of ``t1``.

    Parameters
    ----------
    tmp_path
        The test's directory.

    Returns
    -------
    Path
        The store's directory (``t1``, ``t2``, ``t10``; ``../secret/s.parquet`` beside it).
    """
    store = tmp_path / "store"
    _parquet(store / "t1" / "a.parquet", 1)
    _parquet(store / "t2" / "a.parquet", 2)
    _parquet(store / "t10" / "a.parquet", 10)
    _parquet(tmp_path / "secret" / "s.parquet", 42)
    os.symlink(tmp_path / "secret" / "s.parquet", store / "t1" / "link.parquet")
    os.symlink(tmp_path / "secret", store / "t1" / "linked-dir")
    return store


def test_local_mode_reads_a_tables_own_files(local_store: Path) -> None:
    assert _read(f"{local_store}/t1/a.parquet", (f"{local_store}/t1/",)) == [1]
    assert _read(f"{local_store}/t1/a.parquet", (f"{local_store}/t1/", f"{local_store}/t2/")) == [1]


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param("../secret/s.parquet", id="a file outside the tables"),
        pytest.param("t1/../../secret/s.parquet", id="dotdot"),
        pytest.param("t1/link.parquet", id="a symlink out of a table directory"),
        pytest.param("t1/linked-dir/s.parquet", id="a symlinked directory out of a table"),
        pytest.param("t10/a.parquet", id="sibling t10 against t1/"),
        pytest.param("t2/a.parquet", id="another table"),
        pytest.param("*/a.parquet", id="a glob above the table"),
    ],
)
def test_local_mode_refuses_what_lies_outside_the_tables(local_store: Path, relative: str) -> None:
    """Red on DuckDB 1.5 before allow-list mode: local mode read anything the worker could."""
    with pytest.raises(ValidationError) as caught:
        _read(f"{local_store}/{relative}", (f"{local_store}/t1/",))
    assert str(caught.value) == REFUSED_LOCATION
    assert "secret" not in str(caught.value)


def test_local_mode_refuses_a_glob_that_meets_a_symlink_out(local_store: Path) -> None:
    """A glob inside the table directory names the symlink DuckDB refuses; the text does not."""
    with pytest.raises(ValidationError) as caught:
        _read(f"{local_store}/t1/*.parquet", (f"{local_store}/t1/",))
    assert str(caught.value) == REFUSED_LOCATION
