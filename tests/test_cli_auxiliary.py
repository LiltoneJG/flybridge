from __future__ import annotations

import json
from pathlib import Path

from conftest import ENABLED_GITHUB, write_config
from flybridge_cli.main import main


def test_doctor_dispatches_through_the_public_entrypoint(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path)

    result = main(["--config", str(config_path), "doctor"])

    assert result in {0, 1}
    assert '"config"' in capsys.readouterr().out


def test_operator_guide_indexes_paths_without_printing_skill_content(
    tmp_path: Path, capsys
) -> None:
    skill = tmp_path / "operator" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("private operator policy", encoding="utf-8")
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, skills={"operator": [skill], "response_language": "Japanese"})

    result = main(["--config", str(config_path), "operator", "guide"])

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert result == 0
    assert '"response_language": "Japanese"' in output
    assert str(skill.resolve()) in output
    assert "private operator policy" not in output
    assert set(payload) == {"response_language", "skill_paths", "instruction"}
    assert "Decide workflow-wide operational constraints once" in payload["instruction"]
    assert "pass the direct URL" in payload["instruction"]
    assert "do not accept narrower validation" in payload["instruction"]
    assert "not code inspection alone" in payload["instruction"]
    assert "Hosted CI is complete once triggered" in payload["instruction"]
    assert "If a reviewer requests changes" in payload["instruction"]


def test_doctor_succeeds_when_configured_github_login_is_authenticated(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        orca={"agents": {"single": "codex"}},
        github={"enabled": True, "login": "alice", "boards": ENABLED_GITHUB["boards"]},
    )

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def verify(self) -> dict[str, str]:
            return {"app_version": "test"}

    monkeypatch.setattr("flybridge_cli.commands.auxiliary.OrcaClient", FakeOrcaClient)
    monkeypatch.setattr(
        "flybridge_cli.commands.auxiliary.shutil.which",
        lambda executable: "/usr/bin/" + executable,
    )
    monkeypatch.setattr(
        "flybridge_cli.commands.auxiliary.agent_cli_is_resolvable",
        lambda *_args, **_kwargs: True,
    )

    class FakeCli:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def inspect_accounts(self):
            return {
                "hostname": "github.com",
                "configured_login": "alice",
                "active_login": "bob",
                "authenticated_logins": ["bob", "alice"],
                "configured_login_authenticated": True,
            }

    monkeypatch.setattr("flybridge_cli.commands.auxiliary.GitHubCli", FakeCli)

    assert main(["--config", str(config_path), "doctor", "--mode", "single"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["github_login"] == "alice"
    assert report["github_active_login"] == "bob"
    assert report["github_login_authenticated"] is True


def test_doctor_fails_when_configured_github_login_is_unauthenticated(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        orca={"agents": {"single": "codex"}},
        github={"enabled": True, "login": "alice", "boards": ENABLED_GITHUB["boards"]},
    )

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def verify(self) -> dict[str, str]:
            return {"app_version": "test"}

    monkeypatch.setattr("flybridge_cli.commands.auxiliary.OrcaClient", FakeOrcaClient)
    monkeypatch.setattr(
        "flybridge_cli.commands.auxiliary.shutil.which",
        lambda executable: "/usr/bin/" + executable,
    )
    monkeypatch.setattr(
        "flybridge_cli.commands.auxiliary.agent_cli_is_resolvable",
        lambda *_args, **_kwargs: True,
    )

    class FakeCli:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def inspect_accounts(self):
            return {
                "hostname": "github.com",
                "configured_login": "alice",
                "active_login": "bob",
                "authenticated_logins": ["bob"],
                "configured_login_authenticated": False,
            }

    monkeypatch.setattr("flybridge_cli.commands.auxiliary.GitHubCli", FakeCli)

    assert main(["--config", str(config_path), "doctor", "--mode", "single"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["github_login_authenticated"] is False


def test_doctor_reports_an_invalid_operator_skill_path(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, skills={"operator": [tmp_path / "missing" / "SKILL.md"]})

    result = main(["--config", str(config_path), "doctor"])

    output = capsys.readouterr().out
    assert result == 1
    assert '"skill_sources_valid": false' in output
    assert "skills.operator[0] does not exist" in output
