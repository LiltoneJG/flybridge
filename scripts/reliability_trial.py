#!/usr/bin/env python3
"""Validate and summarize local Flybridge reliability-trial records."""

from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from _bootstrap import activate_project_environment

activate_project_environment()

from flybridge_core import ArgumentParser

VARIANTS = ("baseline", "trial")
WORK_KINDS = ("implementation", "validation", "review")
SCENARIOS = (
    "normal-exit",
    "abnormal-exit",
    "missing-result",
    "question-waiting",
    "monitor-restart",
    "delayed-duplicate-result",
    "interrupted-cleanup",
    "validation-only",
    "retained-dirty-work",
)
METRICS = (
    "completion_seconds",
    "intervention_count",
    "intervention_seconds",
    "detection_seconds",
    "recovery_seconds",
    "missing_results",
    "stale_running_states",
    "orphan_ownership",
    "rework_count",
    "resource_wait_seconds",
)
COUNT_METRICS = {
    "intervention_count",
    "missing_results",
    "stale_running_states",
    "orphan_ownership",
    "rework_count",
}


def _number(record: dict[str, Any], metric: str, index: int) -> float:
    value = record.get(metric)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"record {index}: {metric} must be a non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"record {index}: {metric} must be a finite non-negative number")
    if metric in COUNT_METRICS and not number.is_integer():
        raise ValueError(f"record {index}: {metric} must be an integer")
    return number


def load_records(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("trial input must be a non-empty JSON array")
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(payload, 1):
        if not isinstance(item, dict):
            raise TypeError(f"record {index}: expected an object")
        variant = item.get("variant")
        scenario = item.get("scenario")
        work_kind = item.get("work_kind")
        if variant not in VARIANTS:
            raise ValueError(f"record {index}: variant must be one of {VARIANTS}")
        if scenario not in SCENARIOS:
            raise ValueError(f"record {index}: scenario must be one of {SCENARIOS}")
        if work_kind not in WORK_KINDS:
            raise ValueError(f"record {index}: work_kind must be one of {WORK_KINDS}")
        key = (variant, scenario, work_kind)
        if key in seen:
            raise ValueError(f"record {index}: duplicate variant/scenario/work_kind {key}")
        seen.add(key)
        normalized = {"variant": variant, "scenario": scenario, "work_kind": work_kind}
        normalized.update({metric: _number(item, metric, index) for metric in METRICS})
        records.append(normalized)

    for variant in VARIANTS:
        observed_scenarios = {r["scenario"] for r in records if r["variant"] == variant}
        missing_scenarios = sorted(set(SCENARIOS) - observed_scenarios)
        missing_work = sorted(
            set(WORK_KINDS) - {r["work_kind"] for r in records if r["variant"] == variant}
        )
        if missing_scenarios or missing_work:
            raise ValueError(
                f"{variant}: incomplete coverage; scenarios={missing_scenarios}, work_kinds={missing_work}"
            )
    return records


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["variant"]].append(record)
    aggregates: dict[str, dict[str, float | int]] = {}
    for variant in VARIANTS:
        items = grouped[variant]
        aggregates[variant] = {"records": len(items)}
        for metric in METRICS:
            values = [float(item[metric]) for item in items]
            aggregates[variant][metric] = (
                int(sum(values)) if metric in COUNT_METRICS else round(statistics.mean(values), 3)
            )
    deltas = {
        metric: round(float(aggregates["trial"][metric]) - float(aggregates["baseline"][metric]), 3)
        for metric in METRICS
    }
    return {
        "schema_version": 1,
        "coverage": {
            "variants": list(VARIANTS),
            "scenarios": list(SCENARIOS),
            "work_kinds": list(WORK_KINDS),
        },
        "aggregates": aggregates,
        "trial_minus_baseline": deltas,
    }


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(prog="reliability_trial.py", description=__doc__)
    parser.add_argument("input", type=Path, help="local JSON array containing trial records")
    parser.add_argument("--output", type=Path, help="optional local path for the safe aggregate")
    args = parser.parse_args(argv)
    try:
        result = summarize(load_records(args.input))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        parser.error(str(error))
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
