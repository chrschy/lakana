"""Subprocess helpers shared by the venv build and the Python task runner."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import stat
import subprocess  # nosec B404 -- only signals a process the caller started; no shell
from collections.abc import Callable, Sequence
from pathlib import Path

from lakana_core.identifiers import shown

logger = logging.getLogger(__name__)
_USERINFO = re.compile(rb"(://)[^/@\s]+@")
_OPEN_USERINFO = re.compile(rb"[/@\s]")


def _merged(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Join spans that overlap.

    Parameters
    ----------
    spans
        Half-open ``(start, end)`` pairs, in any order.

    Returns
    -------
    list of tuple
        The spans in order, each run of overlapping ones as one span. Two spans
        that only touch stay two.
    """
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


class OutputRedactor:
    """Remove secrets from a process's output before it reaches a log.

    What is redacted depends only on the output, never on where one read
    ended and the next began. Every occurrence of every secret is a span,
    occurrences that overlap each other included; with those bytes masked,
    the userinfo of every ``://userinfo@`` is a span too. Spans that overlap
    are joined, and each joined span is replaced by one ``***``.

    Output is forwarded when a line ends, or when more than `max_line` bytes
    wait for the end of their line. The redactor keeps the unredacted end of
    the output for as long as its fate is open. Open are: bytes at the end
    that are the start of a secret, an unfinished ``://userinfo`` that has not
    reached its ``@`` yet (unless it is longer than `max_line`: then it is
    forwarded as it is), and a ``:`` or ``:/`` at the end. When a finished
    span reaches into the kept bytes, its ``***`` is forwarded and the
    redactor remembers how many of the kept bytes it covers, so they stay
    masked and are not announced a second time.

    So at most ``max(max_line, longest secret + 1, 2)`` bytes are kept between
    two reads, whatever the output is.

    Parameters
    ----------
    secrets
        Values to replace with ``***``; empty strings are ignored.
    sink
        Receives the redacted bytes.
    max_line
        Most bytes kept while waiting for the end of a line or of a ``://userinfo``.
    """

    def __init__(
        self, secrets: Sequence[str], sink: Callable[[bytes], None], max_line: int = 65536
    ) -> None:
        self._secrets = sorted(
            {secret.encode("utf-8") for secret in secrets if secret}, key=len, reverse=True
        )
        self._sink = sink
        self._max_line = max_line
        self._buffer = bytearray()
        self._covered = 0

    def _secret_spans(self, data: bytes) -> list[tuple[int, int]]:
        """Find every complete occurrence of every secret.

        Parameters
        ----------
        data
            The kept bytes.

        Returns
        -------
        list of tuple
            The occurrences, joined where they overlap. The bytes an earlier
            call already redacted come first, as a span that starts at -1.
        """
        spans = [(-1, self._covered)] if self._covered else []
        for secret in self._secrets:
            at = data.find(secret)
            while at >= 0:
                spans.append((at, at + len(secret)))
                at = data.find(secret, at + 1)
        return _merged(spans)

    def _open_secret(self, data: bytes) -> int:
        """Find where the end of the output may be the start of a secret.

        Parameters
        ----------
        data
            The kept bytes.

        Returns
        -------
        int
            The first position from which the rest of `data` is the start of
            a secret, but not all of it; the length of `data` if there is none.
        """
        view = memoryview(data)
        earliest = len(data)
        for secret in self._secrets:
            at = data.find(secret[:1], max(len(data) - len(secret) + 1, 0))
            while 0 <= at < earliest:
                if secret.startswith(view[at:]):
                    earliest = at
                at = data.find(secret[:1], at + 1)
        return earliest

    def _open_userinfo(self, masked: bytearray, decided: int) -> int:
        """Find where an unfinished ``://userinfo`` may begin.

        Parameters
        ----------
        masked
            The kept bytes, with every secret masked.
        decided
            The bytes before this position cannot become part of a secret any more.

        Returns
        -------
        int
            The position of a ``:`` or ``:/`` that ends the decided bytes, or
            of a ``://`` whose userinfo has not ended and is not longer than
            `max_line`, whichever comes first; `decided` if there is neither.
        """
        start = decided
        if masked.endswith(b":/", 0, decided):
            start = decided - 2
        elif masked.endswith(b":", 0, decided):
            start = decided - 1
        scheme = masked.rfind(b"://", 0, decided)
        if (
            scheme >= 0
            and len(masked) - scheme <= self._max_line
            and _OPEN_USERINFO.search(masked, scheme + 3, decided) is None
        ):
            start = scheme
        return start

    def _flush(self, final: bool) -> None:
        """Forward what can no longer change, redacted, and keep the rest.

        Parameters
        ----------
        final
            Whether the output has ended, so that nothing is kept.
        """
        data = bytes(self._buffer)
        spans = self._secret_spans(data)
        masked = bytearray(data)
        for start, end in spans:
            masked[max(start, 0) : end] = b"*" * (end - max(start, 0))
        decided = len(data) if final else self._open_secret(data)
        spans += [
            (match.start() + 3, match.end() - 1) for match in _USERINFO.finditer(masked, 0, decided)
        ]
        cut = len(data) if final else self._open_userinfo(masked, decided)
        line = data.rfind(b"\n") + 1
        if not final and len(data) - line <= self._max_line:
            cut = min(cut, line)
        out = bytearray()
        position = covered = 0
        for start, end in _merged(spans):
            if start >= cut:
                break
            out += data[position : max(start, position)]
            if start >= 0:
                out += b"***"
            position = min(end, cut)
            covered = end - position
        out += data[position:cut]
        del self._buffer[:cut]
        self._covered = covered
        if out:
            self._sink(bytes(out))

    def feed(self, chunk: bytes) -> None:
        """Take one read's worth of output.

        Parameters
        ----------
        chunk
            The bytes read.
        """
        self._buffer += chunk
        if b"\n" in chunk or len(self._buffer) > self._max_line:
            self._flush(final=False)

    def close(self) -> None:
        """Forward whatever is still kept; safe to call twice."""
        self._flush(final=True)


