"""Selection of assets for a run."""

from __future__ import annotations

import pytest

from lakana_core.errors import ValidationError
from lakana_core.runs.selection import (
    MAX_PATTERN_LENGTH,
    MAX_PATTERNS,
    ManifestGraph,
    Selection,
    downstream_closure,
    resolve_selection,
    selected_upstreams,
    unselected_upstreams,
)

_MANIFEST = {
    "assets": [
        {"table": "bronze.raw"},
        {"table": "silver.clean"},
        {"table": "gold.daily"},
        {"table": "gold.weekly"},
        {"table": "other.lonely"},
    ],
    "edges": [
        ["bronze.raw", "silver.clean"],
        ["silver.clean", "gold.daily"],
        ["silver.clean", "gold.weekly"],
    ],
}


@pytest.fixture
def graph() -> ManifestGraph:
    """Build the five-asset DAG above.

    Returns
    -------
    ManifestGraph
        The graph.
    """
    return ManifestGraph.from_manifest(_MANIFEST)


def test_no_patterns_selects_everything(graph: ManifestGraph) -> None:
    """A full run covers the whole DAG."""
    assert resolve_selection(graph, Selection()) == (
        "bronze.raw",
        "gold.daily",
        "gold.weekly",
        "other.lonely",
        "silver.clean",
    )


def test_glob_patterns_union(graph: ManifestGraph) -> None:
    """Patterns are case-sensitive shell globs over dotted names; results are unioned."""
    assert resolve_selection(graph, Selection(patterns=("gold.*", "bronze.raw"))) == (
        "bronze.raw",
        "gold.daily",
        "gold.weekly",
    )


def test_upstream_and_downstream_closures(graph: ManifestGraph) -> None:
    """`upstream` adds every ancestor, `downstream` every descendant; both may be combined."""
    assert resolve_selection(graph, Selection(patterns=("gold.daily",), upstream=True)) == (
        "bronze.raw",
        "gold.daily",
        "silver.clean",
    )
    assert resolve_selection(graph, Selection(patterns=("silver.clean",), downstream=True)) == (
        "gold.daily",
        "gold.weekly",
        "silver.clean",
    )
    both = Selection(patterns=("silver.clean",), upstream=True, downstream=True)
    assert resolve_selection(graph, both) == (
        "bronze.raw",
        "gold.daily",
        "gold.weekly",
        "silver.clean",
    )


def test_a_pattern_matching_nothing_is_422_naming_it(graph: ManifestGraph) -> None:
    """Spec: an unknown pattern is a validation error."""
    with pytest.raises(ValidationError, match=r"--select 'gold\.monthly' matches no asset"):
        resolve_selection(graph, Selection(patterns=("gold.monthly",)))


def test_a_bare_namespace_gets_a_wildcard_hint(graph: ManifestGraph) -> None:
    """`--select gold` suggests `gold.*`."""
    with pytest.raises(ValidationError, match=r"did you mean 'gold\.\*'\?"):
        resolve_selection(graph, Selection(patterns=("gold",)))


def test_patterns_are_case_sensitive(graph: ManifestGraph) -> None:
    """Table names are case-sensitive, so are patterns."""
    with pytest.raises(ValidationError):
        resolve_selection(graph, Selection(patterns=("Gold.*",)))


def test_an_empty_manifest_has_nothing_to_run() -> None:
    """A project version without assets is a validation error, not an empty run."""
    with pytest.raises(ValidationError, match="no assets"):
        resolve_selection(ManifestGraph.from_manifest({"assets": [], "edges": []}), Selection())


@pytest.mark.parametrize(
    "patterns",
    [
        ("",),
        ("   ",),
        ("x" * (MAX_PATTERN_LENGTH + 1),),
        tuple(f"a{i}" for i in range(MAX_PATTERNS + 1)),
    ],
)
def test_malformed_pattern_lists_are_refused(
    graph: ManifestGraph, patterns: tuple[str, ...]
) -> None:
    """Empty, overlong or too many patterns are validation errors, checked before matching."""
    with pytest.raises(ValidationError):
        resolve_selection(graph, Selection(patterns=patterns))


def test_downstream_closure_for_resume(graph: ManifestGraph) -> None:
    """Resume covers the given assets plus everything downstream of them, sorted."""
    assert downstream_closure(graph, ["silver.clean"]) == (
        "gold.daily",
        "gold.weekly",
        "silver.clean",
    )
    assert downstream_closure(graph, ["gone.table"]) == ()


