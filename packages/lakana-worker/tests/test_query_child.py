"""Tests for the query child: in this process for every branch, and as a real process."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest

from lakana_worker import query_child
from lakana_worker.query_protocol import (
    CHECK,
    DESCRIBE,
    END_OF_STREAM,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_REFUSED,
    MAX_REQUEST_BYTES,
    MEMORY_LIMIT_ADVICE,
    NO_RESULT_MESSAGE,
    READ,
    WRITE,
    ErrorDocument,
    Request,
    decode_error,
    encode_request,
)

MODULE = "lakana_worker.query_child"
CHILD_ARGV = [sys.executable, "-I", "-B", "-m", MODULE]
EXTENSIONS = str(Path.home() / ".duckdb" / "extensions")
SLOW_STREAM = "SELECT a.range FROM range(60000) a, range(60000) b WHERE a.range + b.range < 0"
"""A statement that streams and produces no row: the reader waits inside DuckDB for its first
batch, at a flat 90 MB, for about 9 s at two threads. Then it ends by itself, which is the
bound of every test that uses it should the stop under test fail."""
WIDE_ROWS = "SELECT i, repeat('x', 50) AS s FROM range(3000000) AS t(i)"
"""About 200 MB of stream that the child never holds in memory; no test reads it to the end."""
OVER_THE_LIMIT = (
    "SELECT max(rn) AS n FROM (SELECT row_number() OVER (ORDER BY b DESC) AS rn FROM "
    "(SELECT i, repeat('x', 100) || i::VARCHAR AS b FROM range(200000) AS r(i)))"
)
"""A sort of about 22 MB of rows, which cannot be held at a 32MB limit and must spill."""


def _request(sql: str = "SELECT 42 AS answer", **overrides: object) -> Request:
    """Build a request for the real child.

    Parameters
    ----------
    sql
        The statement.
    overrides
        Fields to replace.

    Returns
    -------
    Request
        The request.
    """
    fields: dict[str, object] = {
        "mode": READ,
        "sql": sql,
        "parameters": {},
        "restrict_local_filesystem": True,
        "memory_limit": "200MB",
        "threads": 2,
        "temp_directory": "/nonexistent/lakana-test-spill",
        "max_temp_directory_size": "1GB",
        "batch_rows": 2048,
        "extension_directory": EXTENSIONS,
        "parent_pid": os.getpid(),
    }
    fields.update(overrides)
    return Request(**fields)


def _main(
    request: Request | bytes, asked: threading.Event | None = None, parent: int | None = None
) -> tuple[int, bytes, str]:
    """Run `query_child.main` in this process; return the status, the stream and stderr.

    Parameters
    ----------
    request
        The request, or raw bytes for standard input.
    asked
        The ask flag; a fresh one by default.
    parent
        The parent process id to report; this process by default.

    Returns
    -------
    tuple
        The exit status, the stream and standard error.
    """
    raw = request if isinstance(request, bytes) else encode_request(request)
    stream, stderr = io.BytesIO(), io.StringIO()
    code = query_child.main(
        io.BytesIO(raw),
        stream,
        stderr,
        asked or threading.Event(),
        parent=lambda: os.getpid() if parent is None else parent,
    )
    return code, stream.getvalue(), stderr.getvalue()


def _table(stream: bytes) -> pa.Table:
    """Read a complete Arrow IPC stream.

    Parameters
    ----------
    stream
        The bytes of the stream.

    Returns
    -------
    pa.Table
        The table.
    """
    assert stream.endswith(END_OF_STREAM)
    return pa.ipc.open_stream(stream).read_all()


def _failed(request: Request, asked: threading.Event | None = None) -> ErrorDocument:
    """Run a request that must fail and return its error document.

    Parameters
    ----------
    request
        The request.
    asked
        The ask flag.

    Returns
    -------
    ErrorDocument
        The document on standard error.
    """
    code, _stream, stderr = _main(request, asked)
    assert code == EXIT_FAILED
    error = decode_error(stderr.encode())
    assert error is not None
    return error


# --- in this process -------------------------------------------------------------------------


def test_a_read_writes_a_complete_stream_and_exits_0() -> None:
    code, stream, stderr = _main(_request())
    assert (code, stderr) == (EXIT_OK, "")
    assert _table(stream).to_pydict() == {"answer": [42]}


def test_the_statement_reads_every_value_as_a_variable() -> None:
    """Each value the platform sends arrives as a DuckDB variable of its own type."""
    moment = dt.datetime(2026, 10, 8, 12, 30, tzinfo=dt.UTC)
    parameters = {
        "urls": ["a", "b"],
        "n": 7,
        "d": Decimal("1.50"),
        "day": dt.date(2026, 10, 8),
        "naive": dt.datetime(2026, 10, 8, 12, 30),
        "aware": moment,
    }
    sql = (
        "SELECT len(getvariable('urls')) AS urls, getvariable('n') + 1 AS n, "
        "getvariable('d') * 2 AS d, getvariable('day') + 1 AS day, "
        "getvariable('naive') < getvariable('naive') + INTERVAL 1 HOUR AS naive, "
        "epoch(getvariable('aware')) AS aware"
    )
    code, stream, _stderr = _main(_request(sql, parameters=parameters, mode=WRITE))
    assert code == EXIT_OK
    assert _table(stream).to_pylist() == [
        {
            "urls": 2,
            "n": 8,
            "d": Decimal("3.00"),
            "day": dt.date(2026, 10, 9),
            "naive": True,
            "aware": moment.timestamp(),
        }
    ]


def test_a_variable_read_streams_in_more_than_one_batch(
    tmp_path: Path,
) -> None:
    """A million rows read through a files variable arrive as a stream of several batches."""
    path = tmp_path / "rows.parquet"
    duckdb.sql(
        "COPY (SELECT i, repeat('x', 50) || i::VARCHAR AS s FROM range(1000000) AS t(i)) "
        f"TO '{path}' (FORMAT parquet)"
    )
    request = _request(
        "SELECT * FROM read_parquet(getvariable('v_0_files'))",
        parameters={"v_0_files": [str(path)]},
        restrict_local_filesystem=False,
        memory_limit="32MB",
        threads=1,
    )

    code, stream, stderr = _main(request)

    assert (code, stderr) == (EXIT_OK, "")
    batches = list(pa.ipc.open_stream(stream))
    assert len(batches) > 1
    assert sum(batch.num_rows for batch in batches) == 1_000_000


def test_the_statement_runs_without_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    """`sql` is called with the statement alone: given parameters it holds the whole result.

    DuckDB does not count a materialised result against `memory_limit`, so no memory limit can
    show that regression; the call itself is what is checked.
    """
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    real_connect = duckdb.connect

    class Spy:
        """A connection that records how `sql` is called."""

        def __init__(self) -> None:
            self.inner = real_connect(":memory:")

        def sql(self, *args: object, **kwargs: object) -> object:
            calls.append((args, kwargs))
            return self.inner.sql(*args, **kwargs)  # ty: ignore[invalid-argument-type]

        def __getattr__(self, name: str) -> object:
            return getattr(self.inner, name)

    monkeypatch.setattr(query_child.duckdb, "connect", lambda *_a, **_k: Spy())
    request = _request(
        "SELECT len(getvariable('v_0_files')) AS n", parameters={"v_0_files": ["a", "b"]}
    )

    code, stream, stderr = _main(request)

    assert (code, stderr) == (EXIT_OK, "")
    assert _table(stream).to_pylist() == [{"n": 2}]
    assert calls == [((request.sql,), {})]


def test_a_failing_statement_never_echoes_a_value_it_was_given(tmp_path: Path) -> None:
    """The URL is a variable's value, never SQL text, so DuckDB's error cannot quote it."""
    path = tmp_path / "X-Amz-Signature=SECRET123.parquet"
    duckdb.sql(f"COPY (SELECT 1 AS id) TO '{path}' (FORMAT parquet)")
    request = _request(
        "SELECT nope FROM read_parquet(getvariable('v_0_files'))",
        parameters={"v_0_files": [str(path)]},
        restrict_local_filesystem=False,
    )

    code, _stream, stderr = _main(request)

    error = decode_error(stderr.encode())
    assert code == EXIT_FAILED
    assert error is not None
    assert 'Referenced column "nope" not found' in error.message
    assert "SECRET123" not in stderr


def test_a_variable_name_that_is_not_an_identifier_is_never_written_into_sql() -> None:
    """Names are written into `SET VARIABLE`, where DuckDB takes no bound name."""
    connection = duckdb.connect(":memory:")
    with pytest.raises(ValueError, match="not an identifier"):
        query_child.set_variables(connection, {'x" = 1; SELECT 1; --': ["a"]})
    assert connection.execute("SELECT count(*) FROM duckdb_variables()").fetchone() == (0,)


def test_a_write_and_a_describe_run_in_utc_and_a_read_in_the_session_zone() -> None:
    sql = "SELECT TIMESTAMPTZ '2026-10-08 12:00:00+02' AS t"
    session_zone = duckdb.sql("SELECT current_setting('TimeZone')").fetchone()
    assert session_zone is not None
    read = _table(_main(_request(sql))[1])
    write = _table(_main(_request(sql, mode=WRITE))[1])
    describe = _table(_main(_request(f"SELECT * FROM ({sql}) LIMIT 0", mode=DESCRIBE))[1])
    assert read.schema.field("t").type == pa.timestamp("us", tz=session_zone[0])
    assert write.schema.field("t").type == pa.timestamp("us", tz="UTC")
    assert describe.schema.field("t").type == pa.timestamp("us", tz="UTC")
    assert describe.num_rows == 0


def test_the_connection_holds_no_credential_and_loads_nothing_by_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAAMBIENT")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "ambient-token")
    connection = duckdb.connect(":memory:")
    try:
        query_child.configure(connection, _request(restrict_local_filesystem=False))
        settings = dict(
            connection.execute(
                "SELECT name, value FROM duckdb_settings() WHERE name IN ('s3_access_key_id', "
                "'s3_secret_access_key', 's3_session_token', 'allow_persistent_secrets', "
                "'autoinstall_known_extensions', 'autoload_known_extensions', "
                "'allow_community_extensions', 'extension_directory', 'threads', "
                "'disabled_filesystems', 'temp_directory', 'max_temp_directory_size', "
                "'memory_limit')"
            ).fetchall()
        )
        loaded = connection.execute(
            "SELECT install_path FROM duckdb_extensions() WHERE extension_name = 'httpfs' "
            "AND loaded"
        ).fetchone()
    finally:
        connection.close()
    assert settings == {
        "s3_access_key_id": "",
        "s3_secret_access_key": "",
        "s3_session_token": "",
        "allow_persistent_secrets": "false",
        "autoinstall_known_extensions": "false",
        "autoload_known_extensions": "false",
        "allow_community_extensions": "false",
        "extension_directory": EXTENSIONS,
        "threads": "2",
        "disabled_filesystems": "",
        "temp_directory": "/nonexistent/lakana-test-spill",
        "max_temp_directory_size": "953.6 MiB",
        "memory_limit": "190.7 MiB",
    }
    assert loaded is not None and loaded[0].startswith(EXTENSIONS)


def test_a_check_accepts_good_sizes_with_an_empty_stream() -> None:
    code, stream, stderr = _main(_request("", mode=CHECK, extension_directory="/nowhere"))
    assert (code, stderr) == (EXIT_OK, "")
    assert _table(stream).num_columns == 0


@pytest.mark.parametrize("field", ["memory_limit", "max_temp_directory_size"])
def test_a_check_names_the_size_duckdb_cannot_read(field: str) -> None:
    error = _failed(_request("", mode=CHECK, **{field: "lots"}))
    assert error == ErrorDocument("invalid", field)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\xff not json", "query request: not a JSON document\n"),
        (b"[]", "query request: not a JSON object\n"),
    ],
)
def test_an_unreadable_request_is_refused_before_anything_runs(raw: bytes, message: str) -> None:
    assert _main(raw) == (EXIT_REFUSED, b"", message)


def test_a_child_whose_parent_is_not_the_worker_refuses_to_run() -> None:
    code, stream, stderr = _main(_request(), parent=1)
    assert (code, stream) == (EXIT_REFUSED, b"")
    assert stderr == "the worker that started this query is gone\n"


def test_a_syntax_error_is_about_the_query_and_never_echoes_the_sql() -> None:
    error = _failed(_request("SELEC secret_expression"))
    assert error.kind == "invalid"
    assert error.message.startswith("Query failed: Parser Error: ")
    assert "LINE" not in error.message
    assert "secret_expression" not in error.message


@pytest.mark.parametrize("sql", ["", "-- nothing"])
def test_sql_without_a_result_is_about_the_query(sql: str) -> None:
    assert _failed(_request(sql)) == ErrorDocument("invalid", NO_RESULT_MESSAGE)


def test_a_local_read_is_refused_as_an_error_about_the_query() -> None:
    error = _failed(_request("SELECT * FROM read_csv('/etc/hostname')"))
    assert error.kind == "invalid"
    assert "LocalFileSystem has been disabled" in error.message


def test_a_refused_spill_is_out_of_memory_with_the_setting_to_raise() -> None:
    error = _failed(_request(OVER_THE_LIMIT, memory_limit="32MB", threads=1))
    assert error == ErrorDocument(
        "out_of_memory",
        "Query failed: the query ran out of memory and cannot spill to disk. "
        f"{MEMORY_LIMIT_ADVICE}",
    )


def test_a_describe_never_plans_a_second_time() -> None:
    error = _failed(_request(OVER_THE_LIMIT, mode=DESCRIBE, memory_limit="32MB", threads=1))
    assert error.kind == "invalid"
    assert "LocalFileSystem has been disabled" in error.message


def test_a_storage_failure_is_transient() -> None:
    error = _failed(_request("SELECT * FROM read_parquet('http://127.0.0.1:9/none.parquet')"))
    assert error.kind == "transient"
    assert error.message.startswith("Query failed: ")


def test_an_ask_that_arrives_before_the_statement_stops_it_from_starting() -> None:
    asked = threading.Event()
    asked.set()
    assert _failed(_request(SLOW_STREAM), asked).kind == "interrupted"


def test_an_ask_seen_after_configuring_ends_the_child_before_the_statement() -> None:
    """Without the main thread's interrupts, the check after configuring is what stops it.

    Through `main`, an ask that is already set also interrupts the connection, and that
    interrupt may land inside `configure`: which of the two paths ends the child then depends
    on timing, and so did the coverage of this one.
    """
    asked = threading.Event()
    asked.set()
    sink = io.BytesIO()
    error = query_child.serve(_request(SLOW_STREAM), sink, query_child.Session(), asked)
    assert error is not None
    assert error.kind == "interrupted"
    assert sink.getvalue() == b""


def test_an_ask_interrupts_a_statement_that_waits_inside_duckdb() -> None:
    asked = threading.Event()
    timer = threading.Timer(0.5, asked.set)
    timer.start()
    started = time.monotonic()
    try:
        error = _failed(_request(SLOW_STREAM), asked)
    finally:
        timer.cancel()
    assert error == ErrorDocument("interrupted", "The query stopped because it was asked to.")
    assert time.monotonic() - started < 5


class _BrokenSink(io.BytesIO):
    """A sink whose write fails from the n-th call on, as a pipe nobody reads does."""

    def __init__(self, good_writes: int, fail_flush: bool = False) -> None:
        super().__init__()
        self.left = good_writes
        self._fail_flush = fail_flush

    def write(self, data: object) -> int:
        """Fail once the allowed writes are used up.

        Parameters
        ----------
        data
            The bytes.

        Returns
        -------
        int
            The number of bytes written.

        Raises
        ------
        BrokenPipeError
            Once the allowed writes are used up.
        """
        if self.left <= 0:
            raise BrokenPipeError(32, "Broken pipe")
        self.left -= 1
        return super().write(data)  # ty: ignore[invalid-argument-type]

    def flush(self) -> None:
        """Fail if asked to.

        Raises
        ------
        BrokenPipeError
            When the sink was built to fail its flush.
        """
        if self._fail_flush:
            raise BrokenPipeError(32, "Broken pipe")


def _writes_of(sql: str) -> int:
    """Count the writes pyarrow makes to its sink for a statement that succeeds.

    Parameters
    ----------
    sql
        The statement.

    Returns
    -------
    int
        The number of `write` calls up to and including the end marker.
    """
    sink = _BrokenSink(10**9)
    assert query_child.serve(_request(sql), sink, query_child.Session(), threading.Event()) is None
    return 10**9 - sink.left


@pytest.mark.parametrize("sql", ["SELECT 1 AS i", "SELECT i FROM range(5000) AS t(i)"])
def test_a_reader_that_went_away_is_never_reported_as_a_storage_failure(sql: str) -> None:
    """A broken pipe is an `OSError`, like DuckDB's I/O errors; it must not look like one.

    The sink fails at every write there is, however many pyarrow makes for the statement.
    """
    total = _writes_of(sql)
    assert total >= 2
    for good_writes in range(total):
        error = query_child.serve(
            _request(sql, batch_rows=1000),
            _BrokenSink(good_writes),
            query_child.Session(),
            threading.Event(),
        )
        assert error == ErrorDocument("interrupted", "The worker stopped reading the result.")


def test_a_reader_that_went_away_before_the_flush_is_not_a_storage_failure() -> None:
    sink = _BrokenSink(10**9, fail_flush=True)
    request = _request("SELECT 1 AS i")
    error = query_child.serve(request, sink, query_child.Session(), threading.Event())
    assert error == ErrorDocument("interrupted", "The worker stopped reading the result.")


class _AskingSink(io.BytesIO):
    """A sink that sets an event once enough bytes were written, as an ask mid-stream does."""

    def __init__(self, asked: threading.Event, after_bytes: int) -> None:
        super().__init__()
        self._asked = asked
        self._after = after_bytes

    def write(self, data: object) -> int:
        """Write, then set the event once `after_bytes` bytes are in.

        Parameters
        ----------
        data
            The bytes.

        Returns
        -------
        int
            The number of bytes written.
        """
        written = super().write(data)  # ty: ignore[invalid-argument-type]
        if self.tell() >= self._after:
            self._asked.set()
        return written


def test_an_ask_after_the_first_batch_ends_the_stream_without_its_end_marker() -> None:
    asked = threading.Event()
    sink = _AskingSink(asked, after_bytes=8000)
    request = _request("SELECT i FROM range(10000) AS t(i)", batch_rows=1000)
    error = query_child.serve(request, sink, query_child.Session(), asked)
    assert error == ErrorDocument("interrupted", "The query stopped because it was asked to.")
    assert 8000 <= len(sink.getvalue()) < 40_000  # a batch is 8,000 bytes; the whole is 80,000
    assert not sink.getvalue().endswith(END_OF_STREAM)


def test_an_error_while_a_later_batch_is_fetched_is_classified_like_any_other() -> None:
    """Past the first batch DuckDB's error arrives as a plain `OSError` (see `_is_transient`).

    One thread: with several, DuckDB sometimes reports a sibling's interrupt instead.
    """
    sql = "SELECT CASE WHEN x > 150000 THEN error('boom') ELSE x END FROM range(200000) t(x)"
    code, stream, stderr = _main(_request(sql, threads=1))
    error = decode_error(stderr.encode())
    assert code == EXIT_FAILED
    assert error is not None and error.kind == "invalid" and "boom" in error.message
    assert not stream.endswith(END_OF_STREAM)


_REFUSAL = "Permission Error: File system LocalFileSystem has been disabled by configuration"


class _Planner:
    """A connection stand-in whose `EXPLAIN` plans, or fails as a local read does."""

    def __init__(self, *, plans: bool, during: Callable[[], None] | None = None) -> None:
        self._plans = plans
        self._during = during
        self._real = duckdb.connect(":memory:")
        self.planned: list[str] = []

    def extract_statements(self, sql: str) -> list[duckdb.Statement]:
        """Split `sql` as DuckDB does.

        Parameters
        ----------
        sql
            The text.

        Returns
        -------
        list
            The statements.
        """
        return self._real.extract_statements(sql)

    def execute(self, sql: str, parameters: object = None) -> None:
        """Plan `sql`, or refuse it as DuckDB refuses a local read.

        Parameters
        ----------
        sql
            The `EXPLAIN` statement.
        parameters
            Ignored.

        Raises
        ------
        duckdb.PermissionException
            If the stand-in does not plan.
        """
        assert sql.startswith("EXPLAIN ")
        self.planned.append(sql)
        if self._during is not None:
            self._during()
        if not self._plans:
            raise duckdb.PermissionException(_REFUSAL)


def _kind(
    exc: BaseException,
    statement: str | None = None,
    planner: _Planner | None = None,
    asked: threading.Event | None = None,
) -> str:
    """Classify `exc` as the child does and return the kind.

    Parameters
    ----------
    exc
        The failure.
    statement
        The statement that failed.
    planner
        The connection stand-in.
    asked
        The ask flag.

    Returns
    -------
    str
        The kind.
    """
    connection = planner or _Planner(plans=True)
    return query_child.failure(
        connection,  # ty: ignore[invalid-argument-type]
        exc,
        statement,
        asked or threading.Event(),
    ).kind


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (OSError("IO Error: connection reset"), "transient"),
        (OSError("HTTP Error: HTTP GET error on 'x' (HTTP 503)"), "transient"),
        (OSError("connection reset by peer"), "transient"),
        (duckdb.IOException("IO Error: no such file"), "transient"),
        (duckdb.OutOfMemoryException("Out of Memory Error: failed to allocate"), "out_of_memory"),
        (OSError("Out of Memory Error: failed to allocate"), "out_of_memory"),
        (duckdb.InternalException("INTERNAL Error: assertion"), "internal"),
        (duckdb.FatalException("FATAL Error: database invalidated"), "internal"),
        (OSError("INTERNAL Error: assertion"), "internal"),
        (OSError("Invalid Input Error: x"), "invalid"),
        (OSError("Conversion Error: bad"), "invalid"),
        (OSError("Out of Range Error: bad"), "invalid"),
        (OSError("Not implemented Error: bad"), "invalid"),
        (duckdb.BinderException("Binder Error: no column"), "invalid"),
        (duckdb.CatalogException("Catalog Error: no table"), "invalid"),
        (duckdb.ParserException("Parser Error: syntax"), "invalid"),
        (duckdb.ConversionException("Conversion Error: bad"), "invalid"),
        (duckdb.ConstraintException("Constraint Error: dup"), "invalid"),
        (duckdb.InvalidInputException("Invalid Input Error: x"), "invalid"),
    ],
)
def test_the_kind_follows_the_duckdb_error_type(exc: BaseException, kind: str) -> None:
    assert _kind(exc) == kind


def test_an_out_of_memory_message_names_the_setting_and_drops_duckdbs_own_advice() -> None:
    exc = duckdb.OutOfMemoryException(
        "Out of Memory Error: failed to allocate data of size 256.0 MiB (1/2 used)\n\n"
        "Possible solutions:\n* Reducing the number of threads (SET threads=X)"
    )
    error = query_child.failure(
        _Planner(plans=True),  # ty: ignore[invalid-argument-type]
        exc,
        None,
        threading.Event(),
    )
    assert error.message == (
        "Query failed: Out of Memory Error: failed to allocate data of size 256.0 MiB "
        f"(1/2 used) {MEMORY_LIMIT_ADVICE}"
    )


@pytest.mark.parametrize(
    ("exc", "plans", "statement", "kind"),
    [
        (duckdb.PermissionException(_REFUSAL), True, "SELECT 1", "out_of_memory"),
        (OSError(_REFUSAL), True, "SELECT 1", "out_of_memory"),
        (duckdb.PermissionException(_REFUSAL), False, "SELECT 1", "invalid"),
        (OSError(_REFUSAL), False, "SELECT 1", "invalid"),
        (duckdb.PermissionException(_REFUSAL), True, None, "invalid"),
        (duckdb.PermissionException("Permission Error: no"), True, "SELECT 1", "invalid"),
        (OSError("IO Error: " + _REFUSAL), True, "SELECT 1", "transient"),
    ],
)
def test_a_refusal_is_a_spill_only_if_the_statement_plans(
    exc: BaseException, plans: bool, statement: str | None, kind: str
) -> None:
    assert _kind(exc, statement, _Planner(plans=plans)) == kind


@pytest.mark.parametrize("raises", [True, False])
def test_an_ask_during_the_second_planning_stays_visible(raises: bool) -> None:
    asked = threading.Event()

    def during() -> None:
        asked.set()
        if raises:
            raise duckdb.InterruptException("Interrupt Error: Interrupted!")

    planner = _Planner(plans=True, during=during)
    kind = _kind(duckdb.PermissionException(_REFUSAL), "SELECT 1", planner, asked)
    assert kind == "interrupted"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "SELECT 1; INSERT INTO c VALUES (2)",
        "SET threads = 1",
        "LOAD 'somewhere/extension'",
        "INSERT INTO c VALUES (2)",
        "SELEC 1",
        "-- nothing",
    ],
)
def test_only_one_select_is_planned_again(sql: str) -> None:
    """`EXPLAIN a; b` runs `b`: user SQL must never execute a second time."""
    planner = _Planner(plans=True)
    assert _kind(duckdb.PermissionException(_REFUSAL), sql, planner) == "invalid"
    assert planner.planned == []


def test_one_select_is_planned_again_as_the_statement_duckdb_extracted() -> None:
    planner = _Planner(plans=True)
    statement = "SELECT 1; -- trailing"
    assert _kind(duckdb.PermissionException(_REFUSAL), statement, planner) == "out_of_memory"
    assert planner.planned == ["EXPLAIN SELECT 1; -- trailing"]


def test_an_unexpected_exception_becomes_a_document_without_its_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def explode(connection: object, request: object) -> None:
        raise RuntimeError("s3://bucket/" + "secret" + "-path")

    monkeypatch.setattr(query_child, "configure", explode)
    error = _failed(_request())
    assert error == ErrorDocument(
        "internal", "Query failed: the query process hit an unexpected error (RuntimeError)."
    )
    printed = capsys.readouterr().err
    assert "in explode" in printed and printed.rstrip().endswith("RuntimeError")
    assert "secret-path" not in printed


def test_a_connection_that_fails_to_close_does_not_stop_serve_from_answering() -> None:
    class Stubborn(query_child.Session):
        def close(self) -> None:
            """Fail as a broken connection might.

            Raises
            ------
            RuntimeError
                Always.
            """
            raise RuntimeError("cannot close")

    sink = io.BytesIO()
    assert query_child.serve(_request(), sink, Stubborn(), threading.Event()) is None
    assert _table(sink.getvalue()).to_pydict() == {"answer": [42]}


def test_a_request_over_the_limit_is_refused_and_never_read_beyond_it() -> None:
    reads: list[int] = []

    class Stdin(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            """Record the size asked for.

            Parameters
            ----------
            size
                How many bytes.

            Returns
            -------
            bytes
                More than the limit.
            """
            reads.append(-1 if size is None else size)
            return b" " * (MAX_REQUEST_BYTES + 1)

    stream, stderr = io.BytesIO(), io.StringIO()
    code = query_child.main(Stdin(), stream, stderr, threading.Event(), parent=os.getpid)
    assert (code, stderr.getvalue()) == (EXIT_REFUSED, "query request: larger than 64 MiB\n")
    assert reads == [MAX_REQUEST_BYTES + 1]


def test_interrupting_a_closed_session_does_nothing() -> None:
    session = query_child.Session()
    session.interrupt()
    session.open()
    session.interrupt()
    session.close()
    session.close()
    session.interrupt()


def test_die_with_parent_asks_the_kernel_for_sigkill(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    class Libc:
        def prctl(self, option: int, value: int) -> int:
            """Record the request.

            Parameters
            ----------
            option
                The prctl option.
            value
                Its value.

            Returns
            -------
            int
                0.
            """
            calls.append((option, value))
            return 0

    monkeypatch.setattr(query_child.sys, "platform", "linux")
    monkeypatch.setattr(query_child.ctypes, "CDLL", lambda name, use_errno: Libc())
    query_child.die_with_parent()
    assert calls == [(1, signal.SIGKILL)]


def test_die_with_parent_exits_when_the_kernel_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Libc:
        def prctl(self, option: int, value: int) -> int:
            """Refuse.

            Parameters
            ----------
            option
                The prctl option.
            value
                Its value.

            Returns
            -------
            int
                -1.
            """
            return -1

    monkeypatch.setattr(query_child.sys, "platform", "linux")
    monkeypatch.setattr(query_child.ctypes, "CDLL", lambda name, use_errno: Libc())
    with pytest.raises(SystemExit) as caught:
        query_child.die_with_parent()
    assert caught.value.code == EXIT_REFUSED
    assert "could not ask the kernel" in capsys.readouterr().err


def test_die_with_parent_does_nothing_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(query_child.sys, "platform", "darwin")
    monkeypatch.setattr(query_child.ctypes, "CDLL", lambda name, use_errno: pytest.fail("called"))
    query_child.die_with_parent()


class _Exit(Exception):
    """Stands in for `os._exit` in `run_cli` tests."""


def _run_cli(monkeypatch: pytest.MonkeyPatch, main: object) -> tuple[int, list[str]]:
    """Run `run_cli` with the process-level calls recorded instead of made.

    Parameters
    ----------
    monkeypatch
        The fixture.
    main
        Stands in for `query_child.main`.

    Returns
    -------
    tuple
        The status passed to `os._exit` and the steps taken.
    """
    steps: list[str] = []

    def leave(code: int) -> None:
        raise _Exit(code)

    monkeypatch.setattr(query_child, "die_with_parent", lambda: steps.append("pdeathsig"))
    monkeypatch.setattr(
        query_child.signal, "signal", lambda signum, handler: steps.append(f"signal {signum}")
    )
    monkeypatch.setattr(query_child.os, "dup", lambda fd: steps.append(f"dup {fd}") or 99)
    monkeypatch.setattr(query_child.os, "dup2", lambda a, b: steps.append(f"dup2 {a} {b}"))
    monkeypatch.setattr(query_child.os, "fdopen", lambda fd, mode: io.BytesIO())
    monkeypatch.setattr(query_child.os, "_exit", leave)
    monkeypatch.setattr(query_child, "main", main)
    with pytest.raises(_Exit) as caught:
        query_child.run_cli()
    return caught.value.args[0], steps


def test_run_cli_prepares_the_process_in_order_and_exits_with_mains_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, steps = _run_cli(monkeypatch, lambda stdin, stream, stderr, asked: 3)
    assert code == 3
    assert steps == ["pdeathsig", f"signal {int(signal.SIGTERM)}", "dup 1", "dup2 2 1"]


def test_run_cli_reports_a_crash_and_still_ends_the_process(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def crash(stdin: object, stream: object, stderr: object, asked: object) -> int:
        raise RuntimeError("boom")

    code, _steps = _run_cli(monkeypatch, crash)
    assert code == EXIT_REFUSED
    assert "RuntimeError: boom" in capsys.readouterr().err


# --- as a real process -----------------------------------------------------------------------


class _Real:
    """A real child started the way the worker starts it, always killed and reaped at the end."""

    def __init__(
        self,
        request_fields: dict[str, object],
        tmp_path: Path,
        code: str | None = None,
        zone: str | None = None,
        send: bool = True,
    ) -> None:
        self._request_fields = request_fields
        self.home, self.tmp = tmp_path / "home", tmp_path / "tmp"
        self.home.mkdir()
        self.tmp.mkdir()
        argv = CHILD_ARGV if code is None else [sys.executable, "-I", "-B", "-c", code]
        self.process = subprocess.Popen(
            argv,
            cwd=tmp_path,
            env={
                "PATH": os.environ["PATH"],
                "HOME": str(self.home),
                "TMPDIR": str(self.tmp),
                **({} if zone is None else {"TZ": zone}),
            },
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        if send:
            self.send()

    def send(self) -> None:
        """Write the request to the child and close its standard input.

        A child that cannot be given its request is killed and reaped before the error goes on,
        because the object is never entered and so never exited.
        """
        try:
            request = _request(
                temp_directory=str(self.tmp),
                **self._request_fields,  # ty: ignore[invalid-argument-type]
            )
            assert self.process.stdin is not None
            self.process.stdin.write(encode_request(request))
            self.process.stdin.close()
            # Python 3.11's communicate() flushes stdin even when it is closed, and raises.
            self.process.stdin = None
        except BaseException:
            self.__exit__()
            raise

    def read_at_least(self, size: int, timeout: float = 30.0) -> bytes:
        """Read from the child's standard output until `size` bytes are in, with a bound.

        Parameters
        ----------
        size
            How many bytes to read at least.
        timeout
            Seconds to wait at most.

        Returns
        -------
        bytes
            What was read, at least `size` bytes.
        """
        assert self.process.stdout is not None
        fd = self.process.stdout.fileno()
        data = b""
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while len(data) < size:
                left = deadline - time.monotonic()
                if left <= 0 or not selector.select(left):
                    pytest.fail(f"the child wrote {len(data)} of {size} bytes in {timeout} s")
                chunk = os.read(fd, 1 << 20)
                assert chunk, "the child closed its standard output early"
                data += chunk
        return data

    def __enter__(self) -> _Real:
        """Hand out the running child.

        Returns
        -------
        _Real
            The child.
        """
        return self

    def __exit__(self, *exc: object) -> None:
        """Kill the group and reap, whatever the test did."""
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=20)
        for pipe in (self.process.stdout, self.process.stderr):
            if pipe is not None:
                pipe.close()

    def end(self, timeout: float = 30.0) -> tuple[int, bytes, bytes]:
        """Wait for the child's end; return its status, standard output and standard error.

        Parameters
        ----------
        timeout
            Seconds to wait.

        Returns
        -------
        tuple
            The status, standard output and standard error.
        """
        out, err = self.process.communicate(timeout=timeout)
        return self.process.returncode, out, err


def test_the_real_child_answers_with_a_complete_stream(tmp_path: Path) -> None:
    sql = "SELECT i % 10 AS k, sum(i) AS s FROM range(5000) AS t(i) GROUP BY 1 ORDER BY 1"
    with _Real({"sql": sql}, tmp_path) as child:
        code, out, err = child.end()
    assert (code, err) == (EXIT_OK, b"")
    assert _table(out).num_rows == 10


def test_a_child_with_an_empty_home_loads_httpfs_from_the_workers_directory(
    tmp_path: Path,
) -> None:
    """Without `extension_directory` DuckDB looks under the empty HOME and tries to download."""
    sql = "SELECT count(*) AS n FROM duckdb_extensions() WHERE extension_name = 'httpfs' AND loaded"
    with _Real({"sql": sql, "restrict_local_filesystem": False}, tmp_path) as child:
        code, out, err = child.end()
        assert (code, err) == (EXIT_OK, b"")
        assert _table(out).to_pydict() == {"n": [1]}
        assert list(child.home.iterdir()) == []


def test_a_read_is_exported_in_the_zone_of_the_childs_tz_variable(tmp_path: Path) -> None:
    """DuckDB takes its session zone from TZ, which is why the worker passes its own on."""
    sql = "SELECT TIMESTAMPTZ '2026-10-08 12:00:00+02' AS t"
    with _Real({"sql": sql}, tmp_path, zone="Asia/Tokyo") as child:
        code, out, _err = child.end()
    assert code == EXIT_OK
    assert _table(out).schema.field("t").type == pa.timestamp("us", tz="Asia/Tokyo")


LINUX = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")


def _wait_for_file(path: Path, timeout: float = 30.0) -> None:
    """Wait, with a bound, until a file exists and holds something.

    Parameters
    ----------
    path
        The file.
    timeout
        Seconds to wait at most.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return
        time.sleep(0.01)
    pytest.fail(f"{path} was not written in {timeout} s")


