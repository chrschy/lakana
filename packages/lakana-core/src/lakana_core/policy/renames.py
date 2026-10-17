"""Finding a row filter or column mask whose column a rename may have moved.

Policies bind to column names, while an Iceberg table is read by field id. A rename through
the REST catalog therefore changes which column a policy covers without any error: a mask on
`ssn` follows the name when `email` is renamed to `ssn`. Each policy records the id of the
table schema it was written against; a read compares the schemas from then on and refuses when
a name the policy uses changed owner.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pyiceberg.table.metadata import TableMetadata

from lakana_core.identifiers import shown

POLICY_COLUMN_RENAMED = (
    "`{table}` has a row filter or column mask on column `{column}`, and a column was renamed "
    "since the policy was created, so the policy may now cover a different column. Nothing is "
    "shown. An administrator must review the table's row filters and column masks, revoke each "
    "one that names a renamed column, and create it again for the column it is meant for."
)
"""Why a read of a table is refused when a rename touched a column of one of its policies."""


@dataclass(frozen=True)
class PolicyColumns:
    """The columns one policy uses and the schema it was written against.

    Parameters
    ----------
    columns
        The names the policy uses: a mask's own column and every column its expression reads,
        or every column a row filter reads, as the policy spells them.
    schema_id
        The id of the table's schema when the policy was created; `None` for a policy that
        records none, which is judged from the table's oldest schema.
    """

    columns: frozenset[str]
    schema_id: int | None


def policy_column_renamed_message(column: str, table: str) -> str:
    """Word the refusal of a read after a rename touched a policy's column.

    Parameters
    ----------
    column
        The policy's column, as the policy spells it.
    table
        The table's dotted name, as the query addressed it.

    Returns
    -------
    str
        `POLICY_COLUMN_RENAMED` with the names escaped.
    """
    return POLICY_COLUMN_RENAMED.format(column=shown(column), table=shown(table))


def _touched_by(metadata: TableMetadata, policy: PolicyColumns) -> set[str]:
    """Find the names of a policy that changed owner or were renamed since its schema.

    Parameters
    ----------
    metadata
        The table's metadata, with every schema it lists.
    policy
        The policy.

    Returns
    -------
    set of str
        The policy's names, as it spells them, that a field id lost or gained between its
        schema and the current one; empty when none did.
    """
    spelled = {name.lower(): name for name in policy.columns}
    ordered = sorted(metadata.schemas, key=lambda schema: schema.schema_id)
    start = ordered[0].schema_id if policy.schema_id is None else policy.schema_id
    steps = [schema for schema in ordered if schema.schema_id >= start]
    current = metadata.schema()
    if all(schema.schema_id != current.schema_id for schema in steps):
        steps.append(current)
    touched: set[str] = set()
    owner: dict[str, int] = {}
    previous: dict[int, str] | None = None
    for schema in steps:
        names = {field.field_id: field.name.lower() for field in schema.fields}
        for field_id, name in names.items():
            if name in spelled and owner.setdefault(name, field_id) != field_id:
                touched.add(spelled[name])
        if previous is not None:
            for field_id, name in names.items():
                before = previous.get(field_id)
                if before is not None and before != name:
                    touched.update(spelled[n] for n in (before, name) if n in spelled)
        previous = names
    return touched


def renamed_policy_column(metadata: TableMetadata, policies: Iterable[PolicyColumns]) -> str | None:
    """Find a policy column that a rename may have moved onto another column.

    Between a policy's schema and the current one, a field id that changed its name makes both
    names suspect, and so does a name that now belongs to another field id (a column dropped,
    a different one added under its name). The comparison ignores letter case, as the database
    does. Each policy is judged from its own schema.

    Parameters
    ----------
    metadata
        The table's metadata, with every schema it lists.
    policies
        The policies that apply to the reader.

    Returns
    -------
    str or None
        The first affected name in sorted order, as its policy spells it; `None` when no
        policy is affected.
    """
    touched: set[str] = set()
    for policy in policies:
        touched |= _touched_by(metadata, policy)
    if not touched:
        return None
    return min(touched, key=lambda name: (name.lower(), name))
