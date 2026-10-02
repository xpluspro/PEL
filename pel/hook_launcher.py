"""Absolute launcher usable from any Claude project without an editable install."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pel.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["hook", *sys.argv[1:]]))

