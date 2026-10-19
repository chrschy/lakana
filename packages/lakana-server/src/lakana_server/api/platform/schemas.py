"""Pydantic models for the platform API's request and response bodies."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, model_validator
from pydantic_core import PydanticCustomError

from lakana_core.auth import Action, SecurableKind
from lakana_core.catalog.maintenance import (
    DEFAULT_KEEP_DAYS,
    DEFAULT_KEEP_LAST,
    DEFAULT_ORPHAN_AGE_HOURS,
    MAX_ORPHAN_AGE_HOURS,
    MIN_ORPHAN_AGE_HOURS,
)
from lakana_core.catalog.writes import MAX_SOURCE_NAME, MAX_SOURCE_VERSIONS
from lakana_core.grants.roles import Role
from lakana_core.models import AuditEventType


def _check_storage_options(value: object) -> object:
    """Refuse storage `options` that are not a flat text-to-text object, naming no key.

    Storage options can carry inline S3 credentials, and pydantic puts a mapping key into the
    location of its error, so a credential typed into the wrong place would be echoed. The error
    here names neither a key nor a value. `None` passes (the update request may leave it out).

    Parameters
    ----------
    value
        What the client sent as `options`.

    Returns
    -------
    object
        `value`, unchanged, when it is `None` or every key and value is a string.

    Raises
    ------
    pydantic_core.PydanticCustomError
        If `value` is not an object or any of its values is not a string.
    """
    if value is None:
        return value
    if not isinstance(value, dict) or not all(isinstance(item, str) for item in value.values()):
        raise PydanticCustomError(
            "storage_options",
            "options must be an object whose entries are all text, such as "
            '{"endpoint": "https://s3.corp"}; send each entry as a string.',
        )
    return value


StorageOptions = Annotated[dict[str, str], BeforeValidator(_check_storage_options)]
"""An environment's storage options; a bad shape is refused without echoing any of it."""

OptionalStorageOptions = Annotated[dict[str, str] | None, BeforeValidator(_check_storage_options)]
"""`StorageOptions`, or `None` when the request leaves them unchanged."""


class StorageConfigIn(BaseModel):
    """Storage configuration as accepted from a client.

    Parameters
    ----------
    scheme
        Storage backend.
    root
        Root URI for the environment.
    options
        Backend-specific options.
    """

    scheme: Literal["file", "s3", "abfs"]
    root: str
    options: StorageOptions = {}


class EnvironmentCreate(BaseModel):
    """Request body for creating an environment.

    Parameters
    ----------
    name
        Unique environment name.
    storage
        Where the environment's data lives.
    is_protected
        Whether deleting this environment and changing its storage are
        refused (`CatalogService.delete_environment`/
        `update_environment_storage`) until cleared via
        `PATCH /api/v1/environments/{name}`.
    properties
        Free-form metadata.
    """

    name: str
    storage: StorageConfigIn
    is_protected: bool = False
    properties: dict[str, str] = {}


class EnvironmentOut(BaseModel):
    """Response body for an environment.

    Parameters
    ----------
    name
        Environment name.
    storage_root
        The environment's storage root URI.
    storage_secret_ref
        The name of the secret backing this environment's storage
        credentials, or `None` if it has none.
    is_protected
        Whether deleting this environment and changing its storage are
        refused.
    properties
        Free-form metadata.
    """

    name: str
    storage_root: str
    storage_secret_ref: str | None
    is_protected: bool
    properties: dict[str, str]

    model_config = {"from_attributes": True}


class EnvironmentPatch(BaseModel):
    """Request body for toggling an environment's protected flag.

    Parameters
    ----------
    is_protected
        The new value. Required, unlike `EnvironmentStorageUpdate`'s
        fields: there is exactly one thing `PATCH /api/v1/environments/{name}`
        can change, so there is no "leave unchanged if omitted" case for it
        to support. `StrictBool`, not `bool`: pydantic's lax `bool` coerces
        a string like `"true"` or an int like `1`, which would silently
        accept a client's mistake on an endpoint whose entire job is
        flipping one flag correctly.
    """

    is_protected: StrictBool

    model_config = {"extra": "forbid"}


class NamespaceCreate(BaseModel):
    """Request body for creating a namespace.

    Parameters
    ----------
    path
        Dot-joined namespace path, such as ``gold.sales``.
    properties
        Free-form metadata.
    break_glass_reason
        Write to a protected environment outside a run; needs `MANAGE_ENVIRONMENT`,
        audited.
    """

    path: str
    properties: dict[str, str] = {}
    break_glass_reason: str | None = Field(default=None, max_length=500)


class NamespaceOut(BaseModel):
    """Response body for a namespace.

    Parameters
    ----------
    path
        Dot-joined namespace path.
    properties
        Free-form metadata.
    """

    path: str
    properties: dict[str, str]


class WriteTargetIn(BaseModel):
    """Where a write run puts its result.

    Parameters
    ----------
    table
        The target's dotted name.
    mode
        `create`, `replace`, `append` or `view`.
    incremental_key
        The key column, `append` only.
    evolve_schema
        Let the target adopt the result's schema, `replace`/`append` only.
    """

    model_config = ConfigDict(extra="forbid")

    table: str = Field(min_length=1, max_length=1024)
    mode: Literal["create", "replace", "append", "view"]
    incremental_key: str | None = Field(default=None, max_length=255)
    evolve_schema: bool = False


class QuerySubmit(BaseModel):
    """Request body for submitting a query.

    Parameters
    ----------
    sql
        The SQL text to run.
    target
        Makes this a write run: the result goes into this table or
        view instead of a result file.
    break_glass_reason
        Write to a protected environment outside a run; needs `MANAGE_ENVIRONMENT`,
        audited.
    """

    sql: str
    target: WriteTargetIn | None = None
    break_glass_reason: str | None = Field(default=None, max_length=500)


class CheckRowsRequest(BaseModel):
    """Request body for the query that shows the rows a check of a run found.

    Parameters
    ----------
    check
        The check as `lakana check list` names it: ``<namespace>.<table>/<check>``. The run
        comes from the path; the snapshot the check read and the stored result are looked up
        by the server, never named by the caller.
    """

    model_config = ConfigDict(extra="forbid")

    check: str = Field(min_length=1, max_length=1000)


class QuerySubmitOut(BaseModel):
    """Response body for a freshly submitted query.

    Parameters
    ----------
    query_id
        The submitted query's id, to poll or fetch results with.
    """

    query_id: str


class ArtifactLimitsOut(BaseModel):
    """What one artifact version may hold on this server (`LAKANA_ARTIFACTS__MAX_*`).

    Parameters
    ----------
    max_files
        Most files in one version.
    max_file_bytes
        Largest single file.
    max_bytes
        Largest total size of one version.
    """

    max_files: int
    max_file_bytes: int
    max_bytes: int


class InfoOut(BaseModel):
    """Response body for `GET /api/v1/info`.

    Parameters
    ----------
    version
        The platform version the server runs. A project's `lakana[assets]` pin must be
        compatible with it.
    artifact_limits
        The artifact limits the operator set, so a client can check a tree against them before
        it sends a byte. An older server does not send it.
    """

    version: str
    artifact_limits: ArtifactLimitsOut


class QueryStatusOut(BaseModel):
    """Response body for a query's current status.

    Parameters
    ----------
    query_id
        The query's id.
    status
        `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED` or `CANCELLED`.
    error_message
        Set when `status` is `FAILED` or `CANCELLED`.
    lineage
        Column lineage, set only when `status` is `SUCCEEDED`.
    write_mode
        A write run's mode, `None` for a read.
    target
        A write run's target, as submitted.
    rows_written
        Rows a write run wrote.
    snapshot_id
        The snapshot a write run committed, if any.
    watermark
        The type-tagged watermark an incremental append filtered on.
    schema_diff
        How a write changed its target's schema, if it did.
    maintenance
        A maintenance run's `{"request", "report"}`. The report's `conflict`
        is `true` when the table changed during maintenance and nothing was
        applied (retry); its `note` says in words why part of the run was
        skipped.
    error_cause
        Why a `FAILED` or `CANCELLED` run ended: `not_found`, `permission_denied`,
        `result_too_large`, `invalid_query`, `out_of_memory`, `timeout`, `cancelled`, `worker_lost`
        or `internal`.
        `null` for a run that did not end that way and for a failure recorded without one
        (a run from before the field existed).
    waiting_for
        Why a `QUEUED` query has not started, or `null` for any other status: a stable `code`
        (`no_worker`, `no_worker_of_kind`, `user_limit`, `lock_timeout`, `starting` or
        `workers_busy`), one
        sentence in `message`, and the numbers the sentence uses. `workers` is the number of
        workers that take this kind of work and `ahead` the queued work those workers take
        before this query; both count the whole instance. `no_worker_of_kind` (workers are
        running and none takes this kind of work) carries `workers` (0) and `other_workers`.
        `user_limit` (the submitter already runs as many ad-hoc queries as the limit per user
        allows) carries `running`, the submitter's own number, and `limit`, and no `ahead`; a
        manager who reads another principal's row gets `limit` only. `lock_timeout` (a worker
        waited too long for the lock that serialises the start of the submitter's queries, so
        workers pass them by for a few seconds) carries `workers` and `next_attempt_at`, the end
        of that time.
    """

    query_id: str
    status: str
    error_message: str | None = None
    lineage: dict[str, list[dict[str, str]]] | None = None
    write_mode: str | None = None
    target: str | None = None
    rows_written: int | None = None
    snapshot_id: int | None = None
    watermark: str | None = None
    schema_diff: dict[str, list[dict[str, str]]] | None = None
    maintenance: dict | None = None
    error_cause: str | None = None
    waiting_for: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Why this is waiting, or null when it is not: a stable `code`, one sentence in "
            "`message`, and the numbers the sentence uses."
        ),
    )


