#!/usr/bin/env python3
"""Keep the audit supervisor on the public MCP contract."""

from __future__ import annotations

import json
import os
import re
import shlex
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
WORKER_SHELL_DENIAL = (
    "The audit worker may use run_shell_command only for constrained direct rg searches "
    "within roots from the latest task_context; all other shell execution is denied."
)
RG_REQUIRED_FLAGS = {"--hidden", "--no-config", "--no-ignore-parent", "--no-ignore-vcs"}
RG_BOOLEAN_FLAGS = RG_REQUIRED_FLAGS | {
    "-n",
    "--line-number",
    "--no-heading",
    "--with-filename",
    "-F",
    "--fixed-strings",
    "-i",
    "--ignore-case",
    "-S",
    "--smart-case",
    "-s",
    "--case-sensitive",
    "-w",
    "--word-regexp",
    "-v",
    "--invert-match",
    "-c",
    "--count",
    "-l",
    "--files-with-matches",
    "-L",
    "--files-without-match",
    "-o",
    "--only-matching",
    "--files",
}
RG_VALUE_FLAGS = {
    "-g",
    "--glob",
    "-A",
    "--after-context",
    "-B",
    "--before-context",
    "-C",
    "--context",
    "-m",
    "--max-count",
    "--max-columns",
    "--max-filesize",
    "--ignore-file",
}
RG_LONG_VALUE_FLAGS = {flag for flag in RG_VALUE_FLAGS if flag.startswith("--")}
RG_CONTEXT_FLAGS = {"-A", "--after-context", "-B", "--before-context", "-C", "--context"}
SECRET_BASENAMES = {".env", ".envrc", "credentials", "id_rsa", "id_ed25519"}
SECRET_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}


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


def allowed_worker_search(payload: dict[str, Any]) -> bool:
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or any(
        value in command for value in ("\n", "\0", "`", "$(", "${")
    ):
        return False
    if re.search(r"\$[A-Za-z_]", command):
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    if (
        not tokens
        or tokens[0] != "rg"
        or any(token and all(character in "();<>|&" for character in token) for token in tokens[1:])
    ):
        return False
    context = assigned_task_context(payload)
    if context is None:
        return False
    references = context.get("references")
    ignore_file = references.get("rg_excludes") if isinstance(references, dict) else None
    if not isinstance(ignore_file, str) or not Path(ignore_file).is_absolute():
        return False

    separator = tokens[1:].count("--")
    if separator != 1:
        return False
    separator_index = tokens.index("--", 1)
    options = tokens[1:separator_index]
    operands = tokens[separator_index + 1 :]
    parsed: dict[str, list[str | None]] = {}
    index = 0
    while index < len(options):
        token = options[index]
        value: str | None = None
        flag = token
        if token.startswith("--") and "=" in token:
            flag, _, value = token.partition("=")
            if flag not in RG_LONG_VALUE_FLAGS:
                return False
        if flag in RG_BOOLEAN_FLAGS:
            if value is not None:
                return False
        elif flag in RG_VALUE_FLAGS:
            if value is None:
                index += 1
                if index >= len(options):
                    return False
                value = options[index]
        else:
            return False
        parsed.setdefault(flag, []).append(value)
        index += 1

    if not RG_REQUIRED_FLAGS.issubset(parsed):
        return False
    ignore_values = parsed.get("--ignore-file", [])
    if (
        len(ignore_values) != 1
        or Path(str(ignore_values[0])).resolve() != Path(ignore_file).resolve()
    ):
        return False
    globs = parsed.get("-g", []) + parsed.get("--glob", [])
    if len(globs) > 20 or any(
        value is None or not value.startswith("!") or len(value) > 256 for value in globs
    ):
        return False
    for flag in RG_CONTEXT_FLAGS:
        if not _bounded_integers(parsed.get(flag, []), minimum=0, maximum=10):
            return False
    for flag, maximum in (("-m", 200), ("--max-count", 200), ("--max-columns", 500)):
        if not _bounded_integers(parsed.get(flag, []), minimum=1, maximum=maximum):
            return False
    if not _bounded_filesizes(parsed.get("--max-filesize", []), maximum=10 * 1024 * 1024):
        return False

    files_mode = "--files" in parsed
    if not operands or (not files_mode and len(operands) < 2):
        return False
    paths = operands if files_mode else operands[1:]
    if len(paths) > 20 or (not files_mode and len(operands[0]) > 4096):
        return False
    directories, exact_files = authorized_search_roots(context)
    return bool(directories or exact_files) and all(
        authorized_search_path(path, directories, exact_files) for path in paths
    )


