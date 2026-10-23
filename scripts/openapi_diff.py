"""Classify the differences between two OpenAPI documents as breaking or additive.

The platform REST API is a versioned contract (``docs/site/reference/rest-api.md``). Its
OpenAPI document is committed as ``docs/site/reference/openapi.json``; CI's ``conventional``
check compares the pull request's copy with the base branch's and, with
``--require-breaking-title``, fails when a breaking difference is not announced by a ``!`` in
the pull request title (``check_conventional.py`` then requires the ``BREAKING CHANGE:``
footer). An additive difference only updates the snapshot.

A difference is **breaking** when a call that worked, or a reading of an answer that worked,
may stop working:

- a route or a method removed;
- a parameter, request field, request body, response field, response or media type removed;
- a new required parameter, request field or request body, or one that becomes required;
- a request value that accepts less: a narrower type, a new or tighter limit or pattern, a new
  list of allowed values, a changed default, other fields refused;
- a response value that may now be missing, null or of another type;
- an allowed value (enum) removed, in a request or a response;
- a documented status code removed (a changed status code is a removed one and a new one).

Everything else that changes the contract is **additive**: a new route, method, optional
parameter or field, response field, status code or allowed value, a request value that accepts
more, a deprecation. Text (summaries, descriptions, titles, operation ids, tags) does not
count, and neither does the name of a component schema: references are resolved and compared
by shape, the alternatives of a union are paired by component name and then by kind (so
``Model`` and ``Model | None`` differ only by null, and a renamed component inside a union is
compared by its shape), an ``allOf`` is compared as the merge of its members (the stricter
value of each setting; members that cannot be combined are breaking), and a path
parameter is matched by its place in the path, not its name.

Usage: ``python3 scripts/openapi_diff.py BASE HEAD [--require-breaking-title]``; the title is
read from ``PR_TITLE``. A missing BASE (a branch that predates the snapshot) is not an error.
The script uses only the standard library, so CI runs it without installing the project.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BREAKING_TITLE = re.compile(r"^[a-z]+(?:\([^)]*\))?!:")
"""A title marked breaking; the same rule as ``check_conventional.py``'s."""

CONTRACT_URL = "https://lakana-labs.github.io/lakana/reference/rest-api/"
REGENERATE = "uv run python scripts/generate_reference.py"
_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_PATH_PARAMETER = re.compile(r"\{([^}]*)\}")
_JSON = "application/json"
_MAXIMA = ("maxLength", "maximum", "exclusiveMaximum", "maxItems", "maxProperties")
_MINIMA = ("minLength", "minimum", "exclusiveMinimum", "minItems", "minProperties")
_ANY = "any"
_CONFLICTS = "x-merge-conflicts"
"""Where the merge of an ``allOf`` lists what it could not combine."""
_CONFLICTING = ("type", "format", "pattern", "anyOf", "oneOf", "multipleOf")
"""Settings that cannot be combined by taking the stricter value; different ones conflict."""

Schema = dict[str, Any]
_Alternative = tuple[str | None, str, Schema]
"""One alternative of a union: its component name (or ``None``), its kind, its schema."""


@dataclass(frozen=True)
class Change:
    """One difference between two OpenAPI documents.

    Parameters
    ----------
    breaking
        Whether an existing client may stop working.
    where
        The operation and the place in it, such as ``GET /api/v1/x: request body `a```.
    what
        What changed, such as ``removed``.
    """

    breaking: bool
    where: str
    what: str

    def __str__(self) -> str:
        """Render the change for a reader.

        Returns
        -------
        str
            ``where: what``.
        """
        return f"{self.where}: {self.what}"


def _label(kind: str) -> str:
    """Name a kind of value for a reader.

    Parameters
    ----------
    kind
        A kind from ``_kind`` or a component name.

    Returns
    -------
    str
        The kind, with ``any`` spelled ``any value``.
    """
    return "any value" if kind == _ANY else kind


def _kind(schema: Schema) -> str:
    """Name the kind of value a schema without alternatives describes.

    Parameters
    ----------
    schema
        A resolved schema with at most one type.

    Returns
    -------
    str
        The JSON type, ``string (<format>)`` for a string with a format, ``object`` for a schema
        with properties, ``array`` for one with items, ``any`` otherwise.
    """
    kind = schema.get("type")
    if kind == "string" and "format" in schema:
        return f"string ({schema['format']})"
    if isinstance(kind, str):
        return kind
    if "properties" in schema or "additionalProperties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    return _ANY


