"""Retry and timeout policy of one asset: resolution, operator caps, manifest read-back."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from lakana_core.config import QueryLimitsConfig, RunConfig
from lakana_core.projects.frontmatter import AssetMetadata
from lakana_core.projects.issues import ProjectIssue
from lakana_core.projects.parse import ParsedAsset

DEFAULT_RETRIES = 2
"""Retries when neither front-matter nor `lakana.yaml` sets any."""

DEFAULT_RETRY_DELAY_SECONDS = 30
"""First retry delay when unset; doubled per attempt by the scheduler."""


MAX_RETRY_DELAY_SECONDS = 600
"""Largest first retry delay; the scheduler's backoff ceiling (10 minutes)."""


@dataclass(frozen=True)
class RunCaps:
    """Operator maximums a project's run settings must stay within.

    Parameters
    ----------
    max_retries
        `LAKANA_RUN__MAX_RETRIES`.
    max_timeout_seconds
        `LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS`, whole seconds; also the
        default timeout.
    """

    max_retries: int = 5
    max_timeout_seconds: int = 900

    @classmethod
    def from_config(cls, query_limits: QueryLimitsConfig, run: RunConfig) -> RunCaps:
        """Build the caps from the operator's settings.

        Parameters
        ----------
        query_limits
            Supplies `timeout_seconds`.
        run
            Supplies `max_retries`.

        Returns
        -------
        RunCaps
            The timeout rounded down to whole seconds, never below one.
        """
        return cls(
            max_retries=run.max_retries,
            max_timeout_seconds=max(1, int(query_limits.timeout_seconds)),
        )


@dataclass(frozen=True)
class RunPolicy:
    """What applies to one asset's task run.

    Parameters
    ----------
    retries
        Retries after the first attempt (`max_attempts = retries + 1`).
    retry_delay_seconds
        First retry delay.
    timeout_seconds
        Wall-clock limit of one attempt.
    """

    retries: int
    retry_delay_seconds: int
    timeout_seconds: int


def resolve_run_policy(metadata: AssetMetadata, caps: RunCaps) -> RunPolicy:
    """Apply the defaults to an asset's merged metadata.

    Parameters
    ----------
    metadata
        Front-matter merged over `lakana.yaml` defaults.
    caps
        Supplies the default timeout.

    Returns
    -------
    RunPolicy
        The values as configured; caps are checked by `run_policy_issues`.
    """
    return RunPolicy(
        retries=metadata.retries if metadata.retries is not None else DEFAULT_RETRIES,
        retry_delay_seconds=(
            metadata.retry_delay
            if metadata.retry_delay is not None
            else DEFAULT_RETRY_DELAY_SECONDS
        ),
        timeout_seconds=(
            metadata.timeout if metadata.timeout is not None else caps.max_timeout_seconds
        ),
    )


def run_policy_issues(assets: Sequence[ParsedAsset], caps: RunCaps) -> list[ProjectIssue]:
    """Report every asset whose run settings exceed an operator maximum.

    Parameters
    ----------
    assets
        The parsed assets.
    caps
        The operator maximums.

    Returns
    -------
    list of ProjectIssue
        One issue per exceeded cap, on the asset's front-matter line.
    """
    issues: list[ProjectIssue] = []
    for asset in assets:
        policy = resolve_run_policy(asset.metadata, caps)
        if policy.retries > caps.max_retries:
            issues.append(
                ProjectIssue(
                    asset.path,
                    asset.block_line,
                    f"retries {policy.retries} is above the operator maximum of "
                    f"{caps.max_retries} (LAKANA_RUN__MAX_RETRIES)",
                )
            )
        if policy.retry_delay_seconds > MAX_RETRY_DELAY_SECONDS:
            issues.append(
                ProjectIssue(
                    asset.path,
                    asset.block_line,
                    f"retry_delay {policy.retry_delay_seconds}s is above the maximum of "
                    f"{MAX_RETRY_DELAY_SECONDS}s (the retry backoff ceiling)",
                )
            )
        if policy.timeout_seconds > caps.max_timeout_seconds:
            issues.append(
                ProjectIssue(
                    asset.path,
                    asset.block_line,
                    f"timeout {policy.timeout_seconds}s is above the operator maximum of "
                    f"{caps.max_timeout_seconds}s (LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS)",
                )
            )
    return issues


def _whole(value: object, *, minimum: int) -> int | None:
    """Return `value` if it is an integer of at least `minimum`, else `None`.

    Parameters
    ----------
    value
        A value read back from a manifest.
    minimum
        The smallest acceptable value.

    Returns
    -------
    int or None
        The value, or `None` for a boolean, a non-integer or too small a number.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


def policy_from_manifest(entry: Mapping[str, object], caps: RunCaps) -> RunPolicy:
    """Read an asset's policy back from a stored manifest entry.

    Manifests deployed by an older release have no policy keys and get the defaults
    ; a malformed value also falls back to its default rather than
    raising. The current caps are applied, so lowering an operator maximum
    after a deploy takes effect on the next run.

    Parameters
    ----------
    entry
        One element of `manifest_json["assets"]`.
    caps
        The current operator maximums.

    Returns
    -------
    RunPolicy
        The policy to apply.
    """
    retries = _whole(entry.get("retries"), minimum=0)
    delay = _whole(entry.get("retry_delay_seconds"), minimum=0)
    timeout = _whole(entry.get("timeout_seconds"), minimum=1)
    return RunPolicy(
        retries=min(retries if retries is not None else DEFAULT_RETRIES, caps.max_retries),
        retry_delay_seconds=min(
            delay if delay is not None else DEFAULT_RETRY_DELAY_SECONDS,
            MAX_RETRY_DELAY_SECONDS,
        ),
        timeout_seconds=min(
            timeout if timeout is not None else caps.max_timeout_seconds,
            caps.max_timeout_seconds,
        ),
    )
