"""One query in a process of its own: ``python -I -B -m lakana_worker.query_child``.

The worker starts this module once per query, with its own interpreter (``-I``: the working
directory and the environment's ``PYTHON*`` variables are ignored; ``-B``: no bytecode files, which
``PYTHONDONTWRITEBYTECODE`` would not achieve under ``-I``), in a new session, with an environment
built from scratch and a private ``HOME`` and ``TMPDIR``. The module reads one
request from standard input (`lakana_worker.query_protocol.Request`), opens one in-memory DuckDB
connection, configures it, runs the one statement and writes the result to standard output as an
Arrow IPC stream. It holds no credential: the statement reads presigned URLs.

**Allow-list mode.** DuckDB opens only what the request names: the locations of the tables the
query reads (`read_prefixes`: a directory in local mode, a URL prefix otherwise), the query's
private temp directory (DuckDB allows its own ``temp_directory``) and the CA bundle. Anything
else, another host, another scheme, a sibling directory, a local file, a symlink out of a table
directory, is refused while the statement is planned. DuckDB compares URLs as strings and sends a
``..`` segment to the server unchanged, so before anything runs the child checks every file the
variables name against the prefixes (`file_outside_prefixes`) and refuses the request when one
lies outside.

**One SELECT, checked by DuckDB's parser.** The lock holds settings, not secrets: ``CREATE SECRET
(TYPE http, HTTP_PROXY ...)`` or ``EXTRA_HTTP_HEADERS`` would still route the reads, presigned URLs
included, through a proxy of the statement's choosing. So before anything is configured the child
splits the text with `duckdb.DuckDBPyConnection.extract_statements` and runs it only if that is
exactly one ``SELECT`` (`NOT_ONE_SELECT` otherwise, an ``internal`` failure). The guard refuses
all of these first; this is the second layer.

Why each configuration statement is there, and why their order matters:

1. ``extension_directory`` comes first. The child's ``HOME`` is empty, and DuckDB looks for
   installed extensions under ``HOME``; without the worker's directory ``INSTALL httpfs`` would
   download the extension for every query, and fail without network access.
2. ``allow_community_extensions = false``; then ``INSTALL``/``LOAD httpfs``, which need the
   extension directory, before external access is switched off.
3. ``s3_access_key_id``, ``s3_secret_access_key`` and ``s3_session_token`` are cleared.
   `duckdb.connect()` fills them from ``AWS_*`` environment variables. The child's environment
   has none, and the statements stay as a second layer.
4. ``allow_persistent_secrets = false`` keeps DuckDB's disk-backed secret store out (the secret
   manager would read local disk on the first httpfs read);
   ``autoinstall_known_extensions`` and ``autoload_known_extensions`` are off, so that no
   function loads an extension the module did not load on purpose.
5. ``ca_cert_file`` is the request's bundle. DuckDB 2 reads the bundle lazily, through the local
   file system, on the first HTTPS request; it must be named and allowed, or every HTTPS read
   fails with ``Cannot read a CA certificate bundle``.
6. ``temp_directory`` before external access is switched off: afterwards DuckDB refuses to
   change it (``Modifying the temp_directory has been disabled``).
7. ``allowed_paths`` (the bundle) and ``allowed_directories`` (the prefixes), before external
   access is switched off: afterwards DuckDB refuses to change them.
8. ``enable_external_access = false``: from here on DuckDB opens only the allowed paths and
   directories, and the temp directory.
9. The limits: ``memory_limit``, ``threads``, ``max_temp_directory_size`` (``0KB`` unless the
   request allows spilling, which only local mode does: a query over ``memory_limit`` then fails
   with DuckDB's ``Out of Memory Error``) and, for a mode other than ``read``, ``TimeZone``. All
   are still settable after step 8.
10. ``lock_configuration = true`` last: no ``SET``, ``RESET`` or ``PRAGMA`` of any setting
    succeeds after it (``SET VARIABLE`` still does). A second layer behind the guard, which
    refuses ``SET`` itself, against ``http_proxy`` (a proxy would receive the presigned URLs),
    ``extra_http_headers`` and ``ca_cert_file`` as settings (not as a secret: see above).

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
import functools
import os
import re
import signal
import sys
import threading
import traceback
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from typing import BinaryIO, NoReturn, TextIO

import duckdb
import pyarrow as pa

from lakana_worker.query_protocol import (
    CHECK,
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
    UNCONVERTED_VALUE,
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
_CA_REFUSAL = "Cannot read a CA certificate bundle"
"""DuckDB 2's refusal to load the CA bundle under the restriction. It should not occur once
``ca_cert_file`` and ``allowed_paths`` name the bundle; pinned so that a DuckDB change shows."""

REFUSED_LOCATION = (
    "Query failed: the query tried to open a file or an address that is not one of the tables "
    "it reads."
)
"""What a child says of DuckDB's refusal to open a path or URL outside the allowed ones. DuckDB's
own text quotes the path, so it is never passed on."""

OUTSIDE_PREFIXES = (
    "Query failed: a file of the query lies outside the locations it was given. This is a "
    "defect of Lakana; please report it."
)
"""What a child says, without running anything, when a file of a variable lies outside the
request's prefixes, or a prefix has a shape the child does not accept."""

