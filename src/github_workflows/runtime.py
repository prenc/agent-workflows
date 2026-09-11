"""High-level, path-free facade over the reviewed workflow domain modules."""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import hashlib
import hmac
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
GIT_TIMEOUT_SECONDS = 10
HISTORY_ARTIFACT_TOTAL_BYTES = 25 * 1024 * 1024
TASK_HISTORY_LIMIT = 40
TASK_VALIDATION_LIMIT = 40
TASK_VALIDATION_EXCERPT_BYTES = 2 * 1024
TASK_INVENTORY_DEFAULT_PACKAGE_LIMIT = 100
TASK_INVENTORY_REQUESTED_PACKAGE_LIMIT = 50
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


def _extension_references_root() -> Path:
    """Resolve the extension references directory for this install layout."""
    package_file = Path(__file__).resolve()
    reference_root = package_file.parents[2] / "extensions" / "github-workflows" / "references"
    if not reference_root.is_dir():
        raise RuntimeError(
            "extension references root is missing: "
            f"{reference_root}; the package loaded from {package_file} is not an "
            "agent-workflows repository checkout, so the extension references "
            "cannot be resolved. Install the package editable from a checkout "
            "or run the workflow from a checkout so workers can read the "
            "reference documents."
        )
    return reference_root


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
    ) -> dict[str, Any]:
        """Record one bounded agent observation outside workflow state."""
        repository, workflow, run_id, task = self._feedback_attribution(request.task_ref)
        stored_provenance = dict(provenance or {})
        if task is not None:
            stored_provenance["task"] = task
        result = feedback.append(
            message=request.message,
            tool=request.tool,
            origin=None,
            repository=repository,
            workflow=workflow,
            run_id=run_id,
            provenance=stored_provenance,
            private_paths=self.feedback_private_paths(),
        )
        return {
            **result,
            "context_attached": bool(
                request.tool or task is not None or stored_provenance.get("conversation")
            ),
        }

    @staticmethod
    def _invoke(
        handler: Callable[[argparse.Namespace], Any],
        **values: Any,
    ) -> dict[str, Any]:
        output = StringIO()
        with redirect_stdout(output):
            result = handler(argparse.Namespace(**values))
        rendered = output.getvalue().strip()
        payload = json.loads(rendered) if rendered else {}
        if not isinstance(payload, dict):
            raise ValueError("workflow operation returned a non-object response")
        if isinstance(result, int) and result not in {0, payload.get("returncode")}:
            raise RuntimeError(f"workflow operation failed with exit code {result}")
        return payload

    @staticmethod
    def _probe_validation_status(result: dict[str, Any]) -> str:
        probe_status = result.get("probe_status")
        if probe_status in {"succeeded", "failed", "timed-out", "unavailable", "worktree-modified"}:
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
            except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
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
                raise ValueError(
                    "history ingest accepts at most 100 records per call after artifact expansion"
                )
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
        try:
            result = subprocess.run(
                ["git", "-C", worktree, "rev-parse", "--verify", "HEAD^{commit}"],
                check=False,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError(
                f"audit worktree HEAD verification timed out after {GIT_TIMEOUT_SECONDS} s"
            ) from error
        actual = result.stdout.strip()
        if result.returncode != 0 or not re.fullmatch(r"[0-9a-fA-F]{40}", actual):
            raise ValueError("audit worktree HEAD could not be verified")
        if actual.lower() != expected.lower():
            raise ValueError("audit worktree HEAD no longer matches audit SHA")
        return actual.lower()

    def _stale_audit_worktree(self, state: dict[str, Any], retained_worktree: Path) -> Path | None:
        raw_worktree = state.get("audit_worktree")
        if not isinstance(raw_worktree, str):
            return None
        worktree = Path(raw_worktree).resolve()
        managed_roots = {
            (self.workspace / ".worktrees").resolve(),
            self._cache_worktree_path().resolve(),
        }
        if worktree.parent not in managed_roots or not worktree.name.startswith("gh-audit-repo-"):
            raise ValueError("stale audit worktree is outside the managed location")
        if worktree == retained_worktree:
            return None
        return worktree if worktree.exists() else None

    def _discard_stale_run(
        self,
        workflow: WorkflowName,
        *,
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
                prefix = f"gh-audit-pr-{self._run_ref(run_id)}-"
                for root in (
                    self.workspace / ".worktrees",
                    self._cache_worktree_path(),
                ):
                    if root.is_dir():
                        for worktree in root.iterdir():
                            if (
                                worktree.name.startswith(prefix)
                                and not worktree.is_symlink()
                                and worktree.is_dir()
                            ):
                                try:
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
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE,
                                        text=True,
                                        timeout=GIT_TIMEOUT_SECONDS,
                                    )
                                except subprocess.TimeoutExpired as error:
                                    raise ValueError(
                                        "stale probe worktree removal timed out "
                                        f"after {GIT_TIMEOUT_SECONDS} s"
                                    ) from error
                repo = state.get("repository")
                if isinstance(repo, str) and repo:
                    try:
                        staging = github_cache.repo_dir(self.project_dir, repo) / "staging"
                    except ValueError:
                        staging = None
                    if staging is not None:
                        (staging / f"records-{run_id}.sqlite3").unlink(missing_ok=True)
        shutil.rmtree(current)

    @staticmethod
    def _ensure_private_directory(path: Path) -> Path:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise PermissionError("managed worktree root must be an owned directory")
        path.chmod(0o700)
        return path.resolve()

    def _cache_worktree_path(self) -> Path:
        cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))).expanduser()
        if not cache.is_absolute():
            raise ValueError("XDG_CACHE_HOME must be an absolute path")
        identity = hashlib.sha256(str(self.workspace).encode()).hexdigest()[:16]
        return cache / "agent-workflows" / "worktrees" / identity

    def _cache_worktree_root(self) -> Path:
        cache_root = self._cache_worktree_path()
        cache = cache_root.parents[2]
        application = self._ensure_private_directory(cache / "agent-workflows")
        worktrees = self._ensure_private_directory(application / "worktrees")
        return self._ensure_private_directory(worktrees / cache_root.name)

    def _ensure_local_worktree_ignored(self) -> None:
        probe = ".worktrees/probe"
        try:
            ignored = subprocess.run(
                ["git", "-C", str(self.workspace), "check-ignore", "-q", "--no-index", probe],
                check=False,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError(
                f"worktree root check-ignore timed out after {GIT_TIMEOUT_SECONDS} s"
            ) from error
        if ignored.returncode == 0:
            return
        if ignored.returncode != 1:
            raise ValueError("Git could not determine whether .worktrees is ignored")
        try:
            exclude_result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.workspace),
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-common-dir",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError(
                f"Git common directory rev-parse timed out after {GIT_TIMEOUT_SECONDS} s"
            ) from error
        exclude_value = exclude_result.stdout.strip()
        if exclude_result.returncode != 0 or not exclude_value:
            raise ValueError("Git could not resolve its private info exclude file")
        common = Path(exclude_value)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            common_descriptor = os.open(common, directory_flags)
        except OSError as error:
            raise PermissionError("Git common directory must be an owned directory") from error
        try:
            common_metadata = os.fstat(common_descriptor)
            if not stat.S_ISDIR(common_metadata.st_mode) or common_metadata.st_uid != os.geteuid():
                raise PermissionError("Git common directory must be an owned directory")
            try:
                info_descriptor = os.open("info", directory_flags, dir_fd=common_descriptor)
            except OSError as error:
                raise PermissionError("Git info directory must be an owned directory") from error
            try:
                info_metadata = os.fstat(info_descriptor)
                if not stat.S_ISDIR(info_metadata.st_mode) or info_metadata.st_uid != os.geteuid():
                    raise PermissionError("Git info directory must be an owned directory")
                try:
                    exclude_descriptor = os.open(
                        "exclude",
                        os.O_APPEND | os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=info_descriptor,
                    )
                except OSError as error:
                    raise PermissionError(
                        "Git info exclude must be an owned regular file"
                    ) from error
                try:
                    exclude_metadata = os.fstat(exclude_descriptor)
                    if (
                        not stat.S_ISREG(exclude_metadata.st_mode)
                        or exclude_metadata.st_uid != os.geteuid()
                    ):
                        raise PermissionError("Git info exclude must be an owned regular file")
                    prefix = (
                        b"\n"
                        if exclude_metadata.st_size
                        and os.pread(exclude_descriptor, 1, exclude_metadata.st_size - 1) != b"\n"
                        else b""
                    )
                    os.write(exclude_descriptor, prefix + b".worktrees/\n")
                finally:
                    os.close(exclude_descriptor)
            finally:
                os.close(info_descriptor)
        finally:
            os.close(common_descriptor)
        try:
            verified = subprocess.run(
                ["git", "-C", str(self.workspace), "check-ignore", "-q", "--no-index", probe],
                check=False,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError(
                f"worktree exclude verification check-ignore timed out after {GIT_TIMEOUT_SECONDS} s"
            ) from error
        if verified.returncode != 0:
            raise ValueError("Git info exclude did not ignore .worktrees")

    def _worktree_root(self) -> Path:
        self._ensure_local_worktree_ignored()
        return self._ensure_private_directory(self.workspace / ".worktrees")

    def _source_and_worktree(self, confirmed_sha: str | None) -> dict[str, Any]:
        source = self._invoke(workflow_run.audit_source, project_root=self.workspace)
        source_sha = str(source["sha"])
        if confirmed_sha is not None and confirmed_sha != source_sha:
            raise ValueError("the audit source changed after confirmation; repeat source preflight")
        if source.get("confirmation_required") and confirmed_sha is None:
            raise ValueError(
                "the audit source is not main/master; obtain user confirmation and retry"
            )
        sha = source_sha
        worktree = self._worktree_root() / f"gh-audit-repo-{sha[:7]}"
        if worktree.exists():
            try:
                actual = subprocess.run(
                    ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=GIT_TIMEOUT_SECONDS,
                ).stdout.strip()
            except subprocess.TimeoutExpired as error:
                raise ValueError(
                    f"retained worktree HEAD verification timed out after {GIT_TIMEOUT_SECONDS} s"
                ) from error
            if actual != sha:
                raise ValueError("the retained audit worktree points to a different commit")
        else:
            worktree.parent.mkdir(parents=True, exist_ok=True)
            try:
                subprocess.run(
                    [
                        "git",
                        "-c",
                        "core.hooksPath=/dev/null",
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
                    timeout=GIT_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as error:
                raise ValueError(
                    f"audit worktree creation timed out after {GIT_TIMEOUT_SECONDS} s"
                ) from error
        project_venv = self.workspace / ".venv"
        worktree_venv = worktree / ".venv"
        if project_venv.is_dir() and not worktree_venv.exists():
            worktree_venv.symlink_to(project_venv, target_is_directory=True)
        return {
            **source,
            "audit_worktree": str(worktree),
            "source_confirmed": confirmed_sha is not None,
        }

    def _pull_probe_worktree(self, state: dict[str, Any], candidate: dict[str, Any]) -> Path:
        number = candidate.get("pull_number")
        expected = candidate.get("head_sha")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or not isinstance(expected, str)
            or not re.fullmatch(r"[0-9a-fA-F]{40}", expected)
        ):
            raise ValueError("pull-request probe candidate requires pull_number and full head_sha")
        try:
            remote = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.workspace),
                    "ls-remote",
                    "origin",
                    f"refs/pull/{number}/head",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError("pull-request head verification timed out") from error
        live = remote.stdout.split(maxsplit=1)[0].lower() if remote.stdout.strip() else ""
        if remote.returncode != 0 or live != expected.lower():
            raise ValueError("pull-request head changed or could not be verified")
        try:
            fetched = subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-C",
                    str(self.workspace),
                    "fetch",
                    "--no-tags",
                    "--no-write-fetch-head",
                    "--no-recurse-submodules",
                    "origin",
                    f"refs/pull/{number}/head",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError("pull-request head fetch timed out") from error
        if fetched.returncode != 0:
            raise ValueError("pull-request head could not be fetched for a probe")
        run_ref = self._run_ref(str(state["run_id"]))
        worktree = self._worktree_root() / (
            f"gh-audit-pr-{run_ref}-{number}-{expected[:7].lower()}"
        )
        if worktree.is_symlink():
            raise ValueError("pull-request probe worktree must not be a symlink")
        if not worktree.exists():
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-C",
                    str(self.workspace),
                    "worktree",
                    "add",
                    "--detach",
                    str(worktree),
                    expected,
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        metadata = worktree.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise PermissionError("pull-request probe worktree must be an owned directory")
        actual = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--verify", "HEAD^{commit}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        ).stdout.strip()
        if actual.lower() != expected.lower():
            raise ValueError("pull-request probe worktree does not match its captured head")
        project_venv = self.workspace / ".venv"
        if project_venv.is_dir() and not (worktree / ".venv").exists():
            (worktree / ".venv").symlink_to(project_venv, target_is_directory=True)
        return worktree

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
    def _same_canonical_json(left: Any, right: Any) -> bool:
        try:
            return json.dumps(
                left, allow_nan=False, sort_keys=True, separators=(",", ":")
            ) == json.dumps(right, allow_nan=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _validate_execution_blocked_retry(
        previous: dict[str, Any], invocation_id: str | None
    ) -> None:
        if previous.get("execution_blocked_requires_resume"):
            raise ValueError("execution-blocked tasks require the run to pause and resume")
        blocked_invocation = previous.get("execution_blocked_invocation")
        if blocked_invocation is None:
            return
        if invocation_id is None:
            raise ValueError("execution-blocked retry requires invocation context")
        if blocked_invocation == invocation_id:
            raise ValueError("execution-blocked tasks cannot be retried in the same invocation")

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
        if mode in {"discover", "reconcile", "verify"}:
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
    def _audit_history_links(cls, assignment: dict[str, Any]) -> list[dict[str, Any]]:
        links: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()

        def add(kind: str, number: Any, field: str) -> None:
            if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                raise ValueError(f"assignment.{field} must contain positive integers")
            key = (kind, number)
            if key not in seen:
                seen.add(key)
                links.append({"kind": kind, "number": number})

        for field, kind in (
            ("leads", "issue"),
            ("history_issues", "issue"),
            ("history_pulls", "pull"),
        ):
            values = assignment.get(field, [])
            if not isinstance(values, list):
                raise ValueError(f"assignment.{field} must be an array")
            for number in values:
                add(kind, number, field)
        values = assignment.get("history_links", [])
        if not isinstance(values, list):
            raise ValueError("assignment.history_links must be an array")
        for item in values:
            if not isinstance(item, dict) or set(item) != {"kind", "number"}:
                raise ValueError("assignment.history_links entries require only kind and number")
            kind = item.get("kind")
            if kind not in {"issue", "pull"}:
                raise ValueError("assignment.history_links kind must be issue or pull")
            add(str(kind), item.get("number"), "history_links")
        if len(links) > TASK_HISTORY_LIMIT:
            raise ValueError(
                f"audit assignments accept at most {TASK_HISTORY_LIMIT} explicit history links"
            )
        return links

    @classmethod
    def _audit_task_assignment(
        cls,
        assignment: dict[str, Any],
        *,
        caller_supplied: bool,
    ) -> dict[str, Any]:
        links = cls._audit_history_links(assignment)
        if assignment.get("mode") == "reconcile" and not links:
            raise ValueError("reconcile assignment requires at least one history link")
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

    @staticmethod
    def _non_blank(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"assignment.{field} must be a non-empty string")
        return value.strip()

    def _curation_assigned_artifacts(
        self, assignment: dict[str, Any], run_dir: Path | None = None
    ) -> dict[str, Path]:
        paths = {
            field: Path(self._non_blank(assignment.get(field), field))
            for field in ("candidate_bundle", "issue_snapshot")
        }
        if any(path.is_absolute() or ".." in path.parts for path in paths.values()):
            raise ValueError("curation artifact paths must be run-relative")
        run_root = run_dir or self.current("gh-curate-issues")
        artifacts = (run_root / "artifacts").resolve()
        resolved: dict[str, Path] = {}
        for field, path in paths.items():
            unresolved = run_root / path
            if unresolved.is_symlink():
                raise ValueError(f"assignment.{field} must name a non-symlink file")
            candidate = unresolved.resolve()
            try:
                candidate.relative_to(artifacts)
            except ValueError as error:
                raise ValueError(f"assignment.{field} must be under run artifacts") from error
            if not candidate.is_file():
                raise ValueError(f"assignment.{field} must name an existing regular file")
            resolved[field] = candidate
        return resolved

    def _curation_bundle(self, assignment: dict[str, Any]) -> dict[str, Any]:
        issue = assignment.get("issue")
        if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
            raise ValueError("curation assignment.issue must be a positive integer")
        resolved = self._curation_assigned_artifacts(assignment)["candidate_bundle"]
        try:
            bundle = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
            raise ValueError("assignment.candidate_bundle must contain valid UTF-8 JSON") from error
        if not isinstance(bundle, dict):
            raise ValueError("candidate bundle must be a JSON object")
        selected = bundle.get("selected_issue")
        matches = bundle.get("matches")
        if (
            not isinstance(selected, dict)
            or selected.get("kind") != "issue"
            or selected.get("number") != issue
        ):
            raise ValueError("candidate bundle selected_issue must match assignment.issue")
        for field in ("state", "snapshot"):
            self._non_blank(selected.get(field), f"candidate_bundle.selected_issue.{field}")
        if selected["snapshot"] != assignment["issue_snapshot"]:
            raise ValueError("candidate bundle selected_issue snapshot must match assignment")
        if not isinstance(matches, list):
            raise ValueError("candidate bundle matches must be an array")
        seen: dict[tuple[str, int], str] = {("issue", issue): str(selected.get("state", ""))}
        for item in matches:
            if not isinstance(item, dict):
                raise ValueError("candidate bundle matches must contain objects")
            kind, number, state = item.get("kind"), item.get("number"), item.get("state")
            if (
                kind not in {"issue", "pull"}
                or isinstance(number, bool)
                or not isinstance(number, int)
                or number < 1
            ):
                raise ValueError("candidate bundle matches require kind and positive number")
            key = (kind, number)
            normalized_state = str(state or "")
            self._non_blank(state, "candidate_bundle.matches.state")
            self._non_blank(item.get("snapshot"), "candidate_bundle.matches.snapshot")
            if key in seen:
                detail = "contradictory " if seen[key] != normalized_state else "duplicate "
                raise ValueError(f"candidate bundle contains a {detail}{kind} #{number}")
            seen[key] = normalized_state
        if not isinstance(bundle.get("relationships"), (dict, list)):
            raise ValueError("candidate bundle relationships must be an object or array")
        for field in ("repository", "cutoff", "watermark"):
            self._non_blank(bundle.get(field), f"candidate_bundle.{field}")
        default_sha = self._non_blank(bundle.get("default_sha"), "candidate_bundle.default_sha")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", default_sha):
            raise ValueError("candidate bundle default_sha must be a full hexadecimal SHA")
        state = self.state("gh-curate-issues")
        if bundle["repository"] != state.get("repository"):
            raise ValueError("candidate bundle repository must match the current run")
        history = state.get("history")
        committed_sha = history.get("default_sha") if isinstance(history, dict) else None
        if committed_sha and default_sha != committed_sha:
            raise ValueError("candidate bundle default_sha must match committed history")
        return assignment

    def _generic_task_assignment(
        self, workflow: WorkflowName, assignment: dict[str, Any]
    ) -> dict[str, Any]:
        if workflow == "gh-curate-issues":
            for field in ("issue_snapshot",):
                self._non_blank(assignment.get(field), field)
            return self._curation_bundle(assignment)
        if workflow != "gh-implement-issue":
            return assignment
        issues = assignment.get("issues")
        if not isinstance(issues, list) or not issues:
            raise ValueError("implementation assignment.issues must be a non-empty array")
        seen: set[int] = set()
        for issue in issues:
            if not isinstance(issue, dict):
                raise ValueError("implementation assignment.issues must contain objects")
            number = issue.get("number")
            if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                raise ValueError("implementation issue number must be a positive integer")
            if number in seen:
                raise ValueError(f"implementation assignment repeats issue #{number}")
            seen.add(number)
            for field in ("snapshot", "accepted_scope"):
                self._non_blank(issue.get(field), f"issues.{field}")
        for field in (
            "worktree",
            "branch",
            "rebased_base_sha",
            "round_objective",
            "acceptance_condition",
        ):
            self._non_blank(assignment.get(field), field)
        sha = assignment["rebased_base_sha"]
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise ValueError("assignment.rebased_base_sha must be a full hexadecimal SHA")
        for field in ("pull_request", "remote_lease", "execution_environment"):
            if not isinstance(assignment.get(field), dict):
                raise ValueError(f"assignment.{field} must be an object")
        pull_state = self._non_blank(assignment["pull_request"].get("state"), "pull_request.state")
        if pull_state not in {"none", "open"}:
            raise ValueError("assignment.pull_request.state must be none or open")
        if pull_state == "none" and set(assignment["pull_request"]) != {"state"}:
            raise ValueError("assignment.pull_request for new work must contain only state=none")
        if pull_state == "open":
            pull_request = assignment["pull_request"]
            for field in ("initial_draft", "required_worker_draft"):
                if not isinstance(pull_request.get(field), bool):
                    raise ValueError(f"assignment.pull_request.{field} must be a boolean")
            round_mode = self._non_blank(
                pull_request.get("pr_round_mode"), "pull_request.pr_round_mode"
            )
            if round_mode not in {"implementation", "verification-only"}:
                raise ValueError(
                    "assignment.pull_request.pr_round_mode must be implementation or "
                    "verification-only"
                )
            expected_end_state = self._non_blank(
                pull_request.get("pr_expected_end_state"),
                "pull_request.pr_expected_end_state",
            )
            if expected_end_state not in {"draft", "unchanged"}:
                raise ValueError(
                    "assignment.pull_request.pr_expected_end_state must be draft or unchanged"
                )
            if round_mode == "implementation" and (
                not pull_request["required_worker_draft"] or expected_end_state != "draft"
            ):
                raise ValueError(
                    "implementation PR rounds require required_worker_draft=true and "
                    "pr_expected_end_state=draft"
                )
            if round_mode == "verification-only" and (
                pull_request["initial_draft"]
                or pull_request["required_worker_draft"]
                or expected_end_state != "unchanged"
            ):
                raise ValueError(
                    "verification-only PR rounds require initial_draft=false, "
                    "required_worker_draft=false, and pr_expected_end_state=unchanged"
                )
        self._non_blank(assignment["remote_lease"].get("state"), "remote_lease.state")
        environment = assignment["execution_environment"]
        if environment.get("mode") not in {"native", "shared", "isolated"}:
            raise ValueError(
                "assignment.execution_environment.mode must be native, shared, or isolated"
            )
        pythonpath = environment.get("pythonpath", [])
        if not isinstance(pythonpath, list) or any(
            not isinstance(item, str) or not item.strip() for item in pythonpath
        ):
            raise ValueError("assignment.execution_environment.pythonpath must be an array")
        if environment["mode"] == "shared":
            worktree = Path(assignment["worktree"])
            worktree = worktree if worktree.is_absolute() else self.workspace / worktree
            for value in pythonpath:
                relative = Path(value)
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or "\\" in value
                    or "\x00" in value
                    or value != value.strip()
                    or value != relative.as_posix()
                ):
                    raise ValueError(
                        "shared execution_environment.pythonpath entries must be normalized "
                        "project-relative paths"
                    )
                if worktree.is_dir():
                    candidate = (worktree / relative).resolve()
                    try:
                        candidate.relative_to(worktree.resolve())
                    except ValueError as error:
                        raise ValueError(
                            "shared execution_environment.pythonpath entries must remain "
                            "inside the assigned worktree"
                        ) from error
                    if not candidate.is_dir():
                        raise ValueError(
                            "shared execution_environment.pythonpath entries must exist under "
                            "the assigned worktree"
                        )
        instructions = assignment.get("repository_instructions")
        if (
            not isinstance(instructions, list)
            or not instructions
            or any(not isinstance(item, str) or not item.strip() for item in instructions)
        ):
            raise ValueError("assignment.repository_instructions must be a non-empty array")
        validation_plan = assignment.get("validation_plan")
        if not isinstance(validation_plan, list) or any(
            not isinstance(item, str) or not item.strip() for item in validation_plan
        ):
            raise ValueError("assignment.validation_plan must be an array of strings")
        return assignment

    @staticmethod
    def _validate_assignment_validations(state: dict[str, Any], assignment: dict[str, Any]) -> None:
        if "validation_ids" in assignment and assignment.get("mode") != "verify":
            raise ValueError("assignment.validation_ids is accepted only for verify tasks")
        identifiers = assignment.get("validation_ids", [])
        if not isinstance(identifiers, list) or any(
            not isinstance(item, str) or not item for item in identifiers
        ):
            raise ValueError("verify assignment.validation_ids must be an array of strings")
        if len(identifiers) > TASK_VALIDATION_LIMIT:
            raise ValueError(
                f"verify assignments accept at most {TASK_VALIDATION_LIMIT} validations"
            )
        known = state.get("validations", {})
        missing = [item for item in identifiers if not isinstance(known, dict) or item not in known]
        if missing:
            raise ValueError(f"verify assignment references unknown validation IDs: {missing}")

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
        if mode not in {"discover", "reconcile", "verify"}:
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
            return ["plan", "mark_running", "checkpoint", "complete", "fail", "abandon"]
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
        if state.get("status") == "suspended":
            allowed_actions = (
                ["checkpoint", "complete", "fail"]
                if task.get("status") in {"queued", "running", "checkpointed"}
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

    @classmethod
    def _validate_generic_terminal(cls, state: dict[str, Any]) -> None:
        errors, missing_required, _failed = workflow_run.generic_terminal_state(state)
        if missing_required:
            errors.append(
                f"required logical tasks without an integrated completion: {missing_required}"
            )
        if errors:
            raise ValueError("generic workflow cannot be finalized: " + "; ".join(errors))

    @classmethod
    def _blocked_terminal(cls, state: dict[str, Any], note: str | None) -> dict[str, Any]:
        return workflow_run.generic_blocked_terminal(state, note)

    def _start_run(self, request: RunManageRequest) -> dict[str, Any]:
        workflow = request.workflow
        with self.lock():
            current = self.current(workflow)
            previous = workflow_run.load_state(current) if current.is_dir() else None
            if workflow == "gh-audit-repo" and previous is not None:
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
            expected_revision = previous["revision"] if previous is not None else None
        # git worktree create/verify/remove and the audit source inspection run
        # outside the exclusive lock so concurrent operations are not blocked.
        inputs = {
            "repository": request.repository,
            "inputs": request.invocation(),
        }
        if workflow == "gh-audit-repo":
            inputs = {
                **inputs,
                **self._source_and_worktree(request.confirmed_source_sha),
            }
            if previous is not None:
                stale = self._stale_audit_worktree(
                    previous, Path(str(inputs["audit_worktree"])).resolve()
                )
                if stale is not None:
                    try:
                        subprocess.run(
                            [
                                "git",
                                "-C",
                                str(self.workspace),
                                "worktree",
                                "remove",
                                "--force",
                                str(stale),
                            ],
                            check=True,
                            capture_output=True,
                            text=True,
                            timeout=GIT_TIMEOUT_SECONDS,
                        )
                    except subprocess.TimeoutExpired as error:
                        raise ValueError(
                            f"stale worktree removal timed out after {GIT_TIMEOUT_SECONDS} s"
                        ) from error
        else:
            limit = request.invocation()["n"]
            inputs["tasks"] = {}
            inputs["scheduler"] = {
                "limit": limit,
                "integration_queue": [],
                "supervisor_activity": None,
            }
            inputs["pending"] = []
        with self.lock():
            current = self.current(workflow)
            fresh = workflow_run.load_state(current) if current.is_dir() else None
            # The cold case re-validates too: a run that appeared while the
            # source-and-worktree phase ran unlocked was initialized by a
            # concurrent start and must not be discarded and reinitialized.
            if previous is None:
                changed = fresh is not None
            else:
                changed = (
                    fresh is None
                    or fresh.get("run_id") != previous["run_id"]
                    or fresh.get("revision") != expected_revision
                )
            if changed:
                raise RuntimeError(
                    "workflow state changed while the run start was in flight; retry"
                )
            self._discard_stale_run(
                workflow, acknowledged_publication=request.acknowledge_pending_publication
            )
            with self._json_file(inputs) as source:
                self._invoke(workflow_run.initialize, **self._base(workflow), input=source)
            state = self.state(workflow)
            return self._receipt(workflow, state, True, next_actions=self._next_actions(state))

    def run_manage(self, request: RunManageRequest) -> dict[str, Any]:
        if request.action == "start":
            return self._start_run(request)
        with self.lock():
            if request.action == "resume":
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
                blocked_terminal: dict[str, Any] | None = None
                if request.action == "finish" and request.workflow != "gh-audit-repo":
                    if request.outcome == "blocked":
                        blocked_terminal = self._blocked_terminal(state, request.note)
                    else:
                        self._validate_generic_terminal(state)
                status = {
                    "checkpoint": "in-progress",
                    "pause": "suspended",
                    "abort": "aborted",
                    "finish": "blocked" if blocked_terminal is not None else "complete",
                }[request.action]
                terminal = request.action in {"abort", "finish"}
                update = (
                    {"pending": request.pending}
                    if request.action == "checkpoint"
                    and request.workflow != "gh-audit-repo"
                    and "pending" in request.model_fields_set
                    else None
                )
                if blocked_terminal is not None:
                    update = {"terminal": blocked_terminal}
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
        if isinstance(state.get("terminal"), dict):
            summary["terminal"] = state["terminal"]
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

    def task_manage(
        self, request: TaskManageRequest, *, invocation_id: str | None = None
    ) -> dict[str, Any]:
        request_task_id = getattr(request, "task_id", None)
        if request_task_id is not None and not SAFE_ID.fullmatch(request_task_id):
            raise ValueError("task_id contains unsupported characters")
        with self.lock():
            state = self.state(request.workflow)
            if request.workflow != "gh-audit-repo":
                return self._generic_task_manage(request, state, invocation_id=invocation_id)
            managed_task_id = request_task_id
            current_task = state.get("tasks", {}).get(request_task_id)

            def unchanged() -> dict[str, Any]:
                current = self.state("gh-audit-repo")
                return self._receipt(
                    "gh-audit-repo",
                    current,
                    False,
                    **self._task_receipt_fields(
                        "gh-audit-repo", current, cast(str, managed_task_id)
                    ),
                )

            if isinstance(current_task, dict):
                current_status = current_task.get("status")
                if request.action == "mark_running" and current_status == "running":
                    return unchanged()
                if (
                    request.action in {"checkpoint", "complete"}
                    and current_status
                    == {
                        "checkpoint": "checkpointed",
                        "complete": "completed",
                    }[request.action]
                ):
                    reference = current_task.get(
                        "checkpoint" if request.action == "checkpoint" else "result"
                    )
                    if self._same_canonical_json(
                        self._task_report("gh-audit-repo", reference), request.report
                    ):
                        return unchanged()
                    raise ValueError(f"conflicting repeated task {request.action} report")
                if (
                    request.action in {"fail", "abandon"}
                    and current_status
                    == {
                        "fail": "failed",
                        "abandon": "abandoned",
                    }[request.action]
                ):
                    if current_task.get("note") == request.note:
                        return unchanged()
                    raise ValueError(f"conflicting repeated task {request.action}")
                if request.action == "integration_begin":
                    activity = state.get("scheduler", {}).get("supervisor_activity")
                    if (
                        isinstance(activity, dict)
                        and activity.get("kind") == "integration"
                        and activity.get("task_id") == request_task_id
                    ):
                        return unchanged()
                if request.action == "integration_end" and current_task.get("integrated"):
                    return unchanged()
            if request.action in {"plan", "retry"}:
                attempt = 1
                if request.action == "retry":
                    previous = state.get("tasks", {}).get(request.task_id)
                    if not isinstance(previous, dict):
                        raise ValueError("retry requires an existing task_id")
                    self._validate_execution_blocked_retry(previous, invocation_id)
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
                        self._validate_assignment_validations(state, revised["assignment"])
                        managed_task_id = str(queued[0]["id"])
                        compared = ("role", "unit", "assignment", "required")
                        if all(
                            name not in revised or queued[0].get(name) == revised[name]
                            for name in compared
                        ):
                            return unchanged()
                        self._event(
                            {
                                "type": "task-plan-update",
                                "task": revised,
                            }
                        )
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
                self._validate_assignment_validations(state, assignment)
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
                task = state.get("tasks", {}).get(request.task_id)
                if (
                    isinstance(task, dict)
                    and task.get("status") == "queued"
                    and request.action
                    in {
                        "checkpoint",
                        "complete",
                        "fail",
                    }
                ):
                    payload["start_recovered"] = True
                artifact_path: Path | None = None
                if request.action in {"checkpoint", "complete"}:
                    if not isinstance(task, dict) or task.get("status") not in {
                        "queued",
                        "running",
                        "checkpointed",
                    }:
                        raise ValueError("report requires an accepted worker task")
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
                if request.action == "fail" and request.note == "execution-blocked":
                    if invocation_id is None:
                        payload["execution_blocked_requires_resume"] = True
                    else:
                        payload["execution_blocked_invocation"] = invocation_id
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
        self,
        request: TaskManageRequest,
        state: dict[str, Any],
        *,
        invocation_id: str | None = None,
    ) -> dict[str, Any]:
        run_status = state.get("status")
        late_result = request.action in {"checkpoint", "complete", "fail"}
        if run_status != "in-progress" and not (run_status == "suspended" and late_result):
            raise ValueError("current generic workflow run is not active")
        tasks = state.setdefault("tasks", {})
        scheduler = state.setdefault("scheduler", {"limit": 3, "integration_queue": []})
        queue = scheduler.setdefault("integration_queue", [])
        managed_task_id = getattr(request, "task_id", None)

        def unchanged() -> dict[str, Any]:
            return self._receipt(
                request.workflow,
                state,
                False,
                **self._task_receipt_fields(request.workflow, state, cast(str, managed_task_id)),
            )

        current_task = tasks.get(managed_task_id)
        if isinstance(current_task, dict):
            current_status = current_task.get("status")
            if request.action == "mark_running" and current_status == "running":
                return unchanged()
            if (
                request.action in {"checkpoint", "complete"}
                and current_status
                == {
                    "checkpoint": "checkpointed",
                    "complete": "completed",
                }[request.action]
            ):
                reference = current_task.get(
                    "checkpoint" if request.action == "checkpoint" else "result"
                )
                if self._same_canonical_json(
                    self._task_report(request.workflow, reference), request.report
                ):
                    return unchanged()
                raise ValueError(f"conflicting repeated task {request.action} report")
            if (
                request.action in {"fail", "abandon"}
                and current_status
                == {
                    "fail": "failed",
                    "abandon": "abandoned",
                }[request.action]
            ):
                if current_task.get("note") == request.note:
                    return unchanged()
                raise ValueError(f"conflicting repeated task {request.action}")
            if request.action == "integration_begin":
                activity = scheduler.get("supervisor_activity")
                if isinstance(activity, dict) and activity.get("task_id") == managed_task_id:
                    return unchanged()
            if request.action == "integration_end" and current_task.get("integrated"):
                return unchanged()
        if request.action in {"plan", "retry"}:
            if request.action == "retry":
                previous = tasks.get(request.task_id)
                if not isinstance(previous, dict) or previous.get("status") not in {
                    "completed",
                    "failed",
                    "abandoned",
                }:
                    raise ValueError("retry requires a terminal existing task")
                self._validate_execution_blocked_retry(previous, invocation_id)
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
                    assignment = self._generic_task_assignment(request.workflow, plan.assignment)
                    task = queued[0]
                    managed_task_id = str(task["id"])
                    revised = {
                        "role": plan.role or "worker",
                        "unit": plan.unit or logical_id,
                        "assignment": assignment,
                        "required": plan.required,
                    }
                    if all(task.get(name) == value for name, value in revised.items()):
                        return unchanged()
                    task.update(revised)
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
            assignment = self._generic_task_assignment(request.workflow, assignment)
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
                "queued": {"running", "checkpointed", "completed", "failed", "abandoned"},
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
            if request.action in {"checkpoint", "complete", "fail"} and current_status == "queued":
                task["start_recovered_at"] = workflow_run.utc_now()
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
            if request.action == "fail" and request.note == "execution-blocked":
                if invocation_id is None:
                    task["execution_blocked_requires_resume"] = True
                else:
                    task["execution_blocked_invocation"] = invocation_id
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

    @staticmethod
    def _history_cursor(payload: dict[str, Any]) -> str:
        task_ref = str(payload["task_ref"])
        _, run_ref, task_id = WorkflowRuntime._parse_task_ref(task_ref)
        task_hash = hashlib.blake2s(task_id.encode(), digest_size=6).hexdigest()
        query_hash = str(payload["query"])[:12]
        body = ".".join(
            (
                "hc1",
                run_ref,
                task_hash,
                str(payload["generation"]),
                query_hash,
                str(payload["offset"]),
            )
        )
        checksum = hashlib.blake2s(body.encode(), digest_size=6).hexdigest()
        return f"{body}.{checksum}"

    @staticmethod
    def _decode_history_cursor(cursor: str) -> dict[str, Any]:
        if cursor.startswith("hc1."):
            parts = cursor.split(".")
            if len(parts) != 7:
                raise ValueError("history_cursor is malformed or corrupt")
            version, run_ref, task_hash, generation, query_hash, offset, checksum = parts
            body = ".".join(parts[:-1])
            expected_checksum = hashlib.blake2s(body.encode(), digest_size=6).hexdigest()
            if not hmac.compare_digest(checksum, expected_checksum):
                raise ValueError("history_cursor is malformed or corrupt")
            if (
                version != "hc1"
                or not re.fullmatch(r"[0-9a-f]{12}", run_ref)
                or not re.fullmatch(r"[0-9a-f]{12}", task_hash)
                or not re.fullmatch(r"[0-9a-f]{12}", query_hash)
                or not generation.isdigit()
                or not offset.isdigit()
            ):
                raise ValueError("history_cursor is malformed or corrupt")
            return {
                "version": 1,
                "run_ref": run_ref,
                "task_hash": task_hash,
                "generation": int(generation),
                "query_hash": query_hash,
                "offset": int(offset),
            }
        try:
            padding = "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(cursor + padding))
        except (ValueError, UnicodeError, json.JSONDecodeError, binascii.Error) as error:
            raise ValueError("history_cursor is malformed") from error
        if not isinstance(value, dict):
            raise ValueError("history_cursor is malformed")
        return value

    def _audit_task_history(
        self,
        state: dict[str, Any],
        assignment: dict[str, Any],
        task_ref: str,
        history_cursor: str | None,
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
        links = self._audit_history_links(assignment)
        terms = " ".join(
            value
            for value in (
                str(assignment.get("area", "")).removeprefix("area/").replace("-", " "),
                str(assignment.get("focus", "")),
            )
            if value
        )
        generation = history.get("generation")
        query_fingerprint = hashlib.sha256(
            json.dumps(
                {"links": links, "state": "open", "terms": terms},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        offset = 0
        if history_cursor is not None:
            cursor = self._decode_history_cursor(history_cursor)
            if cursor.get("version") == 1:
                _, run_ref, task_id = self._parse_task_ref(task_ref)
                if cursor["run_ref"] != run_ref:
                    raise ValueError("history_cursor belongs to another run")
                task_hash = hashlib.blake2s(task_id.encode(), digest_size=6).hexdigest()
                if cursor["task_hash"] != task_hash:
                    raise ValueError("history_cursor belongs to another task")
                if cursor["generation"] != generation:
                    raise ValueError("history_cursor history generation has changed")
                if cursor["query_hash"] != query_fingerprint[:12]:
                    raise ValueError("history_cursor assignment has changed")
            else:
                if cursor.get("task_ref") != task_ref:
                    raise ValueError("history_cursor belongs to another task or run")
                if cursor.get("generation") != generation:
                    raise ValueError("history_cursor history generation has changed")
                if cursor.get("query") != query_fingerprint:
                    raise ValueError("history_cursor assignment has changed")
            offset = cursor.get("offset")
            if (
                isinstance(offset, bool)
                or not isinstance(offset, int)
                or offset <= 0
                or offset % TASK_HISTORY_LIMIT
                or offset > int(history.get("record_count") or 0)
            ):
                raise ValueError("history_cursor offset is invalid")

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

        with self._json_file(links) as linked:
            queried = self._invoke(
                github_cache.query_records,
                repo=repo,
                project_dir=self.project_dir,
                cache_root=self.project_dir,
                db=database,
                cutoff=None,
                linked=linked if links else None,
                terms=terms,
                terms_file=None,
                kind=None,
                state="open",
                limit=TASK_HISTORY_LIMIT,
                offset=offset,
                fill=True,
                output=None,
            )
        records = projected(queried)
        has_more = bool(queried.get("has_more"))
        next_cursor = (
            self._history_cursor(
                {
                    "task_ref": task_ref,
                    "generation": generation,
                    "query": query_fingerprint,
                    "offset": offset + len(records),
                }
            )
            if has_more
            else None
        )
        return {
            "cache": {
                "generation": generation,
                "record_count": history.get("record_count"),
                "complete": True,
                "last_sync_at": history.get("last_sync_at"),
            },
            "selection": {
                "record_count": len(records),
                "limit": TASK_HISTORY_LIMIT,
                "has_more": has_more,
                "records": records,
                "next_cursor": next_cursor,
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
            list(dict.fromkeys(item for item in requested if isinstance(item, str) and item))[
                :TASK_INVENTORY_REQUESTED_PACKAGE_LIMIT
            ]
            if isinstance(requested, list)
            else []
        )
        selected: dict[str, Any] = {}
        missing: list[str] = []
        if requested_names:
            for name in requested_names:
                found = available.get(normalized(name))
                if found is None:
                    missing.append(name)
                else:
                    selected[found[0]] = found[1]
            package_view = {
                "mode": "requested",
                "limit": TASK_INVENTORY_REQUESTED_PACKAGE_LIMIT,
                "returned_count": len(selected),
                "all_inventory_packages": len(selected) == len(packages),
            }
        else:
            selected = dict(
                sorted(packages.items(), key=lambda item: (normalized(str(item[0])), str(item[0])))[
                    :TASK_INVENTORY_DEFAULT_PACKAGE_LIMIT
                ]
            )
            package_view = {
                "mode": "bounded-default",
                "limit": TASK_INVENTORY_DEFAULT_PACKAGE_LIMIT,
                "returned_count": len(selected),
                "all_inventory_packages": len(selected) == len(packages),
            }
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
                "package_view": package_view,
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
        requested_ids = set(assignment.get("validation_ids", []))
        validations = state.get("validations", {})
        if not isinstance(validations, dict):
            return None
        records = [
            {key: validation[key] for key in TASK_VALIDATION_FIELDS if key in validation}
            for validation_id, validation in validations.items()
            if isinstance(validation, dict)
            and (validation.get("candidate_id") == candidate_id or validation_id in requested_ids)
        ]
        return {
            "candidate_id": candidate_id,
            "record_count": len(records),
            "records": records,
        }

    def _audit_task_knowledge(
        self, state: dict[str, Any], assignment: dict[str, Any]
    ) -> dict[str, Any]:
        area = assignment.get("area")
        if (not isinstance(area, str) or not area) and assignment.get("mode") == "verify":
            candidate = assignment.get("candidate")
            if isinstance(candidate, dict):
                area = candidate.get("area")
        requested: list[str] = []
        if isinstance(area, str) and area:
            audit_knowledge.slug(area)
            requested.append(area)
            if area != "area/shared-core":
                requested.append("area/shared-core")
        documents: list[dict[str, Any]] = []
        missing: list[str] = []
        areas_root = self.project_dir / "workflows" / "gh-audit-repo" / "knowledge" / "areas"
        if areas_root.is_symlink():
            raise ValueError("audit knowledge areas directory must not be a symlink")
        if not areas_root.is_dir():
            return {
                "requested_areas": requested,
                "documents": documents,
                "missing_areas": requested,
            }
        resolved_root = areas_root.resolve()
        for requested_area in requested:
            path = areas_root / f"{audit_knowledge.slug(requested_area)}.md"
            if not path.exists():
                missing.append(requested_area)
                continue
            if path.is_symlink():
                raise ValueError(f"knowledge document for {requested_area} must not be a symlink")
            resolved = path.resolve()
            try:
                resolved.relative_to(resolved_root)
            except ValueError as error:
                raise ValueError(
                    f"knowledge document for {requested_area} must remain under active areas"
                ) from error
            if not resolved.is_file():
                raise ValueError(f"knowledge document for {requested_area} must be a file")
            document = audit_knowledge.parse_document(resolved)
            document_area = document.get("area")
            actual_area = document_area.get("id") if isinstance(document_area, dict) else None
            if actual_area != requested_area:
                raise ValueError(
                    f"knowledge document for {requested_area} identifies area {actual_area!r}"
                )
            revision = document.get("revision")
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                raise ValueError(f"knowledge document for {requested_area} has invalid revision")
            source_sha = document.get("source_sha")
            if not isinstance(source_sha, str) or not source_sha:
                raise ValueError(f"knowledge document for {requested_area} has invalid source SHA")
            findings = document.get("findings", [])
            bootstrap_leads = document.get("bootstrap_leads", [])
            if not isinstance(findings, list) or not isinstance(bootstrap_leads, list):
                raise ValueError(f"knowledge document for {requested_area} has invalid content")
            documents.append(
                {
                    "area": requested_area,
                    "revision": revision,
                    "source_sha": source_sha,
                    "matches_audit_sha": source_sha == state.get("sha"),
                    "content": {
                        "area": document_area,
                        "findings": findings,
                        "bootstrap_leads": bootstrap_leads,
                    },
                }
            )
        return {
            "requested_areas": requested,
            "documents": documents,
            "missing_areas": missing,
        }

    def task_context(self, task_ref: str, history_cursor: str | None = None) -> dict[str, Any]:
        workflow, _, _ = self._parse_task_ref(task_ref)
        if workflow in {"gh-audit-repo", "gh-curate-issues"}:
            with self.lock():
                return self._task_context(task_ref, history_cursor)
        return self._task_context(task_ref, history_cursor)

    def _task_context(self, task_ref: str, history_cursor: str | None = None) -> dict[str, Any]:
        workflow, run_ref, task_id = self._parse_task_ref(task_ref)
        state = self.state(workflow)
        if not self._task_ref_matches(state, run_ref):
            raise ValueError("task_ref is stale; use the current value from task_manage")
        task = state.get("tasks", {}).get(task_id)
        if not isinstance(task, dict):
            raise ValueError("task_ref is unknown; use the exact value from task_manage")
        if history_cursor is not None and workflow != "gh-audit-repo":
            raise ValueError("history_cursor is supported only for audit worker context")
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
        reference_root = _extension_references_root()
        references = {
            "runtime_policy": str(reference_root / "github-runtime-policy.md"),
            "issue_conventions": str(reference_root / "github-issue-conventions.md"),
        }
        if workflow == "gh-audit-repo":
            references["rg_excludes"] = str(reference_root / "github-rg-excludes.ignore")
        if workflow == "gh-implement-issue":
            references["pull_request_template"] = str(reference_root / "github-pr-template.md")
        stored_history = state.get("history")
        default_sha = (
            stored_history.get("default_sha") if isinstance(stored_history, dict) else None
        )
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
            "run_context": {
                "repository": state.get("repository"),
                "default_sha": state.get("sha") or default_sha,
                "dry_run": bool(state.get("inputs", {}).get("dry_run", False)),
            },
        }
        if workflow == "gh-curate-issues":
            run_dir = state.get("run_dir")
            if not isinstance(run_dir, str) or not run_dir:
                raise ValueError("current curation run does not contain a run directory")
            result["run_context"]["assigned_artifacts"] = {
                field: str(path)
                for field, path in self._curation_assigned_artifacts(
                    assignment, Path(run_dir).resolve()
                ).items()
            }
        continuation = self._continuation_context(workflow, state, task)
        if continuation is not None:
            result["continuation"] = continuation
        if workflow == "gh-audit-repo":
            result["audit_worktree_head"] = self._verified_audit_worktree_head(state)
            result["knowledge"] = self._audit_task_knowledge(state, assignment)
            result["history"] = self._audit_task_history(
                state, assignment, task_ref, history_cursor
            )
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
                classified = github_cache.database_state(database, repo, "records")
                staging_fields = (
                    {
                        "staging_state": classified["staging_state"],
                        "recovery_action": classified["recovery_action"],
                    }
                    if source == "staging"
                    else {}
                )
                if classified["staging_state"] != "valid":
                    return {
                        "cache_source": source,
                        "cache_exists": False,
                        "generation": 0,
                        "record_count": 0,
                        "full_history_complete": False,
                        "last_sync_at": None,
                        "default_sha": None,
                        **staging_fields,
                    }
                validated = classified
                metadata = validated["metadata"]
                return {
                    "cache_source": source,
                    "cache_exists": True,
                    "generation": int(metadata.get("generation", "0")),
                    "record_count": int(validated["count"]),
                    "full_history_complete": metadata.get("full_history_complete") == "true",
                    "last_sync_at": metadata.get("last_sync_at") or None,
                    "default_sha": metadata.get("default_sha") or None,
                    **staging_fields,
                }

            def sync_receipt(history: dict[str, Any], status: str, **extra: Any) -> dict[str, Any]:
                if request.workflow == "gh-audit-repo":
                    self._event({"type": "history-sync", "status": status, "value": history})
                    updated = self.state(request.workflow)
                    history = updated["history"]
                else:
                    state["history"] = history
                    state["revision"] += 1
                    state["updated_at"] = workflow_run.utc_now()
                    workflow_run.write_state(self.current(request.workflow), state)
                    workflow_run.append_journal(
                        self.current(request.workflow),
                        "history_sync",
                        revision=state["revision"],
                        status=status,
                    )
                    updated = state
                return self._receipt(
                    request.workflow,
                    updated,
                    True,
                    history=history,
                    **extra,
                )

            if request.action == "status":
                staging = github_cache.database_state(work_db, repo, "records")
                use_staging = staging["staging_state"] == "valid"
                database = work_db if use_staging else live_db
                source = "staging" if use_staging else "committed"
                history = {
                    **state.get("history", {}),
                    **cache_summary(database, source),
                    "staging_state": staging["staging_state"],
                    "recovery_action": staging["recovery_action"],
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
                    expanded_count = sum(len(records) for records in grouped.values())
                    if expanded_count > 100:
                        raise ValueError(
                            "history ingest accepts at most 100 records per call after "
                            "artifact expansion"
                        )
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
                            run_id=run_id,
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
                self._invoke(github_cache.abort_database, **common, db=work_db)
                history = {
                    **state.get("history", {}),
                    **cache_summary(live_db, "committed"),
                    "sync_status": "aborted",
                }
                return sync_receipt(history, "pending")
            metadata = github_cache.require_valid_database(work_db, repo, "records")["metadata"]
            full_history_complete = request.full_history_complete
            if full_history_complete is None:
                full_history_complete = metadata.get("full_history_complete") == "true"
            default_sha = request.default_sha or metadata.get("default_sha")
            if not default_sha or default_sha == "unknown":
                raise ValueError(
                    "fresh history commit requires default_sha; copy the full immutable "
                    "default-branch SHA from the live repository read"
                )
            result = self._invoke(
                lambda args: github_cache.commit_database(args, "records"),
                **common,
                run_id=run_id,
                db=work_db,
                base_generation=int(metadata["generation"]),
                synced_at=request.fetched_at or workflow_run.utc_now(),
                default_sha=default_sha,
                full_history_complete=full_history_complete,
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

    def _audit_inventory_probed(self, request: InventoryRequest) -> dict[str, Any]:
        with self.lock():
            state, worktree, run_dir = self._audit_paths()
            inventory = state.get("inventory")
            revision = inventory.get("revision") if isinstance(inventory, dict) else None
            if request.action == "initialize":
                if "inventory" in state:
                    raise ValueError("environment inventory already exists")
            elif revision is None:
                raise ValueError("audit inventory has not been initialized")
            expected_revision = state["revision"]
        # The package and program probe subprocesses run outside the exclusive
        # lock; state is re-read and revision-checked under the lock before the
        # collected sources are persisted.
        if request.action == "initialize":
            collected = audit_inventory.build_initial_inventory(self.workspace, worktree)
        elif request.action == "refresh":
            collected = audit_inventory.refresh_sources(self.workspace, worktree)
        else:
            probes = [program.model_dump(mode="json") for program in request.programs]
            collected = audit_inventory.collect_program_facts(
                self.workspace, worktree, run_dir, probes
            )
        with self.lock():
            state = self.state("gh-audit-repo")
            if state["revision"] != expected_revision:
                raise RuntimeError(
                    "audit state changed while the inventory collection was in flight; "
                    f"expected revision {expected_revision}, found {state['revision']}"
                )
            if request.action == "initialize":
                return audit_inventory.commit_initial_inventory(run_dir, collected)
            if request.action == "refresh":
                return audit_inventory.commit_refresh(run_dir, collected, revision)
            facts, request_ids = collected
            return audit_inventory.commit_program_facts(run_dir, facts, request_ids, revision)

    def audit_inventory(self, request: InventoryRequest) -> dict[str, Any]:
        if request.action in {"initialize", "refresh", "program"}:
            return self._audit_inventory_probed(request)
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
            if request.action == "status":
                return self._invoke(audit_inventory.status, **common)
            if revision is None:
                raise ValueError("audit inventory has not been initialized")
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
                area_values = []
                for item in request.areas:
                    value = item.model_dump(mode="json", exclude_none=True)
                    if not item.title_supplied:
                        value.pop("title", None)
                    area_values.append(value)
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
            candidate = state.get("candidates", {}).get(request.candidate_id)
            if not isinstance(candidate, dict):
                raise ValueError("probe refers to an unknown candidate")
            expected_revision = state["revision"]
            candidate_status = candidate.get("status")
            if candidate_status in workflow_run.AUDIT_CANDIDATE_TERMINAL:
                raise ValueError(
                    f"candidate {request.candidate_id} is in terminal status "
                    f"{candidate_status}; probe refused before execution"
                )
            candidate = dict(candidate)
            probe_sha = state.get("sha")
            artifact_dir = run_dir / "validation" / request.probe_id
            values = {
                "project_root": self.workspace,
                "project_dir": self.project_dir,
                "audit_worktree": worktree,
                "run_dir": run_dir,
                "probe_id": request.probe_id,
                "pythonpath": request.pythonpath,
                "kind": request.kind,
                "selector": getattr(request, "selectors", None),
                "code": getattr(request, "code", None),
            }
        if candidate.get("artifact_kind") == "pull":
            worktree = self._pull_probe_worktree(state, candidate)
            probe_sha = candidate.get("head_sha")
            values["audit_worktree"] = worktree
        # The probe subprocess runs outside the exclusive lock; state is re-read
        # and revision-checked under the lock before the result is persisted.
        try:
            self._invoke(audit_probe.run_probe, **values)
            with self.lock():
                state, worktree, run_dir = self._audit_paths()
                if request.candidate_id not in state.get("candidates", {}):
                    raise ValueError("probe refers to an unknown candidate")
                if state["revision"] != expected_revision:
                    raise RuntimeError(
                        "audit state changed while the probe was in flight; "
                        f"expected revision {expected_revision}, found {state['revision']}"
                    )
                artifact_path = artifact_dir / "result.json"
                try:
                    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as error:
                    raise ValueError("probe did not produce a valid result artifact") from error
                if not isinstance(artifact, dict) or artifact.get("probe_id") != request.probe_id:
                    raise ValueError("probe result artifact has an invalid identity")
                if (
                    candidate.get("artifact_kind") == "pull"
                    and artifact.get("repo_sha") != probe_sha
                ):
                    raise ValueError("probe result does not match its immutable source SHA")
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
                artifact_ref = f"validation/{request.probe_id}/result.json"
                self._event(
                    {
                        "type": "candidate-upsert",
                        "candidate": {
                            "id": request.candidate_id,
                            "status": "validation-pending",
                        },
                    }
                )
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
                            "source_kind": (
                                "pull" if candidate.get("artifact_kind") == "pull" else "default"
                            ),
                            "source_sha": probe_sha,
                            "stdout_excerpt": persisted_stdout,
                            "stderr_excerpt": persisted_stderr,
                            "stdout_truncated": persisted_stdout_truncated,
                            "stderr_truncated": persisted_stderr_truncated,
                        },
                    }
                )
        except (RuntimeError, ValueError):
            # A rejected event would orphan the probe artifact against
            # state["validations"]; the directory is unique to this probe
            # id, so removing it restores the pre-probe layout.
            if artifact_dir.exists():
                shutil.rmtree(artifact_dir)
            raise
        if artifact.get("worktree_unchanged") is False:
            raise ValueError(
                f"probe {request.probe_id} modified the audit worktree; "
                f"outcome recorded at {artifact_ref}"
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
        operation: str | None = None
        if field:
            raw_value = getattr(request, field)
            value = raw_value.model_dump(mode="json", exclude_none=True)
            payload = {"type": event_type, field: value}
            collection = {"shard": "shards", "candidate": "candidates", "verdict": "verdicts"}[
                field
            ]
            identity = value["candidate_id"] if field == "verdict" else value["id"]
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
            # Derive the created/updated label from a state read under the lock
            # so it matches the merge actually applied by the event below.
            if field:
                before = self.state("gh-audit-repo")
                registry = before.get(collection, {})
                existed = identity in registry or (
                    field == "verdict"
                    and any(
                        isinstance(record, dict) and record.get("candidate_id") == identity
                        for record in registry.values()
                    )
                )
                operation = "updated" if existed else "created"
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
