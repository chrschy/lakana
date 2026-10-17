"""One query in a process of its own: ``python -I -B -m lakana_worker.query_child``.

The worker starts this module once per query, with its own interpreter (``-I``: the working
directory and the environment's ``PYTHON*`` variables are ignored; ``-B``: no bytecode files, which
``PYTHONDONTWRITEBYTECODE`` would not achieve under ``-I``), in a new session, with an environment
built from scratch and a private ``HOME`` and ``TMPDIR``. The module reads one
request from standard input (`lakana_worker.query_protocol.Request`), opens one in-memory DuckDB
connection, configures it, runs the one statement and writes the result to standard output as an
Arrow IPC stream. It holds no credential: the statement reads presigned URLs.

Why each configuration statement is there, and why their order matters:

* ``extension_directory`` comes first. The child's ``HOME`` is empty, and DuckDB looks for
  installed extensions under ``HOME``; without the worker's directory ``INSTALL httpfs`` would
  download the extension for every query, and fail without network access.
* ``s3_access_key_id``, ``s3_secret_access_key`` and ``s3_session_token`` are cleared.
  `duckdb.connect()` fills them from ``AWS_*`` environment variables. The child's environment
  has none, and the statements stay as a second layer.
* ``allow_persistent_secrets = false`` keeps DuckDB's disk-backed secret store out, and must be
  set before the local file system is disabled: the secret manager reads local disk on the first
  httpfs read.
* ``autoinstall_known_extensions`` and ``autoload_known_extensions`` are off, so that no
  function loads an extension the module did not load on purpose.
* ``disabled_filesystems = 'LocalFileSystem'`` comes after ``INSTALL``/``LOAD httpfs``, which
  need the local disk. It also blocks DuckDB's own spill file, so a query over ``memory_limit``
  fails instead of spilling; that refusal has the same text as a refused local read, and
  `_is_refused_spill` tells them apart by planning the statement again.
* The resource limits come last; none touches the file system when set.

**Values are variables, not parameters.** Every value the statement reads (a file list, a
field-id map, a watermark) is set with ``SET VARIABLE "<name>" = ?`` before the statement, and
the statement runs with no parameters through ``connection.sql(sql).to_arrow_reader(n)``. With
parameters, ``sql()`` holds the whole result in memory before the first batch, and DuckDB 2
removed the Arrow export from ``execute()`` results.

**Being asked to stop.** The statement runs in a second thread. The main thread stays in Python,
so the handler of ``SIGTERM`` runs at once; it sets a flag, and the main thread then calls
`connection.interrupt()` ten times a second until the statement's thread ends. DuckDB clears the
interrupt flag when a statement starts, hence the repetition. With the statement on the main
thread the handler would not run while pyarrow waits for the next batch (measured: never within
60 s); with this arrangement a statement stopped 10 to 20 ms after the signal. The ask reaches
a child that is working in DuckDB or between two batches. A child blocked in a write to a pipe
nobody reads, or in reading a standard input nobody closed, does not react to it until the
blocking call returns: the worker reads the pipe, closes the input, or kills the group.

**Standard output is the result and nothing else.** Before anything runs, descriptor 1 is
duplicated for the Arrow stream and then pointed at standard error, so whatever DuckDB, an
extension or a stray ``print`` writes to "standard output" cannot corrupt the stream.

**The worker's death.** On Linux ``PR_SET_PDEATHSIG`` kills the child when the worker's thread
that started it ends. macOS has no such request: a child there can outlive a worker that is
killed, until it writes to the closed pipe or finishes.

A process boundary, not a sandbox: the child runs as the worker's user and sees its file system.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import re
import signal
import sys
import threading
import traceback
from collections.abc import Callable, Mapping, Sequence
from typing import BinaryIO, NoReturn, TextIO

import duckdb
import pyarrow as pa

from lakana_worker.query_protocol import (
    CHECK,
    DESCRIBE,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_REFUSED,
    INTERNAL,
    INTERRUPTED,
    INVALID,
    MAX_REQUEST_BYTES,
    MEMORY_LIMIT_ADVICE,
    NO_RESULT_MESSAGE,
    OUT_OF_MEMORY,
    READ,
    TRANSIENT,
    ErrorDocument,
    ParameterValue,
    Request,
    decode_request,
    encode_error,
)

_DUCKDB_LINE_MARKER = re.compile(r"^LINE \d+:")
"""Matches DuckDB's own echo of the offending line, e.g. ``LINE 1: SELECT ...``."""

