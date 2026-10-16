"""The asset DAG and the project-level checks that need no database."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from lakana_core.projects.issues import ProjectIssue
from lakana_core.projects.parse import ParsedAsset
from lakana_core.projects.project_config import ResolvedProjectConfig


@dataclass(frozen=True)
class ProjectGraph:
    """The DAG of one repository.

    Parameters
    ----------
    edges
        ``(upstream, downstream)`` pairs between assets of this repository,
        sorted. Self-references are omitted.
    external_inputs
        Every referenced table this repository does not produce, sorted.
    """

    edges: tuple[tuple[str, str], ...]
    external_inputs: tuple[str, ...]


def build_graph(assets: Sequence[ParsedAsset]) -> ProjectGraph:
    """Split every asset's references into DAG edges and external inputs.

    Parameters
    ----------
    assets
        The parsed assets.

    Returns
    -------
    ProjectGraph
        Edges and external inputs.
    """
    produced = {asset.table for asset in assets}
    edges: set[tuple[str, str]] = set()
    external: set[str] = set()
    for asset in assets:
        for reference in asset.references:
            if reference == asset.table:
                continue
            if reference in produced:
                edges.add((reference, asset.table))
            else:
                external.add(reference)
    return ProjectGraph(edges=tuple(sorted(edges)), external_inputs=tuple(sorted(external)))


def find_cycle(assets: Sequence[ParsedAsset]) -> list[str] | None:
    """Find one dependency cycle, if any.

    Walks assets in table-name order, depth first along their references in
    reference order, so the reported cycle is deterministic.

    Parameters
    ----------
    assets
        The parsed assets.

    Returns
    -------
    list of str or None
        The cycle's tables, the first repeated at the end; `None` if acyclic.
    """
    upstream = {asset.table: [r for r in asset.references if r != asset.table] for asset in assets}
    done: set[str] = set()
    for root in sorted(upstream):
        if root in done:
            continue
        path = [root]
        on_path = {root}
        frames = [iter(upstream[root])]
        while frames:
            for reference in frames[-1]:
                if reference in on_path:
                    return [*path[path.index(reference) :], reference]
                if reference in done or reference not in upstream:
                    continue
                path.append(reference)
                on_path.add(reference)
                frames.append(iter(upstream[reference]))
                break
            else:
                frames.pop()
                finished = path.pop()
                on_path.discard(finished)
                done.add(finished)
    return None


def _field_present(asset: ParsedAsset, field: str) -> bool:
    """Report whether a required field is set on merged metadata.

    Parameters
    ----------
    asset
        The asset.
    field
        ``description``, ``owner`` or ``tags``.

    Returns
    -------
    bool
        Whether it has a non-empty value.
    """
    return bool(getattr(asset.metadata, field))


def validate_project(
    assets: Sequence[ParsedAsset], config: ResolvedProjectConfig
) -> list[ProjectIssue]:
    """Run every check that needs only the repository itself.

    Parameters
    ----------
    assets
        The parsed assets.
    config
        `lakana.yaml` resolved for the target environment.

    Returns
    -------
    list of ProjectIssue
        Every problem: a cycle, missing required fields, documented columns
        the query does not produce, and undocumented columns under
        `column_docs`.
    """
    issues: list[ProjectIssue] = []
    by_table = {asset.table: asset for asset in assets}
    cycle = find_cycle(assets)
    if cycle is not None:
        first = by_table[cycle[0]]
        issues.append(ProjectIssue(first.path, first.body_line, "cycle: " + " -> ".join(cycle)))
    for asset in sorted(assets, key=lambda a: a.path):
        line = asset.block_line or asset.body_line
        for field in config.requirements.fields:
            if not _field_present(asset, field):
                issues.append(ProjectIssue(asset.path, line, f"missing required field {field!r}"))
        if asset.columns is None:
            continue
        for documented in asset.metadata.columns:
            if documented not in asset.columns:
                issues.append(
                    ProjectIssue(
                        asset.path,
                        line,
                        f"documents column {documented!r}, which the query does not produce",
                    )
                )
        if config.requirements.column_docs:
            for position, column in enumerate(asset.columns, start=1):
                if not column:
                    issues.append(
                        ProjectIssue(
                            asset.path,
                            line,
                            f"column {position} has no name; alias it so it can be documented",
                        )
                    )
                elif column not in asset.metadata.columns:
                    issues.append(
                        ProjectIssue(asset.path, line, f"column {column!r} is not documented")
                    )
    return issues