def _covers(wide: str, narrow: str) -> bool:
    """Tell whether every value of one kind is also a value of another.

    Parameters
    ----------
    wide
        The kind that may be wider.
    narrow
        The kind that may be narrower.

    Returns
    -------
    bool
        True when every ``narrow`` value is a ``wide`` value.
    """
    return (
        wide in (narrow, _ANY)
        or (wide == "number" and narrow == "integer")
        or (wide == "string" and narrow.startswith("string ("))
    )


def _covers_alternative(wide: str, narrow: str) -> bool:
    """Tell whether one alternative of a union takes every value of another one left unpaired.

    Parameters
    ----------
    wide
        The kind of the alternative that may be wider.
    narrow
        The kind of the unpaired alternative.

    Returns
    -------
    bool
        As `_covers`, except that an unpaired object or array is covered only by ``any``: it was
        left over because no alternative of its kind was free to compare it with.
    """
    if narrow in ("object", "array"):
        return wide == _ANY
    return _covers(wide, narrow)


def _allowed(schema: Schema) -> list[Any] | None:
    """List the values a schema allows, if it lists them.

    Parameters
    ----------
    schema
        A resolved schema.

    Returns
    -------
    list or None
        The ``enum`` values, the ``const`` as one value, or ``None`` when any value is allowed.
    """
    if "enum" in schema:
        return list(schema["enum"])
    if "const" in schema:
        return [schema["const"]]
    return None


def _is_number(value: object) -> bool:
    """Tell whether a value is a number (a boolean is not).

    Parameters
    ----------
    value
        Any value.

    Returns
    -------
    bool
        True for an int or a float.
    """
    return isinstance(value, int | float) and not isinstance(value, bool)


def _shown(value: object) -> str:
    """Show a limit or a default in a message.

    Parameters
    ----------
    value
        The value, or ``None`` when there is none.

    Returns
    -------
    str
        ``none``, or the value as JSON (strings unquoted).
    """
    if value is None:
        return "none"
    return value if isinstance(value, str) else json.dumps(value)


def _field(where: str, name: str) -> str:
    """Name a field inside a place.

    Parameters
    ----------
    where
        The place, possibly already naming a field (ending in a backtick).
    name
        The field's name.

    Returns
    -------
    str
        ``where `name``` or, inside a field, ``where `outer.name```.
    """
    if where.endswith("`"):
        return f"{where[:-1]}.{name}`"
    return f"{where} `{name}`"


def _normalized(path: str) -> str:
    """Remove the names of a path's parameters.

    Parameters
    ----------
    path
        A path template such as ``/x/{name}``.

    Returns
    -------
    str
        The template with every parameter written ``{}``.
    """
    return _PATH_PARAMETER.sub("{}", path)


