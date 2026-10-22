"""`docker_stack`'s listing, without Docker: an object removed while it is listed is skipped."""

from __future__ import annotations

from typing import Any

import pytest
from docker_stack import labelled_names

docker_errors = pytest.importorskip("docker.errors")


class _RacingApi:
    """A low-level client whose objects are gone by the time anyone inspects them."""

    def __init__(self) -> None:
        self.filters: list[dict[str, Any]] = []

    def containers(self, *, all: bool, filters: dict[str, Any]) -> list[dict[str, Any]]:
        """List two containers, as Docker answers the list call.

        Parameters
        ----------
        all
            Stopped ones too.
        filters
            Recorded.

        Returns
        -------
        list of dict
            Their list entries.
        """
        self.filters.append(filters)
        return [{"Id": "c1", "Names": ["/lt1-task-a"]}, {"Id": "c2", "Names": ["/lt1-task-b"]}]

    def networks(self, *, filters: dict[str, Any]) -> list[dict[str, Any]]:
        """List one network.

        Parameters
        ----------
        filters
            Recorded.

        Returns
        -------
        list of dict
            Its list entry.
        """
        self.filters.append(filters)
        return [{"Id": "n1", "Name": "lt1-task-a"}]

    def inspect_container(self, ident: str) -> dict[str, Any]:
        """Fail as for a container the launcher removed after the list call.

        Parameters
        ----------
        ident
            The container.

        Raises
        ------
        docker.errors.NotFound
            Always.
        """
        raise docker_errors.NotFound(f"No such container: {ident}")

    inspect_network = inspect_container


def test_labelled_names_come_from_the_list_answers_and_inspect_nothing() -> None:
    """CI's NotFound: `containers.list` inspected a container `stop` had just removed."""
    api = _RacingApi()

    assert labelled_names(api, "lakana.launcher.stack=lt1") == [
        "lt1-task-a",
        "lt1-task-b",
        "lt1-task-a",
    ]
    assert api.filters == [{"label": ["lakana.launcher.stack=lt1"]}] * 2