class ExternalTableCreate(BaseModel):
    """Request body for registering a raw-zone table.

    Parameters
    ----------
    name
        Table name.
    location_pattern
        A pattern shaped `"raw/<dir...>/<basename-glob>"`. The directory
        part is this table's exclusive claim within the environment; the
        final component is a basename-only glob matched against direct
        children of that directory only.
    format
        `"parquet"`, `"csv"`, or `"json"`.
    break_glass_reason
        Write to a protected environment outside a run; needs `MANAGE_ENVIRONMENT`,
        audited.
    """

    name: str
    location_pattern: str
    format: str
    break_glass_reason: str | None = Field(default=None, max_length=500)


class ExternalTableOut(BaseModel):
    """Response body for a registered raw-zone table.

    The wire field is still named `"schema"` (`Field(alias="schema")`); the
    Python attribute is `table_schema`, not `schema`, because `BaseModel`
    itself defines a (deprecated, v1-compat) `schema()` method, and pydantic
    warns at class-definition time that a field named `schema` shadows it
    . `populate_by_name=True` keeps construction
    by either name working — `tables.py` builds this from a dict keyed
    `"schema"` — and `serialize_by_alias=True` keeps the JSON key `schema`
    for callers that call `.model_dump()` directly instead of going through
    FastAPI's `response_model` (which already serializes by alias).

    Parameters
    ----------
    name
        Table name.
    namespace
        Dot-joined namespace path.
    location_pattern
        The glob it was registered with.
    format
        Its file format.
    schema
        The inferred column-name-to-type mapping.
    """

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    name: str
    namespace: str
    location_pattern: str
    format: str
    table_schema: dict[str, str] = Field(alias="schema")


class ColumnIn(BaseModel):
    """One column in a caller-declared table schema.

    `extra="forbid"` refuses an unrecognised field
    the same way `TableCreate` does at the table level.

    Parameters
    ----------
    name
        Column name. Bounded to 255 characters — generous for any real
        column name, small enough to keep a pathological request cheap to
        reject.
    type
        An Iceberg primitive type name — one of `boolean`, `int`, `long`,
        `float`, `double`, `decimal(p,s)`, `date`, `time`, `timestamp`,
        `timestamptz`, `string`, `uuid`, or `binary`. Nested types are out
        of scope for M4. `lakana_core.catalog.json_schema.to_iceberg_schema`
        is the single place that maps this name onto a pyiceberg type.
        Bounded to 64 characters — the longest real value here,
        `decimal(38,38)`-shaped, is nowhere near that.
    nullable
        Whether the column may hold nulls.
    """

    name: str = Field(max_length=255)
    type: str = Field(max_length=64)
    nullable: bool = True

    model_config = {"extra": "forbid"}


class TableCreate(BaseModel):
    """Request body for creating a managed table from a JSON schema.

    `extra="forbid"` refuses an unrecognised field outright (422) rather
    than silently ignoring it — in particular, a caller-sent `"location"`
    is refused the same way the Iceberg router already refuses one, instead
    of being accepted and quietly dropped, which would misleadingly suggest
    it had any effect.

    The wire field is still named `"schema"` (`Field(alias="schema")`); the
    Python attribute is `columns`, not `schema`, because `BaseModel` itself
    defines a (deprecated, v1-compat) `schema()` method, and pydantic warns
    at class-definition time that a field named `schema` shadows it — `ExternalTableOut` has the
    same shape (`table_schema`
    aliased to `schema`), fixed the same way.

    Parameters
    ----------
    name
        Table name.
    columns
        The table's columns, in order. Field ids are assigned `1..n` in
        this order. Bounded to 1000 columns.
    break_glass_reason
        Write to a protected environment outside a run; needs `MANAGE_ENVIRONMENT`,
        audited.
    """

    name: str
    columns: list[ColumnIn] = Field(alias="schema", max_length=1000)
    break_glass_reason: str | None = Field(default=None, max_length=500)

    model_config = {"extra": "forbid", "populate_by_name": True}


class AppendOut(BaseModel):
    """Response body for a governed Arrow append.

    Parameters
    ----------
    rows
        Number of rows appended.
    files
        Number of data files this append wrote. `0` when `rows` is `0`.
    snapshot_id
        The id of the snapshot this append committed, or `None` when `rows`
        is `0` — an empty append commits nothing.
    """

    rows: int
    files: int
    snapshot_id: int | None


class ReplaceOut(BaseModel):
    """The outcome of an Arrow replace.

    Parameters
    ----------
    rows
        Rows written.
    files
        Data files written.
    snapshot_id
        The snapshot committed.
    schema_diff
        How the table's schema changed, or `None` when it didn't.
    """

    rows: int
    files: int
    snapshot_id: int | None
    schema_diff: dict[str, list[dict[str, str]]] | None = None


class ColumnOut(BaseModel):
    """One column of a table's schema, in the JSON column form.

    Parameters
    ----------
    name
        Column name.
    type
        The Iceberg type in the JSON form (`"long"`, `"decimal(10,2)"`, ...),
        or the Iceberg string for a nested type.
    nullable
        Whether the column accepts nulls.
    description
        The column's description: the edit when it has one, else the project's text; `None`
        when there is none.
    description_source
        ``edited`` or ``project``; `None` when there is no description.
    """

    name: str
    type: str
    nullable: bool
    description: str | None = None
    description_source: Literal["project", "edited"] | None = None


class TableReferenceOut(BaseModel):
    """One table or view a view references.

    Parameters
    ----------
    id
        Its id.
    name
        Its dotted name when the view was created.
    """

    id: str
    name: str


class TableOut(BaseModel):
    """Response body for a table, external table or view, read-only.

    Parameters
    ----------
    name
        Table name.
    namespace
        Dot-joined namespace path the table lives in.
    uuid
        The table's id (the Iceberg ``table-uuid`` for a managed table).
    kind
        ``TABLE``, ``EXTERNAL``, ``VIEW`` or ``ARTIFACT``.
    metadata_location
        Absolute URI of the current metadata file; `None` for an external
        table or a view.
    columns
        The columns, serialized as `schema`.
    sql
        A view's SELECT. `None` if the caller may not read every reference
        .
    references
        A view's references. Empty if the caller may not read every
        reference.
    hidden_references
        The number of the view's references the caller may not read, and
        so are withheld from `sql` and `references`.
    snapshot_id
        The current snapshot of a managed table; `None`
        without one, and for a view or an external table.
    state
        ``not_published`` for a table whose first build did not finish, or failed a blocking
        check, and that no later run has published; ``published`` for everything else.
    state_message
        Why it is not published, naming the check and the run; `None` otherwise.
    description
        The table's description: the edit when it has one, else the project's text; `None`
        when there is none.
    description_source
        ``edited`` or ``project``; `None` when there is no description.
    last_write_at
        When the current snapshot was committed; `None` without one, and for a view, an
        external table and an artifact.
    row_count
        The rows of the current snapshot, from its summary; `None` when missing.
    size_bytes
        The size of its data files, from its summary; `None` when missing.
    file_count
        Its data files, from its summary; `None` when missing.
    """

    name: str
    namespace: str
    uuid: str
    kind: Literal["TABLE", "EXTERNAL", "VIEW", "ARTIFACT"] = "TABLE"
    metadata_location: str | None
    columns: list[ColumnOut] = Field(serialization_alias="schema")
    sql: str | None = None
    references: list[TableReferenceOut] | None = None
    hidden_references: int = 0
    snapshot_id: int | None = None
    state: Literal["published", "not_published", "kept"] = "published"
    state_message: str | None = None
    description: str | None = None
    description_source: Literal["project", "edited"] | None = None
    last_write_at: datetime | None = None
    row_count: int | None = None
    size_bytes: int | None = None
    file_count: int | None = None