NOT_ONE_SELECT = (
    "Query failed: the query process was given something other than one SELECT statement. "
    "This is a defect of Lakana; please report it."
)
"""What a child says, without running anything, of SQL that DuckDB's parser does not read as
exactly one ``SELECT``. Every mode sends one (a describe wraps it in ``SELECT * FROM (...)
LIMIT 0``); anything else (a second statement, ``CREATE SECRET``, ``SET``, ``PRAGMA``,
``ATTACH``, ``COPY``, ``LOAD``, ``CALL``) is refused here as the layer behind the guard."""

NO_SPILL = (
    f"Query failed: the query ran out of memory and cannot spill to disk. {MEMORY_LIMIT_ADVICE}"
)
"""What a child says of running out of memory when the request does not allow spilling."""

NETWORK_SETTINGS = "https://lakana-labs.github.io/lakana/reference/network-settings/"
"""The page that says how to trust a private certificate authority."""

_REMOTE_PREFIX = re.compile(
    r"https?://(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?"
    r"/[^?#\s\\]+/"
)
"""A remote prefix: ``http(s)://host[:port]/`` and a non-empty path ending in ``/``. No user
information, query, fragment, white space or backslash."""

_DOT_ESCAPE = re.compile(r"%2e", re.IGNORECASE)
"""A percent-escaped dot, which a server may decode into a ``..`` segment."""

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

_READ_CAST = re.compile(
    r'failed to cast column "(?P<column>[^"]*)" from type (?P<stored>.+?) to (?P<wanted>.+?): :'
)
"""DuckDB's report of a column a file stores in a type the read cannot convert to."""


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


def _is_permission(exc: BaseException) -> bool:
    """Tell DuckDB's refusal to open a path or URL from any other error.

    Parameters
    ----------
    exc
        The `duckdb.Error` or `OSError` that ended the query.

    Returns
    -------
    bool
        Whether DuckDB reports a ``Permission Error``, by the class or by the message's prefix.
    """
    return isinstance(exc, duckdb.PermissionException) or _error_type(exc) == "Permission"


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


def _has_dot_segment(path: str, separators: str) -> bool:
    """Tell whether a path holds a ``.`` or ``..`` segment.

    Parameters
    ----------
    path
        The path, or the part of a URL after its prefix.
    separators
        The characters that separate segments.

    Returns
    -------
    bool
        Whether a segment is ``.`` or ``..``.
    """
    return any(segment in (".", "..") for segment in re.split(f"[{re.escape(separators)}]", path))


def valid_prefix(prefix: str) -> bool:
    """Tell whether a prefix has a shape the child gives DuckDB as an allowed location.

    Parameters
    ----------
    prefix
        One of the request's `read_prefixes`.

    Returns
    -------
    bool
        For a remote prefix: ``http(s)://host[:port]/`` and a non-empty path ending in ``/``
        whose segments are neither empty nor ``.``/``..`` and that holds no ``%2e``. For a local
        one: absolute, ending in ``/``, `os.path.normpath`-canonical before that slash, and not
        ``/`` itself. Nothing else (no NUL byte either).
    """
    if "\x00" in prefix:
        return False
    if prefix.startswith(("http://", "https://")):
        if _REMOTE_PREFIX.fullmatch(prefix) is None:
            return False
        path = prefix.split("/", 3)[3][:-1]
        return not _DOT_ESCAPE.search(path) and all(
            segment not in ("", ".", "..") for segment in path.split("/")
        )
    directory = prefix[:-1]
    return (
        prefix.startswith("/")
        and not prefix.startswith("//")
        and prefix.endswith("/")
        and directory != ""
        and os.path.normpath(directory) == directory
    )


