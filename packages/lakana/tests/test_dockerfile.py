"""Static checks on the repo's Dockerfile.

Non-root, healthcheck, no build tools in the final stage, a real home for
DuckDB's extension cache, tini as PID 1, and locked third-party versions.
"""

import re
from pathlib import Path

_DOCKERFILE = Path(__file__).resolve().parents[3] / "Dockerfile"


def _all_instructions() -> list[str]:
    r"""Return every logical instruction in the Dockerfile, builder stage included.

    Continuation lines (a physical line ending in `\\`) are joined onto the
    instruction they continue, so a check does not depend on which physical
    line within an instruction happens to carry the text it looks for.

    Returns
    -------
    list of str
        One joined, stripped, non-comment string per Dockerfile instruction.
    """
    raw = [ln.strip() for ln in _DOCKERFILE.read_text().splitlines()]
    raw = [ln for ln in raw if ln and not ln.startswith("#")]
    logical: list[str] = []
    buffer = ""
    for ln in raw:
        buffer = f"{buffer} {ln}".strip()
        if buffer.endswith("\\"):
            buffer = buffer[:-1].rstrip()
        else:
            logical.append(buffer)
            buffer = ""
    if buffer:
        logical.append(buffer)
    return logical


def _final_stage() -> list[str]:
    """Return the logical instructions after the last `FROM`.

    Returns
    -------
    list of str
        The final build stage's instructions, joined per `_all_instructions`.
    """
    instructions = _all_instructions()
    last_from = max(i for i, ln in enumerate(instructions) if ln.upper().startswith("FROM "))
    return instructions[last_from:]


def test_final_stage_runs_as_non_root_uid_10001() -> None:
    """The image runs as `lakana` (uid 10001), never root."""
    users = [ln for ln in _final_stage() if ln.upper().startswith("USER ")]
    assert users and users[-1].split()[1] in {"lakana", "10001", "10001:10001"}


def test_final_stage_has_a_healthz_healthcheck() -> None:
    """Docker and Compose can probe the server without curl."""
    assert any(ln.upper().startswith("HEALTHCHECK") and "/healthz" in ln for ln in _final_stage())


def test_final_stage_sets_a_writable_tmpdir() -> None:
    """DuckDB spills to TMPDIR, a directory created and owned by `lakana`."""
    lines = _final_stage()
    assert any(ln.upper().startswith("ENV ") and "TMPDIR=/var/tmp/lakana" in ln for ln in lines)
    assert any(
        "mkdir -p" in ln and "/var/tmp/lakana" in ln and "chown -R lakana:lakana" in ln
        for ln in lines
    )


def test_final_stage_sets_a_real_home_owned_by_lakana() -> None:
    """`HOME` must exist and be writable, or every worker query fails on `INSTALL httpfs`."""
    lines = _final_stage()
    assert any(ln.upper().startswith("ENV ") and "HOME=/home/lakana" in ln for ln in lines)
    assert any(
        "mkdir -p" in ln and "/home/lakana" in ln and "chown -R lakana:lakana" in ln for ln in lines
    )


def test_final_stage_pre_installs_httpfs_offline() -> None:
    """`httpfs` is installed at build time so no worker query hits the network for it."""
    assert any("INSTALL httpfs" in ln for ln in _final_stage())


def test_final_stage_uses_tini_as_entrypoint() -> None:
    """PID 1 must forward SIGTERM so `docker stop` and Kubernetes shutdown are prompt."""
    entrypoints = [ln for ln in _final_stage() if ln.upper().startswith("ENTRYPOINT")]
    assert entrypoints and "tini" in entrypoints[-1]


def test_builder_installs_from_locked_constraints() -> None:
    """Third-party versions come from `uv.lock`, not an unpinned PyPI resolve.

    The install happens in the builder stage — the final stage has no pip.
    """
    assert any(
        "pip install" in ln and "-c /wheels/constraints.txt" in ln for ln in _all_instructions()
    )


def test_final_stage_installs_no_compilers() -> None:
    """Build tools stay in the builder stage; the final stage installs only runtime packages."""
    text = " ".join(_final_stage())
    for tool in ("build-essential", "gcc", "g++", "make"):
        assert tool not in text


def test_final_stage_installs_git_and_ssh_for_deploys() -> None:
    """The worker fetches repositories with the git binary, over https or ssh."""
    text = " ".join(_final_stage())
    assert "git" in text.split()
    assert "openssh-client" in text.split()
    assert "--no-install-recommends" in text


def test_final_stage_ships_uv_pinned_by_digest() -> None:
    """The `uv` binary on the worker's PATH, from a stage pinned by tag and digest.

    The pin is a `FROM` line because Dependabot's docker ecosystem does not read the image of a
    `COPY --from=<image>` (dependabot-core #5103); `COPY --from=uv` then takes the binary.
    """
    pinned = re.compile(r"^FROM ghcr\.io/astral-sh/uv:\d+\.\d+\.\d+@sha256:[0-9a-f]{64} AS uv$")
    assert [ln for ln in _all_instructions() if pinned.match(ln)], _all_instructions()
    assert "COPY --from=uv /uv /usr/local/bin/uv" in _final_stage()


def test_final_stage_owns_the_worker_cache_directory_privately() -> None:
    """The venv cache must be owned by uid 10001 and mode 0700, or every Python task fails.

    A named volume mounted over an existing directory inherits its owner and mode on first use.
    """
    text = " ".join(_final_stage())
    assert "mkdir -p" in text
    assert re.search(r"chown -R lakana:lakana (/\S+ )*/home/lakana( /\S+)*(?= |$)", text), text
    assert "chmod 0700 /home/lakana/.cache/lakana-worker" in text
    assert "/home/lakana/.cache/lakana-worker" in text


def test_the_image_prepares_the_directories_the_launcher_and_the_worker_mount() -> None:
    """A named volume takes owner and mode from the image's directory on first use."""
    text = _DOCKERFILE.read_text(encoding="utf-8")

    assert "install -d -o 0 -g 10001 -m 0770 /run/lakana-launcher" in text
    # Sticky as well as set-group-id: the worker's group can add files but never rename or
    # delete the token or the socket, so no link can be swapped in under the launcher.
    assert "chmod 3770 /run/lakana-launcher" in text
    assert "install -d -o 0 -g 0 -m 0700 /var/lib/lakana-launcher" in text
    assert "install -d -o 10001 -g 10001 -m 0700 /home/lakana/.cache/lakana-worker/attempts" in text
