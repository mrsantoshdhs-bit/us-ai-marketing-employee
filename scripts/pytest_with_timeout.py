"""Run pytest with a hard time limit: if the run exceeds it, every thread's stack is printed and the
interpreter exits, so a hung test never leaves a process behind. Usage:
    python scripts/pytest_with_timeout.py <seconds> [pytest args...]
"""

import faulthandler
import sys

import pytest

if __name__ == "__main__":
    limit = int(sys.argv[1])
    faulthandler.dump_traceback_later(limit, exit=True)
    sys.exit(pytest.main(["-q", "-p", "no:cacheprovider", "-o", "addopts=", *sys.argv[2:]]))
