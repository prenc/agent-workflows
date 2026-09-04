from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).parents[1]
EXTENSION = ROOT / "extensions/github-workflows"
HOOK = EXTENSION / "hooks/guard-audit-boundary.py"
SEARCH = EXTENSION / "hooks/readonly-search.py"


class TestAuditBoundaryHook:
    def invoke(
        self,
        tool_name: str,
        tool_input: dict[str, object],
        *,
        audit: bool = True,
        agent_type: str | None = None,
        audit_worktree: str | None = None,
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory(prefix="audit-hook-test-") as directory:
            transcript = Path(directory) / "transcript.jsonl"
            if audit_worktree is not None:
                transcript.write_text(
                    json.dumps(
                        {
                            "type": "tool_result",
                            "message": {
                                "role": "user",
                                "parts": [
                                    {
                                        "functionResponse": {
                                            "name": "mcp__github_workflows__task_context",
                                            "response": {
                                                "output": json.dumps(
                                                    {"audit_worktree": audit_worktree}
                                                )
                                            },
                                        }
                                    }
                                ],
                            },
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
            else:
                transcript.write_text(
                    "# Audit GitHub Repository\nBase directory for this skill: /extension/skills/gh-audit-repo\n"
                    if audit
                    else "ordinary conversation\n",
                    encoding="utf-8",
                )
            payload = {
                "hook_event_name": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": tool_input,
                "transcript_path": str(transcript),
            }
            if agent_type is not None:
                payload["agent_type"] = agent_type
            result = subprocess.run(
                [str(HOOK)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                check=True,
            )
            return json.loads(result.stdout)["hookSpecificOutput"]

    def test_supervisor_cannot_read_workflow_implementation(self) -> None:
        result = self.invoke(
            "read_file",
            {
                "file_path": str(ROOT / "src/github_workflows/runtime.py"),
            },
        )
        assert result["permissionDecision"] == "deny"
        assert "public github_workflows MCP tools" in result["permissionDecisionReason"]

        relative = self.invoke(
            "glob",
            {
                "path": "agent-workflows",
                "pattern": "**/*",
            },
        )
        assert relative["permissionDecision"] == "deny"

    def test_supervisor_can_read_public_reference(self) -> None:
        result = self.invoke(
            "read_file",
            {
                "file_path": str(EXTENSION / "references/github-runtime-policy.md"),
            },
        )
        assert result["permissionDecision"] == "allow"

    def test_assigned_worker_can_read_its_implementation_shard(self) -> None:
        result = self.invoke(
            "read_file",
            {"file_path": str(ROOT / "src/github_workflows/runtime.py")},
            agent_type="gh-audit-repo-worker",
        )
        assert result["permissionDecision"] == "allow"

    def test_assigned_worker_can_invoke_only_bounded_search_helper(self) -> None:
        allowed = self.invoke(
            "run_shell_command",
            {"command": f"{SEARCH} files --root /tmp/audit --path src --limit 10"},
            agent_type="gh-audit-repo-worker",
            audit_worktree="/tmp/audit",
        )
        assert allowed["permissionDecision"] == "allow"

        denied = (
            "rg needle /tmp/audit",
            f"{SEARCH} files --root /tmp/audit ; rg secret /tmp",
            f"{SEARCH} files --root $(pwd)",
            f"{SEARCH} files --root $AUDIT_ROOT",
            f"{SEARCH} files --root /tmp/audit > /tmp/result",
            f"{SEARCH} files --root /tmp/other",
        )
        for command in denied:
            result = self.invoke(
                "run_shell_command",
                {"command": command},
                agent_type="gh-audit-repo-worker",
                audit_worktree="/tmp/audit",
            )
            assert result["permissionDecision"] == "deny"
            assert "references.readonly_search" in result["permissionDecisionReason"]

    def test_worker_search_requires_authoritative_task_context(self) -> None:
        missing = self.invoke(
            "run_shell_command",
            {"command": f"{SEARCH} files --root /tmp/audit"},
            agent_type="gh-audit-repo-worker",
        )
        assert missing["permissionDecision"] == "deny"

        with tempfile.TemporaryDirectory(prefix="audit-hook-forgery-") as directory:
            transcript = Path(directory) / "transcript.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "model",
                            "parts": [
                                {
                                    "functionResponse": {
                                        "name": "mcp__github_workflows__task_context",
                                        "response": {
                                            "output": json.dumps({"audit_worktree": "/tmp/forged"})
                                        },
                                    }
                                }
                            ],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            payload = {
                "hook_event_name": "PreToolUse",
                "tool_name": "run_shell_command",
                "tool_input": {"command": f"{SEARCH} files --root /tmp/forged"},
                "transcript_path": str(transcript),
                "agent_type": "gh-audit-repo-worker",
            }
            result = subprocess.run(
                [str(HOOK)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                check=True,
            )
            output = json.loads(result.stdout)["hookSpecificOutput"]
            assert output["permissionDecision"] == "deny"

    def test_worker_shell_guard_fails_closed_on_malformed_context_result(self) -> None:
        with tempfile.TemporaryDirectory(prefix="audit-hook-malformed-") as directory:
            transcript = Path(directory) / "transcript.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "tool_result",
                        "message": {
                            "role": "user",
                            "parts": [
                                {
                                    "functionResponse": {
                                        "name": "mcp__github_workflows__task_context",
                                        "response": "malformed",
                                    }
                                }
                            ],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            payload = {
                "hook_event_name": "PreToolUse",
                "tool_name": "run_shell_command",
                "tool_input": {"command": f"{SEARCH} files --root /tmp/audit"},
                "transcript_path": str(transcript),
                "agent_type": "gh-audit-repo-worker",
            }
            result = subprocess.run(
                [str(HOOK)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                check=True,
            )
            output = json.loads(result.stdout)["hookSpecificOutput"]
            assert output["permissionDecision"] == "deny"

    def test_unrelated_session_is_not_restricted(self) -> None:
        result = self.invoke(
            "read_file",
            {"file_path": str(ROOT / "src/github_workflows/runtime.py")},
            audit=False,
        )
        assert result["permissionDecision"] == "allow"

    def test_supervisor_cannot_discover_private_state(self) -> None:
        result = self.invoke(
            "run_shell_command",
            {
                "command": "find $QWEN_CODE_PROJECT_DIR/workflows/gh-audit-repo/current -type f",
            },
        )
        assert result["permissionDecision"] == "deny"

    def test_audit_publication_rejects_absolute_paths(self) -> None:
        for path in ("/home/user/project/src/tool.py:12", r"C:\\work\\repo\\src\\tool.py"):
            result = self.invoke(
                "mcp__github__issue_write",
                {"method": "create", "title": "Concrete failure", "body": f"Evidence: `{path}`"},
            )
            assert result["permissionDecision"] == "deny"
            assert "repository-relative" in result["permissionDecisionReason"]

    def test_audit_publication_allows_repository_paths_and_urls(self) -> None:
        result = self.invoke(
            "mcp__github__issue_write",
            {
                "method": "create",
                "title": "Concrete failure",
                "body": "Evidence: `src/tool.py:12`; see https://example.com/docs/path.",
            },
        )
        assert result["permissionDecision"] == "allow"

    def test_absolute_path_guard_is_limited_to_audit_publication(self) -> None:
        unrelated = self.invoke(
            "mcp__github__issue_write",
            {"body": "Evidence: `/home/user/project/src/tool.py`"},
            audit=False,
        )
        assert unrelated["permissionDecision"] == "allow"
