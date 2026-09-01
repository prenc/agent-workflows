from __future__ import annotations

import argparse
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest

from github_workflows import feedback
from github_workflows.cli import run_feedback
from github_workflows.models import RunManageRequest, WorkflowFeedbackRequest
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
    assert len(records) == 2
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
    )
    assert run_feedback(list_args) == 0
    listed = json.loads(capsys.readouterr().out)
    assert "arguments" not in listed[0]
    assert "response" not in listed[0]

    show_args = argparse.Namespace(feedback_command="show", feedback_id=result["feedback_id"])
    assert run_feedback(show_args) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["arguments"] == {"report": {"status": "complete"}}
    assert shown["response"] == "report must be an object"


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