class LastMaintenanceOut(BaseModel):
    """The last maintenance run that changed a table.

    Parameters
    ----------
    run_id
        The query run that did it.
    finished_at
        When it ended.
    status
        ``complete``, or ``stopped`` when it ran past its time limit and stopped at a safe point;
        running maintenance again continues.
    snapshots_expired
        Snapshots it expired.
    files_deleted
        Orphaned files it deleted.
    bytes_deleted
        Their total size.
    tags_removed
        Tags of failed checks it removed.
    """

    run_id: uuid.UUID
    finished_at: datetime
    status: Literal["complete", "stopped"]
    snapshots_expired: int
    files_deleted: int
    bytes_deleted: int
    tags_removed: int


class CheckHealthOut(BaseModel):
    """The newest result of one check of a table, as `CheckHealth` holds it.

    Parameters
    ----------
    name
        `CheckHealth.name`.
    check
        `CheckHealth.check`.
    run_id
        `CheckHealth.run_id`.
    outcome
        `CheckHealth.outcome`.
    on_failure
        `CheckHealth.on_failure`.
    failing_rows
        `CheckHealth.failing_rows`.
    error
        `CheckHealth.error`.
    rollback_outcome
        `CheckHealth.rollback_outcome`.
    kept
        `CheckHealth.kept`.
    not_published
        `CheckHealth.not_published`.
    finished_at
        `CheckHealth.finished_at`.
    """

    name: str
    check: str
    run_id: uuid.UUID
    outcome: str
    on_failure: str
    failing_rows: int | None
    error: str | None
    rollback_outcome: str | None
    kept: bool
    not_published: bool
    finished_at: datetime


class ReaderOut(BaseModel):
    """A principal's use of a table over the window.

    Parameters
    ----------
    principal
        The principal's name.
    read_count
        How many of its succeeded queries named the table.
    last_read_at
        When the last of them completed.
    """

    principal: str
    read_count: int
    last_read_at: datetime


class TableHealthOut(BaseModel):
    """A table's health at a glance.

    Parameters
    ----------
    days
        The window `readers` covers.
    last_maintenance
        The last maintenance that changed the table; `None` if there is none.
    checks
        The newest result of each check of the table.
    readers
        Who read the table in the window, most reads first; `None` for a caller who may not see
        the table's grants.
    """

    days: int
    last_maintenance: LastMaintenanceOut | None
    checks: list[CheckHealthOut]
    readers: list[ReaderOut] | None


class DescriptionIn(BaseModel):
    """Request body that sets or resets a description.

    Parameters
    ----------
    description
        The new text (1 to 4,000 characters, not blank), or `null` to remove the edit so that the
        project's text shows again. The field must be sent.
    """

    description: str | None


class LineageNodeOut(BaseModel):
    """One node of a lineage graph.

    Parameters
    ----------
    id
        The table's id.
    name
        Its dotted name.
    kind
        ``TABLE``, ``EXTERNAL``, ``VIEW`` or ``ARTIFACT``; `None` once dropped.
    dropped
        Whether the table no longer exists.
    """

    id: str
    name: str
    kind: str | None
    dropped: bool


class LineageEdgeOut(BaseModel):
    """One edge of a lineage graph.

    Parameters
    ----------
    source_id
        The source table's id.
    target_id
        The target table's id.
    source_snapshot_id
        The source snapshot read, if known.
    target_snapshot_id
        The target snapshot committed, if any.
    query_run_id
        The run that wrote it, if any.
    created_at
        When it was written.
    """

    source_id: str
    target_id: str
    source_snapshot_id: int | None
    target_snapshot_id: int | None
    query_run_id: str | None
    created_at: datetime


class MaintenanceIn(BaseModel):
    """Request body for table maintenance.

    Parameters
    ----------
    keep_days
        Snapshots younger than this many days are kept.
    keep_last
        The most recent this many snapshots are always kept.
    dry_run
        Report what would happen without changing anything.
    older_than_hours
        Unreferenced files younger than this many hours are never deleted;
        1 to 87600 (ten years).
    """

    model_config = ConfigDict(extra="forbid")

    keep_days: int = Field(default=DEFAULT_KEEP_DAYS, ge=0)
    keep_last: int = Field(default=DEFAULT_KEEP_LAST, ge=1)
    dry_run: bool = False
    older_than_hours: int = Field(
        default=DEFAULT_ORPHAN_AGE_HOURS, ge=MIN_ORPHAN_AGE_HOURS, le=MAX_ORPHAN_AGE_HOURS
    )


class LineageOut(BaseModel):
    """Response body for a table's lineage.

    Parameters
    ----------
    root_id
        The table asked about.
    direction
        `upstream` or `downstream`.
    depth
        The hops walked.
    nodes
        Every node the caller may read.
    edges
        One edge per source/target pair, the most recent.
    hidden_nodes
        Nodes omitted because the caller can't read them.
    truncated
        Whether the walk stopped at the node cap with a readable node left
        out. A node the caller can't read never sets it.
    """

    root_id: str
    direction: str
    depth: int
    nodes: list[LineageNodeOut]
    edges: list[LineageEdgeOut]
    hidden_nodes: int
    truncated: bool


class PrincipalCreate(BaseModel):
    """Request body for creating a principal.

    Parameters
    ----------
    name
        Unique principal name.
    type
        Whether this is a human user or a service account.
    """

    name: str
    type: Literal["USER", "SERVICE_ACCOUNT"]


class PrincipalOut(BaseModel):
    """Response body for a principal.

    Parameters
    ----------
    name
        Principal name.
    type
        Whether this is a human user or a service account.
    is_active
        Whether this principal can currently authenticate.
    """

    name: str
    type: str
    is_active: bool

    model_config = {"from_attributes": True}


class GroupCreate(BaseModel):
    """Request body for creating a group.

    Parameters
    ----------
    name
        Unique group name.
    """

    name: str


class GroupOut(BaseModel):
    """Response body for a group.

    Parameters
    ----------
    name
        Group name.
    """

    name: str

    model_config = {"from_attributes": True}


class TokenIssueIn(BaseModel):
    """Request body for issuing a PAT.

    Parameters
    ----------
    name
        A human label for this PAT (e.g. "laptop", "CI").
    """

    name: str


class TokenIssueOut(BaseModel):
    """Response body for a freshly issued PAT.

    Parameters
    ----------
    key_id
        The public lookup key.
    name
        The label given at issuance.
    token
        The full token value. Shown here exactly once — never retrievable
        again.
    """

    key_id: str
    name: str
    token: str


class TokenOut(BaseModel):
    """Response body for a listed PAT. Never includes a secret.

    Parameters
    ----------
    key_id
        The public lookup key.
    name
        The label given at issuance.
    last_used_at
        When this PAT last successfully authenticated a request, or `None`
        if never.
    """

    key_id: str
    name: str
    last_used_at: datetime | None = None

    model_config = {"from_attributes": True}


class TaskTokenMintIn(BaseModel):
    """Request body for minting a task token.

    Parameters
    ----------
    principal
        The identity the token authenticates as. At most 255 characters (the
        `principals.name` column), because a refused mint records it in the
        audit log.
    environment
        The one environment this task may touch.
    asset_scope
        The declared asset name/pattern strings.
    ttl_seconds
        Seconds until the token expires, from now. Bounded to `> 0` here, at
        the HTTP boundary, since an already-dead (non-positive) TTL is
        never a sensible request. The *upper* bound is deliberately not
        enforced here: `TaskTokenService.mint` is the single source of that
        cap (`max_ttl_seconds`, operator-configured via
        `LAKANA_TASK_TOKENS__MAX_TTL_SECONDS`), so raising the operator's
        configured cap above this schema's own default actually takes
        effect, instead of being silently overridden by a value hard-coded
        here. `TaskTokenService.mint` itself accepts any
        `int`, including negative values, for tests that construct an
        already-expired token by calling the service directly.
    """

    principal: str = Field(max_length=255)
    environment: str
    asset_scope: list[str]
    ttl_seconds: int = Field(gt=0)


