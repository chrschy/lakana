"""Pydantic models for the Iceberg REST wire format.

Every schema here carries JSON-shaped data (dicts, lists, plain strings) —
never a pyiceberg type. `lakana_core.iceberg_json` is where a dict
becomes a `Schema`/`PartitionSpec`/etc; that conversion happens in
`router.py`, not in these models, so this module needs no pyiceberg import
either (`lakana-server` as a whole must not import it).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateNamespaceRequest(BaseModel):
    """Wire shape of a namespace creation request.

    Parameters
    ----------
    namespace
        The namespace to create, as a list of parts.
    properties
        Free-form metadata to attach.
    """

    namespace: list[str]
    properties: dict[str, str] = {}


class NamespaceResponseBody(BaseModel):
    """Wire shape of a namespace response.

    Parameters
    ----------
    namespace
        The namespace, as a list of parts.
    properties
        Its properties.
    """

    namespace: list[str]
    properties: dict[str, str] = {}


class ListNamespacesResponseBody(BaseModel):
    """Wire shape of a namespace listing.

    Parameters
    ----------
    namespaces
        Every namespace, each as a list of parts.
    """

    namespaces: list[list[str]]


class UpdateNamespacePropertiesRequest(BaseModel):
    """Wire shape of a namespace properties update request.

    Parameters
    ----------
    removals
        Property keys to remove.
    updates
        Properties to set.
    """

    removals: list[str] = []
    updates: dict[str, str] = {}


class UpdateNamespacePropertiesResponseBody(BaseModel):
    """Wire shape of a namespace properties update response.

    Parameters
    ----------
    updated
        Keys that were set.
    removed
        Keys that were present and have been removed.
    missing
        Keys requested for removal that were not present.
    """

    updated: list[str]
    removed: list[str]
    missing: list[str]


class ConfigResponseBody(BaseModel):
    """Wire shape of `GET /v1/config`.

    Both fields are required by pyiceberg's own `ConfigResponse` model, even
    when empty — confirmed by reading `RestCatalog._fetch_config`, which
    merges both into its properties unconditionally.

    Parameters
    ----------
    defaults
        Default configuration values.
    overrides
        Configuration values the server forces.
    """

    defaults: dict[str, str] = {}
    overrides: dict[str, str] = {}


class CreateTableRequest(BaseModel):
    """Wire shape of a table creation request.

    `schema_`/`partition_spec`/`write_order` carry pyiceberg-shaped dicts,
    validated by `lakana_core.iceberg_json` in the router — not by this model,
    so a malformed nested shape raises `pydantic.ValidationError` from inside
    the endpoint body rather than at request-parsing time. This is
    deliberate: duplicating pyiceberg's own schema/partition-spec/sort-order
    models here would drift from pyiceberg's real definitions over time.

    Parameters
    ----------
    name
        Table name.
    location
        Must be `None`. Lakana assigns every managed table's location itself;
        a non-`None` value is refused by
        `CatalogService.create_table` rather than honoured.
    schema_
        The Iceberg schema, as a wire-format dict.
    partition_spec
        The partition spec, as a wire-format dict, or `None` for
        unpartitioned.
    write_order
        The sort order, as a wire-format dict, or `None` for unsorted.
    stage_create
        Whether to stage the creation without committing. Not implemented;
        `True` is rejected explicitly rather than silently ignored.
    properties
        Table properties.
    """

    name: str
    location: str | None = None
    schema_: dict[str, Any] = Field(alias="schema")
    partition_spec: dict[str, Any] | None = Field(default=None, alias="partition-spec")
    write_order: dict[str, Any] | None = Field(default=None, alias="write-order")
    stage_create: bool = Field(default=False, alias="stage-create")
    properties: dict[str, str] = {}

    model_config = {"populate_by_name": True}


class TableResponseBody(BaseModel):
    """Wire shape of a table response (`TableResponse` in pyiceberg's client).

    Parameters
    ----------
    metadata_location
        Absolute URI of the current metadata file.
    metadata
        The full Iceberg table metadata, as pyiceberg itself serialises it.
    config
        Per-table configuration overrides. Always empty at M1.
    storage_credentials
        Vended storage credentials. Always empty at M1 — see issue #5.
    """

    metadata_location: str | None = Field(alias="metadata-location")
    metadata: dict[str, Any]
    config: dict[str, str] = {}
    storage_credentials: list[dict[str, Any]] = Field(default=[], alias="storage-credentials")

    model_config = {"populate_by_name": True}


class ListTablesResponseBody(BaseModel):
    """Wire shape of a table listing.

    Parameters
    ----------
    identifiers
        Each identifier as `{"namespace": [...], "name": str}`.
    """

    identifiers: list[dict[str, Any]]


class TableIdentifierBody(BaseModel):
    """Wire shape of a table identifier (`TableIdentifier` in pyiceberg's client).

    Used as the nested shape of `RenameTableRequest.source`/`.destination`.
    Typed as a model rather than `dict[str, Any]` so that a missing `name`
    or `namespace`, or a `namespace` sent as something other than a list,
    fails as a `RequestValidationError` (already caught and correctly
    shaped by `IcebergRoute`) instead of a raw `KeyError` or a namespace
    silently misparsed by `NamespacePath.from_parts` when handed a string.

    Parameters
    ----------
    namespace
        The namespace, as a list of parts.
    name
        The table's name within that namespace.
    """

    namespace: list[str]
    name: str


class RenameTableRequest(BaseModel):
    """Wire shape of a rename request.

    Parameters
    ----------
    source
        The table to move.
    destination
        Where to move it.
    """

    source: TableIdentifierBody
    destination: TableIdentifierBody


class CommitTableRequest(BaseModel):
    """Wire shape of a table commit request.

    Parameters
    ----------
    identifier
        The table identifier the client believes it is committing to.
        Accepted but not cross-checked against the URL path, which already
        identifies the table unambiguously.
    requirements
        Conditions that must hold, as wire-format dicts — validated by
        `lakana_core.iceberg_json.parse_table_requirements` in the router.
    updates
        Updates to apply, as wire-format dicts — validated by
        `lakana_core.iceberg_json.parse_table_updates` in the router.
    """

    identifier: dict[str, Any] | None = None
    requirements: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
