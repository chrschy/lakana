"""Tests for namespace paths and table references."""

import pytest

from lakana_core.errors import ValidationError
from lakana_core.identifiers import (
    NAMESPACE_SEPARATOR,
    NamespacePath,
    TableRef,
    shown,
    shown_path,
    validate_path_component,
)


def test_from_parts_round_trips_through_joined() -> None:
    """A path built from parts serialises and parses back identically."""
    path = NamespacePath.from_parts(["gold", "sales"])
    assert path.joined() == f"gold{NAMESPACE_SEPARATOR}sales"
    assert NamespacePath.parse(path.joined()) == path


def test_single_part_path_has_no_parent() -> None:
    """A top-level namespace has no parent."""
    assert NamespacePath.from_parts(["gold"]).parent() is None


def test_parent_drops_the_last_part() -> None:
    """The parent of a nested path is the path without its final component."""
    parent = NamespacePath.from_parts(["gold", "sales", "eu"]).parent()
    assert parent == NamespacePath.from_parts(["gold", "sales"])


def test_child_appends_a_part() -> None:
    """A child extends the path by one component."""
    child = NamespacePath.from_parts(["gold"]).child("sales")
    assert child == NamespacePath.from_parts(["gold", "sales"])


def test_str_is_dotted_for_display() -> None:
    """Display form is dotted, not separator-joined."""
    assert str(NamespacePath.from_parts(["gold", "sales"])) == "gold.sales"


def test_empty_path_is_rejected() -> None:
    """A namespace must have at least one component."""
    with pytest.raises(ValidationError):
        NamespacePath.from_parts([])


def test_empty_component_is_rejected() -> None:
    """An empty component would make the joined form ambiguous."""
    with pytest.raises(ValidationError):
        NamespacePath.from_parts(["gold", ""])


def test_component_containing_the_separator_is_rejected() -> None:
    """The separator cannot appear inside a component."""
    with pytest.raises(ValidationError):
        NamespacePath.from_parts([f"gold{NAMESPACE_SEPARATOR}sales"])


@pytest.mark.parametrize("component", [".", "..", "a/b", "../../escaped", "  ", "\t"])
def test_traversal_slash_and_whitespace_only_components_are_rejected(component: str) -> None:
    """A component must not be a traversal segment, contain '/', or be blank.

    ``"a/b"`` is rejected rather than silently accepted, because accepting it
    would make ``from_parts(["a/b"])`` and ``from_parts(["a", "b"])`` collide
    on the identical storage location once a table location is built from
    the namespace's parts.
    """
    with pytest.raises(ValidationError):
        NamespacePath.from_parts(["gold", component])


def test_legitimate_nested_namespace_is_still_accepted() -> None:
    """The new component guard does not reject an ordinary nested namespace."""
    path = NamespacePath.from_parts(["gold", "sales", "eu-west"])
    assert path.parts == ("gold", "sales", "eu-west")


def test_paths_are_hashable_and_comparable() -> None:
    """Paths are value objects, usable as dict keys."""
    a = NamespacePath.from_parts(["gold"])
    b = NamespacePath.from_parts(["gold"])
    assert a == b
    assert len({a, b}) == 1


def test_table_ref_carries_environment_namespace_and_name() -> None:
    """A table reference names an environment, a namespace and a table."""
    ref = TableRef(
        environment="prod",
        namespace=NamespacePath.from_parts(["gold", "sales"]),
        name="monthly",
    )
    assert ref.environment == "prod"
    assert str(ref.namespace) == "gold.sales"
    assert ref.name == "monthly"


def test_namespace_component_rejects_a_nul_byte() -> None:
    """A NUL byte is never a legitimate identifier character.

    `FsspecStorage._full` rejects it, but `table_location` builds a URI
    without going through `Storage`, so this layer has to reject it too or
    the caller gets a low-level `ValueError` from the OS instead of a
    `ValidationError`.
    """
    with pytest.raises(ValidationError):
        NamespacePath.from_parts(["a\x00b"])


def test_table_name_rejects_a_nul_byte() -> None:
    """The same holds for a table name, which is validated independently."""
    with pytest.raises(ValidationError):
        validate_path_component("a\x00b", what="Table name")


