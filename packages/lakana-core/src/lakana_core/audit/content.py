"""Which fields of an audit event are content, and how much query text is stored.

A field of an event's `detail` is either **shown** to every reader of the audit log (a name, an
id, a count, a fixed reason) or **content**: a value that can carry anything, today the SQL text
of `QUERY_COMPLETED` and the watermark (a column value) of `TABLE_WRITE`. `CONTENT_FIELDS` and
`SHOWN_FIELDS` are the one list of that decision. `AuditService.query` hides the content of
another principal's event from a reader who does not hold `READ_QUERY_TEXT`, replacing each such
value with `fingerprint` of it; a reader's own events are always shown in full.

A field in neither list is hidden like content, so a field added to an event without a decision
never shows. `AuditService.record` warns (`UnclassifiedAuditFieldWarning`) when it writes such a
field; the test suite turns that warning into an error (`pyproject.toml`), so a new field fails
the tests of the code that writes it until it is classified here.

`LAKANA_AUDIT__QUERY_TEXT` decides what a new `QUERY_COMPLETED` stores of the text
(`stored_query_text`); each process installs it once at start (`install_query_text`). Events
already written keep what they stored.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import warnings
from collections.abc import Mapping
from types import MappingProxyType

from lakana_core.config import QUERY_TEXT_MODES
from lakana_core.models import AuditEventType

CONTENT_FIELDS: Mapping[AuditEventType, frozenset[str]] = MappingProxyType(
    {
        AuditEventType.QUERY_COMPLETED: frozenset({"sql_text"}),
        # the highest value of an incremental write's key column: any column value (#239)
        AuditEventType.TABLE_WRITE: frozenset({"watermark"}),
    }
)
"""The fields of each event type that hold content, hidden without `READ_QUERY_TEXT`.

Content is a query's text or a value read from a table's columns: a reader of the audit log
need not hold `READ_TABLE` on the table it came from. A break-glass `reason` is not here: it is
written for the reviewer of the audit log, which is what it is for. A table or column
description is never in an event (only its lengths); a schema diff holds column names and types
only; a check's failure holds the count of rows, never the rows.
"""

SHOWN_FIELDS: frozenset[str] = frozenset(
    {
        # who and what
        "action",
        "actor",
        "artifact",
        "artifact_id",
        "asset_scope",
        "check",
        "check_result_id",
        "checks",
        "claim",
        "column",
        "credential_secret",
        "delivery_id",
        "deploy_id",
        "dropped_name",
        "environment",
        "event",
        "grant_id",
        "group",
        "group_id",
        "holder_run_id",
        "id",
        "issuer",
        "job",
        "key_id",
        "kind",
        "location",
        "locations",
        "manifest_sha256",
        "mode",
        "name",
        "operation",
        "path",
        "principal",
        "principal_id",
        "project_version_id",
        "query_id",
        "ref",
        "repository",
        "requested_by",
        "resumed_from_run_id",
        "role",
        "run_as",
        "run_id",
        "sha",
        "storage_root",
        "subject",
        "submitted_by",
        "table",
        "table_id",
        "target",
        "task_run_id",
        "type",
        "url",
        "use",
        "version",
        "waits_for_run_id",
        # states, outcomes and fixed reasons
        "already_deleted",
        "complete",
        "conflict",
        "in_run",
        "is_protected",
        "kept_not_restored",
        "kept_reason",
        "kept_restored",
        "kept_unknown",
        "note",
        "outcome",
        "problem",
        "protected",
        "purge",
        "reason",
        "replaced",
        "request_trigger",
        "reset",
        "secret_ref_set",
        "shortened",
        "state",
        "status",
        "stopped",
        "trigger",
        "unchecked",
        "view",
        "why",
        # counts, sizes and times
        "after",
        "before",
        "bytes",
        "bytes_deleted",
        "claimed_at",
        "count",
        "counts",
        "days",
        "deleted_at",
        "diff",
        "distinct_others",
        "dropped_at",
        "dry_run",
        "expires_at",
        "file_count",
        "files",
        "files_deleted",
        "freed_files",
        "keep_days",
        "keep_last",
        "kept",
        "merged",
        "missed",
        "more_locations",
        "new_length",
        "old_length",
        "older_than_days",
        "on",
        "others_shown",
        "previous",
        "previous_days",
        "purge_after",
        "restored_policies",
        "retention",
        "retention_cleared",
        "revoked_policies",
        "row_count",
        "rows",
        "rows_shown",
        "scheduled_for",
        "size_bytes",
        "snapshot_id",
        "snapshots_expired",
        "suppressed",
        "tables",
        "tables_touched",
        "tables_without_tombstone",
        "tags_removed",
        "tasks",
        "versions",
        # settings and changes
        "approved_options",
        "changed",
        "filters",
        "options_keys",
        "schema_after",
        "schema_before",
        "schema_diff",
        "targeting_key",
    }
)
"""The fields every reader of the audit log sees: names, ids, counts, times and fixed reasons.