_DUCKDB_CARET_MARKER = re.compile(r"^\s*\^+\s*$")
"""Matches the caret-only line DuckDB prints under a ``LINE n:`` echo."""

_DUCKDB_ERROR_TYPE = re.compile(r"^([A-Z][A-Za-z]*(?: [A-Za-z]+)*) Error:")
"""Matches the error-type prefix DuckDB puts on its messages, e.g. ``Binder Error:``."""

_TRANSIENT_ERROR_TYPES = frozenset({"IO", "HTTP"})
_OUT_OF_MEMORY_ERROR_TYPES = frozenset({"Out of Memory"})
_INTERNAL_ERROR_TYPES = frozenset({"INTERNAL", "FATAL"})
_SPILL_REFUSAL = "LocalFileSystem has been disabled by configuration"
"""DuckDB's refusal to open a file while the local file system is disabled. A refused local read
and a refused spill file produce the same text, so it never decides alone."""

_INTERRUPT_SECONDS = 0.1
"""How often the main thread interrupts the connection once it was asked to stop."""

_ASKED = "The query stopped because it was asked to."
_LOST_CAUSE = (
    "The statement failed, but DuckDB reported an interruption instead of the cause. "
    "Run it again to see the error."
)
"""With several threads DuckDB can cancel a sibling task of a failing statement and report that
interruption in place of the statement's own error (about 1 failure in 2,000 at 4 threads in
DuckDB 1.5.5). Nothing here interrupted the connection, so the cause is lost."""
_SINK_CLOSED = "The worker stopped reading the result."
_PR_SET_PDEATHSIG = 1
_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
"""A name the child sets a variable under. DuckDB binds no name in `SET VARIABLE`, so the name
is written into the statement, double-quoted, and must be an identifier."""

_Statement = str


def strip_duckdb_context(message: str) -> str:
    """Remove DuckDB's `LINE n:`/caret echo of the query text from an error message.

    DuckDB's own binder/parser errors quote the offending SQL back verbatim
    under a `LINE n:` marker, followed by a caret-marker line pointing at
    the token — and the SQL this module executes is already-rewritten,
    catalog-generated SQL (`lakana_core.query_rewrite.rewrite_query`),
    which can embed a row filter's or column mask's own restricted-subset
    expression directly in the CTE it builds. Neither must ever reach a
    caller: this truncates the message at the first such line, since
    everything from there on is DuckDB echoing text this module itself
    supplied, never new diagnostic information.

    Parameters
    ----------
    message
        The exception text as `str(exc)` produced it.

    Returns
    -------
    str
        `message` with the first `LINE n:` line (or caret-marker line, if
        that comes first) and everything after it removed. Returned
        unchanged if neither marker appears.
    """
    lines = message.splitlines()
    for index, line in enumerate(lines):
        if _DUCKDB_LINE_MARKER.match(line) or _DUCKDB_CARET_MARKER.match(line):
            return "\n".join(lines[:index]).rstrip()
    return message


def _is_transient(exc: BaseException) -> bool:
    """Tell a retryable I/O failure from a permanent SQL failure.

    DuckDB raises `IOException` for storage and network trouble, but an
    error surfacing while a later batch is fetched arrives as a plain
    `OSError` whatever its cause — a failed `CAST` as much as a dropped
    connection. Those are told apart by the error-type prefix DuckDB puts on
    its message: any type but `IO`/`HTTP` is a SQL error that a retry would
    only repeat.

    Parameters
    ----------
    exc
        The `duckdb.Error` or `OSError` that ended the query.

    Returns
    -------
    bool
        Whether a retry may succeed.
    """
    if isinstance(exc, duckdb.IOException):
        return True
    if not isinstance(exc, OSError):
        return False
    matched = _DUCKDB_ERROR_TYPE.match(str(exc))
    return matched is None or matched.group(1) in _TRANSIENT_ERROR_TYPES


def _error_type(exc: BaseException) -> str | None:
    """Read the error type DuckDB put on a plain `OSError`'s message.

    Parameters
    ----------
    exc
        The exception.

    Returns
    -------
    str or None
        The prefix (``Out of Memory``, ``INTERNAL``, ...) of an `OSError`
        carrying one; `None` for any other exception, whose class says it.
    """
    if not isinstance(exc, OSError):
        return None
    matched = _DUCKDB_ERROR_TYPE.match(str(exc))
    return None if matched is None else matched.group(1)