class _Comparison:
    """Compare two OpenAPI documents and collect the changes.

    Parameters
    ----------
    base
        The earlier document.
    head
        The later document.
    """

    def __init__(self, base: Mapping[str, Any], head: Mapping[str, Any]) -> None:
        self.base = base
        self.head = head
        self.changes: list[Change] = []
        self._seen: set[tuple[str, str, bool]] = set()
        self._base_path = {key: path for key, _, path, _ in self._operations(base)}

    def _add(self, breaking: bool, where: str, what: str) -> None:
        """Record one change.

        Parameters
        ----------
        breaking
            Whether it is breaking.
        where
            The place.
        what
            What changed.
        """
        self.changes.append(Change(breaking, where, what))

    @staticmethod
    def _resolve(doc: Mapping[str, Any], item: object) -> tuple[Any, str | None]:
        """Follow ``$ref`` until an object without one.

        Parameters
        ----------
        doc
            The document the reference points into.
        item
            A schema, parameter, request body or response, possibly a reference.

        Returns
        -------
        tuple
            The resolved object and the last component name followed (``None`` without one).
        """
        name = None
        while isinstance(item, dict) and "$ref" in item:
            target: Any = doc
            for part in item["$ref"].removeprefix("#/").split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            name = item["$ref"].rsplit("/", 1)[-1]
            item = target
        return item, name

    def _unwrap(self, doc: Mapping[str, Any], schema: object) -> tuple[Schema, str | None]:
        """Resolve a schema and unwrap an ``allOf`` of one schema.

        Parameters
        ----------
        doc
            The document the schema belongs to.
        schema
            The schema.

        Returns
        -------
        tuple
            The schema and its component name. An ``allOf`` of one schema is that schema, of
            several their merge (properties and required fields of all members); the wrapper's
            own keys win. The boolean schema ``true`` is ``{}``.
        """
        resolved, name = self._resolve(doc, schema)
        if not isinstance(resolved, dict):
            return {}, name
        members = resolved.get("allOf")
        if not isinstance(members, list) or not members:
            return resolved, name
        outer = {key: value for key, value in resolved.items() if key != "allOf"}
        if len(members) == 1:
            inner, inner_name = self._unwrap(doc, members[0])
            return self._merge(doc, inner, outer), inner_name or name
        merged: Schema = {}
        for member in members:
            inner, _ = self._unwrap(doc, member)
            merged = self._merge(doc, merged, inner)
        return self._merge(doc, merged, outer), name

    def _merge(self, doc: Mapping[str, Any], first: Schema, second: Schema) -> Schema:
        """Combine two schemas that must both hold, taking the stricter value of each setting.

        Parameters
        ----------
        doc
            The document the schemas belong to, to resolve references of shared properties.
        first
            A resolved schema, the earlier member.
        second
            A resolved schema, the later member.

        Returns
        -------
        dict
            The schema a value must match to match both: the smallest maximum, the largest
            minimum, the intersection of the allowed values, the union of the required fields,
            shared properties merged the same way. A setting only one member has is kept. What
            cannot be combined (different types, formats, patterns, no common value, different
            alternatives) is listed under ``_CONFLICTS`` and the first member's value stays.
        """
        merged: Schema = {**first}
        conflicts = list(first.get(_CONFLICTS, ())) + list(second.get(_CONFLICTS, ()))
        for key, value in second.items():
            if key == _CONFLICTS or key not in merged:
                if key != _CONFLICTS:
                    merged[key] = value
                continue
            mine = merged[key]
            if mine == value:
                continue
            if key in _MAXIMA and _is_number(mine) and _is_number(value):
                merged[key] = min(mine, value)
            elif key in _MINIMA and _is_number(mine) and _is_number(value):
                merged[key] = max(mine, value)
            elif key == "required" and isinstance(mine, list) and isinstance(value, list):
                merged[key] = list(dict.fromkeys([*mine, *value]))
            elif key == "properties" and isinstance(mine, dict) and isinstance(value, dict):
                merged[key] = self._merge_properties(doc, mine, value)
            elif key in ("items", "additionalProperties") and isinstance(mine, dict | bool):
                merged[key] = self._merge_sub(doc, mine, value)
            elif key == "nullable":
                merged[key] = bool(mine) and bool(value)
            elif key == "deprecated":
                merged[key] = bool(mine) or bool(value)
            elif key in _CONFLICTING:
                conflicts.append(f"{key} {_shown(mine)} and {_shown(value)}")
        allowed = [_allowed(first), _allowed(second)]
        if allowed[0] is not None and allowed[1] is not None:
            values = [item for item in allowed[0] if item in allowed[1]]
            merged.pop("const", None)
            merged["enum"] = values
            if not values:
                conflicts.append("enum and const without a common value")
        if conflicts:
            merged[_CONFLICTS] = list(dict.fromkeys(conflicts))
        return merged

    def _merge_properties(self, doc: Mapping[str, Any], first: Schema, second: Schema) -> Schema:
        """Combine the ``properties`` of two members.

        Parameters
        ----------
        doc
            The document the schemas belong to.
        first
            The properties of the earlier member.
        second
            The properties of the later member.

        Returns
        -------
        dict
            All properties; one both members have is the merge of the two.
        """
        merged = dict(first)
        for name, schema in second.items():
            merged[name] = self._merge_sub(doc, merged[name], schema) if name in merged else schema
        return merged

    def _merge_sub(self, doc: Mapping[str, Any], first: object, second: object) -> Schema | bool:
        """Combine two sub-schemas (a shared property, ``items``, ``additionalProperties``).

        Parameters
        ----------
        doc
            The document the schemas belong to.
        first
            The earlier schema, possibly a reference, or a boolean.
        second
            The later schema.

        Returns
        -------
        dict or bool
            The merged schema; ``False`` if either is ``False``; the other if one is ``True``.
        """
        if first is False or second is False:
            return False
        if first is True:
            return second
        if second is True:
            return first
        one, _ = self._unwrap(doc, first)
        two, _ = self._unwrap(doc, second)
        return self._merge(doc, one, two)

    def _alternatives(
        self, doc: Mapping[str, Any], schema: Schema, name: str | None
    ) -> list[_Alternative]:
        """Split a schema into the alternatives a value may match.

        Parameters
        ----------
        doc
            The document the schema belongs to.
        schema
            A resolved, unwrapped schema.
        name
            The component the schema came from, if any.

        Returns
        -------
        list of tuple
            ``(name, kind, schema)`` per alternative: its component name (``None`` for an
            inline schema), its kind, and the schema.
        """
        found: list[_Alternative] = []
        for union in ("anyOf", "oneOf"):
            for option in schema.get(union, ()):
                inner, inner_name = self._unwrap(doc, option)
                found.extend(self._alternatives(doc, inner, inner_name))
        if found:
            return found
        if isinstance(schema.get("type"), list):
            return [(None, kind, {**schema, "type": kind}) for kind in schema["type"]]
        return [(name, _kind(schema), schema)]

    def schema(self, base: object, head: object, where: str, *, request: bool) -> None:
        """Compare two schemas.

        Parameters
        ----------
        base
            The earlier schema.
        head
            The later schema.
        where
            The place, for messages.
        request
            True for a value the client sends, False for one it reads.
        """
        old, old_name = self._unwrap(self.base, base)
        new, new_name = self._unwrap(self.head, head)
        if old_name and new_name:
            key = (old_name, new_name, request)
            if key in self._seen:
                return
            self._seen.add(key)
        for conflict in new.get(_CONFLICTS, ()):
            if conflict not in old.get(_CONFLICTS, ()):
                self._add(True, where, f"allOf members cannot be combined: {conflict}")
        if new.get("deprecated") and not old.get("deprecated"):
            self._add(False, where, "deprecated")
        if request and "default" in old and "default" in new and old["default"] != new["default"]:
            self._add(
                True,
                where,
                f"default changed from {_shown(old['default'])} to {_shown(new['default'])}",
            )
        olds = self._alternatives(self.base, old, old_name)
        news = self._alternatives(self.head, new, new_name)
        if len(olds) == 1 and len(news) == 1:
            self._single(olds[0][2], news[0][2], where, request=request)
            return
        self._union(olds, news, where, request=request)

    @staticmethod
    def _pair(
        olds: list[_Alternative], news: list[_Alternative]
    ) -> tuple[list[tuple[Schema, Schema]], list[_Alternative], list[_Alternative]]:
        """Pair the alternatives of two unions.

        First by component name, then by identical shape (a component renamed and reordered at
        once), then what is left by kind, in order: a renamed component, or an inline schema
        that became a component, is still compared by its shape.

        Parameters
        ----------
        olds
            The earlier alternatives.
        news
            The later alternatives.

        Returns
        -------
        tuple
            The paired schemas, the earlier alternatives left over, the later ones left over.
        """
        pairs: list[tuple[Schema, Schema]] = []
        olds, news = list(olds), list(news)
        for same in (
            lambda a, b: a[0] is not None and a[0] == b[0],
            lambda a, b: a[2] == b[2],
            lambda a, b: a[1] == b[1],
        ):
            for old in list(olds):
                match = next((new for new in news if same(old, new)), None)
                if match is not None:
                    pairs.append((old[2], match[2]))
                    olds.remove(old)
                    news.remove(match)
        return pairs, olds, news

    def _union(
        self,
        olds: list[_Alternative],
        news: list[_Alternative],
        where: str,
        *,
        request: bool,
    ) -> None:
        """Compare two lists of alternatives.

        Parameters
        ----------
        olds
            The earlier alternatives.
        news
            The later alternatives.
        where
            The place, for messages.
        request
            True for a value the client sends, False for one it reads.
        """
        pairs, removed, added = self._pair(olds, news)
        for old, new in pairs:
            self._single(old, new, where, request=request)
        for name, kind, _ in removed:
            label = _label(name or kind)
            if not request:
                self._add(False, where, f"no longer {label}")
            elif not any(_covers_alternative(other, kind) for _, other, _ in news):
                self._add(True, where, f"no longer accepts {label}")
        for name, kind, _ in added:
            label = _label(name or kind)
            if request:
                self._add(False, where, f"also accepts {label}")
            elif not any(_covers_alternative(other, kind) for _, other, _ in olds):
                self._add(True, where, f"may now be {label}")

    def _single(self, old: Schema, new: Schema, where: str, *, request: bool) -> None:
        """Compare two schemas of one kind each.

        Parameters
        ----------
        old
            The earlier schema.
        new
            The later schema.
        where
            The place, for messages.
        request
            True for a value the client sends, False for one it reads.
        """
        old_kind, new_kind = _kind(old), _kind(new)
        if old_kind != new_kind:
            wider, narrower = (new_kind, old_kind) if request else (old_kind, new_kind)
            if _covers(wider, narrower):
                verb = "widened" if request else "narrowed"
                self._add(
                    False, where, f"type {verb} from {_label(old_kind)} to {_label(new_kind)}"
                )
            else:
                self._add(
                    True, where, f"type changed from {_label(old_kind)} to {_label(new_kind)}"
                )
            return
        self._allowed_values(old, new, where, request=request)
        if request:
            self._limits(old, new, where)
        if old_kind == "object":
            self._object(old, new, where, request=request)
        if old_kind == "array" and isinstance(old.get("items"), dict):
            self.schema(old["items"], new.get("items", {}), f"{where} []", request=request)

    def _allowed_values(self, old: Schema, new: Schema, where: str, *, request: bool) -> None:
        """Compare the lists of allowed values (``enum``, ``const``).

        Parameters
        ----------
        old
            The earlier schema.
        new
            The later schema.
        where
            The place, for messages.
        request
            True for a value the client sends, False for one it reads.
        """
        before, after = _allowed(old), _allowed(new)
        if before is None or after is None:
            if request and after is not None:
                values = ", ".join(repr(value) for value in after)
                self._add(True, where, f"now accepts only {values}")
            elif request and before is not None:
                self._add(False, where, "accepts any value")
            return
        for value in before:
            if value not in after:
                self._add(True, where, f"value {value!r} removed")
        for value in after:
            if value not in before:
                self._add(False, where, f"value {value!r} new")

    def _limits(self, old: Schema, new: Schema, where: str) -> None:
        """Compare the limits and the pattern of a request value.

        Parameters
        ----------
        old
            The earlier schema.
        new
            The later schema.
        where
            The place, for messages.
        """
        for key in (*_MAXIMA, *_MINIMA, "pattern"):
            before, after = old.get(key), new.get(key)
            if before == after:
                continue
            if after is None:
                narrower = False
            elif before is None or key == "pattern":
                narrower = True
            else:
                narrower = after < before if key in _MAXIMA else after > before
            self._add(narrower, where, f"{key} changed from {_shown(before)} to {_shown(after)}")

    def _object(self, old: Schema, new: Schema, where: str, *, request: bool) -> None:
        """Compare the fields of two object schemas.

        Parameters
        ----------
        old
            The earlier schema.
        new
            The later schema.
        where
            The place, for messages.
        request
            True for a value the client sends, False for one it reads.
        """
        old_fields, new_fields = old.get("properties", {}), new.get("properties", {})
        old_required, new_required = set(old.get("required", ())), set(new.get("required", ()))
        for name in old_fields:
            if name not in new_fields:
                self._add(True, _field(where, name), "removed")
        for name, schema in new_fields.items():
            place = _field(where, name)
            if name not in old_fields:
                if request and name in new_required:
                    self._add(True, place, "new and required")
                else:
                    self._add(False, place, "new")
                continue
            was, now = name in old_required, name in new_required
            if was != now:
                if request:
                    self._add(now, place, "now required" if now else "now optional")
                else:
                    self._add(was, place, "may now be missing" if was else "now always present")
            self.schema(old_fields[name], schema, place, request=request)
        self._other_fields(
            old.get("additionalProperties"), new.get("additionalProperties"), where, request=request
        )

    def _other_fields(self, old: object, new: object, where: str, *, request: bool) -> None:
        """Compare what two object schemas say about fields they do not name.

        Parameters
        ----------
        old
            The earlier ``additionalProperties`` (``None`` when absent).
        new
            The later ``additionalProperties``.
        where
            The place, for messages.
        request
            True for a value the client sends, False for one it reads.
        """
        if isinstance(old, dict) and isinstance(new, dict):
            self.schema(old, new, f"{where} {{}}", request=request)
        elif request and new is False and old is not False:
            self._add(True, where, "no longer accepts other fields")
        elif request and old is False and new is not False:
            self._add(False, where, "accepts other fields")

    def _content(
        self, old: Mapping[str, Any], new: Mapping[str, Any], where: str, *, request: bool
    ) -> None:
        """Compare the media types of a request body or a response.

        Parameters
        ----------
        old
            The earlier ``content`` object.
        new
            The later ``content`` object.
        where
            The place, for messages.
        request
            True for a request body, False for a response.
        """
        for media in old:
            place = where if media == _JSON else f"{where} {media}"
            if media not in new:
                self._add(True, place, "removed")
            elif "schema" in old[media] and "schema" in new[media]:
                self.schema(old[media]["schema"], new[media]["schema"], place, request=request)
        for media in new:
            if media not in old:
                self._add(False, where if media == _JSON else f"{where} {media}", "new")

    def _parameters(self, old: list[Any], new: list[Any], path: str, where: str) -> None:
        """Compare the parameters of two operations.

        Parameters
        ----------
        old
            The earlier operation's parameters.
        new
            The later operation's parameters.
        path
            The later operation's path, to place path parameters.
        where
            The operation, for messages.
        """

        def keyed(doc: Mapping[str, Any], items: list[Any], template: str) -> dict[Any, Schema]:
            names = _PATH_PARAMETER.findall(template)
            found: dict[Any, Schema] = {}
            for item in items:
                parameter, _ = self._resolve(doc, item)
                name, place = parameter["name"], parameter["in"]
                if place == "path" and name in names:
                    found[place, names.index(name)] = parameter
                else:
                    found[place, name.lower() if place == "header" else name] = parameter
            return found

        before = keyed(self.base, old, self._base_path.get(_normalized(path), path))
        after = keyed(self.head, new, path)
        for key, parameter in before.items():
            if key not in after:
                self._add(
                    True, f"{where}: {parameter['in']} parameter `{parameter['name']}`", "removed"
                )
        for key, parameter in after.items():
            place = f"{where}: {parameter['in']} parameter `{parameter['name']}`"
            required = bool(parameter.get("required"))
            if key not in before:
                self._add(required, place, "new and required" if required else "new")
                continue
            if required != bool(before[key].get("required")):
                self._add(required, place, "now required" if required else "now optional")
            if parameter.get("deprecated") and not before[key].get("deprecated"):
                self._add(False, place, "deprecated")
            self.schema(
                before[key].get("schema", {}), parameter.get("schema", {}), place, request=True
            )

    def _body(self, old: object, new: object, where: str) -> None:
        """Compare the request bodies of two operations.

        Parameters
        ----------
        old
            The earlier request body, or ``None``.
        new
            The later request body, or ``None``.
        where
            The operation, for messages.
        """
        place = f"{where}: request body"
        old, _ = self._resolve(self.base, old)
        new, _ = self._resolve(self.head, new)
        if old is None and new is None:
            return
        if new is None:
            self._add(True, place, "removed")
            return
        required = bool(new.get("required"))
        if old is None:
            self._add(required, place, "new and required" if required else "new")
            return
        if required and not old.get("required"):
            self._add(True, place, "now required")
        self._content(old.get("content", {}), new.get("content", {}), place, request=True)

    def _responses(self, old: Mapping[str, Any], new: Mapping[str, Any], where: str) -> None:
        """Compare the documented responses of two operations.

        Parameters
        ----------
        old
            The earlier ``responses`` object.
        new
            The later ``responses`` object.
        where
            The operation, for messages.
        """
        for status, response in old.items():
            place = f"{where}: response {status}"
            if status not in new:
                self._add(True, place, "removed")
                continue
            before, _ = self._resolve(self.base, response)
            after, _ = self._resolve(self.head, new[status])
            self._content(before.get("content", {}), after.get("content", {}), place, request=False)
        for status in new:
            if status not in old:
                self._add(False, f"{where}: response {status}", "new")

    def _operations(self, doc: Mapping[str, Any]) -> Iterator[tuple[str, str, str, Schema]]:
        """List the operations of a document.

        Parameters
        ----------
        doc
            The document.

        Yields
        ------
        tuple
            ``(normalized path, method, path, operation)`` in the document's order.
        """
        for path, item in doc.get("paths", {}).items():
            for method, operation in item.items():
                if method in _METHODS:
                    yield _normalized(path), method, path, operation

    def run(self) -> list[Change]:
        """Compare the two documents.

        Returns
        -------
        list of Change
            Every change, removed operations first, then the later document's order.
        """
        before = {
            (key, method): operation for key, method, _, operation in self._operations(self.base)
        }
        after = {(key, method) for key, method, _, _ in self._operations(self.head)}
        for key, method, path, _ in self._operations(self.base):
            if (key, method) not in after:
                self._add(True, f"{method.upper()} {path}", "removed")
        for key, method, path, operation in self._operations(self.head):
            where = f"{method.upper()} {path}"
            old = before.get((key, method))
            if old is None:
                self._add(False, where, "new")
                continue
            if operation.get("deprecated") and not old.get("deprecated"):
                self._add(False, where, "deprecated")
            self._parameters(
                old.get("parameters", []), operation.get("parameters", []), path, where
            )
            self._body(old.get("requestBody"), operation.get("requestBody"), where)
            self._responses(old.get("responses", {}), operation.get("responses", {}), where)
        return self.changes


