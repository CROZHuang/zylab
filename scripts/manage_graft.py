#!/usr/bin/env python3
"""CLI entrypoint for the pinned optional Graft component."""
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import graft_component  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(graft_component.main())