class TaskTokenMintOut(BaseModel):
    """Response body for a freshly minted task token.

    Parameters
    ----------
    key_id
        The public lookup key.
    token
        The full token value. Shown here exactly once.
    """

    key_id: str
    token: str


class GrantCreate(BaseModel):
    """Request body for creating a grant.

    Exactly one of `principal`/`group` and exactly one of `role`/`action`
    must be given; `GrantService.grant` raises `ValidationError` otherwise.
    `environment`/`namespace`/`table` must match what `kind` expects — an
    ENVIRONMENT-kind grant requires `environment` and forbids `namespace`/
    `table`, and so on; `GrantService.grant` validates this too.

    Parameters
    ----------
    principal
        Name of the principal to grant to, or `None` if granting to a group.
    group
        Name of the group to grant to, or `None` if granting to a principal.
    role
        A role template to grant, or `None` if granting a raw action.
    action
        A raw action to grant, or `None` if granting a role.
    kind
        Which tier of the securable hierarchy this grant applies to.
    environment
        The environment, for an ENVIRONMENT/NAMESPACE/TABLE-scoped grant.
    namespace
        The dot-joined namespace path, such as ``gold.sales``, for a
        NAMESPACE/TABLE-scoped grant.
    table
        The table name, for a TABLE-scoped grant.
    """

    principal: str | None = None
    group: str | None = None
    role: Role | None = None
    action: Action | None = None
    kind: SecurableKind
    environment: str | None = None
    namespace: str | None = None
    table: str | None = None


class GrantOut(BaseModel):
    """Response body for a grant.

    Parameters
    ----------
    id
        The grant's id.
    principal_id
        The principal this grant applies to, or `None` if it applies to a
        group.
    group_id
        The group this grant applies to, or `None` if it applies to a
        principal.
    role
        The role template granted, or `None` if a raw action was granted.
    action
        The raw action granted, or `None` if a role was granted.
    kind
        Which tier of the securable hierarchy this grant applies to.
    environment
        The environment this grant applies to, if any.
    namespace
        The dot-joined namespace path this grant applies to, if any.
    table
        The table this grant applies to, if any.
    granted_by
        The principal who created this grant.
    granted_at
        When this grant was created.
    note
        ``no effect: <ACTION> needs the ADMIN role`` for a single-action grant
        of `MANAGE_GRANT`, `MANAGE_GROUP` or `MANAGE_TOKEN`, which work only
        with the `ADMIN` role on the instance; `None` otherwise.
    """

    id: uuid.UUID
    principal_id: uuid.UUID | None
    group_id: uuid.UUID | None
    role: Role | None
    action: Action | None
    kind: SecurableKind
    environment: str | None
    namespace: str | None
    table: str | None
    granted_by: uuid.UUID
    granted_at: datetime
    note: str | None = None


class RowFilterCreate(BaseModel):
    """Request body for creating a row filter.

    Exactly one of `principal`/`group` must be given;
    `RowFilterService.create` raises `ValidationError` otherwise, and
    validates `expression` against the restricted subset.

    Parameters
    ----------
    principal
        Name of the principal this filter applies to, or `None` if
        applying to a group.
    group
        Name of the group this filter applies to, or `None` if applying to
        a principal.
    environment
        The environment the filtered table lives in.
    namespace
        The dot-joined namespace path, such as ``gold.sales``.
    table
        The table name.
    expression
        The restricted-subset predicate.
    """

    principal: str | None = None
    group: str | None = None
    environment: str
    namespace: str
    table: str
    expression: str


class RowFilterOut(BaseModel):
    """Response body for a row filter.

    Parameters
    ----------
    id
        The row filter's id.
    principal_id
        The principal this filter applies to, or `None` if it applies to a
        group.
    group_id
        The group this filter applies to, or `None` if it applies to a
        principal.
    environment
        The environment this filter applies to.
    namespace
        The dot-joined namespace path this filter applies to.
    table
        The table this filter applies to.
    expression
        The restricted-subset predicate.
    created_by
        The principal who created this filter.
    created_at
        When this filter was created.
    """

    id: uuid.UUID
    principal_id: uuid.UUID | None
    group_id: uuid.UUID | None
    environment: str
    namespace: str
    table: str
    expression: str
    created_by: uuid.UUID
    created_at: datetime


class ColumnMaskCreate(BaseModel):
    """Request body for creating a column mask.

    Exactly one of `principal`/`group` must be given;
    `ColumnMaskService.create` raises `ValidationError` otherwise, and
    validates `expression` against the mask grammar (the restricted subset
    plus `substr`/`substring`/`||`/`md5`).

    Parameters
    ----------
    principal
        Name of the principal this mask applies to, or `None` if applying
        to a group.
    group
        Name of the group this mask applies to, or `None` if applying to a
        principal.
    environment
        The environment the masked table lives in.
    namespace
        The dot-joined namespace path, such as ``gold.sales``.
    table
        The table name.
    column
        The masked column's name.
    expression
        The replacement value.
    """

    principal: str | None = None
    group: str | None = None
    environment: str
    namespace: str
    table: str
    column: str
    expression: str


class ColumnMaskOut(BaseModel):
    """Response body for a column mask.

    Parameters
    ----------
    id
        The column mask's id.
    principal_id
        The principal this mask applies to, or `None` if it applies to a
        group.
    group_id
        The group this mask applies to, or `None` if it applies to a
        principal.
    environment
        The environment this mask applies to.
    namespace
        The dot-joined namespace path this mask applies to.
    table
        The table this mask applies to.
    column
        The masked column's name.
    expression
        The replacement value.
    created_by
        The principal who created this mask.
    created_at
        When this mask was created.
    """

    id: uuid.UUID
    principal_id: uuid.UUID | None
    group_id: uuid.UUID | None
    environment: str
    namespace: str
    table: str
    column: str
    expression: str
    created_by: uuid.UUID
    created_at: datetime


def _check_secret_values(value: object) -> object:
    """Refuse a secret's `values` that is not a flat text-to-text object, naming no key.

    pydantic puts a mapping key into the location of its error, and a secret typed into
    the wrong place can be that key; the error here names neither a key nor a value.

    Parameters
    ----------
    value
        What the client sent as `values`.

    Returns
    -------
    object
        `value`, unchanged, when every key and value is a string.

    Raises
    ------
    pydantic_core.PydanticCustomError
        If `value` is not an object or any of its values is not a string.
    """
    if not isinstance(value, dict) or not all(isinstance(item, str) for item in value.values()):
        raise PydanticCustomError(
            "secret_values",
            "values must be an object whose entries are all text, such as "
            '{"access_key_id": "..."}; send each entry as a string.',
        )
    return value


SecretValues = Annotated[dict[str, str], BeforeValidator(_check_secret_values)]
"""A secret's plaintext key-value bag; a bad shape is refused without echoing any of it."""


