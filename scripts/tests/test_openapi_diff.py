"""Tests for the classifier of differences between two OpenAPI documents."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from scripts import check_conventional
from scripts.openapi_diff import BREAKING_TITLE, Change, compare, main


def _doc(paths: dict[str, Any], schemas: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a minimal OpenAPI document.

    Parameters
    ----------
    paths
        The ``paths`` object.
    schemas
        The ``components.schemas`` object; none when omitted.

    Returns
    -------
    dict
        The document.
    """
    doc: dict[str, Any] = {"openapi": "3.1.0", "info": {"title": "t", "version": "1"}}
    doc["paths"] = paths
    if schemas is not None:
        doc["components"] = {"schemas": schemas}
    return doc


def _json(schema: dict[str, Any]) -> dict[str, Any]:
    """Wrap a schema as JSON content.

    Parameters
    ----------
    schema
        The schema.

    Returns
    -------
    dict
        A ``content`` object with one ``application/json`` entry.
    """
    return {"application/json": {"schema": schema}}


def _api(
    *,
    body: dict[str, Any] | None = None,
    response: dict[str, Any] | None = None,
    parameters: list[dict[str, Any]] | None = None,
    schemas: dict[str, Any] | None = None,
    path: str = "/api/v1/things",
    method: str = "post",
) -> dict[str, Any]:
    """Build a document with one operation.

    Parameters
    ----------
    body
        The request body schema; no body when omitted.
    response
        The schema of the 200 response; a response without content when omitted.
    parameters
        The operation's parameters.
    schemas
        The ``components.schemas`` object.
    path
        The path of the operation.
    method
        The method of the operation.

    Returns
    -------
    dict
        The document.
    """
    operation: dict[str, Any] = {"responses": {"200": {"description": "OK"}}}
    if response is not None:
        operation["responses"]["200"]["content"] = _json(response)
    if body is not None:
        operation["requestBody"] = {"required": True, "content": _json(body)}
    if parameters is not None:
        operation["parameters"] = parameters
    return _doc({path: {method: operation}}, schemas)