def compare(base: Mapping[str, Any], head: Mapping[str, Any]) -> list[Change]:
    """Classify every difference between two OpenAPI documents.

    Parameters
    ----------
    base
        The earlier document (the base branch's).
    head
        The later document (the pull request's).

    Returns
    -------
    list of Change
        Every difference that changes the contract, each breaking or additive.
    """
    return _Comparison(base, head).run()


def _load(path: str) -> Mapping[str, Any]:
    """Read an OpenAPI document.

    Parameters
    ----------
    path
        The file.

    Returns
    -------
    Mapping
        The document.

    Raises
    ------
    ValueError
        When the file is not valid JSON, with a message naming it and the fix.
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(
            f"{path} is not valid JSON ({error}). Regenerate it with `{REGENERATE}`."
        ) from error


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Compare two OpenAPI documents and, if asked, require a breaking title.

    Parameters
    ----------
    argv
        Command-line arguments; ``None`` reads ``sys.argv``.
    environ
        Where to read ``PR_TITLE``; defaults to ``os.environ``.

    Returns
    -------
    int
        1 when a document cannot be read, or when ``--require-breaking-title`` is given and a
        breaking change meets a title without ``!``; 0 otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("base", help="the earlier document (the base branch's)")
    parser.add_argument("head", help="the later document (the pull request's)")
    parser.add_argument(
        "--require-breaking-title",
        action="store_true",
        help="fail when a breaking change meets a PR_TITLE without '!'",
    )
    args = parser.parse_args(argv)
    env = environ if environ is not None else os.environ
    if not Path(args.base).exists():
        print(f"{args.base} has no OpenAPI document: nothing to compare it with.")
        return 0
    if not Path(args.head).exists():
        print(
            f"::error::{args.head}: no such file. Generate it with `{REGENERATE}` and commit it.",
            file=sys.stderr,
        )
        return 1
    try:
        changes = compare(_load(args.base), _load(args.head))
    except ValueError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    if not changes:
        print("The platform REST API did not change.")
        return 0
    breaking = [str(change) for change in changes if change.breaking]
    additive = [str(change) for change in changes if not change.breaking]
    for title, items in (("Breaking", breaking), ("Additive", additive)):
        if items:
            print(f"{title} changes of the platform REST API ({len(items)}):")
            print("\n".join(f"  - {item}" for item in items))
    if not breaking:
        print("No breaking change: the updated snapshot is all this needs.")
        return 0
    if args.require_breaking_title and not BREAKING_TITLE.match(env.get("PR_TITLE", "")):
        print(
            f"::error::This pull request makes {len(breaking)} breaking change(s) to the "
            "platform REST API, but its title has no '!'. Keep the API compatible, or mark the "
            "title breaking (`feat(scope)!: ...`), end the description with 'BREAKING CHANGE: "
            "<what changed and how to migrate>' and add an upgrade note. What counts as "
            f"breaking: {CONTRACT_URL}",
            file=sys.stderr,
        )
        print("\n".join(f"  - {item}" for item in breaking), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
