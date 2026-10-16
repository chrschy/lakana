"""Check a pull request against the conventions commitlint cannot express.

commitlint (`commitlint.config.mjs`) checks the PR title. This script checks
the rest, reading `HEAD_REF`, `PR_TITLE` and `PR_BODY` from the environment:

- the branch follows Conventional Branch: `<type>/<lowercase-description>`,
  with release-please and Dependabot branches exempt;
- a title marked breaking (`type!:` or `type(scope)!:`) carries a
  `BREAKING CHANGE: <migration note>` footer in the description's final
  paragraph, where release-please reads it into the changelog;
- HTML comments and Markdown code are ignored throughout;
- every closing keyword (`Closes`, `Fixes`, `Resolves`, ...) names exactly
  one issue, since GitHub closes only the first issue after a keyword.

PRs are squash-merged with the title as subject and the description as body,
so these rules are what the release tooling actually sees.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping

BRANCH_TYPES = ("feat", "fix", "perf", "revert", "docs", "refactor", "test", "ci", "build", "chore")
"""Branch prefixes: the commit types, minus `deps`, which only Dependabot uses."""

_BRANCH = re.compile(rf"^(?:{'|'.join(BRANCH_TYPES)})/[a-z0-9][a-z0-9._-]*$")
_EXEMPT_BRANCH_PREFIXES = ("release-please--", "dependabot/")
_BREAKING_TITLE = re.compile(r"^[a-z]+(?:\([^)]*\))?!:")
_BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE:[ \t]*(\S.*)?$")
_NON_PROSE = re.compile(r"<!--.*?-->|```.*?```|`[^`\n]*`", re.DOTALL)
_PLACEHOLDER = re.compile(r"^<[^>]*>$")
_KEYWORD_WITH_SEVERAL_ISSUES = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#\d+(?:\s*(?:,|and)?\s*#\d+)+",
    re.IGNORECASE,
)


def check_branch(branch: str) -> str | None:
    """Check a branch name against Conventional Branch.

    Parameters
    ----------
    branch
        The pull request's head branch.

    Returns
    -------
    str or None
        An error message, or `None` if the name is fine.
    """
    if branch.startswith(_EXEMPT_BRANCH_PREFIXES) or _BRANCH.match(branch):
        return None
    return (
        f"Branch {branch!r} doesn't follow Conventional Branch: use "
        f"<type>/<lowercase-description> with type one of {', '.join(BRANCH_TYPES)}."
    )


def check_breaking_change_footer(title: str, body: str) -> str | None:
    """Require a `BREAKING CHANGE:` footer when the title is marked breaking.

    Parameters
    ----------
    title
        The pull request title (the squash commit's subject).
    body
        The pull request description (the squash commit's body).

    Returns
    -------
    str or None
        An error message, or `None` if the title isn't breaking or the
        footer is present, in the final paragraph, and carries real text
        (not empty, not the template's `<...>` placeholder).
    """
    if not _BREAKING_TITLE.match(title):
        return None
    paragraphs = [p for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
    last = paragraphs[-1].splitlines() if paragraphs else []
    for line in last:
        match = _BREAKING_FOOTER.match(line.strip())
        if match and match.group(1) and not _PLACEHOLDER.match(match.group(1).strip()):
            return None
    return (
        "The title is marked breaking ('!'), so the description's final paragraph needs "
        "'BREAKING CHANGE: <what changed and how to migrate>' — release-please puts that "
        "text in the changelog."
    )


def check_issue_references(body: str) -> str | None:
    """Require one closing keyword per referenced issue.

    Parameters
    ----------
    body
        The pull request description.

    Returns
    -------
    str or None
        An error message naming the offending phrase, or `None`.
    """
    match = _KEYWORD_WITH_SEVERAL_ISSUES.search(body)
    if match is None:
        return None
    return (
        f"{match.group(0)!r}: use one closing keyword per issue ('Closes #1' then "
        "'Closes #2'); GitHub closes only the first issue after a keyword."
    )


def strip_non_prose(body: str) -> str:
    """Remove HTML comments and Markdown code, where GitHub doesn't act on keywords.

    The PR template's guidance lives in comments, and descriptions quote
    examples in code; stripping both keeps them from counting as footers or
    issue references.

    Parameters
    ----------
    body
        The raw pull request description.

    Returns
    -------
    str
        The description without comments, fenced blocks, or inline code.
    """
    return _NON_PROSE.sub("", body)


def main(environ: Mapping[str, str] | None = None) -> int:
    """Run every check against the pull request described by the environment.

    Parameters
    ----------
    environ
        Where to read `HEAD_REF`, `PR_TITLE` and `PR_BODY`; defaults to
        `os.environ`.

    Returns
    -------
    int
        0 if every check passes, 1 otherwise.
    """
    env = environ if environ is not None else os.environ
    title = env.get("PR_TITLE", "")
    body = strip_non_prose(env.get("PR_BODY") or "")
    errors = [
        error
        for error in (
            check_branch(env.get("HEAD_REF", "")),
            check_breaking_change_footer(title, body),
            check_issue_references(body),
        )
        if error is not None
    ]
    for error in errors:
        print(f"::error::{error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
