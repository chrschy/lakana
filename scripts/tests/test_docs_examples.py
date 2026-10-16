"""Tests for the documentation examples and their runner."""

import os
import re
import subprocess
from pathlib import Path

from scripts.docs_blocks import read_source

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "docs" / "examples"


def test_every_example_has_an_executable_run_script_with_the_standard_header() -> None:
    """An example is a directory with `run.sh`; `project/` is optional."""
    examples = sorted(p for p in EXAMPLES.iterdir() if p.is_dir())
    assert examples, "docs/examples holds no example; add docs/examples/<name>/run.sh"
    for example in examples:
        script = example / "run.sh"
        relative = script.relative_to(ROOT)
        assert script.is_file(), f"{relative} is missing; every example directory needs a run.sh"
        assert script.read_text(encoding="utf-8").startswith(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
        ), f"{relative} must start with the lines `#!/usr/bin/env bash` and `set -euo pipefail`"
        assert script.stat().st_mode & 0o111, f"{relative} is not executable; run chmod +x on it"


def test_every_region_is_read_back_by_the_block_checker() -> None:
    """The checker's own rule decides: unbalanced or doubled regions raise, empty ones fail here."""
    names = re.compile(r"docs:(?:start|end)\s+([A-Za-z0-9_-]+)")
    scripts = sorted(EXAMPLES.glob("*/*.sh"))
    assert scripts
    for script in scripts:
        found = set(names.findall(script.read_text(encoding="utf-8")))
        relative = script.relative_to(ROOT).as_posix()
        for name in sorted(found):
            assert read_source(ROOT, f"{relative}#{name}").strip(), (
                f"{relative}#{name} is empty; put the lines a page shows between its markers"
            )
    runs = [p for p in scripts if p.name == "run.sh"]
    for run in runs:
        assert "docs:start" in run.read_text(encoding="utf-8"), (
            f"{run.relative_to(ROOT)} has no `docs:start <name>` region; wrap each command "
            "a page shows in `# docs:start <name>` and `# docs:end <name>`"
        )


def test_the_scripts_pass_a_syntax_check() -> None:
    """`bash -n` on the runner and on every shell script of every example."""
    for script in [ROOT / "scripts" / "run_docs_examples.sh", *EXAMPLES.glob("*/*.sh")]:
        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"{script.relative_to(ROOT)} has a syntax error: {result.stderr}"
        )


def test_the_runner_refuses_an_unknown_example() -> None:
    """A typo in the name must not pass as "nothing to run"."""
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "run_docs_examples.sh"), "no-such-example"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "no-such-example" in result.stderr
    assert "docs/examples" in result.stderr


RUNNER = ROOT / "scripts" / "run_docs_examples.sh"
_STUB = """#!/usr/bin/env bash
# A stand-in for `lakana`: what it was asked is appended to $STUB_CALLS.
echo "$*" >> "$STUB_CALLS"
case "$*" in
  "--output json runs list "*)
    [ -n "${STUB_LIST_FAILS:-}" ] && { echo "boom" >&2; exit 3; }
    cat "$STUB_LIST"
    ;;
  "runs logs bad-log "*) echo "no such run" >&2; exit 1 ;;
  "runs logs "*) echo "BUILD OUTPUT of $3" ;;
esac
"""
_LIST = """[
  {"id": "run-ok", "status": "succeeded", "repository": "r"},
  {"id": "bad-log", "status": "failed", "repository": "r"},
  {"id": "run-failed", "status": "failed", "repository": "r"},
  {"id": "run-cancelled", "status": "cancelled", "repository": "r"}
]"""