def test_selected_and_unselected_upstreams(graph: ManifestGraph) -> None:
    """Inside a selection, each task waits only for selected upstreams."""
    selected = ["silver.clean", "gold.daily"]
    assert selected_upstreams(graph, selected) == {
        "gold.daily": ("silver.clean",),
        "silver.clean": (),
    }
    assert unselected_upstreams(graph, "silver.clean", selected) == ("bronze.raw",)
    assert unselected_upstreams(graph, "gold.daily", selected) == ()


def test_selection_json_round_trip() -> None:
    """`selection_json` stores the patterns and flags."""
    selection = Selection(patterns=("gold.*",), upstream=True)
    assert selection.to_json() == {"patterns": ["gold.*"], "upstream": True, "downstream": False}
    assert Selection.from_json(selection.to_json()) == selection


def test_selection_from_malformed_json_is_empty() -> None:
    """A stored value of the wrong shape reads back as the full selection, never raises."""
    assert Selection.from_json({"patterns": "gold.*", "upstream": "yes"}) == Selection()


def test_a_manifest_of_the_wrong_shape_is_tolerated() -> None:
    """Entries without a string table and malformed edges are skipped."""
    graph = ManifestGraph.from_manifest(
        {
            "assets": [{"table": "a.b"}, {"nope": 1}, "x"],
            "edges": [["a.b"], ["a.b", 3], "zz", [["x"], "a.b"], [{}, "a.b"], ["a.b", {}]],
        }
    )
    assert (graph.assets, graph.edges) == (("a.b",), ())
    assert ManifestGraph.from_manifest({"assets": "x", "edges": None}).assets == ()


def test_a_diamond_visits_each_asset_once() -> None:
    """Converging paths reach a node twice; the closure still terminates with one copy."""
    graph = ManifestGraph.from_manifest(
        {
            "assets": [{"table": t} for t in ("a.a", "a.b", "a.c", "a.d")],
            "edges": [["a.a", "a.b"], ["a.a", "a.c"], ["a.b", "a.d"], ["a.c", "a.d"]],
        }
    )
    assert downstream_closure(graph, ["a.a"]) == ("a.a", "a.b", "a.c", "a.d")
    assert resolve_selection(graph, Selection(patterns=("a.d",), upstream=True)) == (
        "a.a",
        "a.b",
        "a.c",
        "a.d",
    )
    assert downstream_closure(graph, ["a.b", "a.a"]) == ("a.a", "a.b", "a.c", "a.d")


def test_a_deep_chain_does_not_recurse() -> None:
    """A 1500-asset chain resolves in both directions without hitting the recursion limit."""
    names = [f"deep.t{i:04d}" for i in range(1500)]
    graph = ManifestGraph.from_manifest(
        {
            "assets": [{"table": n} for n in names],
            "edges": [[names[i], names[i + 1]] for i in range(len(names) - 1)],
        }
    )
    up = resolve_selection(graph, Selection(patterns=(names[-1],), upstream=True))
    down = resolve_selection(graph, Selection(patterns=(names[0],), downstream=True))
    assert up == tuple(names)
    assert down == tuple(names)
    assert downstream_closure(graph, [names[0]]) == tuple(names)


def test_unknown_pattern_among_valid_ones_is_still_refused(graph: ManifestGraph) -> None:
    """One bad pattern fails the whole selection, naming that pattern."""
    with pytest.raises(ValidationError, match=r"'nope\.\*' matches no asset"):
        resolve_selection(graph, Selection(patterns=("gold.*", "nope.*")))


def test_a_namespace_hint_is_absent_for_an_unrelated_pattern(graph: ManifestGraph) -> None:
    """The hint only appears when the pattern names a namespace."""
    with pytest.raises(ValidationError) as excinfo:
        resolve_selection(graph, Selection(patterns=("gold.monthly",)))
    assert "did you mean" not in str(excinfo.value)


def test_star_crosses_namespace_dots() -> None:
    """`*` matches dots too: `gold.*` covers nested namespaces, `*.daily` any depth."""
    graph = ManifestGraph.from_manifest(
        {
            "assets": [{"table": t} for t in ("gold.t", "gold.sub.t", "a.b.daily", "x.daily")],
            "edges": [],
        }
    )
    assert resolve_selection(graph, Selection(patterns=("gold.*",))) == ("gold.sub.t", "gold.t")
    assert resolve_selection(graph, Selection(patterns=("*.daily",))) == ("a.b.daily", "x.daily")
