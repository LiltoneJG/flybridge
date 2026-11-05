from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "reliability_trial.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("reliability_trial", SCRIPT)
assert SPEC and SPEC.loader
trial = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trial)


def _records() -> list[dict[str, object]]:
    records = []
    for variant in trial.VARIANTS:
        for index, scenario in enumerate(trial.SCENARIOS):
            records.append(
                {
                    "variant": variant,
                    "scenario": scenario,
                    "work_kind": trial.WORK_KINDS[index % len(trial.WORK_KINDS)],
                    **{
                        metric: index + (1 if variant == "baseline" else 0)
                        for metric in trial.METRICS
                    },
                }
            )
    return records


def test_trial_summary_requires_coverage_and_reports_directional_delta(tmp_path: Path) -> None:
    path = tmp_path / "records.json"
    path.write_text(json.dumps(_records()), encoding="utf-8")

    summary = trial.summarize(trial.load_records(path))

    assert summary["aggregates"]["baseline"]["records"] == 9
    assert summary["aggregates"]["trial"]["records"] == 9
    assert summary["trial_minus_baseline"]["intervention_count"] == -9
    assert summary["trial_minus_baseline"]["completion_seconds"] == -1


def test_trial_input_rejects_incomplete_scenarios(tmp_path: Path) -> None:
    records = _records()
    records.pop()
    path = tmp_path / "records.json"
    path.write_text(json.dumps(records), encoding="utf-8")

    with pytest.raises(ValueError, match="incomplete coverage"):
        trial.load_records(path)


def test_trial_input_rejects_invalid_metric(tmp_path: Path) -> None:
    records = _records()
    records[0]["recovery_seconds"] = -1
    path = tmp_path / "records.json"
    path.write_text(json.dumps(records), encoding="utf-8")

    with pytest.raises(ValueError, match="recovery_seconds"):
        trial.load_records(path)