class CappedSink:
    """Forward output up to a byte limit, then say once that it was truncated.

    Output past the limit is still read by the caller and dropped here, so a
    process that floods its output never blocks on a full pipe. One instance
    wrapped around the log sink caps everything that reaches it: the venv
    build and the task process alike.

    Parameters
    ----------
    sink
        Receives the bytes that fit.
    limit
        Most bytes forwarded.
    """

    def __init__(self, sink: Callable[[bytes], None], limit: int) -> None:
        self._sink = sink
        self._limit = limit
        self._seen = 0
        self._truncated = False

    def __call__(self, data: bytes) -> None:
        """Take one piece of output.

        Parameters
        ----------
        data
            The bytes.
        """
        if self._truncated or not data:
            return
        room = self._limit - self._seen
        if len(data) <= room:
            self._seen += len(data)
            self._sink(data)
            return
        self._truncated = True
        self._seen = self._limit
        self._sink(
            data[:room] + f"\n[log truncated: output exceeded {self._limit} bytes]\n".encode()
        )


def is_private_directory(path: Path, forbidden: int = 0o022) -> bool:
    """Say whether a path is a real directory the worker's user owns and others cannot touch.

    Parameters
    ----------
    path
        The path; a symlink is not a directory here, and a missing path is not private.
    forbidden
        Permission bits that must not be set: ``0o022`` (group or other may write)
        for a shared cache, ``0o077`` (no group or other access at all) for a
        directory that holds a task's secrets.

    Returns
    -------
    bool
        Whether it is a directory, owned by the effective uid, with none of the
        `forbidden` bits.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & forbidden
    )


_MAX_REMOVE_DEPTH = 128
"""Deepest directory nesting `remove_private_tree` follows; each level holds one descriptor."""


_CHMOD_AT_WITHOUT_FOLLOWING = (
    os.chmod in os.supports_dir_fd and os.chmod in os.supports_follow_symlinks
)
"""Whether `os.chmod` works relative to a directory descriptor and can refuse to follow a link.

