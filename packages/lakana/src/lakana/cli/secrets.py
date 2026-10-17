"""`lakana secret *`: write-only secrets."""

from __future__ import annotations

import json
import sys
from typing import Annotated

import typer

from lakana.cli import _output
from lakana.cli._state import CliError, confirm, parse_key_values, resolve_environment, state_of

secret_app = typer.Typer(help="Manage write-only secrets.", no_args_is_help=True)

_ENV = Annotated[str | None, typer.Option("--env", help="Environment (default: the profile's).")]
_VALUE = Annotated[
    list[str] | None,
    typer.Option(
        "--value", help="key=value (repeatable). Lands in shell history; prefer --values-stdin."
    ),
]
_STDIN = Annotated[
    bool, typer.Option("--values-stdin", help="Read a JSON object of values from stdin.")
]


def read_values(value: list[str] | None, values_stdin: bool) -> dict[str, str]:
    """Collect secret values from `--value` or `--values-stdin`.

    Parameters
    ----------
    value
        `--value` items.
    values_stdin
        Whether to read a JSON object from stdin.

    Returns
    -------
    dict of str to str
        The values.

    Raises
    ------
    CliError
        Unless exactly one source is used and it yields a string-to-string
        object (exit 2).
    """
    if bool(value) == values_stdin:
        raise CliError("Pass exactly one of --value key=value or --values-stdin")
    if not values_stdin:
        return parse_key_values(value, "--value", sensitive=True)
    try:
        data = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        raise CliError(f"--values-stdin expects a JSON object: {exc}") from exc
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise CliError("--values-stdin expects a JSON object of string keys and values")
    return data


@secret_app.command("create")
def create(
    ctx: typer.Context,
    name: str,
    env: _ENV = None,
    value: _VALUE = None,
    values_stdin: _STDIN = False,
) -> None:
    """Create secret NAME; its values can never be read back.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The secret name.
    env
        The environment.
    value
        Values as key=value.
    values_stdin
        Read values as JSON from stdin.
    """
    state = state_of(ctx)
    values = read_values(value, values_stdin)
    environment = resolve_environment(state, env)
    secret = state.client().secrets.create(environment, name, values)
    _output.done(
        state, f"Created secret {_output.inline(name)} in {_output.inline(environment)}", secret
    )


@secret_app.command("list")
def list_secrets(ctx: typer.Context, env: _ENV = None) -> None:
    """List secrets (names and metadata only).

    `format_version` 1 marks a secret stored in the original format; set it
    again with `lakana secret rotate` to store it in the current one.

    `use` says what a secret may be used for: a package index or a repository (`use_target`
    names it), or its environment's storage and webhooks. A secret with no use serves its
    environment only; `lakana secret bind` gives it one.

    Parameters
    ----------
    ctx
        The Typer context.
    env
        The environment.
    """
    state = state_of(ctx)
    secrets = state.client().secrets.list(resolve_environment(state, env))
    _output.emit(
        state,
        secrets,
        columns=[
            "name",
            "environment",
            "use",
            "use_target",
            "format_version",
            "key_id",
            "created_by",
            "created_at",
        ],
        droppable=["created_by", "environment", "key_id", "format_version", "use_target"],
    )


@secret_app.command("rotate")
def rotate(
    ctx: typer.Context,
    name: str,
    env: _ENV = None,
    value: _VALUE = None,
    values_stdin: _STDIN = False,
) -> None:
    """Replace secret NAME's values.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The secret name.
    env
        The environment.
    value
        Values as key=value.
    values_stdin
        Read values as JSON from stdin.
    """
    state = state_of(ctx)
    values = read_values(value, values_stdin)
    environment = resolve_environment(state, env)
    secret = state.client().secrets.rotate(environment, name, values)
    _output.done(
        state, f"Rotated secret {_output.inline(name)} in {_output.inline(environment)}", secret
    )


@secret_app.command("bind")
def bind(
    ctx: typer.Context,
    name: str,
    env: _ENV = None,
    index: Annotated[
        str | None, typer.Option("--index", help="Bind it to this package index.")
    ] = None,
    repository: Annotated[
        str | None, typer.Option("--repository", help="Bind it to this repository.")
    ] = None,
    own_env: Annotated[
        bool,
        typer.Option("--own-env", help="Bind it to its environment: storage, webhooks and tasks."),
    ] = False,
) -> None:
    """Bind secret NAME to the one use it may serve; needs the right to manage secrets.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The secret name.
    env
        The environment.
    index
        A package index's name.
    repository
        A repository's name.
    own_env
        Bind it to its environment instead.

    Raises
    ------
    CliError
        Unless exactly one of the three options is given (exit 2).
    """
    chosen = [
        (kind, target)
        for kind, target, given in (
            ("package_index", index, index is not None),
            ("repository", repository, repository is not None),
            ("environment", None, own_env),
        )
        if given
    ]
    if len(chosen) != 1:
        raise CliError("Pass exactly one of --index, --repository or --own-env")
    kind, target = chosen[0]
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    secret = state.client().secrets.bind(environment, name, kind, target)
    noun = {"package_index": "package index", "repository": "repository"}.get(kind)
    use = f"{noun} {_output.inline(target or '')}" if noun else "its environment"
    _output.done(
        state,
        f"Bound secret {_output.inline(name)} in {_output.inline(environment)} to {use}",
        secret,
    )


@secret_app.command("revoke")
def revoke(
    ctx: typer.Context,
    name: str,
    env: _ENV = None,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")] = False,
) -> None:
    """Revoke secret NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The secret name.
    env
        The environment.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    confirm(state, f"Revoke secret {environment}/{name}?", yes)
    state.client().secrets.revoke(environment, name)
    _output.done(state, f"Revoked secret {_output.inline(name)} in {_output.inline(environment)}")


def register(app: typer.Typer) -> None:
    """Add the `secret` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(secret_app, name="secret")
