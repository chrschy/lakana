"""Helpers shared by the venv build and the task runner."""

from __future__ import annotations

import base64
import errno
import itertools
import logging
import os
import random
import signal
import stat
import string
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from lakana_worker.processes import (
    CappedSink,
    OutputRedactor,
    is_private_directory,
    remove_private_tree,
    signal_group,
    tree_size,
)


def _collect(secrets: list[str], chunks: list[bytes], max_line: int = 65536) -> bytes:
    """Feed chunks through a redactor.

    Parameters
    ----------
    secrets
        Values to hide.
    chunks
        The reads, in order.
    max_line
        Longest line buffered.

    Returns
    -------
    bytes
        Everything the sink received.
    """
    out: list[bytes] = []
    redactor = OutputRedactor(secrets, out.append, max_line=max_line)
    for chunk in chunks:
        redactor.feed(chunk)
    redactor.close()
    redactor.close()
    return b"".join(out)


def test_secrets_and_userinfo_are_redacted() -> None:
    out = _collect(
        ["hunter2", "svc:hunter2", ""],
        [b"proxy http://svc:hunter2@proxy.corp:3128 said hunter2\n", b"plain line\n"],
    )
    assert out == b"proxy http://***@proxy.corp:3128 said ***\nplain line\n"


def test_a_secret_split_across_two_reads_is_still_redacted() -> None:
    assert _collect(["lakana_task_k1_s3cr3t"], [b"token lakana_task_k", b"1_s3cr3t end\n"]) == (
        b"token *** end\n"
    )


def test_an_unterminated_tail_is_flushed_on_close() -> None:
    assert _collect(["s3cr3t"], [b"no newline s3cr3t"]) == b"no newline ***"
    assert _collect([], []) == b""


def test_a_very_long_line_is_flushed_before_its_newline() -> None:
    out = _collect(["s3cr3t"], [b"a" * 10, b"s3cr3t", b"b" * 10, b"\n"], max_line=12)
    assert b"s3cr3t" not in out
    assert out.count(b"a") == 10 and out.count(b"b") == 10 and out.endswith(b"\n")


def test_tree_size_sums_regular_files_without_following_links(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"12345")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "b").write_bytes(b"123")
    (tmp_path / "link").symlink_to("/etc")
    assert tree_size(tmp_path) >= 8
    assert tree_size(tmp_path) < 4096
    assert tree_size(tmp_path / "missing") == 0


def test_signal_group_kills_the_whole_group_and_ignores_a_reaped_process() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    assert signal_group(process, signal.SIGKILL) is True
    assert process.wait(timeout=10) == -signal.SIGKILL
    assert signal_group(process, signal.SIGKILL) is False


def test_signal_group_says_when_the_group_is_already_gone() -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    process.wait(timeout=10)
    process.returncode = None  # as if not yet reaped: the group is gone all the same
    assert signal_group(process, signal.SIGKILL) is False
    process.returncode = 0