def _inside(url: str, prefix: str) -> bool:
    """Tell whether a file lies under one prefix, without a way out of it.

    Parameters
    ----------
    url
        A file the statement reads: a presigned URL, or a local path.
    prefix
        A prefix `valid_prefix` accepted.

    Returns
    -------
    bool
        Whether `url` starts with `prefix` and the rest (up to ``?`` for a URL) holds no
        ``.``/``..`` segment and no ``%2e``. A URL's rest is also checked once percent-decoded,
        split at ``/`` and at a backslash, as a server might read it.
    """
    if "\x00" in url or not url.startswith(prefix):
        return False
    rest = url[len(prefix) :]
    if not prefix.startswith(("http://", "https://")):
        return not _DOT_ESCAPE.search(rest) and not _has_dot_segment(rest, "/")
    rest = rest.split("?", 1)[0]
    return not (
        _DOT_ESCAPE.search(rest)
        or _has_dot_segment(rest, "/")
        or _has_dot_segment(urllib.parse.unquote(rest), "/\\")
    )


def file_outside_prefixes(request: Request) -> bool:
    """Tell whether the request must be refused before anything runs (fail closed).

    DuckDB checks only what it opens, and sends a URL's ``..`` to the server unchanged
    (``.../t1/../t2/a.parquet`` passes an allowed ``.../t1/``), so the child checks the files
    itself.

    Parameters
    ----------
    request
        The request: its prefixes and the file lists among its variables.

    Returns
    -------
    bool
        Whether a prefix has a shape `valid_prefix` refuses, the CA bundle is not an absolute
        path, or a string of a list-valued variable does not lie inside one of the prefixes.
    """
    if not all(valid_prefix(prefix) for prefix in request.read_prefixes):
        return True
    if not os.path.isabs(request.ca_bundle):
        return True
    return any(
        not any(_inside(url, prefix) for prefix in request.read_prefixes)
        for value in request.parameters.values()
        if isinstance(value, list)
        for url in value
    )


