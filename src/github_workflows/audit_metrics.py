#!/usr/bin/env python3
"""Summarize registered audit task and Qwen telemetry without reading conversation content."""

from __future__ import annotations

import argparse
import ast
import collections
import datetime as dt
import json
from pathlib import Path
from typing import Any


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def telemetry_event(payload: object) -> dict[str, Any]:
    parsed = payload
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(payload)
            except (SyntaxError, ValueError):
                return {}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("uiEvent"), dict):
        return {}
    return parsed["uiEvent"]


def registered_logs(
    project_dir: Path, task_ids: set[str]
) -> tuple[list[Path], list[dict[str, Any]]]:
    logs: list[Path] = []
    metadata: list[dict[str, Any]] = []
    root = project_dir / "subagents"
    if not root.is_dir():
        return logs, metadata
    for path in root.glob("*/*.meta.json"):
        try:
            item = load_object(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if item.get("agentId") not in task_ids:
            continue
        metadata.append(item)
        compact = path.with_name(path.name.removesuffix(".meta.json") + ".jsonl")
        if compact.is_file():
            logs.append(compact)
    return logs, metadata


def supervisor_intervals(
    journal: Path, end: dt.datetime | None
) -> list[tuple[dt.datetime, dt.datetime]]:
    intervals: list[tuple[dt.datetime, dt.datetime]] = []
    started: dt.datetime | None = None
    if not journal.is_file():
        return intervals
    with journal.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = item.get("event")
            timestamp = parse_time(item.get("timestamp"))
            if timestamp is None:
                continue
            if event in {"audit_integration_start", "audit_supervisor_start"}:
                started = timestamp
            elif event in {"audit_integration_complete", "audit_supervisor_complete"} and started:
                if timestamp >= started:
                    intervals.append((started, timestamp))
                started = None
    if started and end and end >= started:
        intervals.append((started, end))
    return intervals


def interval_summary(intervals: list[tuple[dt.datetime, dt.datetime]]) -> tuple[float, int]:
    events: list[tuple[dt.datetime, int]] = []
    for start, end in intervals:
        if end > start:
            events.extend(((start, 1), (end, -1)))
    events.sort(key=lambda item: (item[0], item[1]))
    active = 0
    peak = 0
    seconds = 0.0
    previous: dt.datetime | None = None
    for timestamp, delta in events:
        if previous is not None and active:
            seconds += (timestamp - previous).total_seconds()
        active += delta
        peak = max(peak, active)
        previous = timestamp
    return seconds, peak


def summarize(project_dir: Path, run_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    tasks = state.get("tasks") if isinstance(state.get("tasks"), dict) else {}
    task_ids = {
        str(task.get("agent_id") or task_id)
        for task_id, task in tasks.items()
        if isinstance(task, dict)
    }
    logs, metadata = registered_logs(project_dir, task_ids)
    counters: collections.Counter[str] = collections.Counter()
    tools: collections.Counter[str] = collections.Counter()
    for path in logs:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "system" or record.get("subtype") != "ui_telemetry":
                    continue
                event = telemetry_event(record.get("systemPayload"))
                if event.get("event.name") == "qwen-code.api_response":
                    counters["model_responses"] += 1
                    for field in (
                        "input_token_count",
                        "output_token_count",
                        "thoughts_token_count",
                        "total_token_count",
                        "cached_content_token_count",
                    ):
                        try:
                            counters[field] += int(event.get(field, 0) or 0)
                        except (TypeError, ValueError):
                            pass
                elif event.get("event.name") == "qwen-code.tool_call":
                    counters["tool_calls"] += 1
                    tools[str(event.get("function_name", "unknown"))] += 1
    worker_intervals: list[tuple[dt.datetime, dt.datetime]] = []
    for item in metadata:
        start = parse_time(item.get("createdAt"))
        end = parse_time(item.get("lastUpdatedAt"))
        if start and end and end >= start:
            worker_intervals.append((start, end))
    started = parse_time(state.get("created_at"))
    updated = parse_time(state.get("updated_at"))
    wall_seconds = (
        (updated - started).total_seconds() if started and updated and updated >= started else None
    )
    supervisor = supervisor_intervals(run_dir / "journal.jsonl", updated)
    worker_seconds = sum((end - start).total_seconds() for start, end in worker_intervals)
    supervisor_seconds = sum((end - start).total_seconds() for start, end in supervisor)
    material_active_seconds, peak_concurrency = interval_summary(worker_intervals + supervisor)
    material_idle_seconds = (
        max(0.0, wall_seconds - material_active_seconds) if wall_seconds is not None else None
    )
    statuses = collections.Counter(str(task.get("status", "unknown")) for task in tasks.values())
    logical_attempts: dict[object, list[str]] = collections.defaultdict(list)
    for task in tasks.values():
        logical_attempts[task.get("logical_id")].append(str(task.get("status", "unknown")))
    recovered_tasks = sum(
        "completed" in values and any(status in {"failed", "abandoned"} for status in values)
        for values in logical_attempts.values()
    )
    candidates = state.get("candidates") if isinstance(state.get("candidates"), dict) else {}
    candidate_statuses = collections.Counter(
        str(candidate.get("status", "unknown")) for candidate in candidates.values()
    )
    return {
        "run_id": state.get("run_id"),
        "wall_seconds": wall_seconds,
        "registered_logical_tasks": len({task.get("logical_id") for task in tasks.values()}),
        "registered_attempts": len(tasks),
        "task_statuses": dict(sorted(statuses.items())),
        "recorded_worker_seconds": round(worker_seconds, 3),
        "recorded_supervisor_material_seconds": round(supervisor_seconds, 3),
        "material_active_seconds": round(material_active_seconds, 3),
        "material_idle_seconds": round(material_idle_seconds, 3)
        if material_idle_seconds is not None
        else None,
        "observed_peak_material_concurrency": peak_concurrency,
        "recovered_logical_tasks": recovered_tasks,
        "telemetry_files": len(logs),
        "telemetry": dict(sorted(counters.items())),
        "tool_calls_by_name": dict(sorted(tools.items())),
        "context7_calls": sum(
            count for name, count in tools.items() if name.startswith("mcp__context7__")
        ),
        "context7_query_calls": sum(
            count
            for name, count in tools.items()
            if name in {"mcp__context7__query-docs", "mcp__context7__query_docs"}
        ),
        "context7_resolution_calls": sum(
            count
            for name, count in tools.items()
            if name in {"mcp__context7__resolve-library-id", "mcp__context7__resolve_library_id"}
        ),
        "candidate_statuses": dict(sorted(candidate_statuses.items())),
        "validation_records": len(state.get("validations", {})),
        "mutation_records": len(state.get("mutations", [])),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    project_dir = args.project_dir.expanduser().resolve()
    run_dir = args.run_dir.expanduser().resolve()
    expected = project_dir / "workflows" / "gh-audit-repo" / "current"
    if run_dir != expected:
        raise ValueError("run directory must be the current project-local audit run")
    state = load_object(run_dir / "state.json")
    if state.get("schema_version") != 2 or state.get("workflow") != "gh-audit-repo":
        raise ValueError("current run is not a gh-audit-repo schema v2 state")
    print(json.dumps(summarize(project_dir, run_dir, state), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"audit-metrics: {error}", file=__import__("sys").stderr)
        raise SystemExit(2)
