from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

HELPER = Path(__file__).parents[1] / "src/github_workflows/workflow_run.py"


class TestWorkflowRun:
    def setup_method(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="workflow-run-test-")
        root = Path(self.temporary.name).resolve()
        self.project = root / "repo"
        self.project.mkdir()
        self.project_dir = root / "qwen-project"

    def teardown_method(self) -> None:
        self.temporary.cleanup()

    def call(self, command: str, workflow: str, *arguments: str, check: bool = True):
        result = subprocess.run(
            [
                str(HELPER),
                command,
                "--project-root",
                str(self.project),
                "--project-dir",
                str(self.project_dir),
                "--workflow",
                workflow,
                *arguments,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if check and result.returncode:
            pytest.fail(result.stdout + result.stderr)
        return result

    def initialize(self, workflow: str = "gh-audit-repo") -> dict:
        source = self.project / "input.json"
        source.write_text('{"repository":"example/repo"}\n')
        return json.loads(self.call("initialize", workflow, "--input", str(source)).stdout)

    def audit_event(self, revision: int, payload: dict, *, check: bool = True):
        source = self.project / f"event-{revision}.json"
        source.write_text(json.dumps(payload) + "\n")
        return self.call(
            "audit-event",
            "gh-audit-repo",
            "--expected-revision",
            str(revision),
            "--input",
            str(source),
            check=check,
        )

    def result_artifact(self, name: str, payload: dict | None = None) -> str:
        path = self.project_dir / "workflows/gh-audit-repo/current/areas" / name
        path.write_text(json.dumps(payload or {"status": "complete"}) + "\n")
        return str(path)

    def test_initialize_creates_one_private_current_run(self) -> None:
        result = self.initialize()
        current = self.project_dir / "workflows/gh-audit-repo/current"
        assert Path(result["run_dir"]) == current
        state = json.loads((current / "state.json").read_text())
        assert state["status"] == "in-progress"
        assert state["schema_version"] == 2
        assert state["repository"] == "example/repo"
        assert state["scheduler"]["limit"] == 3
        assert state["tasks"] == {}
        assert state["candidates"] == {}
        assert (current / "journal.jsonl").is_file()
        for name in ("areas", "candidates", "validation"):
            assert (current / name).is_dir()

    def test_resume_needs_no_run_id_and_unfinished_blocks_replacement(self) -> None:
        self.initialize("gh-curate-issues")
        resumed = json.loads(self.call("resume", "gh-curate-issues").stdout)
        assert resumed["status"] == "in-progress"
        blocked = self.call(
            "initialize",
            "gh-curate-issues",
            "--input",
            str(self.project / "input.json"),
            check=False,
        )
        assert blocked.returncode != 0
        assert "use --resume" in blocked.stderr

    def test_terminal_current_is_replaced(self) -> None:
        first = self.initialize("gh-implement-issue")
        self.call(
            "finalize", "gh-implement-issue", "--expected-revision", "1", "--status", "complete"
        )
        second = self.initialize("gh-implement-issue")
        assert first["run_id"] != second["run_id"]

    def test_checkpoint_rejects_stale_revision(self) -> None:
        self.initialize()
        failed = self.call(
            "checkpoint",
            "gh-audit-repo",
            "--expected-revision",
            "0",
            "--status",
            "partial",
            check=False,
        )
        assert failed.returncode != 0
        state = json.loads(
            (self.project_dir / "workflows/gh-audit-repo/current/state.json").read_text()
        )
        assert state["revision"] == 1

    def test_audit_v2_rejects_partial_map_checkpoint(self) -> None:
        self.initialize()
        update = self.project / "update.json"
        update.write_text('{"candidates":{"one":{"status":"queued"}}}\n')
        failed = self.call(
            "checkpoint",
            "gh-audit-repo",
            "--expected-revision",
            "1",
            "--status",
            "in-progress",
            "--input",
            str(update),
            check=False,
        )
        assert failed.returncode == 2
        assert "typed audit-event" in failed.stderr

    def test_audit_input_cannot_prepopulate_internal_state(self) -> None:
        source = self.project / "internal-input.json"
        source.write_text('{"repository":"example/repo","tasks":{"fake":{}}}\n')
        failed = self.call(
            "initialize",
            "gh-audit-repo",
            "--input",
            str(source),
            check=False,
        )
        assert failed.returncode == 2
        assert "internal state fields" in failed.stderr

    def test_audit_concurrency_must_be_a_positive_integer(self) -> None:
        source = self.project / "invalid-concurrency.json"
        source.write_text('{"repository":"example/repo","inputs":{"n":true}}\n')
        failed = self.call(
            "initialize",
            "gh-audit-repo",
            "--input",
            str(source),
            check=False,
        )
        assert failed.returncode == 2
        assert "positive integer" in failed.stderr
        assert not (self.project_dir / "workflows/gh-audit-repo/current").exists()

    def test_result_first_scheduler_reserves_supervisor_lane(self) -> None:
        self.initialize()
        revision = 1
        for number in range(1, 4):
            result = self.audit_event(
                revision,
                {
                    "type": "task-register",
                    "task": {
                        "id": f"task-{number}",
                        "logical_id": f"discover-{number}",
                        "role": "discover",
                        "unit": f"shard/{number}",
                        "status": "running",
                    },
                },
            )
            revision = json.loads(result.stdout)["revision"]
        completed = self.audit_event(
            revision,
            {
                "type": "task-transition",
                "task_id": "task-1",
                "status": "completed",
                "result": self.result_artifact("report-1.json"),
            },
        )
        revision = json.loads(completed.stdout)["revision"]
        status = json.loads(self.call("audit-status", "gh-audit-repo").stdout)
        assert status["running_workers"] == 2
        assert status["worker_slots"] == 0
        assert status["next_action"] == "integrate-result"
        assert status["control_plane_available"]
        blocked = self.audit_event(
            revision,
            {
                "type": "task-register",
                "task": {
                    "id": "task-4",
                    "logical_id": "discover-4",
                    "role": "discover",
                    "unit": "shard/4",
                    "status": "running",
                },
            },
            check=False,
        )
        assert blocked.returncode == 2
        started = self.audit_event(revision, {"type": "integration-start", "task_id": "task-1"})
        revision = json.loads(started.stdout)["revision"]
        finished = self.audit_event(revision, {"type": "integration-complete", "task_id": "task-1"})
        status = json.loads(finished.stdout)["scheduler"]
        assert status["worker_slots"] == 1
        assert status["next_action"] == "launch-worker"

    def test_typed_candidate_updates_do_not_clobber_registry(self) -> None:
        self.initialize()
        first = self.audit_event(
            1,
            {
                "type": "candidate-upsert",
                "candidate": {"id": "C-1", "status": "rejected"},
            },
        )
        revision = json.loads(first.stdout)["revision"]
        self.audit_event(
            revision,
            {
                "type": "candidate-upsert",
                "candidate": {"id": "C-2", "status": "duplicate"},
            },
        )
        state = json.loads(
            (self.project_dir / "workflows/gh-audit-repo/current/state.json").read_text()
        )
        assert set(state["candidates"]) == {"C-1", "C-2"}

    def test_audit_finalization_rejects_nonterminal_task(self) -> None:
        self.initialize()
        result = self.audit_event(
            1,
            {
                "type": "task-register",
                "task": {
                    "id": "task-1",
                    "logical_id": "discover-1",
                    "role": "discover",
                    "unit": "shard/1",
                    "status": "running",
                },
            },
        )
        revision = json.loads(result.stdout)["revision"]
        failed = self.call(
            "finalize",
            "gh-audit-repo",
            "--expected-revision",
            str(revision),
            "--status",
            "complete",
            check=False,
        )
        assert failed.returncode == 2
        assert "nonterminal tasks" in failed.stderr

    def test_audit_finalization_accepts_integrated_terminal_state(self) -> None:
        self.initialize()
        revision = 1
        for phase in ("source", "history", "structure", "discovery", "verification", "publication"):
            result = self.audit_event(
                revision,
                {
                    "type": "phase-set",
                    "phase": phase,
                    "value": {"status": "complete"},
                },
            )
            revision = json.loads(result.stdout)["revision"]
        result = self.audit_event(
            revision,
            {
                "type": "shard-upsert",
                "shard": {
                    "id": "shard/core",
                    "area": "area/core",
                    "status": "complete",
                },
            },
        )
        revision = json.loads(result.stdout)["revision"]
        result = self.audit_event(
            revision,
            {
                "type": "task-register",
                "task": {
                    "id": "task-1",
                    "logical_id": "discover-core",
                    "role": "discover",
                    "unit": "shard/core",
                    "status": "running",
                },
            },
        )
        revision = json.loads(result.stdout)["revision"]
        result = self.audit_event(
            revision,
            {
                "type": "task-transition",
                "task_id": "task-1",
                "status": "completed",
                "result": self.result_artifact("core.json"),
            },
        )
        revision = json.loads(result.stdout)["revision"]
        result = self.audit_event(revision, {"type": "integration-start", "task_id": "task-1"})
        revision = json.loads(result.stdout)["revision"]
        result = self.audit_event(revision, {"type": "integration-complete", "task_id": "task-1"})
        revision = json.loads(result.stdout)["revision"]
        result = self.audit_event(
            revision,
            {
                "type": "candidate-upsert",
                "candidate": {"id": "C-1", "status": "rejected"},
            },
        )
        revision = json.loads(result.stdout)["revision"]
        finalized = self.call(
            "finalize",
            "gh-audit-repo",
            "--expected-revision",
            str(revision),
            "--status",
            "complete",
        )
        assert json.loads(finalized.stdout)["status"] == "complete"

    def test_candidate_updates_merge_and_terminal_disposition_is_stable(self) -> None:
        self.initialize()
        result = self.audit_event(
            1,
            {
                "type": "candidate-upsert",
                "candidate": {"id": "C-1", "status": "discovered", "title": "Lead"},
            },
        )
        revision = json.loads(result.stdout)["revision"]
        result = self.audit_event(
            revision,
            {
                "type": "candidate-upsert",
                "candidate": {"id": "C-1", "status": "rejected", "reason": "disproved"},
            },
        )
        revision = json.loads(result.stdout)["revision"]
        state = json.loads(
            (self.project_dir / "workflows/gh-audit-repo/current/state.json").read_text()
        )
        assert state["candidates"]["C-1"]["title"] == "Lead"
        reopened = self.audit_event(
            revision,
            {
                "type": "candidate-upsert",
                "candidate": {"id": "C-1", "status": "verified"},
            },
            check=False,
        )
        assert reopened.returncode == 2

    def test_validation_artifact_must_be_registered_from_validation_directory(self) -> None:
        self.initialize()
        outside = self.project / "result.json"
        outside.write_text("{}\n")
        result = self.audit_event(
            1,
            {
                "type": "validation-record",
                "validation": {
                    "id": "probe-1",
                    "artifact": str(outside),
                    "status": "succeeded",
                },
            },
            check=False,
        )
        assert result.returncode == 2
        assert "validation directory" in result.stderr

    def test_completed_task_requires_an_existing_run_local_result(self) -> None:
        self.initialize()
        result = self.audit_event(
            1,
            {
                "type": "task-register",
                "task": {
                    "id": "task-1",
                    "logical_id": "discover-core",
                    "role": "discover",
                    "unit": "shard/core",
                    "status": "running",
                },
            },
        )
        revision = json.loads(result.stdout)["revision"]
        failed = self.audit_event(
            revision,
            {
                "type": "task-transition",
                "task_id": "task-1",
                "status": "completed",
                "result": "areas/missing.json",
            },
            check=False,
        )
        assert failed.returncode == 2
        state = json.loads(
            (self.project_dir / "workflows/gh-audit-repo/current/state.json").read_text()
        )
        assert state["revision"] == revision
        assert state["tasks"]["task-1"]["status"] == "running"

    def test_replacement_attempts_are_consecutively_numbered(self) -> None:
        self.initialize()
        result = self.audit_event(
            1,
            {
                "type": "task-register",
                "task": {
                    "id": "task-1",
                    "logical_id": "discover-core",
                    "role": "discover",
                    "unit": "shard/core",
                    "attempt": 1,
                    "status": "running",
                },
            },
        )
        revision = json.loads(result.stdout)["revision"]
        result = self.audit_event(
            revision,
            {
                "type": "task-transition",
                "task_id": "task-1",
                "status": "failed",
                "error": "worker lost",
            },
        )
        revision = json.loads(result.stdout)["revision"]
        result = self.audit_event(revision, {"type": "integration-start", "task_id": "task-1"})
        revision = json.loads(result.stdout)["revision"]
        result = self.audit_event(revision, {"type": "integration-complete", "task_id": "task-1"})
        revision = json.loads(result.stdout)["revision"]
        skipped = self.audit_event(
            revision,
            {
                "type": "task-register",
                "task": {
                    "id": "task-3",
                    "logical_id": "discover-core",
                    "role": "discover",
                    "unit": "shard/core",
                    "attempt": 3,
                    "status": "running",
                },
            },
            check=False,
        )
        assert skipped.returncode == 2
        accepted = self.audit_event(
            revision,
            {
                "type": "task-register",
                "task": {
                    "id": "task-2",
                    "logical_id": "discover-core",
                    "role": "discover",
                    "unit": "shard/core",
                    "attempt": 2,
                    "status": "running",
                },
            },
        )
        assert json.loads(accepted.stdout)["revision"] > revision

    def test_audit_status_rejects_helper_integrity_drift(self) -> None:
        self.initialize()
        state_path = self.project_dir / "workflows/gh-audit-repo/current/state.json"
        state = json.loads(state_path.read_text())
        state["helper_hashes"] = {}
        state_path.write_text(json.dumps(state) + "\n")
        result = self.call("audit-status", "gh-audit-repo", check=False)
        assert result.returncode == 2
        assert "helpers changed" in result.stderr

    def test_published_candidate_requires_a_mutation_record(self) -> None:
        self.initialize()
        result = self.audit_event(
            1,
            {
                "type": "candidate-upsert",
                "candidate": {
                    "id": "C-1",
                    "status": "published",
                },
            },
        )
        revision = json.loads(result.stdout)["revision"]
        failed = self.call(
            "finalize",
            "gh-audit-repo",
            "--expected-revision",
            str(revision),
            "--status",
            "complete",
            check=False,
        )
        assert failed.returncode == 2
        assert "without mutation records" in failed.stderr

    def test_audit_source_uses_primary_local_head(self) -> None:
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.project)], check=True)
        subprocess.run(
            ["git", "-C", str(self.project), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(["git", "-C", str(self.project), "config", "user.name", "Test"], check=True)
        (self.project / "tracked.txt").write_text("committed\n")
        subprocess.run(["git", "-C", str(self.project), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(self.project), "commit", "-qm", "fixture"], check=True)
        (self.project / "tracked.txt").write_text("modified\n")
        result = subprocess.run(
            [str(HELPER), "audit-source", "--project-root", str(self.project)],
            check=True,
            capture_output=True,
            text=True,
        )
        source = json.loads(result.stdout)
        assert source["branch"] == "main"
        assert source["excluded_dirty_state"]["tracked_entries"] == 1
        assert not source["confirmation_required"]
