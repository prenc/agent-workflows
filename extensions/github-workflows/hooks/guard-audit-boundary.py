#!/usr/bin/env python3
"""Keep the audit supervisor on the public MCP contract."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

AUDIT_MARKERS = (
    "# Audit GitHub Repository",
    "skills/gh-audit-repo/SKILL.md",
    "Base directory for this skill:",
)
DENIAL = (
    "The gh-audit-repo supervisor must use the public github_workflows MCP tools; "
    "do not inspect extension implementation or private workflow storage to infer behavior. "
    "Use run_status for lifecycle guidance and the action-specific tool schema for inputs. "
    "Only an assigned gh-audit-repo-worker may inspect implementation in its immutable shard."
)
PUBLIC_PATH_DENIAL = (
    "Published GitHub text must not contain absolute host paths; use repository-relative "
    "paths such as src/package/module.py."
)
UNIX_ABSOLUTE_PATH = re.compile(r"(?<![:</\w])/(?!/)(?:[A-Za-z0-9._+-]+/)*[A-Za-z0-9._+-]+")
WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?<![A-Za-z0-9_])[A-Z]:[\\/](?:[^\s`'\"<>]+)")
PUBLIC_TEXT_FIELDS = {"title", "body", "comment"}


def decision(value: str, reason: str | None = None) -> dict[str, Any]:
    output: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": value,
    }
    if reason:
        output["permissionDecisionReason"] = reason
    return {"hookSpecificOutput": output}


def audit_session(payload: dict[str, Any]) -> bool:
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str):
        return False
    try:
        text = Path(transcript).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "# Audit GitHub Repository" in text or (
        "gh-audit-repo" in text and "Base directory for this skill:" in text
    )


def targets_private_boundary(payload: dict[str, Any]) -> bool:
    tool_input = payload.get("tool_input", {})
    rendered = json.dumps(tool_input, sort_keys=True).replace("\\\\", "/")
    lowered = rendered.lower()
    extension = str(Path(__file__).resolve().parents[1]).replace("\\", "/").lower()
    repository = str(Path(__file__).resolve().parents[3]).replace("\\", "/").lower()
    project = os.environ.get("QWEN_CODE_PROJECT_DIR", "").replace("\\", "/").lower().rstrip("/")

    implementation_targets = (
        f"{repository}/src/github_workflows",
        f"{extension}/agents",
        f"{extension}/qwen-extension.json",
        "src/github_workflows",
        "agents/extensions/github-workflows/agents",
        "agents/extensions/github-workflows/qwen-extension.json",
    )
    if any(target in lowered for target in implementation_targets):
        return True
    relative_extension = "agent-workflows"
    if relative_extension in lowered and "/references/" not in lowered:
        return True
    if project and any(
        target in lowered for target in (f"{project}/github", f"{project}/workflows/gh-audit-repo")
    ):
        return True
    shell_markers = (
        "qwen_code_project_dir",
        "records-v1.sqlite3",
        "workflows/gh-audit-repo/current",
    )
    return payload.get("tool_name") == "run_shell_command" and any(
        marker in lowered for marker in shell_markers
    )


def public_text_has_absolute_path(payload: dict[str, Any]) -> bool:
    tool_name = str(payload.get("tool_name", ""))
    if "github" not in tool_name or not any(
        operation in tool_name for operation in ("issue_write", "add_issue_comment")
    ):
        return False
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    for field in PUBLIC_TEXT_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and (
            UNIX_ABSOLUTE_PATH.search(value) or WINDOWS_ABSOLUTE_PATH.search(value)
        ):
            return True
    return False


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        if payload.get("hook_event_name") != "PreToolUse":
            return 0
        if payload.get("agent_type") == "gh-audit-repo-worker":
            print(json.dumps(decision("allow")))
            return 0
        if audit_session(payload):
            if public_text_has_absolute_path(payload):
                print(json.dumps(decision("deny", PUBLIC_PATH_DENIAL)))
                return 0
            if targets_private_boundary(payload):
                print(json.dumps(decision("deny", DENIAL)))
                return 0
        print(json.dumps(decision("allow")))
    except Exception:
        # A local policy helper must not disrupt unrelated or malformed sessions.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
