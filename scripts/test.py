#!/usr/bin/env python3
"""Run Flybridge tests without loading external pytest plugins."""

from __future__ import annotations

import os
import subprocess
import sys

from _bootstrap import activate_project_environment

activate_project_environment()

from flybridge_core import ArgumentParser


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(prog="test.py", description=__doc__)
    parser.add_argument("pytest_args", nargs="*", help="arguments passed to pytest")
    args, pytest_args = parser.parse_known_args(argv)
    environment = os.environ.copy()
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args.pytest_args, *pytest_args],
        check=False,
        env=environment,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
