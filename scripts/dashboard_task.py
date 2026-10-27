"""Keep the dashboard running (Windows Task Scheduler: at logon and every 15 minutes).

Starts `aime serve` unless the dashboard already answers on its port or a maintenance window is active
(deploys stop the dashboard on purpose). Each run either exits at once or becomes the dashboard process.
"""

import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))


def listening(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main() -> int:
    from aime import maintenance
    from aime.config import Settings

    settings = Settings.load()
    if maintenance.active(settings.data_dir):
        return 0
    if listening(settings.dashboard_host, settings.dashboard_port):
        return 0
    from aime.cli import main as cli_main

    return cli_main(["serve"])


if __name__ == "__main__":
    sys.exit(main())