def test_signal_group_treats_a_group_of_exited_processes_as_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS refuses to signal a group whose members have all exited but are not yet reaped."""
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    process.wait(timeout=10)
    process.returncode = None

    def refuse(pgid: int, signum: int) -> None:
        """Fail as macOS's killpg does for a group of zombies.

        Parameters
        ----------
        pgid
            The process group.
        signum
            The signal.

        Raises
        ------
        PermissionError
            Always.
        """
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", refuse)
    assert signal_group(process, signal.SIGKILL) is False
    process.returncode = 0


_SECRET = "hunter2secret"


@pytest.mark.parametrize("offset", range(1, len(_SECRET)))
def test_a_secret_straddling_the_forced_flush_is_redacted(offset: int) -> None:
    out = _collect(
        [_SECRET],
        [b"a" * 20 + _SECRET.encode()[:offset], _SECRET.encode()[offset:] + b" tail\n"],
        max_line=12,
    )
    assert _SECRET.encode() not in out and _SECRET.encode()[:offset] not in out.replace(
        b"a" * 20, b""
    )
    assert out == b"a" * 20 + b"*** tail\n"


_URL = b"http://svc:pw12@pypi.corp/x"


@pytest.mark.parametrize("offset", range(1, len(_URL)))
def test_userinfo_straddling_the_forced_flush_is_redacted(offset: int) -> None:
    out = _collect([], [b"a" * 60 + b" " + _URL[:offset], _URL[offset:] + b"\n"], max_line=40)
    assert b"svc" not in out and b"pw12" not in out
    assert out == b"a" * 60 + b" http://***@pypi.corp/x\n"


def test_a_secret_with_a_line_break_is_redacted_across_two_reads() -> None:
    secret = "abc\ndefgh"
    assert _collect([secret], [b"x abc\n", b"defgh y\n"]) == b"x *** y\n"
    assert _collect([secret], [b"x abc\nde", b"fgh y\n"]) == b"x *** y\n"
    assert _collect([secret], [b"x abc", b"\ndefgh y\n"]) == b"x *** y\n"
    assert _collect([secret], [b"x abc\nzzz\n", b"defgh\n"]) == b"x abc\nzzz\ndefgh\n"


def test_a_secret_with_a_line_break_holds_back_at_most_one_byte_fewer_than_its_length() -> None:
    secret = "abc\ndefgh"
    out: list[bytes] = []
    redactor = OutputRedactor([secret], out.append)
    for _ in range(200):
        redactor.feed(b"a line of ordinary output\n")
        assert len(redactor._buffer) <= len(secret) - 1
    redactor.close()
    assert b"".join(out) == b"a line of ordinary output\n" * 200


def test_an_endless_scheme_prefix_does_not_hold_the_buffer_forever() -> None:
    out: list[bytes] = []
    redactor = OutputRedactor([], out.append, max_line=12)
    redactor.feed(b"http://" + b"x" * 100)
    assert len(b"".join(out)) > 50
    redactor.close()
    assert b"".join(out) == b"http://" + b"x" * 100


def test_output_that_may_be_the_start_of_a_secret_waits_and_other_output_does_not() -> None:
    out: list[bytes] = []
    redactor = OutputRedactor(["x" * 40], out.append, max_line=12)
    redactor.feed(b"x" * 13)
    assert out == []
    redactor.close()
    assert b"".join(out) == b"x" * 13
    redactor.feed(b"y" * 13)
    assert b"".join(out) == b"x" * 13 + b"y" * 13


def test_the_userinfo_pattern_has_no_unbounded_run_before_the_scheme_separator() -> None:
    from lakana_worker.processes import _USERINFO

    assert b"+://" not in _USERINFO.pattern and b"*://" not in _USERINFO.pattern
    assert b"\\w+" not in _USERINFO.pattern


def test_a_huge_run_of_word_characters_is_linear_and_unchanged() -> None:
    import time

    data = b"A" * (256 * 1024)
    started = time.monotonic()
    out = _collect([], [data, b"\n"])
    assert out == data + b"\n"
    assert time.monotonic() - started < 5


_LONG_SECRET = "".join(
    random.Random(5000).choices(string.ascii_letters + string.digits + "-_.~:/@% \n", k=5000)
)
_BASIC = base64.b64encode(b"svc:p@ss/w:rd").decode()

_CHUNKING_CASES: dict[str, tuple[list[str], list[tuple[bytes, bytes]]]] = {
    "a strict prefix of another (the regression N1)": (
        ["hunter", "hunter2-prod", "ci:hunter", "ci:hunter2-prod", "nl\npw"],
        [
            (b"auth ci:hunter2-prod end\n", b"auth *** end\n"),
            (b"auth hunter2-prod, hunter and ci:hunter.\n", b"auth ***, *** and ***.\n"),
            (
                b"x nl\npw y https://ci:hunter2-prod@pkgs.example.org/simple/a.whl\n",
                b"x *** y https://***@pkgs.example.org/simple/a.whl\n",
            ),
            (b"hunter2-pro nl\np hunter2-prod", b"***2-pro nl\np ***"),
        ],
    ),
    "a prefix pair next to a secret with a line break": (
        ["abc", "abcdefgh", "x\ny"],
        [
            (b"tok abcdefgh end\n", b"tok *** end\n"),
            (b"tok abcdefg end abc x\ny\n", b"tok ***defg end *** ***\n"),
        ],
    ),
    "a suffix of another": (
        ["defgh", "abc\ndefgh"],
        [(b"p abc\ndefgh q defgh r abc\nzz\n", b"p *** q *** r abc\nzz\n")],
    ),
    "self-overlap": (
        ["aaa", "aaaaa"],
        [(b"b aaaaaaa b aa b aaaa\n", b"b *** b aa b ***\n")],
    ),
    "the end of one is the start of another": (
        ["abc", "cde"],
        [(b"xabcdey abcd cdeabc\n", b"x***y ***d ******\n")],
    ),
    "only a line break": (["\n"], [(b"ab\ncd\n\nef", b"ab***cd******ef")]),
    "one and two line breaks": (["\n", "\n\n"], [(b"ab\ncd\n\nef", b"ab***cd***ef")]),
    "one character": (["Z"], [(b"aZbZZc Z\n", b"a***b******c ***\n")]),
    "a secret with a line break inside a URL": (
        ["pa\nss"],
        [(b"get https://user:pa\nss@host/x\nnext pa\nss\n", b"get https://***@host/x\nnext ***\n")],
    ),
    "every form of an index credential": (
        [
            "p@ss/w:rd",
            "svc:p@ss/w:rd",
            "p%40ss%2Fw%3Ard",
            "svc:p%40ss%2Fw%3Ard",
            _BASIC,
            base64.b64encode(b"p@ss/w:rd").decode(),
        ],
        [
            (
                b"error: https://svc:p%40ss%2Fw%3Ard@pkgs.example.org/simple failed\n",
                b"error: https://***@pkgs.example.org/simple failed\n",
            ),
            (
                b"  Caused by: HTTP status client error (401 Unauthorized) for url\n"
                b"  (https://svc:p@ss/w:rd@pkgs.example.org/files/x.whl)\n",
                b"  Caused by: HTTP status client error (401 Unauthorized) for url\n"
                b"  (https://***@pkgs.example.org/files/x.whl)\n",
            ),
            (
                b"Authorization: Basic " + _BASIC.encode() + b"\npassword p@ss/w:rd.\n",
                b"Authorization: Basic ***\npassword ***.\n",
            ),
        ],
    ),
    "URLs with nothing listed": (
        [],
        [
            (
                b"GET http://other:unlisted@127.0.0.1:8080/x and http://host/a@b ok\n",
                b"GET http://***@127.0.0.1:8080/x and http://host/a@b ok\n",
            ),
            (b"a:b :/ x:// y ://@z ://u@ t://://v@w", b"a:b :/ x:// y ://@z ://***@ t://://***@w"),
            (b"see https://user:pw", b"see https://user:pw"),
            (
                "gr\u00fc\u00dfe \u2603 https://\u00fc:\u00df@h\u00f6st/\n".encode(),
                "gr\u00fc\u00dfe \u2603 https://***@h\u00f6st/\n".encode(),
            ),
        ],
    ),
    "a 5,000-character secret": (
        [_LONG_SECRET, "tail"],
        [
            (
                b"pre "
                + _LONG_SECRET.encode()
                + b" mid "
                + _LONG_SECRET.encode()[:4999]
                + b" tail\n",
                b"pre *** mid " + _LONG_SECRET.encode()[:4999] + b" ***\n",
            )
        ],
    ),
}


def _random_splits(text: bytes, count: int, seed: int) -> list[list[bytes]]:
    """Cut a text at random places, the same places on every run.

    Parameters
    ----------
    text
        The bytes to cut.
    count
        How many ways of cutting to return.
    seed
        Seed of the generator.

    Returns
    -------
    list of list of bytes
        `count` lists of reads; each joins to `text` and has three to nine reads.
    """
    rng = random.Random(seed)
    splits = []
    for _ in range(count):
        cuts = sorted(rng.randrange(len(text) + 1) for _ in range(rng.randint(2, 8)))
        edges = [0, *cuts, len(text)]
        splits.append([text[a:b] for a, b in itertools.pairwise(edges)])
    return splits


def _chunkings(text: bytes) -> list[list[bytes]]:
    """List the ways a text is fed to the redactor in the chunking tests.

    Parameters
    ----------
    text
        The bytes to cut.

    Returns
    -------
    list of list of bytes
        Every two-way split, a few hundred seeded multi-way splits (thirty for
        a long text) and, for a short text, one byte per read.
    """
    short = len(text) < 1000
    chunkings = [[text[:cut], text[cut:]] for cut in range(1, len(text))]
    chunkings += _random_splits(text, 300 if short else 30, seed=len(text))
    if short:
        chunkings.append([text[i : i + 1] for i in range(len(text))])
    return chunkings


@pytest.mark.parametrize("name", list(_CHUNKING_CASES))
def test_one_read_gives_the_expected_redaction(name: str) -> None:
    secrets, texts = _CHUNKING_CASES[name]
    for text, expected in texts:
        assert _collect(secrets, [text]) == expected


@pytest.mark.parametrize("name", list(_CHUNKING_CASES))
def test_every_chunking_gives_the_output_of_one_read(name: str) -> None:
    secrets, texts = _CHUNKING_CASES[name]
    forms = [secret.encode() for secret in secrets]
    for text, _expected in texts:
        single = _collect(secrets, [text])
        assert not any(form in single for form in forms)
        for chunks in _chunkings(text):
            assert b"".join(chunks) == text
            out = _collect(secrets, chunks)
            assert out == single, [len(chunk) for chunk in chunks]


@pytest.mark.parametrize("name", list(_CHUNKING_CASES))
def test_no_chunking_lets_a_secret_through_when_long_lines_are_flushed_early(name: str) -> None:
    secrets, texts = _CHUNKING_CASES[name]
    forms = [secret.encode() for secret in secrets]
    for text, _expected in texts:
        if len(text) >= 1000:
            continue
        for chunks in _chunkings(text):
            out = _collect(secrets, chunks, max_line=8)
            assert not any(form in out for form in forms), [len(chunk) for chunk in chunks]


def test_the_carry_over_is_bounded_by_the_longest_secret_and_the_longest_line() -> None:
    out: list[bytes] = []
    redactor = OutputRedactor(["aaa", "0123456789"], out.append, max_line=32)
    for chunk in (b"a" * 500, b"x 012345678", b"https://" + b"u" * 500, b":/", b"y\n" * 50):
        redactor.feed(chunk)
        assert len(redactor._buffer) <= max(32, len("0123456789") + 1)
    redactor.close()
    assert b"".join(out) == (b"***x 012345678https://" + b"u" * 500 + b":/" + b"y\n" * 50)


def test_capped_sink_forwards_up_to_the_limit_then_says_so_once() -> None:
    out: list[bytes] = []
    sink = CappedSink(out.append, limit=10)
    sink(b"12345")
    sink(b"6789012345")
    sink(b"more")
    sink(b"")
    assert b"".join(out) == b"1234567890\n[log truncated: output exceeded 10 bytes]\n"
    exact: list[bytes] = []
    fits = CappedSink(exact.append, limit=4)
    fits(b"1234")
    assert b"".join(exact) == b"1234"
    fits(b"5")
    assert b"".join(exact) == b"1234\n[log truncated: output exceeded 4 bytes]\n"


def test_is_private_directory(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    assert is_private_directory(private)
    assert is_private_directory(private, forbidden=0o077)
    private.chmod(0o750)
    assert is_private_directory(private)
    assert not is_private_directory(private, forbidden=0o077)
    private.chmod(0o770)
    assert not is_private_directory(private)
    private.chmod(0o707)
    assert not is_private_directory(private)
    assert not is_private_directory(tmp_path / "missing")
    plain = tmp_path / "file"
    plain.write_text("x")
    assert not is_private_directory(plain)
    link = tmp_path / "link"
    link.symlink_to(private)
    assert not is_private_directory(link)


def test_is_private_directory_refuses_another_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setattr("lakana_worker.processes.os.geteuid", lambda: 4242)
    assert not is_private_directory(private)


def _modes(root: Path) -> dict[str, int]:
    """Record every mode under a tree, without following links.

    Parameters
    ----------
    root
        The tree.

    Returns
    -------
    dict
        Relative path to permission bits.
    """
    found = {".": stat.S_IMODE(root.lstat().st_mode)}
    for directory, dirs, files in os.walk(root):
        for name in (*dirs, *files):
            path = Path(directory, name)
            found[str(path.relative_to(root))] = stat.S_IMODE(path.lstat().st_mode)
    return found


def test_remove_private_tree_removes_a_plain_tree(tmp_path: Path) -> None:
    tree = tmp_path / "attempt"
    (tree / "home" / "deep").mkdir(parents=True)
    (tree / "home" / "deep" / "f").write_text("x")
    (tree / "result.json").write_text("{}")
    assert remove_private_tree(tree) is True
    assert not tree.exists()
    assert tmp_path.exists()


def test_remove_private_tree_on_a_missing_path_is_fine(tmp_path: Path) -> None:
    assert remove_private_tree(tmp_path / "missing") is True
    assert remove_private_tree(tmp_path / "missing" / "deeper") is True


def test_remove_private_tree_never_follows_a_root_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "keep.txt").write_text("keep")
    (outside / "sub").chmod(0o750)
    outside.chmod(0o755)
    before = _modes(outside)
    link = tmp_path / "code"
    link.symlink_to(outside)
    assert remove_private_tree(link) is True
    assert not link.is_symlink() and not link.exists()
    assert _modes(outside) == before
    assert (outside / "sub" / "keep.txt").read_text() == "keep"


def test_remove_private_tree_does_not_follow_a_nested_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "keep.txt").write_text("keep")
    outside.chmod(0o755)
    before = _modes(outside)
    tree = tmp_path / "attempt"
    (tree / "home").mkdir(parents=True)
    (tree / "home" / "dirlink").symlink_to(outside)
    (tree / "filelink").symlink_to(outside / "sub" / "keep.txt")
    (tree / "dangling").symlink_to(tmp_path / "nowhere")
    assert remove_private_tree(tree) is True
    assert not tree.exists()
    assert _modes(outside) == before
    assert (outside / "sub" / "keep.txt").read_text() == "keep"


@pytest.mark.parametrize("where", ["root", "sub", "nested"])
@pytest.mark.parametrize("mode", [0o000, 0o500, 0o100, 0o200])
def test_remove_private_tree_removes_directories_the_task_locked(
    tmp_path: Path, where: str, mode: int
) -> None:
    tree = tmp_path / "attempt"
    (tree / "sub" / "nested").mkdir(parents=True)
    (tree / "sub" / "nested" / "f").write_text("x")
    (tree / "sub" / "g").write_text("x")
    locked = {"root": tree, "sub": tree / "sub", "nested": tree / "sub" / "nested"}[where]
    locked.chmod(mode)
    if where == "nested":
        (tree / "sub").chmod(mode)
    try:
        assert remove_private_tree(tree) is True
    finally:
        if tree.exists():  # make pytest's own cleanup possible if the test fails
            for directory, _dirs, _files in os.walk(tree):
                os.chmod(directory, 0o700)
    assert not tree.exists()


def test_remove_private_tree_removes_special_files_and_locked_files(tmp_path: Path) -> None:
    tree = tmp_path / "attempt"
    tree.mkdir()
    os.mkfifo(tree / "pipe")
    (tree / "locked").write_text("x")
    (tree / "locked").chmod(0o000)
    assert remove_private_tree(tree) is True
    assert not tree.exists()


def test_remove_private_tree_removes_a_read_only_tree(tmp_path: Path) -> None:
    tree = tmp_path / "attempt"
    (tree / "code" / "src").mkdir(parents=True)
    (tree / "code" / "src" / "a.py").write_text("x")
    (tree / "code" / "src" / "a.py").chmod(0o444)
    for directory in (tree / "code" / "src", tree / "code"):
        directory.chmod(0o555)
    assert remove_private_tree(tree) is True
    assert not tree.exists()


def test_remove_private_tree_removes_a_plain_file_at_the_path(tmp_path: Path) -> None:
    leftover = tmp_path / "attempt"
    leftover.write_text("x")
    assert remove_private_tree(leftover) is True
    assert not leftover.exists()


def test_remove_private_tree_reports_what_it_cannot_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    tree = tmp_path / "attempt"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "f").write_text("x")
    real_unlink = os.unlink

    def unlink(path: str, *, dir_fd: int | None = None) -> None:
        if path == "f":
            raise PermissionError(1, "operation not permitted")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr("lakana_worker.processes.os.unlink", unlink)
    with caplog.at_level(logging.WARNING):
        assert remove_private_tree(tree) is False
    assert tree.exists()
    assert str(tree) in caplog.text


def test_remove_private_tree_refuses_a_path_without_a_name(tmp_path: Path) -> None:
    assert remove_private_tree(Path("/")) is False


def test_remove_private_tree_gives_up_on_a_chain_too_deep_for_its_descriptors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tree = tmp_path / "attempt"
    tree.mkdir()
    descriptor = os.open(tree, os.O_RDONLY)
    try:
        for _ in range(300):
            os.mkdir("d", dir_fd=descriptor)
            child = os.open("d", os.O_RDONLY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)
    with caplog.at_level(logging.WARNING):
        assert remove_private_tree(tree) is False
    assert tree.exists()
    assert str(tree) in caplog.text


def _fail_when(
    monkeypatch: pytest.MonkeyPatch,
    function: str,
    when: Callable[..., bool],
    error: OSError,
) -> None:
    """Make one `os` function, as seen by `lakana_worker.processes`, fail on chosen calls.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch.
    function
        The `os` function's name.
    when
        Receives the call's arguments; true means fail.
    error
        What to raise.
    """
    real = getattr(os, function)

    def wrapper(*args: object, **kwargs: object) -> object:
        if when(*args, **kwargs):
            raise error
        return real(*args, **kwargs)

    monkeypatch.setattr(f"lakana_worker.processes.os.{function}", wrapper)


def _tree_with(tmp_path: Path, *names: str) -> Path:
    """Build a small tree.

    Parameters
    ----------
    tmp_path
        Where to build it.
    *names
        Relative file paths to create.

    Returns
    -------
    Path
        The tree's root.
    """
    tree = tmp_path / "attempt"
    tree.mkdir()
    for name in names:
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).write_text("x")
    return tree


def test_a_directory_swapped_for_a_link_after_the_listing_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    before = _modes(outside)
    tree = _tree_with(tmp_path)
    (tree / "swapped").symlink_to(outside)
    real_lstat = os.lstat

    def lstat(path: str, *, dir_fd: int | None = None) -> os.stat_result:
        if path == "swapped":  # the racing task: it was a directory when listed
            return real_lstat(outside)
        return real_lstat(path, dir_fd=dir_fd)

    monkeypatch.setattr("lakana_worker.processes.os.lstat", lstat)
    assert remove_private_tree(tree) is False
    assert _modes(outside) == before
    assert (outside / "keep").read_text() == "keep"


def test_a_locked_directory_that_cannot_be_opened_by_path_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _tree_with(tmp_path, "sub/f")
    (tree / "sub").chmod(0)
    _fail_when(
        monkeypatch,
        "open",
        lambda *a, **k: bool(a[1] & getattr(os, "O_PATH", 0)),
        OSError(errno.EBADF, "bad"),
    )
    try:
        assert remove_private_tree(tree) is False
    finally:
        (tree / "sub").chmod(0o700)


def test_a_locked_root_that_cannot_be_opened_by_path_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _tree_with(tmp_path, "f")
    tree.chmod(0)
    _fail_when(
        monkeypatch,
        "open",
        lambda *a, **k: bool(a[1] & getattr(os, "O_PATH", 0)),
        OSError(errno.EBADF, "bad"),
    )
    try:
        assert remove_private_tree(tree) is False
    finally:
        tree.chmod(0o700)


def test_a_locked_entry_that_is_not_a_directory_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _tree_with(tmp_path, "pretender")
    (tree / "pretender").chmod(0)
    real_lstat = os.lstat

    def lstat(path: str, *, dir_fd: int | None = None) -> os.stat_result:
        if path == "pretender":  # swapped for a file after the listing
            return real_lstat(tree)
        return real_lstat(path, dir_fd=dir_fd)

    monkeypatch.setattr("lakana_worker.processes.os.lstat", lstat)
    _fail_when(
        monkeypatch,
        "open",
        lambda *a, **k: a[0] == "pretender" and not a[1] & getattr(os, "O_PATH", 0),
        PermissionError(errno.EACCES, "denied"),
    )
    assert remove_private_tree(tree) is False
    assert (tree / "pretender").exists()


def test_a_chmod_that_fails_on_a_locked_directory_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _tree_with(tmp_path, "sub/f")
    (tree / "sub").chmod(0)
    _fail_when(
        monkeypatch,
        "chmod",
        lambda path, *a, **k: str(path).startswith("/proc/self/fd/"),
        OSError(errno.EPERM, "denied"),
    )
    try:
        assert remove_private_tree(tree) is False
    finally:
        (tree / "sub").chmod(0o700)


def test_an_entry_that_vanishes_during_the_walk_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _tree_with(tmp_path, "gone")
    _fail_when(
        monkeypatch,
        "lstat",
        lambda path, **k: path == "gone",
        FileNotFoundError(errno.ENOENT, "gone"),
    )
    assert remove_private_tree(tree) is False


def test_a_subdirectory_that_cannot_be_listed_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _tree_with(tmp_path, "sub/f")
    calls: list[int] = []

    def when(*_args: object, **_kwargs: object) -> bool:
        calls.append(1)
        return len(calls) == 2  # the root's own chmod passes, the child's fails

    _fail_when(monkeypatch, "fchmod", when, OSError(errno.EIO, "io"))
    assert remove_private_tree(tree) is False


def test_an_unreadable_parent_is_reported(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    (parent / "attempt").mkdir(parents=True)
    parent.chmod(0)
    try:
        assert remove_private_tree(parent / "attempt") is False
    finally:
        parent.chmod(0o700)


def test_a_root_that_cannot_be_unlinked_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leftover = tmp_path / "attempt"
    leftover.write_text("x")
    _fail_when(monkeypatch, "unlink", lambda *a, **k: True, PermissionError(errno.EPERM, "no"))
    assert remove_private_tree(leftover) is False


def test_remove_private_tree_removes_a_sealed_input_copy_and_a_killed_download(
    tmp_path: Path,
) -> None:
    """Point 12: files 0400 and directories 0500 below `inputs`, and a partial leftover."""
    tree = tmp_path / "attempt"
    sealed = tree / "inputs" / "model"
    (sealed / "weights" / "deep").mkdir(parents=True)
    (sealed / "weights" / "deep" / "w.bin").write_text("x")
    (sealed / "config.json").write_text("{}")
    leftover = tree / "inputs" / ".model.lakana-partial-ab12cd"
    (leftover / "half").mkdir(parents=True)
    (leftover / "half" / "part").write_text("x")
    (tree / "output").mkdir(mode=0o700)
    (tree / "output" / "m.bin").write_text("x")
    for directory, _dirs, files in os.walk(tree, topdown=False):
        for name in files:
            os.chmod(os.path.join(directory, name), 0o400)
        os.chmod(directory, 0o500)
    try:
        assert remove_private_tree(tree) is True
    finally:
        if tree.exists():  # make pytest's own cleanup possible if the test fails
            for directory, _dirs, _files in os.walk(tree):
                os.chmod(directory, 0o700)
    assert not tree.exists()