def configure(connection: duckdb.DuckDBPyConnection, request: Request) -> None:
    """Apply the isolation settings and the limits, in their required order, then lock them.

    See the module's docstring for why each statement is there.

    Parameters
    ----------
    connection
        A fresh in-memory connection.
    request
        Supplies the extension directory, the CA bundle, the prefixes, the temp directory, the
        limits and the mode: a mode other than ``read`` pins the session time zone to UTC, so a
        ``TIMESTAMPTZ`` exports as a type a table can store.
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
    connection.execute("SET ca_cert_file = ?", [request.ca_bundle])
    connection.execute("SET temp_directory = ?", [request.temp_directory])
    connection.execute("SET allowed_paths = ?", [[request.ca_bundle]])
    connection.execute("SET allowed_directories = ?", [list(request.read_prefixes)])
    connection.execute("SET enable_external_access = false")
    connection.execute("SET memory_limit = ?", [request.memory_limit])
    connection.execute("SET threads = ?", [request.threads])
    connection.execute(
        "SET max_temp_directory_size = ?",
        [request.max_temp_directory_size if request.spill else "0KB"],
    )
    if request.mode != READ:
        connection.execute("SET TimeZone = 'UTC'")
    connection.execute("SET lock_configuration = true")


def failure(
    exc: BaseException,
    asked: threading.Event,
    files: Sequence[str] = (),
    mapped: bool = False,
    *,
    spill: bool = False,
    ca_bundle: str = "",
) -> ErrorDocument:
    """Classify a DuckDB failure and word it for the user.

    Parameters
    ----------
    exc
        The `duckdb.Error` or `OSError` that ended the statement.
    asked
        Set once the child was asked to stop; the failure is then `interrupted`, whatever DuckDB
        said. An interrupt DuckDB reports without that ask is a lost cause (`invalid`, as before,
        with a message that says so).
    files
        The file URLs of the reads, as variables carried them. DuckDB quotes a file by its
        name when it cannot read or convert it, and a URL can hold a signature, so each is
        replaced by ``<file>`` in the message.
    mapped
        Whether a read goes through a field-id map. DuckDB then converts a file's column to
        the mapped type while it reads, and a conversion error quotes the cell: under a row
        filter the text names no column, so any conversion error is replaced by a fixed one
        that also covers a cast of the query's own.
    spill
        Whether the request allowed spilling. Without it, running out of memory is worded as
        "cannot spill to disk": decided by the request, never by DuckDB's text.
    ca_bundle
        The request's CA bundle, named in the message of a refused bundle.

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
    for url in sorted({url for url in files if url}, key=len, reverse=True):
        text = text.replace(url, "<file>")
    cast = _READ_CAST.search(text)
    if cast is not None:
        # DuckDB appends the cell it could not convert, which can be a masked value or one of a
        # row a filter hides; only the column and the two types are safe to say.
        text = (
            f'a file of a table this query reads stores column "{cast["column"]}" as '
            f"{cast['stored']}, which cannot be read as {cast['wanted']}. Ask the owner of "
            "that table to write its rows again, so that every file stores the column in "
            "one type."
        )
    elif mapped and text.startswith("Conversion Error"):
        text = UNCONVERTED_VALUE
    message = f"Query failed: {text}"
    if _is_transient(exc):
        return ErrorDocument(TRANSIENT, message)
    if _is_out_of_memory(exc):
        if not spill:
            return ErrorDocument(OUT_OF_MEMORY, NO_SPILL)
        reason = text.split("\n", 1)[0].strip()
        return ErrorDocument(OUT_OF_MEMORY, f"Query failed: {reason} {MEMORY_LIMIT_ADVICE}")
    if _is_permission(exc):
        if _CA_REFUSAL in text:
            return ErrorDocument(
                INTERNAL,
                f"Query failed: the query process could not read its certificate authorities "
                f"from {ca_bundle}. Set AWS_CA_BUNDLE or SSL_CERT_FILE on the worker to a "
                f"readable PEM file of your object store's certificate authority; see "
                f"{NETWORK_SETTINGS}.",
            )
        return ErrorDocument(INVALID, REFUSED_LOCATION)
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
    reader: pa.RecordBatchReader,
    sink: BinaryIO,
    asked: threading.Event,
    classify: Callable[[BaseException], ErrorDocument],
) -> ErrorDocument | None:
    """Write a result to the sink as an Arrow IPC stream, end marker included.

    An error from DuckDB (raised by `next`) is caught by the inner handler, an error from the
    sink (raised by a write) by the outer one: a broken pipe is an `OSError` too, and must not
    be reported as a storage error of the query.

    Parameters
    ----------
    reader
        The result as an Arrow reader.
    sink
        Where the stream goes (standard output).
    asked
        Set once the child was asked to stop; checked before each batch.
    classify
        Turns an error of DuckDB into the document (`failure`, with the request's details).

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
                return classify(exc)
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
        if request.mode != CHECK and file_outside_prefixes(request):
            return ErrorDocument(INTERNAL, OUTSIDE_PREFIXES)
        connection = session.open()
        files = [
            url for value in request.parameters.values() if isinstance(value, list) for url in value
        ]
        classify = functools.partial(
            failure,
            asked=asked,
            files=files,
            mapped=any(isinstance(value, tuple) for value in request.parameters.values()),
            spill=request.spill,
            ca_bundle=request.ca_bundle,
        )
        try:
            if request.mode == CHECK:
                refused = _check(connection, request)
                if refused is not None:
                    return refused
                reader = pa.RecordBatchReader.from_batches(pa.schema([]), [])
            else:
                statements = connection.extract_statements(request.sql)
                if not statements:
                    return ErrorDocument(INVALID, NO_RESULT_MESSAGE)
                if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                    return ErrorDocument(INTERNAL, NOT_ONE_SELECT)
                configure(connection, request)
                if asked.is_set():
                    return ErrorDocument(INTERRUPTED, _ASKED)
                set_variables(connection, request.parameters)
                # `sql` and not `execute`: its relation streams through `to_arrow_reader`, and it
                # is the call whose result keeps an Arrow export in DuckDB 2. Nothing is bound to
                # the statement; what it reads was set as variables above.
                result = connection.sql(request.sql)
                if result is None:
                    return ErrorDocument(INVALID, NO_RESULT_MESSAGE)
                reader = result.to_arrow_reader(request.batch_rows)
        except (duckdb.Error, OSError) as exc:
            return classify(exc)
        return _stream(reader, sink, asked, classify)
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
