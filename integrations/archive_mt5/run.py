"""Absolute-path entry point compatible with the venv's `python -I -u`.

This adds only the known integration directory, not arbitrary cwd/PYTHONPATH.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from archive_mt5.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