def test_the_real_child_stops_within_seconds_of_sigterm_while_it_waits_for_a_batch(
    tmp_path: Path,
) -> None:
    """The probe's finding: with the statement on the main thread this never returns.

    A first statement writes a marker file, so the test knows the child is past its set-up and
    inside DuckDB; the slow statement follows it in the same text and is still running when the
    signal arrives, whether it has started or is about to (the repeated interrupt reaches it).
    """
    marker = tmp_path / "started.csv"
    sql = f"COPY (SELECT 1 AS started) TO '{marker}'; {SLOW_STREAM}"
    with _Real({"sql": sql, "restrict_local_filesystem": False}, tmp_path) as child:
        _wait_for_file(marker)
        asked = time.monotonic()
        os.killpg(child.process.pid, signal.SIGTERM)
        code, _out, err = child.end(timeout=20)
        seconds = time.monotonic() - asked
    assert code == EXIT_FAILED
    assert decode_error(err) == ErrorDocument(
        "interrupted", "The query stopped because it was asked to."
    )
    assert seconds < 5


def test_killing_the_group_mid_stream_leaves_no_process_no_end_marker_and_no_file(
    tmp_path: Path,
) -> None:
    with _Real({"sql": WIDE_ROWS}, tmp_path) as child:
        first = child.read_at_least(1024 * 1024)
        os.killpg(child.process.pid, signal.SIGKILL)
        code, rest, _err = child.end(timeout=20)
        members = subprocess.run(
            ["pgrep", "-g", str(child.process.pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        ).stdout.split()
    assert code == -signal.SIGKILL
    assert len(first) >= 1024 * 1024
    assert not (first + rest).endswith(END_OF_STREAM)
    assert members == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["home", "tmp"]
    assert list(child.tmp.iterdir()) == [] and list(child.home.iterdir()) == []


def _closed_early(tmp_path: Path, sql: str, read_first: int) -> tuple[int, bytes]:
    """Run a real child whose reader closes its end of standard output early.

    Parameters
    ----------
    tmp_path
        The test's directory.
    sql
        The statement.
    read_first
        Bytes to read before the close; 0 closes before the request is even sent.

    Returns
    -------
    tuple
        The child's exit status and its standard error.
    """
    with _Real({"sql": sql}, tmp_path, send=read_first > 0) as child:
        if read_first:
            child.read_at_least(read_first)
        assert child.process.stdout is not None
        child.process.stdout.close()
        child.process.stdout = None  # so that `communicate` reads standard error only
        if not read_first:
            child.send()
        _out, err = child.process.communicate(timeout=30)
    return child.process.returncode, err


class _Spy:
    """A real connection that records every statement string handed to `execute`.

    Parameters
    ----------
    real
        The connection to wrap.
    """

    def __init__(self, real: duckdb.DuckDBPyConnection) -> None:
        self._real = real
        self.executed: list[str] = []

    def execute(self, sql: str, parameters: object = None) -> duckdb.DuckDBPyConnection:
        """Record `sql`, then run it.

        Parameters
        ----------
        sql
            The statement text.
        parameters
            The bound values.

        Returns
        -------
        duckdb.DuckDBPyConnection
            The real connection, with the result.
        """
        self.executed.append(sql)
        return self._real.execute(sql, parameters)  # type: ignore[arg-type]

    def __getattr__(self, name: str) -> object:
        """Hand every other attribute to the real connection.

        Parameters
        ----------
        name
            The attribute.

        Returns
        -------
        object
            The real connection's attribute.
        """
        return getattr(self._real, name)


def _refused_spill_through_a_spy(sql: str) -> tuple[ErrorDocument, _Spy]:
    """Run `sql` on a real, configured connection as the child does, and classify its failure.

    The local file system is disabled and the memory limit is 32MB at one thread, so the sort
    inside `sql` must spill and DuckDB refuses its spill file.

    Parameters
    ----------
    sql
        The statement, which must fail.

    Returns
    -------
    tuple of (ErrorDocument, _Spy)
        What `query_child.failure` made of the failure, and the spy with every string sent.

    Raises
    ------
    AssertionError
        If the statement did not fail.
    """
    request = _request(sql, memory_limit="32MB", threads=1)
    real = duckdb.connect(":memory:")
    spy = _Spy(real)
    try:
        query_child.configure(spy, request)  # ty: ignore[invalid-argument-type]
        try:
            for _batch in spy.execute(sql, None).to_arrow_reader(request.batch_rows):
                pass
        except (duckdb.Error, OSError) as exc:
            document = query_child.failure(
                spy,  # ty: ignore[invalid-argument-type]
                exc,
                sql,
                threading.Event(),
            )
            return document, spy
        raise AssertionError("the statement was expected to fail")
    finally:
        real.close()


def test_a_multi_statement_string_is_never_executed_again_after_a_refused_spill() -> None:
    """Against real DuckDB: a refused spill after a CREATE and INSERT re-runs neither.

    The spy sees every string the child sends. The whole text goes to DuckDB once; after the
    spill is refused nothing more is sent, in particular no `EXPLAIN` of a text that holds the
    INSERT (which would run it again).
    """
    sql = (
        "CREATE TABLE c(i INT); INSERT INTO c VALUES (1); "
        "SELECT * FROM (SELECT i, repeat('x', 100) || i::VARCHAR AS b "
        "FROM range(200000) AS r(i)) ORDER BY b DESC"
    )
    document, spy = _refused_spill_through_a_spy(sql)
    assert document.kind == "invalid"
    assert "LocalFileSystem has been disabled" in document.message
    assert [text for text in spy.executed if text == sql] == [sql]
    assert not [text for text in spy.executed if "EXPLAIN" in text]


def test_a_single_select_is_planned_again_with_its_parameters_on_the_real_connection() -> None:
    """Against real DuckDB: the single re-plan is an `EXPLAIN` of the same text."""
    sql = (
        "SELECT * FROM (SELECT i, repeat('x', 100) || i::VARCHAR AS b "
        "FROM range(200000) AS r(i)) ORDER BY b DESC"
    )
    document, spy = _refused_spill_through_a_spy(sql)
    assert document.kind == "out_of_memory"
    assert spy.executed[-1] == f"EXPLAIN {sql}"


@pytest.mark.parametrize(
    ("sql", "read_first", "document"),
    [
        (
            WIDE_ROWS,
            64 * 1024,
            ErrorDocument("interrupted", "The worker stopped reading the result."),
        ),
        (
            "SELECT 1 AS i",
            0,
            ErrorDocument("interrupted", "The worker stopped reading the result."),
        ),
        ("SELEC 1", 0, None),
    ],
)
def test_a_reader_that_closes_the_pipe_leaves_the_error_document_as_the_last_line(
    tmp_path: Path, sql: str, read_first: int, document: ErrorDocument | None
) -> None:
    """Closing the output stream would flush again, raise, and print a traceback behind it."""
    code, err = _closed_early(tmp_path, sql, read_first)
    assert code == EXIT_FAILED
    found = decode_error(err)
    assert found is not None, err.decode(errors="replace")
    if document is None:
        assert found.kind == "invalid"
    else:
        assert found == document
    assert b"Traceback" not in err


def test_what_is_written_to_descriptor_1_lands_on_standard_error_not_in_the_stream(
    tmp_path: Path,
) -> None:
    code_text = (
        f"import os, {MODULE} as child\n"
        "real = child.main\n"
        "def main(*args, **kwargs):\n"
        "    os.write(1, b'an extension prints here\\n')\n"
        "    print('and so does a stray print', flush=True)\n"
        "    return real(*args, **kwargs)\n"
        "child.main = main\n"
        "child.run_cli()\n"
    )
    with _Real({}, tmp_path, code=code_text) as child:
        code, out, err = child.end()
    assert code == EXIT_OK
    assert _table(out).to_pydict() == {"answer": [42]}
    assert err == b"an extension prints here\nand so does a stray print\n"


def test_importing_the_child_loads_nothing_of_the_core(tmp_path: Path) -> None:
    """Every import is paid by every query: `lakana_core.config` alone costs 130 ms."""
    code = (
        f"import sys, {MODULE}\n"
        "heavy = {'lakana_core', 'sqlalchemy', 'pydantic', 'pydantic_settings', 'pyiceberg'}\n"
        "print(sorted(heavy & {name.split('.')[0] for name in sys.modules}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
        timeout=60,
    )
    assert done.stdout.strip() == "[]"


def _handles_sigterm(pid: int) -> bool:
    """Say whether a process has installed a handler for SIGTERM (Linux).

    Parameters
    ----------
    pid
        The process id.

    Returns
    -------
    bool
        Whether the signal is caught.
    """
    status = Path(f"/proc/{pid}/status").read_text()
    caught = next(line for line in status.splitlines() if line.startswith("SigCgt:"))
    return bool(int(caught.split()[1], 16) >> (signal.SIGTERM - 1) & 1)


def _running(pid: int) -> bool:
    """Say whether a process exists and is not a zombie.

    Parameters
    ----------
    pid
        The process id.

    Returns
    -------
    bool
        Whether it runs.
    """
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]
    except OSError:
        return False
    return state != "Z"


LONG_STREAM = "SELECT a.range FROM range(200000) a, range(200000) b WHERE a.range + b.range < 0"
"""The same shape as `SLOW_STREAM`, long enough by construction (minutes) that the statement is
still running whenever the test looks; the test ends it, and `finally` kills the group."""


@LINUX
def test_the_child_dies_when_its_worker_is_killed() -> None:
    """A stand-in worker starts the real child and is then killed with SIGKILL."""
    worker = (
        "import json, os, subprocess, sys, time\n"
        "request = json.loads(sys.argv[1]); request['parent_pid'] = os.getpid()\n"
        "child = subprocess.Popen(json.loads(sys.argv[2]), stdin=subprocess.PIPE,\n"
        "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)\n"
        "child.stdin.write(json.dumps(request).encode()); child.stdin.close()\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    request = json.loads(encode_request(_request(LONG_STREAM)))
    process = subprocess.Popen(
        [sys.executable, "-c", worker, json.dumps(request), json.dumps(CHILD_ARGV)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    child_pid = int(process.stdout.readline())
    try:
        deadline = time.monotonic() + 30
        while not _handles_sigterm(child_pid):  # `run_cli` has prepared the process
            assert _running(child_pid), "the child ended before it was ready"
            assert time.monotonic() < deadline, "the child never became ready"
            time.sleep(0.01)
        process.kill()
        process.wait(timeout=10)
        deadline = time.monotonic() + 2
        while _running(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _running(child_pid)
    finally:
        process.kill()
        process.wait(timeout=10)
        process.stdout.close()
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child_pid, signal.SIGKILL)


def test_a_child_that_cannot_be_given_its_request_is_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[subprocess.Popen[bytes]] = []
    leave = _Real.__exit__

    def spy(self: _Real, *exc: object) -> None:
        seen.append(self.process)
        leave(self, *exc)

    def refuse(request: object) -> bytes:
        raise RuntimeError("no request today")

    monkeypatch.setattr(_Real, "__exit__", spy)
    monkeypatch.setattr(sys.modules[__name__], "encode_request", refuse)
    with pytest.raises(RuntimeError, match="no request today"):
        _Real({}, tmp_path)
    assert len(seen) == 1 and seen[0].poll() is not None


_LOST_CAUSE = (
    "The statement failed, but DuckDB reported an interruption instead of the cause. "
    "Run it again to see the error."
)


@pytest.mark.parametrize(
    "exc",
    [OSError("INTERRUPT Error: Interrupted!"), duckdb.InterruptException("Interrupted!")],
    ids=["from the fetch", "from execute"],
)
def test_an_interrupt_nobody_asked_for_says_that_the_cause_was_lost(exc: BaseException) -> None:
    """DuckDB can report a sibling thread's interrupt instead of the error."""
    error = query_child.failure(
        _Planner(plans=True),  # ty: ignore[invalid-argument-type]
        exc,
        None,
        threading.Event(),
    )
    assert (error.kind, error.message) == ("invalid", _LOST_CAUSE)


def test_an_interrupt_that_was_asked_for_keeps_its_wording() -> None:
    asked = threading.Event()
    asked.set()
    error = query_child.failure(
        _Planner(plans=True),  # ty: ignore[invalid-argument-type]
        OSError("INTERRUPT Error: Interrupted!"),
        None,
        asked,
    )
    assert error == ErrorDocument("interrupted", "The query stopped because it was asked to.")


def test_other_errors_are_not_taken_for_a_lost_cause() -> None:
    error = query_child.failure(
        _Planner(plans=True),  # ty: ignore[invalid-argument-type]
        OSError("Conversion Error: boom"),
        None,
        threading.Event(),
    )
    assert error.message == "Query failed: Conversion Error: boom"