def _is_interrupt(exc: BaseException) -> bool:
    """Tell DuckDB's interrupt error from any other.

    Parameters
    ----------
    exc
        The `duckdb.Error` or `OSError` that ended the query.

    Returns
    -------
    bool
        Whether DuckDB reports an interruption (``INTERRUPT Error``).
    """
    return isinstance(exc, duckdb.InterruptException) or _error_type(exc) == "INTERRUPT"


def _is_out_of_memory(exc: BaseException) -> bool:
    """Tell an out-of-memory failure from any other DuckDB error.

    Parameters
    ----------
    exc
        The `duckdb.Error` or `OSError` that ended the query.

    Returns
    -------
    bool
        Whether DuckDB ran out of memory.
    """
    return isinstance(exc, duckdb.OutOfMemoryException) or (
        _error_type(exc) in _OUT_OF_MEMORY_ERROR_TYPES
    )


def _is_permission_refusal(exc: BaseException) -> bool:
    """Tell DuckDB's refusal to use the disabled local file system from any other error.

    Parameters
    ----------
    exc
        The `duckdb.Error` or `OSError` that ended the query.

    Returns
    -------
    bool
        Whether DuckDB refused to touch the local file system. A refused local read and
        a refused spill file both qualify.
    """
    is_permission = isinstance(exc, duckdb.PermissionException) or (
        _error_type(exc) == "Permission"
    )
    return is_permission and _SPILL_REFUSAL in str(exc)


def _is_internal(exc: BaseException) -> bool:
    """Tell a DuckDB internal or fatal error from an error about the query.

    Parameters
    ----------
    exc
        The `duckdb.Error` or `OSError` that ended the query.

    Returns
    -------
    bool
        Whether DuckDB failed on its own bug or state, not on the SQL.
    """
    return isinstance(exc, (duckdb.InternalException, duckdb.FatalException)) or (
        _error_type(exc) in _INTERNAL_ERROR_TYPES
    )


def configure(connection: duckdb.DuckDBPyConnection, request: Request) -> None:
    """Apply the isolation settings and the limits, in their required order.

    See the module's docstring for why each statement is there.

    Parameters
    ----------
    connection
        A fresh in-memory connection.
    request
        Supplies the extension directory, the limits and the mode: a mode other than ``read``
        pins the session time zone to UTC, so a ``TIMESTAMPTZ`` exports as a type a table can
        store.
    """
    connection.execute("SET extension_directory = ?", [request.extension_directory])
    connection.execute("SET allow_community_extensions = false")
    connection.execute("INSTALL httpfs")
    connection.execute("LOAD httpfs")
    for credential in ("s3_access_key_id", "s3_secret_access_key", "s3_session_token"):
        connection.execute(f"SET {credential} = ''")
    connection.execute("SET allow_persistent_secrets = false")
    connection.execute("SET autoinstall_known_extensions = false")
    connection.execute("SET autoload_known_extensions = false")
    if request.restrict_local_filesystem:
        connection.execute("SET disabled_filesystems = 'LocalFileSystem'")
    connection.execute("SET memory_limit = ?", [request.memory_limit])
    connection.execute("SET threads = ?", [request.threads])
    connection.execute("SET temp_directory = ?", [request.temp_directory])
    connection.execute("SET max_temp_directory_size = ?", [request.max_temp_directory_size])
    if request.mode != READ:
        connection.execute("SET TimeZone = 'UTC'")


def _is_refused_spill(
    connection: duckdb.DuckDBPyConnection, exc: BaseException, statement: _Statement | None
) -> bool:
    """Tell a refused spill file from a refused local read.

    A local read is refused while the statement is planned; a spill is refused while it runs.
    So the statement is planned again on the same connection (``EXPLAIN`` executes nothing): if
    it plans, the refusal came from a spill. Only a text of exactly one ``SELECT`` is planned
    again, as the statement DuckDB extracted from it: ``EXPLAIN a; b`` would execute ``b``.

    Parameters
    ----------
    connection
        The connection the statement failed on.
    exc
        The `duckdb.Error` or `OSError` that ended the statement.
    statement
        The SQL; `None` when nothing may be planned again (a describe).

    Returns
    -------
    bool
        Whether the refusal came from a spill file.
    """
    if statement is None or not _is_permission_refusal(exc):
        return False
    try:
        statements = connection.extract_statements(statement)
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            return False
        connection.execute(f"EXPLAIN {statements[0].query}")
    except (duckdb.Error, OSError):
        return False
    return True


