"""Validation findings with a file and line, collected rather than raised one at a time."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from lakana_core.errors import ValidationError


@dataclass(frozen=True)
class ProjectIssue:
    """One problem found in a project.

    Parameters
    ----------
    path
        Repository-relative POSIX path of the file, e.g. ``assets/bronze/x.sql``.
    line
        1-based line, or `None` when the problem has no single line.
    message
        What is wrong, phrased for the person who wrote the file.
    """

    path: str
    line: int | None
    message: str

    def render(self) -> str:
        """Format as ``path:line: message`` (or ``path: message``).

        Returns
        -------
        str
            The one-line form shown in `error_message` and the CLI.
        """
        where = self.path if self.line is None else f"{self.path}:{self.line}"
        return f"{where}: {self.message}"


class ProjectValidationError(ValidationError):
    """Every issue found in a project, raised together.

    Parameters
    ----------
    issues
        The findings, in the order found. Must not be empty.
    """

    def __init__(self, issues: Sequence[ProjectIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("\n".join(issue.render() for issue in self.issues))
