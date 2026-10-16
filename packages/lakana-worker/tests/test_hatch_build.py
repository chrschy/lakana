"""Tests for `hatch_build.py`'s metadata hook."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_HATCH_BUILD_PATH = Path(__file__).resolve().parents[1] / "hatch_build.py"


def _load_hatch_build() -> ModuleType:
    """Load `packages/lakana-worker/hatch_build.py` as a standalone module.

    It sits outside the `lakana_worker` package on purpose -- hatchling's
    custom metadata hook loader (`[tool.hatch.metadata.hooks.custom]`) looks
    for `hatch_build.py` at the project root, not inside `src/` -- so it's
    loaded here by file path rather than by package import.

    Returns
    -------
    ModuleType
        The loaded module.
    """
    spec = importlib.util.spec_from_file_location("lakana_worker_hatch_build", _HATCH_BUILD_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_core_dependency_is_pinned_to_the_resolved_version() -> None:
    """The hook appends `lakana-core==<version>` to the configured dependency list.

    The other dependencies, supplied via the hook's own config table
    (`[tool.hatch.metadata.hooks.custom]`), pass through unchanged -- only
    `lakana-core` gets a version pin, computed from `metadata["version"]` the
    same way `packages/lakana/hatch_build.py`'s `ServerExtraMetadataHook` pins
    `lakana-server`/`lakana-worker`.
    """
    hatch_build = _load_hatch_build()
    hook = hatch_build.CoreDependencyMetadataHook(
        root=str(_HATCH_BUILD_PATH.parent),
        config={"dependencies": ["duckdb>=1.1"]},
    )
    metadata = {"version": "1.2.3"}

    hook.update(metadata)

    assert metadata["dependencies"] == ["duckdb>=1.1", "lakana-core==1.2.3"]