def _bounded_integers(values: list[str | None], *, minimum: int, maximum: int) -> bool:
    if len(values) > 1:
        return False
    return all(
        value is not None and value.isdecimal() and minimum <= int(value) <= maximum
        for value in values
    )


def _bounded_filesizes(values: list[str | None], *, maximum: int) -> bool:
    if len(values) > 1:
        return False
    for value in values:
        match = re.fullmatch(r"([1-9][0-9]*)([KMG]?)", value or "", re.IGNORECASE)
        if match is None:
            return False
        multiplier = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}[match.group(2).upper()]
        if int(match.group(1)) * multiplier > maximum:
            return False
    return True


def authorized_search_roots(context: dict[str, Any]) -> tuple[list[Path], list[Path]]:
    directories: list[Path] = []
    worktree = context.get("audit_worktree")
    if isinstance(worktree, str) and Path(worktree).is_absolute():
        directories.append(Path(worktree).resolve())
    inventory = context.get("inventory")
    environment = inventory.get("python_environment") if isinstance(inventory, dict) else None
    if isinstance(environment, dict):
        for key in ("interpreter_prefix", "stdlib_root"):
            value = environment.get(key)
            if isinstance(value, str) and Path(value).is_absolute():
                directories.append(Path(value).resolve())
    exact_files: list[Path] = []
    references = context.get("references")
    if isinstance(references, dict):
        for value in references.values():
            if isinstance(value, str) and Path(value).is_absolute():
                exact_files.append(Path(value).resolve())
    return directories, exact_files


def _secret_path(path: Path) -> bool:
    name = path.name.lower()
    lowered_parts = {part.lower() for part in path.parts}
    return (
        name in SECRET_BASENAMES
        or name.startswith((".env.", "secrets.", "credentials."))
        or path.suffix.lower() in SECRET_SUFFIXES
        or ".git" in lowered_parts
        or any(part.startswith(".env.") for part in lowered_parts)
    )


def authorized_search_path(value: str, directories: list[Path], exact_files: list[Path]) -> bool:
    path = Path(value)
    if not path.is_absolute() or _secret_path(path):
        return False
    resolved = path.resolve()
    if _secret_path(resolved):
        return False
    project = os.environ.get("QWEN_CODE_PROJECT_DIR")
    if project:
        private_root = Path(project).expanduser().resolve()
        if any(
            resolved == candidate or candidate in resolved.parents
            for candidate in (private_root / "workflows", private_root / "github")
        ):
            return False
    if resolved in exact_files:
        return True
    return any(resolved == root or root in resolved.parents for root in directories)


def assigned_task_context(payload: dict[str, Any]) -> dict[str, Any] | None:
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str):
        return None
    try:
        lines = Path(transcript).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("type") != "tool_result":
            continue
        message = row.get("message") if isinstance(row, dict) else None
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        parts = message.get("parts") if isinstance(message, dict) else None
        if not isinstance(parts, list):
            continue
        for part in reversed(parts):
            response = part.get("functionResponse") if isinstance(part, dict) else None
            if not isinstance(response, dict) or response.get("name") != (
                "mcp__github_workflows__task_context"
            ):
                continue
            response_body = response.get("response")
            if not isinstance(response_body, dict):
                continue
            value = response_body.get("output")
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    continue
            if not isinstance(value, dict):
                continue
            for candidate in (
                value,
                value.get("structuredContent"),
                value.get("structured_content"),
            ):
                root = candidate.get("audit_worktree") if isinstance(candidate, dict) else None
                if isinstance(root, str) and Path(root).is_absolute():
                    return candidate
            return None
    return None


def main() -> int:
    payload: Any = None
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        if payload.get("hook_event_name") != "PreToolUse":
            return 0
        if payload.get("agent_type") == "gh-audit-repo-worker":
            allowed = payload.get("tool_name") != "run_shell_command" or allowed_worker_search(
                payload
            )
            print(
                json.dumps(
                    decision(
                        "allow" if allowed else "deny",
                        None if allowed else WORKER_SHELL_DENIAL,
                    )
                )
            )
            return 0
        if audit_session(payload):
            if public_text_has_absolute_path(payload):
                print(json.dumps(decision("deny", PUBLIC_PATH_DENIAL)))
                return 0
            if targets_private_boundary(payload):
                print(json.dumps(decision("deny", DENIAL)))
                return 0
        print(json.dumps(decision("allow")))
    except Exception:  # noqa: BLE001 - boundary hooks must fail safely on malformed input
        if (
            isinstance(payload, dict)
            and payload.get("agent_type") == "gh-audit-repo-worker"
            and payload.get("tool_name") == "run_shell_command"
        ):
            print(json.dumps(decision("deny", WORKER_SHELL_DENIAL)))
        # A local policy helper must not disrupt unrelated or malformed sessions.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
