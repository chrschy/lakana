"""`LAKANA_LAUNCHER__*`: what the launcher needs, and the message for each mistake."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lakana_worker.launcher.settings import LauncherSettings, SettingsError, load_settings

_IMAGES = '{"3.13": "ghcr.io/chrschy/lakana-task:0.14.0-py3.13"}'


def test_the_two_required_settings_and_the_defaults() -> None:
    """Only the stack and the images must be set; everything else has a working default."""
    settings = load_settings(
        {"LAKANA_LAUNCHER__STACK": "lakana", "LAKANA_LAUNCHER__IMAGES": _IMAGES}
    )

    assert settings == LauncherSettings(
        stack="lakana",
        images={"3.13": "ghcr.io/chrschy/lakana-task:0.14.0-py3.13"},
        work_volume="lakana-task-work",
    )
    assert settings.socket == Path("/run/lakana-launcher/launcher.sock")
    assert settings.no_new_privileges == "auto"
    assert (settings.max_memory_mb, settings.max_cpus, settings.max_pids) == (65536, 64.0, 32768)


@pytest.mark.parametrize(
    "name",
    [
        "TASK_USER",
        "BUILD_USER",
        "DOCKER_SOCKET",
        "STATE_DIR",
        "EGRESS_KEY",
        "SERVER_SERVICE",
        "SERVER_ALIAS",
        "EGRESS_SERVICE",
        "MAX_SECONDS",
        "KEEP_ENVIRONMENTS",
        "WORK_VOLUME",
    ],
)
def test_only_what_a_person_needs_is_a_setting(name: str) -> None:
    """Users, service names, paths of the stack and the like are constants, not variables.

    Parameters
    ----------
    name
        A former variable, after the prefix.
    """
    environ = {"LAKANA_LAUNCHER__STACK": "lakana", "LAKANA_LAUNCHER__IMAGES": _IMAGES}
    environ["LAKANA_LAUNCHER__" + name] = "0:0"

    with pytest.raises(SettingsError, match=f"{name} is not a setting .it is a constant now"):
        load_settings(environ)

    settings = load_settings(
        {"LAKANA_LAUNCHER__STACK": "lakana", "LAKANA_LAUNCHER__IMAGES": _IMAGES}
    )
    assert settings.task_user == "10001:10001" and settings.build_user == "10002:10002"
    assert settings.docker_socket == Path("/var/run/docker.sock")


def test_the_task_limits_are_settings() -> None:
    """The ceilings of memory, CPUs and processes are the three that a person tunes."""
    settings = load_settings(
        {
            "LAKANA_LAUNCHER__STACK": "lakana",
            "LAKANA_LAUNCHER__IMAGES": _IMAGES,
            "LAKANA_LAUNCHER__MAX_MEMORY_MB": "8192",
            "LAKANA_LAUNCHER__MAX_CPUS": "4",
            "LAKANA_LAUNCHER__MAX_PIDS": "512",
        }
    )

    assert (settings.max_memory_mb, settings.max_cpus, settings.max_pids) == (8192, 4.0, 512)


def test_an_allowed_ingestion_root_is_kept() -> None:
    """A plain host directory passes."""
    settings = load_settings(
        {
            "LAKANA_LAUNCHER__STACK": "lakana",
            "LAKANA_LAUNCHER__IMAGES": _IMAGES,
            "LAKANA_LAUNCHER__INGESTION_ROOTS": '{"landing": "/srv/landing", "x": "/data/x"}',
        }
    )

    assert settings.ingestion_roots == {"landing": "/srv/landing", "x": "/data/x"}


_BASE = {"LAKANA_LAUNCHER__STACK": "lakana", "LAKANA_LAUNCHER__IMAGES": _IMAGES}
_ROOTS = "LAKANA_LAUNCHER__INGESTION_ROOTS"
_SECRET_PATH = "srv/secret-landing"


def _roots(path: str) -> dict[str, str]:
    """Build an environment whose only ingestion root is `path`.

    Parameters
    ----------
    path
        The host directory.

    Returns
    -------
    dict
        The launcher's environment.
    """
    return {**_BASE, _ROOTS: json.dumps({"landing": path})}


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        pytest.param(
            {"LAKANA_LAUNCHER__IMAGES": _IMAGES}, "LAKANA_LAUNCHER__STACK is not set", id="no-stack"
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__STACK": "Lakana"},
            "LAKANA_LAUNCHER__STACK must be",
            id="stack-upper",
        ),
        pytest.param(
            {"LAKANA_LAUNCHER__STACK": "lakana"},
            "LAKANA_LAUNCHER__IMAGES is not set",
            id="no-images",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__IMAGES": '{"3.10": "x"}'},
            "LAKANA_LAUNCHER__IMAGES must be",
            id="python-3-10",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__IMAGES": '{"3.13": "a b"}'},
            "LAKANA_LAUNCHER__IMAGES must be",
            id="image-space",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__IMAGES": "[1]"},
            "LAKANA_LAUNCHER__IMAGES must be",
            id="images-list",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__NO_NEW_PRIVILEGES": "off"},
            "must be auto or on",
            id="nnp-off",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__MAX_PIDS": "lots"},
            "LAKANA_LAUNCHER__MAX_PIDS must be",
            id="pids-text",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__MAX_PIDS": "\u00b2"},
            "LAKANA_LAUNCHER__MAX_PIDS must be",
            id="pids-superscript",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__MAX_CPUS": "0"},
            "LAKANA_LAUNCHER__MAX_CPUS must be",
            id="cpus-zero",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__MAX_MEMORY_MB": "1"},
            "LAKANA_LAUNCHER__MAX_MEMORY_MB must be",
            id="memory-tiny",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__SOCKET": "launcher.sock"},
            "LAKANA_LAUNCHER__SOCKET must be an absolute path",
            id="socket-relative",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__SOCKET": "/run/../x.sock"},
            "LAKANA_LAUNCHER__SOCKET must be an absolute path",
            id="socket-dots",
        ),
        pytest.param({**_BASE, _ROOTS: "[1]"}, "INGESTION_ROOTS must be", id="roots-list"),
        pytest.param({**_BASE, _ROOTS: "{nope"}, "INGESTION_ROOTS must be", id="roots-bad-json"),
        pytest.param(
            {**_BASE, _ROOTS: '{"Bad Name": "/srv/x"}'}, "INGESTION_ROOTS must be", id="root-name"
        ),
        pytest.param({**_BASE, _ROOTS: '{"x": 1}'}, "INGESTION_ROOTS must be", id="root-not-text"),
        pytest.param(_roots("srv/x"), "INGESTION_ROOTS must be", id="root-relative"),
        pytest.param(_roots("/srv/../etc"), "INGESTION_ROOTS must be", id="root-dots"),
        pytest.param(_roots("/srv/x/"), "INGESTION_ROOTS must be", id="root-trailing-slash"),
        pytest.param(_roots("/"), "INGESTION_ROOTS must be", id="root-slash"),
        pytest.param(_roots("/run"), "INGESTION_ROOTS must be", id="root-run"),
        pytest.param(
            _roots("/run/lakana-launcher"), "INGESTION_ROOTS must be", id="root-under-run"
        ),
        pytest.param(_roots("/var"), "INGESTION_ROOTS must be", id="root-var-holds-docker-socket"),
        pytest.param(_roots("/var/run"), "INGESTION_ROOTS must be", id="root-var-run"),
        pytest.param(_roots("/var/run/docker"), "INGESTION_ROOTS must be", id="root-under-var-run"),
        pytest.param(_roots("/var/lib"), "INGESTION_ROOTS must be", id="root-var-lib-holds-docker"),
        pytest.param(
            _roots("/var/lib/docker"), "INGESTION_ROOTS must be", id="root-var-lib-docker"
        ),
        pytest.param(
            _roots("/var/lib/docker/volumes"), "INGESTION_ROOTS must be", id="root-under-docker"
        ),
        pytest.param(_roots("/proc"), "INGESTION_ROOTS must be", id="root-proc"),
        pytest.param(_roots("/proc/1/root"), "INGESTION_ROOTS must be", id="root-under-proc"),
        pytest.param(_roots("/sys"), "INGESTION_ROOTS must be", id="root-sys"),
        pytest.param(_roots("/sys/fs"), "INGESTION_ROOTS must be", id="root-under-sys"),
        pytest.param(_roots("/dev"), "INGESTION_ROOTS must be", id="root-dev"),
        pytest.param(_roots("/dev/shm"), "INGESTION_ROOTS must be", id="root-under-dev"),
        pytest.param(_roots("/etc"), "INGESTION_ROOTS must be", id="root-etc"),
        pytest.param(_roots("/etc/ssl"), "INGESTION_ROOTS must be", id="root-under-etc"),
        pytest.param(_roots("//"), "INGESTION_ROOTS must be", id="root-double-slash"),
        pytest.param(_roots("/srv/a\nb"), "INGESTION_ROOTS must be", id="root-newline"),
        pytest.param(_roots("/srv/a\x00b"), "INGESTION_ROOTS must be", id="root-nul"),
        pytest.param(_roots("/srv/a\x1bb"), "INGESTION_ROOTS must be", id="root-escape"),
        pytest.param(_roots("/srv/a\x7fb"), "INGESTION_ROOTS must be", id="root-delete"),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__SOCKET": "/run/a\nb.sock"},
            "SOCKET must be an absolute path",
            id="socket-newline",
        ),
        pytest.param(_roots("//run"), "INGESTION_ROOTS must be", id="root-double-slash-run"),
        pytest.param(
            _roots("//var/run"), "INGESTION_ROOTS must be", id="root-double-slash-var-run"
        ),
        pytest.param(
            _roots("//var/lib/docker/volumes"),
            "INGESTION_ROOTS must be",
            id="root-double-slash-docker",
        ),
        pytest.param(
            _roots("//srv/landing"), "INGESTION_ROOTS must be", id="root-double-slash-plain"
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__SOCKET": "//run/x.sock"},
            "SOCKET must be an absolute path",
            id="socket-double-slash",
        ),
        pytest.param(
            _roots("/var/lib/containerd"), "INGESTION_ROOTS must be", id="root-containerd"
        ),
        pytest.param(_roots("/var/snap/docker"), "INGESTION_ROOTS must be", id="root-snap-docker"),
        pytest.param(_roots("/var/snap"), "INGESTION_ROOTS must be", id="root-holds-snap-docker"),
        pytest.param(_roots("/root"), "INGESTION_ROOTS must be", id="root-root-home"),
        pytest.param(_roots("/root/data"), "INGESTION_ROOTS must be", id="root-under-root-home"),
        pytest.param(_roots("/boot"), "INGESTION_ROOTS must be", id="root-boot"),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__IMAGES": '{"3.13": "a:1", "3.13": "b:2"}'},
            "repeats",
            id="images-repeated-key",
        ),
        pytest.param(
            {**_BASE, _ROOTS: '{"a": "/srv/a", "a": "/srv/b"}'},
            "INGESTION_ROOTS must be",
            id="roots-repeated-key",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__MAX_MEMORY": "1"},
            "LAKANA_LAUNCHER__MAX_MEMORY is not a setting; the settings are",
            id="unknown-variable",
        ),
        pytest.param(
            {**_BASE, "LAKANA_LAUNCHER__TASK_USER": "0:0"},
            "LAKANA_LAUNCHER__TASK_USER is not a setting",
            id="former-variable",
        ),
        pytest.param(
            {**_roots("/srv"), "LAKANA_LAUNCHER__SOCKET": "/srv/launcher/launcher.sock"},
            "INGESTION_ROOTS must be",
            id="root-holds-the-launcher-socket",
        ),
        pytest.param(
            {**_roots("/srv/launcher"), "LAKANA_LAUNCHER__SOCKET": "/srv/launcher/launcher.sock"},
            "INGESTION_ROOTS must be",
            id="root-is-the-launcher-socket-directory",
        ),
    ],
)
def test_each_mistake_names_the_setting_and_never_echoes_a_value(
    environ: dict[str, str], message: str
) -> None:
    """The ingestion roots are host paths; a message names the setting, not the value.

    Parameters
    ----------
    environ
        The launcher's environment.
    message
        Part of the error.
    """
    with pytest.raises(SettingsError, match=message) as caught:
        load_settings(environ)
    for name, value in environ.items():
        if name == _ROOTS:
            assert value not in str(caught.value)
    assert _SECRET_PATH not in str(caught.value)


def test_a_refused_root_does_not_name_its_path() -> None:
    """The path an operator wrote stays out of the log."""
    with pytest.raises(SettingsError) as caught:
        load_settings(_roots("/etc/" + _SECRET_PATH))
    assert _SECRET_PATH not in str(caught.value)


def test_a_blank_former_variable_is_ignored_and_a_set_one_says_to_remove_it() -> None:
    """Compose passes empty values for unset variables; only a value is a mistake."""
    blank = {**_BASE, "LAKANA_LAUNCHER__TASK_USER": "  "}

    assert load_settings(blank) == load_settings(_BASE)
    with pytest.raises(SettingsError, match=r"TASK_USER is not a setting .*remove it"):
        load_settings({**_BASE, "LAKANA_LAUNCHER__TASK_USER": "0:0"})