True on macOS (``fchmodat`` with ``AT_SYMLINK_NOFOLLOW``); Linux's Python reports False, and
Linux restores access through ``O_PATH`` instead.
"""


def _is_own_directory(info: os.stat_result) -> bool:
    """Say whether a status describes a real directory of the worker's own user.

    Parameters
    ----------
    info
        The result of an `os.lstat` or `os.fstat`.

    Returns
    -------
    bool
        Whether it is a directory owned by the effective uid.
    """
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()


def restore_directory_access(name: str, parent_fd: int) -> bool:
    """Set a directory the task locked back to mode 0700, never through a symlink.

    User code can set its own directories to mode 000, which keeps their owner, the
    worker's user, from opening them. Where the platform has ``O_PATH`` (Linux), the
    directory is opened by ``O_PATH`` and ``O_NOFOLLOW`` and chmod-ed through that
    descriptor, so a rename that swaps in a link cannot redirect the chmod. Where it has
    not (macOS), the chmod is relative to `parent_fd` and does not follow a link. Where
    neither exists, nothing is chmod-ed by path: the directory is left and the reason
    logged.

    Parameters
    ----------
    name
        The directory's name in its parent.
    parent_fd
        A descriptor of the parent directory.

    Returns
    -------
    bool
        Whether `name` was a directory of the worker's own user and is mode 0700 now;
        `False` if it is anything else or the restore failed.

    Raises
    ------
    FileNotFoundError
        If `name` is gone.
    """
    if hasattr(os, "O_PATH"):
        return _restore_through_a_path_descriptor(name, parent_fd)
    if not _CHMOD_AT_WITHOUT_FOLLOWING:
        logger.warning(
            "The locked directory %s cannot be made accessible safely on this platform: it has "
            "neither O_PATH nor a chmod that refuses to follow a symlink, so it is left in "
            "place. Restore its access by hand (chmod u+rwx) and remove it.",
            shown(name),
        )
        return False
    return _restore_relative_to_the_parent(name, parent_fd)


def _restore_through_a_path_descriptor(name: str, parent_fd: int) -> bool:
    """Chmod a locked directory to 0700 through an ``O_PATH`` descriptor (Linux).

    Parameters
    ----------
    name
        The directory's name in its parent.
    parent_fd
        A descriptor of the parent directory.

    Returns
    -------
    bool
        Whether the directory, a real one of the worker's own user, is mode 0700 now.

    Raises
    ------
    FileNotFoundError
        If `name` is gone.
    """
    try:
        handle = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError:
        return False
    try:
        if not _is_own_directory(os.fstat(handle)):
            return False
        # The descriptor names the inode that was checked, whatever the name points to now.
        os.chmod(f"/proc/self/fd/{handle}", 0o700)
    except OSError:
        return False
    finally:
        os.close(handle)
    return True


def _restore_relative_to_the_parent(name: str, parent_fd: int) -> bool:
    """Chmod a locked directory to 0700 by name below its parent, not following a link (macOS).

    A mode-000 directory cannot be opened by its owner without ``O_PATH``, so there is
    no descriptor of it to chmod through. This is race-safe enough: `parent_fd` pins the
    parent, so a rename of a directory above cannot redirect the call, and
    ``follow_symlinks=False`` (``AT_SYMLINK_NOFOLLOW``) means a symlink swapped in after
    the `lstat` has at most its own mode changed, never its target's. What remains is a
    swap for another object of the worker's own user (a non-root caller cannot chmod
    anyone else's, and root never gets here: its first open ignores the mode); mode 0700
    grants nobody else anything, and the caller's reopen with ``O_DIRECTORY`` and
    ``O_NOFOLLOW`` refuses whatever is not a directory.

    Parameters
    ----------
    name
        The directory's name in its parent.
    parent_fd
        A descriptor of the parent directory.

    Returns
    -------
    bool
        Whether the directory, a real one of the worker's own user, is mode 0700 now.

    Raises
    ------
    FileNotFoundError
        If `name` is gone.
    """
    try:
        if not _is_own_directory(os.lstat(name, dir_fd=parent_fd)):
            return False
        os.chmod(name, 0o700, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise
    except (OSError, ValueError, NotImplementedError):
        # Linux's emulation refuses a symlink with a ValueError rather than an OSError.
        return False
    return True


def _open_directory(name: str, parent_fd: int) -> int | None:
    """Open a real directory below `parent_fd`, making it readable first if its mode forbids that.

    Never follows a symlink: the open uses ``O_NOFOLLOW``, and a locked directory is
    made accessible by `restore_directory_access`, which never chmods through a link.

    Parameters
    ----------
    name
        The entry's name in the parent.
    parent_fd
        A descriptor of the parent directory.

    Returns
    -------
    int or None
        A descriptor, or `None` if the entry is not a real directory or cannot be opened.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except PermissionError:
        pass
    except OSError:
        return None
    try:
        if not restore_directory_access(name, parent_fd):
            return None
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError:
        return None


