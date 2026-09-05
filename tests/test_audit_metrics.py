from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HELPER = Path(__file__).parents[1] / "src/github_workflows/audit_metrics.py"


class TestAuditMetrics:
    def setup_method(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="audit-metrics-test-")
        root = Path(self.temporary.name).resolve()
        self.project_dir = root / "qwen-project"
        self.run_dir = self.project_dir / "workflows" / "gh-audit-repo" / "current"
        self.run_dir.mkdir(parents=True)
        state = {
            "schema_version": 2,
            "workflow": "gh-audit-repo",
            "run_id": "run-1",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:02:00Z",
            "tasks": {
                "task-1": {"logical_id": "discover-core", "status": "completed"},
                "task-2": {"logical_id": "discover-core", "status": "failed"},
            },
            "candidates": {"C-1": {"status": "published"}},
            "validations": {"probe-1": {"status": "succeeded"}},
            "mutations": [{"candidate_id": "C-1", "action": "create"}],
        }
        (self.run_dir / "state.json").write_text(json.dumps(state) + "\n")
        (self.run_dir / "journal.jsonl").write_text("")
        self.write_agent_log("task-1", registered=True)
        self.write_agent_log("unregistered", registered=False)

    def teardown_method(self) -> None:
        self.temporary.cleanup()

    def write_agent_log(self, agent_id: str, *, registered: bool) -> None:
        directory = self.project_dir / "subagents" / agent_id
        directory.mkdir(parents=True)
        base = directory / f"agent-{agent_id}"
        metadata = {
            "agentId": agent_id,
            "createdAt": "2026-01-01T00:00:10Z",
            "lastUpdatedAt": "2026-01-01T00:01:10Z",
        }
        base.with_suffix(".meta.json").write_text(json.dumps(metadata) + "\n")
        events = [
            {
                "type": "system",
                "subtype": "ui_telemetry",
                "systemPayload": repr(
                    {
                        "uiEvent": {
                            "event.name": "qwen-code.api_response",
                            "input_token_count": 100 if registered else 999,
                            "output_token_count": 20,
                            "total_token_count": 120,
                        }
                    }
                ),
            },
            {
                "type": "system",
                "subtype": "ui_telemetry",
                "systemPayload": repr(
                    {
                        "uiEvent": {
                            "event.name": "qwen-code.tool_call",
                            "function_name": "mcp__context7__query-docs",
                        }
                    }
                ),
            },
            {
                "type": "system",
                "subtype": "ui_telemetry",
                "systemPayload": json.dumps(
                    {
                        "uiEvent": {
                            "event.name": "qwen-code.tool_call",
                            "function_name": "mcp__context7__resolve-library-id",
                        }
                    }
                ),
            },
        ]
        base.with_suffix(".jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))

    def metrics(self) -> dict:
        result = subprocess.run(
            [
                sys.executable,
                str(HELPER),
                "--project-dir",
                str(self.project_dir),
                "--run-dir",
                str(self.run_dir),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    def test_reports_only_registered_task_telemetry(self) -> None:
        metrics = self.metrics()
        assert metrics["wall_seconds"] == 120.0
        assert metrics["registered_logical_tasks"] == 1
        assert metrics["registered_attempts"] == 2
        assert metrics["task_statuses"] == {"completed": 1, "failed": 1}
        assert metrics["recorded_worker_seconds"] == 60.0
        assert metrics["material_active_seconds"] == 60.0
        assert metrics["material_idle_seconds"] == 60.0
        assert metrics["observed_peak_material_concurrency"] == 1
        assert metrics["recovered_logical_tasks"] == 1
        assert metrics["telemetry"]["input_token_count"] == 100
        assert metrics["context7_calls"] == 2
        assert metrics["context7_query_calls"] == 1
        assert metrics["context7_resolution_calls"] == 1
        assert metrics["candidate_statuses"] == {"published": 1}
        assert metrics["validation_records"] == 1
        assert metrics["mutation_records"] == 1

    def test_includes_supervisor_material_intervals_in_concurrency(self) -> None:
        events = [
            {"event": "audit_integration_start", "timestamp": "2026-01-01T00:00:20Z"},
            {"event": "audit_integration_complete", "timestamp": "2026-01-01T00:00:30Z"},
        ]
        (self.run_dir / "journal.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events)
        )
        metrics = self.metrics()
        assert metrics["recorded_supervisor_material_seconds"] == 10.0
        assert metrics["material_active_seconds"] == 60.0
        assert metrics["observed_peak_material_concurrency"] == 2

    def test_rejects_a_noncurrent_run_directory(self) -> None:
        other = self.run_dir.parent / "other"
        other.mkdir()
        result = subprocess.run(
            [
                sys.executable,
                str(HELPER),
                "--project-dir",
                str(self.project_dir),
                "--run-dir",
                str(other),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