class SecretBind(BaseModel):
    """Request body for binding a secret to its use.

    Parameters
    ----------
    kind
        ``package_index``, ``repository`` or ``environment``.
    target
        The index's or repository's name; absent or null for the environment.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["package_index", "repository", "environment"]
    target: str | None = None


class SecretCreate(BaseModel):
    """Request body for creating a secret.

    Parameters
    ----------
    name
        The secret's name, unique within its environment.
    values
        The plaintext key-value bag to encrypt and store. Never echoed
        back in any response.
    use
        What the secret is for, so that it is bound as it is created; absent leaves it unbound.
    """

    name: str
    values: SecretValues
    use: SecretBind | None = None


class SecretRotate(BaseModel):
    """Request body for rotating a secret.

    Parameters
    ----------
    values
        The new plaintext key-value bag.
    """

    values: SecretValues


class SecretOut(BaseModel):
    """Response body for a secret. Structurally cannot carry a value or ciphertext field.

    Parameters
    ----------
    id
        The secret's id.
    name
        The secret's name.
    environment
        The environment this secret belongs to.
    created_by
        The principal who created this secret.
    created_at
        When this secret was created.
    format_version
        How the stored secret is encrypted. 1 is the original format; a
        secret still at 1 becomes the current format when it is set again.
    key_id
        Which key provider and key wrapped the secret's data key. Safe to
        show: it does not reveal the key.
    use
        What the secret is bound to: ``package_index``, ``repository``, ``environment``, or
        `None` for an unbound secret.
    use_target
        The index's or repository's name; `None` otherwise, also when that entry was removed.
    use_target_url
        Its URL, which is where the secret is sent; `None` when `use_target` is.
    """

    id: uuid.UUID
    name: str
    environment: str
    created_by: uuid.UUID
    created_at: datetime
    format_version: int
    key_id: str
    use: str | None = None
    use_target: str | None = None
    use_target_url: str | None = None


class AuditEventOut(BaseModel):
    """Response body for one audit event.

    Parameters
    ----------
    id
        The event's id.
    occurred_at
        When the event occurred.
    principal_id
        The principal attributed to this event, or `None` for a failed
        authentication.
    event_type
        The kind of event.
    securable_kind
        Which tier of the securable hierarchy this event's target belongs
        to, if any.
    securable_environment
        The environment this event's target belongs to, if any.
    securable_namespace
        The dot-joined namespace path this event's target belongs to, if
        any.
    securable_table
        The table this event's target refers to, if any.
    detail
        Event-specific facts not already captured by the fields above.
    """

    id: uuid.UUID
    occurred_at: datetime
    principal_id: uuid.UUID | None
    event_type: AuditEventType
    securable_kind: SecurableKind | None
    securable_environment: str | None
    securable_namespace: str | None
    securable_table: str | None
    detail: dict


class AuditEventPage(BaseModel):
    """One page of audit events, newest first.

    Parameters
    ----------
    items
        The events on this page.
    next_cursor
        Pass back as `?cursor=` to fetch the next page; `None` when this is
        the last page.
    """

    items: list[AuditEventOut]
    next_cursor: str | None


class TaskTokenScopeOut(BaseModel):
    """The scope a task-token caller is narrowed to.

    Parameters
    ----------
    environment
        The one environment the token may touch.
    asset_scope
        The declared asset patterns.
    """

    environment: str
    asset_scope: list[str]


class WhoamiOut(BaseModel):
    """Response body for `GET /api/v1/whoami`.

    Parameters
    ----------
    name
        The caller's principal name.
    type
        The caller's principal type.
    task_token
        The task token's scope when the caller authenticated with one;
        `None` for a PAT.
    """

    name: str
    type: Literal["USER", "SERVICE_ACCOUNT"]
    task_token: TaskTokenScopeOut | None


class EnvironmentStorageUpdate(BaseModel):
    """Request body for updating an environment's storage configuration.

    Parameters
    ----------
    options
        Replaces the environment's storage options entirely, if given.
    secret_ref
        Replaces the environment's storage secret reference, if given.
        Setting this requires `MANAGE_SECRET` on the environment, not just
        `MANAGE_ENVIRONMENT` — see `CatalogService.update_environment_storage`.
    """

    options: OptionalStorageOptions = None
    secret_ref: str | None = None


class RepositoryCreate(BaseModel):
    """Request body for registering a repository.

    Parameters
    ----------
    name
        Unique name; no dots.
    kind
        ``local``, ``https`` or ``ssh``.
    url
        ``https://…``, ``ssh://…``, ``[user@]host:path``, or an absolute path.
    credential_secret
        Secret name resolved in the target environment at deploy time.
    credential_username
        HTTPS username; default ``oauth2``.
    ssh_known_hosts
        ``known_hosts`` lines; required for ``ssh``.
    default_ref
        Ref deployed when none is given.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["local", "https", "ssh"]
    url: str
    credential_secret: str | None = None
    credential_username: str | None = None
    ssh_known_hosts: str | None = None
    default_ref: str | None = None


class RepositoryUpdate(BaseModel):
    """Request body for changing a repository; omitted fields stay, `null` clears.

    Parameters
    ----------
    url
        New URL.
    credential_secret
        New secret name.
    credential_username
        New HTTPS username.
    ssh_known_hosts
        New ``known_hosts`` lines.
    default_ref
        New default ref.
    """

    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    credential_secret: str | None = None
    credential_username: str | None = None
    ssh_known_hosts: str | None = None
    default_ref: str | None = None


