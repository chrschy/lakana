"""First-run setup for the Compose team bundle: create `dev` over REST.

Runs as the one-shot `setup` service (`python -m lakana.cli.deploy.setup`),
after the server is healthy and SeaweedFS's `lakana` bucket exists. It uses
only the public REST API through the SDK (CLAUDE.md rule 2), and is
idempotent: a restarted stack re-runs it and it creates nothing. It also sets the
bundle's own storage secret again when that is still in the old stored format (1),
the one secret Lakana itself created; secrets an operator created are never touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections.abc import Callable
from typing import TextIO

import lakana
from lakana._models import Environment
from lakana.errors import (
    AuthenticationError,
    LakanaConnectionError,
    LakanaError,
    NotFound,
    PermissionDenied,
    ServerError,
)

ENVIRONMENT = "dev"
SECRET_NAME = "seaweedfs-s3"  # nosec B105 -- a secret's name, not a credential value
OPTIONS = {"s3.endpoint": "http://seaweedfs:8333"}
STORAGE = {"scheme": "s3", "root": "s3://lakana/dev", "options": OPTIONS}
_VARIABLES = (
    "LAKANA_SETUP_URL",
    "LAKANA_SETUP_TOKEN",
    "LAKANA_SETUP_S3_ACCESS_KEY",
    "LAKANA_SETUP_S3_SECRET_KEY",
)
_RETRYABLE = (LakanaConnectionError, ServerError)
_RETRY_DELAY_SECONDS = 2.0


def _options_digest(options: dict[str, str]) -> str:
    """Fingerprint a storage options map as the server does.

    Parameters
    ----------
    options
        The options.

    Returns
    -------
    str
        The SHA-256 of the options as sorted-key, compact JSON, in hex.
    """
    canonical = json.dumps(options, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _is_operator_managed(env: Environment) -> bool:
    """Report whether a pre-existing `dev` belongs to an operator, not this bundle.

    Parameters
    ----------
    env
        The environment already found under `ENVIRONMENT`.

    Returns
    -------
    bool
        `True` if its storage root differs from `STORAGE["root"]`, its storage options are
        not exactly `STORAGE["options"]` (or the server does not say), or its storage secret
        is already bound to a name other than `SECRET_NAME`. The secret is presented to the
        endpoint in the options, so it is attached only where they are the bundle's own.
    """
    if env.storage_root != STORAGE["root"]:
        return True
    if env.storage_options_digest != _options_digest(OPTIONS):
        return True
    return env.storage_secret_ref is not None and env.storage_secret_ref != SECRET_NAME


def run_setup(
    client: lakana.Client, *, access_key: str, secret_key: str, out: TextIO | None = None
) -> None:
    """Ensure `dev`, its storage secret and the binding between them exist.

    Leaves a pre-existing `dev` completely untouched -- no secret created,
    no rebind attempted -- if its storage doesn't match this bundle's own
    : that `dev` belongs to an operator,
    not this bootstrap, and guessing at it risks annexing an environment
    or clobbering an intentionally different secret binding.

    Parameters
    ----------
    client
        An SDK client authenticated as the bootstrap admin.
    access_key
        SeaweedFS S3 access key.
    secret_key
        SeaweedFS S3 secret key.
    out
        Where progress lines go. `None` (the default) resolves to
        `sys.stdout` at call time rather than once at import, so a caller
        that reassigns `sys.stdout` after this module is imported (e.g. a
        test's `capsys`) is still followed on every call, including a
        retried one.
    """
    if out is None:
        out = sys.stdout
    try:
        env = client.environments.get(ENVIRONMENT)
    except NotFound:
        env = client.environments.create(ENVIRONMENT, storage=STORAGE)
        out.write(f"Created environment '{ENVIRONMENT}' at {STORAGE['root']}\n")
    else:
        if env.storage_root == STORAGE["root"] and env.storage_options_digest != _options_digest(
            OPTIONS
        ):
            out.write(
                f"environment '{ENVIRONMENT}': its storage options differ from the bundle's, "
                f"so the storage secret '{SECRET_NAME}' is not attached. An administrator "
                f"checks the options (they decide where the secret is sent), creates the secret "
                f"if it is missing (lakana secret create) and attaches it: "
                f"lakana env set-storage {ENVIRONMENT} --secret-ref {SECRET_NAME}\n"
            )
            return
        if _is_operator_managed(env):
            out.write(
                f"environment '{ENVIRONMENT}' is operator-managed "
                f"(storage {env.storage_root!r}); leaving it unchanged\n"
            )
            return
    values = {"s3.access-key-id": access_key, "s3.secret-access-key": secret_key}
    existing = {s.name: s for s in client.secrets.list(ENVIRONMENT)}
    if SECRET_NAME not in existing:
        client.secrets.create(ENVIRONMENT, SECRET_NAME, values)
        out.write(f"Created storage secret '{SECRET_NAME}'\n")
    elif existing[SECRET_NAME].format_version < 2:
        # The values are the bundle's own, so setting it again needs no read of
        # the old one. Against a server that predates the format field the SDK
        # reports 1 too, so that server gets a harmless rotate on every start;
        # the bundle's server and this step are the same image, so it cannot occur.
        client.secrets.rotate(ENVIRONMENT, SECRET_NAME, values)
        out.write(f"Set storage secret '{SECRET_NAME}' again in the current stored format\n")
    if env.storage_secret_ref != SECRET_NAME:
        client.environments.update_storage(ENVIRONMENT, secret_ref=SECRET_NAME)
        out.write(f"Bound '{SECRET_NAME}' to '{ENVIRONMENT}'\n")


def _run_with_retry(
    client: lakana.Client,
    *,
    access_key: str,
    secret_key: str,
    sleep: Callable[[float], None],
    attempts: int,
    out: TextIO | None = None,
) -> None:
    """Run `run_setup`, retrying while the server is still settling.

    `run_setup` is idempotent, so retrying the whole call is safe even if
    an earlier attempt made partial progress before failing.

    Parameters
    ----------
    client
        An SDK client authenticated as the bootstrap admin.
    access_key
        SeaweedFS S3 access key.
    secret_key
        SeaweedFS S3 secret key.
    sleep
        Called with the delay, in seconds, between attempts.
    attempts
        The maximum number of attempts.
    out
        Where progress lines go; forwarded to every `run_setup` attempt
        (a caller-supplied
        stream is not ignored under retry). `None` resolves to
        `sys.stdout` at call time, same as `run_setup`'s own default.

    Raises
    ------
    lakana.errors.LakanaError
        The last attempt's `LakanaConnectionError`/`ServerError`, once
        `attempts` is exhausted.
    """
    attempt = 1
    while True:
        try:
            run_setup(client, access_key=access_key, secret_key=secret_key, out=out)
            return
        except _RETRYABLE:
            if attempt >= attempts:
                raise
            sleep(_RETRY_DELAY_SECONDS)
            attempt += 1


def main(*, sleep: Callable[[float], None] = time.sleep, attempts: int = 30) -> int:
    """Run setup from the `LAKANA_SETUP_*` environment.

    Parameters
    ----------
    sleep
        Called between retry attempts; overridden in tests to stay
        instant.
    attempts
        The maximum number of attempts against a server that answers
        health but is still settling (`LakanaConnectionError`/`ServerError`).

    Returns
    -------
    int
        0 on success -- including a revoked or unauthorized bootstrap
        credential, which is treated as
        intentional, not a failure -- 1 if some other API call failed, or 2
        if a variable is missing.
    """
    missing = [name for name in _VARIABLES if not os.environ.get(name)]
    if missing:
        sys.stderr.write(f"setup: missing {', '.join(missing)}\n")
        return 2
    try:
        with lakana.Client(
            url=os.environ["LAKANA_SETUP_URL"], token=os.environ["LAKANA_SETUP_TOKEN"]
        ) as client:
            _run_with_retry(
                client,
                access_key=os.environ["LAKANA_SETUP_S3_ACCESS_KEY"],
                secret_key=os.environ["LAKANA_SETUP_S3_SECRET_KEY"],
                sleep=sleep,
                attempts=attempts,
            )
    except (AuthenticationError, PermissionDenied):
        # Two real causes reach this, and setup can't tell them apart from a
        # 401/403 alone: an operator who followed deploy-with-compose.md's
        # "Revoking the bootstrap credential" flow leaves every later `docker
        # compose up` re-running `setup` with that now-dead token -- the
        # intended case, nothing to do. Less commonly, `.env`'s bootstrap PAT
        # was never registered against the database this stack is now
        # pointed at (a bundle directory deleted without `down -v`, or a
        # second bundle re-using an old `pgdata`), which also 401s and also
        # shouldn't crash the container -- but does leave `dev` unconfigured,
        # which the message below flags without claiming to know which case
        # this is. Either way, failing the container here would be wrong:
        # the first case breaks every restart after the documented
        # procedure, and the second can't be fixed by retrying.
        sys.stdout.write(
            "bootstrap credential rejected (retired after revocation, or a re-run init "
            "over an old volume); skipping setup\n"
        )
        return 0
    except LakanaError as exc:
        status = f" ({exc.status})" if exc.status is not None else ""
        sys.stderr.write(f"setup: {type(exc).__name__}{status}\n")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
