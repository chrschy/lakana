"""Hatchling metadata hook: pin `lakana-core` to this exact release.

hatch-vcs resolves every workspace package's version from the same git tag
(`raw-options.root = "../.."` in each package's `[tool.hatch.version]`), so
`lakana-core` can be pinned to precisely this build's own version with no
hand-edited version anywhere (rule 3): hatchling resolves `project.version`
before any metadata hook's `update()` runs
(`hatchling.metadata.core.ProjectMetadata.core`), so `metadata["version"]`
below is already the real, resolved version. See
`packages/lakana/hatch_build.py`'s `ServerExtraMetadataHook`, which pins
`lakana-server`/`lakana-worker` inside `lakana`'s `server` extra the same way.

`dependencies` is dynamic in `pyproject.toml`, so hatchling requires this
hook to supply the *entire* list, not just the pinned addition. The other
dependencies, with their explanatory comments, live under this hook's own
config table (`[tool.hatch.metadata.hooks.custom]`) so they stay readable as
ordinary TOML instead of being hard-coded here.
"""

from __future__ import annotations

from typing import Any

from hatchling.metadata.plugin.interface import MetadataHookInterface


class CoreDependencyMetadataHook(MetadataHookInterface):
    """Appends a `lakana-core==<version>` pin to the configured dependencies."""

    def update(self, metadata: dict[str, Any]) -> None:
        """Set `dependencies` to the configured list plus a `lakana-core` pin.

        Parameters
        ----------
        metadata
            The `project` table, mutated in place.
        """
        version = metadata["version"]
        metadata["dependencies"] = [*self.config["dependencies"], f"lakana-core=={version}"]
