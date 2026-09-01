from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from pydantic import ValidationError

from github_workflows import github_cache, workflow_run
from github_workflows.models import (
    AuditRecordRequest,
    HistoryManageRequest,
    HistoryQueryRequest,
    InventoryRequest,
    KnowledgeRequest,
    ProbeRequest,
    PublishRequest,
    RunManageRequest,
    TaskManageRequest,
)
from github_workflows.runtime import WorkflowRuntime

ROOT = Path(__file__).parents[1]
EXTENSION = ROOT / "extensions/github-workflows"


class TestRuntimeSafety:
    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            ({"probe_status": "unavailable"}, "unavailable"),
            ({"returncode": 0, "timed_out": False}, "succeeded"),
            ({"returncode": 7, "timed_out": False}, "failed"),
            ({"returncode": -15, "timed_out": True}, "timed-out"),
        ],
    )
    def test_probe_validation_status_reflects_execution(
        self, result: dict[str, object], expected: str
    ) -> None:
        assert WorkflowRuntime._probe_validation_status(result) == expected

    def make_runtime(self, root: Path) -> WorkflowRuntime:
        workspace = root / "repo"
        workspace.mkdir()
        return WorkflowRuntime(workspace, root / "qwen-project")

    def initialize_audit(self, runtime: WorkflowRuntime) -> None:
        inputs = {
            "repository": "example/repo",
            "inputs": {"n": 1},
            "audit_worktree": str(runtime.workspace),
            "primary_worktree": str(runtime.workspace),
            "branch": "main",
            "sha": "a" * 40,
            "source_confirmed": True,
            "confirmation_required": False,
            "excluded_dirty_state": {"dirty": False},
        }
        with runtime._json_file(inputs) as source:
            runtime._invoke(
                workflow_run.initialize,
                **runtime._base("gh-audit-repo"),
                input=source,
            )

    def test_fresh_start_cleanup_is_limited_to_previous_run_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-fresh-start-") as directory:
            runtime = self.make_runtime(Path(directory))
            self.initialize_audit(runtime)
            state = runtime.state("gh-audit-repo")
            managed_worktree = runtime.workspace / ".worktrees" / "gh-audit-repo-aaaaaaa"
            state["audit_worktree"] = str(managed_worktree)
            workflow_run.write_state(runtime.current("gh-audit-repo"), state)
            staging = runtime.project_dir / "github" / "staging"
            staging.mkdir(parents=True)
            transaction = staging / f"records-{state['run_id']}.sqlite3"
            transaction.write_text("stale", encoding="utf-8")
            unrelated = staging / "records-other-run.sqlite3"
            unrelated.write_text("keep", encoding="utf-8")
            knowledge = runtime.current("gh-audit-repo").parent / "knowledge" / "areas"
            knowledge.mkdir(parents=True)
            retained = knowledge / "core.md"
            retained.write_text("knowledge", encoding="utf-8")

            runtime._discard_stale_run("gh-audit-repo")

            assert not runtime.current("gh-audit-repo").exists()
            assert not transaction.exists()
            assert unrelated.is_file()
            assert retained.is_file()

    def test_fresh_start_preserves_ambiguous_publication_for_resume(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-pending-publication-") as directory:
            runtime = self.make_runtime(Path(directory))
            self.initialize_audit(runtime)
            state = runtime.state("gh-audit-repo")
            state["history"]["publication_pending"] = True
            workflow_run.write_state(runtime.current("gh-audit-repo"), state)

            with pytest.raises(ValueError, match="pending publication requires resume"):
                runtime._discard_stale_run("gh-audit-repo")

            assert runtime.current("gh-audit-repo").is_dir()

    def test_every_public_request_rejects_unknown_fields(self) -> None:
        cases = [
            (
                RunManageRequest,
                {"action": "start", "workflow": "gh-curate-issues", "repository": "x/y"},
            ),
            (TaskManageRequest, {"action": "plan", "task": {"logical_id": "task"}}),
            (HistoryManageRequest, {"action": "prepare"}),
            (HistoryQueryRequest, {}),
            (InventoryRequest, {"action": "status"}),
            (KnowledgeRequest, {"action": "show"}),
            (ProbeRequest, {"kind": "python", "probe_id": "probe-1", "code": "pass"}),
            (
                AuditRecordRequest,
                {"action": "phase", "phase": {"name": "structure", "status": "complete"}},
            ),
            (PublishRequest, {"action": "begin", "candidate_id": "C-1", "mutation": "create"}),
        ]
        for model, payload in cases:
            with pytest.raises(ValidationError):
                model.model_validate({**payload, "typo_field": True})

    def test_history_query_is_bounded_and_requires_a_selector(self) -> None:
        assert HistoryQueryRequest(state="open").limit == 25
        for limit in (0, 101):
            with pytest.raises(ValidationError):
                HistoryQueryRequest(state="open", limit=limit)

        with tempfile.TemporaryDirectory(prefix="history-selector-") as directory:
            root = Path(directory)
            runtime = self.make_runtime(root)
            workflow = "gh-curate-issues"
            runtime.run_manage(
                RunManageRequest(
                    action="start",
                    workflow=workflow,
                    repository="example/repo",
                )
            )
            runtime.history_manage(HistoryManageRequest(action="prepare", workflow=workflow))
            runtime.history_manage(
                HistoryManageRequest(
                    action="commit",
                    workflow=workflow,
                    full_history_complete=True,
                )
            )
            with pytest.raises(ValueError, match="requires a record selector"):
                runtime.history_query(HistoryQueryRequest(workflow=workflow))

    def test_history_ingest_requires_one_bounded_input(self) -> None:
        with pytest.raises(ValidationError, match="exactly one"):
            HistoryManageRequest(action="ingest")
        with pytest.raises(ValidationError, match="exactly one"):
            HistoryManageRequest(
                action="ingest",
                records=[{"kind": "issue", "number": 1}],
                artifacts=[{"kind": "issue", "path": "artifact.json"}],
            )
        with pytest.raises(ValidationError):
            HistoryManageRequest(
                action="ingest",
                records=[{"kind": "issue", "number": number} for number in range(101)],
            )

    def test_history_artifacts_are_bounded_qwen_tool_results(self) -> None:
        with tempfile.TemporaryDirectory(prefix="history-artifacts-") as directory:
            root = Path(directory)
            qwen_home = root / "qwen-home"
            tool_results = qwen_home / "tmp" / "session" / "tool-results"
            tool_results.mkdir(parents=True)

            issue_page = tool_results / "issues.txt"
            issue_page.write_text('{"issues":[{"number":1}]}', encoding="utf-8")
            issue_page.chmod(0o600)
            record_page = tool_results / "records.txt"
            record_page.write_text('[{"number":2}]', encoding="utf-8")
            record_page.chmod(0o600)
            with mock.patch.dict("os.environ", {"QWEN_HOME": str(qwen_home)}):
                records = WorkflowRuntime._history_artifact_records(
                    [str(issue_page), str(record_page)],
                    "issue",
                )
                assert [item["number"] for item in records] == [1, 2]

                pull_page = tool_results / "pulls.txt"
                pull_page.write_text('{"pullRequests":[]}', encoding="utf-8")
                pull_page.chmod(0o600)
                assert WorkflowRuntime._history_artifact_records([str(pull_page)], "pull") == []
                with pytest.raises(ValueError, match="does not match"):
                    WorkflowRuntime._history_artifact_records([str(pull_page)], "issue")

                malformed = tool_results / "malformed.txt"
                malformed.write_text("{", encoding="utf-8")
                malformed.chmod(0o600)
                with pytest.raises(ValueError, match="valid UTF-8 JSON"):
                    WorkflowRuntime._history_artifact_records([str(malformed)], "issue")

                oversized = tool_results / "oversized.txt"
                oversized.write_bytes(b" " * (5 * 1024 * 1024 + 1))
                oversized.chmod(0o600)
                with pytest.raises(ValueError, match="history artifact exceeds"):
                    WorkflowRuntime._history_artifact_records([str(oversized)], "issue")

                outside = root / "outside.json"
                outside.write_text("[]", encoding="utf-8")
                outside.chmod(0o600)
                with pytest.raises(ValueError, match="persisted-output"):
                    WorkflowRuntime._history_artifact_records([str(outside)], "issue")

                linked = tool_results / "linked.txt"
                linked.symlink_to(issue_page)
                with pytest.raises(ValueError, match="non-symlink"):
                    WorkflowRuntime._history_artifact_records([str(linked)], "issue")

    def test_supervisor_finish_accepts_the_observed_empty_value(self) -> None:
        with tempfile.TemporaryDirectory(prefix="supervisor-finish-") as directory:
            runtime = self.make_runtime(Path(directory))
            self.initialize_audit(runtime)
            runtime.audit_record(
                AuditRecordRequest(
                    action="supervisor_start",
                    activity={"kind": "history-sync"},
                )
            )
            runtime.audit_record(AuditRecordRequest(action="supervisor_finish"))
            assert runtime.state("gh-audit-repo")["scheduler"]["supervisor_activity"] is None
            with pytest.raises(ValidationError):
                AuditRecordRequest(action="supervisor_finish", unexpected=True)

    def test_run_lifecycle_cannot_be_bypassed_by_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-lifecycle-") as directory:
            runtime = self.make_runtime(Path(directory))
            workflow = "gh-curate-issues"
            runtime.run_manage(
                RunManageRequest(
                    action="start",
                    workflow=workflow,
                    repository="example/repo",
                )
            )
            runtime.run_manage(RunManageRequest(action="pause", workflow=workflow))
            paused = runtime.run_status(workflow)
            assert paused["status"] == "suspended"
            assert paused["scheduler"]["next_action"] == "resume"
            assert paused["scheduler"]["worker_slots"] == 0
            with pytest.raises(ValueError, match="invalid run transition"):
                runtime.run_manage(RunManageRequest(action="checkpoint", workflow=workflow))
            resumed = runtime.run_manage(RunManageRequest(action="resume", workflow=workflow))
            assert resumed["status"] == "in-progress"
            runtime.run_manage(RunManageRequest(action="finish", workflow=workflow))
            with pytest.raises(ValueError, match="invalid run transition"):
                runtime.run_manage(RunManageRequest(action="checkpoint", workflow=workflow))

    def test_generic_retry_collision_and_integration_concurrency_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-generic-safety-") as directory:
            runtime = self.make_runtime(Path(directory))
            workflow = "gh-curate-issues"
            runtime.run_manage(
                RunManageRequest(
                    action="start",
                    workflow=workflow,
                    repository="example/repo",
                    n=1,
                )
            )
            runtime.task_manage(
                TaskManageRequest(
                    action="plan",
                    workflow=workflow,
                    task={"logical_id": "foo"},
                )
            )
            runtime.task_manage(
                TaskManageRequest(
                    action="plan",
                    workflow=workflow,
                    task={"logical_id": "unrelated"},
                )
            )
            runtime.task_manage(
                TaskManageRequest(action="mark_running", workflow=workflow, task_id="foo-1")
            )
            runtime.task_manage(
                TaskManageRequest(action="fail", workflow=workflow, task_id="foo-1")
            )
            retry = runtime.task_manage(
                TaskManageRequest(action="retry", workflow=workflow, task_id="foo-1")
            )
            assert retry["task_id"] == "foo-2"
            assert runtime.state(workflow)["tasks"]["unrelated-1"]["logical_id"] == "unrelated"

            second = WorkflowRuntime(runtime.workspace, Path(directory) / "qwen-concurrency")
            second.run_manage(
                RunManageRequest(
                    action="start",
                    workflow=workflow,
                    repository="example/repo",
                    n=1,
                )
            )
            for task_id in ("running-1", "queued-1"):
                second.task_manage(
                    TaskManageRequest(
                        action="plan",
                        workflow=workflow,
                        task={"logical_id": task_id.rsplit("-", 1)[0]},
                    )
                )
            second.task_manage(
                TaskManageRequest(
                    action="mark_running",
                    workflow=workflow,
                    task_id="running-1",
                )
            )
            second.task_manage(
                TaskManageRequest(
                    action="abandon",
                    workflow=workflow,
                    task_id="queued-1",
                )
            )
            with pytest.raises(ValueError, match="exceed material-work concurrency"):
                second.task_manage(
                    TaskManageRequest(
                        action="integration_begin",
                        workflow=workflow,
                        task_id="queued-1",
                    )
                )

    def test_failed_resume_concurrency_change_keeps_run_suspended(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-resume-atomic-") as directory:
            runtime = self.make_runtime(Path(directory))
            workflow = "gh-curate-issues"
            runtime.run_manage(
                RunManageRequest(
                    action="start",
                    workflow=workflow,
                    repository="example/repo",
                    n=2,
                )
            )
            for logical_id in ("task-one", "task-two"):
                planned = runtime.task_manage(
                    TaskManageRequest(
                        action="plan",
                        workflow=workflow,
                        task={"logical_id": logical_id},
                    )
                )
                runtime.task_manage(
                    TaskManageRequest(
                        action="mark_running",
                        workflow=workflow,
                        task_id=planned["task_id"],
                    )
                )
            runtime.run_manage(RunManageRequest(action="pause", workflow=workflow))
            revision = runtime.state(workflow)["revision"]
            with pytest.raises(ValueError, match="currently running worker count"):
                runtime.run_manage(
                    RunManageRequest(
                        action="resume",
                        workflow=workflow,
                        n=1,
                    )
                )
            state = runtime.state(workflow)
            assert state["status"] == "suspended"
            assert state["revision"] == revision
            assert state["scheduler"]["limit"] == 2

    def test_audit_report_is_not_overwritten_after_transition_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-report-safety-") as directory:
            runtime = self.make_runtime(Path(directory))
            self.initialize_audit(runtime)
            runtime.task_manage(
                TaskManageRequest(
                    action="plan",
                    task={"logical_id": "task", "role": "structure", "unit": "repo"},
                )
            )
            runtime.task_manage(TaskManageRequest(action="mark_running", task_id="task-1"))
            runtime.task_manage(
                TaskManageRequest(
                    action="complete",
                    task_id="task-1",
                    report={"value": 1},
                )
            )
            artifact = runtime.current("gh-audit-repo") / "areas/task-1.json"
            revision = runtime.state("gh-audit-repo")["revision"]
            with pytest.raises(ValueError, match="running or checkpointed"):
                runtime.task_manage(
                    TaskManageRequest(
                        action="complete",
                        task_id="task-1",
                        report={"value": 2},
                    )
                )
            assert json.loads(artifact.read_text()) == {"value": 1}
            assert runtime.state("gh-audit-repo")["revision"] == revision

    def test_helper_drift_stops_work_and_status_exposes_complete_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-helper-drift-") as directory:
            runtime = self.make_runtime(Path(directory))
            self.initialize_audit(runtime)
            state_path = runtime.current("gh-audit-repo") / "state.json"
            state = json.loads(state_path.read_text())
            state["helper_hashes"] = {}
            workflow_run.write_state(runtime.current("gh-audit-repo"), state)

            status = runtime.run_status("gh-audit-repo")
            assert not status["helper_integrity"]["valid"]
            assert status["scheduler"]["next_action"] == "abort-and-start-new-run"
            assert status["helper_integrity"]["required_action"] == "abort-and-start-new-run"
            assert status["scheduler"]["worker_slots"] == 0
            for field in (
                "repository",
                "sha",
                "branch",
                "audit_worktree",
                "source_confirmed",
                "head_drift",
                "history",
                "inventory",
                "shards",
                "verdicts",
                "validations",
                "mutations",
                "metrics",
            ):
                assert field in status
            with pytest.raises(ValueError, match="helpers changed"):
                runtime.audit_metrics()

    def test_publication_finish_is_atomic_and_validated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-publish-safety-") as directory:
            runtime = self.make_runtime(Path(directory))
            self.initialize_audit(runtime)
            with pytest.raises(ValueError, match="unknown candidate"):
                runtime.audit_publish(
                    PublishRequest(
                        action="begin",
                        candidate_id="C-missing",
                        mutation="create",
                    )
                )
            assert not runtime.state("gh-audit-repo")["history"]["publication_pending"]

            runtime.audit_record(
                AuditRecordRequest(
                    action="candidate",
                    candidate={"id": "C-1", "status": "verified"},
                )
            )
            runtime.audit_publish(
                PublishRequest(
                    action="begin",
                    candidate_id="C-1",
                    mutation="create",
                )
            )
            state = runtime.state("gh-audit-repo")
            del state["candidates"]["C-1"]
            workflow_run.write_state(runtime.current("gh-audit-repo"), state)
            revision = state["revision"]
            with pytest.raises(ValueError, match="unknown candidate"):
                runtime.audit_publish(
                    PublishRequest(
                        action="finish",
                        candidate_id="C-1",
                        mutation="create",
                        receipt={"url": "https://example.invalid/1"},
                    )
                )
            failed = runtime.state("gh-audit-repo")
            assert failed["revision"] == revision
            assert failed["history"]["publication_pending"]
            assert failed["mutations"] == []

    def test_missing_history_query_does_not_create_a_database(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-history-readonly-") as directory:
            runtime = self.make_runtime(Path(directory))
            workflow = "gh-curate-issues"
            runtime.run_manage(
                RunManageRequest(
                    action="start",
                    workflow=workflow,
                    repository="example/repo",
                )
            )
            database = github_cache.live_path(
                github_cache.repo_dir(runtime.project_dir, "example/repo"),
                "records",
            )
            with pytest.raises(ValueError, match="cache is missing"):
                runtime.history_query(HistoryQueryRequest(workflow=workflow))
            assert not database.exists()
