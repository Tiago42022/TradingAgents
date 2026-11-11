"""Absolute-path entry point for the operator's Python -I environment."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from archive_signals.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
