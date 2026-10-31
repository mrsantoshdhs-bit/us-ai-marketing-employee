"""Does the dashboard answer with a page? (stdlib only: the watchdog and the dashboard task use it without the app.)

A port that accepts a connection proves little: the operating system accepts for a hung dashboard, and for another
program holding the port. Only a page counts.

A hung dashboard cannot be replaced from outside: the Dashboard task's run IS the dashboard, and Task Scheduler starts no
second copy while that run lives (IgnoreNew). So the dashboard watches itself from a thread of its own, and a dashboard
that has stopped answering ends its own process; the task's next trigger, within 15 minutes, starts a new one.
"""

from __future__ import annotations

import http.client
import os
import sys
import threading
import time
from pathlib import Path

PAGE = "/login"
SELF_CHECK_QUERY = "self_check=1"  # left out of the access log (one line a minute)
PAGE_TIMEOUT = 10.0
CHECK_EVERY = 60.0
FAILURES_TO_END = 5  # five minutes without a page: a slow moment or a waking PC does not end it


def page_answers(host: str, port: int, timeout: float = PAGE_TIMEOUT, path: str = PAGE) -> bool:
    """Any HTTP answer counts, a redirect or a refusal included: something is alive to give it."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path)
        conn.getresponse().read(1)
        return True
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def self_check(host: str, port: int, data_dir: Path, probe=page_answers, sleep=time.sleep, end=os._exit,
               rounds: int | None = None) -> None:
    """The loop of the dashboard's watching thread. It acts only after it has seen a page once, and never during a
    maintenance window (the task does not start a new dashboard then)."""
    from aime import maintenance

    seen, failures = False, 0
    while rounds is None or rounds > 0:
        rounds = None if rounds is None else rounds - 1
        sleep(CHECK_EVERY)
        if probe(host, port, path=f"{PAGE}?{SELF_CHECK_QUERY}"):
            seen, failures = True, 0
            continue
        failures += 1
        if seen and failures >= FAILURES_TO_END and not maintenance.active(data_dir):
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] The dashboard has not answered for {failures} minutes: ending this "
                  "process so that the Dashboard task starts a new one", file=sys.stderr or sys.stdout, flush=True)
            end(3)
            return


def start_self_check(host: str, port: int, data_dir: Path) -> None:
    threading.Thread(target=self_check, args=(host, port, data_dir), name="dashboard-self-check", daemon=True).start()
