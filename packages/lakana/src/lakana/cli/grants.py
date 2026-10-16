"""`lakana grant *`: grant, list and revoke privileges."""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from typing import Annotated, Protocol

import typer

from lakana._models import Grant
from lakana.cli import _output
from lakana.cli._state import CliError, confirm, state_of

grant_app = typer.Typer(help="Manage grants.", no_args_is_help=True)

_PRINCIPAL = Annotated[str | None, typer.Option("--principal", help="Principal name.")]
_GROUP = Annotated[str | None, typer.Option("--group", help="Group name.")]
_ENV = Annotated[str | None, typer.Option("--env", help="Environment.")]
_NS = Annotated[str | None, typer.Option("--ns", help="Namespace (dotted).")]
_TABLE = Annotated[str | None, typer.Option("--table", help="Table name.")]


class _HasSubject(Protocol):
    """What names who a grant or a policy is for."""

    @property
    def principal_id(self) -> str | None:
        """The principal's id, or `None` when the subject is a group."""

    @property
    def group_id(self) -> str | None:
        """The group's id, or `None` when the subject is a principal."""


_NO_EFFECT_LINE = (
    "no effect: the grant names an action that works only with the ADMIN role on the instance. "
    "Revoke it with `lakana grant revoke ID`."
)


_MAX_REVOKE_LINES = 5
"""The most commands written out under the table; `--output json` has every grant."""


def _revoke_lines(grants: Sequence[Grant]) -> list[str]:
    """Write the command that revokes each grant without effect.

    The table leaves `id` out on a narrow terminal, so the line under it gives the
    whole id. A note naming the action and who the grant is for comes first, so that
    the command is alone on its line and can be copied as it is.

    Parameters
    ----------
    grants
        The listed grants.

    Returns
    -------
    list of str
        Two lines per grant the server sent a note for, at most `_MAX_REVOKE_LINES`
        grants (the note, then the indented command), then a line that counts the rest;
        what the server sent is shown as text.
    """
    stale = [grant for grant in grants if grant.note]
    lines: list[str] = []
    for grant in stale[:_MAX_REVOKE_LINES]:
        what = _output.inline(grant.action or grant.role or "")
        lines.append(f"{what}, {_output.inline(subject_of(grant))}:")
        lines.append(f"  lakana grant revoke {shlex.quote(_output.inline(grant.id))}")
    if len(stale) > _MAX_REVOKE_LINES:
        lines.append(f"and {len(stale) - _MAX_REVOKE_LINES} more: use --output json")
    return lines


def _marker(grant: Grant) -> str:
    """Say in a table cell that a grant has no effect.

    Parameters
    ----------
    grant
        A listed grant.

    Returns
    -------
    str
        ``no effect`` when the server sent a note, else an empty cell.
    """
    return "no effect" if grant.note else ""


def subject_of(item: _HasSubject) -> str:
    """Say who a grant or a policy is for.

    Parameters
    ----------
    item
        A grant, row filter or column mask.

    Returns
    -------
    str
        `principal <id>` or `group <id>`.
    """
    if item.principal_id is not None:
        return f"principal {item.principal_id}"
    return f"group {item.group_id}"


def exactly_one(first: str | None, second: str | None, flags: str) -> None:
    """Require exactly one of two mutually exclusive options.

    Parameters
    ----------
    first
        The first option's value.
    second
        The second option's value.
    flags
        Both flags, for the message, e.g. `"--principal or --group"`.

    Raises
    ------
    CliError
        If both or neither are set (exit 2).
    """
    if (first is None) == (second is None):
        raise CliError(f"Pass exactly one of {flags}")


