"""High-level, path-free facade over the reviewed workflow domain modules."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, cast

from . import (
    audit_inventory,
    audit_knowledge,
    audit_metrics,
    audit_probe,
    feedback,
    github_cache,
    workflow_run,
)
from .models import (
    AuditRecordRequest,
    HistoryManageRequest,
    HistoryQueryRequest,
    InventoryRequest,
    KnowledgeRequest,
    ProbeRequest,
    PublishRequest,
    RunManageRequest,
    TaskManageRequest,
    WorkflowFeedbackRequest,
    WorkflowName,
)

SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
HISTORY_ARTIFACT_BYTES = 5 * 1024 * 1024
HISTORY_ARTIFACT_TOTAL_BYTES = 25 * 1024 * 1024
TASK_HISTORY_LIMIT = 40
TASK_VALIDATION_EXCERPT_BYTES = 2 * 1024
TASK_HISTORY_FIELDS = (
    "kind",
    "number",
    "url",
    "title",
    "state",
    "state_reason",
    "labels",
)
TASK_METADATA_FIELDS = ("id", "logical_id", "role", "unit", "attempt", "status", "required")
TASK_VALIDATION_FIELDS = (
    "id",
    "probe_id",
    "candidate_id",
    "status",
    "artifact",
    "returncode",
    "timed_out",
    "worktree_unchanged",
    "stdout_excerpt",
    "stderr_excerpt",
    "stdout_truncated",
    "stderr_truncated",
)
CANDIDATE_FINGERPRINT_FIELD = "candidate_fingerprint"
WORKFLOW_REF_NAMES: dict[WorkflowName, str] = {
    "gh-audit-repo": "audit",
    "gh-curate-issues": "curate",
    "gh-implement-issue": "implement",
}
REF_WORKFLOWS = {name: workflow for workflow, name in WORKFLOW_REF_NAMES.items()}


class WorkflowRuntime:
    """Resolve ambient Qwen state and expose atomic workflow operations."""

    def __init__(self, workspace: Path, project_dir: Path | None = None) -> None:
        self.workspace = workspace.expanduser().resolve()
        if not self.workspace.is_dir():
            raise ValueError("workspace must be an existing directory")
        configured = project_dir or (
            Path(os.environ["QWEN_CODE_PROJECT_DIR"])
            if os.environ.get("QWEN_CODE_PROJECT_DIR")
            else None
        )
        if configured is None:
            raise ValueError("QWEN_CODE_PROJECT_DIR is required")
        self.project_dir = configured.expanduser().resolve()

    @contextmanager
    def lock(self) -> Iterator[None]:
        lock_dir = self.project_dir / "workflows"
        lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(lock_dir, 0o700)
        descriptor = os.open(lock_dir / ".runtime.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def current(self, workflow: WorkflowName) -> Path:
        return self.project_dir / "workflows" / workflow / "current"

    def state(self, workflow: WorkflowName) -> dict[str, Any]:
        return workflow_run.load_state(self.current(workflow))

    def feedback_private_paths(self) -> list[tuple[Path, str]]:
        """Return path replacements shared by live failures and durable feedback."""
        return [
            (self.project_dir, "<project-state>"),
            (self.workspace, "<workspace>"),
            (Path.home(), "<home>"),
        ]

    def _feedback_attribution(
        self, task_ref: str | None = None
    ) -> tuple[str | None, str | None, str | None, dict[str, str] | None]:
        active: list[dict[str, Any]] = []
        for workflow in ("gh-audit-repo", "gh-curate-issues", "gh-implement-issue"):
            try:
                state = self.state(workflow)
            except (OSError, ValueError):
                continue
            if state.get("status") not in workflow_run.TERMINAL:
                active.append(state)
        workflow = str(active[0].get("workflow")) if len(active) == 1 else None
        run_id = str(active[0].get("run_id")) if len(active) == 1 else None
        task: dict[str, str] | None = None
        if task_ref is not None:
            parsed_workflow, parsed_run_ref, task_id = self._parse_task_ref(task_ref)
            workflow = parsed_workflow
            # Legacy full workflow names carry the raw run ID for feedback attribution.
            # Remove this branch with legacy task-ref parsing after pre-short-ref runs
            # no longer need to resume.
            run_id = parsed_run_ref if task_ref.split(":", 1)[0] in workflow_run.WORKFLOWS else None
            task = {"id": task_id}
            try:
                state = self.state(parsed_workflow)
            except (OSError, ValueError):
                state = {}
            matching_state = state if self._task_ref_matches(state, parsed_run_ref) else {}
            if matching_state:
                run_id = str(matching_state["run_id"])
            stored_task = matching_state.get("tasks", {}).get(task_id)
            if isinstance(stored_task, dict):
                role = stored_task.get("role")
                if isinstance(role, str) and role:
                    task["role"] = role
            repository = matching_state.get("repository")
            if not isinstance(repository, str) or not re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
                repository = None
            return repository, workflow, run_id, task
        repositories = {
            str(state["repository"])
            for state in active
            if isinstance(state.get("repository"), str)
            and re.fullmatch(r"[^/\s]+/[^/\s]+", state["repository"])
        }
        repository = next(iter(repositories)) if len(repositories) == 1 else None
        if repository is None:
            repository = feedback.repository_from_workspace(self.workspace)
        return repository, workflow, run_id, task

    def workflow_feedback(
        self,
        request: WorkflowFeedbackRequest,
        *,
        provenance: dict[str, Any] | None = None,
        failure_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record one bounded agent observation outside workflow state."""
        repository, workflow, run_id, task = self._feedback_attribution(request.task_ref)
        attached = failure_context is not None or request.tool is not None
        context = failure_context or {}
        stored_provenance = {
            **(provenance or {}),
            **(context.get("provenance") or {}),
        }
        if task is not None:
            stored_provenance["task"] = task
        origin = dict(context.get("origin") or {})
        if request.error_ref is not None:
            origin["error_ref"] = request.error_ref
        result = feedback.append(
            message=request.message,
            tool=context.get("tool", request.tool),
            origin=origin or None,
            repository=repository,
            workflow=workflow,
            run_id=run_id,
            provenance=stored_provenance,
            private_paths=self.feedback_private_paths(),
        )
        return {**result, "context_attached": attached}

    @staticmethod
    def _invoke(
        handler: Callable[[argparse.Namespace], Any],
        *,
        allow_timeout: bool = False,
        **values: Any,
    ) -> dict[str, Any]:
        output = StringIO()
        with redirect_stdout(output):
            result = handler(argparse.Namespace(**values))
        rendered = output.getvalue().strip()
        payload = json.loads(rendered) if rendered else {}
        if not isinstance(payload, dict):
            raise ValueError("workflow operation returned a non-object response")
        expected_timeout = allow_timeout and result == 124 and payload.get("timed_out") is True
        if (
            isinstance(result, int)
            and result not in {0, payload.get("returncode")}
            and not expected_timeout
        ):
            raise RuntimeError(f"workflow operation failed with exit code {result}")
        return payload

    @staticmethod
    def _probe_validation_status(result: dict[str, Any]) -> str:
        probe_status = result.get("probe_status")
        if probe_status in {"succeeded", "failed", "timed-out", "unavailable"}:
            return probe_status
        if result.get("timed_out") is True:
            return "timed-out"
        returncode = result.get("returncode")
        if isinstance(returncode, int) and not isinstance(returncode, bool):
            return "succeeded" if returncode == 0 else "failed"
        return "unavailable"

    @staticmethod
    @contextmanager
    def _json_file(value: Any) -> Iterator[Path]:
        with tempfile.TemporaryDirectory(prefix="qwen-workflow-input-") as directory:
            path = Path(directory) / "input.json"
            path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
            yield path

    def _base(self, workflow: WorkflowName) -> dict[str, Any]:
        return {
            "project_root": self.workspace,
            "project_dir": self.project_dir,
            "workflow": workflow,
        }

    def _event(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.state("gh-audit-repo")
        with self._json_file(payload) as source:
            return self._invoke(
                workflow_run.audit_event,
                **self._base("gh-audit-repo"),
                expected_revision=state["revision"],
                input=source,
            )

    @staticmethod
    def _history_artifact_root() -> Path:
        configured = os.environ.get("QWEN_HOME")
        return (
            Path(configured) if configured else Path.home() / ".qwen"
        ).expanduser().resolve() / "tmp"

    @classmethod
    def _history_artifact_records(cls, paths: list[str], kind: str) -> list[dict[str, Any]]:
        root = cls._history_artifact_root()
        records: list[dict[str, Any]] = []
        total_bytes = 0
        envelopes = {
            "issue": ("issues",),
            "pull": ("pullRequests", "pull_requests", "pulls"),
        }
        wrong_envelopes = {
            "issue": {"pullRequests", "pull_requests", "pulls"},
            "pull": {"issues"},
        }
        for raw_path in paths:
            candidate = Path(raw_path).expanduser()
            try:
                metadata = candidate.lstat()
            except OSError as error:
                raise ValueError(f"history artifact is unavailable: {raw_path}") from error
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise ValueError("history artifacts must be regular, non-symlink files")
            resolved = candidate.resolve()
            try:
                relative = resolved.relative_to(root)
            except ValueError as error:
                raise ValueError("history artifacts must be Qwen persisted-output files") from error
            if len(relative.parts) != 3 or relative.parts[1] != "tool-results":
                raise ValueError("history artifacts must be under Qwen tmp/*/tool-results/")
            if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
                raise ValueError(
                    "history artifacts must be owned by the current user and privately writable"
                )
            if metadata.st_size > HISTORY_ARTIFACT_BYTES:
                raise ValueError(f"history artifact exceeds {HISTORY_ARTIFACT_BYTES} bytes")
            total_bytes += metadata.st_size
            if total_bytes > HISTORY_ARTIFACT_TOTAL_BYTES:
                raise ValueError(
                    f"history artifacts exceed {HISTORY_ARTIFACT_TOTAL_BYTES} bytes in total"
                )
            try:
                payload = json.loads(resolved.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise ValueError("history artifact must contain valid UTF-8 JSON") from error
            if isinstance(payload, list):
                page = payload
            elif isinstance(payload, dict):
                mismatched = wrong_envelopes[kind].intersection(payload)
                if mismatched:
                    raise ValueError(f"history artifact envelope does not match kind {kind}")
                page = None
                for name in (*envelopes[kind], "records"):
                    if name in payload:
                        page = payload[name]
                        break
                if page is None:
                    raise ValueError("history artifact does not contain a supported record list")
            else:
                raise ValueError("history artifact must contain a JSON object or array")
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise ValueError("every history artifact record must be an object")
            records.extend(page)
            if len(records) > 100:
                raise ValueError("history ingest accepts at most 100 records")
        return records

    @staticmethod
    def _compact_history_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Discard GitHub detail payloads before they reach private history storage."""
        fields = {
            "number",
            "issue_number",
            "pull_number",
            "state",
            "state_reason",
            "stateReason",
            "title",
            "labels",
            "assignees",
            "created_at",
            "createdAt",
            "updated_at",
            "updatedAt",
            "closed_at",
            "closedAt",
            "merged_at",
            "mergedAt",
            "url",
            "html_url",
            "base_ref",
            "base",
            "head_ref",
            "head",
            "head_sha",
            "headSha",
        }
        return [
            {key: value for key, value in record.items() if key in fields} for record in records
        ]

    def _audit_paths(self) -> tuple[dict[str, Any], Path, Path]:
        state = self.state("gh-audit-repo")
        if state.get("status") != "in-progress":
            raise ValueError("the current audit run is not active")
        worktree = state.get("audit_worktree")
        if not isinstance(worktree, str):
            raise ValueError("current audit state does not contain an audit worktree")
        return state, Path(worktree).resolve(), self.current("gh-audit-repo")

    @staticmethod
    def _verified_audit_worktree_head(state: dict[str, Any]) -> str:
        worktree = state.get("audit_worktree")
        expected = state.get("sha")
        if not isinstance(worktree, str) or not isinstance(expected, str):
            raise ValueError("audit worker context requires an audit worktree and SHA")
        result = subprocess.run(
            ["git", "-C", worktree, "rev-parse", "--verify", "HEAD^{commit}"],
            check=False,
            capture_output=True,
            text=True,
        )
        actual = result.stdout.strip()
        if result.returncode != 0 or not re.fullmatch(r"[0-9a-fA-F]{40}", actual):
            raise ValueError("audit worktree HEAD could not be verified")
        if actual.lower() != expected.lower():
            raise ValueError("audit worktree HEAD no longer matches audit SHA")
        return actual.lower()

    def _discard_stale_run(
        self,
        workflow: WorkflowName,
        *,
        retained_worktree: Path | None = None,
        acknowledged_publication: bool = False,
    ) -> None:
        current = self.current(workflow)
        if not current.is_dir():
            return
        state = workflow_run.load_state(current)
        if workflow == "gh-audit-repo":
            history = state.get("history", {})
            publication_pending = isinstance(history, dict) and history.get("publication_pending")
            if publication_pending and not acknowledged_publication:
                raise ValueError("pending publication requires resume")
            if publication_pending:
                workflow_run.append_journal(
                    current,
                    "publication_discarded",
                    candidate_id=history.get("candidate_id"),
                    operation=history.get("operation"),
                )
            run_id = str(state.get("run_id", ""))
            if SAFE_ID.fullmatch(run_id):
                staging = self.project_dir / "github" / "staging"
                (staging / f"records-{run_id}.sqlite3").unlink(missing_ok=True)
            raw_worktree = state.get("audit_worktree")
            if isinstance(raw_worktree, str):
                worktree = Path(raw_worktree).resolve()
                managed_roots = {
                    (self.workspace / ".worktrees").resolve(),
                    self._cache_worktree_root(),
                }
                if worktree.parent not in managed_roots or not worktree.name.startswith(
                    "gh-audit-repo-"
                ):
                    raise ValueError("stale audit worktree is outside the managed location")
                if worktree.exists() and worktree != retained_worktree:
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(self.workspace),
                            "worktree",
                            "remove",
                            "--force",
                            str(worktree),
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
        shutil.rmtree(current)

    @staticmethod
    def _ensure_private_directory(path: Path) -> Path:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise PermissionError("worktree cache must be an owned directory")
        path.chmod(0o700)
        return path.resolve()

    def _cache_worktree_root(self) -> Path:
        cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))).expanduser()
        if not cache.is_absolute():
            raise ValueError("XDG_CACHE_HOME must be an absolute path")
        application = self._ensure_private_directory(cache / "agent-workflows")
        worktrees = self._ensure_private_directory(application / "worktrees")
        identity = hashlib.sha256(str(self.workspace).encode()).hexdigest()[:16]
        return self._ensure_private_directory(worktrees / identity)

    def _worktree_root(self) -> Path:
        ignored = subprocess.run(
            [
                "git",
                "-C",
                str(self.workspace),
                "check-ignore",
                "-q",
                "--no-index",
                ".worktrees/probe",
            ],
            check=False,
        )
        return (
            (self.workspace / ".worktrees").resolve()
            if ignored.returncode == 0
            else self._cache_worktree_root()
        )

    def _source_and_worktree(self, confirmed: bool) -> dict[str, Any]:
        source = self._invoke(workflow_run.audit_source, project_root=self.workspace)
        if source.get("confirmation_required") and not confirmed:
            raise ValueError(
                "the audit source is not main/master; obtain user confirmation and retry"
            )
        sha = str(source["sha"])
        worktree = self._worktree_root() / f"gh-audit-repo-{sha[:7]}"
        if worktree.exists():
            actual = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if actual != sha:
                raise ValueError("the retained audit worktree points to a different commit")
        else:
            worktree.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.workspace),
                    "worktree",
                    "add",
                    "--detach",
                    str(worktree),
                    sha,
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        project_venv = self.workspace / ".venv"
        worktree_venv = worktree / ".venv"
        if project_venv.is_dir() and not worktree_venv.exists():
            worktree_venv.symlink_to(project_venv, target_is_directory=True)
        return {**source, "audit_worktree": str(worktree), "source_confirmed": confirmed}

    @staticmethod
    def _receipt(
        workflow: WorkflowName, state: dict[str, Any], changed: bool, **extra: Any
    ) -> dict[str, Any]:
        return {
            "run_id": state.get("run_id"),
            "workflow": workflow,
            "status": state.get("status"),
            "revision": state.get("revision"),
            "changed": changed,
            **extra,
        }

    @staticmethod
    def _run_ref(run_id: str) -> str:
        return hashlib.blake2s(run_id.encode(), digest_size=6).hexdigest()

    @classmethod
    def _task_ref_matches(cls, state: dict[str, Any], run_ref: str) -> bool:
        run_id = state.get("run_id")
        # Accepting the raw run ID preserves legacy workflow:run-id:task-id refs.
        # Remove the raw run_id alternative after pre-short-ref runs no longer
        # need to resume.
        return isinstance(run_id, str) and run_ref in {run_id, cls._run_ref(run_id)}

    @staticmethod
    def _task_ref(workflow: WorkflowName, state: dict[str, Any], task_id: str) -> str:
        run_id = state.get("run_id")
        if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id):
            raise ValueError("current run has an invalid run_id")
        return f"{WORKFLOW_REF_NAMES[workflow]}:{WorkflowRuntime._run_ref(run_id)}:{task_id}"

    @staticmethod
    def _audit_task_role(plan: Any, fallback: str | None = None) -> str:
        assignment = plan.assignment if plan is not None else {}
        mode = assignment.get("mode") if isinstance(assignment, dict) else None
        supplied = plan.role if plan is not None else None
        if mode in {"discover", "verify"}:
            if supplied is not None and supplied != mode:
                raise ValueError("task role must match assignment.mode")
            return str(mode)
        return str(supplied or fallback or "worker")

    @staticmethod
    def _candidate_fingerprint(candidate: dict[str, Any]) -> str:
        candidate_id = candidate.get("id")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise ValueError("verify assignment candidate requires a non-empty string id")
        canonical = {
            key: value
            for key, value in candidate.items()
            if key not in {"fingerprint", CANDIDATE_FINGERPRINT_FIELD}
        }
        try:
            rendered = json.dumps(
                canonical,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as error:
            raise ValueError("verify assignment candidate must be canonical JSON") from error
        return hashlib.sha256(rendered.encode()).hexdigest()

    @classmethod
    def _audit_task_assignment(
        cls,
        assignment: dict[str, Any],
        *,
        caller_supplied: bool,
    ) -> dict[str, Any]:
        if assignment.get("mode") != "verify":
            return assignment
        candidate = assignment.get("candidate")
        if not isinstance(candidate, dict):
            if caller_supplied:
                raise ValueError("verify assignment requires one canonical candidate object")
            raise ValueError(
                "verify task assignment is incompatible; retry it with a canonical candidate"
            )
        if caller_supplied and (
            CANDIDATE_FINGERPRINT_FIELD in assignment
            or "fingerprint" in candidate
            or CANDIDATE_FINGERPRINT_FIELD in candidate
        ):
            raise ValueError("candidate fingerprints are server-owned")
        canonical = {
            key: value
            for key, value in candidate.items()
            if key not in {"fingerprint", CANDIDATE_FINGERPRINT_FIELD}
        }
        fingerprint = cls._candidate_fingerprint(canonical)
        if not caller_supplied:
            stored_fingerprint = assignment.get(CANDIDATE_FINGERPRINT_FIELD)
            if stored_fingerprint != fingerprint:
                raise ValueError(
                    "verify task assignment is incompatible; retry it with a canonical candidate"
                )
        return {
            **assignment,
            "candidate": canonical,
            CANDIDATE_FINGERPRINT_FIELD: fingerprint,
        }

    def _write_audit_task_report(
        self, task_id: str, report: dict[str, Any], *, checkpoint: bool
    ) -> tuple[Path, str]:
        suffix = ""
        if checkpoint:
            sequence = 1
            while (
                self.current("gh-audit-repo") / f"areas/{task_id}.checkpoint.{sequence}.json"
            ).exists():
                sequence += 1
            suffix = f".checkpoint.{sequence}"
        reference = f"areas/{task_id}{suffix}.json"
        path = self.current("gh-audit-repo") / reference
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(rendered)
            os.chmod(path, 0o600)
        except FileExistsError as error:
            kind = "checkpoint" if checkpoint else "result"
            raise ValueError(f"task {kind} artifact already exists") from error
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path, reference

    def _validate_audit_report(
        self,
        task: dict[str, Any],
        report: dict[str, Any],
        action: str,
    ) -> str | None:
        assignment = self._audit_task_assignment(task.get("assignment", {}), caller_supplied=False)
        mode = assignment.get("mode")
        if mode not in {"discover", "verify"}:
            return str(report["status"]) if isinstance(report.get("status"), str) else None
        status = report.get("status")
        allowed = (
            {"partial", "CONTEXT_REQUEST", "MCP_UNAVAILABLE"}
            if action == "checkpoint"
            else {"complete", "partial"}
        )
        if status not in allowed:
            raise ValueError(f"{action} report status must be one of: {', '.join(sorted(allowed))}")
        if mode == "verify":
            expected = assignment.get(CANDIDATE_FINGERPRINT_FIELD)
            supplied = report.get(CANDIDATE_FINGERPRINT_FIELD)
            if action == "complete" and not isinstance(supplied, str):
                raise ValueError("complete verify report requires candidate_fingerprint")
            if supplied is not None and supplied != expected:
                raise ValueError("verify report candidate_fingerprint does not match assignment")
        return str(status)

    @staticmethod
    def _task_actions(task: dict[str, Any], scheduler: dict[str, Any]) -> list[str]:
        status = task.get("status")
        if status == "queued":
            return ["plan", "mark_running", "abandon"]
        if status == "running":
            return ["checkpoint", "complete", "fail", "abandon"]
        if status == "checkpointed":
            return ["mark_running", "complete", "fail", "abandon"]
        if status in {"completed", "failed", "abandoned"}:
            actions = ["retry"]
            if not task.get("integrated") and scheduler.get("next_action") == "integrate-result":
                actions.insert(0, "integration_begin")
            return actions
        return []

    @classmethod
    def _task_receipt_fields(
        cls, workflow: WorkflowName, state: dict[str, Any], task_id: str
    ) -> dict[str, Any]:
        task = state.get("tasks", {}).get(task_id)
        if not isinstance(task, dict):
            raise ValueError("managed task is missing from workflow state")
        scheduler = cls._scheduler_status(state)
        allowed_actions = cls._task_actions(task, scheduler)
        if workflow == "gh-audit-repo" and state.get("status") == "suspended":
            allowed_actions = (
                ["checkpoint", "complete", "fail", "abandon"]
                if task.get("status") in {"running", "checkpointed"}
                else []
            )
        return {
            "task_id": task_id,
            "task_ref": cls._task_ref(workflow, state, task_id),
            "task": task,
            "allowed_actions": allowed_actions,
            "scheduler": scheduler,
        }

    @staticmethod
    def _parse_task_ref(task_ref: str) -> tuple[WorkflowName, str, str]:
        parts = task_ref.split(":", 2)
        if len(parts) != 3:
            raise ValueError("task_ref must be copied exactly from task_manage")
        workflow_name, run_ref, task_id = parts
        # Falling back to a full gh-* workflow name accepts legacy references.
        # Remove the fallback after pre-short-ref runs no longer need to resume.
        workflow = REF_WORKFLOWS.get(workflow_name, workflow_name)
        if workflow not in workflow_run.WORKFLOWS:
            raise ValueError("task_ref contains an unsupported workflow")
        if not SAFE_ID.fullmatch(run_ref) or not SAFE_ID.fullmatch(task_id):
            raise ValueError("task_ref contains an invalid run or task")
        return cast(WorkflowName, workflow), run_ref, task_id

    @staticmethod
    def _generic_scheduler_status(state: dict[str, Any]) -> dict[str, Any]:
        scheduler = state.get("scheduler")
        tasks = state.get("tasks")
        if not isinstance(scheduler, dict) or not isinstance(tasks, dict):
            raise ValueError("generic workflow scheduler state is missing")
        inputs = state.get("inputs")
        if not isinstance(inputs, dict):
            raise ValueError("generic workflow inputs state is missing")
        limit = scheduler.get("limit")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("generic workflow concurrency must be a positive integer")
        queue = scheduler.get("integration_queue", [])
        if not isinstance(queue, list):
            raise ValueError("generic workflow integration queue is invalid")
        activity = scheduler.get("supervisor_activity")
        running = sum(
            task.get("status") == "running" for task in tasks.values() if isinstance(task, dict)
        )
        available = max(0, limit - running - (1 if activity is not None else 0))
        if queue:
            available = 0
        pending = state.get("pending", [])
        if not isinstance(pending, list):
            raise ValueError("generic workflow pending state is invalid")
        if pending:
            available = 0
        statuses = {task.get("status") for task in tasks.values() if isinstance(task, dict)}
        logical: dict[str, list[dict[str, Any]]] = {}
        for task in tasks.values():
            if isinstance(task, dict):
                logical.setdefault(str(task.get("logical_id", "")), []).append(task)
        missing_required = any(
            any(attempt.get("required", True) for attempt in attempts)
            and not any(
                attempt.get("status") == "completed" and attempt.get("integrated")
                for attempt in attempts
            )
            for attempts in logical.values()
        )
        if activity is not None:
            next_action = "finish-integration"
        elif queue:
            next_action = "integrate-result"
        elif pending:
            next_action = "resolve-pending"
        elif state.get("workflow") == "gh-implement-issue" and inputs.get("targets") and not tasks:
            next_action = "plan-tasks"
        elif "queued" in statuses and available > 0:
            next_action = "launch-worker"
        elif "checkpointed" in statuses:
            next_action = "resume-worker"
        elif running:
            next_action = "wait"
        elif missing_required:
            next_action = "retry-required-task"
        else:
            next_action = "ready-to-finish"
        return {
            "limit": limit,
            "running_workers": running,
            "supervisor_activity": activity,
            "integration_queue": list(queue),
            "worker_slots": available,
            "next_action": next_action,
            "control_plane_available": True,
        }

    @classmethod
    def _scheduler_status(cls, state: dict[str, Any]) -> dict[str, Any]:
        audit = state.get("workflow") == "gh-audit-repo" and state.get("schema_version") == 2
        status = (
            workflow_run.audit_scheduler_status(state)
            if audit
            else cls._generic_scheduler_status(state)
        )
        run_status = state.get("status")
        if run_status != "in-progress":
            status = {
                **status,
                "worker_slots": 0,
                "next_action": "resume" if run_status in {"suspended", "partial"} else "none",
            }
        return status

    @staticmethod
    def _validate_generic_terminal(state: dict[str, Any]) -> None:
        errors: list[str] = []
        tasks = state.get("tasks", {})
        if not isinstance(tasks, dict):
            raise ValueError("generic workflow tasks state is invalid")
        inputs = state.get("inputs", {})
        if (
            state.get("workflow") == "gh-implement-issue"
            and isinstance(inputs, dict)
            and inputs.get("targets")
            and not tasks
        ):
            errors.append("target work has not been planned")
        terminal = {"completed", "failed", "abandoned"}
        nonterminal = sorted(
            task_id
            for task_id, task in tasks.items()
            if not isinstance(task, dict) or task.get("status") not in terminal
        )
        if nonterminal:
            errors.append(f"nonterminal tasks: {nonterminal}")
        unintegrated = sorted(
            task_id
            for task_id, task in tasks.items()
            if isinstance(task, dict)
            and task.get("status") in terminal
            and not task.get("integrated")
        )
        if unintegrated:
            errors.append(f"unintegrated terminal tasks: {unintegrated}")
        logical: dict[str, list[dict[str, Any]]] = {}
        for task in tasks.values():
            if isinstance(task, dict):
                logical.setdefault(str(task.get("logical_id", "")), []).append(task)
        missing_required = sorted(
            logical_id
            for logical_id, attempts in logical.items()
            if any(attempt.get("required", True) for attempt in attempts)
            and not any(
                attempt.get("status") == "completed" and attempt.get("integrated")
                for attempt in attempts
            )
        )
        if missing_required:
            errors.append(
                f"required logical tasks without an integrated completion: {missing_required}"
            )
        scheduler = state.get("scheduler", {})
        if not isinstance(scheduler, dict):
            errors.append("scheduler state is invalid")
        else:
            if scheduler.get("integration_queue"):
                errors.append("integration queue is not empty")
            if scheduler.get("supervisor_activity") is not None:
                errors.append("supervisor material activity is still active")
        if state.get("pending"):
            errors.append("pending operations are not empty")
        if errors:
            raise ValueError("generic workflow cannot be finalized: " + "; ".join(errors))

    def run_manage(self, request: RunManageRequest) -> dict[str, Any]:
        with self.lock():
            if request.action == "start":
                if request.workflow == "gh-audit-repo" and self.current(request.workflow).is_dir():
                    previous = workflow_run.load_state(self.current(request.workflow))
                    history = previous.get("history", {})
                    if isinstance(history, dict) and history.get("publication_pending"):
                        if previous.get("status") in workflow_run.RESUMABLE:
                            raise ValueError("pending publication requires resume")
                        if not request.acknowledge_pending_publication:
                            raise ValueError(
                                "the previous terminal run has pending publication "
                                f"(candidate {history.get('candidate_id')}, "
                                f"operation {history.get('operation')}); "
                                "pass acknowledge_pending_publication to discard the "
                                "in-flight publication transaction and start a new run"
                            )
                inputs = {
                    "repository": request.repository,
                    "inputs": request.invocation(),
                }
                if request.workflow == "gh-audit-repo":
                    inputs = {**inputs, **self._source_and_worktree(request.source_confirmed)}
                    self._discard_stale_run(
                        request.workflow,
                        retained_worktree=Path(str(inputs["audit_worktree"])).resolve(),
                        acknowledged_publication=request.acknowledge_pending_publication,
                    )
                else:
                    self._discard_stale_run(request.workflow)
                    limit = request.invocation()["n"]
                    inputs["tasks"] = {}
                    inputs["scheduler"] = {
                        "limit": limit,
                        "integration_queue": [],
                        "supervisor_activity": None,
                    }
                    inputs["pending"] = []
                with self._json_file(inputs) as source:
                    self._invoke(
                        workflow_run.initialize, **self._base(request.workflow), input=source
                    )
            elif request.action == "resume":
                if request.n is not None:
                    state = self.state(request.workflow)
                    scheduler = self._scheduler_status(state)
                    if scheduler["running_workers"] > request.n:
                        raise ValueError(
                            "cannot lower concurrency below currently running worker count"
                        )
                self._invoke(workflow_run.resume, **self._base(request.workflow))
                if request.n is not None:
                    if request.workflow == "gh-audit-repo":
                        self._event(
                            {
                                "type": "directive-update",
                                "directive": {
                                    "concurrency": request.n,
                                    "kind": "resume-concurrency",
                                },
                            }
                        )
                    else:
                        self._set_generic_concurrency(request.workflow, request.n)
            elif request.action == "directive":
                self._event({"type": "directive-update", "directive": request.directive()})
            else:
                state = self.state(request.workflow)
                if request.action == "finish" and request.workflow != "gh-audit-repo":
                    self._validate_generic_terminal(state)
                status = {
                    "checkpoint": "in-progress",
                    "pause": "suspended",
                    "abort": "aborted",
                    "finish": "complete",
                }[request.action]
                terminal = request.action in {"abort", "finish"}
                update = (
                    {"pending": request.pending}
                    if request.action == "checkpoint"
                    and request.workflow != "gh-audit-repo"
                    and "pending" in request.model_fields_set
                    else None
                )
                with self._json_file(update) if update is not None else nullcontext() as source:
                    self._invoke(
                        lambda args: workflow_run.update_state(args, terminal=terminal),
                        **self._base(request.workflow),
                        expected_revision=state["revision"],
                        status=status,
                        input=source,
                        event=request.note or ("pending_updated" if update is not None else None),
                    )
            state = self.state(request.workflow)
            return self._receipt(
                request.workflow, state, True, next_actions=self._next_actions(state)
            )

    def _set_generic_concurrency(self, workflow: WorkflowName, limit: int) -> None:
        state = self.state(workflow)
        scheduler = dict(state.get("scheduler", {}))
        tasks = state.get("tasks", {})
        running = (
            sum(
                task.get("status") == "running" for task in tasks.values() if isinstance(task, dict)
            )
            if isinstance(tasks, dict)
            else 0
        )
        if running > limit:
            raise ValueError("cannot lower concurrency below currently running worker count")
        scheduler["limit"] = limit
        invocation = dict(state.get("inputs", {}))
        invocation["n"] = limit
        with self._json_file({"scheduler": scheduler, "inputs": invocation}) as source:
            self._invoke(
                lambda args: workflow_run.update_state(args, terminal=False),
                **self._base(workflow),
                expected_revision=state["revision"],
                status=state["status"],
                input=source,
                event="resume_concurrency_changed",
            )

    @classmethod
    def _next_actions(cls, state: dict[str, Any]) -> list[str]:
        if state.get("status") in workflow_run.TERMINAL:
            return []
        if state.get("status") in {"suspended", "partial"}:
            return ["resume"]
        scheduler = state.get("scheduler")
        if isinstance(scheduler, dict):
            return [cls._scheduler_status(state)["next_action"]]
        return []

    def run_status(self, workflow: WorkflowName) -> dict[str, Any]:
        state = self.state(workflow)
        raw_tasks = state.get("tasks", {})
        tasks = (
            {
                task_id: {**task, "task_ref": self._task_ref(workflow, state, task_id)}
                for task_id, task in raw_tasks.items()
                if isinstance(task, dict)
            }
            if isinstance(raw_tasks, dict)
            else {}
        )
        scheduler = self._scheduler_status(state)
        summary: dict[str, Any] = {
            "run_id": state.get("run_id"),
            "workflow": workflow,
            "status": state.get("status"),
            "revision": state.get("revision"),
            "inputs": state.get("inputs", {}),
            "pending": state.get("pending", []),
            "tasks": tasks,
            "scheduler": scheduler,
        }
        if workflow == "gh-audit-repo" and state.get("schema_version") == 2:
            finish_blockers = workflow_run.audit_finish_blockers(self.current(workflow), state)
            finish_ready = state.get("status") == "in-progress" and not finish_blockers
            allowed_actions = sorted(
                {
                    str(blocker["allowed_action"])
                    for blocker in finish_blockers
                    if blocker.get("allowed_action")
                }
            )
            if finish_ready:
                allowed_actions.append("finish")
            summary.update(
                {
                    "repository": state.get("repository"),
                    "branch": state.get("branch"),
                    "sha": state.get("sha"),
                    "upstream": state.get("upstream"),
                    "ahead": state.get("ahead"),
                    "behind": state.get("behind"),
                    "primary_worktree": state.get("primary_worktree"),
                    "audit_worktree": state.get("audit_worktree"),
                    "source_confirmed": state.get("source_confirmed"),
                    "confirmation_required": state.get("confirmation_required"),
                    "excluded_dirty_state": state.get("excluded_dirty_state"),
                    "finish_ready": finish_ready,
                    "finish_blockers": finish_blockers,
                    "allowed_actions": allowed_actions,
                    "phases": state.get("phases", {}),
                    "history": state.get("history", {}),
                    "inventory": state.get("inventory"),
                    "shards": state.get("shards", {}),
                    "candidates": state.get("candidates", {}),
                    "validations": state.get("validations", {}),
                    "verdicts": state.get("verdicts", {}),
                    "mutations": state.get("mutations", []),
                    "metrics": state.get("metrics", {}),
                    "head_drift": state.get("head_drift", {}),
                    "limitations": state.get("limitations", []),
                }
            )
        return summary

    def task_manage(self, request: TaskManageRequest) -> dict[str, Any]:
        request_task_id = getattr(request, "task_id", None)
        if request_task_id is not None and not SAFE_ID.fullmatch(request_task_id):
            raise ValueError("task_id contains unsupported characters")
        with self.lock():
            state = self.state(request.workflow)
            if request.workflow != "gh-audit-repo":
                return self._generic_task_manage(request, state)
            managed_task_id = request_task_id
            if request.action in {"plan", "retry"}:
                attempt = 1
                if request.action == "retry":
                    previous = state.get("tasks", {}).get(request.task_id)
                    if not isinstance(previous, dict):
                        raise ValueError("retry requires an existing task_id")
                    logical_id = str(previous["logical_id"])
                    attempt = (
                        max(
                            int(item.get("attempt", 1))
                            for item in state["tasks"].values()
                            if item.get("logical_id") == logical_id
                        )
                        + 1
                    )
                    task_id = f"{logical_id}-{attempt}"
                    plan = request.task
                    role = (
                        self._audit_task_role(plan, str(previous.get("role") or "worker"))
                        if plan
                        else str(previous.get("role") or "worker")
                    )
                    unit = plan.unit if plan else previous.get("unit") or logical_id
                    assignment = plan.assignment if plan else previous.get("assignment", {})
                    required = plan.required if plan else bool(previous.get("required", True))
                else:
                    plan = request.task
                    logical_id = plan.logical_id
                    queued = [
                        item
                        for item in state.get("tasks", {}).values()
                        if item.get("logical_id") == logical_id and item.get("status") == "queued"
                    ]
                    if queued:
                        role = self._audit_task_role(plan, str(queued[0].get("role") or "worker"))
                        revised = plan.model_dump(mode="json", exclude_none=True)
                        revised["role"] = role
                        revised["assignment"] = self._audit_task_assignment(
                            revised["assignment"], caller_supplied=True
                        )
                        self._event(
                            {
                                "type": "task-plan-update",
                                "task": revised,
                            }
                        )
                        managed_task_id = str(queued[0]["id"])
                        updated = self.state("gh-audit-repo")
                        return self._receipt(
                            "gh-audit-repo",
                            updated,
                            True,
                            **self._task_receipt_fields("gh-audit-repo", updated, managed_task_id),
                        )
                    if any(
                        item.get("logical_id") == logical_id
                        for item in state.get("tasks", {}).values()
                    ):
                        raise ValueError("logical task already has an attempt; use retry")
                    task_id = f"{logical_id}-1"
                    role = self._audit_task_role(plan)
                    unit = plan.unit or logical_id
                    assignment = plan.assignment
                    required = plan.required
                assignment = self._audit_task_assignment(
                    assignment, caller_supplied=plan is not None
                )
                if not SAFE_ID.fullmatch(logical_id):
                    raise ValueError("logical_id contains unsupported characters")
                if not SAFE_ID.fullmatch(task_id):
                    raise ValueError("generated task_id contains unsupported characters")
                if task_id in state.get("tasks", {}):
                    raise ValueError(f"task already exists: {task_id}")
                task = {
                    "id": task_id,
                    "logical_id": logical_id,
                    "agent_id": task_id,
                    "role": role,
                    "unit": unit,
                    "attempt": attempt,
                    "status": "queued",
                    "required": required,
                    "assignment": assignment,
                }
                if request.action == "retry" and request.note:
                    task["retry_note"] = request.note
                    task["retry_from_attempt"] = int(previous.get("attempt", 1))
                self._event({"type": "task-register", "task": task})
                managed_task_id = task_id
            elif request.action in {"integration_begin", "integration_end"}:
                event = (
                    "integration-start"
                    if request.action == "integration_begin"
                    else "integration-complete"
                )
                self._event({"type": event, "task_id": request.task_id})
            else:
                status = {
                    "mark_running": "running",
                    "checkpoint": "checkpointed",
                    "complete": "completed",
                    "fail": "failed",
                    "abandon": "abandoned",
                }[request.action]
                payload: dict[str, Any] = {
                    "type": "task-transition",
                    "task_id": request.task_id,
                    "status": status,
                }
                artifact_path: Path | None = None
                if request.action in {"checkpoint", "complete"}:
                    task = state.get("tasks", {}).get(request.task_id)
                    if not isinstance(task, dict) or task.get("status") not in {
                        "running",
                        "checkpointed",
                    }:
                        raise ValueError("report requires a running or checkpointed task")
                    report_status = self._validate_audit_report(
                        task, request.report, request.action
                    )
                    artifact_path, artifact_ref = self._write_audit_task_report(
                        request.task_id,
                        request.report,
                        checkpoint=request.action == "checkpoint",
                    )
                    payload["checkpoint" if request.action == "checkpoint" else "result"] = (
                        artifact_ref
                    )
                    if report_status is not None:
                        payload["report_status"] = report_status
                if request.note:
                    payload["note"] = request.note
                try:
                    self._event(payload)
                except Exception:
                    if artifact_path is not None:
                        artifact_path.unlink(missing_ok=True)
                    raise
            updated = self.state("gh-audit-repo")
            return self._receipt(
                "gh-audit-repo",
                updated,
                True,
                **self._task_receipt_fields("gh-audit-repo", updated, managed_task_id),
            )

    def _generic_task_manage(
        self, request: TaskManageRequest, state: dict[str, Any]
    ) -> dict[str, Any]:
        if state.get("status") != "in-progress":
            raise ValueError("current generic workflow run is not active")
        tasks = state.setdefault("tasks", {})
        scheduler = state.setdefault("scheduler", {"limit": 3, "integration_queue": []})
        queue = scheduler.setdefault("integration_queue", [])
        managed_task_id = getattr(request, "task_id", None)
        if request.action in {"plan", "retry"}:
            if request.action == "retry":
                previous = tasks.get(request.task_id)
                if not isinstance(previous, dict) or previous.get("status") not in {
                    "completed",
                    "failed",
                    "abandoned",
                }:
                    raise ValueError("retry requires a terminal existing task")
                logical_id = str(previous["logical_id"])
                attempt = (
                    max(
                        int(item.get("attempt", 1))
                        for item in tasks.values()
                        if item.get("logical_id") == logical_id
                    )
                    + 1
                )
                task_id = f"{logical_id}-{attempt}"
                plan = request.task
                role = (plan.role or "worker") if plan else previous.get("role") or "worker"
                unit = plan.unit if plan else previous.get("unit") or logical_id
                assignment = plan.assignment if plan else previous.get("assignment", {})
                required = plan.required if plan else bool(previous.get("required", True))
            else:
                plan = request.task
                logical_id = plan.logical_id
                queued = [
                    item
                    for item in tasks.values()
                    if item.get("logical_id") == logical_id and item.get("status") == "queued"
                ]
                if queued:
                    task = queued[0]
                    task.update(
                        {
                            "role": plan.role or "worker",
                            "unit": plan.unit or logical_id,
                            "assignment": plan.assignment,
                            "required": plan.required,
                        }
                    )
                    managed_task_id = str(task["id"])
                    state["revision"] += 1
                    state["updated_at"] = workflow_run.utc_now()
                    workflow_run.write_state(self.current(request.workflow), state)
                    workflow_run.append_journal(
                        self.current(request.workflow),
                        "task_managed",
                        revision=state["revision"],
                        task_id=managed_task_id,
                        action=request.action,
                    )
                    return self._receipt(
                        request.workflow,
                        state,
                        True,
                        **self._task_receipt_fields(request.workflow, state, managed_task_id),
                    )
                if any(item.get("logical_id") == logical_id for item in tasks.values()):
                    raise ValueError("logical task already has an attempt; use retry")
                attempt = 1
                task_id = f"{logical_id}-1"
                role = plan.role or "worker"
                unit = plan.unit or logical_id
                assignment = plan.assignment
                required = plan.required
            if not SAFE_ID.fullmatch(logical_id):
                raise ValueError("logical_id contains unsupported characters")
            if not SAFE_ID.fullmatch(task_id):
                raise ValueError("generated task_id contains unsupported characters")
            if task_id in tasks:
                raise ValueError(f"task already exists: {task_id}")
            managed_task_id = task_id
            tasks[task_id] = {
                "id": task_id,
                "logical_id": logical_id,
                "agent_id": task_id,
                "role": role,
                "unit": unit,
                "attempt": attempt,
                "status": "queued",
                "required": required,
                "assignment": assignment,
                "integrated": False,
            }
            if request.action == "retry" and request.note:
                tasks[task_id]["retry_note"] = request.note
                tasks[task_id]["retry_from_attempt"] = int(previous.get("attempt", 1))
        elif request.action in {"integration_begin", "integration_end"}:
            task = tasks.get(request.task_id)
            if not isinstance(task, dict):
                raise ValueError("unknown task_id")
            if request.action == "integration_begin":
                if not queue or queue[0] != request.task_id:
                    raise ValueError("integration must process the oldest result first")
                if scheduler.get("supervisor_activity") is not None:
                    raise ValueError("another supervisor integration is already active")
                if (
                    self._generic_scheduler_status(state)["running_workers"] + 1
                    > scheduler["limit"]
                ):
                    raise ValueError(
                        "supervisor integration would exceed material-work concurrency"
                    )
                scheduler["supervisor_activity"] = {
                    "kind": "integration",
                    "task_id": request.task_id,
                }
            else:
                activity = scheduler.get("supervisor_activity")
                if not isinstance(activity, dict) or activity.get("task_id") != request.task_id:
                    raise ValueError("task is not being integrated")
                task["integrated"] = True
                queue.remove(request.task_id)
                scheduler["supervisor_activity"] = None
        else:
            task = tasks.get(request.task_id)
            if not isinstance(task, dict):
                raise ValueError("unknown task_id")
            status = {
                "mark_running": "running",
                "checkpoint": "checkpointed",
                "complete": "completed",
                "fail": "failed",
                "abandon": "abandoned",
            }[request.action]
            allowed = {
                "queued": {"running", "abandoned"},
                "running": {"checkpointed", "completed", "failed", "abandoned"},
                "checkpointed": {"running", "completed", "failed", "abandoned"},
            }
            if status not in allowed.get(str(task.get("status")), set()):
                raise ValueError(f"invalid task transition: {task.get('status')} -> {status}")
            if (
                request.action == "mark_running"
                and self._generic_scheduler_status(state)["worker_slots"] < 1
            ):
                raise ValueError("generic workflow concurrency is saturated")
            task["status"] = status
            if request.action in {"checkpoint", "complete"}:
                result_dir = self.current(request.workflow) / "results"
                result_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                suffix = ".checkpoint" if request.action == "checkpoint" else ""
                result = result_dir / f"{request.task_id}{suffix}.json"
                rendered = json.dumps(request.report, indent=2, sort_keys=True) + "\n"
                try:
                    with result.open("x", encoding="utf-8") as handle:
                        handle.write(rendered)
                    os.chmod(result, 0o600)
                except FileExistsError as error:
                    kind = "checkpoint" if request.action == "checkpoint" else "result"
                    raise ValueError(f"task {kind} artifact already exists") from error
                task["checkpoint" if request.action == "checkpoint" else "result"] = (
                    f"results/{result.name}"
                )
            if request.note:
                task["note"] = request.note
            if status in {"completed", "failed", "abandoned"} and request.task_id not in queue:
                queue.append(request.task_id)
        state["revision"] += 1
        state["updated_at"] = workflow_run.utc_now()
        workflow_run.write_state(self.current(request.workflow), state)
        workflow_run.append_journal(
            self.current(request.workflow),
            "task_managed",
            revision=state["revision"],
            task_id=managed_task_id,
            action=request.action,
        )
        return self._receipt(
            request.workflow,
            state,
            True,
            **self._task_receipt_fields(request.workflow, state, managed_task_id),
        )

    def _task_report(self, workflow: WorkflowName, reference: Any) -> dict[str, Any] | None:
        if not isinstance(reference, str):
            return None
        current = self.current(workflow).resolve()
        candidate = (
            (current / reference).resolve()
            if not Path(reference).is_absolute()
            else Path(reference).resolve()
        )
        try:
            candidate.relative_to(current)
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (ValueError, OSError, UnicodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _continuation_context(
        self, workflow: WorkflowName, state: dict[str, Any], task: dict[str, Any]
    ) -> dict[str, Any] | None:
        result: dict[str, Any] = {}
        retry_note = task.get("retry_note")
        retry_from_attempt = task.get("retry_from_attempt")
        if (
            isinstance(retry_note, str)
            and retry_note
            and isinstance(retry_from_attempt, int)
            and not isinstance(retry_from_attempt, bool)
            and retry_from_attempt > 0
        ):
            result["retry"] = {
                "from_attempt": retry_from_attempt,
                "note": retry_note,
            }
        attempts = sorted(
            (
                item
                for item in state.get("tasks", {}).values()
                if isinstance(item, dict)
                and item.get("logical_id") == task.get("logical_id")
                and int(item.get("attempt", 1)) <= int(task.get("attempt", 1))
            ),
            key=lambda item: int(item.get("attempt", 1)),
            reverse=True,
        )
        for attempt in attempts:
            report = self._task_report(workflow, attempt.get("checkpoint"))
            if report is not None:
                result.update({"attempt": attempt.get("attempt"), "report": report})
                return result
        return result or None

    def _audit_task_history(
        self, state: dict[str, Any], assignment: dict[str, Any]
    ) -> dict[str, Any]:
        history = state.get("history", {})
        if not isinstance(history, dict) or not history.get("full_history_complete"):
            raise ValueError("audit worker context requires complete committed GitHub history")
        repo = state.get("repository")
        if not isinstance(repo, str):
            raise ValueError("current run does not contain a repository name")
        database = github_cache.live_path(github_cache.repo_dir(self.project_dir, repo), "records")
        if not database.is_file():
            raise ValueError("committed GitHub history cache is missing")
        links: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for number in assignment.get("leads", []):
            if isinstance(number, int) and not isinstance(number, bool):
                seen.add(("issue", number))
                links.append({"kind": "issue", "number": number})
        for item in assignment.get("history_links", []):
            if not isinstance(item, dict):
                continue
            key = (item.get("kind"), item.get("number"))
            if key[0] in {"issue", "pull"} and isinstance(key[1], int) and key not in seen:
                seen.add(cast(tuple[str, int], key))
                links.append({"kind": key[0], "number": key[1]})
        if len(links) > 40:
            raise ValueError("audit assignments accept at most 40 explicit history links")
        terms = " ".join(
            value
            for value in (
                str(assignment.get("area", "")).removeprefix("area/").replace("-", " "),
                str(assignment.get("focus", "")),
            )
            if value
        )

        def query(query_terms: str) -> dict[str, Any]:
            with self._json_file(links) as linked:
                return self._invoke(
                    github_cache.query_records,
                    repo=repo,
                    project_dir=self.project_dir,
                    cache_root=self.project_dir,
                    db=database,
                    cutoff=None,
                    linked=linked if links else None,
                    terms=query_terms,
                    terms_file=None,
                    kind=None,
                    state="open",
                    limit=TASK_HISTORY_LIMIT,
                    output=None,
                )

        def projected(queried: dict[str, Any]) -> list[dict[str, Any]]:
            return [
                {
                    key: record[key]
                    for key in TASK_HISTORY_FIELDS
                    if isinstance(record, dict) and key in record
                }
                for record in queried.get("records", [])
                if isinstance(record, dict)
            ]

        relevant = query(terms) if links or terms else {"records": [], "has_more": False}
        records = projected(relevant)
        if links:
            by_key = {
                (record.get("kind"), record.get("number")): record
                for record in records
                if isinstance(record, dict)
            }
            linked_records = [
                by_key[(item["kind"], item["number"])]
                for item in links
                if (item["kind"], item["number"]) in by_key
            ]
            linked_keys = {(item["kind"], item["number"]) for item in links}
            records = linked_records + [
                record
                for record in records
                if (record.get("kind"), record.get("number")) not in linked_keys
            ]
        fallback = query("")
        selected_keys = {(record.get("kind"), record.get("number")) for record in records}
        records.extend(
            record
            for record in projected(fallback)
            if (record.get("kind"), record.get("number")) not in selected_keys
        )
        has_more = (
            bool(relevant.get("has_more"))
            or bool(fallback.get("has_more"))
            or len(records) > TASK_HISTORY_LIMIT
        )
        records = records[:TASK_HISTORY_LIMIT]
        return {
            "cache": {
                "generation": history.get("generation"),
                "record_count": history.get("record_count"),
                "complete": True,
            },
            "selection": {
                "record_count": len(records),
                "limit": TASK_HISTORY_LIMIT,
                "has_more": has_more,
                "records": records,
            },
        }

    @staticmethod
    def _worker_inventory(inventory: Any, assignment: dict[str, Any]) -> dict[str, Any] | None:
        """Return task-relevant environment facts without the full package inventory."""
        if not isinstance(inventory, dict):
            return None
        sources = inventory.get("sources")
        if not isinstance(sources, dict):
            sources = {}
        environment = sources.get("python_environment")
        if not isinstance(environment, dict):
            environment = {}
        packages = environment.get("packages")
        if not isinstance(packages, dict):
            packages = {}

        def normalized(name: str) -> str:
            return re.sub(r"[-_.]+", "-", name).lower()

        available = {
            normalized(str(name)): (str(name), version) for name, version in packages.items()
        }
        requested = assignment.get("python_packages", [])
        requested_names = (
            list(dict.fromkeys(item for item in requested if isinstance(item, str) and item))[:50]
            if isinstance(requested, list)
            else []
        )
        selected: dict[str, Any] = {}
        missing: list[str] = []
        for name in requested_names:
            found = available.get(normalized(name))
            if found is None:
                missing.append(name)
            else:
                selected[found[0]] = found[1]
        python_environment = {
            key: environment[key]
            for key in (
                "available",
                "source",
                "executable",
                "python",
                "interpreter_prefix",
                "stdlib_root",
            )
            if key in environment
        }
        python_environment.update(
            {
                "package_count": len(packages),
                "packages": selected,
                "missing_requested_packages": missing,
            }
        )
        return {
            "revision": inventory.get("revision"),
            "updated_at": inventory.get("updated_at"),
            "python_environment": python_environment,
            "repository_manifests": sources.get("repository_manifests", {}),
            "programs": sources.get("programs", {}),
            "declared": sources.get("declared", {}),
            "context": sources.get("context", {}),
            "requests": inventory.get("requests", {}),
        }

    @staticmethod
    def _worker_validation(
        state: dict[str, Any], assignment: dict[str, Any]
    ) -> dict[str, Any] | None:
        if assignment.get("mode") != "verify":
            return None
        candidate = assignment.get("candidate")
        candidate_id = candidate.get("id") if isinstance(candidate, dict) else None
        if not isinstance(candidate_id, str) or not candidate_id:
            return None
        validations = state.get("validations", {})
        if not isinstance(validations, dict):
            return None
        records = [
            {key: validation[key] for key in TASK_VALIDATION_FIELDS if key in validation}
            for validation in validations.values()
            if isinstance(validation, dict) and validation.get("candidate_id") == candidate_id
        ]
        return {
            "candidate_id": candidate_id,
            "record_count": len(records),
            "records": records,
        }

    def task_context(self, task_ref: str) -> dict[str, Any]:
        workflow, run_ref, task_id = self._parse_task_ref(task_ref)
        state = self.state(workflow)
        if not self._task_ref_matches(state, run_ref):
            raise ValueError("task_ref is stale; use the current value from task_manage")
        task = state.get("tasks", {}).get(task_id)
        if not isinstance(task, dict):
            raise ValueError("task_ref is unknown; use the exact value from task_manage")
        assignment = task.get("assignment", {})
        if workflow == "gh-audit-repo":
            assignment = self._audit_task_assignment(assignment, caller_supplied=False)
        source_kind = assignment.get(
            "source_kind", "program" if task.get("role") == "program" else "repository"
        )
        priorities = (
            [
                "domain skill",
                "specialized MCP",
                "Context7",
                "official documentation",
                "dependency source",
            ]
            if source_kind == "python-library"
            else [
                "bundled version-matched documentation",
                "official documentation",
                "Context7",
                "dependency source",
            ]
            if source_kind == "program"
            else ["repository source", "official documentation", "Context7"]
        )
        reference_root = (
            Path(__file__).resolve().parents[2] / "extensions" / "github-workflows" / "references"
        )
        references = {
            "runtime_policy": str(reference_root / "github-runtime-policy.md"),
            "issue_conventions": str(reference_root / "github-issue-conventions.md"),
        }
        if workflow == "gh-audit-repo":
            references["readonly_search"] = str(
                reference_root.parent / "hooks" / "readonly-search.py"
            )
        if workflow == "gh-implement-issue":
            references["pull_request_template"] = str(reference_root / "github-pr-template.md")
        result = {
            "task_ref": task_ref,
            "task_id": task_id,
            "workflow": workflow,
            "run_id": state.get("run_id"),
            "audit_sha": state.get("sha"),
            "audit_worktree": state.get("audit_worktree"),
            "repository": state.get("repository"),
            "task": {key: task[key] for key in TASK_METADATA_FIELDS if key in task},
            "assignment": assignment,
            "inventory": self._worker_inventory(state.get("inventory"), assignment),
            "documentation": {
                "source_priority": priorities,
                "context7_query_budget": 12,
                "supervisor_extension_queries": 5,
            },
            "references": references,
            "control_plane": {"user_messages_always_available": True},
        }
        continuation = self._continuation_context(workflow, state, task)
        if continuation is not None:
            result["continuation"] = continuation
        if workflow == "gh-audit-repo":
            result["audit_worktree_head"] = self._verified_audit_worktree_head(state)
            result["history"] = self._audit_task_history(state, assignment)
            validation = self._worker_validation(state, assignment)
            if validation is not None:
                result["validation"] = validation
        return result

    def history_manage(self, request: HistoryManageRequest) -> dict[str, Any]:
        with self.lock():
            state = self.state(request.workflow)
            if request.workflow == "gh-audit-repo":
                if state.get("status") != "in-progress":
                    raise ValueError("the current audit run is not active")
            repo = state.get("repository")
            if not isinstance(repo, str):
                raise ValueError("current run does not contain a repository name")
            run_id = str(state["run_id"])
            common = {"repo": repo, "project_dir": self.project_dir, "cache_root": self.project_dir}
            directory = github_cache.repo_dir(self.project_dir, repo)
            work_db = directory / "staging" / f"records-{run_id}.sqlite3"
            live_db = github_cache.live_path(directory, "records")

            def cache_summary(database: Path, source: str) -> dict[str, Any]:
                if not database.is_file():
                    return {
                        "cache_source": source,
                        "cache_exists": False,
                        "generation": 0,
                        "record_count": 0,
                        "full_history_complete": False,
                        "last_sync_at": None,
                        "default_sha": None,
                    }
                with github_cache.connect_readonly(database) as connection:
                    validated = github_cache.validate(connection, repo, "records")
                metadata = validated["metadata"]
                return {
                    "cache_source": source,
                    "cache_exists": True,
                    "generation": int(metadata.get("generation", "0")),
                    "record_count": int(validated["count"]),
                    "full_history_complete": metadata.get("full_history_complete") == "true",
                    "last_sync_at": metadata.get("last_sync_at") or None,
                    "default_sha": metadata.get("default_sha") or None,
                }

            def sync_receipt(history: dict[str, Any], status: str, **extra: Any) -> dict[str, Any]:
                if request.workflow == "gh-audit-repo":
                    self._event({"type": "history-sync", "status": status, "value": history})
                    updated = self.state(request.workflow)
                    history = updated["history"]
                else:
                    updated = state
                return self._receipt(
                    request.workflow,
                    updated,
                    True,
                    history=history,
                    **extra,
                )

            if request.action == "status":
                database = work_db if work_db.is_file() else live_db
                source = "staging" if work_db.is_file() else "committed"
                history = {
                    **state.get("history", {}),
                    **cache_summary(database, source),
                }
                return self._receipt(
                    request.workflow,
                    state,
                    False,
                    history=history,
                )
            if request.action == "prepare":
                result = self._invoke(
                    lambda args: github_cache.prepare_database(args, "records"),
                    **common,
                    run_id=run_id,
                    rebuild=False,
                    no_cache=False,
                )
                history = {
                    **state.get("history", {}),
                    **cache_summary(work_db, "staging"),
                    "sync_status": "prepared",
                    "base_generation": int(result["base_generation"]),
                }
                return sync_receipt(
                    history,
                    "in-progress",
                    mode=result.get("mode", "prepared"),
                )
            if request.action == "ingest":
                grouped: dict[str, list[dict[str, Any]]] = {"issue": [], "pull": []}
                if request.artifacts:
                    for kind in grouped:
                        paths = [item.path for item in request.artifacts if item.kind == kind]
                        if paths:
                            grouped[kind].extend(self._history_artifact_records(paths, kind))
                else:
                    for item in request.records:
                        grouped[item.kind].append(
                            item.model_dump(mode="json", exclude_none=True, exclude={"kind"})
                        )
                accepted = 0
                history = dict(state.get("history", {}))
                counts = dict(history.get("ingested", {}))
                for kind, raw_records in grouped.items():
                    if not raw_records:
                        continue
                    records = self._compact_history_records(raw_records)
                    with self._json_file({"records": records}) as source:
                        result = self._invoke(
                            github_cache.ingest_records,
                            **common,
                            db=work_db,
                            kind=kind,
                            input=source,
                            source=request.source,
                            fetched_at=request.fetched_at,
                        )
                    accepted += result.get("accepted", len(records))
                    counts[kind] = counts.get(kind, 0) + len(records)
                history.update(cache_summary(work_db, "staging"))
                history.update({"sync_status": "ingesting", "ingested": counts})
                return sync_receipt(
                    history,
                    "in-progress",
                    accepted=accepted,
                )
            if request.action == "abort":
                self._invoke(github_cache.abort_database, db=work_db)
                history = {
                    **state.get("history", {}),
                    **cache_summary(live_db, "committed"),
                    "sync_status": "aborted",
                }
                return sync_receipt(history, "pending")
            with github_cache.connect(work_db) as connection:
                metadata = github_cache.validate(connection, repo, "records")["metadata"]
            full_history_complete = request.full_history_complete
            if full_history_complete is None:
                full_history_complete = metadata.get("full_history_complete") == "true"
            result = self._invoke(
                lambda args: github_cache.commit_database(args, "records"),
                **common,
                run_id=run_id,
                db=work_db,
                base_generation=int(metadata["generation"]),
                synced_at=request.fetched_at or workflow_run.utc_now(),
                default_sha=str(state.get("sha") or "unknown"),
                full_history_complete=full_history_complete,
                repo_sha=None,
                keep_shas=5,
                retention_days=90,
            )
            history = {
                **state.get("history", {}),
                **cache_summary(live_db, "committed"),
                "sync_status": "committed",
            }
            return sync_receipt(
                history,
                "complete",
                generation=result.get("generation"),
            )

    def history_query(self, request: HistoryQueryRequest) -> dict[str, Any]:
        state = self.state(request.workflow)
        repo = state.get("repository")
        if not isinstance(repo, str):
            raise ValueError("current run does not contain a repository name")
        database = github_cache.live_path(github_cache.repo_dir(self.project_dir, repo), "records")
        if not database.is_file():
            raise ValueError(
                "committed GitHub history cache is missing; prepare and commit it first"
            )
        if not any((request.terms, request.kind, request.state, request.cutoff, request.linked)):
            raise ValueError(
                "history_query requires a record selector; use history_manage action status "
                "for cache metadata"
            )
        linked_records = [item.model_dump(mode="json") for item in request.linked]
        with self._json_file(linked_records) as linked:
            return self._invoke(
                github_cache.query_records,
                repo=repo,
                project_dir=self.project_dir,
                cache_root=self.project_dir,
                db=database,
                cutoff=request.cutoff,
                linked=linked if request.linked else None,
                terms=request.terms,
                terms_file=None,
                kind=request.kind,
                state=request.state,
                limit=request.limit,
                output=None,
            )

    def audit_inventory(self, request: InventoryRequest) -> dict[str, Any]:
        with self.lock():
            state, worktree, run_dir = self._audit_paths()
            common = {
                "project_root": self.workspace,
                "project_dir": self.project_dir,
                "audit_worktree": worktree,
                "run_dir": run_dir,
            }
            inventory = state.get("inventory")
            revision = inventory.get("revision") if isinstance(inventory, dict) else None
            if request.action == "initialize":
                return self._invoke(audit_inventory.initialize, **common)
            if request.action == "status":
                return self._invoke(audit_inventory.status, **common)
            if revision is None:
                raise ValueError("audit inventory has not been initialized")
            if request.action == "refresh":
                return self._invoke(audit_inventory.refresh, **common, expected_revision=revision)
            if request.action == "program":
                probes = [program.model_dump(mode="json") for program in request.programs]
                with self._json_file(probes) as source:
                    return self._invoke(
                        audit_inventory.inspect_programs,
                        **common,
                        input=source,
                        expected_revision=revision,
                    )
            payload = (
                request.facts
                if request.action == "record_declared"
                else request.fact.model_dump(mode="json", exclude_none=True)
            )
            with self._json_file(payload) as source:
                if request.action == "record_declared":
                    return self._invoke(
                        audit_inventory.record_facts,
                        **common,
                        input=source,
                        expected_revision=revision,
                    )
                request_id = (
                    request.request_id
                    or hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
                )
                return self._invoke(
                    audit_inventory.record_context,
                    **common,
                    input=source,
                    request_id=request_id,
                    expected_revision=revision,
                )

    def audit_knowledge(self, request: KnowledgeRequest) -> dict[str, Any]:
        with self.lock():
            state = self.state("gh-audit-repo")
            if request.action in {"reconcile", "update"} and state.get("status") != "in-progress":
                raise ValueError("the current audit run is not active")
            sha = str(state.get("sha") or "unknown")
            if request.action == "show":
                return self._invoke(
                    audit_knowledge.show,
                    project_dir=self.project_dir,
                    area=getattr(request, "area", None),
                )
            if request.action == "reconcile":
                area_values = [
                    item.model_dump(mode="json", exclude_none=True) for item in request.areas
                ]
                with self._json_file({"areas": area_values}) as areas:
                    return self._invoke(
                        audit_knowledge.reconcile,
                        project_dir=self.project_dir,
                        areas=areas,
                        repo_sha=sha,
                    )
            if not request.area:
                raise ValueError("knowledge update/context requires an area")
            if request.action == "context":
                with self._json_file(request.versions) as versions:
                    return self._invoke(
                        audit_knowledge.context,
                        project_dir=self.project_dir,
                        area=request.area,
                        versions=versions,
                    )
            root = audit_knowledge.knowledge_root(argparse.Namespace(project_dir=self.project_dir))
            document = audit_knowledge.parse_document(
                root / "areas" / f"{audit_knowledge.slug(request.area)}.md"
            )
            findings = [item.model_dump(mode="json") for item in request.findings]
            with self._json_file({"findings": findings}) as source:
                return self._invoke(
                    audit_knowledge.update,
                    project_dir=self.project_dir,
                    area=request.area,
                    input=source,
                    repo_sha=sha,
                    expected_revision=document["revision"],
                )

    def audit_probe(self, request: ProbeRequest) -> dict[str, Any]:
        with self.lock():
            state, worktree, run_dir = self._audit_paths()
            if request.candidate_id not in state.get("candidates", {}):
                raise ValueError("probe refers to an unknown candidate")
            values = {
                "project_root": self.workspace,
                "project_dir": self.project_dir,
                "audit_worktree": worktree,
                "run_dir": run_dir,
                "probe_id": request.probe_id,
                "pythonpath": None,
                "kind": request.kind,
                "selector": getattr(request, "selectors", None),
                "code": getattr(request, "code", None),
            }
            self._invoke(audit_probe.run_probe, allow_timeout=True, **values)
            artifact_path = run_dir / "validation" / request.probe_id / "result.json"
            try:
                artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise ValueError("probe did not produce a valid result artifact") from error
            if not isinstance(artifact, dict) or artifact.get("probe_id") != request.probe_id:
                raise ValueError("probe result artifact has an invalid identity")
            status = self._probe_validation_status(artifact)

            def bounded(name: str, limit: int) -> tuple[str, bool]:
                value = artifact.get(name, "")
                text = value if isinstance(value, str) else ""
                truncated = bool(artifact.get(f"{name.removesuffix('_excerpt')}_truncated"))
                return text[:limit], truncated or len(text) > limit

            persisted_stdout, persisted_stdout_truncated = bounded(
                "stdout_excerpt", TASK_VALIDATION_EXCERPT_BYTES
            )
            persisted_stderr, persisted_stderr_truncated = bounded(
                "stderr_excerpt", TASK_VALIDATION_EXCERPT_BYTES
            )
            self._event(
                {
                    "type": "candidate-upsert",
                    "candidate": {
                        "id": request.candidate_id,
                        "status": "validation-pending",
                    },
                }
            )
            artifact_ref = f"validation/{request.probe_id}/result.json"
            self._event(
                {
                    "type": "validation-record",
                    "validation": {
                        "id": request.probe_id,
                        "probe_id": request.probe_id,
                        "candidate_id": request.candidate_id,
                        "status": status,
                        "artifact": artifact_ref,
                        "returncode": artifact.get("returncode"),
                        "timed_out": bool(artifact.get("timed_out")),
                        "worktree_unchanged": bool(artifact.get("worktree_unchanged")),
                        "stdout_excerpt": persisted_stdout,
                        "stderr_excerpt": persisted_stderr,
                        "stdout_truncated": persisted_stdout_truncated,
                        "stderr_truncated": persisted_stderr_truncated,
                    },
                }
            )
            stdout, stdout_truncated = bounded("stdout_excerpt", 8 * 1024)
            stderr, stderr_truncated = bounded("stderr_excerpt", 8 * 1024)
            return {
                "probe_id": request.probe_id,
                "candidate_id": request.candidate_id,
                "status": status,
                "artifact": artifact_ref,
                "returncode": artifact.get("returncode"),
                "timed_out": bool(artifact.get("timed_out")),
                "worktree_unchanged": bool(artifact.get("worktree_unchanged")),
                "stdout_excerpt": stdout,
                "stderr_excerpt": stderr,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "validation_recorded": True,
            }

    def audit_record(self, request: AuditRecordRequest) -> dict[str, Any]:
        mapping = {
            "phase": ("phase-set", None),
            "shard": ("shard-upsert", "shard"),
            "candidate": ("candidate-upsert", "candidate"),
            "verdict": ("verdict-record", "verdict"),
            "limitation": ("limitation-add", None),
            "pending": ("pending-set", None),
            "head_drift": ("head-drift", None),
            "supervisor_start": ("supervisor-start", None),
            "supervisor_finish": ("supervisor-complete", None),
        }
        event_type, field = mapping[request.action]
        before = self.state("gh-audit-repo")
        operation: str | None = None
        if field:
            raw_value = getattr(request, field)
            value = raw_value.model_dump(mode="json", exclude_none=True)
            payload = {"type": event_type, field: value}
            collection = {"shard": "shards", "candidate": "candidates", "verdict": "verdicts"}[
                field
            ]
            identity = value["candidate_id"] if field == "verdict" else value["id"]
            registry = before.get(collection, {})
            existed = identity in registry or (
                field == "verdict"
                and any(
                    isinstance(record, dict) and record.get("candidate_id") == identity
                    for record in registry.values()
                )
            )
            operation = "updated" if existed else "created"
        elif request.action == "phase":
            phase = request.phase
            phase_value = phase.model_dump(mode="json", exclude={"name"}, exclude_none=True)
            payload = {
                "type": event_type,
                "phase": phase.name,
                "value": {**phase_value.pop("summary", {}), **phase_value},
            }
            operation = "updated"
        elif request.action == "limitation":
            payload = {"type": event_type, "limitation": request.limitation}
        elif request.action == "pending":
            payload = {"type": event_type, "pending": request.pending}
        elif request.action == "head_drift":
            payload = {"type": event_type, "value": request.head_drift}
        elif request.action == "supervisor_start":
            payload = {
                "type": event_type,
                **request.activity.model_dump(mode="json", exclude_none=True),
            }
        else:
            payload = {"type": event_type}
        with self.lock():
            result = self._event(payload)
            state = self.state("gh-audit-repo")
            return self._receipt(
                "gh-audit-repo",
                state,
                True,
                operation=operation,
                scheduler=result.get("scheduler"),
            )

    def audit_publish(self, request: PublishRequest) -> dict[str, Any]:
        with self.lock():
            state = self.state("gh-audit-repo")
            if state.get("status") != "in-progress":
                raise ValueError("the current audit run is not active")
            history = dict(state.get("history", {}))
            if request.action == "begin":
                if history.get("publication_pending"):
                    raise ValueError("another publication is already pending")
                if request.candidate_id not in state.get("candidates", {}):
                    raise ValueError("publication refers to an unknown candidate")
                history.update(
                    {
                        "publication_pending": True,
                        "candidate_id": request.candidate_id,
                        "operation": request.operation,
                    }
                )
                history.pop("mutation", None)
                self._event({"type": "history-set", "value": history})
            else:
                if not history.get("publication_pending"):
                    raise ValueError("no publication is pending")
                if history.get("candidate_id") != request.candidate_id:
                    raise ValueError("publication does not match the pending candidate")
                operation = history.get("operation", history.get("mutation"))
                if operation not in {"create", "update", "no-op", "close", "dry-run"}:
                    raise ValueError("pending publication has an unsupported operation")
                if request.action == "finish" and not request.receipt:
                    raise ValueError("finished publication requires a non-empty receipt")
                if request.action == "failed":
                    history.update(
                        {
                            "publication_pending": False,
                            "outcome": "failed",
                            "error": request.error,
                        }
                    )
                else:
                    history.update(
                        {
                            "publication_pending": request.action == "uncertain",
                            "outcome": request.action,
                            "receipt": request.receipt,
                        }
                    )
                history["operation"] = operation
                history.pop("mutation", None)
                if request.action == "finish":
                    self._event(
                        {
                            "type": "publication-complete",
                            "value": history,
                            "mutation": {
                                "candidate_id": request.candidate_id,
                                "action": operation,
                                "receipt": request.receipt,
                            },
                        }
                    )
                else:
                    self._event({"type": "history-set", "value": history})
            updated = self.state("gh-audit-repo")
            return self._receipt("gh-audit-repo", updated, True, publication=updated.get("history"))

    def audit_metrics(self) -> dict[str, Any]:
        with self.lock():
            state = self.state("gh-audit-repo")
            summary = audit_metrics.summarize(
                self.project_dir, self.current("gh-audit-repo"), state
            )
            self._event({"type": "metrics-update", "value": summary})
            return summary
