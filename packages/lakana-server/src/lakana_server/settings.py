"""Runtime settings for lakana-server."""

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lakana_core.config import (
    ArtifactsConfig,
    DatabaseConfig,
    DenialAuditConfig,
    LogConfig,
    QueryLimitsConfig,
    RunConfig,
    SecretsConfig,
    TaskTokensConfig,
    validate_repo_roots,
)


class AppendConfig(BaseModel):
    """Operator-level bounds on a governed Arrow append.

    Parameters
    ----------
    max_bytes
        Largest Arrow IPC stream body `POST .../tables/{name}/data` accepts,
        enforced by reading `request.stream()` with a running total: once
        the accumulated size exceeds this bound, the request is refused
        with `PayloadTooLargeError` (413) before the rest of the body is
        read, and nothing is written.
    max_concurrent
        Most governed appends allowed to be in flight at once in this
        process, enforced by a non-blocking
        `lakana_server.concurrency.AppendConcurrencyLimiter` on
        `app.state.append_concurrency_limiter`. A request past this bound
        is refused immediately with `lakana_server.errors.
        AppendConcurrencyLimitError` (503, `Retry-After: 1`) rather than
        queuing.
    read_timeout_seconds
        Two bounds on reading a request body. For a governed append, the longest the whole
        read may take: reading is wrapped in `asyncio.timeout`, and exceeding it refuses the
        request with `lakana_server.errors.AppendReadTimeoutError` (408) and nothing is
        written. For an artifact upload it is the longest the
        server waits for the next piece of a file, and the longest the manifest request may take
        in all; a file's body as a whole ends when its upload expires
        (`LAKANA_ARTIFACTS__UPLOAD_TTL_SECONDS`). Either way this bounds how long a slow or
        stalled client can occupy a slot at all, separate from `max_concurrent` bounding how
        many may do so at once.
    max_arrays
        Most cumulative batches-times-columns (`RecordBatch.num_columns`,
        summed over every batch decoded so far) a single append's decoded
        data may contain, checked in `lakana_server.api.platform.tables.
        _decode_arrow_stream` alongside `max_bytes`. Each decoded batch
        costs roughly 1-3 KB of object overhead in decode and again in
        pyiceberg, independent of its byte size — many tiny batches inside
        an otherwise byte-cap-respecting body can still amplify memory and
        CPU cost far beyond what `max_bytes` alone implies. Exceeding it
        refuses the request with `PayloadTooLargeError` (413), suggesting
        fewer, larger batches, before the rest of the stream is decoded
        .
    """

    max_bytes: int = Field(default=268_435_456, gt=0)
    max_concurrent: int = Field(default=4, gt=0)
    read_timeout_seconds: float = Field(default=300, gt=0)
    max_arrays: int = Field(default=100_000, gt=0)


class AuthAuditConfig(BaseModel):
    """Operator-level bound on failed-authentication audit writes.

    Parameters
    ----------
    per_ip_per_minute
        `AUTHENTICATION_FAILED` audit rows recorded per client per minute,
        enforced by `lakana_server.auth_rate_limit.AuthAuditLimiter`. A
        client's requests past this rate still receive 401; they are just
        not individually recorded, so a retrying attacker cannot flood the
        audit log with one row per attempt.
    """

    per_ip_per_minute: int = Field(default=10, gt=0)


class RequestBodyConfig(BaseModel):
    """Bound on the body of a JSON request (`LAKANA_REQUEST_BODY__*`).

    Parameters
    ----------
    max_bytes
        Largest body, in bytes, the server accepts on a route whose body is JSON; a larger one
        is answered 413 before anything is parsed, and before the caller is authenticated.
        The default (1 MiB) is far above any ordinary request: a 100,000-character SQL statement
        of ASCII characters is under 0.6 MiB as JSON even with every character escaped. A character
        outside the Basic Multilingual Plane takes up to 12 bytes escaped, so a statement made of
        those can pass 1 MiB (raise this setting if you send such SQL). Routes that stream a body on
        purpose (table data, artifact files and manifests) are not counted: they have their own
        limits.
    """

    max_bytes: int = Field(default=1_048_576, gt=0)


