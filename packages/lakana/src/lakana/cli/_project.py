"""Find the project around the working directory: `lakana.yaml`, its `repo:`, dirty state.

`lakana` cannot import `lakana_core`, so the alias-refusing YAML loader is
repeated here in miniature; the file is the user's own, but the same loader
keeps one rule for every `lakana.yaml` reader.
"""

from __future__ import annotations

import subprocess  # nosec B404 -- `git status` with fixed argv
from pathlib import Path

import yaml
from yaml.events import AliasEvent, CollectionStartEvent, ScalarEvent
from yaml.nodes import Node

from lakana.cli._state import CliError

CONFIG_FILE = "lakana.yaml"
_MAX_BYTES = 64 * 1024


class _NoAliasLoader(yaml.SafeLoader):
    """A `SafeLoader` that refuses anchors and aliases."""

    def compose_node(self, parent: Node | None, index: object) -> Node | None:
        """Compose one node, refusing anchors and aliases.

        Parameters
        ----------
        parent
            The parent node.
        index
            Position within the parent.

        Returns
        -------
        yaml.nodes.Node or None
            The node.

        Raises
        ------
        yaml.YAMLError
            At the first anchor or alias.
        """
        event = self.peek_event()
        anchored = isinstance(event, ScalarEvent | CollectionStartEvent) and event.anchor
        if isinstance(event, AliasEvent) or anchored:
            raise yaml.YAMLError("anchors and aliases are not allowed")
        return super().compose_node(parent, index)


def load_project_yaml(text: str) -> object:
    """Parse `lakana.yaml` text.

    Parameters
    ----------
    text
        The file's content.

    Returns
    -------
    object
        The loaded document.

    Raises
    ------
    CliError
        If it is too large, malformed or uses an anchor or alias (exit 2).
    """
    if len(text.encode()) > _MAX_BYTES:
        raise CliError("lakana.yaml is larger than 64 KiB")
    try:
        return yaml.load(text, Loader=_NoAliasLoader)  # nosec B506 -- SafeLoader subclass
    except yaml.YAMLError as exc:
        raise CliError(f"lakana.yaml is not valid: {exc}") from exc


def _read_capped(path: Path) -> str:
    """Read a file as UTF-8 without reading more than the size cap plus one byte.

    Parameters
    ----------
    path
        The file.

    Returns
    -------
    str
        Its text.

    Raises
    ------
    CliError
        If it cannot be read, is larger than the cap, or is not UTF-8 (exit 2).
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_BYTES + 1)
    except OSError as exc:
        raise CliError(f"Can't read {path}: {exc}") from exc
    if len(raw) > _MAX_BYTES:
        raise CliError("lakana.yaml is larger than 64 KiB")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CliError("lakana.yaml is not valid UTF-8") from exc


def find_project_root(start: Path) -> Path | None:
    """Find the directory holding `lakana.yaml`, climbing only inside a git repository.

    Inside a repository the search runs from `start` up to the git root, as git
    does. Outside any repository only `start` itself is checked, so a stray
    `lakana.yaml` in a parent directory never names the repository. A symlinked
    `lakana.yaml` is ignored, as the server's discovery refuses it.

    Parameters
    ----------
    start
        Where to begin (usually the working directory).

    Returns
    -------
    Path or None
        The project root, or `None`.
    """
    here = start.resolve()
    chain = (here, *here.parents)
    git_root = next((d for d in chain if (d / ".git").exists()), None)
    searched = (here,) if git_root is None else chain[: chain.index(git_root) + 1]
    for directory in searched:
        candidate = directory / CONFIG_FILE
        if candidate.is_file() and not candidate.is_symlink():
            return directory
    return None


def project_repo(start: Path) -> str | None:
    """Return `lakana.yaml`'s `repo:` for the project around `start`.

    Parameters
    ----------
    start
        Where to begin.

    Returns
    -------
    str or None
        The repository name, or `None` with no project or no `repo:`.
    """
    root = find_project_root(start)
    if root is None:
        return None
    data = load_project_yaml(_read_capped(root / CONFIG_FILE))
    repo = data.get("repo") if isinstance(data, dict) else None
    return repo if isinstance(repo, str) else None


def is_dirty(root: Path) -> bool:
    """Report whether `root` is a git work tree with uncommitted changes.

    Parameters
    ----------
    root
        The project root.

    Returns
    -------
    bool
        `False` outside git or if git is unavailable.
    """
    try:
        result = subprocess.run(  # nosec B603 B607 -- fixed argv
            ["git", "-c", "core.fsmonitor=false", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0 and bool(result.stdout.strip())
