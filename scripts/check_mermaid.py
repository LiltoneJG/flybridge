"""Render Mermaid diagrams in Markdown files to catch syntax errors."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

MERMAID_CLI = "@mermaid-js/mermaid-cli@12.0.0"
MERMAID_FENCE = re.compile(r"(?m)^[ \t]{0,3}(?:`{3,}|:{3,})mermaid(?:[ \t(]|$)")


def main(paths: list[str]) -> int:
    if not shutil.which("npx"):
        print("Mermaid check requires Node.js and npx (Node.js 22.13 or newer).", file=sys.stderr)
        return 1

    failures = 0
    with TemporaryDirectory(prefix="flybridge-mermaid-") as directory:
        temporary = Path(directory)
        puppeteer_config = temporary / "puppeteer.json"
        puppeteer_config.write_text(json.dumps({"args": ["--no-sandbox"]}))

        for index, path_string in enumerate(paths):
            path = Path(path_string)
            if not MERMAID_FENCE.search(path.read_text(encoding="utf-8")):
                continue

            output = temporary / f"rendered-{index}.md"
            result = subprocess.run(
                [
                    "npx",
                    "--yes",
                    MERMAID_CLI,
                    "-p",
                    str(puppeteer_config),
                    "-i",
                    str(path),
                    "-o",
                    str(output),
                    "-a",
                    str(temporary / f"images-{index}"),
                    "-j",
                    "2",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if result.returncode:
                failures += 1
                print(f"Mermaid rendering failed in {path}:\n{result.stdout}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
