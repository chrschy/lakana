"""The CI token exchange: a CI job's OIDC ID token for a Lakana token that allows one deploy.

``POST /api/v1/auth/oidc/token`` is the one route of the platform API that takes no credential:
the ID token is the credential. Its body is read here, at most `MAX_EXCHANGE_BODY_BYTES`
(16 KiB), before anything parses it; the verification (`lakana_core.ci_oidc`) runs in the
threadpool, because the key cache is synchronous and may fetch over the network. A refusal is
recorded as `OIDC_EXCHANGE_REFUSED`, at most `LAKANA_AUTH_AUDIT__PER_IP_PER_MINUTE` rows per
client and minute (its own bucket, apart from failed authentications), with the code, the
issuer when it is a configured one, and, once the ID token verified, the environment and
repository asked for. Never the token.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from lakana_core.audit.service import AuditService
from lakana_core.ci_oidc.exchange import CiExchangeRefused, CiTokenService
from lakana_core.errors import AuthenticationError, PermissionDenied, ValidationError
from lakana_core.models import AuditEventType
from lakana_server.api.platform.schemas import CiTokenExchangeIn, CiTokenOut
from lakana_server.body_limit import streams_its_own_body
from lakana_server.errors import PayloadTooLargeError

MAX_EXCHANGE_BODY_BYTES = 16 * 1024
"""The largest exchange request read; an ID token is at most 16 KiB too."""

router = APIRouter(prefix="/api/v1/auth/oidc", tags=["auth"])


async def _read_body(request: Request) -> bytes:
    """Read the request body, refusing it once it is over the limit.

    Parameters
    ----------
    request
        The request.

    Returns
    -------
    bytes
        The body.

    Raises
    ------
    PayloadTooLargeError
        Mapped to 413, if the declared or delivered body is over `MAX_EXCHANGE_BODY_BYTES`.
    """
    message = (
        f"The exchange request is larger than {MAX_EXCHANGE_BODY_BYTES} bytes; send only "
        "id_token, environment and repository."
    )
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_EXCHANGE_BODY_BYTES:
        raise PayloadTooLargeError(message)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_EXCHANGE_BODY_BYTES:
            raise PayloadTooLargeError(message)
    return bytes(body)


def _parse(body: bytes) -> CiTokenExchangeIn:
    """Parse the exchange request without ever repeating a value of it.

    Parameters
    ----------
    body
        The raw body.

    Returns
    -------
    CiTokenExchangeIn
        The request.

    Raises
    ------
    ValidationError
        Mapped to 422, naming the fields that are wrong, never their values.
    """
    try:
        return CiTokenExchangeIn.model_validate_json(body)
    except PydanticValidationError as exc:
        fields = sorted(
            {".".join(str(part) for part in error["loc"]) or "body" for error in exc.errors()}
        )
        raise ValidationError(
            "The exchange request must be a JSON object with the texts id_token, environment "
            f"and repository and nothing else; wrong: {', '.join(fields)[:200]}."
        ) from None


def _record_refusal(request: Request, session: Session, refusal: CiExchangeRefused) -> None:
    """Record a refused exchange, if the client's bucket admits it.

    Parameters
    ----------
    request
        The request, for the limiter and the client's address.
    session
        A session for the row, committed on its own.
    refusal
        The refusal.
    """
    client = request.client.host if request.client else "unknown"
    admitted, suppressed = request.app.state.oidc_exchange_audit_limiter.admit(client)
    if not admitted:
        return
    detail: dict[str, Any] = {"code": refusal.code, **refusal.detail}
    if refusal.issuer is not None:
        detail["issuer"] = refusal.issuer
    if suppressed > 0:
        detail["suppressed"] = suppressed
    AuditService(session).record(
        None, AuditEventType.OIDC_EXCHANGE_REFUSED, detail=detail, commit=True
    )


def _exchange(request: Request, body: CiTokenExchangeIn) -> CiTokenOut:
    """Exchange the ID token, in a session of its own (run in the threadpool).

    Parameters
    ----------
    request
        The request, for the engine, the settings, the verifier and the limiter.
    body
        The parsed request.

    Returns
    -------
    CiTokenOut
        The token.

    Raises
    ------
    AuthenticationError
        Mapped to 401, if the ID token is refused or was exchanged before.
    PermissionDenied
        Mapped to 403, if it verified and no single enabled rule accepts it.
    """
    state = request.app.state
    with Session(state.engine) as session:
        service = CiTokenService(session, ttl_seconds=state.settings.ci_oidc.token_ttl_seconds)
        try:
            issued = service.exchange(
                state.ci_oidc_verifier,
                body.id_token,
                environment=body.environment,
                repository=body.repository,
            )
        except CiExchangeRefused as refusal:
            session.rollback()
            _record_refusal(request, session, refusal)
            if refusal.authenticated:
                raise PermissionDenied(refusal.message) from None
            raise AuthenticationError(refusal.message) from None
    assert issued.rule_id is not None  # nosec B101 -- every issued token has its rule
    return CiTokenOut(
        token=issued.token,
        expires_at=issued.expires_at,
        principal=issued.principal,
        environment=issued.environment,
        repository=issued.repository,
        kinds=issued.kinds,
        sha=issued.sha,
        rule_id=issued.rule_id,
    )


@router.post(
    "/token",
    response_model=CiTokenOut,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": CiTokenExchangeIn.model_json_schema()}},
        }
    },
)
@streams_its_own_body
async def exchange_ci_id_token(request: Request) -> CiTokenOut:
    """Exchange a CI job's OIDC ID token for a Lakana CI token; no credential needed.

    The token acts as the service account of the one enabled CI trust rule whose conditions
    the ID token's claims match, for the environment and repository asked for. It may queue one
    deploy (of the ID token's ``sha`` only) or dry run there, then read that run, and expires
    after `LAKANA_CI_OIDC__TOKEN_TTL_SECONDS` (one hour by default).

    Parameters
    ----------
    request
        The request; its body is ``{"id_token", "environment", "repository"}``, at most 16 KiB.

    Returns
    -------
    CiTokenOut
        The token, shown once.
    """
    body = _parse(await _read_body(request))
    return await run_in_threadpool(_exchange, request, body)
