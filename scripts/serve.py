"""Start the dashboard from any working directory: python scripts/serve.py [--data-dir DIR] [--port N]."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from aime.cli import main  # noqa: E402

sys.exit(main(["serve", *sys.argv[1:]]))