@grant_app.command("create")
def create(
    ctx: typer.Context,
    kind: Annotated[str, typer.Option("--kind", help="INSTANCE, ENVIRONMENT, NAMESPACE or TABLE.")],
    principal: _PRINCIPAL = None,
    group: _GROUP = None,
    role: Annotated[
        str | None,
        typer.Option("--role", help="VIEWER, ANALYST, DATA_SCIENTIST, DATA_ENGINEER or ADMIN."),
    ] = None,
    action: Annotated[
        str | None, typer.Option("--action", help="A single action, e.g. READ_TABLE.")
    ] = None,
    env: _ENV = None,
    ns: _NS = None,
    table: _TABLE = None,
) -> None:
    """Grant a role or action on a securable to a principal or group.

    Parameters
    ----------
    ctx
        The Typer context.
    kind
        The securable's kind.
    principal
        The principal receiving the grant.
    group
        The group receiving the grant.
    role
        A role template.
    action
        A single action.
    env
        The securable's environment.
    ns
        The securable's namespace.
    table
        The securable's table.
    """
    state = state_of(ctx)
    exactly_one(principal, group, "--principal or --group")
    exactly_one(role, action, "--role or --action")
    grant = state.client().grants.grant(
        principal=principal,
        group=group,
        role=role.upper() if role else None,
        action=action.upper() if action else None,
        kind=kind.upper(),
        environment=env,
        namespace=ns,
        table=table,
    )
    _output.done(
        state, f"Created grant {grant.id} ({grant.role or grant.action} on {grant.kind})", grant
    )


@grant_app.command("list")
def list_grants(
    ctx: typer.Context,
    principal: _PRINCIPAL = None,
    group: _GROUP = None,
    env: _ENV = None,
    ns: _NS = None,
    table: _TABLE = None,
) -> None:
    """List active grants, optionally filtered.

    The table names who each grant is for in `subject`. When the terminal is too narrow it
    leaves out `kind`, `environment`, `id` and `namespace`, in that order; `--output json`
    always has every field.

    A grant of `MANAGE_GRANT`, `MANAGE_GROUP` or `MANAGE_TOKEN` as a single action has the
    marker `no effect` in a `note` column, which never drops (a narrow terminal leaves out
    `table` after the others, so that `subject` and the marker stay whole), and a line under
    the table explains it, followed by the command that revokes each such grant with its whole
    id (the first five; `--output json` has all of them and carries the full sentence). Revoke
    such a grant, and give the `ADMIN` role to whoever should keep the ability.

    Parameters
    ----------
    ctx
        The Typer context.
    principal
        Only grants to this principal.
    group
        Only grants to this group.
    env
        Only grants in this environment.
    ns
        Only grants on this namespace.
    table
        Only grants on this table.
    """
    state = state_of(ctx)
    grants = state.client().grants.list(
        principal_name=principal, group_name=group, environment=env, namespace=ns, table=table
    )
    columns = ["id", "subject", "role", "action", "kind", "environment", "namespace", "table"]
    droppable = ["kind", "environment", "id", "namespace"]
    footnotes: dict[str, str] = {}
    if any(grant.note for grant in grants):
        columns.append("note")
        droppable.append("table")
        footnotes["note"] = "\n".join([_NO_EFFECT_LINE, *_revoke_lines(grants)])
    _output.emit(
        state,
        grants,
        columns=columns,
        droppable=droppable,
        derived={"subject": subject_of, "note": _marker},
        whole=["subject", "note"],
        footnotes=footnotes,
    )


@grant_app.command("revoke")
def revoke(
    ctx: typer.Context,
    grant_id: str,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")] = False,
) -> None:
    """Revoke grant GRANT_ID.

    Take the id from `grant list`; on a narrow terminal the table leaves it out, so widen the
    terminal or use `--output json`.

    Parameters
    ----------
    ctx
        The Typer context.
    grant_id
        The grant id.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    confirm(state, f"Revoke grant {grant_id}?", yes)
    state.client().grants.revoke(grant_id)
    _output.done(state, f"Revoked grant {grant_id}")


def register(app: typer.Typer) -> None:
    """Add the `grant` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(grant_app, name="grant")