@pytest.mark.parametrize("component", ["a.b", ".hidden", "trailing.", "a..b"])
def test_a_component_containing_a_dot_is_rejected_and_named(component: str) -> None:
    """A literal dot would make ``["a.b"]`` and ``["a", "b"]`` render identically."""
    with pytest.raises(ValidationError, match=r"Namespace component .*contain a '\.'") as exc:
        NamespacePath.from_parts(["gold", component])
    assert repr(component) in str(exc.value)
    with pytest.raises(ValidationError, match=r"Table name .*contain a '\.'"):
        validate_path_component(component, what="Table name")


def test_child_rejects_a_dotted_name() -> None:
    """A child component is a new name, so it is held to the new rule."""
    with pytest.raises(ValidationError, match=r"a\.b"):
        NamespacePath.from_parts(["gold"]).child("a.b")


def test_parse_and_lenient_from_parts_keep_a_dotted_stored_name_readable() -> None:
    """Names created before dots were refused stay reachable: the stored form is not re-checked."""
    stored = NamespacePath.parse(f"gold{NAMESPACE_SEPARATOR}a.b")
    assert stored.parts == ("gold", "a.b")
    assert NamespacePath.from_parts(["a.b"], allow_dots=True).parts == ("a.b",)
    with pytest.raises(ValidationError, match="'/'"):
        NamespacePath.parse("a.b/c")


def test_shown_cuts_a_long_name_and_escapes_what_cannot_be_printed() -> None:
    """A name echoed in a message is at most 200 ASCII characters."""
    assert shown("a" * 100_000) == "a" * 200
    assert shown("a\nb\u00e9") == "a\\nb\\xe9"
    assert shown(7) == "int"


def test_a_long_undotted_name_is_not_echoed_whole() -> None:
    """`TableRef.from_dotted` names the problem without the whole 100 KB."""
    with pytest.raises(ValidationError) as caught:
        TableRef.from_dotted("dev", "x" * 100_000)
    assert len(str(caught.value)) < 300


def test_a_long_name_with_a_dot_in_a_component_is_not_echoed_whole() -> None:
    """The dot rule of `validate_path_component` quotes at most 200 characters."""
    with pytest.raises(ValidationError) as caught:
        validate_path_component("x" * 100_000 + ".y", what="Table name")
    assert len(str(caught.value)) < 300


def test_a_table_reference_refuses_a_nul_in_its_name_or_environment() -> None:
    """A NUL never reaches a query: Postgres would answer it with a server error (#71)."""
    namespace = NamespacePath.from_parts(["models"])
    with pytest.raises(ValidationError, match="Table name must not contain a NUL byte"):
        TableRef("dev", namespace, "ch\x00urn")
    with pytest.raises(ValidationError, match="environment name must not contain a NUL byte"):
        TableRef("de\x00v", namespace, "churn")
    assert TableRef("dev", namespace, "churn").dotted() == "models.churn"


def test_shown_path_keeps_the_start_and_the_whole_file_name_of_a_long_path() -> None:
    """A long storage path loses its middle, so the message still names the file."""
    short = "s3://bucket/warehouse/t/data/00000-1.parquet"
    assert shown_path(short) == short
    long = "s3://bucket/" + "/".join(["d" * 90] * 4) + "/data/00000-1-abc.parquet"
    cut = shown_path(long)
    assert len(cut) <= 200
    assert cut.startswith("s3://bucket/ddd")
    assert cut.endswith("…/00000-1-abc.parquet")
    assert shown_path("s3://b/" + "x" * 500 + "/f\n.parquet").endswith("/f\\n.parquet")


def test_shown_path_cuts_a_file_name_that_is_itself_too_long_from_its_front() -> None:
    """The end of the name, where its extension is, survives."""
    cut = shown_path("s3://bucket/" + "n" * 400 + ".parquet")
    assert len(cut) <= 200
    assert cut.startswith("s3://bucket/")
    assert cut.endswith("n.parquet")
    assert shown_path("n" * 400 + ".parquet").endswith(".parquet")
    assert shown_path(7) == "int"