def failure(
    connection: duckdb.DuckDBPyConnection,
    exc: BaseException,
    statement: _Statement | None,
    asked: threading.Event,
    files: Sequence[str] = (),
) -> ErrorDocument:
    """Classify a DuckDB failure and word it for the user.

    Parameters
    ----------
    connection
        The connection the statement failed on, for the second planning.
    exc
        The `duckdb.Error` or `OSError` that ended the statement.
    statement
        The SQL, or `None` when nothing may be planned again.
    asked
        Set once the child was asked to stop; the failure is then `interrupted`, whatever DuckDB
        said. An interrupt DuckDB reports without that ask is a lost cause (`invalid`, as before,
        with a message that says so).
    files
        The file URLs of the reads, as variables carried them. DuckDB quotes a file by its
        name when it cannot read or convert it, and a URL can hold a signature, so each is
        replaced by ``<file>`` in the message.

    Returns
    -------
    ErrorDocument
        The kind and the message for the user. The message never echoes the SQL or a file.
    """
    if asked.is_set():
        return ErrorDocument(INTERRUPTED, _ASKED)
    if _is_interrupt(exc):
        return ErrorDocument(INVALID, _LOST_CAUSE)
    text = strip_duckdb_context(str(exc))
    for url in sorted(set(files), key=len, reverse=True):
        text = text.replace(url, "<file>")
    message = f"Query failed: {text}"
    if _is_transient(exc):
        return ErrorDocument(TRANSIENT, message)
    if _is_out_of_memory(exc):
        reason = text.split("\n", 1)[0].strip()
        return ErrorDocument(OUT_OF_MEMORY, f"Query failed: {reason} {MEMORY_LIMIT_ADVICE}")
    refused_spill = _is_refused_spill(connection, exc, statement)
    if asked.is_set():
        return ErrorDocument(INTERRUPTED, _ASKED)
    if refused_spill:
        return ErrorDocument(
            OUT_OF_MEMORY,
            "Query failed: the query ran out of memory and cannot spill to disk. "
            f"{MEMORY_LIMIT_ADVICE}",
        )
    if _is_internal(exc):
        return ErrorDocument(INTERNAL, message)
    return ErrorDocument(INVALID, message)


