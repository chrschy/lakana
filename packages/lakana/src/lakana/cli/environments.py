"""`lakana env *`: manage environments."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

import typer

from lakana.cli import _output
from lakana.cli._state import CliError, confirm, parse_key_values, state_of

env_app = typer.Typer(help="Manage environments.", no_args_is_help=True)

_SCHEMES = ("file", "s3", "abfs")
_YES = Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")]
_OPTIONS = Annotated[
    list[str] | None,
    typer.Option(
        "--option",
        help=(
            "Storage option key=value (repeatable). Values land in shell history; "
            "prefer --secret-ref for credentials."
        ),
    ),
]
_STORAGE_ROOT_HELP = "e.g. file:///data or s3://bucket/lake"


@env_app.command("create")
def create(
    ctx: typer.Context,
    name: str,
    storage_root: Annotated[str, typer.Option("--storage-root", help=_STORAGE_ROOT_HELP)],
    option: _OPTIONS = None,
) -> None:
    """Create environment NAME (with bronze/silver/gold namespaces).

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment name.
    storage_root
        Its storage root URI.
    option
        Storage options as key=value.

    Raises
    ------
    CliError
        If the root has no supported scheme (exit 2).
    """
    state = state_of(ctx)
    scheme = urlsplit(storage_root).scheme
    if scheme not in _SCHEMES:
        raise CliError(
            f"--storage-root must start with one of {', '.join(s + '://' for s in _SCHEMES)}; "
            f"got {storage_root!r}"
        )
    environment = state.client().environments.create(
        name,
        storage={
            "scheme": scheme,
            "root": storage_root,
            "options": parse_key_values(option, "--option", sensitive=True),
        },
    )
    _output.done(
        state,
        f"Created environment {_output.inline(name)} at {_output.inline(storage_root)}",
        environment,
    )


@env_app.command("list")
def list_environments(
    ctx: typer.Context,
    deleted: Annotated[
        bool,
        typer.Option(
            "--deleted",
            help="List the deleted environments that can still be restored, and until when.",
        ),
    ] = False,
) -> None:
    """List environments you can see, or with --deleted the deleted ones.

    A deleted environment keeps its tables' files until `purge_after` (the server's undo
    period, 7 days by default); until then `lakana env restore NAME` brings it back.

    Parameters
    ----------
    ctx
        The Typer context.
    deleted
        List the deleted environments instead.
    """
    state = state_of(ctx)
    if deleted:
        _output.emit(
            state,
            state.client().environments.list(deleted=True),
            columns=["name", "deleted_at", "purge_after", "storage_root"],
            droppable=["storage_root"],
        )
        return
    _output.emit(
        state,
        state.client().environments.list(),
        columns=["name", "storage_root", "is_protected"],
        droppable=["storage_root"],
    )


@env_app.command("get")
def get(ctx: typer.Context, name: str) -> None:
    """Show environment NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment.
    """
    state = state_of(ctx)
    _output.emit(state, state.client().environments.get(name))


@env_app.command("delete")
def delete(
    ctx: typer.Context,
    name: str,
    purge: Annotated[
        bool,
        typer.Option(
            "--purge",
            help=(
                "Delete the tables' files at the worker's next clean-up instead of after the "
                "undo period; the environment cannot be restored then. Also for an environment "
                "already deleted."
            ),
        ),
    ] = False,
    yes: _YES = False,
) -> None:
    """Delete environment NAME. Its tables' files are kept for the undo period, then deleted.

    Within the server's undo period (`LAKANA_TABLES__UNDO_DAYS`, 7 days by default), `lakana
    env restore NAME` brings the environment back with its tables, their data and their grants.
    Until then the name stays taken, and `lakana env list --deleted` shows until when. Views
    and external tables are removed; an external table's files are never deleted. `--purge`
    skips the period.

    Refused (409) while any repository is deployed to it: run
    `lakana undeploy REPO --env NAME` for each first. Refused while protected.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment.
    purge
        Make the files due at once.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    if purge:
        question = (
            f"Delete environment {name!r} and its tables' files at the next clean-up? This "
            "cannot be undone."
        )
    else:
        question = (
            f"Delete environment {name!r}? It can be restored with its tables during the undo "
            "period."
        )
    confirm(state, question, yes)
    deleted = state.client().environments.delete(name, purge=purge)
    shown = _output.inline(name)
    if deleted is None or deleted.purge_after is None:
        _output.done(state, f"Deleted environment {shown}", deleted)
    elif purge:
        _output.done(
            state,
            f"Deleted environment {shown}. Its tables' files are deleted at the next clean-up; "
            "it cannot be restored.",
            deleted,
        )
    else:
        until = deleted.purge_after.strftime("%Y-%m-%d %H:%M UTC")
        _output.done(
            state,
            f"Deleted environment {shown}. Restore it with its tables until {until} with "
            f"`lakana env restore {shown}`.",
            deleted,
        )


@env_app.command("restore")
def restore(ctx: typer.Context, name: str) -> None:
    """Restore the deleted environment NAME, with the tables, data and grants it had.

    Works until the end of the undo period that `lakana env list --deleted` shows, and not
    after `lakana env delete NAME --purge`. A table dropped before the environment was deleted
    stays dropped; restore it afterwards with `lakana table undrop`. Needs MANAGE_ENVIRONMENT
    (deleting the environment revoked the grants given on it, so usually an administrator's).

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment.
    """
    state = state_of(ctx)
    environment = state.client().environments.restore(name)
    _output.done(state, f"Restored environment {_output.inline(name)}", environment)


@env_app.command("set-storage")
def set_storage(
    ctx: typer.Context,
    name: str,
    option: _OPTIONS = None,
    secret_ref: Annotated[
        str | None, typer.Option("--secret-ref", help="Secret holding credentials.")
    ] = None,
) -> None:
    """Change environment NAME's storage options or credentials secret.

    While the environment has a storage secret, changing the options also needs the right to
    manage secrets in it (the ADMIN role), because an option such as `s3.endpoint` decides
    where the secret is sent. `--option` replaces all the options, so pass every one the
    environment should have. Attaching a secret approves the options it has then.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment.
    option
        New storage options as key=value.
    secret_ref
        The name of a secret holding storage credentials.

    Raises
    ------
    CliError
        If nothing would change (exit 2).
    """
    state = state_of(ctx)
    if not option and secret_ref is None:
        raise CliError("Nothing to change: pass --option and/or --secret-ref")
    environment = state.client().environments.update_storage(
        name,
        options=parse_key_values(option, "--option", sensitive=True) if option else None,
        secret_ref=secret_ref,
    )
    _output.done(state, f"Updated storage for environment {_output.inline(name)}", environment)


@env_app.command("protect")
def protect(ctx: typer.Context, name: str) -> None:
    """Protect environment NAME: lock writes down to deployed runs.

    A protected environment refuses every write outside runs of the deployed
    code (SQL, SDK, CLI and Iceberg REST clients alike) whatever the caller's
    grants; choose the identity a deployed job runs as with `lakana deployments set-run-as`. A
    holder of MANAGE_ENVIRONMENT can make one audited write with `--break-glass
    --reason`. It also refuses deletion and storage changes.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment.
    """
    state = state_of(ctx)
    environment = state.client().environments.set_protection(name, True)
    _output.done(state, f"Protected environment {_output.inline(name)}", environment)


@env_app.command("unprotect")
def unprotect(ctx: typer.Context, name: str, yes: _YES = False) -> None:
    """Remove protection from environment NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    confirm(state, f"Remove protection from environment {name!r}?", yes)
    environment = state.client().environments.set_protection(name, False)
    _output.done(state, f"Unprotected environment {_output.inline(name)}", environment)


@env_app.command("retention")
def retention(
    ctx: typer.Context,
    name: str,
    days: Annotated[
        int | None,
        typer.Option(
            "--days",
            min=1,
            max=3650,
            help="Tables that set no retention expire N days after their last write or read.",
        ),
    ] = None,
    off: Annotated[
        bool, typer.Option("--off", help="Turn the environment's retention off.")
    ] = False,
    yes: _YES = False,
) -> None:
    """Show environment NAME's retention, or set it with --days or turn it off with --off.

    The retention applies to every managed table in the environment that sets none of its own
    (`lakana table retention`). Such a table expires N days after its last activity, and never
    sooner than N days after this command, so turning it on gives every table its full period.
    Expired tables are dropped by the worker's clean-up and can be restored with `lakana table
    undrop` during the undo period. Off unless set. Needs MANAGE_ENVIRONMENT; refused for a
    protected environment, whose tables never expire.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment.
    days
        `--days`.
    off
        `--off`.
    yes
        Skip the confirmation prompt of `--days`.

    Raises
    ------
    CliError
        With both options (exit 2).
    """
    state = state_of(ctx)
    environments = state.client().environments
    if days is not None and off:
        raise CliError("Give --days or --off, not both.")
    if days is None and not off:
        shown = environments.get(name)
        if state.output == "json":
            _output.emit(state, shown)
            return
        _output.emit(state, _retention_line(shown.name, shown.retention_days, now=False))
        return
    if days is not None:
        confirm(
            state,
            f"Let every table in {name!r} that sets no retention of its own expire {days} days "
            "after its last write or read?",
            yes,
        )
    changed = environments.set_retention(name, days)
    _output.done(state, _retention_line(changed.name, changed.retention_days, now=True), changed)


def _retention_line(name: str, days: int | None, *, now: bool) -> str:
    """Say what an environment's retention does.

    Parameters
    ----------
    name
        The environment.
    days
        Its retention, or `None` for off.
    now
        Whether it was just changed.

    Returns
    -------
    str
        One sentence.
    """
    shown = _output.inline(name)
    if days is None:
        return f"{shown} has no retention: its tables expire only if they set a retention."
    verb = "now expire" if now else "expire"
    return (
        f"Tables in {shown} that set no retention of their own {verb} {days} days after their "
        "last write or read. See what expires soon with: lakana table expiring --env "
        f"{shown}"
    )


def register(app: typer.Typer) -> None:
    """Add the `env` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(env_app, name="env")
