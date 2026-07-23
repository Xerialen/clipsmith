#!/usr/bin/env python3
"""Compatibility wrapper for the canonical render orchestrator.

The implementation lives in ``clipsmith.render`` so tests and downstream code
have one import path. This wrapper preserves the historical operator command:

    python scripts/render/render_candidate.py <candidate-id>
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from clipsmith.render import *  # noqa: F401,F403 - compatibility re-export
from clipsmith.render import main


if __name__ == "__main__":
    main()
