r"""Conversion between the Iceberg REST wire format and NamespacePath.

Two wire shapes exist for the same value: a URL path segment joins parts
with the raw unit separator character (`\x1f`), matching
`lakana_core.identifiers.NAMESPACE_SEPARATOR` exactly — this is not a
coincidence; the separator was chosen for this reason (see the architecture
doc's identity decisions). FastAPI/Starlette URL-decodes path parameters
before an endpoint ever sees them, so a namespace segment arrives here
already containing the literal separator character, not a percent-encoded
stand-in for it. A JSON response body instead uses a list of parts.
`decode_namespace` and `encode_namespace` are not a round-trip pair of each
other: `decode_namespace` only ever parses the joined URL-segment form, and
`encode_namespace` only ever produces the list form used in response
bodies — no call site passes one function's output into the other.
"""

from __future__ import annotations

from lakana_core.identifiers import NamespacePath


def decode_namespace(raw: str) -> NamespacePath:
    """Parse a namespace path segment from a request URL.

    Parameters
    ----------
    raw
        The `{namespace}` path parameter, already URL-decoded by Starlette,
        so it already contains the literal separator character rather than
        a percent-encoded stand-in for it.

    Returns
    -------
    NamespacePath
        The parsed path.

    Raises
    ------
    lakana_core.errors.ValidationError
        If any component is invalid.
    """
    return NamespacePath.parse(raw)


def encode_namespace(path: NamespacePath) -> list[str]:
    """Render a namespace as the Iceberg wire format's list of parts.

    Parameters
    ----------
    path
        The namespace to render.

    Returns
    -------
    list of str
        The namespace's parts, outermost first — the shape used in every
        JSON response that names a namespace (`ListNamespacesResponse`,
        `NamespaceResponse`, `ListTablesResponse`'s per-entry `namespace`).
    """
    return list(path.parts)