def _print_failed_run_logs(
    tmp_path: Path, listing: str = _LIST, **env: str
) -> subprocess.CompletedProcess[str]:
    """Run the runner's `print_failed_run_logs` against a stub `lakana` on PATH.

    Parameters
    ----------
    tmp_path
        Where the stub, its listing and its call log go.
    listing
        What the stub prints for `runs list`.
    **env
        Extra environment variables for the stub.

    Returns
    -------
    subprocess.CompletedProcess
        The finished shell, output captured as text.
    """
    text = RUNNER.read_text(encoding="utf-8")
    match = re.search(r"^print_failed_run_logs\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert match, "scripts/run_docs_examples.sh has no print_failed_run_logs function"
    stub = tmp_path / "bin" / "lakana"
    stub.parent.mkdir()
    stub.write_text(_STUB, encoding="utf-8")
    stub.chmod(0o755)
    (tmp_path / "list.json").write_text(listing, encoding="utf-8")
    (tmp_path / "calls").touch()
    environ = {
        "PATH": f"{stub.parent}:/usr/bin:/bin",
        "STUB_LIST": str(tmp_path / "list.json"),
        "STUB_CALLS": str(tmp_path / "calls"),
        **env,
    }
    script = (
        f"set -euo pipefail\n{match.group(0)}\nprint_failed_run_logs dev\necho reached-the-end\n"
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False, env=environ
    )


def test_a_failed_example_prints_the_logs_of_the_runs_that_did_not_succeed(tmp_path: Path) -> None:
    """The listing is machine-readable (`--output json`); a succeeded run is left out."""
    result = _print_failed_run_logs(tmp_path)
    assert result.returncode == 0
    assert "reached-the-end" in result.stdout
    assert "logs of the runs that did not succeed" in result.stderr
    assert "BUILD OUTPUT of run-failed" in result.stderr
    assert "BUILD OUTPUT of run-cancelled" in result.stderr
    assert "run-ok" not in result.stderr
    calls = (tmp_path / "calls").read_text(encoding="utf-8")
    assert "--output json runs list --env dev" in calls
    assert "runs logs run-failed --env dev" in calls


def test_a_log_that_cannot_be_read_costs_one_line_and_the_rest_still_prints(tmp_path: Path) -> None:
    """A failing `lakana runs logs` neither stops the loop nor changes the exit status."""
    result = _print_failed_run_logs(tmp_path)
    assert result.returncode == 0
    assert "could not read the logs of run bad-log" in result.stderr
    assert result.stderr.index("bad-log") < result.stderr.index("BUILD OUTPUT of run-failed")


def test_a_listing_that_fails_or_is_not_json_costs_one_line(tmp_path: Path) -> None:
    """The original failure stays the story: no traceback, no exit status of its own."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    failing = _print_failed_run_logs(tmp_path / "a", STUB_LIST_FAILS="1")
    garbage = _print_failed_run_logs(tmp_path / "b", listing="not json")
    for result in (failing, garbage):
        assert result.returncode == 0
        assert "reached-the-end" in result.stdout
        assert "Traceback" not in result.stderr
    assert "could not list the runs" in failing.stderr
    assert "no run that did not succeed" in garbage.stderr


def test_a_listing_without_a_failed_run_says_so(tmp_path: Path) -> None:
    """Nothing to show is said, so the heading is not mistaken for a hang."""
    result = _print_failed_run_logs(tmp_path, listing='[{"id": "a", "status": "succeeded"}]')
    assert result.returncode == 0
    assert "no run that did not succeed" in result.stderr


def test_the_runner_prints_them_before_it_stops_the_instance() -> None:
    """In `run_one`, after the instance's log and before `cleanup`."""
    text = RUNNER.read_text(encoding="utf-8")
    body = text[text.index("run_one() {") :]
    assert (
        body.index("the instance's log:")
        < body.index("print_failed_run_logs dev")
        < body.index("\n  cleanup\n")
    )


_START_STUB = """#!/usr/bin/env bash
# A stand-in for `lakana server start`: says it is up, then waits to be stopped.
echo "Lakana is running"
exec sleep 60
"""


def _run_with_budget_file(
    tmp_path: Path, budget_file: str | None, **env: str
) -> subprocess.CompletedProcess[str]:
    """Run the runner on a one-example tree and a stub `lakana`; the example just succeeds.

    Parameters
    ----------
    tmp_path
        A scratch directory that holds the copy of the runner and the example.
    budget_file
        What the example's `budget` file holds; `None` for no file.
    **env
        Extra environment variables (`LAKANA_DOCS_EXAMPLE_BUDGET`).

    Returns
    -------
    subprocess.CompletedProcess
        The finished runner.
    """
    scripts = tmp_path / "scripts"
    example = tmp_path / "docs" / "examples" / "demo"
    bin_dir = tmp_path / "bin"
    for folder in (scripts, example, bin_dir):
        folder.mkdir(parents=True)
    (scripts / "run_docs_examples.sh").write_text(RUNNER.read_text(encoding="utf-8"))
    (example / "run.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    if budget_file is not None:
        (example / "budget").write_text(budget_file)
    stub = bin_dir / "lakana"
    stub.write_text(_START_STUB)
    stub.chmod(0o755)
    environment = {
        key: value for key, value in os.environ.items() if key != "LAKANA_DOCS_EXAMPLE_BUDGET"
    }
    environment.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
            **env,
        }
    )
    return subprocess.run(
        ["bash", str(scripts / "run_docs_examples.sh"), "demo"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=environment,
    )


def test_a_budget_file_sets_the_budget_of_its_example(tmp_path: Path) -> None:
    """A valid `budget` file is the example's budget when the environment's is shorter."""
    result = _run_with_budget_file(tmp_path, "300\n")
    assert result.returncode == 0, result.stderr
    assert "budget 300 s" in result.stdout


def test_the_longer_of_the_file_and_the_environment_applies(tmp_path: Path) -> None:
    """A shorter file does not lower `LAKANA_DOCS_EXAMPLE_BUDGET`; a longer one raises it."""
    shorter = _run_with_budget_file(tmp_path / "a", "200", LAKANA_DOCS_EXAMPLE_BUDGET="240")
    assert "budget 240 s" in shorter.stdout, shorter.stdout
    longer = _run_with_budget_file(tmp_path / "b", "300", LAKANA_DOCS_EXAMPLE_BUDGET="240")
    assert "budget 300 s" in longer.stdout, longer.stdout


def test_a_budget_file_that_is_not_a_number_is_refused_with_its_name(tmp_path: Path) -> None:
    """A bad value exits 2 and names the file, instead of running with a guess."""
    result = _run_with_budget_file(tmp_path, "five minutes\n")
    assert result.returncode == 2
    assert "docs/examples/demo/budget" in result.stderr
    assert "one number of seconds" in result.stderr