A field name means the same in every event that has it. A field that is content in some event
type is listed in `CONTENT_FIELDS` for it and never here.
"""


class UnclassifiedAuditFieldWarning(UserWarning):
    """An audit event was written with a field that neither list classifies.

    The field is stored as given and hidden like content from a reader without
    `READ_QUERY_TEXT`. Add it to `CONTENT_FIELDS` or `SHOWN_FIELDS`.
    """


_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")

_QUERY_TEXT: str = "full"
"""What a new `QUERY_COMPLETED` stores of its SQL text; set once by `install_query_text`."""


def install_query_text(mode: str) -> None:
    """Set what this process's new `QUERY_COMPLETED` events store of their SQL text.

    The server and the worker call it once at start with `LAKANA_AUDIT__QUERY_TEXT`.

    Parameters
    ----------
    mode
        ``full``, ``hash`` or ``off`` (`lakana_core.config.AuditConfig`, which validates it).

    Raises
    ------
    ValueError
        If `mode` is not one of `QUERY_TEXT_MODES`.
    """
    global _QUERY_TEXT
    if mode not in QUERY_TEXT_MODES:
        raise ValueError(f"The query text mode must be full, hash or off, not {mode!r}.")
    _QUERY_TEXT = mode


def fingerprint(text: str) -> dict[str, object]:
    """Describe a text by its SHA-256 and length, without the text.

    The same text always gives the same fingerprint, so a reader can tell that two events hold
    the same query; it is not a secret: who guesses the exact text can confirm the guess.

    Parameters
    ----------
    text
        The text.

    Returns
    -------
    dict
        ``{"sha256": <hex of its UTF-8 bytes>, "length": <characters>}``.
    """
    return {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "length": len(text)}


def stored_query_text(sql: str) -> str | dict[str, object] | None:
    """Give what a new `QUERY_COMPLETED` event stores of a query's text.

    Parameters
    ----------
    sql
        The query's text.

    Returns
    -------
    str or dict or None
        The text (``full``), its `fingerprint` (``hash``), or `None` (``off``), by the mode
        `install_query_text` set.
    """
    if _QUERY_TEXT == "full":
        return sql
    if _QUERY_TEXT == "hash":
        return fingerprint(sql)
    return None


def _is_fingerprint(value: object) -> bool:
    """Say whether a stored value is already a `fingerprint`.

    Parameters
    ----------
    value
        A value from an event's detail.

    Returns
    -------
    bool
        `True` for a dict of exactly ``sha256`` (64 hexadecimal digits) and ``length`` (a whole
        number), which can carry no text.
    """
    if not isinstance(value, dict) or set(value) != {"sha256", "length"}:
        return False
    digest, length = value["sha256"], value["length"]
    return (
        isinstance(digest, str)
        and _HEX_DIGEST.fullmatch(digest) is not None
        and isinstance(length, int)
        and not isinstance(length, bool)
    )


def unclassified_fields(event_type: AuditEventType, detail: Mapping[str, object]) -> list[str]:
    """Name the fields of a detail that neither list classifies.

    Parameters
    ----------
    event_type
        The event's type.
    detail
        Its detail.

    Returns
    -------
    list of str
        The field names in neither `CONTENT_FIELDS[event_type]` nor `SHOWN_FIELDS`, sorted.
    """
    content = CONTENT_FIELDS.get(event_type, frozenset())
    return sorted(key for key in detail if key not in content and key not in SHOWN_FIELDS)


def warn_unclassified(event_type: AuditEventType, detail: Mapping[str, object]) -> None:
    """Warn about the fields of a new event that neither list classifies.

    Parameters
    ----------
    event_type
        The event's type.
    detail
        Its detail.
    """
    unknown = unclassified_fields(event_type, detail)
    if unknown:
        warnings.warn(
            f"The audit event {event_type.value} has fields that are neither content nor shown: "
            f"{unknown}. Add each to CONTENT_FIELDS or SHOWN_FIELDS in "
            "lakana_core.audit.content; until then a reader without READ_QUERY_TEXT sees only "
            "their SHA-256 and length.",
            UnclassifiedAuditFieldWarning,
            stacklevel=3,
        )


def hidden_fields(event_type: AuditEventType, detail: Mapping[str, object]) -> list[str]:
    """Name the fields of another principal's event that a reader without the right must not see.

    Parameters
    ----------
    event_type
        The event's type.
    detail
        Its detail.

    Returns
    -------
    list of str
        Its content fields that it has, and every field that is not classified.
    """
    content = CONTENT_FIELDS.get(event_type, frozenset())
    return sorted(key for key in detail if key in content or key not in SHOWN_FIELDS)


def shown_detail(detail: Mapping[str, object], hidden: list[str]) -> dict[str, object]:
    """Copy a detail for a reader, with each hidden field replaced by its fingerprint.

    The copy is deep, so changing the answer never changes the stored row. A hidden value that
    is `None` (nothing was stored) or already a fingerprint (stored with
    ``LAKANA_AUDIT__QUERY_TEXT=hash``) stays as it is; one that is not text is fingerprinted as
    its JSON.

    Parameters
    ----------
    detail
        The stored detail.
    hidden
        The fields to hide (`hidden_fields`), or none.

    Returns
    -------
    dict
        The detail to show.
    """
    shown = copy.deepcopy(dict(detail))
    for key in hidden:
        value = shown[key]
        if value is None or _is_fingerprint(value):
            continue
        text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
        shown[key] = fingerprint(text)
    return shown
