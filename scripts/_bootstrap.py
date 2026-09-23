"""Re-execute a manual script with the checkout's synchronized interpreter."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_PYTHON = ROOT / ".venv" / "bin" / "python"


def activate_project_environment() -> None:
    """Use the interpreter created by ``uv sync --all-packages``."""
    if Path(sys.executable).resolve() == PROJECT_PYTHON.resolve():
        return
    if not PROJECT_PYTHON.is_file():
        raise RuntimeError("run 'uv sync --all-packages' before executing this script")
    os.execv(str(PROJECT_PYTHON), [str(PROJECT_PYTHON), *sys.argv])
