from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ENABLED_GITHUB = {
    "enabled": True,
    "owner": "example",
    "owner_type": "user",
    "project_number": 1,
    "status_field": "Status",
    "todo_status": "Todo",
    "priority_field": "Priority",
    "priority_values": ["High"],
}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def required_config(**overrides: Any) -> dict[str, Any]:
    payload = {
        "default_mode": "single",
        "orca": {"agents": {}},
        "skills": {
            "sources": [],
            "roles": {},
            "response_language": "English",
            "language_specific": [],
        },
        "queue": {"observer": False, "resources": []},
        "github": {"enabled": False},
    }
    return _jsonable(_merge(payload, overrides))


def write_config(path: Path, **overrides: Any) -> Path:
    path.write_text(json.dumps(required_config(**overrides), indent=2), encoding="utf-8")
    return path
