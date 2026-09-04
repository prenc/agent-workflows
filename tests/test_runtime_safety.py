from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
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
    def test_probe_timeout_payload_is_available_for_registration(self) -> None:
        def timed_out(_: Any) -> int:
            print(json.dumps({"returncode": -15, "timed_out": True}))
            return 124

        payload = WorkflowRuntime._invoke(timed_out, allow_timeout=True)

        assert payload["timed_out"] is True
        assert payload["returncode"] == -15

    def test_non_timeout_exit_mismatch_remains_an_error(self) -> None:
        def mismatched(_: Any) -> int:
            print(json.dumps({"returncode": -15, "timed_out": False}))
            return 124

        with pytest.raises(RuntimeError):
            WorkflowRuntime._invoke(mismatched, allow_timeout=True)

    def test_timed_out_probe_is_returned_and_registered(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-probe-timeout-") as directory:
            runtime = self.make_runtime(Path(directory))
            self.initialize_audit(runtime)
            runtime.history_manage(HistoryManageRequest(action="prepare"))
            runtime.history_manage(
                HistoryManageRequest(action="commit", full_history_complete=True)
            )
            runtime.audit_record(
                AuditRecordRequest(
                    action="candidate",
                    candidate={"id": "candidate-timeout", "status": "discovered"},
                )
            )

            def timed_out(args: Any) -> int:
                artifact_dir = args.run_dir / "validation" / args.probe_id
                artifact_dir.mkdir(parents=True, exist_ok=True)
                artifact = {
                    "probe_id": args.probe_id,
                    "probe_status": "timed-out",
                    "returncode": -15,
                    "timed_out": True,
                    "worktree_unchanged": True,
                    "stdout_excerpt": "partial output",
                    "stderr_excerpt": "",
                }
                (artifact_dir / "result.json").write_text(json.dumps(artifact))
                print(json.dumps({"returncode": -15, "timed_out": True}))
                return 124

            with mock.patch(
                "github_workflows.runtime.audit_probe.run_probe", side_effect=timed_out
            ):
                result = runtime.audit_probe(
                    ProbeRequest(
                        kind="python",
                        probe_id="probe-timeout",
                        candidate_id="candidate-timeout",
                        code="pass",
                    )
                )

            assert result["status"] == "timed-out"
            assert result["stdout_excerpt"] == "partial output"
            assert result["artifact"] == "validation/probe-timeout/result.json"
            validation = runtime.state("gh-audit-repo")["validations"]["probe-timeout"]
            assert validation["status"] == "timed-out"
            assert validation["candidate_id"] == "candidate-timeout"
            planned = runtime.task_manage(
                TaskManageRequest(
                    action="plan",
                    task={
                        "logical_id": "verify-timeout",
                        "assignment": {
                            "mode": "verify",
                            "candidate": {"id": "candidate-timeout"},
                        },
                    },
                )
            )

            context = runtime.task_context(planned["task_ref"])

            readonly_search = Path(context["references"]["readonly_search"])
            assert readonly_search.name == "readonly-search.py"
            assert readonly_search.is_file()

            assert context["validation"] == {
                "candidate_id": "candidate-timeout",
                "record_count": 1,
                "records": [
                    {
                        "id": "probe-timeout",
                        "probe_id": "probe-timeout",
                        "candidate_id": "candidate-timeout",
                        "status": "timed-out",
                        "artifact": "validation/probe-timeout/result.json",
                    }
                ],
            }

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
            (
                ProbeRequest,
                {
                    "kind": "python",
                    "probe_id": "probe-1",
                    "candidate_id": "candidate-1",
                    "code": "pass",
                },
            ),
            (
                AuditRecordRequest,
                {"action": "phase", "phase": {"name": "structure", "status": "complete"}},
            ),
            (PublishRequest, {"action": "begin", "candidate_id": "C-1", "operation": "create"}),
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
                TaskManageRequest(
                    action="retry",
                    workflow=workflow,
                    task_id="foo-1",
                    note="Use the refreshed issue state",
                )
            )
            assert retry["task_id"] == "foo-2"
            assert runtime.task_context(retry["task_ref"])["continuation"] == {
                "retry": {"from_attempt": 1, "note": "Use the refreshed issue state"}
            }
            branched = runtime.task_manage(
                TaskManageRequest(
                    action="retry",
                    workflow=workflow,
                    task_id="foo-1",
                    note="Branch again from the original failure",
                )
            )
            assert branched["task_id"] == "foo-3"
            assert runtime.task_context(branched["task_ref"])["continuation"] == {
                "retry": {
                    "from_attempt": 1,
                    "note": "Branch again from the original failure",
                }
            }
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

    def test_run_status_exposes_structured_finish_blockers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-finish-readiness-") as directory:
            runtime = self.make_runtime(Path(directory))
            self.initialize_audit(runtime)

            status = runtime.run_status("gh-audit-repo")

            assert status["finish_ready"] is False
            blockers = {blocker["kind"]: blocker for blocker in status["finish_blockers"]}
            assert blockers["incomplete-phases"]["allowed_action"] == "phase"
            assert "phase" in status["allowed_actions"]

    def test_publication_finish_is_atomic_and_validated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-publish-safety-") as directory:
            runtime = self.make_runtime(Path(directory))
            self.initialize_audit(runtime)
            with pytest.raises(ValueError, match="unknown candidate"):
                runtime.audit_publish(
                    PublishRequest(
                        action="begin",
                        candidate_id="C-missing",
                        operation="create",
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
                    operation="create",
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
                        receipt={"url": "https://example.invalid/1"},
                    )
                )
            failed = runtime.state("gh-audit-repo")
            assert failed["revision"] == revision
            assert failed["history"]["publication_pending"]
            assert failed["mutations"] == []

    @pytest.mark.parametrize(
        ("operation", "status"),
        [
            ("create", "published"),
            ("update", "updated"),
            ("no-op", "no-op"),
            ("close", "closed"),
            ("dry-run", "dry-run"),
        ],
    )
    def test_publication_finish_atomically_disposes_candidate(
        self, operation: str, status: str
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-publish-disposition-") as directory:
            runtime = self.make_runtime(Path(directory))
            self.initialize_audit(runtime)
            candidate_id = f"C-{operation}"
            runtime.audit_record(
                AuditRecordRequest(
                    action="candidate",
                    candidate={"id": candidate_id, "status": "verified"},
                )
            )
            runtime.audit_publish(
                PublishRequest(action="begin", candidate_id=candidate_id, operation=operation)
            )
            result = runtime.audit_publish(
                PublishRequest(
                    action="finish",
                    candidate_id=candidate_id,
                    receipt={"url": "https://example.invalid/1"},
                )
            )

            state = runtime.state("gh-audit-repo")
            assert state["candidates"][candidate_id]["status"] == status
            assert state["mutations"][-1]["action"] == operation
            assert result["publication"]["publication_pending"] is False

    def test_publication_finish_reads_legacy_pending_operation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-publish-legacy-") as directory:
            runtime = self.make_runtime(Path(directory))
            self.initialize_audit(runtime)
            runtime.audit_record(
                AuditRecordRequest(
                    action="candidate",
                    candidate={"id": "C-legacy", "status": "verified"},
                )
            )
            state = runtime.state("gh-audit-repo")
            state["history"].update(
                {
                    "publication_pending": True,
                    "candidate_id": "C-legacy",
                    "mutation": "update",
                }
            )
            workflow_run.write_state(runtime.current("gh-audit-repo"), state)

            runtime.audit_publish(
                PublishRequest(
                    action="finish",
                    candidate_id="C-legacy",
                    receipt={"url": "https://example.invalid/1"},
                )
            )

            updated = runtime.state("gh-audit-repo")
            assert updated["candidates"]["C-legacy"]["status"] == "updated"
            assert updated["history"] == {
                "candidate_id": "C-legacy",
                "operation": "update",
                "outcome": "finish",
                "publication_pending": False,
                "receipt": {"url": "https://example.invalid/1"},
            }

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

    def test_audit_mode_owns_integration_and_shard_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-audit-shard-") as directory:
            runtime = self.make_runtime(Path(directory))
            self.initialize_audit(runtime)
            planned = runtime.task_manage(
                TaskManageRequest(
                    action="plan",
                    task={
                        "logical_id": "discover-core",
                        "assignment": {
                            "mode": "discover",
                            "shard_id": "shard-core",
                            "area": "area/shared-core",
                            "paths": ["src/core.py"],
                        },
                    },
                )
            )
            assert planned["task"]["role"] == "discover"
            assert planned["task"]["requires_integration"] is True
            assert runtime.state("gh-audit-repo")["shards"]["shard-core"]["status"] == "pending"

            runtime.task_manage(TaskManageRequest(action="mark_running", task_id="discover-core-1"))
            assert runtime.state("gh-audit-repo")["shards"]["shard-core"]["status"] == "running"
            completed = runtime.task_manage(
                TaskManageRequest(
                    action="complete",
                    task_id="discover-core-1",
                    report={"status": "partial", "remaining": "one module"},
                )
            )
            assert completed["task"]["result"] == "areas/discover-core-1.json"
            runtime.task_manage(
                TaskManageRequest(
                    action="plan",
                    task={
                        "logical_id": "verify-core",
                        "assignment": {
                            "mode": "verify",
                            "shard_id": "shard-core",
                            "candidate": {"id": "C-core"},
                        },
                    },
                )
            )
            runtime.task_manage(
                TaskManageRequest(action="integration_begin", task_id="discover-core-1")
            )
            runtime.task_manage(
                TaskManageRequest(action="integration_end", task_id="discover-core-1")
            )
            assert runtime.state("gh-audit-repo")["shards"]["shard-core"]["status"] == "partial"

    def test_verify_assignment_fingerprint_is_server_owned_and_report_bound(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-verify-fingerprint-") as directory:
            runtime = self.make_runtime(Path(directory))
            self.initialize_audit(runtime)
            runtime.history_manage(HistoryManageRequest(action="prepare"))
            runtime.history_manage(
                HistoryManageRequest(action="commit", full_history_complete=True)
            )
            candidate = {
                "id": "C-core",
                "root_cause": "cache entries are retained",
                "paths": ["src/core.py"],
            }

            planned = runtime.task_manage(
                TaskManageRequest(
                    action="plan",
                    task={
                        "logical_id": "verify-core",
                        "assignment": {"mode": "verify", "candidate": candidate},
                    },
                )
            )
            fingerprint = planned["task"]["assignment"]["candidate_fingerprint"]

            assert len(fingerprint) == 64
            assert int(fingerprint, 16) >= 0
            context = runtime.task_context(planned["task_ref"])
            assert context["assignment"]["candidate"] == candidate
            assert context["assignment"]["candidate_fingerprint"] == fingerprint

            revised = runtime.task_manage(
                TaskManageRequest(
                    action="plan",
                    task={
                        "logical_id": "verify-core",
                        "assignment": {
                            "candidate": {
                                "paths": ["src/core.py"],
                                "root_cause": "cache entries are retained",
                                "id": "C-core",
                            },
                            "mode": "verify",
                        },
                    },
                )
            )
            assert revised["task"]["assignment"]["candidate_fingerprint"] == fingerprint

            with pytest.raises(ValueError, match="requires one canonical candidate"):
                runtime.task_manage(
                    TaskManageRequest(
                        action="plan",
                        task={
                            "logical_id": "verify-missing",
                            "assignment": {"mode": "verify", "candidate_id": "C-core"},
                        },
                    )
                )
            with pytest.raises(ValueError, match="server-owned"):
                runtime.task_manage(
                    TaskManageRequest(
                        action="plan",
                        task={
                            "logical_id": "verify-supplied",
                            "assignment": {
                                "mode": "verify",
                                "candidate": candidate,
                                "candidate_fingerprint": fingerprint,
                            },
                        },
                    )
                )

            runtime.task_manage(TaskManageRequest(action="mark_running", task_id="verify-core-1"))
            with pytest.raises(ValueError, match="requires candidate_fingerprint"):
                runtime.task_manage(
                    TaskManageRequest(
                        action="complete",
                        task_id="verify-core-1",
                        report={"status": "complete"},
                    )
                )
            with pytest.raises(ValueError, match="does not match"):
                runtime.task_manage(
                    TaskManageRequest(
                        action="complete",
                        task_id="verify-core-1",
                        report={"status": "complete", "candidate_fingerprint": "0" * 64},
                    )
                )
            completed = runtime.task_manage(
                TaskManageRequest(
                    action="complete",
                    task_id="verify-core-1",
                    report={
                        "status": "complete",
                        "candidate_fingerprint": fingerprint,
                    },
                )
            )
            runtime.task_manage(
                TaskManageRequest(action="integration_begin", task_id=completed["task_id"])
            )
            runtime.task_manage(
                TaskManageRequest(action="integration_end", task_id=completed["task_id"])
            )
            retried = runtime.task_manage(
                TaskManageRequest(
                    action="retry",
                    task_id=completed["task_id"],
                    note="Recheck the pinned runtime source only",
                )
            )
            assert retried["task"]["assignment"]["candidate_fingerprint"] == fingerprint
            assert runtime.task_context(retried["task_ref"])["continuation"] == {
                "retry": {
                    "from_attempt": 1,
                    "note": "Recheck the pinned runtime source only",
                }
            }
            runtime.task_manage(
                TaskManageRequest(action="mark_running", task_id=retried["task_id"])
            )
            runtime.task_manage(TaskManageRequest(action="fail", task_id=retried["task_id"]))
            branched = runtime.task_manage(
                TaskManageRequest(
                    action="retry",
                    task_id=completed["task_id"],
                    note="Branch again from the original verification",
                )
            )
            assert branched["task_id"] == "verify-core-3"
            assert runtime.task_context(branched["task_ref"])["continuation"] == {
                "retry": {
                    "from_attempt": 1,
                    "note": "Branch again from the original verification",
                }
            }

    def test_incompatible_verify_assignment_must_be_replanned(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-legacy-fingerprint-") as directory:
            runtime = self.make_runtime(Path(directory))
            self.initialize_audit(runtime)
            runtime.history_manage(HistoryManageRequest(action="prepare"))
            runtime.history_manage(
                HistoryManageRequest(action="commit", full_history_complete=True)
            )
            state = runtime.state("gh-audit-repo")
            state["tasks"]["verify-legacy-1"] = {
                "id": "verify-legacy-1",
                "logical_id": "verify-legacy",
                "agent_id": "verify-legacy-1",
                "role": "verify",
                "unit": "verify-legacy",
                "attempt": 1,
                "status": "queued",
                "required": True,
                "requires_integration": True,
                "integrated": False,
                "assignment": {"mode": "verify", "candidate_id": "C-legacy"},
            }
            workflow_run.write_state(runtime.current("gh-audit-repo"), state)
            task_ref = runtime._task_ref("gh-audit-repo", state, "verify-legacy-1")

            with pytest.raises(ValueError, match="incompatible; retry it"):
                runtime.task_context(task_ref)

            runtime.task_manage(TaskManageRequest(action="mark_running", task_id="verify-legacy-1"))
            with pytest.raises(ValueError, match="incompatible; retry it"):
                runtime.task_manage(
                    TaskManageRequest(
                        action="complete",
                        task_id="verify-legacy-1",
                        report={
                            "status": "complete",
                            "candidate_fingerprint": "0" * 64,
                        },
                    )
                )

            runtime.task_manage(
                TaskManageRequest(
                    action="fail",
                    task_id="verify-legacy-1",
                    note="incompatible verify assignment",
                )
            )
            retried = runtime.task_manage(
                TaskManageRequest(
                    action="retry",
                    task_id="verify-legacy-1",
                    task={
                        "logical_id": "verify-legacy",
                        "unit": "verify-legacy",
                        "assignment": {
                            "mode": "verify",
                            "candidate": {"id": "C-legacy"},
                        },
                    },
                )
            )
            assert len(retried["task"]["assignment"]["candidate_fingerprint"]) == 64
            assert "continuation" not in runtime.task_context(retried["task_ref"])

    def test_suspended_audit_accepts_only_late_worker_results(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-audit-suspend-") as directory:
            runtime = self.make_runtime(Path(directory))
            self.initialize_audit(runtime)
            runtime.history_manage(HistoryManageRequest(action="prepare"))
            runtime.history_manage(
                HistoryManageRequest(action="commit", full_history_complete=True)
            )
            runtime.task_manage(
                TaskManageRequest(
                    action="plan",
                    task={
                        "logical_id": "discover-core",
                        "assignment": {
                            "mode": "discover",
                            "shard_id": "shard-core",
                            "area": "area/shared-core",
                            "paths": ["src/core.py"],
                        },
                    },
                )
            )
            runtime.task_manage(TaskManageRequest(action="mark_running", task_id="discover-core-1"))
            paused = runtime.run_manage(RunManageRequest(action="pause", workflow="gh-audit-repo"))
            assert paused["status"] == "suspended"

            checkpointed = runtime.task_manage(
                TaskManageRequest(
                    action="checkpoint",
                    task_id="discover-core-1",
                    report={"status": "partial", "remaining": "tests"},
                )
            )
            assert checkpointed["task"]["status"] == "checkpointed"
            assert checkpointed["task"]["checkpoint"] == ("areas/discover-core-1.checkpoint.1.json")
            assert checkpointed["scheduler"]["next_action"] == "resume"
            context = runtime.task_context(checkpointed["task_ref"])
            assert context["continuation"] == {
                "attempt": 1,
                "report": {"status": "partial", "remaining": "tests"},
            }
            with pytest.raises(RuntimeError):
                runtime.task_manage(
                    TaskManageRequest(
                        action="plan",
                        task={
                            "logical_id": "discover-late",
                            "assignment": {"mode": "discover"},
                        },
                    )
                )

    def test_audit_checkpoint_can_be_refreshed_after_resuming_task(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-audit-checkpoint-refresh-") as directory:
            runtime = self.make_runtime(Path(directory))
            self.initialize_audit(runtime)
            runtime.history_manage(HistoryManageRequest(action="prepare"))
            runtime.history_manage(
                HistoryManageRequest(action="commit", full_history_complete=True)
            )
            runtime.task_manage(
                TaskManageRequest(
                    action="plan",
                    task={
                        "logical_id": "discover-core",
                        "assignment": {
                            "mode": "discover",
                            "shard_id": "shard-core",
                            "area": "area/shared-core",
                            "paths": ["src/core.py"],
                        },
                    },
                )
            )
            runtime.task_manage(TaskManageRequest(action="mark_running", task_id="discover-core-1"))
            first = runtime.task_manage(
                TaskManageRequest(
                    action="checkpoint",
                    task_id="discover-core-1",
                    report={"status": "partial", "remaining": "tests"},
                )
            )
            runtime.task_manage(TaskManageRequest(action="mark_running", task_id="discover-core-1"))
            second = runtime.task_manage(
                TaskManageRequest(
                    action="checkpoint",
                    task_id="discover-core-1",
                    report={"status": "partial", "remaining": "documentation"},
                )
            )

            assert first["task"]["checkpoint"] == "areas/discover-core-1.checkpoint.1.json"
            assert second["task"]["checkpoint"] == "areas/discover-core-1.checkpoint.2.json"
            assert runtime.task_context(second["task_ref"])["continuation"]["report"] == {
                "status": "partial",
                "remaining": "documentation",
            }
            runtime.task_manage(
                TaskManageRequest(
                    action="fail",
                    task_id="discover-core-1",
                    note="turn budget exhausted",
                )
            )
            retried = runtime.task_manage(
                TaskManageRequest(
                    action="retry",
                    task_id="discover-core-1",
                    note="Inspect only the remaining documentation",
                )
            )
            assert runtime.task_context(retried["task_ref"])["continuation"] == {
                "retry": {
                    "from_attempt": 1,
                    "note": "Inspect only the remaining documentation",
                },
                "attempt": 1,
                "report": {"status": "partial", "remaining": "documentation"},
            }

    def test_revising_queued_audit_task_transfers_pending_shard(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-audit-revise-shard-") as directory:
            runtime = self.make_runtime(Path(directory))
            self.initialize_audit(runtime)
            runtime.task_manage(
                TaskManageRequest(
                    action="plan",
                    task={
                        "logical_id": "discover-core",
                        "assignment": {
                            "mode": "discover",
                            "shard_id": "shard-old",
                            "area": "area/shared-core",
                            "paths": ["src/old.py"],
                        },
                    },
                )
            )
            runtime.task_manage(
                TaskManageRequest(
                    action="plan",
                    task={
                        "logical_id": "verify-core",
                        "assignment": {
                            "mode": "verify",
                            "shard_id": "shard-old",
                            "candidate": {"id": "C-core"},
                        },
                    },
                )
            )
            revised = runtime.task_manage(
                TaskManageRequest(
                    action="plan",
                    task={
                        "logical_id": "discover-core",
                        "assignment": {
                            "mode": "discover",
                            "shard_id": "shard-new",
                            "area": "area/shared-core",
                            "paths": ["src/new.py"],
                        },
                    },
                )
            )

            state = runtime.state("gh-audit-repo")
            assert set(state["shards"]) == {"shard-new"}
            assert revised["task"]["assignment"]["shard_id"] == "shard-new"
            assert state["shards"]["shard-new"]["status"] == "pending"

    def test_audit_task_context_supplies_bounded_compact_history(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-audit-context-") as directory:
            runtime = self.make_runtime(Path(directory))
            self.initialize_audit(runtime)
            runtime.history_manage(HistoryManageRequest(action="prepare"))
            runtime.history_manage(
                HistoryManageRequest(
                    action="ingest",
                    records=[
                        {
                            "kind": "issue",
                            "number": 1,
                            "state": "open",
                            "title": "Shared core cache grows",
                            "labels": ["area/shared-core"],
                        },
                        {
                            "kind": "issue",
                            "number": 2,
                            "state": "closed",
                            "title": "Earlier parser defect",
                        },
                        {
                            "kind": "pull",
                            "number": 3,
                            "state": "open",
                            "title": "Unrelated documentation",
                        },
                    ],
                )
            )
            runtime.history_manage(
                HistoryManageRequest(action="commit", full_history_complete=True)
            )
            planned = runtime.task_manage(
                TaskManageRequest(
                    action="plan",
                    task={
                        "logical_id": "discover-core",
                        "assignment": {
                            "mode": "discover",
                            "area": "area/shared-core",
                            "focus": "cache behavior",
                            "leads": [2],
                        },
                    },
                )
            )

            context = runtime.task_context(planned["task_ref"])

            assert context["history"]["cache"] == {
                "generation": 1,
                "record_count": 3,
                "complete": True,
            }
            assert context["history"]["selection"]["record_count"] == 2
            assert context["history"]["selection"]["has_more"] is False
            assert [
                (record["kind"], record["number"], record["title"])
                for record in context["history"]["selection"]["records"]
            ] == [
                ("issue", 2, "Earlier parser defect"),
                ("issue", 1, "Shared core cache grows"),
            ]

    def test_audit_task_context_reports_an_exact_history_window(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-audit-history-window-") as directory:
            runtime = self.make_runtime(Path(directory))
            self.initialize_audit(runtime)
            runtime.history_manage(HistoryManageRequest(action="prepare"))
            runtime.history_manage(
                HistoryManageRequest(
                    action="ingest",
                    records=[
                        {
                            "kind": "issue",
                            "number": number,
                            "state": "open",
                            "title": f"Shared core finding {number}",
                            "labels": ["area/shared-core"],
                        }
                        for number in range(1, 42)
                    ],
                )
            )
            runtime.history_manage(
                HistoryManageRequest(action="commit", full_history_complete=True)
            )
            planned = runtime.task_manage(
                TaskManageRequest(
                    action="plan",
                    task={
                        "logical_id": "discover-core",
                        "assignment": {
                            "mode": "discover",
                            "area": "area/shared-core",
                            "focus": "shared core",
                        },
                    },
                )
            )

            selection = runtime.task_context(planned["task_ref"])["history"]["selection"]

            assert selection["record_count"] == 40
            assert selection["limit"] == 40
            assert selection["has_more"] is True
            assert len(selection["records"]) == 40

    def test_audit_task_context_selects_only_requested_python_packages(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-audit-inventory-context-") as directory:
            runtime = self.make_runtime(Path(directory))
            self.initialize_audit(runtime)
            runtime.history_manage(HistoryManageRequest(action="prepare"))
            runtime.history_manage(
                HistoryManageRequest(action="commit", full_history_complete=True)
            )
            state = runtime.state("gh-audit-repo")
            state["inventory"] = {
                "revision": 7,
                "updated_at": "2026-09-01T12:00:00Z",
                "sources": {
                    "python_environment": {
                        "available": True,
                        "source": "project-venv",
                        "executable": ".venv/bin/python",
                        "python": "3.12.8",
                        "interpreter_prefix": "/workspace/.venv",
                        "stdlib_root": "/usr/lib/python3.12",
                        "packages": {
                            **{f"unused-package-{index}": "1.0" for index in range(250)},
                            "Pydantic": "2.13.0",
                        },
                    },
                    "repository_manifests": {"pyproject.toml": "sha256:abc"},
                    "programs": {"rg": {"status": "available", "version": "14.1.1"}},
                    "declared": {"python": ">=3.12"},
                    "context": {},
                },
                "requests": {},
            }
            workflow_run.write_state(runtime.current("gh-audit-repo"), state)
            planned = runtime.task_manage(
                TaskManageRequest(
                    action="plan",
                    task={
                        "logical_id": "discover-core",
                        "assignment": {
                            "mode": "discover",
                            "area": "area/shared-core",
                            "python_packages": ["pydantic", "missing-package"],
                        },
                    },
                )
            )

            context = runtime.task_context(planned["task_ref"])

            assert context["task"] == {
                "id": "discover-core-1",
                "logical_id": "discover-core",
                "role": "discover",
                "unit": "discover-core",
                "attempt": 1,
                "status": "queued",
                "required": True,
            }
            assert context["inventory"]["python_environment"] == {
                "available": True,
                "source": "project-venv",
                "executable": ".venv/bin/python",
                "python": "3.12.8",
                "interpreter_prefix": "/workspace/.venv",
                "stdlib_root": "/usr/lib/python3.12",
                "package_count": 251,
                "packages": {"Pydantic": "2.13.0"},
                "missing_requested_packages": ["missing-package"],
            }
            assert len(json.dumps(context)) < 8_000