class RepositoryOut(BaseModel):
    """Response body for a repository. Carries a secret's name, never a value.

    Parameters
    ----------
    id
        The repository's id.
    name
        Its name.
    kind
        ``local``, ``https`` or ``ssh``.
    url
        Its URL.
    credential_secret
        The bound secret's name.
    credential_username
        HTTPS username, or `None` for ``oauth2``.
    ssh_known_hosts
        Trusted host keys.
    default_ref
        Default ref.
    created_by
        Who registered it.
    created_at
        When.
    updated_at
        Last change.
    """

    id: uuid.UUID
    name: str
    kind: str
    url: str
    credential_secret: str | None
    credential_username: str | None
    ssh_known_hosts: str | None
    default_ref: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class PackageIndexCreate(BaseModel):
    """Request body for registering a package index.

    Parameters
    ----------
    name
        Unique name; no dots.
    url
        ``https://host/path`` of the index, without a user name or password.
    credential_secret
        Name of the secret that holds ``username`` and ``password``, per environment.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    url: str
    credential_secret: str | None = None


class PackageIndexUpdate(BaseModel):
    """Request body for changing a package index; omitted fields stay, `null` clears the secret.

    Parameters
    ----------
    url
        New URL.
    credential_secret
        New secret name.
    """

    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    credential_secret: str | None = None


class PackageIndexOut(BaseModel):
    """Response body for a package index. Carries a secret's name, never a value.

    Parameters
    ----------
    id
        The index's id.
    name
        Its name.
    url
        Its URL, in stored form.
    credential_secret
        The bound secret's name.
    created_by
        Who registered it.
    created_at
        When.
    updated_at
        Last change.
    """

    id: uuid.UUID
    name: str
    url: str
    credential_secret: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class DeployCreate(BaseModel):
    """Request body for a deploy, dry run or rollback.

    Parameters
    ----------
    repository
        Registered repository name.
    kind
        ``deploy`` (default), ``dry_run`` or ``rollback``.
    ref
        Deploy/dry run: ref or full SHA; default the repository's default ref, else ``HEAD``.
    to_sha
        Rollback: a full SHA already deployed to this environment.
    to_previous
        Rollback: the project version current before the present one.
    """

    model_config = ConfigDict(extra="forbid")

    repository: str
    kind: Literal["deploy", "dry_run", "rollback"] = "deploy"
    ref: str | None = None
    to_sha: str | None = None
    to_previous: StrictBool = False


class DeployAccepted(BaseModel):
    """Response body for an accepted deploy.

    Parameters
    ----------
    id
        The deploy run's id.
    status
        ``queued``.
    """

    id: uuid.UUID
    status: str


class DeployOut(BaseModel):
    """Response body for a deploy run.

    Parameters
    ----------
    id
        The run's id.
    environment
        Environment name.
    repository
        Repository name.
    kind
        ``deploy``, ``dry_run`` or ``rollback``.
    status
        ``queued``, ``running``, ``succeeded`` or ``failed``.
    requested_ref
        Ref or SHA asked for.
    resolved_sha
        The commit fetched.
    rollback_to_project_version_id
        A rollback's target.
    error_message
        Why it failed.
    diff
        Table names per change category.
    project_version_id
        The project version it made current.
    principal_id
        Who submitted it.
    created_at
        Queued at.
    started_at
        Claimed at.
    finished_at
        Finished at.
    hidden_adopted
        Adopted tables left out of `diff` because the caller may not read them.
    checks
        The checks the project has, table to ``[{"check", "on_failure"}]``; `None` for a run
        without checks.
    warnings
        What the deploy leaves for its owner to act on, as sentences (a removed asset whose
        table is kept after a failed check).
    """

    id: uuid.UUID
    environment: str
    repository: str
    kind: str
    status: str
    requested_ref: str | None
    resolved_sha: str | None
    rollback_to_project_version_id: uuid.UUID | None
    error_message: str | None
    diff: dict[str, list[str]] | None
    project_version_id: uuid.UUID | None
    principal_id: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    hidden_adopted: int = 0
    checks: dict[str, list[dict[str, str]]] | None = None
    warnings: list[str] = Field(default_factory=list)


class DeploymentOut(BaseModel):
    """The current project version of one repository.

    Parameters
    ----------
    repository
        Repository name.
    project_version_id
        Current project version.
    commit_sha
        Its commit.
    updated_at
        When the pointer moved.
    run_as
        The bound run principal, if any.
    """

    repository: str
    project_version_id: uuid.UUID
    commit_sha: str
    updated_at: datetime
    run_as: str | None = None


class RunAsIn(BaseModel):
    """Bind a run principal to a deployment.

    Parameters
    ----------
    principal
        Name of an active service account.
    """

    model_config = ConfigDict(extra="forbid")

    principal: str = Field(min_length=1, max_length=255)


class ProjectVersionOut(BaseModel):
    """A project version with its manifest filtered for the caller.

    Parameters
    ----------
    id
        The project version's id.
    environment
        Environment name.
    repository
        Repository name.
    commit_sha
        The commit.
    code_uri
        Code tarball location.
    code_sha256
        Its digest.
    deployed_by
        Who first deployed it.
    created_at
        When.
    manifest
        Assets, DAG edges, external inputs, resolved config; SQL withheld
        where the caller cannot read every referenced table.
    """

    id: uuid.UUID
    environment: str
    repository: str
    commit_sha: str
    code_uri: str
    code_sha256: str
    deployed_by: uuid.UUID | None
    created_at: datetime
    manifest: dict


class OrphanOut(BaseModel):
    """One orphaned table.

    Parameters
    ----------
    repository
        The repository that used to produce it.
    table
        Dotted table name.
    """

    repository: str
    table: str


class OrphansOut(BaseModel):
    """Orphans visible to the caller.

    Parameters
    ----------
    orphans
        Readable orphans.
    hidden
        Orphans the caller may not read.
    """

    orphans: list[OrphanOut]
    hidden: int


class RunCreate(BaseModel):
    """Start a run.

    Parameters
    ----------
    repository
        Repository name.
    select
        Shell-glob patterns over dotted table names; empty runs everything.
    upstream
        Add every ancestor of the matched assets.
    downstream
        Add every descendant of the matched assets.
    """

    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=1, max_length=255)
    select: list[str] = Field(default_factory=list, max_length=100)
    upstream: bool = False
    downstream: bool = False


class CheckResultOut(BaseModel):
    """The result of one data quality check. It holds a count, never a row of the table.

    Parameters
    ----------
    run_id
        The run.
    task
        The task (the checked table) it belongs to.
    name
        ``<table>/<check>``.
    check
        The check's short name.
    path
        Repository-relative path of the check's file.
    table
        The checked table, dotted.
    table_id
        The table's id; `None` when it was not resolved.
    on_failure
        ``block`` or ``warn``.
    outcome
        ``passed``, ``failed``, ``warned`` or ``errored``.
    failing_rows
        The count of rows that are wrong, when the check ran.
    error
        Why a check could not run.
    checked_snapshot_id
        The snapshot it read.
    rollback_outcome
        What the rollback after a failed blocking check did, if there was one.
    policy_applied
        Whether a row filter or column mask applied to the run's principal.
    policy_note
        Text saying what that means for the result, or `None`.
    query_run_id
        The query that ran the check.
    started_at
        Start.
    finished_at
        End.
    """

    run_id: uuid.UUID
    task: str
    name: str
    check: str
    path: str
    table: str
    table_id: uuid.UUID | None
    on_failure: str
    outcome: str
    failing_rows: int | None
    error: str | None
    checked_snapshot_id: int | None
    rollback_outcome: str | None
    policy_applied: bool
    policy_note: str | None
    query_run_id: uuid.UUID | None
    started_at: datetime
    finished_at: datetime


class CheckResultPage(BaseModel):
    """One page of check results, newest first.

    Parameters
    ----------
    items
        The results on this page.
    next_cursor
        Pass back as `?cursor=` to fetch the next page; `None` on the last page.
    """

    items: list[CheckResultOut]
    next_cursor: str | None


class FailureOut(BaseModel):
    """Why a task or an attempt failed.

    Parameters
    ----------
    code
        A stable code: `build_failed`, `code_changed`, `secret_refused`, `no_runtime`,
        `upstream_failed`, `upstream_missing`, `check_blocked`, `check_error`, `query_invalid`,
        `permission_denied`, `not_found`, `memory`, `timeout`, `cancelled`, `worker_lost`,
        `conflict` or `internal`; `unknown` for a failure recorded before the codes existed
        (never stored). A client treats a code it does not know like `internal`.
    cause
        One sentence: what happened. The sentences may be reworded; the codes do not change.
    next_step
        One sentence: what to do about it.
    """

    code: str
    cause: str
    next_step: str


class TaskRunOut(BaseModel):
    """One task run.

    Parameters
    ----------
    asset
        Dotted table name.
    status
        Task status.
    attempt
        Attempts counted.
    max_attempts
        Retries + 1.
    timeout_seconds
        Per-attempt limit.
    next_attempt_at
        When a pending retry may start.
    started_at
        First claim.
    finished_at
        Final state.
    error_message
        Last failure, as free text: the detail of `failure`.
    query_run_id
        The write that executed the last attempt.
    rows_written
        Rows it wrote.
    failure
        Why the task failed, was cancelled or never ran, with its stable code, the cause and the
        next step (`FailureOut`); `null` for a task with no error. A task that failed before the
        codes existed has the code `unknown`.
    waiting_for
        Why a `pending` or `ready` task of a running run has not started, or `null`: a stable
        `code` (`upstream`, `retry`, `setting_back`, `no_worker`, `no_worker_of_kind`,
        `workers_busy` or `starting`), one sentence in `message`, and the numbers or names it
        uses. A task is pipeline work: `no_worker_of_kind` means workers are running and none
        takes pipeline work (`workers` is 0, `other_workers` the workers running), and `workers` in
        `workers_busy` counts the workers that take pipeline work. For `upstream`, `upstream`
        lists the first 50 unfinished upstream tasks (raw names) and `upstream_count` is how
        many there are in all; the sentence names five, escaped.
    kind
        ``asset`` for a table the run builds, ``source_check`` for a table it only checks.
    checks
        The task's check results, by name.
    kept_check
        ``true`` for a ``source_check`` task that checks a table the project builds, because
        that table was kept after a failed check; ``false`` for every other task; ``null`` for a
        ``source_check`` task of a run whose project version is gone, which cannot tell.
    setting_back
        ``true`` while the task has not ended and its next claim only sets its table back:
        its worker was lost after it may have written the table, or its run was cancelled
        while it waited with rows no check had passed. The task is then ``ready``
        (`waiting_for` has the code `setting_back`), ``running`` with that claim, or
        ``pending`` again after that claim's worker drained. ``false`` once it has ended.
    """

    asset: str
    status: str
    attempt: int
    max_attempts: int
    timeout_seconds: int
    next_attempt_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    query_run_id: uuid.UUID | None
    rows_written: int | None
    waiting_for: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Why this is waiting, or null when it is not: a stable `code`, one sentence in "
            "`message`, and the numbers the sentence uses."
        ),
    )
    kind: str = "asset"
    checks: list[CheckResultOut] = Field(default_factory=list)
    kept_check: bool | None = False
    setting_back: bool = False
    failure: FailureOut | None = None


class NotificationOut(BaseModel):
    """One notification of a run.

    Parameters
    ----------
    id
        The delivery id: the ``delivery_id`` a receiver sees, and what
        ``GET /environments/{env}/notifications/{id}`` takes.
    event
        ``failed``, ``recovered`` or ``test``.
    state
        ``pending``, ``delivered`` or ``failed``.
    attempts
        Attempts made so far.
    max_attempts
        Attempts before the delivery counts as failed.
    last_result
        A short text on the last attempt; never the URL.
    delivered_at
        When it was delivered.
    """

    id: uuid.UUID
    event: str
    state: str
    attempts: int
    max_attempts: int
    last_result: str | None
    delivered_at: datetime | None


class DeliveryOut(NotificationOut):
    """One notification delivery on its own, such as the test of a webhook.

    Parameters
    ----------
    job
        The job.
    repository
        Repository name.
    next_attempt_at
        When the next attempt is due; null once the delivery is over.
    """

    job: str
    repository: str
    next_attempt_at: datetime | None


class NotifyTestOut(BaseModel):
    """The test of a webhook, queued.

    Parameters
    ----------
    delivery_id
        The delivery to read with ``GET /environments/{env}/notifications/{delivery_id}``.
    """

    delivery_id: uuid.UUID


class RunOut(BaseModel):
    """One run.

    Parameters
    ----------
    id
        Run id.
    environment
        Environment name.
    repository
        Repository name.
    project_version_id
        The project version it runs.
    commit_sha
        Its commit.
    kind
        ``full``, ``selection`` or ``resume``.
    status
        Run status.
    cancel_requested
        Whether a cancel was requested.
    triggered_by
        Who started it.
    run_as
        Who it writes as.
    resumed_from_run_id
        The run a resume continues.
    selection
        Patterns and flags.
    error_message
        Run-level failure.
    created_at
        Created.
    started_at
        Started.
    finished_at
        Finished.
    tasks
        Task runs (empty in listings).
    job
        The job the run belongs to; null for an ad-hoc run.
    trigger
        ``schedule``, ``api`` (a service account called the API) or ``manual``; null for an
        ad-hoc run.
    scheduled_for
        For a scheduled run, the time the schedule named.
    missed
        How many earlier scheduled times this run stands in for.
    notifications
        The notifications the run queued, oldest first (empty in listings).
    failed_tasks
        How many tasks ended ``failed`` (not ``upstream_failed`` or ``cancelled``); also in
        listings.
    """

    id: uuid.UUID
    environment: str
    repository: str
    project_version_id: uuid.UUID
    commit_sha: str
    kind: str
    status: str
    cancel_requested: bool
    triggered_by: str | None
    run_as: str | None
    resumed_from_run_id: uuid.UUID | None
    selection: dict[str, Any]
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    tasks: list[TaskRunOut]
    job: str | None = None
    trigger: str | None = None
    scheduled_for: datetime | None = None
    missed: int = 0
    notifications: list[NotificationOut] = Field(default_factory=list)
    failed_tasks: int = 0


class JobRunRequest(BaseModel):
    """Name the repository of a job for a start, a pause or a resume.

    Parameters
    ----------
    repository
        Repository name.
    """

    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=1, max_length=255)


class WaitingStartOut(BaseModel):
    """A start of a job that waits for the active run of its project.

    Parameters
    ----------
    job
        The job.
    waits_for_run_id
        The active run it waits for.
    trigger
        ``schedule``, ``api`` or ``manual``.
    requested_at
        When it was requested.
    requested_by
        Who asked, if that principal still exists; null for a scheduled start.
    scheduled_for
        For a scheduled start, the time the schedule named.
    missed
        How many earlier scheduled times the start stands in for.
    merged
        How many starts it stands for.
    message
        One sentence for a person: what it waits for and that it starts then.
    """

    job: str
    waits_for_run_id: uuid.UUID
    trigger: str
    requested_at: datetime
    requested_by: str | None
    scheduled_for: datetime | None
    missed: int
    merged: int
    message: str


class JobStartOut(BaseModel):
    """What a start of a job did: the run (201) or the waiting start (202).

    Parameters
    ----------
    run
        The queued run, or null when the start waits.
    waiting
        The waiting start, or null when a run was queued.
    """

    run: RunOut | None
    waiting: WaitingStartOut | None


class ScheduleTriggerOut(BaseModel):
    """A trigger that starts the job at the times a cron expression names.

    Parameters
    ----------
    kind
        Always ``schedule``.
    cron
        The cron expression.
    timezone
        The schedule's IANA zone.
    paused
        Whether the schedule is paused.
    next_run_at
        The next scheduled time (UTC), or null for a paused schedule.
    """

    kind: Literal["schedule"] = "schedule"
    cron: str
    timezone: str
    paused: bool
    next_run_at: datetime | None


class OnDemandTriggerOut(BaseModel):
    """The trigger every job has: a start through the API, the SDK or the CLI.

    Parameters
    ----------
    kind
        Always ``on_demand``.
    """

    kind: Literal["on_demand"] = "on_demand"


TriggerOut = Annotated[ScheduleTriggerOut | OnDemandTriggerOut, Field(discriminator="kind")]
"""One way a job starts. A client must ignore a ``kind`` it does not know: kinds may be added."""


class JobOut(BaseModel):
    """One job and the platform's state for it.

    Parameters
    ----------
    name
        The job's name.
    environment
        Environment name.
    repository
        Repository name.
    select
        The selection's patterns.
    upstream
        Whether ancestors are added.
    downstream
        Whether descendants are added.
    cron
        The cron expression, or null for a job without a schedule here.
    timezone
        The schedule's IANA zone, or null without a schedule.
    state
        ``scheduled``, ``paused``, ``waiting`` (a start waits for the active run),
        ``waiting to start`` (a start waits and the project is free), ``no schedule``,
        ``no run principal`` or ``run principal unusable``.
    paused
        Whether the schedule is paused.
    next_run_at
        The next scheduled time (UTC), or null.
    next_run_local
        The same moment in the job's zone, ISO 8601 with the offset.
    waiting_for
        Why a start waits (`job_waits`, `job_starts_soon`) or why the schedule cannot start
        (`no_run_principal`, `run_principal_unusable`): a stable `code` and one sentence in
        `message`; null otherwise.
    waits_for_run_id
        For ``waiting``, the run it waits for.
    last_run_id
        The job's latest run, if any.
    last_run_status
        Its status.
    last_run_trigger
        What started it.
    last_run_at
        When it was created.
    triggers
        How the job starts, schedule first: a ``schedule`` trigger when the job has a schedule
        in this environment, and the ``on_demand`` trigger every job has. Kinds may be added.
    """

    name: str
    environment: str
    repository: str
    select: list[str]
    upstream: bool
    downstream: bool
    cron: str | None
    timezone: str | None
    state: str
    paused: bool
    next_run_at: datetime | None
    next_run_local: str | None
    waiting_for: dict[str, Any] | None
    waits_for_run_id: uuid.UUID | None
    last_run_id: uuid.UUID | None
    last_run_status: str | None
    last_run_trigger: str | None
    last_run_at: datetime | None
    triggers: list[TriggerOut]


class TaskAttemptOut(BaseModel):
    """One attempt of a task run.

    Parameters
    ----------
    attempt
        Attempt number.
    worker
        Worker id.
    started_at
        Start.
    finished_at
        End.
    outcome
        How it ended.
    rows_written
        Rows written.
    error_message
        Why it failed, as free text.
    failure
        The code, cause and next step of an attempt that did not succeed (`FailureOut`);
        `null` for an attempt that succeeded or is running.
    """

    attempt: int
    worker: str
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    rows_written: int | None
    error_message: str | None
    failure: FailureOut | None = None


class TaskLogOut(BaseModel):
    """A task's logs.

    Parameters
    ----------
    run_id
        The run.
    asset
        The task.
    text
        Free-text log.
    attempts
        Structured attempts.
    next_offset
        The byte offset of the end of `text` in the stored log; send it as `offset` to
        read only what comes after.
    more
        Whether stored bytes remain after `next_offset` (one response is cut at 1 MiB).
    complete
        Whether the task has finished and `text` reaches the end of its log.
    reset
        Whether the requested `offset` lay beyond the end of the log, which only happens when
        the log is shorter than before; start again from `next_offset`.
    """

    run_id: uuid.UUID
    asset: str
    text: str
    attempts: list[TaskAttemptOut]
    next_offset: int
    more: bool
    complete: bool
    reset: bool


class QueryListOut(BaseModel):
    """One row of the list of an environment's queries.

    Never the SQL text, an error message, lineage, a result location or the worker.

    Parameters
    ----------
    id
        The query's id.
    submitter
        The submitting principal's name, `null` when it was deleted.
    kind
        `read`, `create`, `replace`, `append`, `view` or `maintain`.
    target
        A write run's target table, `null` for a read.
    status
        `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED` or `CANCELLED`.
    created_at
        When it was submitted.
    started_at
        When a worker started it.
    finished_at
        When it ended.
    error_cause
        Why a `FAILED` or `CANCELLED` run ended, as on the status read; never the message.
    waiting_for
        Why a `QUEUED` query has not started, as on the status read (including `user_limit` and
        `no_worker_of_kind`); `null` for other statuses. On another principal's row a manager
        reads `limit` and never the number running.
    """

    id: str
    submitter: str | None
    kind: str
    target: str | None
    status: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_cause: str | None
    waiting_for: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Why this is waiting, or null when it is not: a stable `code`, one sentence in "
            "`message`, and the numbers the sentence uses."
        ),
    )


class WorkerOut(BaseModel):
    """A registered worker.

    Parameters
    ----------
    id
        The worker's id.
    host
        Its host name.
    version
        The Lakana version it runs.
    started_at
        When it registered.
    heartbeat_at
        Its last heartbeat.
    status
        `alive` (heartbeat younger than the lease) or `lost`.
    takes
        The work it takes: `all`, `interactive` (ad-hoc queries) or `pipeline` (deploys, pipeline
        tasks and table maintenance).
    query_run_id
        Its oldest running query, if any.
    deploy_run_id
        Its oldest running deploy, if any.
    task_run_id
        The running task whose lease it holds, if any.
    """

    id: str
    host: str
    version: str
    started_at: datetime
    heartbeat_at: datetime
    status: str
    takes: str
    query_run_id: str | None
    deploy_run_id: str | None
    task_run_id: str | None


class SchedulerOut(BaseModel):
    """The scheduler role and the queue.

    Parameters
    ----------
    holder
        Current holder.
    heartbeat_at
        Its last heartbeat.
    active_runs
        Queued and running runs.
    pending
        Pending task runs.
    ready
        Ready task runs.
    running
        Running task runs.
    """

    holder: str | None
    heartbeat_at: datetime | None
    active_runs: int
    pending: int
    ready: int
    running: int


_MAX_VERSION = 2**31 - 1
"""Largest version number: the catalog column is a 32-bit integer."""

"""A namespace path (1024) and an object name (255) joined by a dot."""


class ArtifactUploadOut(BaseModel):
    """An open upload (`POST .../artifacts/{name}/uploads`).

    Parameters
    ----------
    upload_id
        The upload's id, for the blob and commit requests.
    missing
        The SHA-256 of each file the store does not have yet; send exactly these.
    expires_at
        When the upload stops being usable.
    """

    upload_id: str
    missing: list[str]
    expires_at: datetime


class ArtifactBlobOut(BaseModel):
    """The outcome of one file's upload.

    Parameters
    ----------
    sha256
        The file's SHA-256.
    stored
        `True` if this request stored it; `False` if the artifact already had it.
    """

    sha256: str
    stored: bool


class ArtifactSourceIn(BaseModel):
    """One thing an artifact version was made from, for lineage.

    Parameters
    ----------
    name
        Dotted name of a table, view or artifact.
    version
        The artifact version that was read; omitted records the current one.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_SOURCE_NAME)
    version: Annotated[int, Field(strict=True, ge=1, le=_MAX_VERSION)] | None = None


