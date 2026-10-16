"""The log formatter escapes control characters in the final record."""

from __future__ import annotations

import io
import logging
import re
import subprocess
import sys
from collections.abc import Iterator

import pytest

from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.identifiers import shown
from lakana_core.log_setup import (
    CONTINUATION_PREFIX,
    LOG_FORMAT,
    MAX_RECORD_CHARS,
    UNSAFE,
    SafeFormatter,
)

_STAMP = r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}"
_Captured = tuple[logging.Logger, io.StringIO]
_NASTY = "a\x1bb\rc\nFAKE 2026-10-09 00:00:00,000 ERROR lakana_core.x: forged\x07d\x85e\u202ef"


def _child(code: str) -> subprocess.CompletedProcess[str]:
    """Run `code` in a fresh interpreter, where no test fixture has touched logging.

    Parameters
    ----------
    code
        The program text.

    Returns
    -------
    subprocess.CompletedProcess
        The finished child, output captured as text.
    """
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _raise(error: Exception) -> None:
    """Raise `error`, so that a traceback has a frame to show.

    Parameters
    ----------
    error
        The exception.

    Raises
    ------
    Exception
        `error`, always.
    """
    raise error


@pytest.fixture
def captured() -> Iterator[_Captured]:
    """Give a logger of its own that writes through a real `SafeFormatter` into a stream.

    Yields
    ------
    tuple
        The logger and the stream its handler writes to.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SafeFormatter(LOG_FORMAT))
    logger = logging.Logger("third.party.library", logging.DEBUG)  # outside the logger tree
    logger.addHandler(handler)
    yield logger, stream


def _lines(stream: io.StringIO) -> list[str]:
    """Return what a handler wrote, line by line.

    Parameters
    ----------
    stream
        The handler's stream.

    Returns
    -------
    list[str]
        The lines.
    """
    return stream.getvalue().split("\n")[:-1]


def _assert_safe(stream: io.StringIO) -> None:
    """Assert that the output has no control character but line feeds, and one record header.

    Parameters
    ----------
    stream
        The handler's stream.
    """
    text = stream.getvalue()
    assert UNSAFE.search(text) is None, text
    lines = _lines(stream)
    assert re.match(_STAMP, lines[0])
    for line in lines[1:]:
        assert line.startswith(CONTINUATION_PREFIX), line
    assert sum(bool(re.match(_STAMP, line)) for line in lines) == 1, lines


def test_a_message_is_escaped_and_cannot_forge_a_record(
    captured: tuple[logging.Logger, io.StringIO],
) -> None:
    """ESC, CR, BEL, C1 and a bidi override are written out; a forged header is not a header."""
    logger, stream = captured
    logger.error(_NASTY)
    _assert_safe(stream)
    text = stream.getvalue()
    for piece in ("\\x1b", "\\x0d", "\\x07", "\\x85", "\\u202e"):
        assert piece in text
    assert f"\n{CONTINUATION_PREFIX}FAKE 2026" in text


def test_the_arguments_are_escaped(captured: _Captured) -> None:
    """Text that arrives as a `%s` argument is escaped like the message."""
    logger, stream = captured
    logger.error("name %s here", _NASTY)
    _assert_safe(stream)
    assert "\\x1b" in stream.getvalue()


def test_exception_text_and_traceback_are_escaped(
    captured: tuple[logging.Logger, io.StringIO],
) -> None:
    """The exception's own text, in the traceback, is escaped; the traceback stays readable."""
    logger, stream = captured
    try:
        _raise(ValueError(_NASTY))
    except ValueError:
        logger.exception("failed")
    _assert_safe(stream)
    text = stream.getvalue()
    assert "Traceback (most recent call last):" in text
    assert "ValueError: a\\x1bb\\x0dc" in text
    assert "raise error" in text  # the source line of the traceback is intact


def test_stack_info_is_escaped(captured: _Captured) -> None:
    """A stack printed with the record goes through the same escaping."""
    logger, stream = captured
    logger.error("with stack\x1b", stack_info=True)
    _assert_safe(stream)


