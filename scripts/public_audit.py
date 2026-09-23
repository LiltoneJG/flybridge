#!/usr/bin/env python3
"""Fail when public source contains private identifiers or unsafe local paths."""

from __future__ import annotations

import re
import subprocess
import sys
import unicodedata
from collections.abc import Iterable
from pathlib import Path

import yaml
from _bootstrap import activate_project_environment

activate_project_environment()

from flybridge_core import ArgumentParser

ROOT = Path(__file__).resolve().parents[1]
# The deny list is supplied at run time so that no tracked file has to spell out
# a private name, not even in an obfuscated form.
SKIP = {".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__"}
BINARY_ASSET_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
_SEPARATOR = r"[\s._/\\-]*"
_BOUNDED_LENGTH = 3


class IdentifierError(ValueError):
    """Raised when the private identifier deny list is missing or unusable."""


def identifiers_from_yaml(raw: str) -> tuple[re.Pattern[str], ...]:
    """Read a strictly shaped private-audit YAML document."""
    try:
        config = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise IdentifierError("private audit configuration is not valid YAML") from exc
    if not isinstance(config, dict):
        raise IdentifierError("private audit configuration must be a mapping")
    unknown = set(config) - {"private_identifiers"}
    if unknown:
        raise IdentifierError(
            "private audit configuration contains unknown keys: " + ", ".join(sorted(unknown))
        )
    values = config.get("private_identifiers")
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise IdentifierError("private_identifiers must be a list of strings")
    return compile_identifiers(values)


def identifiers_from_file(path: Path) -> tuple[re.Pattern[str], ...]:
    """Read private identifiers from a local, untracked YAML file."""
    try:
        return identifiers_from_yaml(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise IdentifierError(f"cannot read private audit configuration: {path}") from exc


def compile_identifiers(values: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    """Compile literal identifiers into separator-tolerant, case-insensitive patterns."""
    patterns: list[re.Pattern[str]] = []
    for value in values:
        token = value.strip()
        if not token:
            continue
        if any(unicodedata.category(character) == "Cc" for character in token):
            raise IdentifierError("private identifiers must not contain control characters")
        body = _SEPARATOR.join(re.escape(character) for character in token)
        if len(token) <= _BOUNDED_LENGTH:
            # Initials and abbreviations occur inside ordinary words, so they
            # only match a whole word or a camel-case part of one. The trailing
            # class is case-sensitive on purpose so that `AbCd` still matches.
            body = r"(?<![0-9A-Za-z])" + body + r"(?-i:(?![0-9a-z]))"
        patterns.append(re.compile(body, re.IGNORECASE))
    if not patterns:
        raise IdentifierError("private_identifiers must list at least one identifier")
    return tuple(patterns)


def _normalized(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return "".join(character for character in normalized if unicodedata.category(character) != "Cf")


def audit_paths(
    root: Path, paths: Iterable[Path], patterns: Iterable[re.Pattern[str]]
) -> list[str]:
    """Return unsafe paths relative to ``root`` from an explicit source set."""
    compiled = tuple(patterns)
    failures: list[str] = []
    for path in sorted(paths):
        if not path.is_file() or any(part in SKIP for part in path.parts):
            continue
        relative = path.relative_to(root).as_posix()
        path_hit = any(pattern.search(_normalized(relative)) for pattern in compiled)
        if path.suffix.lower() in BINARY_ASSET_SUFFIXES:
            # Encoded pixels cannot be audited as text; only the name is checked.
            if path_hit:
                failures.append(relative)
            continue
        try:
            raw = path.read_bytes()
            content = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            failures.append(relative)
            continue
        if b"\0" in raw:
            failures.append(relative)
            continue
        normalized_content = _normalized(content)
        if path_hit or any(pattern.search(normalized_content) for pattern in compiled):
            failures.append(relative)
    return failures


def audit(root: Path, patterns: Iterable[re.Pattern[str]]) -> list[str]:
    """Return files unsafe to put in a public source tree."""
    return audit_paths(root, root.rglob("*"), patterns)


def tracked_paths(root: Path) -> list[Path]:
    """Return the Git-tracked source set that a history-free export can contain."""
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
    return [root / Path(item) for item in result.stdout.decode().split("\0") if item]


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(
        prog="public_audit.py",
        description=(
            "Check source files for private identifiers and paths from an explicit YAML file."
        ),
    )
    parser.add_argument("-r", "--root", type=Path, default=ROOT, help="tree to audit")
    parser.add_argument(
        "--audit-config",
        type=Path,
        required=True,
        help="private-audit YAML file",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        patterns = identifiers_from_file(args.audit_config)
    except IdentifierError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        failures = audit_paths(root, tracked_paths(root), patterns)
    except (OSError, UnicodeError, subprocess.CalledProcessError) as exc:
        print(f"cannot read tracked source files: {exc}", file=sys.stderr)
        return 2
    if failures:
        print("private identifier or local path found:", *failures, sep="\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