class ArtifactCommitIn(BaseModel):
    """Body of the commit request.

    Parameters
    ----------
    sources
        What the version was made from; each name once, at most `MAX_SOURCE_VERSIONS`.
    """

    model_config = ConfigDict(extra="forbid")

    sources: list[ArtifactSourceIn] = Field(default_factory=list, max_length=MAX_SOURCE_VERSIONS)

    @model_validator(mode="after")
    def _each_source_once(self) -> ArtifactCommitIn:
        """Refuse a source named twice.

        Returns
        -------
        ArtifactCommitIn
            This model.

        Raises
        ------
        ValueError
            If two sources have one name.
        """
        names = [source.name for source in self.sources]
        if len(set(names)) != len(names):
            raise ValueError("name each source once, with at most one version")
        return self


class ArtifactOut(BaseModel):
    """One artifact, as a listing or a lookup shows it.

    Parameters
    ----------
    name
        The artifact's name.
    namespace
        Dot-joined namespace path.
    uuid
        The artifact's id.
    kind
        Always ``ARTIFACT``.
    current_version
        The highest live version; `None` while it has none.
    versions
        How many versions it has had, deleted ones included.
    size_bytes
        The summed size of its live versions.
    created_at
        When it was created.
    """

    name: str
    namespace: str
    uuid: str
    kind: Literal["ARTIFACT"]
    current_version: int | None
    versions: int
    size_bytes: int
    created_at: datetime