class Settings(BaseSettings):
    """Configuration for the control plane.

    Parameters
    ----------
    database
        Metadata database configuration, in the same shape `lakana-core` uses.
    bootstrap_admin
        Name of the principal created on startup if it does not already
        exist. See `lakana_core.bootstrap.ensure_bootstrap_principal`.
    bootstrap_pat_key_id, bootstrap_pat_secret
        A fixed key_id/secret pair for the bootstrap principal's
        automatically-minted PAT, set only in tests or local development
        for a deterministic credential. When either is `None` (the
        production default), a real random PAT is generated and logged
        once at startup — never persisted in configuration.
    secrets
        Secrets subsystem configuration, in the same shape `lakana-core` uses.
    task_tokens
        Task-token minting bounds (`LAKANA_TASK_TOKENS__MAX_TTL_SECONDS`).
    artifacts
        Artifact-store bounds (`LAKANA_ARTIFACTS__*`): file size, total size, file
        count and upload lifetime.
    append
        Governed Arrow-append bounds: body size (`LAKANA_APPEND__MAX_BYTES`),
        concurrency (`LAKANA_APPEND__MAX_CONCURRENT`), body-read deadline
        (`LAKANA_APPEND__READ_TIMEOUT_SECONDS`), and decoded array count
        (`LAKANA_APPEND__MAX_ARRAYS`).
    request_body
        JSON request body bound (`LAKANA_REQUEST_BODY__MAX_BYTES`).
    auth_audit
        Failed-authentication audit rate limiting
        (`LAKANA_AUTH_AUDIT__PER_IP_PER_MINUTE`).
    denial_audit
        `AUTHORIZATION_DENIED` audit rate limiting per principal
        (`LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE`).
    query_limits
        Only `timeout_seconds` is used here: the default and cap of a task timeout
        . Set it in the environment shared with the worker.
    run
        ``LAKANA_RUN__*``, shared with the worker.
    log
        ``LAKANA_LOG__LEVEL``, shared with the worker: how much the server logs.
    host
        Interface to bind when run via `uvicorn`. Unused by `create_app`
        itself; read by whatever process starts the server.
    port
        Port to bind when run via `uvicorn`. Unused by `create_app` itself.
    repo_roots
        Absolute directories local repositories must lie under
        (`LAKANA_REPO_ROOTS`, a JSON list); validated like `lakana_core.config.Settings`.
        Empty refuses every local repository.
    """

    model_config = SettingsConfigDict(
        env_prefix="LAKANA_", env_nested_delimiter="__", hide_input_in_errors=True
    )

    database: DatabaseConfig
    bootstrap_admin: str = "admin"
    bootstrap_pat_key_id: str | None = None
    bootstrap_pat_secret: str | None = None
    secrets: SecretsConfig = SecretsConfig()
    task_tokens: TaskTokensConfig = TaskTokensConfig()
    artifacts: ArtifactsConfig = ArtifactsConfig()
    append: AppendConfig = AppendConfig()
    request_body: RequestBodyConfig = RequestBodyConfig()
    auth_audit: AuthAuditConfig = AuthAuditConfig()
    denial_audit: DenialAuditConfig = DenialAuditConfig()
    query_limits: QueryLimitsConfig = QueryLimitsConfig()
    run: RunConfig = RunConfig()
    log: LogConfig = LogConfig()
    host: str = "127.0.0.1"
    port: int = 8000
    repo_roots: list[str] = []

    @field_validator("repo_roots")
    @classmethod
    def _repo_roots_are_absolute(cls, value: list[str]) -> list[str]:
        """Validate `LAKANA_REPO_ROOTS`.

        Parameters
        ----------
        value
            The parsed list.

        Returns
        -------
        list of str
            The list, unchanged.
        """
        return validate_repo_roots(value)