def _clear_directory(root_fd: int) -> bool:
    """Remove everything below an open directory, never following a link.

    Iterative, so a directory chain deeper than the interpreter's recursion
    limit cannot defeat it; each directory is made traversable and writable
    before it is listed.

    Parameters
    ----------
    root_fd
        A descriptor of a real directory.

    Returns
    -------
    bool
        Whether everything below it was removed.
    """
    complete = True
    os.fchmod(root_fd, 0o700)
    stack: list[tuple[int, str, list[str]]] = [(root_fd, "", os.listdir(root_fd))]
    while stack:
        fd, entry, pending = stack[-1]
        if not pending:
            stack.pop()
            if stack:
                os.close(fd)
                try:
                    os.rmdir(entry, dir_fd=stack[-1][0])
                except OSError:
                    complete = False
            continue
        name = pending.pop()
        try:
            is_directory = stat.S_ISDIR(os.lstat(name, dir_fd=fd).st_mode)
        except OSError:
            complete = False
            continue
        if not is_directory:
            try:
                os.unlink(name, dir_fd=fd)
            except OSError:
                complete = False
            continue
        child = None if len(stack) >= _MAX_REMOVE_DEPTH else _open_directory(name, fd)
        if child is None:
            complete = False
            continue
        try:
            os.fchmod(child, 0o700)
            stack.append((child, name, os.listdir(child)))
        except OSError:
            os.close(child)
            complete = False
    return complete


def remove_private_tree(path: Path) -> bool:
    """Delete an attempt's directory, which the task owned and may have booby-trapped.

    Safe against everything user code can leave behind: a symlink at `path`
    itself is unlinked, never followed; no symlink below is followed;
    directories the task set to mode 000 or read-only are made traversable
    before they are listed; FIFOs and locked files are unlinked. Every step is
    relative to a directory descriptor, so a rename that races it cannot redirect
    it. A tree nested deeper than 128 levels is left in place (and reported).

    Parameters
    ----------
    path
        The directory (or link, or file) to remove; missing is fine.

    Returns
    -------
    bool
        Whether `path` is gone. A warning naming only `path` is logged when it is not.
    """
    if path.name in ("", ".", ".."):
        logger.warning("refusing to remove %s", path)
        return False
    removed = False
    try:
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except FileNotFoundError:
        return True
    except OSError:
        parent_fd = None
    if parent_fd is not None:
        try:
            removed = _remove_entry(path.name, parent_fd)
        except OSError:
            removed = False
        finally:
            os.close(parent_fd)
    if not removed:
        logger.warning("could not remove %s completely", path)
    return removed


def _remove_entry(name: str, parent_fd: int) -> bool:
    """Remove one entry below a directory descriptor.

    Parameters
    ----------
    name
        The entry's name.
    parent_fd
        A descriptor of its parent.

    Returns
    -------
    bool
        Whether the entry is gone.
    """
    try:
        info = os.lstat(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return True
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return True
    root = _open_directory(name, parent_fd)
    if root is None:
        return False
    try:
        complete = _clear_directory(root)
    finally:
        os.close(root)
    if complete:
        os.rmdir(name, dir_fd=parent_fd)
    return complete


def tree_size(path: Path) -> int:
    """Sum the sizes of the files under `path`, without following links.

    Parameters
    ----------
    path
        A directory (missing is fine).

    Returns
    -------
    int
        Bytes; files that vanish during the walk are skipped.
    """
    total = 0
    for directory, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(FileNotFoundError):
                total += os.lstat(os.path.join(directory, name)).st_size
    return total


def signal_group(process: subprocess.Popen[bytes], signum: int) -> bool:
    """Send a signal to a process and everything it started.

    The process leads its own session (`start_new_session=True`), so its
    process group id is its pid. Does nothing once the leader has been
    reaped, because its pid may then belong to an unrelated group.

    Parameters
    ----------
    process
        A process started with `start_new_session=True`.
    signum
        The signal.

    Returns
    -------
    bool
        Whether the group received the signal; `False` when the leader was already
        reaped or the group was already gone (or, on macOS, only exited processes).
    """
    if process.returncode is not None:
        return False
    try:
        os.killpg(process.pid, signum)
    except (ProcessLookupError, PermissionError):
        # macOS refuses (EPERM) to signal a group whose members have all exited but
        # are not yet reaped; the group is gone either way.
        return False
    return True