class ArtifactFileOut(BaseModel):
    """One file of an artifact version.

    Parameters
    ----------
    path
        Its path inside the artifact, `/`-separated.
    size
        Its size in bytes.
    sha256
        The SHA-256 of its contents.
    """

    path: str
    size: int
    sha256: str


class ArtifactVersionOut(BaseModel):
    """One version of an artifact.

    Parameters
    ----------
    version
        The number; versions can have gaps, since a number is never reused.
    manifest_sha256
        The digest of the canonical manifest: the identity of the content.
    size_bytes
        The summed size of its files.
    file_count
        The number of its files.
    is_directory
        Whether it is a directory (otherwise one file).
    task_run_id
        The task run that committed it, if one did.
    run_id
        That task run's run.
    project_version_id
        The project version the run executed.
    commit_sha
        The commit of that project version.
    created_by
        The name of the principal that committed it.
    created_at
        When it was committed.
    deleted_at
        When it was deleted; `None` while it is live.
    deleted_by
        The name of the principal that deleted it.
    files
        Its files; empty in a listing and for a deleted version.
    deleted_versions
        The numbers of the versions this commit pruned (`keep_versions`).
    """

    version: int
    manifest_sha256: str
    size_bytes: int
    file_count: int
    is_directory: bool
    task_run_id: str | None
    run_id: str | None
    project_version_id: str | None
    commit_sha: str | None
    created_by: str | None
    created_at: datetime
    deleted_at: datetime | None
    deleted_by: str | None
    files: list[ArtifactFileOut]
    deleted_versions: list[int] = Field(default_factory=list)


class ArtifactKeptOut(BaseModel):
    """Files a delete left in storage, for one reason.

    Parameters
    ----------
    reason
        ``other_version`` (a live version also has them), ``open_upload`` (an open upload of the
        artifact names them) or ``left_to_sweep`` (this request did not remove them and the
        server's periodic clean-up decides).
    files
        How many stored files; identical files of one version count once.
    bytes
        Their summed size.
    version
        For ``other_version``, the newest live version that has them, and only for a caller who
        may read the artifact; `None` otherwise.
    """

    reason: Literal["other_version", "open_upload", "left_to_sweep"]
    files: int
    bytes: int
    version: int | None


class ArtifactVersionDeletedOut(ArtifactVersionOut):
    """The answer to deleting a version: the version, and what happened to its files.

    The fields of `ArtifactVersionOut` come first, so a client that reads only those still
    works. The counts are of this request's delete.

    Parameters
    ----------
    freed_files
        Stored files this request removed from storage.
    freed_bytes
        Their summed size.
    kept_files
        Stored files that stayed in storage.
    kept_bytes
        Their summed size.
    kept
        The files that stayed, by reason; empty when none did.
    """

    freed_files: int
    freed_bytes: int
    kept_files: int
    kept_bytes: int
    kept: list[ArtifactKeptOut]


class ArtifactUploadItemOut(BaseModel):
    """One open upload of the caller's, as `GET .../artifacts/uploads` lists it.

    Parameters
    ----------
    upload_id
        The upload's id, for the withdraw route.
    artifact
        The artifact it adds a version to, as ``namespace.name``.
    created_at
        When it was begun.
    expires_at
        When it stops being usable and frees its place.
    """

    upload_id: str
    artifact: str
    created_at: datetime
    expires_at: datetime


class ArtifactUploadPage(BaseModel):
    """One page of the caller's open uploads, newest first.

    Parameters
    ----------
    items
        The uploads on this page.
    next_cursor
        Pass back as `?cursor=` to fetch the next page; `None` on the last page.
    """

    items: list[ArtifactUploadItemOut]
    next_cursor: str | None


class SearchHitOut(BaseModel):
    """One search hit.

    Parameters
    ----------
    kind
        What was found: ``table``, ``column``, ``run`` or ``principal``.
    name
        A display name: ``namespace.table``, ``namespace.table.column``, the run id or the
        principal's name.
    matched
        The field that matched: ``name``, ``description``, ``id``, ``job`` or ``repository``.
    snippet
        For a description match, at most 160 characters of the description around the hit.
    namespace
        For a table or column, its namespace, dotted.
    table
        For a table or column, the table's name.
    table_kind
        For a table, ``TABLE``, ``EXTERNAL``, ``VIEW`` or ``ARTIFACT``.
    column
        For a column, its name.
    column_type
        For a column, its type.
    run_id
        For a run, its id.
    job
        For a run, its job, if it has one.
    repository
        For a run, its repository.
    status
        For a run, its status.
    principal
        For a principal, its name.
    principal_type
        For a principal, ``USER`` or ``SERVICE_ACCOUNT``.
    """

    model_config = ConfigDict(from_attributes=True)

    kind: Literal["table", "column", "run", "principal"]
    name: str
    matched: Literal["name", "description", "id", "job", "repository"]
    snippet: str | None
    namespace: str | None
    table: str | None
    table_kind: str | None
    column: str | None
    column_type: str | None
    run_id: uuid.UUID | None
    job: str | None
    repository: str | None
    status: str | None
    principal: str | None
    principal_type: str | None


class SearchOut(BaseModel):
    """One page of search hits.

    Parameters
    ----------
    results
        The hits, best first: exact matches, then prefix, then substring, then description
        matches; within each, tables before columns before runs before principals.
    limit
        The page size asked for.
    offset
        How many hits were skipped.
    has_more
        Whether a hit follows this page.
    """

    model_config = ConfigDict(from_attributes=True)

    results: list[SearchHitOut]
    limit: int
    offset: int
    has_more: bool