def _obj(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    """Build an object schema.

    Parameters
    ----------
    properties
        Its properties.
    required
        The names of its required properties.

    Returns
    -------
    dict
        The schema.
    """
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    return schema


def _param(name: str, *, required: bool = False, where: str = "query", **schema: object) -> dict:
    """Build a parameter.

    Parameters
    ----------
    name
        Its name.
    required
        Whether it is required.
    where
        Its ``in`` (``query``, ``path``, ``header``).
    **schema
        Its schema; ``type: string`` when empty.

    Returns
    -------
    dict
        The parameter object.
    """
    return {"name": name, "in": where, "required": required, "schema": schema or {"type": "string"}}


def _breaking(base: dict[str, Any], head: dict[str, Any]) -> list[str]:
    """List the breaking changes from one document to another.

    Parameters
    ----------
    base
        The earlier document.
    head
        The later document.

    Returns
    -------
    list of str
        Each breaking change as text.
    """
    return [str(change) for change in compare(base, head) if change.breaking]


def _additive(base: dict[str, Any], head: dict[str, Any]) -> list[str]:
    """List the additive changes from one document to another.

    Parameters
    ----------
    base
        The earlier document.
    head
        The later document.

    Returns
    -------
    list of str
        Each additive change as text.
    """
    return [str(change) for change in compare(base, head) if not change.breaking]


STRING = {"type": "string"}
INTEGER = {"type": "integer"}


def test_identical_documents_have_no_difference() -> None:
    """Nothing changed, nothing to report."""
    doc = _api(body=_obj({"a": STRING}, ("a",)), response=_obj({"b": STRING}, ("b",)))
    assert compare(doc, copy.deepcopy(doc)) == []


def test_text_and_titles_do_not_count() -> None:
    """Summaries, descriptions, titles and operation ids are documentation, not the contract."""
    base = _api(response=_obj({"b": STRING}))
    head = copy.deepcopy(base)
    operation = head["paths"]["/api/v1/things"]["post"]
    operation.update(summary="New", description="New text", operationId="other", tags=["x"])
    operation["responses"]["200"]["description"] = "Changed"
    operation["responses"]["200"]["content"]["application/json"]["schema"]["title"] = "T"
    head["info"] = {"title": "other", "version": "2"}
    assert compare(base, head) == []


# Routes and methods


def test_a_removed_route_is_breaking() -> None:
    """A client calling it would get 404."""
    base = _api()
    head = _doc({})
    assert _breaking(base, head) == ["POST /api/v1/things: removed"]


def test_a_removed_method_is_breaking() -> None:
    """The route stays, but one method of it is gone."""
    base = _api()
    base["paths"]["/api/v1/things"]["get"] = {"responses": {"200": {"description": "OK"}}}
    head = _api()
    assert _breaking(base, head) == ["GET /api/v1/things: removed"]


def test_a_new_route_and_method_are_additive() -> None:
    """No existing call changes."""
    base = _api()
    head = _api()
    head["paths"]["/api/v1/things"]["get"] = {"responses": {"200": {"description": "OK"}}}
    head["paths"]["/api/v1/other"] = {"get": {"responses": {"200": {"description": "OK"}}}}
    assert _breaking(base, head) == []
    assert _additive(base, head) == ["GET /api/v1/things: new", "GET /api/v1/other: new"]


def test_renaming_a_path_parameter_is_not_a_change() -> None:
    """``{name}`` and ``{env}`` in the same place are the same URL on the wire."""
    base = _api(path="/api/v1/x/{name}", parameters=[_param("name", required=True, where="path")])
    head = _api(path="/api/v1/x/{env}", parameters=[_param("env", required=True, where="path")])
    assert compare(base, head) == []


def test_marking_an_operation_deprecated_is_additive() -> None:
    """A deprecation announces a removal; it removes nothing yet."""
    base = _api()
    head = _api()
    head["paths"]["/api/v1/things"]["post"]["deprecated"] = True
    assert _breaking(base, head) == []
    assert _additive(base, head) == ["POST /api/v1/things: deprecated"]


def test_path_items_that_are_not_operations_are_ignored() -> None:
    """A path item may carry ``parameters`` or a ``summary`` next to its methods."""
    base = _api()
    head = _api()
    head["paths"]["/api/v1/things"]["summary"] = "Things"
    assert compare(base, head) == []


# Parameters


def test_a_removed_parameter_is_breaking() -> None:
    """A client sending it would be refused or ignored."""
    base = _api(parameters=[_param("limit")])
    head = _api(parameters=[])
    assert _breaking(base, head) == ["POST /api/v1/things: query parameter `limit`: removed"]


def test_a_new_required_parameter_is_breaking() -> None:
    """Existing calls do not send it."""
    base = _api()
    head = _api(parameters=[_param("limit", required=True)])
    assert _breaking(base, head) == [
        "POST /api/v1/things: query parameter `limit`: new and required"
    ]


def test_a_new_optional_parameter_is_additive() -> None:
    """Existing calls keep working without it."""
    base = _api()
    head = _api(parameters=[_param("limit")])
    assert _breaking(base, head) == []
    assert _additive(base, head) == ["POST /api/v1/things: query parameter `limit`: new"]


def test_a_parameter_that_becomes_required_is_breaking() -> None:
    """Calls that left it out are now refused."""
    base = _api(parameters=[_param("limit")])
    head = _api(parameters=[_param("limit", required=True)])
    assert _breaking(base, head) == ["POST /api/v1/things: query parameter `limit`: now required"]


def test_a_parameter_that_becomes_optional_is_additive() -> None:
    """Every existing call still sends it."""
    base = _api(parameters=[_param("limit", required=True)])
    head = _api(parameters=[_param("limit")])
    assert _additive(base, head) == ["POST /api/v1/things: query parameter `limit`: now optional"]


def test_header_parameters_match_without_regard_to_case() -> None:
    """HTTP header names are case-insensitive."""
    base = _api(parameters=[_param("X-Trace", where="header")])
    head = _api(parameters=[_param("x-trace", where="header")])
    assert compare(base, head) == []


def test_a_narrowed_parameter_type_is_breaking() -> None:
    """``limit=abc`` was accepted; now it is refused."""
    base = _api(parameters=[_param("limit", type="string")])
    head = _api(parameters=[_param("limit", type="integer")])
    assert _breaking(base, head) == [
        "POST /api/v1/things: query parameter `limit`: type changed from string to integer"
    ]


def test_a_deprecated_parameter_is_additive() -> None:
    """It keeps working; the flag announces the removal."""
    base = _api(parameters=[_param("limit")])
    deprecated = _param("limit")
    deprecated["deprecated"] = True
    head = _api(parameters=[deprecated])
    assert _additive(base, head) == ["POST /api/v1/things: query parameter `limit`: deprecated"]


# Request bodies


def test_a_new_required_request_field_is_breaking() -> None:
    """Existing calls do not send it."""
    base = _api(body=_obj({"a": STRING}, ("a",)))
    head = _api(body=_obj({"a": STRING, "b": STRING}, ("a", "b")))
    assert _breaking(base, head) == ["POST /api/v1/things: request body `b`: new and required"]


def test_a_new_optional_request_field_is_additive() -> None:
    """Existing calls keep working without it."""
    base = _api(body=_obj({"a": STRING}, ("a",)))
    head = _api(body=_obj({"a": STRING, "b": STRING}, ("a",)))
    assert _breaking(base, head) == []
    assert _additive(base, head) == ["POST /api/v1/things: request body `b`: new"]


def test_a_removed_request_field_is_breaking() -> None:
    """A client still sending it would have it ignored or refused."""
    base = _api(body=_obj({"a": STRING, "b": STRING}))
    head = _api(body=_obj({"a": STRING}))
    assert _breaking(base, head) == ["POST /api/v1/things: request body `b`: removed"]


def test_a_request_field_that_becomes_required_is_breaking() -> None:
    """Calls that left it out are now refused."""
    base = _api(body=_obj({"a": STRING}))
    head = _api(body=_obj({"a": STRING}, ("a",)))
    assert _breaking(base, head) == ["POST /api/v1/things: request body `a`: now required"]


def test_a_request_field_that_becomes_optional_is_additive() -> None:
    """Every existing call still sends it."""
    base = _api(body=_obj({"a": STRING}, ("a",)))
    head = _api(body=_obj({"a": STRING}))
    assert _additive(base, head) == ["POST /api/v1/things: request body `a`: now optional"]


def test_a_narrowed_request_type_is_breaking() -> None:
    """A value that was accepted is refused."""
    base = _api(body=_obj({"a": {"anyOf": [STRING, {"type": "null"}]}}))
    head = _api(body=_obj({"a": STRING}))
    assert _breaking(base, head) == [
        "POST /api/v1/things: request body `a`: no longer accepts null"
    ]


def test_a_widened_request_type_is_additive() -> None:
    """Every value that was accepted still is."""
    base = _api(body=_obj({"a": STRING}))
    head = _api(body=_obj({"a": {"anyOf": [STRING, {"type": "null"}]}}))
    assert _breaking(base, head) == []
    assert _additive(base, head) == ["POST /api/v1/things: request body `a`: also accepts null"]


def test_a_number_accepts_every_integer() -> None:
    """From integer to number widens a request field; the other way narrows it."""
    narrow = _api(body=_obj({"a": INTEGER}))
    wide = _api(body=_obj({"a": {"type": "number"}}))
    assert _additive(narrow, wide) == [
        "POST /api/v1/things: request body `a`: type widened from integer to number"
    ]
    assert _breaking(wide, narrow) == [
        "POST /api/v1/things: request body `a`: type changed from number to integer"
    ]


def test_a_string_without_a_format_accepts_one_with_a_format() -> None:
    """Dropping ``format: date-time`` from a request field accepts more."""
    dated = _api(body=_obj({"a": {"type": "string", "format": "date-time"}}))
    plain = _api(body=_obj({"a": STRING}))
    assert _breaking(dated, plain) == []
    assert _breaking(plain, dated) == [
        "POST /api/v1/things: request body `a`: type changed from string to string (date-time)"
    ]


def test_a_schema_that_accepts_anything_covers_every_type() -> None:
    """``{}`` in a request accepts every value."""
    typed = _api(body=_obj({"a": STRING}))
    anything = _api(body=_obj({"a": {}}))
    assert _breaking(typed, anything) == []
    assert _breaking(anything, typed) == [
        "POST /api/v1/things: request body `a`: type changed from any value to string"
    ]


@pytest.mark.parametrize(
    ("key", "before", "after"),
    [("maxLength", 10, 5), ("maximum", 10, 5), ("maxItems", 10, 5), ("exclusiveMaximum", 10, 5)],
)
def test_a_lower_maximum_on_a_request_value_is_breaking(key: str, before: int, after: int) -> None:
    """A value at the old limit is refused."""
    base = _api(body=_obj({"a": {"type": "string", key: before}}))
    head = _api(body=_obj({"a": {"type": "string", key: after}}))
    assert _breaking(base, head) == [
        f"POST /api/v1/things: request body `a`: {key} changed from {before} to {after}"
    ]
    assert _breaking(head, base) == []


@pytest.mark.parametrize(
    ("key", "before", "after"),
    [("minLength", 1, 3), ("minimum", 0, 1), ("minItems", 0, 1), ("exclusiveMinimum", 0, 1)],
)
def test_a_higher_minimum_on_a_request_value_is_breaking(key: str, before: int, after: int) -> None:
    """A value at the old limit is refused."""
    base = _api(body=_obj({"a": {"type": "string", key: before}}))
    head = _api(body=_obj({"a": {"type": "string", key: after}}))
    assert _breaking(base, head) == [
        f"POST /api/v1/things: request body `a`: {key} changed from {before} to {after}"
    ]
    assert _additive(head, base) == [
        f"POST /api/v1/things: request body `a`: {key} changed from {after} to {before}"
    ]


def test_a_new_limit_on_a_request_value_is_breaking_and_dropping_one_is_additive() -> None:
    """A limit where there was none narrows what is accepted."""
    base = _api(body=_obj({"a": STRING}))
    head = _api(body=_obj({"a": {"type": "string", "maxLength": 5, "pattern": "^x"}}))
    assert _breaking(base, head) == [
        "POST /api/v1/things: request body `a`: maxLength changed from none to 5",
        "POST /api/v1/things: request body `a`: pattern changed from none to ^x",
    ]
    assert _additive(head, base) == [
        "POST /api/v1/things: request body `a`: maxLength changed from 5 to none",
        "POST /api/v1/things: request body `a`: pattern changed from ^x to none",
    ]


def test_limits_on_response_values_do_not_count() -> None:
    """A client reads what it gets; a limit there promises nothing it relies on."""
    base = _api(response=_obj({"a": {"type": "string", "maxLength": 10}}))
    head = _api(response=_obj({"a": {"type": "string", "maxLength": 5}}))
    assert compare(base, head) == []


def test_a_changed_default_of_a_request_value_is_breaking() -> None:
    """An existing call that leaves the value out now does something else."""
    base = _api(parameters=[_param("limit", type="integer", default=50)])
    head = _api(parameters=[_param("limit", type="integer", default=100)])
    assert _breaking(base, head) == [
        "POST /api/v1/things: query parameter `limit`: default changed from 50 to 100"
    ]


def test_a_new_required_request_body_is_breaking() -> None:
    """Existing calls send none."""
    base = _api()
    head = _api(body=_obj({}))
    assert _breaking(base, head) == ["POST /api/v1/things: request body: new and required"]


def test_a_new_optional_request_body_is_additive() -> None:
    """Existing calls keep working without one."""
    base = _api()
    head = _api(body=_obj({}))
    head["paths"]["/api/v1/things"]["post"]["requestBody"]["required"] = False
    assert _additive(base, head) == ["POST /api/v1/things: request body: new"]


def test_a_removed_request_body_is_breaking() -> None:
    """Existing calls send one that is now ignored or refused."""
    base = _api(body=_obj({}))
    head = _api()
    assert _breaking(base, head) == ["POST /api/v1/things: request body: removed"]


def test_a_request_body_that_becomes_required_is_breaking() -> None:
    """Calls that sent none are now refused."""
    base = _api(body=_obj({}))
    base["paths"]["/api/v1/things"]["post"]["requestBody"]["required"] = False
    head = _api(body=_obj({}))
    assert _breaking(base, head) == ["POST /api/v1/things: request body: now required"]


def test_a_removed_request_media_type_is_breaking() -> None:
    """A client sending that media type is refused."""
    base = _api(body=_obj({}))
    base["paths"]["/api/v1/things"]["post"]["requestBody"]["content"]["text/csv"] = {}
    head = _api(body=_obj({}))
    assert _breaking(base, head) == ["POST /api/v1/things: request body text/csv: removed"]
    assert _additive(head, base) == ["POST /api/v1/things: request body text/csv: new"]


# Responses


def test_a_removed_response_field_is_breaking() -> None:
    """A client reading it finds nothing."""
    base = _api(response=_obj({"a": STRING, "b": STRING}, ("a", "b")))
    head = _api(response=_obj({"a": STRING}, ("a",)))
    assert _breaking(base, head) == ["POST /api/v1/things: response 200 `b`: removed"]


def test_a_new_response_field_is_additive() -> None:
    """Clients ignore fields they do not know."""
    base = _api(response=_obj({"a": STRING}, ("a",)))
    head = _api(response=_obj({"a": STRING, "b": STRING}, ("a", "b")))
    assert _breaking(base, head) == []
    assert _additive(base, head) == ["POST /api/v1/things: response 200 `b`: new"]


def test_a_response_field_that_may_be_missing_is_breaking() -> None:
    """A client reading it may find nothing."""
    base = _api(response=_obj({"a": STRING}, ("a",)))
    head = _api(response=_obj({"a": STRING}))
    assert _breaking(base, head) == ["POST /api/v1/things: response 200 `a`: may now be missing"]
    assert _additive(head, base) == ["POST /api/v1/things: response 200 `a`: now always present"]


def test_a_response_type_that_may_now_be_null_is_breaking() -> None:
    """A client that reads a string may now get null."""
    base = _api(response=_obj({"a": STRING}, ("a",)))
    head = _api(response=_obj({"a": {"anyOf": [STRING, {"type": "null"}]}}, ("a",)))
    assert _breaking(base, head) == ["POST /api/v1/things: response 200 `a`: may now be null"]
    assert _additive(head, base) == ["POST /api/v1/things: response 200 `a`: no longer null"]


def test_a_changed_response_type_is_breaking() -> None:
    """A client parsing a string now gets a number."""
    base = _api(response=_obj({"a": STRING}, ("a",)))
    head = _api(response=_obj({"a": INTEGER}, ("a",)))
    assert _breaking(base, head) == [
        "POST /api/v1/things: response 200 `a`: type changed from string to integer"
    ]


def test_a_narrower_response_type_is_additive() -> None:
    """Every number a client gets is still a number."""
    base = _api(response=_obj({"a": {"type": "number"}}, ("a",)))
    head = _api(response=_obj({"a": INTEGER}, ("a",)))
    assert _additive(base, head) == [
        "POST /api/v1/things: response 200 `a`: type narrowed from number to integer"
    ]


def test_a_changed_status_code_is_breaking() -> None:
    """A client checking for 200 sees 201."""
    base = _api()
    head = _api()
    operation = head["paths"]["/api/v1/things"]["post"]
    operation["responses"] = {"201": operation["responses"]["200"]}
    assert _breaking(base, head) == ["POST /api/v1/things: response 200: removed"]
    assert _additive(base, head) == ["POST /api/v1/things: response 201: new"]


def test_a_removed_response_media_type_is_breaking() -> None:
    """A client asking for it gets something else."""
    base = _api(response=_obj({}))
    base["paths"]["/api/v1/things"]["post"]["responses"]["200"]["content"]["text/csv"] = {}
    head = _api(response=_obj({}))
    assert _breaking(base, head) == ["POST /api/v1/things: response 200 text/csv: removed"]
    assert _additive(head, base) == ["POST /api/v1/things: response 200 text/csv: new"]


def test_a_new_response_field_in_an_array_is_additive() -> None:
    """Items of a list are compared like any object."""
    base = _api(response={"type": "array", "items": _obj({"a": STRING}, ("a",))})
    head = _api(response={"type": "array", "items": _obj({"a": STRING, "b": STRING}, ("a",))})
    assert _additive(base, head) == ["POST /api/v1/things: response 200 [] `b`: new"]


def test_values_of_a_map_are_compared() -> None:
    """``additionalProperties`` with a schema is compared like a field."""
    base = _api(response={"type": "object", "additionalProperties": STRING})
    head = _api(response={"type": "object", "additionalProperties": INTEGER})
    assert _breaking(base, head) == [
        "POST /api/v1/things: response 200 {}: type changed from string to integer"
    ]


def test_refusing_other_request_fields_is_breaking() -> None:
    """A request that carried an extra field is refused."""
    base = _api(body=_obj({"a": STRING}))
    head = _api(body={**_obj({"a": STRING}), "additionalProperties": False})
    assert _breaking(base, head) == [
        "POST /api/v1/things: request body: no longer accepts other fields"
    ]
    assert _additive(head, base) == ["POST /api/v1/things: request body: accepts other fields"]


def test_a_deprecated_field_is_additive() -> None:
    """It keeps working; the flag announces the removal."""
    base = _api(response=_obj({"a": STRING}, ("a",)))
    head = _api(response=_obj({"a": {"type": "string", "deprecated": True}}, ("a",)))
    assert _additive(base, head) == ["POST /api/v1/things: response 200 `a`: deprecated"]


# Enums


def test_a_removed_enum_value_is_breaking() -> None:
    """A client sending or expecting it is surprised."""
    base = _api(body=_obj({"a": {"type": "string", "enum": ["x", "y"]}}))
    head = _api(body=_obj({"a": {"type": "string", "enum": ["x"]}}))
    assert _breaking(base, head) == ["POST /api/v1/things: request body `a`: value 'y' removed"]


def test_a_removed_enum_value_of_a_response_is_breaking() -> None:
    """Shared schemas carry values between requests and responses, so removal counts anywhere."""
    base = _api(response=_obj({"a": {"type": "string", "enum": ["x", "y"]}}, ("a",)))
    head = _api(response=_obj({"a": {"type": "string", "enum": ["x"]}}, ("a",)))
    assert _breaking(base, head) == ["POST /api/v1/things: response 200 `a`: value 'y' removed"]


def test_a_new_enum_value_is_additive() -> None:
    """Clients must accept values they do not know; the contract page says so."""
    base = _api(response=_obj({"a": {"type": "string", "enum": ["x"]}}, ("a",)))
    head = _api(response=_obj({"a": {"type": "string", "enum": ["x", "y"]}}, ("a",)))
    assert _additive(base, head) == ["POST /api/v1/things: response 200 `a`: value 'y' new"]


def test_a_const_counts_as_a_one_value_enum() -> None:
    """Pydantic writes a one-value ``Literal`` as ``const``."""
    base = _api(body=_obj({"a": {"type": "string", "enum": ["x", "y"]}}))
    head = _api(body=_obj({"a": {"type": "string", "const": "x"}}))
    assert _breaking(base, head) == ["POST /api/v1/things: request body `a`: value 'y' removed"]


def test_a_new_enum_on_a_request_value_is_breaking() -> None:
    """Any string was accepted; now only the listed ones are."""
    base = _api(body=_obj({"a": STRING}))
    head = _api(body=_obj({"a": {"type": "string", "enum": ["x"]}}))
    assert _breaking(base, head) == ["POST /api/v1/things: request body `a`: now accepts only 'x'"]
    assert _additive(head, base) == ["POST /api/v1/things: request body `a`: accepts any value"]


def test_a_new_enum_on_a_response_value_is_additive() -> None:
    """The client gets fewer kinds of value than before."""
    base = _api(response=_obj({"a": STRING}, ("a",)))
    head = _api(response=_obj({"a": {"type": "string", "enum": ["x"]}}, ("a",)))
    assert _breaking(base, head) == []
    assert _breaking(head, base) == []


# References, unions and recursion


def test_references_are_resolved_and_renaming_a_schema_is_not_a_change() -> None:
    """The name of a component is not on the wire; its shape is."""
    base = _api(body={"$ref": "#/components/schemas/Old"}, schemas={"Old": _obj({"a": STRING})})
    head = _api(body={"$ref": "#/components/schemas/New"}, schemas={"New": _obj({"a": STRING})})
    assert compare(base, head) == []


def test_a_change_inside_a_referenced_schema_is_found() -> None:
    """The referenced component is compared like an inline one."""
    base = _api(body={"$ref": "#/components/schemas/T"}, schemas={"T": _obj({"a": STRING})})
    head = _api(
        body={"$ref": "#/components/schemas/T"},
        schemas={"T": _obj({"a": STRING, "b": STRING}, ("b",))},
    )
    assert _breaking(base, head) == ["POST /api/v1/things: request body `b`: new and required"]


def test_a_single_all_of_is_unwrapped() -> None:
    """Pydantic wraps a reference with a description in ``allOf``."""
    ref = {"$ref": "#/components/schemas/T"}
    base = _api(response=_obj({"a": ref}, ("a",)), schemas={"T": _obj({"x": STRING}, ("x",))})
    head = _api(
        response=_obj({"a": {"allOf": [ref], "description": "d"}}, ("a",)),
        schemas={"T": _obj({"x": STRING}, ("x",))},
    )
    assert compare(base, head) == []


def test_a_recursive_schema_is_compared_once() -> None:
    """A tree of nodes ends; each component pair is compared once per direction."""
    node = _obj({"children": {"type": "array", "items": {"$ref": "#/components/schemas/Node"}}})
    base = _api(response={"$ref": "#/components/schemas/Node"}, schemas={"Node": node})
    head_node = copy.deepcopy(node)
    head_node["properties"]["name"] = STRING
    head = _api(response={"$ref": "#/components/schemas/Node"}, schemas={"Node": head_node})
    assert _additive(base, head) == ["POST /api/v1/things: response 200 `name`: new"]


def test_union_alternatives_are_matched_by_component_name() -> None:
    """Two object alternatives are told apart by the component they reference."""
    schemas = {"A": _obj({"kind": STRING}), "B": _obj({"size": INTEGER})}
    one = {"$ref": "#/components/schemas/A"}
    two = {"$ref": "#/components/schemas/B"}
    base = _api(body={"oneOf": [one, two]}, schemas=schemas)
    head = _api(body={"oneOf": [one]}, schemas=schemas)
    assert _breaking(base, head) == ["POST /api/v1/things: request body: no longer accepts B"]
    assert _additive(head, base) == ["POST /api/v1/things: request body: also accepts B"]


def test_a_response_union_that_drops_an_alternative_is_additive() -> None:
    """The client gets fewer kinds of value than before."""
    base = _api(response={"anyOf": [STRING, INTEGER]})
    head = _api(response={"anyOf": [STRING]})
    assert _breaking(base, head) == []
    assert _additive(base, head) == ["POST /api/v1/things: response 200: no longer integer"]


def test_a_response_union_that_gains_a_covered_alternative_is_not_breaking() -> None:
    """An integer was already a possible number."""
    base = _api(response={"anyOf": [{"type": "number"}, {"type": "null"}]})
    head = _api(response={"anyOf": [{"type": "number"}, INTEGER, {"type": "null"}]})
    assert _breaking(base, head) == []


def test_a_type_list_counts_as_a_union() -> None:
    """OpenAPI 3.1 may write a nullable string as ``type: [string, null]``."""
    base = _api(body=_obj({"a": {"type": ["string", "null"]}}))
    head = _api(body=_obj({"a": {"anyOf": [STRING, {"type": "null"}]}}))
    assert compare(base, head) == []


def test_an_object_without_a_type_counts_as_an_object() -> None:
    """``properties`` alone makes a schema an object."""
    base = _api(body={"properties": {"a": STRING}})
    head = _api(body=_obj({"a": STRING}))
    assert compare(base, head) == []


# The command line


def _write(path: Path, doc: dict[str, Any]) -> str:
    """Write a document to a file.

    Parameters
    ----------
    path
        Where to write it.
    doc
        The document.

    Returns
    -------
    str
        The path as text.
    """
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


@pytest.fixture
def documents(tmp_path: Path) -> tuple[str, str]:
    """Write a base document and a head document that removes its route.

    Parameters
    ----------
    tmp_path
        Pytest's per-test temporary directory.

    Returns
    -------
    tuple of str
        The paths of the base and the head document.
    """
    return _write(tmp_path / "base.json", _api()), _write(tmp_path / "head.json", _doc({}))


def test_the_command_lists_the_changes_and_passes_without_a_title_check(
    documents: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Run by hand, it reports and never fails."""
    assert main(list(documents), environ={}) == 0
    out = capsys.readouterr().out
    assert "Breaking changes of the platform REST API (1):" in out
    assert "POST /api/v1/things: removed" in out


def test_a_breaking_change_without_a_breaking_title_fails(
    documents: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """The title must say so with ``!``, and the message names the next step."""
    code = main([*documents, "--require-breaking-title"], environ={"PR_TITLE": "feat: x"})
    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith("::error::")
    assert "feat(scope)!:" in err
    assert "BREAKING CHANGE:" in err
    assert "POST /api/v1/things: removed" in err
    assert "https://lakana-labs.github.io/lakana/reference/rest-api/" in err


@pytest.mark.parametrize("title", ["feat!: drop things", "fix(server)!: drop things"])
def test_a_breaking_change_with_a_breaking_title_passes(
    documents: tuple[str, str], title: str
) -> None:
    """A deliberate break, marked as one, is allowed before v1.0.0."""
    assert main([*documents, "--require-breaking-title"], environ={"PR_TITLE": title}) == 0


def test_an_additive_change_needs_no_breaking_title(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only the snapshot changes."""
    base = _write(tmp_path / "base.json", _doc({}))
    head = _write(tmp_path / "head.json", _api())
    assert main([base, head, "--require-breaking-title"], environ={"PR_TITLE": "feat: x"}) == 0
    out = capsys.readouterr().out
    assert "Additive changes of the platform REST API (1):" in out
    assert "No breaking change" in out


def test_no_change_says_so(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An unchanged snapshot reports nothing to do."""
    base = _write(tmp_path / "base.json", _api())
    assert main([base, base], environ={}) == 0
    assert "The platform REST API did not change." in capsys.readouterr().out


def test_a_missing_base_document_is_not_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pull request that adds the snapshot has nothing to compare with."""
    head = _write(tmp_path / "head.json", _api())
    missing = str(tmp_path / "missing.json")
    assert main([missing, head, "--require-breaking-title"], environ={}) == 0
    assert "has no OpenAPI document" in capsys.readouterr().out


def test_a_missing_head_document_fails_and_names_the_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The snapshot must exist on the pull request's side."""
    base = _write(tmp_path / "base.json", _api())
    assert main([base, str(tmp_path / "missing.json")], environ={}) == 1
    err = capsys.readouterr().err
    assert "missing.json" in err
    assert "scripts/generate_reference.py" in err


def test_a_document_that_is_not_json_fails_with_its_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken file is named, not shown as a traceback."""
    base = _write(tmp_path / "base.json", _api())
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert main([base, str(broken)], environ={}) == 1
    err = capsys.readouterr().err
    assert "broken.json is not valid JSON" in err


def test_the_breaking_title_rule_is_the_one_the_convention_check_uses() -> None:
    """Both scripts must agree on what a breaking title is."""
    assert BREAKING_TITLE.pattern == check_conventional._BREAKING_TITLE.pattern


def test_a_change_reads_as_where_and_what() -> None:
    """The text form is what CI prints."""
    assert str(Change(True, "GET /x", "removed")) == "GET /x: removed"


def test_an_array_without_a_type_counts_as_an_array() -> None:
    """``items`` alone makes a schema an array."""
    base = _api(body={"items": STRING})
    head = _api(body={"type": "array", "items": STRING})
    assert compare(base, head) == []


def test_a_request_union_that_drops_a_covered_alternative_accepts_the_same() -> None:
    """Every integer is still accepted as a number."""
    base = _api(body={"anyOf": [INTEGER, {"type": "number"}]})
    head = _api(body={"anyOf": [{"type": "number"}, {"type": "null"}]})
    assert _breaking(base, head) == []


def test_media_types_without_a_schema_are_compared_by_presence_only() -> None:
    """A file download has a media type and no schema."""
    base = _api()
    base["paths"]["/api/v1/things"]["post"]["responses"]["200"]["content"] = {"text/csv": {}}
    assert compare(base, copy.deepcopy(base)) == []


def test_the_boolean_schema_true_accepts_anything() -> None:
    """OpenAPI 3.1 allows ``true`` as a schema; it is the same as ``{}``."""
    base = _api(body=_obj({"a": True}))
    head = _api(body=_obj({"a": {}}))
    assert compare(base, head) == []