def test_a_line_that_is_already_escaped_is_not_escaped_again(
    captured: tuple[logging.Logger, io.StringIO],
) -> None:
    """`shown()` and `%a` output carry backslashes, not control characters, and stay as they are."""
    logger, stream = captured
    logger.error("name %s and %a", shown("x\x1b\ny"), "p\x07q")
    [line] = _lines(stream)
    assert line.endswith("name x\\x1b\\ny and 'p\\x07q'")
    assert "\\\\" not in line


def test_a_plain_message_is_unchanged(captured: _Captured) -> None:
    """Printable text, non-ASCII letters and tabs-free lines pass as they are."""
    logger, stream = captured
    logger.warning("Größe 3 \\ done")
    [line] = _lines(stream)
    assert line.endswith("WARNING third.party.library: Größe 3 \\ done")


def test_the_formatter_never_raises(captured: _Captured) -> None:
    """A record that cannot be formatted is still one safe line; a non-string message works."""
    logger, stream = captured
    logger.error("needs two %s and %s", "one")
    logger.error(object())
    logger.error(None)
    logger.error(12)

    class Hostile:
        """An object whose text cannot be made."""

        def __str__(self) -> str:
            """Fail.

            Raises
            ------
            RuntimeError
                Always.
            """
            raise RuntimeError("no text")

    logger.error(Hostile())
    text = stream.getvalue()
    assert "needs two" in text
    assert text.count("ERROR third.party.library:") == 5
    assert "None" in text
    assert "12" in text


def test_a_hostile_exception_text_is_still_one_safe_record(
    captured: tuple[logging.Logger, io.StringIO],
) -> None:
    """An exception whose `__str__` fails does not lose the record."""
    logger, stream = captured

    class Bad(Exception):
        """An exception that cannot print itself."""

        def __str__(self) -> str:
            """Fail.

            Raises
            ------
            RuntimeError
                Always.
            """
            raise RuntimeError("no text")

    try:
        _raise(Bad())
    except Bad:
        logger.exception("failed\x1b")
    assert "ERROR third.party.library:" in stream.getvalue()
    assert "\x1b" not in stream.getvalue()


def test_configure_logging_installs_the_formatter_on_a_fresh_process() -> None:
    """The handler `configure_logging` adds uses `SafeFormatter`: a library's line is safe."""
    done = _child(
        "import logging\n"
        "from lakana_core.log_setup import configure_logging\n"
        "configure_logging()\n"
        "logging.getLogger('some.library').warning('a\\x1b[31mred\\nforged')\n"
        "try:\n"
        "    raise ValueError('x\\ry')\n"
        "except ValueError:\n"
        "    logging.getLogger('lakana_core.x').exception('boom')\n"
    )
    assert done.returncode == 0, done.stderr
    assert "\x1b" not in done.stderr and "\r" not in done.stderr
    assert "some.library: a\\x1b[31mred\n" + CONTINUATION_PREFIX + "forged" in done.stderr
    assert "ValueError: x\\x0dy" in done.stderr


class _Unprintable:
    """An object whose `repr` and `str` both fail."""

    def __repr__(self) -> str:
        """Fail.

        Raises
        ------
        RuntimeError
            Always.
        """
        raise RuntimeError("no repr")

    __str__ = __repr__


def test_a_message_whose_repr_and_str_fail_still_makes_a_record(captured: _Captured) -> None:
    """The fallback cannot raise: it ends in a constant, and starts like a normal record."""
    logger, stream = captured
    logger.error(_Unprintable())
    [line] = _lines(stream)
    assert re.match(rf"{_STAMP} ERROR third\.party\.library: ", line), line
    assert "test_log_escaping.py:" in line  # the call site


def test_mismatched_arguments_keep_the_traceback_and_the_call_site(
    captured: _Captured,
) -> None:
    """`logger.exception` with args that do not fit loses neither the traceback nor the place."""
    logger, stream = captured
    try:
        _raise(ValueError("kept\x1b"))
    except ValueError:
        logger.exception("two %s %s", "one")
    _assert_safe(stream)
    text = stream.getvalue()
    assert "could not be formatted" in text
    assert "test_log_escaping.py:" in text
    assert "Traceback (most recent call last):" in text
    assert "ValueError: kept\\x1b" in text
    assert re.match(_STAMP, text.splitlines()[0])  # a time first


