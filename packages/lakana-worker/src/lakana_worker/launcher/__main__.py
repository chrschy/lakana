"""Entry point: `lakana-launcher`."""

from __future__ import annotations

import logging
import os

from lakana_worker.launcher.engine import DockerEngine, EngineError
from lakana_worker.launcher.server import remove_stale_socket, serve
from lakana_worker.launcher.service import Launcher, StartError
from lakana_worker.launcher.settings import SettingsError, load_settings

logger = logging.getLogger("lakana_worker.launcher")


def main() -> None:
    """Read the settings, remove a stale socket, start the launcher and serve its socket.

    Raises
    ------
    SystemExit
        With status 2 and the reason in the log when a setting is wrong, the engine is too old,
        `no-new-privileges` is required and breaks, the socket directory is not as the image
        prepares it, or Docker cannot be reached. An ingestion root that fails its probe does
        not stop the launcher: it is marked unusable and the reason is logged.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        settings = load_settings(os.environ)
        remove_stale_socket(settings.socket)
        launcher = Launcher(settings, DockerEngine(settings.docker_socket))
        launcher.start()
        serve(launcher)
    except (SettingsError, StartError, EngineError, OSError) as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
