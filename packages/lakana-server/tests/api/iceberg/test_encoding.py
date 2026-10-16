"""Tests for namespace-path encoding between the Iceberg wire format and NamespacePath."""

from lakana_core.identifiers import NamespacePath
from lakana_server.api.iceberg.encoding import decode_namespace, encode_namespace


def test_decode_round_trips_the_joined_form() -> None:
    """A path survives joined-then-decoded unchanged — the shape a URL path segment actually has."""
    path = NamespacePath.from_parts(["gold", "sales"])
    assert decode_namespace(path.joined()) == path


def test_decode_handles_a_single_part_namespace() -> None:
    """A top-level namespace has no separator to worry about."""
    assert decode_namespace("gold") == NamespacePath.from_parts(["gold"])


def test_decode_handles_the_raw_unit_separator() -> None:
    """The wire format joins parts with the raw separator character.

    FastAPI/Starlette URL-decodes path parameters before this function ever
    sees them, so `raw` already contains the literal separator, not a
    percent-encoded stand-in — this is what makes decode_namespace a plain
    NamespacePath.parse call rather than needing manual unescaping.
    """
    joined = "gold\x1fsales\x1feu"
    assert decode_namespace(joined) == NamespacePath.from_parts(["gold", "sales", "eu"])


def test_encode_produces_a_list_of_parts() -> None:
    """The wire format for a namespace in a JSON body is a list, not a joined string."""
    path = NamespacePath.from_parts(["gold", "sales"])
    assert encode_namespace(path) == ["gold", "sales"]