def test_a_tab_is_four_spaces_as_in_the_cli(captured: _Captured) -> None:
    """The form of `safe_text`: a tab is not written as an escape."""
    logger, stream = captured
    logger.error("a\tb")
    assert _lines(stream)[0].endswith("a    b")


def test_a_lone_surrogate_is_escaped_so_a_strict_stream_takes_it() -> None:
    """The record does not depend on the stream's error handler."""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="utf-8", errors="strict")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SafeFormatter(LOG_FORMAT))
    logger = logging.Logger("surrogate", logging.DEBUG)
    logger.addHandler(handler)
    logger.error("name \ud800 and \udfff")
    stream.flush()
    assert raw.getvalue().decode().rstrip().endswith("name \\ud800 and \\udfff")


def test_a_record_over_the_bound_is_cut_with_a_count_and_no_split_escape(
    captured: _Captured,
) -> None:
    """The cut is before escaping, so no escape is cut in half; the marker counts the rest."""
    logger, stream = captured
    logger.error("\x1b" * (MAX_RECORD_CHARS) + "x" * 100)
    [line] = _lines(stream)
    marker = re.search(r"… \((\d+) more characters left out\)$", line)
    assert marker, line[-80:]
    assert 100 <= int(marker[1]) < 200  # the 100 extra characters and the record's header
    kept = line.rsplit("…", 1)[0]
    assert kept.endswith("\\x1b")  # the last escape is whole
    assert kept.count("\\x1b") * 4 == len(kept) - len(kept[: kept.index("\\x1b")])


def test_engine_echo_output_goes_through_the_safe_formatter() -> None:
    """`LAKANA_DATABASE__ECHO=true` makes SQLAlchemy add a handler of its own: it is made safe."""
    logger = logging.getLogger("sqlalchemy.engine.Engine")
    before, level = list(logger.handlers), logger.level
    logger.handlers[:] = []  # SQLAlchemy adds its handler only to a logger that has none
    try:
        engine = create_engine(DatabaseConfig(url="sqlite://", echo=True))
        engine.dispose()
        added = list(logger.handlers)
        assert added
        assert all(isinstance(h.formatter, SafeFormatter) for h in added)
        # An embedder's handler, there before the engine, keeps its formatter.
        mine = logging.Formatter("%(message)s")
        added[0].setFormatter(mine)
        create_engine(DatabaseConfig(url="sqlite://", echo=True)).dispose()
        assert added[0].formatter is mine
    finally:
        logger.handlers[:] = before
        logger.setLevel(level)


def test_an_exception_that_cannot_be_formatted_does_not_lose_the_record(
    captured: _Captured, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each part of the fallback has its own guard: a failing traceback leaves the rest."""
    logger, stream = captured

    def broken(self: SafeFormatter, exc_info: object) -> str:
        """Fail.

        Parameters
        ----------
        self
            The formatter.
        exc_info
            Unused.

        Raises
        ------
        RuntimeError
            Always.
        """
        raise RuntimeError("no traceback")

    monkeypatch.setattr(SafeFormatter, "formatException", broken)
    try:
        _raise(ValueError("x"))
    except ValueError:
        logger.exception("two %s %s", "one")
    text = stream.getvalue()
    assert "could not be formatted" in text and "(its exception cannot be shown)" in text


def test_a_formatter_whose_escaping_fails_still_ends_in_a_constant_line(
    captured: _Captured, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last resort is a constant string; it cannot fail."""
    logger, stream = captured

    def broken(text: str) -> str:
        """Fail.

        Parameters
        ----------
        text
            Unused.

        Raises
        ------
        RuntimeError
            Always.
        """
        raise RuntimeError("no escape")

    monkeypatch.setattr("lakana_core.log_setup.escape_record", broken)
    logger.error("anything")
    assert stream.getvalue() == "(a log record could not be formatted)\n"