class Session:
    """The statement's connection, shared between its thread and the main thread.

    The connection is private; use `open`, `interrupt` and `close`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connection: duckdb.DuckDBPyConnection | None = None

    def open(self) -> duckdb.DuckDBPyConnection:
        """Open the in-memory connection.

        Returns
        -------
        duckdb.DuckDBPyConnection
            The connection, which `interrupt` and `close` now act on.
        """
        connection = duckdb.connect(":memory:")
        with self._lock:
            self._connection = connection
        return connection

    def interrupt(self) -> None:
        """Interrupt the running statement, if the connection is open."""
        with self._lock:
            if self._connection is not None:
                self._connection.interrupt()

    def close(self) -> None:
        """Close the connection; a later `interrupt` does nothing."""
        with self._lock:
            connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()


def _stream(
    connection: duckdb.DuckDBPyConnection,
    reader: pa.RecordBatchReader,
    sink: BinaryIO,
    statement: _Statement | None,
    asked: threading.Event,
    files: Sequence[str] = (),
) -> ErrorDocument | None:
    """Write a result to the sink as an Arrow IPC stream, end marker included.

    An error from DuckDB (raised by `next`) is caught by the inner handler, an error from the
    sink (raised by a write) by the outer one: a broken pipe is an `OSError` too, and must not
    be reported as a storage error of the query.

    Parameters
    ----------
    connection
        The connection the result comes from.
    reader
        The result as an Arrow reader.
    sink
        Where the stream goes (standard output).
    statement
        The SQL, for the classification of an error.
    asked
        Set once the child was asked to stop; checked before each batch.
    files
        The file URLs of the reads, for `failure` to keep out of a message.

    Returns
    -------
    ErrorDocument or None
        `None` after the end marker was written; otherwise why the stream ends without it.
    """
    iterator = iter(reader)
    try:
        writer = pa.ipc.new_stream(sink, reader.schema)
        while True:
            if asked.is_set():
                return ErrorDocument(INTERRUPTED, _ASKED)
            try:
                batch = next(iterator)
            except StopIteration:
                break
            except (duckdb.Error, OSError) as exc:
                return failure(connection, exc, statement, asked, files)
            writer.write_batch(batch)
        writer.close()
        sink.flush()
    except (OSError, pa.ArrowInvalid):
        return ErrorDocument(INTERRUPTED, _SINK_CLOSED)
    return None


def _check(connection: duckdb.DuckDBPyConnection, request: Request) -> ErrorDocument | None:
    """Try the two size settings and nothing else (the worker's start-up check).

    Parameters
    ----------
    connection
        A fresh in-memory connection.
    request
        Supplies ``memory_limit`` and ``max_temp_directory_size``.

    Returns
    -------
    ErrorDocument or None
        An `invalid` document whose message is the name of the first setting DuckDB cannot
        read; `None` if both are accepted.
    """
    for field in ("memory_limit", "max_temp_directory_size"):
        try:
            connection.execute(f"SET {field} = ?", [getattr(request, field)])
        except duckdb.Error:
            return ErrorDocument(INVALID, field)
    return None


def _report_unexpected(exc: BaseException) -> None:
    """Print where an unexpected exception was raised, to standard error.

    Only the frames (this module's own source lines) and the exception's type are printed, never
    its message: DuckDB's messages can quote the statement. The document the child sends is the
    last line, so the worker's log has the cause and the reader has the document.

    Parameters
    ----------
    exc
        The exception.
    """
    frames = "".join(traceback.format_tb(exc.__traceback__))
    with contextlib.suppress(Exception):
        sys.stderr.write(f"Traceback (most recent call last):\n{frames}{type(exc).__qualname__}\n")


def set_variables(
    connection: duckdb.DuckDBPyConnection, variables: Mapping[str, ParameterValue]
) -> None:
    """Set each value the statement reads as a DuckDB variable of that name.

    The value is bound, never written into SQL text, so no URL can appear in a statement
    DuckDB quotes back in an error. The statement then runs with no parameters, through the
    call whose result streams and keeps an Arrow export in DuckDB 2.

    Parameters
    ----------
    connection
        The configured connection.
    variables
        Name to value, as the request carries them.

    Raises
    ------
    ValueError
        If a name is not an identifier. The platform generates every name, so this is a
        defect of the platform, never a user's mistake.
    """
    for name, value in variables.items():
        if _VARIABLE_NAME.fullmatch(name) is None:
            raise ValueError(f"query variable {ascii(name)[:60]} is not an identifier")
        # `name` is an identifier, checked above; the value is bound.
        statement = f'SET VARIABLE "{name}" = '
        if isinstance(value, list):
            connection.execute(statement + "?::VARCHAR[]", [value])
        elif isinstance(value, tuple):
            # DuckDB 1.5's `read_parquet(schema = ...)` needs all three children; a NULL
            # default makes a column a file lacks read as NULL, as Iceberg says.
            connection.execute(
                statement + "MAP(?::INTEGER[], "
                "?::STRUCT(name VARCHAR, type VARCHAR, default_value VARCHAR)[])",
                [
                    [field_id for field_id, _name, _type in value],
                    [
                        {"name": name, "type": type_, "default_value": None}
                        for _field_id, name, type_ in value
                    ],
                ],
            )
        else:
            connection.execute(statement + "?", [value])


def serve(
    request: Request, sink: BinaryIO, session: Session, asked: threading.Event
) -> ErrorDocument | None:
    """Run one request to its end.

    Raises nothing: an exception that was not expected becomes an ``internal`` document (its type
    only), after its frames and type are printed to standard error. Closing the connection cannot
    raise into the caller either.

    Parameters
    ----------
    request
        What to run and how DuckDB is configured.
    sink
        Where the Arrow stream goes.
    session
        Holds the connection, so the main thread can interrupt it.
    asked
        Set once the child was asked to stop.

    Returns
    -------
    ErrorDocument or None
        `None` for a complete stream; otherwise why the statement failed. An unexpected
        exception becomes an `internal` document that names its class and nothing else.
    """
    try:
        connection = session.open()
        statement: _Statement | None = None
        files = [
            url for value in request.parameters.values() if isinstance(value, list) for url in value
        ]
        try:
            if request.mode == CHECK:
                refused = _check(connection, request)
                if refused is not None:
                    return refused
                reader = pa.RecordBatchReader.from_batches(pa.schema([]), [])
            else:
                configure(connection, request)
                if asked.is_set():
                    return ErrorDocument(INTERRUPTED, _ASKED)
                set_variables(connection, request.parameters)
                if request.mode != DESCRIBE:
                    statement = request.sql
                # `sql` and not `execute`: its relation streams through `to_arrow_reader`, and it
                # is the call whose result keeps an Arrow export in DuckDB 2. Nothing is bound to
                # the statement; what it reads was set as variables above.
                result = connection.sql(request.sql)
                if result is None:
                    return ErrorDocument(INVALID, NO_RESULT_MESSAGE)
                reader = result.to_arrow_reader(request.batch_rows)
        except (duckdb.Error, OSError) as exc:
            return failure(connection, exc, statement, asked, files)
        return _stream(connection, reader, sink, statement, asked, files)
    except BaseException as exc:  # the statement's thread must hand back a document
        _report_unexpected(exc)
        return ErrorDocument(
            INTERNAL,
            f"Query failed: the query process hit an unexpected error ({type(exc).__name__}).",
        )
    finally:
        with contextlib.suppress(Exception):
            session.close()


def main(
    stdin: BinaryIO,
    stream: BinaryIO,
    stderr: TextIO,
    asked: threading.Event,
    parent: Callable[[], int] = os.getppid,
) -> int:
    """Read the request, run it in a second thread, and report how it ended.

    Parameters
    ----------
    stdin
        Where the request is read from.
    stream
        Where the Arrow stream is written.
    stderr
        Where a refusal or the error document is written.
    asked
        Set by the ``SIGTERM`` handler; the main thread then interrupts the connection every
        tenth of a second until the statement's thread ends.
    parent
        Returns the process id of the parent; compared with the request's ``parent_pid``.

    Returns
    -------
    int
        `EXIT_OK`, `EXIT_REFUSED` (unreadable request, or the worker is gone) or `EXIT_FAILED`.
    """
    try:
        request = decode_request(stdin.read(MAX_REQUEST_BYTES + 1))
    except ValueError as exc:
        stderr.write(f"{exc}\n")
        return EXIT_REFUSED
    if parent() != request.parent_pid:
        stderr.write("the worker that started this query is gone\n")
        return EXIT_REFUSED
    session = Session()
    outcome: list[ErrorDocument | None] = []
    thread = threading.Thread(
        target=lambda: outcome.append(serve(request, stream, session, asked)),
        name="lakana-query",
        daemon=True,
    )
    thread.start()
    while thread.is_alive():
        thread.join(_INTERRUPT_SECONDS)
        if asked.is_set():
            session.interrupt()
    error = outcome[0]
    if error is None:
        return EXIT_OK
    stderr.write(encode_error(error) + "\n")
    return EXIT_FAILED


def die_with_parent() -> None:
    """Ask the kernel to kill this process when the worker that started it dies (Linux).

    The signal fires when the *thread* that started this process exits, so the worker keeps
    that thread alive until the process is reaped. A worker that died before this call is
    caught by the ``parent_pid`` check in `main`. Other platforms have no such request.

    Raises
    ------
    SystemExit
        With status `EXIT_REFUSED` if the kernel refuses the request.
    """
    if not sys.platform.startswith("linux"):
        return
    if ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGKILL) != 0:
        sys.stderr.write("could not ask the kernel to end this process with its worker\n")
        raise SystemExit(EXIT_REFUSED)


def run_cli() -> NoReturn:
    """Prepare the process, run `main`, and end the process at once.

    In order: the kernel request of `die_with_parent`; the ``SIGTERM`` handler; a private copy
    of descriptor 1 for the Arrow stream, and descriptor 1 itself pointed at standard error.
    The process ends with `os._exit`, so that no thread DuckDB or pyarrow left behind and no
    interpreter shutdown can delay it.
    """
    code = EXIT_REFUSED
    try:
        die_with_parent()
        asked = threading.Event()
        signal.signal(signal.SIGTERM, lambda signum, frame: asked.set())
        stream_fd = os.dup(1)
        os.dup2(2, 1)
        # Not closed: closing flushes, and a reader that went away makes that raise again behind
        # the error document. `_stream` flushes a complete result; `os._exit` below ends the rest.
        stream = os.fdopen(stream_fd, "wb")
        code = main(sys.stdin.buffer, stream, sys.stderr, asked)
    except Exception:
        traceback.print_exc(file=sys.stderr)
    finally:
        with contextlib.suppress(Exception):
            sys.stderr.flush()
        os._exit(code)


if __name__ == "__main__":
    run_cli()
