"""Scheduled scripts ask for normal priority: Task Scheduler starts tasks low, and a starved tick took 17 minutes to
reach its first job on a busy PC."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aime import process_priority
from aime.scheduler import windows

ROOT = Path(__file__).resolve().parent.parent
only_windows = pytest.mark.skipif(os.name != "nt", reason="Windows process priorities")
SCRIPT = ("import json; from aime import process_priority as p; before = p.current(); ok = p.use_normal_priority(); "
          "print(json.dumps([before, ok, p.current()]))")


@only_windows
def test_a_low_priority_process_gets_normal_priority_and_its_children_inherit_it():
    done = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, cwd=ROOT, check=True,
                          creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS)
    before, ok, after = json.loads(done.stdout)
    assert before["class"] == hex(subprocess.BELOW_NORMAL_PRIORITY_CLASS)
    assert ok is True and after == {"class": hex(subprocess.NORMAL_PRIORITY_CLASS), "memory": 5, "io": 2}


def test_it_never_fails_a_scheduled_run_elsewhere():
    if os.name != "nt":
        assert process_priority.use_normal_priority() is False and process_priority.current() == {}
    else:
        assert process_priority.use_normal_priority() is True


def test_the_scheduled_scripts_ask_for_it_before_loading_the_app():
    for name, app in (("run_tick.py", "from aime.cli import main"), ("dashboard_task.py", "from aime.cli import main")):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert 0 < text.index("use_normal_priority()") < text.index(app)


def test_new_task_definitions_are_not_low_priority():
    source = (ROOT / "aime" / "scheduler" / "windows.py").read_text(encoding="utf-8")
    assert "<Priority>6</Priority>" in source and "<Priority>7</Priority>" not in source and windows.TASK_NAME
