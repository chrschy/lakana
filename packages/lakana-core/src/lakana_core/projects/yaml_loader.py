"""YAML loading for repository content: SafeLoader semantics, no anchors, no aliases.

Refusing anchors and aliases makes alias expansion ("billion laughs")
impossible, so the byte limit checked before parsing bounds memory. Duplicate
keys and ``<<`` merge keys are refused too: PyYAML would otherwise keep the
last of two equal keys and splice merged keys in silently, so what a reviewer
reads would not be what the platform applies.
"""

from __future__ import annotations

from collections.abc import Sequence

import yaml
from yaml.events import AliasEvent, CollectionStartEvent, ScalarEvent
from yaml.nodes import MappingNode, Node

from lakana_core.errors import ValidationError


class YamlError(ValidationError):
    """A YAML document that is malformed, too large or uses anchors/aliases.

    Parameters
    ----------
    message
        What is wrong.
    line
        1-based line within the document, if known.
    """

    def __init__(self, message: str, line: int | None = None) -> None:
        self.line = line
        super().__init__(message)


_MAX_DEPTH = 64
"""Deepest nesting accepted; stops recursion far below Python's limit."""


class _AliasRefused(yaml.MarkedYAMLError):
    """Raised by `_NoAliasLoader` at the first anchor or alias."""


class _TooDeep(yaml.MarkedYAMLError):
    """Raised by `_NoAliasLoader` when nesting exceeds `_MAX_DEPTH`."""


class _AmbiguousMapping(yaml.MarkedYAMLError):
    """Raised by `_NoAliasLoader` at a duplicate key or a merge key."""


_MERGE_TAG = "tag:yaml.org,2002:merge"


class _NoAliasLoader(yaml.SafeLoader):
    """A `SafeLoader` that refuses anchors, aliases, merge keys, duplicate keys and deep nesting."""

    _depth = 0

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[object, object]:
        """Refuse a merge key or a repeated key, then build the mapping.

        Parameters
        ----------
        node
            The mapping node.
        deep
            As PyYAML passes it.

        Returns
        -------
        dict
            The constructed mapping.

        Raises
        ------
        _AmbiguousMapping
            At the first merge key or the second occurrence of a key.
        """
        seen: set[object] = set()
        for key_node, _ in node.value:
            if key_node.tag == _MERGE_TAG:
                raise _AmbiguousMapping(
                    problem="merge keys (<<) are not allowed", problem_mark=key_node.start_mark
                )
            try:
                key = self.construct_object(key_node, deep=True)
                if key in seen:
                    raise _AmbiguousMapping(
                        problem=f"duplicate key {key!r}", problem_mark=key_node.start_mark
                    )
                seen.add(key)
            except TypeError:  # unhashable key: PyYAML's own refusal below names it
                break
        return super().construct_mapping(node, deep=deep)

    def compose_node(self, parent: Node | None, index: object) -> Node | None:
        """Compose one node, refusing it if it carries an anchor or is an alias.

        Parameters
        ----------
        parent
            The parent node, as PyYAML passes it.
        index
            The key or position within the parent, as PyYAML passes it.

        Returns
        -------
        yaml.nodes.Node or None
            The composed node.

        Raises
        ------
        _AliasRefused
            At the first anchor or alias.
        _TooDeep
            When nesting exceeds `_MAX_DEPTH`.
        """
        event = self.peek_event()
        anchored = isinstance(event, ScalarEvent | CollectionStartEvent) and event.anchor
        if isinstance(event, AliasEvent) or anchored:
            raise _AliasRefused(
                problem="anchors and aliases are not allowed", problem_mark=event.start_mark
            )
        if self._depth >= _MAX_DEPTH:
            raise _TooDeep(problem="nesting is too deep", problem_mark=event.start_mark)
        self._depth += 1
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


def load_yaml(text: str, *, what: str, max_bytes: int) -> object:
    """Parse one YAML document from untrusted repository content.

    Parameters
    ----------
    text
        The document.
    what
        Names the document in error messages, e.g. ``"lakana.yaml"``.
    max_bytes
        Largest accepted UTF-8 size.

    Returns
    -------
    object
        The loaded value; `None` for an empty document.

    Raises
    ------
    YamlError
        If the document is too large, malformed, nested too deeply, has a
        value PyYAML cannot construct, uses an anchor, alias or merge key, or
        repeats a key.
    """
    size = len(text.encode("utf-8"))
    if size > max_bytes:
        raise YamlError(f"{what} is larger than {max_bytes // 1024} KiB.")
    try:
        return yaml.load(text, Loader=_NoAliasLoader)  # nosec B506 -- a SafeLoader subclass
    except _AliasRefused as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark is not None else None
        raise YamlError(f"{what}: anchors and aliases are not allowed.", line) from exc
    except _AmbiguousMapping as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark is not None else None
        raise YamlError(f"{what}: {exc.problem}.", line) from exc
    except _TooDeep as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark is not None else None
        raise YamlError(f"{what}: nesting is deeper than {_MAX_DEPTH} levels.", line) from exc
    except yaml.MarkedYAMLError as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark is not None else None
        raise YamlError(f"{what} is not valid YAML: {exc.problem}", line) from exc
    except (ValueError, TypeError, OverflowError) as exc:
        raise YamlError(f"{what} has an invalid value: {exc}") from exc
    except yaml.YAMLError as exc:  # pragma: no cover -- every PyYAML parse error is marked
        raise YamlError(f"{what} is not valid YAML.") from exc


def _find_key(node: Node | None, name: object) -> tuple[Node, Node] | None:
    """Find a key in a mapping node.

    Parameters
    ----------
    node
        Any node of a composed document, or `None` for an empty one.
    name
        The key to look for.

    Returns
    -------
    tuple of Node or None
        The key node and its value node; `None` if `node` is not a mapping or has no such key.
    """
    if not isinstance(node, MappingNode):
        return None
    for key_node, value_node in node.value:
        if getattr(key_node, "value", None) == name:
            return key_node, value_node
    return None


def key_line(text: str, path: Sequence[object]) -> int | None:
    """Find the line of a key inside a document, to point an issue at it.

    Parameters
    ----------
    text
        The document, already accepted by `load_yaml`.
    path
        The keys from the top: ``("jobs", "daily", "schedule", "cron")``. A part that is not a
        key of the mapping at that point ends the walk.

    Returns
    -------
    int or None
        The 1-based line of the last key reached; `None` if not even the first key exists or the
        document cannot be read.
    """
    try:
        node = yaml.compose(text, Loader=_NoAliasLoader)
    except yaml.YAMLError:
        return None
    line: int | None = None
    for part in path:
        found = _find_key(node, part)
        if found is None:
            break
        key_node, node = found
        line = key_node.start_mark.line + 1
    return line
