from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).parents[1]
EXTENSION = ROOT / "extensions/github-workflows"
HOOK = EXTENSION / "hooks/guard-audit-boundary.py"
RG_EXCLUDES = EXTENSION / "references/github-rg-excludes.ignore"


def rg_command(*operands: str, files: bool = False, extra: str = "") -> str:
    mode = "--files " if files else "-n "
    return (
        f"rg {mode}--hidden --no-config --no-ignore-parent --no-ignore-vcs "
        f"--ignore-file {RG_EXCLUDES} {extra}-- " + " ".join(operands)
    )


class TestAuditBoundaryHook:
    def invoke(
        self,
        tool_name: str,
        tool_input: dict[str, object],
        *,
        session: str = "audit",
        task_context: dict[str, object] | None = None,
        project_dir: str | None = None,
        transcript_text: str | None = None,
        unreadable: bool = False,
        raw: bool = False,
    ):
        """Invoke the guard as a subprocess against a modeled client payload.

        ``session`` models which kind of Qwen session issued the tool call using
        the client's real transcript layout: a subagent transcript lives under
        ``.../projects/<project>/subagents/`` and a main-chat transcript under
        ``.../chats/``. The client never sends ``agent_type``.
        """
        with tempfile.TemporaryDirectory(prefix="audit-hook-test-") as directory:
            base = Path(directory) / "projects" / "proj"
            if session in ("worker", "nonaudit"):
                subagent_dir = base / "subagents" / "session-1"
                subagent_dir.mkdir(parents=True)
                transcript = subagent_dir / "agent-worker-1.jsonl"
            else:
                chat_dir = base / "chats"
                chat_dir.mkdir(parents=True)
                transcript = chat_dir / "session-1.jsonl"
            if unreadable:
                # A directory where the transcript file should be makes
                # read_text raise OSError, modeling an unreadable transcript.
                transcript.mkdir(parents=True, exist_ok=True)
            elif transcript_text is not None:
                transcript.write_text(transcript_text, encoding="utf-8")
            elif session == "worker":
                context = task_context or {
                    "audit_worktree": "/tmp/audit",
                    "references": {"rg_excludes": str(RG_EXCLUDES)},
                }
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
                                            "response": {"output": json.dumps(context)},
                                        }
                                    }
                                ],
                            },
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
            elif session == "nonaudit":
                transcript.write_text(
                    "worker conversation without an audit task context\n",
                    encoding="utf-8",
                )
            elif session == "audit":
                transcript.write_text(
                    "# Audit GitHub Repository\n"
                    "Base directory for this skill: /extension/skills/gh-audit-repo\n",
                    encoding="utf-8",
                )
            else:  # unrelated
                transcript.write_text("ordinary conversation\n", encoding="utf-8")
            payload = {
                "hook_event_name": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": tool_input,
                "transcript_path": str(transcript),
                "session_id": "session-1",
                "cwd": directory,
                "permission_mode": "yolo",
                "tool_use_id": "tool-use-1",
            }
            result = subprocess.run(
                [str(HOOK)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                check=True,
                env={
                    **os.environ,
                    **({"QWEN_CODE_PROJECT_DIR": project_dir} if project_dir else {}),
                },
            )
            if raw:
                return result
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
            {"file_path": str(EXTENSION / "references/github-runtime-policy.md")},
        )
        assert result["permissionDecision"] == "allow"

    def test_assigned_worker_can_read_its_implementation_shard(self) -> None:
        result = self.invoke(
            "read_file",
            {"file_path": str(ROOT / "src/github_workflows/runtime.py")},
            session="worker",
        )
        assert result["permissionDecision"] == "allow"

    def test_assigned_worker_can_use_direct_rg_in_authorized_roots(self) -> None:
        context = {
            "audit_worktree": "/tmp/audit",
            "inventory": {
                "python_environment": {
                    "interpreter_prefix": "/tmp/venv",
                    "stdlib_root": "/usr/lib/python3.13",
                }
            },
            "references": {
                "rg_excludes": str(RG_EXCLUDES),
                "runtime_policy": "/tmp/runtime-policy.md",
            },
        }
        commands = (
            rg_command("needle", "/tmp/audit"),
            rg_command("/tmp/audit/data", files=True),
            rg_command("needle", "/tmp/venv/lib"),
            rg_command("needle", "/usr/lib/python3.13"),
            rg_command("needle", "/tmp/runtime-policy.md"),
            rg_command("needle", "/tmp/audit", extra="-g '!vendor/**' -g '!*.min.js' "),
            rg_command("/tmp/audit", files=True, extra="--glob '!build/**' "),
        )
        for command in commands:
            result = self.invoke(
                "run_shell_command",
                {"command": command},
                session="worker",
                task_context=context,
            )
            assert result["permissionDecision"] == "allow", command

    def test_worker_direct_rg_rejects_unsafe_or_unbounded_commands(self) -> None:
        denied = (
            "grep needle /tmp/audit",
            rg_command("needle", "/tmp/audit") + " ; pwd",
            rg_command("$(pwd)", "/tmp/audit"),
            rg_command("$PATTERN", "/tmp/audit"),
            rg_command("needle", "/tmp/audit") + " > /tmp/result",
            rg_command("needle", "/tmp/other"),
            rg_command("needle", "relative/path"),
            rg_command("needle", "/tmp/audit/.env"),
            rg_command("needle", "/tmp/audit/.git/config"),
            "rg -n --hidden --no-config --no-ignore-parent --no-ignore-vcs -- needle /tmp/audit",
            rg_command("needle", "/tmp/audit", extra="--follow "),
            rg_command("needle", "/tmp/audit", extra="--pre command "),
            rg_command("needle", "/tmp/audit", extra="--search-zip "),
            rg_command("needle", "/tmp/audit", extra="--max-count 201 "),
            rg_command("needle", "/tmp/audit", extra="--context 11 "),
            rg_command("needle", "/tmp/audit", extra="--max-filesize 11M "),
            rg_command("needle", "/tmp/audit", extra="-g '.env' "),
            rg_command("needle", "/tmp/audit", extra="-g '*' "),
            rg_command("needle", "/tmp/audit", extra="-g '*.py' "),
            rg_command(
                "needle",
                "/tmp/audit",
                extra="".join(f"-g '!*.{index}' " for index in range(21)),
            ),
        )
        for command in denied:
            result = self.invoke(
                "run_shell_command",
                {"command": command},
                session="worker",
            )
            assert result["permissionDecision"] == "deny"
            assert "constrained direct rg" in result["permissionDecisionReason"]

    def test_worker_direct_rg_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="audit-rg-root-") as directory:
            root = Path(directory)
            link = root / "outside"
            link.symlink_to("/tmp")
            result = self.invoke(
                "run_shell_command",
                {"command": rg_command("needle", str(link))},
                session="worker",
                task_context={
                    "audit_worktree": str(root),
                    "references": {"rg_excludes": str(RG_EXCLUDES)},
                },
            )
        assert result["permissionDecision"] == "deny"

    def test_worker_direct_rg_rejects_private_workflow_storage(self) -> None:
        context = {
            "audit_worktree": "/tmp/project-state/workflows/gh-audit-repo/current",
            "references": {"rg_excludes": str(RG_EXCLUDES)},
        }
        result = self.invoke(
            "run_shell_command",
            {"command": rg_command("needle", str(context["audit_worktree"]))},
            session="worker",
            task_context=context,
            project_dir="/tmp/project-state",
        )
        assert result["permissionDecision"] == "deny"

    def test_secret_ignore_file_keeps_repository_data_searchable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="audit-rg-data-") as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            (data / "example.txt").write_text("searchable\n", encoding="utf-8")
            (root / ".env").write_text("searchable\n", encoding="utf-8")
            result = subprocess.run(
                [
                    "rg",
                    "--hidden",
                    "--no-config",
                    "--no-ignore-parent",
                    "--no-ignore-vcs",
                    "--ignore-file",
                    str(RG_EXCLUDES),
                    "--files",
                    "--",
                    str(root),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        assert "data/example.txt" in result.stdout
        assert ".env" not in result.stdout

    def test_positive_globs_cannot_override_secret_exclusions(self) -> None:
        for glob in (".env", "*", "**/.git/**", "secrets.*"):
            result = self.invoke(
                "run_shell_command",
                {"command": rg_command("/tmp/audit", files=True, extra=f"-g '{glob}' ")},
                session="worker",
            )
            assert result["permissionDecision"] == "deny", glob

    def test_nonaudit_subagent_is_unrestricted(self) -> None:
        # A subagent whose transcript is readable but carries no audit
        # task context is not an audit worker, so its shell stays unrestricted.
        result = self.invoke(
            "run_shell_command",
            {"command": "grep needle /tmp/audit"},
            session="nonaudit",
        )
        assert result["permissionDecision"] == "allow"

        reads = self.invoke(
            "read_file",
            {"file_path": str(ROOT / "src/github_workflows/runtime.py")},
            session="nonaudit",
        )
        assert reads["permissionDecision"] == "allow"

    def test_forged_task_context_is_not_authoritative(self) -> None:
        # A task_context recorded under the assistant role is not authoritative;
        # it must not confer audit-worker status (which would restrict the shell
        # to authorized rg searches). The subagent stays unrestricted.
        forged = (
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
            + "\n"
        )
        result = self.invoke(
            "run_shell_command",
            {"command": "grep needle /tmp/forged"},
            session="worker",
            transcript_text=forged,
        )
        assert result["permissionDecision"] == "allow"

    def test_malformed_task_context_is_not_authoritative(self) -> None:
        # A task_context whose response is not a structured value is not
        # authoritative; the subagent is not treated as an audit worker.
        malformed = (
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
            + "\n"
        )
        result = self.invoke(
            "run_shell_command",
            {"command": "grep needle /tmp/audit"},
            session="worker",
            transcript_text=malformed,
        )
        assert result["permissionDecision"] == "allow"

    def test_subagent_unreadable_transcript_fails_closed(self) -> None:
        # An unreadable subagent transcript is a degraded state: the guard cannot
        # verify an audit task context, so it denies the shell command and logs
        # the degradation to stderr (non-silent).
        result = self.invoke(
            "run_shell_command",
            {"command": "grep needle /tmp/audit"},
            session="worker",
            unreadable=True,
            raw=True,
        )
        output = json.loads(result.stdout)["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "constrained direct rg" in output["permissionDecisionReason"]
        assert "unverifiable" in result.stderr

    def test_unrelated_session_is_not_restricted(self) -> None:
        result = self.invoke(
            "read_file",
            {"file_path": str(ROOT / "src/github_workflows/runtime.py")},
            session="unrelated",
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
        for path in ("/home/user/project/src/tool.py:12", r"C:\work\repo\src\tool.py"):
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

    def test_audit_publication_allows_required_implementation_citations(self) -> None:
        issue = self.invoke(
            "mcp__github__issue_write",
            {
                "method": "create",
                "title": "Hook denies convention-required citations",
                "body": (
                    "Evidence: `src/github_workflows/github_cache.py:128` and "
                    "`extensions/github-workflows/hooks/guard-audit-boundary.py:61`; "
                    "the agent-workflows project name must stay publishable."
                ),
            },
        )
        assert issue["permissionDecision"] == "allow"

        comment = self.invoke(
            "mcp__github__add_issue_comment",
            {
                "issue_number": 28,
                "comment": "Evidence: `src/github_workflows/github_cache.py:128`",
            },
        )
        assert comment["permissionDecision"] == "allow"

    def test_audit_publication_citations_do_not_weaken_boundary_denials(self) -> None:
        absolute = self.invoke(
            "mcp__github__issue_write",
            {
                "method": "create",
                "title": "Concrete failure",
                "body": f"Evidence: `{ROOT / 'src/github_workflows/runtime.py'}`",
            },
        )
        assert absolute["permissionDecision"] == "deny"
        assert "repository-relative" in absolute["permissionDecisionReason"]

        read = self.invoke(
            "read_file",
            {"file_path": str(ROOT / "src/github_workflows/runtime.py")},
        )
        assert read["permissionDecision"] == "deny"
        assert "public github_workflows MCP tools" in read["permissionDecisionReason"]

    def test_absolute_path_guard_is_limited_to_audit_publication(self) -> None:
        unrelated = self.invoke(
            "mcp__github__issue_write",
            {"body": "Evidence: `/home/user/project/src/tool.py`"},
            session="unrelated",
        )
        assert unrelated["permissionDecision"] == "allow"

    def test_detection_gh_audit_repo_and_skill_base_directory(self) -> None:
        # The second detection branch recognizes an audit session from the
        # gh-audit-repo marker plus the skill base-directory line, without the
        # skill H1. This preserves the exact substring semantics of the guard.
        result = self.invoke(
            "read_file",
            {"file_path": str(ROOT / "src/github_workflows/runtime.py")},
            transcript_text=(
                "Launching the gh-audit-repo workflow now.\n"
                "Base directory for this skill: /extension/skills/gh-audit-repo\n"
            ),
        )
        assert result["permissionDecision"] == "deny"

    def test_stale_relative_targets_are_no_longer_denied(self) -> None:
        # The two stale agents/extensions/github-workflows/... literals were
        # superseded by the dynamic extension targets and removed. A path that
        # only carries the stale relative structure (and nothing else the guard
        # protects) is no longer denied.
        result = self.invoke(
            "read_file",
            {"file_path": "/x/agents/extensions/github-workflows/agents/worker.md"},
        )
        assert result["permissionDecision"] == "allow"
