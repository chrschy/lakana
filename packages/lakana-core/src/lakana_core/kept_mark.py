"""What a reader of a table's row saw of its mark of kept data.

A leaf module: the catalog, the run store and the worker all name this type, and none of them may
import another to reach it.
"""

from __future__ import annotations

import uuid
from typing import NamedTuple

__all__ = ["KeptSeen"]


class KeptSeen(NamedTuple):
    """What a reader of a table's row saw of its mark, to be judged against later.

    Parameters
    ----------
    run_id
        `Table.kept_run_id`; `None` when the table had no mark.
    task_run_id
        `Table.kept_task_run_id`.
    snapshot_id
        `Table.kept_snapshot_id`.
    """

    run_id: uuid.UUID | None
    task_run_id: uuid.UUID | None
    snapshot_id: int | None

    @property
    def noted(self) -> bool:
        """Say whether the table had a mark.

        Returns
        -------
        bool
            `True` when `run_id` is set.
        """
        return self.run_id is not None
