from __future__ import annotations

import argparse
import json
import os
import re
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest

from github_workflows import feedback
from github_workflows.cli import run_feedback
from github_workflows.models import (
    RunManageRequest,
    TaskManageRequest,
    WorkflowFeedbackRequest,
)
from github_workflows.runtime import WorkflowRuntime

ROOT = Path(__file__).parents[1]
EXTENSION = ROOT / "extensions" / "github-workflows"


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(path))
    return path


def append_feedback(**overrides: object) -> dict[str, object]:
    values = {
        "message": "The schema rejected a structured report",
        "tool": "task_manage",
        "arguments": {"report": {"status": "complete"}},
        "response": "report must be an object",
        "repository": "example/repo",
        "workflow": "gh-audit-repo",
        "run_id": "run-1",
        "private_paths": [],
    }
    values.update(overrides)
    return feedback.append(**values)  # type: ignore[arg-type]


def test_feedback_is_private_and_sanitized(cache: Path) -> None:
    private = cache.parent / "workspace"
    first = append_feedback(
        message=f"Confusing path {private}",
        arguments={"token": "secret", "path": str(private / "src")},
        private_paths=[(private, "<workspace>")],
    )
    second = append_feedback(
        message=f"Confusing path {private}",
        arguments={"token": "secret", "path": str(private / "src")},
        private_paths=[(private, "<workspace>")],
    )

    records = feedback.read_records()
    assert first["recorded"] is True
    assert second["recorded"] is True
    assert second["feedback_id"] != first["feedback_id"]
    assert re.fullmatch(r"fb-[0-9a-f]{12}", str(first["feedback_id"]))
    assert len(records) == 2
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", records[0]["timestamp"])
    assert records[0]["arguments"] == {
        "token": "<redacted>",
        "path": "<workspace>/src",
    }
    assert records[0]["message"] == "Confusing path <workspace>"
    assert stat.S_IMODE(feedback.storage_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(feedback.storage_path().parent.stat().st_mode) == 0o700


def test_feedback_runtime_infers_one_active_run(cache: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    runtime = WorkflowRuntime(workspace, tmp_path / "project-state")
    runtime.run_manage(
        RunManageRequest(
            action="start",
            workflow="gh-curate-issues",
            repository="example/repo",
        )
    )

    result = runtime.workflow_feedback(
        WorkflowFeedbackRequest(message="The task transition was unclear")
    )
    record = feedback.find(str(result["feedback_id"]))

    assert record["repository"] == "example/repo"
    assert record["workflow"] == "gh-curate-issues"
    assert record["run_id"] == runtime.state("gh-curate-issues")["run_id"]


def test_feedback_attribution_is_null_for_multiple_active_runs(cache: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    runtime = WorkflowRuntime(workspace, tmp_path / "project-state")
    for workflow in ("gh-curate-issues", "gh-implement-issue"):
        runtime.run_manage(
            RunManageRequest(
                action="start",
                workflow=workflow,
                repository="example/repo",
            )
        )

    result = runtime.workflow_feedback(WorkflowFeedbackRequest(message="Ambiguous guidance"))
    record = feedback.find(str(result["feedback_id"]))

    assert record["repository"] == "example/repo"
    assert record["workflow"] is None
    assert record["run_id"] is None


def test_feedback_task_ref_derives_task_provenance(cache: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    runtime = WorkflowRuntime(workspace, tmp_path / "project-state")
    runtime.run_manage(
        RunManageRequest(
            action="start",
            workflow="gh-curate-issues",
            repository="example/repo",
        )
    )
    planned = runtime.task_manage(
        TaskManageRequest.model_validate(
            {
                "action": "plan",
                "workflow": "gh-curate-issues",
                "task": {"logical_id": "review-docs", "role": "review"},
            }
        )
    )

    result = runtime.workflow_feedback(
        WorkflowFeedbackRequest(
            message="The project instruction contradicted the user policy",
            task_ref=planned["task_ref"],
        ),
        provenance={"client": {"name": "qwen-code", "version": "1.2.3"}},
    )
    record = feedback.find(str(result["feedback_id"]))

    assert record["workflow"] == "gh-curate-issues"
    assert record["run_id"] == planned["run_id"]
    assert record["provenance"]["task"] == {"id": planned["task_id"], "role": "review"}
    assert record["provenance"]["client"]["version"] == "1.2.3"


def test_feedback_task_ref_selects_repository_among_multiple_runs(
    cache: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    runtime = WorkflowRuntime(workspace, tmp_path / "project-state")
    references: dict[str, str] = {}
    for workflow, repository in (
        ("gh-curate-issues", "example/curation"),
        ("gh-implement-issue", "example/implementation"),
    ):
        runtime.run_manage(
            RunManageRequest(action="start", workflow=workflow, repository=repository)
        )
        planned = runtime.task_manage(
            TaskManageRequest.model_validate(
                {
                    "action": "plan",
                    "workflow": workflow,
                    "task": {"logical_id": "review-docs", "role": "review"},
                }
            )
        )
        references[workflow] = str(planned["task_ref"])

    result = runtime.workflow_feedback(
        WorkflowFeedbackRequest(
            message="The task instruction was unclear",
            task_ref=references["gh-curate-issues"],
        )
    )

    assert feedback.find(str(result["feedback_id"]))["repository"] == "example/curation"


def test_stale_feedback_task_ref_remains_non_blocking(cache: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    runtime = WorkflowRuntime(workspace, tmp_path / "project-state")
    runtime.run_manage(
        RunManageRequest(
            action="start",
            workflow="gh-curate-issues",
            repository="example/unrelated",
        )
    )

    result = runtime.workflow_feedback(
        WorkflowFeedbackRequest(
            message="The worker instruction was ambiguous",
            task_ref="gh-audit-repo:old-run:discover-core-1",
        )
    )
    record = feedback.find(str(result["feedback_id"]))

    assert record["workflow"] == "gh-audit-repo"
    assert record["run_id"] == "old-run"
    assert record["repository"] is None
    assert record["provenance"]["task"] == {"id": "discover-core-1"}


def test_feedback_ignores_local_path_git_remote(cache: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    runtime = WorkflowRuntime(workspace, tmp_path / "project-state")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "github_workflows.runtime.subprocess.run",
            lambda *args, **kwargs: type("Result", (), {"stdout": "/srv/repos/repo.git\n"})(),
        )
        result = runtime.workflow_feedback(
            WorkflowFeedbackRequest(message="The tool selection was unclear")
        )

    assert feedback.find(str(result["feedback_id"]))["repository"] is None


def test_feedback_rejects_oversized_records_without_creating_a_file(cache: Path) -> None:
    with pytest.raises(ValueError, match="64 KiB"):
        append_feedback(arguments={"payload": "x" * feedback.MAX_RECORD_BYTES})

    assert not feedback.storage_path().exists()


def test_feedback_rejects_symlinked_cache(cache: Path, tmp_path: Path) -> None:
    cache.mkdir()
    target = tmp_path / "other"
    target.mkdir()
    (cache / "agent-workflows").symlink_to(target, target_is_directory=True)

    with pytest.raises(PermissionError):
        append_feedback()


def test_concurrent_feedback_appends_complete_json_lines(cache: Path) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda index: append_feedback(message=f"Confusing behavior {index}"),
                range(12),
            )
        )

    assert all(result["recorded"] for result in results)
    assert len(feedback.read_records()) == 12


def test_failure_registry_sanitizes_bounds_and_expires(tmp_path: Path) -> None:
    private = tmp_path / "workspace"
    clock = 10.0
    registry = feedback.FailureRegistry([(private, "<workspace>")], limit=2, ttl_seconds=5)
    with mock.patch.object(feedback.time, "monotonic", side_effect=lambda: clock):
        first = registry.record(
            tool="run_manage",
            arguments={
                "action": "start",
                "candidate_id": {"body": "private nested content"},
                "instructions": "private user prompt",
                "records": [{"body": "private issue body"}],
                "path": str(private / "src"),
                "token": "private",
            },
            response=f"Failed under {private}",
        )
        stored = registry.resolve(first)
        assert stored is not None
        assert stored["arguments"] == {
            "action": "start",
            "candidate_id": "<dict>",
        }
        assert stored["response"] == "Failed under <workspace>"

        registry.record(tool="run_status", arguments={}, response="failed")
        third = registry.record(tool="task_manage", arguments={}, response="failed")
        assert registry.resolve(first) is None
        assert registry.resolve(third) is not None

        clock = 16.0
        assert registry.resolve(third) is None


def test_failure_registry_omits_oversized_arguments(tmp_path: Path) -> None:
    registry = feedback.FailureRegistry([(tmp_path, "<workspace>")])
    reference = registry.record(
        tool="task_manage",
        arguments={"report": "x" * feedback.MAX_FAILURE_BYTES},
        response="report was rejected",
    )

    stored = registry.resolve(reference)
    assert stored is not None
    assert stored["arguments"] == {"omitted": "automatic failure arguments were not retained"}


def test_failure_registry_bounds_the_complete_snapshot(tmp_path: Path) -> None:
    registry = feedback.FailureRegistry([(tmp_path, "<workspace>")])
    reference = registry.record(
        tool="task_manage",
        arguments={"action": "complete"},
        response="x" * (feedback.MAX_FAILURE_BYTES * 3),
        provenance={"client": {"name": "qwen"}},
    )

    stored = registry.resolve(reference)
    assert stored is not None
    assert feedback._encoded_size(stored) <= feedback.MAX_FAILURE_BYTES
    assert str(stored["response"]).endswith("… <truncated>")


def test_failed_append_restores_previous_file_length(cache: Path) -> None:
    append_feedback(message="Existing feedback")
    path = feedback.storage_path()
    original = path.read_bytes()
    real_write = os.write
    calls = 0

    def interrupted_write(descriptor: int, value: memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(descriptor, value[:10])
        raise OSError("simulated interrupted append")

    with mock.patch.object(feedback.os, "write", side_effect=interrupted_write):
        with pytest.raises(OSError, match="interrupted append"):
            append_feedback(message="Interrupted feedback")

    assert path.read_bytes() == original
    assert [record["message"] for record in feedback.read_records()] == ["Existing feedback"]


def test_reader_waits_for_feedback_writer_lock(cache: Path) -> None:
    append_feedback()
    path = feedback.storage_path()
    started = threading.Event()
    finished = threading.Event()

    def read() -> list[dict[str, object]]:
        started.set()
        records = feedback.read_records()
        finished.set()
        return records

    with ThreadPoolExecutor(max_workers=1) as pool:
        with feedback._locked(path, exclusive=True):
            future = pool.submit(read)
            assert started.wait(timeout=1)
            assert not finished.wait(timeout=0.05)
        assert len(future.result(timeout=1)) == 1


def test_feedback_remove_accepts_exact_and_unique_suffix_ids(cache: Path) -> None:
    first = append_feedback(message="First reviewed item")
    second = append_feedback(message="Keep this item")
    third = append_feedback(message="Third reviewed item")

    removed = feedback.remove([str(first["feedback_id"]), str(third["feedback_id"])[-8:]])

    assert removed == [first["feedback_id"], third["feedback_id"]]
    assert [record["feedback_id"] for record in feedback.read_records()] == [second["feedback_id"]]


def test_feedback_remove_validation_preserves_the_store(cache: Path) -> None:
    append_feedback(message="Keep this item")
    path = feedback.storage_path()
    original = path.read_bytes()

    with pytest.raises(ValueError, match="not found"):
        feedback.remove(["missing-feedback"])

    assert path.read_bytes() == original


def test_failed_feedback_remove_preserves_the_store(cache: Path) -> None:
    result = append_feedback(message="Keep this item")
    path = feedback.storage_path()
    original = path.read_bytes()

    with mock.patch.object(feedback.os, "replace", side_effect=OSError("replace failed")):
        with pytest.raises(OSError, match="replace failed"):
            feedback.remove([str(result["feedback_id"])])

    assert path.read_bytes() == original
    assert feedback.find(str(result["feedback_id"]))["message"] == "Keep this item"


def test_feedback_cli_lists_compact_records_and_shows_context(
    cache: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = append_feedback()
    list_args = argparse.Namespace(
        feedback_command="list",
        repository=None,
        workflow=None,
        tool=None,
        limit=50,
        json_output=False,
    )
    assert run_feedback(list_args) == 0
    table = capsys.readouterr().out
    assert "WHEN (UTC)" in table
    assert "example/repo" in table
    assert "run-1" not in table
    assert "report must be an object" not in table

    list_args.json_output = True
    assert run_feedback(list_args) == 0
    listed = json.loads(capsys.readouterr().out)
    assert "arguments" not in listed[0]
    assert "response" not in listed[0]

    show_args = argparse.Namespace(
        feedback_command="show", feedback_id=str(result["feedback_id"])[-8:]
    )
    assert run_feedback(show_args) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["arguments"] == {"report": {"status": "complete"}}
    assert shown["response"] == "report must be an object"

    remove_args = argparse.Namespace(
        feedback_command="remove", feedback_ids=[str(result["feedback_id"])[-8:]]
    )
    assert run_feedback(remove_args) == 0
    assert capsys.readouterr().out == "Removed 1 feedback record.\n"
    assert feedback.read_records() == []


def test_feedback_table_formats_legacy_ids_and_empty_results() -> None:
    assert feedback.format_table([], width=120) == "No feedback recorded."
    message = "A long explanation " * 20
    table = feedback.format_table(
        [
            {
                "feedback_id": "fb-20260901232545-5f009f5df7",
                "timestamp": "2026-09-01T23:25:45.610861Z",
                "repository": "example/repository-with-a-long-name",
                "workflow": "gh-audit-repo",
                "tool": "glob",
                "message": message,
                "provenance": {"task": {"id": "discover-core-1"}},
            }
        ],
        width=100,
    )

    assert "5f009f5df7" in table
    assert "2026-09-01 23:25:45Z" in table
    assert "example/repository-with-a-long-name" in table
    assert "discover-core" in table
    assert "Summary:" in table
    assert "\n           " in table
    assert " ".join(message.split()) in " ".join(table.split())


def test_feedback_table_escapes_terminal_controls() -> None:
    table = feedback.format_table(
        [
            {
                "feedback_id": "fb-123456789abc",
                "timestamp": "2026-09-01T23:25:45Z",
                "repository": "example/repo",
                "tool": "tool\x1b]8;;https://example.invalid\x07",
                "message": "warning\x1b[2J\x9b31m hidden\u200btext",
            }
        ],
        width=100,
    )

    assert "\x1b" not in table
    assert "\x9b" not in table
    assert "\u200b" not in table
    assert r"\x1b[2J\x9b31m" in table
    assert r"hidden\u200btext" in table


def test_feedback_reader_reports_malformed_record_line(cache: Path) -> None:
    path = feedback.storage_path()
    path.parent.mkdir(parents=True)
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="line 1"):
        feedback.read_records()


def test_workers_receive_only_context_and_feedback_tools() -> None:
    for path in (EXTENSION / "agents").glob("*.md"):
        frontmatter = path.read_text(encoding="utf-8").split("---", 2)[1]
        workflow_tools = {
            line.removeprefix("  - ")
            for line in frontmatter.splitlines()
            if line.startswith("  - mcp__github_workflows__")
        }
        assert workflow_tools == {
            "mcp__github_workflows__task_context",
            "mcp__github_workflows__workflow_feedback",
        }
