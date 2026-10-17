"""Reading a table's columns for the policies that name them: column masks and row filters."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, Protocol

from lakana_core.auth import PrincipalRef
from lakana_core.errors import ConflictError, LakanaError, NotFoundError, PermissionDenied
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.policy.expressions import QualifiedColumnError, validate_restricted_expression

if TYPE_CHECKING:
    from lakana_core.catalog.service import TableDescription

logger = logging.getLogger(__name__)

MAX_LISTED_COLUMNS = 20
"""How many of a table's columns an error message lists before it counts the rest."""


class ColumnSource(Protocol):
    """Whatever can say which columns a table has: `CatalogService` does."""

    def describe_table(self, p: PrincipalRef, ref: TableRef) -> TableDescription:
        """Describe a table, an external table or a view.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table reference.

        Returns
        -------
        TableDescription
            The table, including its columns.
        """
        ...


def read_table_description(
    catalog: ColumnSource, p: PrincipalRef, ref: TableRef, *, policy: str
) -> TableDescription:
    """Describe a table as the caller may, for the policy that is being created.

    Parameters
    ----------
    catalog
        Where to read the table's current columns from.
    p
        The acting principal, already authorized to manage policies.
    ref
        The table the policy is for.
    policy
        What is being created, for the next step in a message: `"mask"` or `"row filter"`.

    Returns
    -------
    TableDescription
        The table, with its columns and, for a managed table, its current schema id.

    Raises
    ------
    PermissionDenied
        If `p` lacks READ_TABLE on the table, which reading its columns needs.
    ConflictError
        If the table has no schema yet, or its columns could not be read.
    """
    dotted = f"{ref.namespace}.{ref.name}"
    try:
        return catalog.describe_table(p, ref)
    except PermissionDenied as exc:
        raise PermissionDenied(
            f"You need READ_TABLE on {dotted!r} so that the column can be checked."
        ) from exc
    except NotFoundError as exc:
        raise ConflictError(
            f"Table {dotted!r} has no schema recorded yet, so its columns cannot be checked. "
            "Create the table with its columns first "
            "(lakana table create NAMESPACE.TABLE --column NAME:TYPE), "
            f"then create the {policy}, then grant READ_TABLE or write data."
        ) from exc
    except (LakanaError, OSError) as exc:
        logger.error("Could not read the columns of table %r: %s", dotted, exc, exc_info=True)
        raise ConflictError(
            f"The columns of table {dotted!r} could not be read; the server's log has the "
            f"cause. Create the {policy} again when the table is readable."
        ) from exc


def read_column_names(
    catalog: ColumnSource, p: PrincipalRef, ref: TableRef, *, policy: str
) -> list[str]:
    """Read the names of a table's current columns, as the caller may.

    Parameters
    ----------
    catalog
        Where to read the table's current columns from.
    p
        The acting principal, already authorized to manage policies.
    ref
        The table the policy is for.
    policy
        What is being created, for the next step in a message: `"mask"` or `"row filter"`.

    Returns
    -------
    list of str
        The column names, in the table's order.

    Raises
    ------
    PermissionDenied
        If `p` lacks READ_TABLE on the table, which reading its columns needs.
    ConflictError
        If the table has no schema yet, or its columns could not be read.
    """
    return [spec.name for spec in read_table_description(catalog, p, ref, policy=policy).columns]


def list_columns(names: list[str]) -> str:
    """Say which columns a table has, for an error message.

    Parameters
    ----------
    names
        The table's column names.

    Returns
    -------
    str
        `"Its columns are: a, b, and 3 more."`, cut at `MAX_LISTED_COLUMNS`; or a sentence
        that the table has no recorded columns.
    """
    if not names:
        return "The table has no recorded columns."
    listed = ", ".join(names[:MAX_LISTED_COLUMNS])
    if len(names) > MAX_LISTED_COLUMNS:
        listed += f", and {len(names) - MAX_LISTED_COLUMNS} more"
    return f"Its columns are: {listed}."


def validate_for_table(
    catalog: ColumnSource,
    p: PrincipalRef,
    *,
    environment: str,
    namespace: str,
    table: str,
    expression: str,
    kind: Literal["filter", "mask"],
) -> None:
    """Validate a policy expression, using the table's columns to word a refusal of a dotted name.

    The grammar decides what is refused, never the table: the columns are read only after the
    expression turned out to hold a column written with a dot, to tell a qualifier (`t.region`)
    from a field of a column (`address.country`). A caller who cannot read them, or a table
    whose columns cannot be read, gets the qualifier wording; nothing here can let an
    expression through.

    Parameters
    ----------
    catalog
        Where to read the table's current columns from.
    p
        The acting principal, already authorized to manage policies.
    environment
        The table's environment.
    namespace
        The namespace, in its unit-separator-joined storage form.
    table
        The table name.
    expression
        The expression to validate.
    kind
        `"filter"` or `"mask"`.

    Raises
    ------
    ValidationError
        As `validate_restricted_expression` raises it.
    """
    try:
        validate_restricted_expression(expression, kind=kind)
    except QualifiedColumnError:
        names: list[str] | None
        kind_policy = "row filter" if kind == "filter" else "mask"
        try:
            ref = TableRef(
                environment=environment, namespace=NamespacePath.parse(namespace), name=table
            )
            names = read_column_names(catalog, p, ref, policy=kind_policy)
        except Exception:  # a refusal can only be reworded here, never turned into an acceptance
            names = None
        validate_restricted_expression(expression, kind=kind, columns=names)
        raise
