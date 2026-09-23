from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# Synthetic stand-ins. The real deny list is supplied through the environment at
# run time and is never written into this repository, not even in pieces.
DENIED = "acmeprivate"
DENIED_INITIALS = "qz"


def _audit_module():
    script = Path(__file__).parents[1] / "scripts" / "public_audit.py"
    spec = importlib.util.spec_from_file_location("public_audit", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(script.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def _patterns(module):
    return module.compile_identifiers([DENIED, DENIED_INITIALS])


def _full_width(value: str) -> str:
    return "".join(chr(ord(character) + 0xFEE0) for character in value)


def test_audit_accepts_a_clean_tree(tmp_path: Path) -> None:
    (tmp_path / "readme.md").write_text("public content", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "notes.md").write_text("also public", encoding="utf-8")
    module = _audit_module()

    assert module.audit(tmp_path, _patterns(module)) == []


@pytest.mark.parametrize(
    "value",
    [
        DENIED,
        DENIED.upper(),
        "-".join(DENIED),
        "acme_private",
        "acme private",
        "/mnt/acme.private/notes",
        r"C:\acme_private\projects",
        "mnt-acme-private-projects",
        f"{DENIED_INITIALS}_suffix",
        f"{DENIED_INITIALS.capitalize()}Task",
        f"{DENIED_INITIALS.upper()}Task",
    ],
)
def test_audit_rejects_identifier_and_workspace_variants(tmp_path: Path, value: str) -> None:
    fixture = tmp_path / "fixture.txt"
    fixture.write_text(value, encoding="utf-8")
    module = _audit_module()

    assert module.audit(tmp_path, _patterns(module)) == ["fixture.txt"]


@pytest.mark.parametrize("value", ["a" + DENIED_INITIALS, "bu" + DENIED_INITIALS + "er"])
def test_audit_keeps_short_identifiers_out_of_ordinary_words(tmp_path: Path, value: str) -> None:
    fixture = tmp_path / "fixture.txt"
    fixture.write_text(value, encoding="utf-8")
    module = _audit_module()

    assert module.audit(tmp_path, _patterns(module)) == []


def test_audit_rejects_unicode_normalized_content_and_file_names(tmp_path: Path) -> None:
    content_fixture = tmp_path / "content.txt"
    content_fixture.write_text(_full_width(DENIED), encoding="utf-8")
    name_fixture = tmp_path / f"{_full_width(DENIED)}-notes.txt"
    name_fixture.write_text("public", encoding="utf-8")
    module = _audit_module()

    assert module.audit(tmp_path, _patterns(module)) == [
        "content.txt",
        f"{_full_width(DENIED)}-notes.txt",
    ]


def test_audit_rejects_zero_width_identifier_obfuscation(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("acme\u200bprivate", encoding="utf-8")
    module = _audit_module()

    assert module.audit(tmp_path, _patterns(module)) == ["fixture.txt"]


@pytest.mark.parametrize("content", [b"\xff\xfeunsupported", b"public\0private"])
def test_audit_fails_closed_for_non_utf8_or_nul_content(tmp_path: Path, content: bytes) -> None:
    fixture = tmp_path / "fixture.txt"
    fixture.write_bytes(content)
    module = _audit_module()

    assert module.audit(tmp_path, _patterns(module)) == ["fixture.txt"]


def test_command_audit_uses_tracked_source_not_ignored_build_output(tmp_path: Path) -> None:
    module = _audit_module()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "source.md").write_text("public", encoding="utf-8")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "artifact.whl").write_text(f"/mnt/{DENIED}/notes", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("dist/\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.md", ".gitignore"], cwd=tmp_path, check=True)

    assert module.audit_paths(tmp_path, module.tracked_paths(tmp_path), _patterns(module)) == []


def test_audit_command_uses_an_explicit_configuration(tmp_path: Path) -> None:
    module = _audit_module()
    config = tmp_path / "private-audit.yaml"
    config.write_text('private_identifiers: ["acmeprivate"]\n', encoding="utf-8")
    source = tmp_path / "README.md"
    source.write_text("public", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)

    assert module.main(["--root", str(tmp_path), "--audit-config", str(config)]) == 0


def test_audit_command_rejects_a_missing_configuration() -> None:
    module = _audit_module()

    with pytest.raises(SystemExit) as error:
        module.main([])

    assert error.value.code == 2
