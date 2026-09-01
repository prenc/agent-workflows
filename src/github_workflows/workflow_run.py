#!/usr/bin/env python3
"""Manage one current Qwen GitHub-workflow run per project and workflow."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

WORKFLOWS = {"gh-audit-repo", "gh-curate-issues", "gh-implement-issue"}
RESUMABLE = {"in-progress", "suspended", "partial"}
TERMINAL = {"complete", "aborted"}
STATUSES = RESUMABLE | TERMINAL
RUN_TRANSITIONS = {
    "in-progress": {"in-progress", "suspended", "complete", "aborted"},
    "partial": {"suspended", "aborted"},
    "suspended": {"aborted"},
    "complete": set(),
    "aborted": set(),
}
RESERVED = {
    "schema_version",
    "workflow",
    "run_id",
    "project_root",
    "project_dir",
    "run_dir",
    "status",
    "revision",
    "created_at",
    "updated_at",
}
AUDIT_TASK_STATUSES = {"queued", "running", "checkpointed", "completed", "failed", "abandoned"}
AUDIT_TASK_TERMINAL = {"completed", "failed", "abandoned"}
AUDIT_CANDIDATE_TERMINAL = {
    "published",
    "updated",
    "no-op",
    "closed",
    "protected",
    "duplicate",
    "rejected",
    "dry-run",
}
AUDIT_CANDIDATE_STATUSES = AUDIT_CANDIDATE_TERMINAL | {
    "discovered",
    "consolidated",
    "validation-pending",
    "verification-pending",
    "verified",
}
AUDIT_PHASES = ("source", "history", "structure", "discovery", "verification", "publication")
AUDIT_PHASE_STATUSES = {"pending", "in-progress", "complete", "skipped", "partial", "failed"}
AUDIT_SHARD_STATUSES = {"pending", "running", "partial", "complete", "skipped", "failed"}
AUDIT_INTERNAL = {
    "phases",
    "shards",
    "tasks",
    "candidates",
    "validations",
    "verdicts",
    "mutations",
    "limitations",
    "pending",
    "head_drift",
    "history",
    "scheduler",
    "metrics",
    "helper_hashes",
    "directives",
    "inventory",
}
AUDIT_TRANSITIONS = {
    "queued": {"running", "abandoned"},
    "running": {"checkpointed", "completed", "failed", "abandoned"},
    "checkpointed": {"running", "completed", "failed", "abandoned"},
    "completed": set(),
    "failed": set(),
    "abandoned": set(),
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def secure_directory(path: Path, *, exist_ok: bool = True) -> None:
    path.mkdir(parents=True, exist_ok=exist_ok, mode=0o700)
    os.chmod(path, 0o700)


def secure_file(path: Path) -> None:
    os.chmod(path, 0o600)


def project_paths(args: argparse.Namespace) -> tuple[Path, Path, str, Path]:
    project_root = args.project_root.expanduser().resolve()
    if not project_root.is_dir():
        raise ValueError("project root must exist")
    workflow = args.workflow
    if workflow not in WORKFLOWS:
        raise ValueError(f"workflow must be one of {sorted(WORKFLOWS)}")
    configured = args.project_dir or os.environ.get("QWEN_CODE_PROJECT_DIR")
    if not configured:
        raise ValueError(
            "QWEN_CODE_PROJECT_DIR is required outside tests; use --project-dir explicitly"
        )
    project_dir = Path(configured).expanduser().resolve()
    secure_directory(project_dir)
    current = project_dir / "workflows" / workflow / "current"
    return project_root, project_dir, workflow, current


def state_path(current: Path) -> Path:
    return current / "state.json"


def journal_path(current: Path) -> Path:
    return current / "journal.jsonl"


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def load_state(current: Path) -> dict[str, Any]:
    path = state_path(current)
    if not path.is_file():
        raise ValueError("current workflow state is missing")
    value = load_json(path)
    schema = value.get("schema_version")
    workflow = value.get("workflow")
    compatible = schema == 1 or (workflow == "gh-audit-repo" and schema == 2)
    if not compatible or value.get("status") not in STATUSES:
        raise ValueError("current workflow state is incompatible")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def audit_helper_hashes() -> dict[str, str]:
    project = Path(__file__).resolve().parents[2]
    extension = project / "extensions" / "github-workflows"
    paths = sorted((project / "src" / "github_workflows").glob("*.py"))
    paths.extend(
        [
            extension / "qwen-extension.json",
            extension / "hooks" / "hooks.json",
            extension / "hooks" / "guard-audit-boundary.py",
            extension / "skills" / "gh-audit-repo" / "SKILL.md",
            extension / "agents" / "gh-audit-repo-worker.md",
            extension / "references" / "github-runtime-policy.md",
            extension / "references" / "github-issue-conventions.md",
            extension / "references" / "github-mcp-suspension.md",
        ]
    )
    return {str(path.relative_to(project)): sha256_file(path) for path in paths if path.is_file()}


def audit_helper_integrity(state: dict[str, Any]) -> dict[str, Any]:
    """Describe whether the reviewed helper bundle still matches this run."""
    expected = state.get("helper_hashes")
    current = audit_helper_hashes()
    valid = isinstance(expected, dict) and expected == current
    changed = sorted(
        key
        for key in set(current) | (set(expected) if isinstance(expected, dict) else set())
        if not isinstance(expected, dict) or expected.get(key) != current.get(key)
    )
    return {
        "valid": valid,
        "changed": changed,
        "required_action": None if valid else "abort-and-start-new-run",
    }


def require_audit_helper_integrity(state: dict[str, Any]) -> None:
    if not audit_helper_integrity(state)["valid"]:
        raise ValueError(
            "reviewed audit helpers changed during the run; abort and start a new audit run"
        )


def audit_concurrency(state: dict[str, Any]) -> int:
    inputs = state.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("audit inputs must be an object")
    value = inputs.get("n", 3)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("audit concurrency must be a positive integer")
    return value


def audit_defaults(supplied: dict[str, Any], now: str) -> dict[str, Any]:
    value = dict(supplied)
    value.setdefault("inputs", {})
    phases = {name: {"status": "pending"} for name in AUDIT_PHASES}
    if isinstance(value.get("sha"), str) and isinstance(value.get("audit_worktree"), str):
        phases["source"] = {
            "status": "complete",
            "sha": value["sha"],
            "recorded_at": now,
        }
    value.setdefault("phases", phases)
    value.setdefault("shards", {})
    value.setdefault("tasks", {})
    value.setdefault("candidates", {})
    value.setdefault("validations", {})
    value.setdefault("verdicts", {})
    value.setdefault("mutations", [])
    value.setdefault("limitations", [])
    value.setdefault("pending", [])
    value.setdefault("head_drift", {"changed": False, "reconciled": True})
    value.setdefault("history", {"publication_pending": False})
    value.setdefault(
        "scheduler",
        {
            "limit": audit_concurrency(value),
            "integration_queue": [],
            "supervisor_activity": None,
            "control_plane_always_available": True,
        },
    )
    value.setdefault(
        "metrics",
        {
            "started_at": now,
            "logical_tasks": 0,
            "task_attempts": 0,
            "failed_attempts": 0,
            "abandoned_attempts": 0,
        },
    )
    value["helper_hashes"] = audit_helper_hashes()
    return value


def write_state(current: Path, value: dict[str, Any]) -> None:
    path = state_path(current)
    temporary = path.with_suffix(".json.new")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    secure_file(temporary)
    os.replace(temporary, path)
    secure_file(path)


def append_journal(current: Path, event: str, **detail: Any) -> None:
    path = journal_path(current)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"event": event, "timestamp": utc_now(), **detail}, sort_keys=True) + "\n"
        )
    secure_file(path)


def git(primary: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(primary), *arguments], check=check, capture_output=True, text=True
    )


def audit_source(args: argparse.Namespace) -> None:
    requested = args.project_root.expanduser().resolve()
    listing = git(requested, "worktree", "list", "--porcelain").stdout.splitlines()
    first = next(
        (line.removeprefix("worktree ") for line in listing if line.startswith("worktree ")), None
    )
    if first is None:
        raise ValueError("repository has no primary worktree")
    primary = Path(first).resolve()
    branch_result = git(primary, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    sha = git(primary, "rev-parse", "HEAD").stdout.strip()
    upstream_result = git(
        primary, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False
    )
    upstream = upstream_result.stdout.strip() if upstream_result.returncode == 0 else None
    ahead = behind = None
    if upstream:
        counts = git(
            primary, "rev-list", "--left-right", "--count", f"HEAD...{upstream}"
        ).stdout.split()
        if len(counts) == 2:
            ahead, behind = map(int, counts)
    status = git(primary, "status", "--porcelain=v1", "--untracked-files=all").stdout.splitlines()
    untracked = sum(line.startswith("??") for line in status)
    print(
        json.dumps(
            {
                "primary_worktree": str(primary),
                "branch": branch,
                "sha": sha,
                "upstream": upstream,
                "ahead": ahead,
                "behind": behind,
                "excluded_dirty_state": {
                    "tracked_entries": len(status) - untracked,
                    "untracked_entries": untracked,
                    "dirty": bool(status),
                },
                "confirmation_required": branch not in {"main", "master"},
            },
            sort_keys=True,
        )
    )


def initialize(args: argparse.Namespace) -> None:
    project_root, project_dir, workflow, current = project_paths(args)
    supplied = load_json(args.input)
    overlap = RESERVED.intersection(supplied)
    if overlap:
        raise ValueError(f"run input contains reserved fields: {sorted(overlap)}")
    if workflow == "gh-audit-repo":
        overlap = AUDIT_INTERNAL.intersection(supplied)
        if overlap:
            raise ValueError(f"audit input contains internal state fields: {sorted(overlap)}")
    now = utc_now()
    run_id = now.replace(":", "").replace("-", "").replace(".", "")
    if workflow == "gh-audit-repo":
        supplied = audit_defaults(supplied, now)
    if current.exists():
        previous = load_state(current)
        if previous["status"] in RESUMABLE:
            raise ValueError("an unfinished current run exists; use --resume")
        shutil.rmtree(current)
    secure_directory(current, exist_ok=False)
    state = {
        **supplied,
        "schema_version": 2 if workflow == "gh-audit-repo" else 1,
        "workflow": workflow,
        "run_id": run_id,
        "project_root": str(project_root),
        "project_dir": str(project_dir),
        "run_dir": str(current),
        "status": "in-progress",
        "revision": 1,
        "created_at": now,
        "updated_at": now,
    }
    write_state(current, state)
    journal_path(current).write_text("", encoding="utf-8")
    secure_file(journal_path(current))
    if workflow == "gh-audit-repo":
        for name in ("areas", "candidates", "validation"):
            secure_directory(current / name)
    append_journal(current, "run_initialized", run_id=run_id)
    print(json.dumps({"run_dir": str(current), "run_id": run_id, "revision": 1}))


def resume(args: argparse.Namespace) -> None:
    _, _, _, current = project_paths(args)
    state = load_state(current)
    if state["workflow"] == "gh-audit-repo" and state["schema_version"] == 1:
        raise ValueError(
            "unfinished audit schema v1 cannot be resumed safely; restart it with the current skill"
        )
    if state["status"] not in RESUMABLE:
        raise ValueError("the current run is not resumable")
    if state["workflow"] == "gh-audit-repo":
        require_audit_helper_integrity(state)
    previous = state["status"]
    if previous != "in-progress":
        state["status"] = "in-progress"
        state["revision"] += 1
        state["updated_at"] = utc_now()
        write_state(current, state)
    append_journal(
        current,
        "run_resumed",
        previous=previous,
        status=state["status"],
        revision=state["revision"],
    )
    print(
        json.dumps(
            {
                "run_dir": str(current),
                "run_id": state["run_id"],
                "status": state["status"],
                "revision": state["revision"],
            }
        )
    )


def update_state(args: argparse.Namespace, *, terminal: bool) -> None:
    _, _, _, current = project_paths(args)
    state = load_state(current)
    if state["revision"] != args.expected_revision:
        raise RuntimeError(
            f"state revision conflict: expected {args.expected_revision}, found {state['revision']}"
        )
    status = args.status
    allowed = TERMINAL if terminal else RESUMABLE
    if status not in allowed:
        raise ValueError(f"status must be one of {sorted(allowed)}")
    if args.input:
        if state.get("workflow") == "gh-audit-repo" and state.get("schema_version") == 2:
            raise ValueError(
                "audit schema v2 state accepts typed audit-event updates, not checkpoint map replacement"
            )
        updates = load_json(args.input)
        overlap = RESERVED.intersection(updates)
        if overlap:
            raise ValueError(f"checkpoint input contains reserved fields: {sorted(overlap)}")
        state.update(updates)
    previous = state["status"]
    if status not in RUN_TRANSITIONS[previous]:
        raise ValueError(f"invalid run transition: {previous} -> {status}")
    if (
        terminal
        and status == "complete"
        and state.get("workflow") == "gh-audit-repo"
        and state.get("schema_version") == 2
    ):
        require_audit_helper_integrity(state)
        validate_audit_terminal(current, state)
    state["status"] = status
    state["revision"] += 1
    state["updated_at"] = utc_now()
    write_state(current, state)
    if previous != status or args.event:
        append_journal(
            current,
            args.event or "status_changed",
            previous=previous,
            status=status,
            revision=state["revision"],
        )
    print(json.dumps({"run_dir": str(current), "status": status, "revision": state["revision"]}))


def audit_state(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    _, _, workflow, current = project_paths(args)
    state = load_state(current)
    if workflow != "gh-audit-repo" or state.get("schema_version") != 2:
        raise ValueError("typed audit operations require a gh-audit-repo schema v2 run")
    if state["status"] != "in-progress":
        raise ValueError("the current audit run is not active")
    require_audit_helper_integrity(state)
    return current, state


def audit_running_count(state: dict[str, Any]) -> int:
    return sum(task.get("status") == "running" for task in state["tasks"].values())


def audit_scheduler_status(state: dict[str, Any]) -> dict[str, Any]:
    scheduler = state["scheduler"]
    running = audit_running_count(state)
    supervisor_lanes = 1 if scheduler.get("supervisor_activity") else 0
    queue = list(scheduler.get("integration_queue", []))
    available = max(0, scheduler["limit"] - running - supervisor_lanes)
    if queue:
        available = 0
    if queue:
        next_action = "integrate-result"
    elif scheduler.get("supervisor_activity"):
        next_action = "finish-supervisor-work"
    elif running:
        next_action = "wait" if available == 0 else "launch-worker"
    else:
        phases = state.get("phases", {})
        if phases.get("source", {}).get("status") != "complete":
            next_action = "establish-source"
        elif phases.get("history", {}).get("status") != "complete":
            next_action = "synchronize-history"
        elif phases.get("structure", {}).get("status") != "complete":
            next_action = "prepare-structure"
        elif not state.get("shards") and phases.get("discovery", {}).get("status") == "pending":
            next_action = "plan-discovery-shards"
        else:
            next_action = "launch-worker" if available else "wait"
    return {
        "limit": scheduler["limit"],
        "running_workers": running,
        "supervisor_activity": scheduler.get("supervisor_activity"),
        "integration_queue": queue,
        "worker_slots": available,
        "next_action": next_action,
        "control_plane_available": True,
    }


def require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def audit_event(args: argparse.Namespace) -> None:
    current, state = audit_state(args)
    if state["revision"] != args.expected_revision:
        raise RuntimeError(
            f"state revision conflict: expected {args.expected_revision}, found {state['revision']}"
        )
    payload = load_json(args.input)
    event_type = require_string(payload.get("type"), "event type")
    tasks = state["tasks"]
    scheduler = state["scheduler"]
    detail: dict[str, Any] = {}

    if event_type == "task-register":
        task = payload.get("task")
        if not isinstance(task, dict):
            raise ValueError("task-register requires a task object")
        task_id = require_string(task.get("id"), "task id")
        if task_id in tasks:
            raise ValueError(f"task already exists: {task_id}")
        status = task.get("status", "queued")
        if status not in {"queued", "running"}:
            raise ValueError("new task status must be queued or running")
        if status == "running" and audit_scheduler_status(state)["worker_slots"] < 1:
            raise ValueError("audit material-work concurrency is saturated")
        logical_id = require_string(task.get("logical_id"), "task logical_id")
        role = require_string(task.get("role"), "task role")
        agent_id = require_string(task.get("agent_id", task_id), "task agent_id")
        if any(item.get("agent_id") == agent_id for item in tasks.values()):
            raise ValueError(f"task agent_id already exists: {agent_id}")
        required = task.get("required", True)
        requires_integration = task.get("requires_integration", role in {"discover", "verify"})
        if not isinstance(required, bool) or not isinstance(requires_integration, bool):
            raise ValueError("task required and requires_integration must be booleans")
        if role in {"discover", "verify"}:
            requires_integration = True
        try:
            attempt = int(task.get("attempt", 1))
        except (TypeError, ValueError) as error:
            raise ValueError("task attempt must be an integer") from error
        prior_attempts = [
            int(item.get("attempt", 1))
            for item in tasks.values()
            if item.get("logical_id") == logical_id
        ]
        prior_for_logical = [
            item for item in tasks.values() if item.get("logical_id") == logical_id
        ]
        latest_prior = max(
            prior_for_logical, key=lambda item: int(item.get("attempt", 1)), default=None
        )
        if latest_prior and latest_prior.get("status") not in AUDIT_TASK_TERMINAL:
            raise ValueError("previous task attempt must be terminal before replacement")
        if prior_attempts and attempt != max(prior_attempts) + 1:
            raise ValueError("replacement task attempts must be consecutively numbered")
        normalized = {
            **task,
            "id": task_id,
            "logical_id": logical_id,
            "agent_id": agent_id,
            "role": role,
            "unit": require_string(task.get("unit"), "task unit"),
            "attempt": attempt,
            "status": status,
            "required": required,
            "requires_integration": requires_integration,
            "integrated": False,
            "registered_at": utc_now(),
        }
        if normalized["attempt"] < 1:
            raise ValueError("task attempt must be positive")
        tasks[task_id] = normalized
        state["metrics"]["task_attempts"] = len(tasks)
        state["metrics"]["logical_tasks"] = len({item["logical_id"] for item in tasks.values()})
        detail = {"task_id": task_id, "logical_id": normalized["logical_id"], "status": status}
    elif event_type == "task-transition":
        task_id = require_string(payload.get("task_id"), "task_id")
        task = tasks.get(task_id)
        if not isinstance(task, dict):
            raise ValueError(f"unknown task: {task_id}")
        status = payload.get("status")
        if status not in AUDIT_TASK_STATUSES:
            raise ValueError("unsupported task status")
        previous = task["status"]
        if status not in AUDIT_TRANSITIONS[previous]:
            raise ValueError(f"invalid task transition: {previous} -> {status}")
        if status == "running" and audit_scheduler_status(state)["worker_slots"] < 1:
            raise ValueError("audit material-work concurrency is saturated")
        task["status"] = status
        task["updated_at"] = utc_now()
        for name in ("result", "checkpoint", "error", "note"):
            if name in payload:
                task[name] = payload[name]
        if status == "completed":
            result = task.get("result")
            if not isinstance(result, str):
                raise ValueError("completed task requires a result artifact")
            result_path = (
                (current / result).resolve()
                if not Path(result).is_absolute()
                else Path(result).resolve()
            )
            try:
                result_path.relative_to(current.resolve())
            except ValueError as error:
                raise ValueError("task result artifact must be inside the current run") from error
            if not result_path.is_file():
                raise ValueError("task result artifact does not exist")
            task["result"] = str(result_path)
        if (
            status in AUDIT_TASK_TERMINAL
            and task["requires_integration"]
            and task_id not in scheduler["integration_queue"]
        ):
            scheduler["integration_queue"].append(task_id)
        if status == "failed":
            state["metrics"]["failed_attempts"] = state["metrics"].get("failed_attempts", 0) + 1
        if status == "abandoned":
            state["metrics"]["abandoned_attempts"] = (
                state["metrics"].get("abandoned_attempts", 0) + 1
            )
        detail = {"task_id": task_id, "previous_task_status": previous, "task_status": status}
    elif event_type == "integration-start":
        task_id = require_string(payload.get("task_id"), "task_id")
        if scheduler.get("supervisor_activity") is not None:
            raise ValueError("supervisor already has material work in progress")
        if not scheduler["integration_queue"] or scheduler["integration_queue"][0] != task_id:
            raise ValueError("integration must process the oldest completed result first")
        if audit_running_count(state) + 1 > scheduler["limit"]:
            raise ValueError("supervisor integration would exceed material-work concurrency")
        scheduler["supervisor_activity"] = {
            "kind": "integration",
            "task_id": task_id,
            "started_at": utc_now(),
        }
        detail = {"task_id": task_id}
    elif event_type == "integration-complete":
        task_id = require_string(payload.get("task_id"), "task_id")
        activity = scheduler.get("supervisor_activity")
        if (
            not isinstance(activity, dict)
            or activity.get("kind") != "integration"
            or activity.get("task_id") != task_id
        ):
            raise ValueError("task is not the active supervisor integration")
        tasks[task_id]["integrated"] = True
        tasks[task_id]["integrated_at"] = utc_now()
        scheduler["integration_queue"].remove(task_id)
        scheduler["supervisor_activity"] = None
        detail = {"task_id": task_id}
    elif event_type == "supervisor-start":
        if scheduler.get("supervisor_activity") is not None:
            raise ValueError("supervisor already has material work in progress")
        if scheduler["integration_queue"]:
            raise ValueError("terminal worker results must be integrated first")
        if audit_running_count(state) + 1 > scheduler["limit"]:
            raise ValueError("supervisor material work would exceed concurrency")
        scheduler["supervisor_activity"] = {
            "kind": require_string(payload.get("kind"), "supervisor activity kind"),
            "unit": payload.get("unit"),
            "started_at": utc_now(),
        }
        detail = {"kind": scheduler["supervisor_activity"]["kind"]}
    elif event_type == "supervisor-complete":
        if scheduler.get("supervisor_activity") is None:
            raise ValueError("supervisor has no material work in progress")
        detail = {"kind": scheduler["supervisor_activity"]["kind"]}
        scheduler["supervisor_activity"] = None
    elif event_type == "shard-upsert":
        shard = payload.get("shard")
        if not isinstance(shard, dict):
            raise ValueError("shard-upsert requires a shard object")
        shard_id = require_string(shard.get("id"), "shard id")
        require_string(shard.get("area"), "shard area")
        if shard.get("status") not in AUDIT_SHARD_STATUSES:
            raise ValueError("shard requires a supported lifecycle status")
        existing = state["shards"].get(shard_id, {})
        if existing.get("status") in {"complete", "skipped"} and shard.get(
            "status"
        ) != existing.get("status"):
            raise ValueError("terminal shard cannot be reopened")
        state["shards"][shard_id] = {**existing, **shard, "id": shard_id}
        detail = {"shard_id": shard_id, "shard_status": shard.get("status")}
    elif event_type in {"candidate-upsert", "validation-record", "verdict-record"}:
        field = {
            "candidate-upsert": "candidate",
            "validation-record": "validation",
            "verdict-record": "verdict",
        }[event_type]
        value = payload.get(field)
        if not isinstance(value, dict):
            raise ValueError(f"{event_type} requires a {field} object")
        unit_id = require_string(value.get("id"), f"{field} id")
        target = {
            "candidate-upsert": "candidates",
            "validation-record": "validations",
            "verdict-record": "verdicts",
        }[event_type]
        existing = state[target].get(unit_id, {})
        if event_type == "candidate-upsert":
            status = value.get("status", existing.get("status"))
            if status not in AUDIT_CANDIDATE_STATUSES:
                raise ValueError("candidate requires a supported lifecycle status")
            if existing.get("status") in AUDIT_CANDIDATE_TERMINAL and status != existing.get(
                "status"
            ):
                raise ValueError("terminal candidate cannot be reopened or redisposed")
        if event_type == "validation-record":
            artifact = value.get("artifact")
            if not isinstance(artifact, str):
                raise ValueError("validation record requires an artifact path")
            artifact_path = Path(artifact).resolve()
            validation_root = (current / "validation").resolve()
            try:
                artifact_path.relative_to(validation_root)
            except ValueError as error:
                raise ValueError(
                    "validation artifact must be inside the current run validation directory"
                ) from error
            if artifact_path.name != "result.json" or not artifact_path.is_file():
                raise ValueError("validation artifact must be an existing result.json")
            candidate_id = value.get("candidate_id")
            if candidate_id is not None and candidate_id not in state["candidates"]:
                raise ValueError("validation record refers to an unknown candidate")
            value = {**value, "artifact": str(artifact_path)}
        if event_type == "verdict-record" and unit_id not in state["candidates"]:
            raise ValueError("verdict record refers to an unknown candidate")
        state[target][unit_id] = {**existing, **value, "id": unit_id}
        detail = {f"{field}_id": unit_id}
    elif event_type == "mutation-record":
        mutation = payload.get("mutation")
        if not isinstance(mutation, dict):
            raise ValueError("mutation-record requires a mutation object")
        candidate_id = require_string(mutation.get("candidate_id"), "mutation candidate_id")
        if candidate_id not in state["candidates"]:
            raise ValueError("mutation record refers to an unknown candidate")
        require_string(mutation.get("action"), "mutation action")
        state["mutations"].append(mutation)
        detail = {"candidate_id": mutation["candidate_id"], "action": mutation.get("action")}
    elif event_type == "limitation-add":
        limitation = require_string(payload.get("limitation"), "limitation")
        if limitation not in state["limitations"]:
            state["limitations"].append(limitation)
        detail = {"limitation": limitation}
    elif event_type == "pending-set":
        pending = payload.get("pending")
        if not isinstance(pending, list) or not all(isinstance(item, str) for item in pending):
            raise ValueError("pending-set requires a string array")
        state["pending"] = pending
        detail = {"pending_count": len(pending)}
    elif event_type == "phase-set":
        phase = require_string(payload.get("phase"), "phase")
        if phase not in AUDIT_PHASES:
            raise ValueError("unsupported audit phase")
        value = payload.get("value")
        if not isinstance(value, dict):
            raise ValueError("phase-set requires an object value")
        if value.get("status") not in AUDIT_PHASE_STATUSES:
            raise ValueError("phase-set requires a supported status")
        state["phases"][phase] = {**state["phases"].get(phase, {}), **value}
        detail = {"phase": phase, "phase_status": value.get("status")}
    elif event_type == "directive-update":
        directive = payload.get("directive")
        if not isinstance(directive, dict):
            raise ValueError("directive-update requires a directive object")
        state.setdefault("directives", []).append({**directive, "recorded_at": utc_now()})
        if "concurrency" in directive:
            concurrency = directive["concurrency"]
            if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
                raise ValueError("directive concurrency must be a positive integer")
            if audit_running_count(state) > concurrency:
                raise ValueError("cannot lower concurrency below currently running worker count")
            scheduler["limit"] = concurrency
            state.setdefault("inputs", {})["n"] = concurrency
        detail = {"directive": directive.get("kind", "update")}
    elif event_type == "head-drift":
        value = payload.get("value")
        if not isinstance(value, dict):
            raise ValueError("head-drift requires an object value")
        state["head_drift"] = value
        detail = {"changed": value.get("changed"), "reconciled": value.get("reconciled")}
    elif event_type == "history-set":
        value = payload.get("value")
        if not isinstance(value, dict):
            raise ValueError("history-set requires an object value")
        state["history"] = value
        detail = {"publication_pending": value.get("publication_pending")}
    elif event_type == "history-sync":
        value = payload.get("value")
        status = payload.get("status")
        if not isinstance(value, dict):
            raise ValueError("history-sync requires an object value")
        if status not in AUDIT_PHASE_STATUSES:
            raise ValueError("history-sync requires a supported phase status")
        state["history"] = value
        state["phases"]["history"] = {
            **state["phases"].get("history", {}),
            "status": status,
            "updated_at": utc_now(),
        }
        detail = {"sync_status": value.get("sync_status"), "phase_status": status}
    elif event_type == "publication-complete":
        value = payload.get("value")
        mutation = payload.get("mutation")
        if not isinstance(value, dict) or not isinstance(mutation, dict):
            raise ValueError("publication-complete requires history and mutation objects")
        candidate_id = require_string(mutation.get("candidate_id"), "mutation candidate_id")
        if candidate_id not in state["candidates"]:
            raise ValueError("mutation record refers to an unknown candidate")
        action = require_string(mutation.get("action"), "mutation action")
        if value.get("publication_pending") is not False:
            raise ValueError("completed publication must clear publication_pending")
        state["history"] = value
        state["mutations"].append(mutation)
        detail = {"candidate_id": candidate_id, "action": action, "publication_pending": False}
    elif event_type == "metrics-update":
        value = payload.get("value")
        if not isinstance(value, dict):
            raise ValueError("metrics-update requires an object value")
        state["metrics"].update(value)
        detail = {"metric_keys": sorted(value)}
    else:
        raise ValueError(f"unsupported audit event type: {event_type}")

    state["revision"] += 1
    state["updated_at"] = utc_now()
    write_state(current, state)
    append_journal(
        current,
        f"audit_{event_type.replace('-', '_')}",
        revision=state["revision"],
        run_status=state["status"],
        **detail,
    )
    print(json.dumps({"revision": state["revision"], "scheduler": audit_scheduler_status(state)}))


def audit_status(args: argparse.Namespace) -> None:
    _, _, workflow, current = project_paths(args)
    state = load_state(current)
    if workflow != "gh-audit-repo" or state.get("schema_version") != 2:
        raise ValueError("audit-status requires a gh-audit-repo schema v2 run")
    require_audit_helper_integrity(state)
    print(json.dumps(audit_scheduler_status(state), indent=2, sort_keys=True))


def validate_audit_terminal(current: Path, state: dict[str, Any]) -> None:
    errors: list[str] = []
    incomplete_phases = sorted(
        phase
        for phase in AUDIT_PHASES
        if state.get("phases", {}).get(phase, {}).get("status") not in {"complete", "skipped"}
    )
    if incomplete_phases:
        errors.append(f"incomplete phases: {incomplete_phases}")
    incomplete_shards = sorted(
        shard_id
        for shard_id, shard in state.get("shards", {}).items()
        if shard.get("status") not in {"complete", "skipped"}
    )
    if incomplete_shards:
        errors.append(f"incomplete shards: {incomplete_shards}")
    tasks = state["tasks"]
    nonterminal = sorted(
        task_id for task_id, task in tasks.items() if task.get("status") not in AUDIT_TASK_TERMINAL
    )
    if nonterminal:
        errors.append(f"nonterminal tasks: {nonterminal}")
    unintegrated = sorted(
        task_id
        for task_id, task in tasks.items()
        if task.get("status") in AUDIT_TASK_TERMINAL
        and task.get("requires_integration")
        and not task.get("integrated")
    )
    if unintegrated:
        errors.append(f"unintegrated terminal tasks: {unintegrated}")
    logical: dict[str, list[dict[str, Any]]] = {}
    for task in tasks.values():
        logical.setdefault(task["logical_id"], []).append(task)
    missing_logical = sorted(
        logical_id
        for logical_id, attempts in logical.items()
        if any(item.get("required", True) for item in attempts)
        and not any(
            item.get("status") == "completed"
            and (not item.get("requires_integration") or item.get("integrated"))
            for item in attempts
        )
    )
    if missing_logical:
        errors.append(f"required logical tasks without an integrated completion: {missing_logical}")
    candidates = state["candidates"]
    unfinished_candidates = sorted(
        candidate_id
        for candidate_id, candidate in candidates.items()
        if candidate.get("status") not in AUDIT_CANDIDATE_TERMINAL
    )
    if unfinished_candidates:
        errors.append(f"nonterminal candidates: {unfinished_candidates}")
    mutation_candidates = {
        mutation.get("candidate_id")
        for mutation in state.get("mutations", [])
        if isinstance(mutation, dict)
    }
    missing_mutations = sorted(
        candidate_id
        for candidate_id, candidate in candidates.items()
        if candidate.get("status") in {"published", "updated", "no-op", "closed", "dry-run"}
        and candidate_id not in mutation_candidates
    )
    if missing_mutations:
        errors.append(f"disposed candidates without mutation records: {missing_mutations}")
    scheduler = state["scheduler"]
    if scheduler.get("integration_queue"):
        errors.append("integration queue is not empty")
    if scheduler.get("supervisor_activity") is not None:
        errors.append("supervisor material activity is still active")
    if state.get("pending"):
        errors.append("pending operations are not empty")
    drift = state.get("head_drift", {})
    if drift.get("changed") and not drift.get("reconciled"):
        errors.append("HEAD drift has not been reconciled")
    if state.get("history", {}).get("publication_pending"):
        errors.append("publication history transaction is pending")
    registered = {
        Path(value["artifact"]).resolve()
        for value in state["validations"].values()
        if isinstance(value, dict) and isinstance(value.get("artifact"), str)
    }
    artifacts = {path.resolve() for path in (current / "validation").glob("*/result.json")}
    if registered != artifacts:
        errors.append("registered validation artifacts do not match validation result files")
    current_hashes = audit_helper_hashes()
    if state.get("helper_hashes") != current_hashes:
        errors.append("reviewed audit helpers changed during the run")
    if errors:
        raise ValueError("audit cannot be finalized: " + "; ".join(errors))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    source = sub.add_parser("audit-source")
    source.add_argument("--project-root", type=Path, required=True)
    source.set_defaults(handler=audit_source)
    for name, handler in (("initialize", initialize), ("resume", resume)):
        command = sub.add_parser(name)
        command.add_argument("--project-root", type=Path, required=True)
        command.add_argument("--project-dir", type=Path)
        command.add_argument("--workflow", required=True)
        if name == "initialize":
            command.add_argument("--input", type=Path, required=True)
        command.set_defaults(handler=handler)
    for name, terminal in (("checkpoint", False), ("finalize", True)):
        command = sub.add_parser(name)
        command.add_argument("--project-root", type=Path, required=True)
        command.add_argument("--project-dir", type=Path)
        command.add_argument("--workflow", required=True)
        command.add_argument("--expected-revision", type=int, required=True)
        command.add_argument("--status", required=True)
        command.add_argument("--input", type=Path)
        command.add_argument("--event")
        command.set_defaults(
            handler=lambda args, terminal=terminal: update_state(args, terminal=terminal)
        )
    command = sub.add_parser("audit-event")
    command.add_argument("--project-root", type=Path, required=True)
    command.add_argument("--project-dir", type=Path)
    command.add_argument("--workflow", default="gh-audit-repo")
    command.add_argument("--expected-revision", type=int, required=True)
    command.add_argument("--input", type=Path, required=True)
    command.set_defaults(handler=audit_event)
    command = sub.add_parser("audit-status")
    command.add_argument("--project-root", type=Path, required=True)
    command.add_argument("--project-dir", type=Path)
    command.add_argument("--workflow", default="gh-audit-repo")
    command.set_defaults(handler=audit_status)
    return root


def main() -> int:
    args = parser().parse_args()
    args.handler(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        ValueError,
        RuntimeError,
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ) as error:
        print(f"workflow-run: {error}", file=sys.stderr)
        raise SystemExit(2)
