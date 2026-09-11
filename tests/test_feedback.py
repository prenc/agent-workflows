from __future__ import annotations

import argparse
import io
import json
import os
import re
import stat
import subprocess
import threading
import tracemalloc
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest

from github_workflows import feedback
from github_workflows.cli import (
    build_agent_feedback_parser,
    build_parser,
    main,
    run_agent_feedback,
    run_feedback,
)
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
        "origin": {
            "failure_kind": "validation",
            "invocation": {
                "argument_types": {"action": "string", "report": "object"},
                "selectors": {"action": "complete"},
                "omitted": ["report"],
                "complete": False,
            },
        },
        "repository": "example/repo",
        "workflow": "gh-audit-repo",
        "run_id": "run-1",
        "private_paths": [],
    }
    values.update(overrides)
    return feedback.append(**values)  # type: ignore[arg-type]


def task_assignment(runtime: WorkflowRuntime, workflow: str, issue: int = 1) -> dict[str, object]:
    if workflow == "gh-curate-issues":
        artifacts = runtime.current("gh-curate-issues") / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        bundle = artifacts / f"bundle-{issue}.json"
        snapshot = f"artifacts/issue-{issue}.json"
        (artifacts / f"issue-{issue}.json").write_text("{}\n", encoding="utf-8")
        bundle.write_text(
            json.dumps(
                {
                    "selected_issue": {
                        "kind": "issue",
                        "number": issue,
                        "state": "open",
                        "snapshot": snapshot,
                    },
                    "matches": [],
                    "relationships": {},
                    "repository": runtime.state("gh-curate-issues")["repository"],
                    "cutoff": "2025-01-01T00:00:00Z",
                    "watermark": "2026-01-01T00:00:00Z",
                    "default_sha": "a" * 40,
                }
            ),
            encoding="utf-8",
        )
        return {
            "issue": issue,
            "issue_snapshot": snapshot,
            "candidate_bundle": f"artifacts/{bundle.name}",
        }
    return {
        "issues": [{"number": issue, "snapshot": "issue.json", "accepted_scope": "scope"}],
        "pull_request": {"state": "none"},
        "worktree": ".worktrees/unit",
        "branch": "work/unit",
        "rebased_base_sha": "a" * 40,
        "remote_lease": {"state": "absent"},
        "round_objective": "Complete scope",
        "acceptance_condition": "Tests pass",
        "repository_instructions": ["AGENTS.md"],
        "validation_plan": ["pytest"],
        "execution_environment": {"mode": "shared", "pythonpath": ["src"]},
    }


def test_feedback_is_private_and_sanitized(cache: Path) -> None:
    private = cache.parent / "workspace"
    first = append_feedback(
        message=f"Confusing path {private}",
        origin={"error_ref": "err-123456789abc", "path": str(private / "src")},
        private_paths=[(private, "<workspace>")],
    )
    second = append_feedback(
        message=f"Confusing path {private}",
        origin={"error_ref": "err-123456789abc", "path": str(private / "src")},
        private_paths=[(private, "<workspace>")],
    )

    records = feedback.read_records()
    assert first["recorded"] is True
    assert first["ref"] == str(first["feedback_id"])[-8:]
    assert second["recorded"] is True
    assert second["feedback_id"] != first["feedback_id"]
    assert re.fullmatch(r"fb-[0-9a-f]{12}", str(first["feedback_id"]))
    assert len(records) == 2
    assert records[0]["status"] == "open"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", records[0]["timestamp"])
    assert records[0]["origin"]["path"] == "<workspace>/src"
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
                targets=["#1"] if workflow == "gh-implement-issue" else [],
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
                "task": {
                    "logical_id": "review-docs",
                    "role": "review",
                    "assignment": task_assignment(runtime, "gh-curate-issues"),
                },
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
            RunManageRequest(
                action="start",
                workflow=workflow,
                repository=repository,
                targets=["#1"] if workflow == "gh-implement-issue" else [],
            )
        )
        planned = runtime.task_manage(
            TaskManageRequest.model_validate(
                {
                    "action": "plan",
                    "workflow": workflow,
                    "task": {
                        "logical_id": "review-docs",
                        "role": "review",
                        "assignment": task_assignment(runtime, workflow),
                    },
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
            feedback.subprocess,
            "run",
            lambda *args, **kwargs: mock.Mock(returncode=0, stdout="/srv/repos/repo.git\n"),
        )
        result = runtime.workflow_feedback(
            WorkflowFeedbackRequest(message="The tool selection was unclear")
        )

    assert feedback.find(str(result["feedback_id"]))["repository"] is None


def test_feedback_rejects_oversized_records_without_creating_a_file(cache: Path) -> None:
    with pytest.raises(ValueError, match="8 KiB"):
        append_feedback(message="x" * feedback.MAX_RECORD_BYTES)

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


def test_feedback_cli_lists_readable_records_and_shows_context(
    cache: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = append_feedback()
    list_args = build_parser().parse_args(["feedback", "ls"])
    assert run_feedback(list_args) == 0
    table = capsys.readouterr().out
    assert "WHEN (LOCAL)" in table
    assert "example/repo" in table
    assert "run-1" not in table
    assert "Summary: The schema rejected a structured report" in table
    assert "report must be an object" not in table

    list_args.json_output = True
    assert run_feedback(list_args) == 0
    listed = json.loads(capsys.readouterr().out)
    assert "arguments" not in listed[0]
    assert "response" not in listed[0]

    show_args = argparse.Namespace(
        feedback_command="show", feedback_ids=[str(result["feedback_id"])[-8:]]
    )
    assert run_feedback(show_args) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["origin"]["invocation"]["omitted"] == ["report"]

    remove_args = argparse.Namespace(
        feedback_command="remove", feedback_ids=[str(result["feedback_id"])[-8:]]
    )
    assert run_feedback(remove_args) == 0
    assert capsys.readouterr().out == "Removed 1 feedback record.\n"
    assert feedback.read_records() == []


def test_agent_feedback_cli_returns_json_without_format_flags(
    cache: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = append_feedback()
    monkeypatch.setattr(feedback, "repository_from_workspace", lambda _workspace: None)

    assert run_agent_feedback(["ls", "--all"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [record["feedback_id"] for record in listed] == [result["feedback_id"]]

    assert run_agent_feedback(["show", str(result["ref"])]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["feedback_id"] == result["feedback_id"]

    assert run_agent_feedback(["add", "The agent command removed environment selection"]) == 0
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["recorded"] is True
    assert feedback.find(recorded["ref"])["message"] == (
        "The agent command removed environment selection"
    )


def test_agent_feedback_is_a_standalone_command() -> None:
    assert build_agent_feedback_parser().prog == "agent-feedback"
    assert "agent-feedback" not in build_parser().format_help()
    assert build_agent_feedback_parser().parse_args(["trace", "12345678"]).detail == "tools"
    assert (
        build_parser().parse_args(["feedback", "trace", "12345678", "--detail", "data"]).detail
        == "data"
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["list", "--json"],
        ["path"],
        ["remove", "12345678"],
    ],
)
def test_agent_feedback_cli_rejects_non_agent_surface_as_json(
    capsys: pytest.CaptureFixture[str], arguments: list[str]
) -> None:
    assert run_agent_feedback(arguments) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert isinstance(json.loads(captured.err)["error"], str)


def test_agent_feedback_cli_mutations_return_json_receipts(
    cache: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = append_feedback(message="Resolve through the agent interface")

    assert run_agent_feedback(["close", str(result["ref"])]) == 0
    closed = json.loads(capsys.readouterr().out)
    assert closed["changed"] == 1
    assert closed["unchanged"] == 0

    assert run_agent_feedback(["close", str(result["ref"])]) == 0
    unchanged = json.loads(capsys.readouterr().out)
    assert unchanged["changed"] == 0
    assert unchanged["unchanged"] == 1

    assert run_agent_feedback(["reopen", str(result["ref"])]) == 0
    reopened = json.loads(capsys.readouterr().out)
    assert reopened == {"changed": 1, "feedback_ids": [result["feedback_id"]]}


def test_feedback_cli_compact_json_is_bounded_and_metadata_only(
    cache: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = append_feedback(message="word\n\x1b[31m" + "x" * 220)
    append_feedback(message="excluded", tool="web_fetch")
    feedback.set_closed([str(result["feedback_id"])], closed=True, disposition="external")
    args = build_parser().parse_args(
        [
            "feedback",
            "ls",
            "--all",
            "--source",
            "task_manage",
            "--status",
            "all",
            "--json",
        ]
    )

    assert run_feedback(args) == 0

    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1
    assert set(listed[0]) == {
        "ref",
        "feedback_id",
        "timestamp",
        "repository",
        "workflow",
        "source",
        "tool",
        "status",
        "summary",
        "disposition",
    }
    assert listed[0]["feedback_id"] == result["feedback_id"]
    assert len(listed[0]["summary"]) <= 160
    assert "\n" not in listed[0]["summary"]
    assert "\x1b" not in listed[0]["summary"]
    assert listed[0]["summary"].endswith("…")
    assert listed[0]["disposition"] == "external"

    args.json_output = False
    assert run_feedback(args) == 0
    rendered = capsys.readouterr().out.rstrip("\n")
    assert len(rendered.splitlines()) > 3
    assert "Summary: word" in rendered
    assert "x" * 220 in rendered
    assert re.search(r"\b\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\b", rendered)


def test_feedback_summary_and_list_have_distinct_complete_views(
    cache: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = append_feedback(tool="task_context", message="First open item")
    second = append_feedback(tool="mcp__github_workflows__task_context", message="Second open item")
    closed = append_feedback(tool="web_fetch", message="Closed item")
    feedback.set_closed([str(closed["feedback_id"])], closed=True)

    summary = feedback.feedback_summary()
    assert summary["open"] == {
        "records": 2,
        "sources": [{"source": "task_context", "records": 2}],
    }
    assert summary["closed"] == {
        "records": 1,
        "sources": [
            {
                "source": "web_fetch",
                "records": 1,
                "dispositions": [{"disposition": "addressed", "records": 1}],
            }
        ],
    }
    assert summary["range"]["oldest"] is not None
    assert summary["storage"]["bytes"] > 0

    args = build_parser().parse_args(["feedback", "ls", "--all", "--json"])
    assert run_feedback(args) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert [record["feedback_id"] for record in rendered] == [
        second["feedback_id"],
        first["feedback_id"],
    ]

    show = build_parser().parse_args(["feedback", "show", str(first["ref"]), str(second["ref"])])
    assert run_feedback(show) == 0
    shown = json.loads(capsys.readouterr().out)
    assert [record["feedback_id"] for record in shown] == [
        first["feedback_id"],
        second["feedback_id"],
    ]

    human_args = build_parser().parse_args(["feedback", "summary"])
    assert run_feedback(human_args) == 0
    human = capsys.readouterr().out
    assert "Records: 3 total" in human
    assert "Open (2)" in human
    assert "Closed (1)" in human
    assert "addressed=1" in human
    assert "task_context" in human

    parser = build_parser()
    for arguments in (
        ["--closed"],
        ["--tool", "task_context"],
        ["--compact"],
        ["--cutoff", "2026-09-01"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(["feedback", "ls", *arguments])
    with pytest.raises(SystemExit) as help_exit:
        parser.parse_args(["feedback", "ls", "--help"])
    assert help_exit.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "50 newest open records" in help_text
    assert "default: 50" in help_text
    assert "default: open" in help_text
    assert "readable record blocks with complete summaries" in help_text
    assert "agent-workflows feedback show REF" in help_text


def test_feedback_short_refs_are_unique_at_creation(cache: Path) -> None:
    generated = [
        mock.Mock(hex="0000aaaaaaaa00000000000000000000"),
        mock.Mock(hex="1111aaaaaaaa00000000000000000000"),
        mock.Mock(hex="2222bbbbbbbb00000000000000000000"),
    ]
    with mock.patch.object(feedback.uuid, "uuid4", side_effect=generated):
        first = append_feedback(message="First item")
        second = append_feedback(message="Second item")

    assert first["ref"] == "aaaaaaaa"
    assert second["ref"] == "bbbbbbbb"
    assert feedback.find(str(first["ref"]))["feedback_id"] == first["feedback_id"]
    assert feedback.find(str(second["ref"]))["feedback_id"] == second["feedback_id"]


def test_feedback_short_refs_lengthen_for_existing_collisions() -> None:
    records = [
        {"feedback_id": "fb-0000aaaaaaaa"},
        {"feedback_id": "fb-1111aaaaaaaa"},
    ]

    assert feedback.feedback_ref("fb-0000aaaaaaaa", records) == "0aaaaaaaa"
    assert feedback.feedback_ref("fb-1111aaaaaaaa", records) == "1aaaaaaaa"


def test_filtered_listing_uses_globally_unambiguous_refs(cache: Path) -> None:
    path = feedback.storage_path()
    path.parent.mkdir(parents=True)
    records = [
        {
            "feedback_id": "fb-0000aaaaaaaa",
            "timestamp": "2026-09-01T00:00:00Z",
            "status": "open",
            "message": "Older",
            "tool": "first",
        },
        {
            "feedback_id": "fb-1111aaaaaaaa",
            "timestamp": "2026-09-02T00:00:00Z",
            "status": "open",
            "message": "Newer",
            "tool": "second",
        },
    ]
    feedback._rewrite(path, records)

    listed = feedback.compact_records(sources=["second"], limit=1)

    assert listed[0]["ref"] == "1aaaaaaaa"
    assert feedback.find(listed[0]["ref"])["feedback_id"] == "fb-1111aaaaaaaa"
    assert feedback.set_closed([listed[0]["ref"]], closed=True) == ["fb-1111aaaaaaaa"]


def test_feedback_cli_add_derives_and_sanitizes_context(
    cache: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(
        feedback,
        "repository_from_workspace",
        lambda _workspace: "example/project",
    )
    message = f"The command exposed {workspace} and {feedback.storage_path().parent}"
    args = build_parser().parse_args(["feedback", "add", message, "--tool", "run_shell_command"])

    assert run_feedback(args) == 0

    output = capsys.readouterr().out
    feedback_reference = re.search(r"\b[0-9a-f]{8}\b", output)
    assert feedback_reference is not None
    record = feedback.find(feedback_reference.group())
    assert record["repository"] == "example/project"
    assert record["workflow"] is None
    assert record["run_id"] is None
    assert record["tool"] == "run_shell_command"
    assert record["message"] == "The command exposed <workspace> and <feedback-cache>"
    assert record["origin"] == {"failure_kind": "manual"}
    assert record["provenance"] == {"client": {"name": "agent-workflows-cli"}}


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/example/project.git", "example/project"),
        ("git@github.com:example/project.git", "example/project"),
        ("/srv/repos/project.git", None),
        ("", None),
    ],
)
def test_feedback_repository_attribution_accepts_only_remote_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote: str,
    expected: str | None,
) -> None:
    monkeypatch.setattr(
        feedback.subprocess,
        "run",
        lambda *args, **kwargs: mock.Mock(returncode=0, stdout=remote),
    )

    assert feedback.repository_from_workspace(tmp_path) == expected


def timed_out_run(*args: object, **kwargs: object) -> object:
    raise subprocess.TimeoutExpired(cmd=list(args[0]), timeout=kwargs["timeout"])  # type: ignore[index]


def test_feedback_git_remote_timeout_degrades_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feedback.subprocess, "run", timed_out_run)

    assert feedback.repository_from_workspace(tmp_path) is None


def test_feedback_transcript_search_timeout_reports_typed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "projects").mkdir()
    monkeypatch.setattr(feedback.subprocess, "run", timed_out_run)

    with pytest.raises(RuntimeError, match="transcript search timed out"):
        feedback._candidate_transcripts("fb-000000000001", tmp_path)


def test_feedback_trace_reports_transcript_timeout(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = append_feedback(origin=None, provenance=None)
    (tmp_path / "qwen-home" / "projects" / "workspace" / "chats").mkdir(parents=True)
    monkeypatch.setenv("QWEN_HOME", str(tmp_path / "qwen-home"))
    monkeypatch.setattr(feedback.subprocess, "run", timed_out_run)

    with pytest.raises(RuntimeError, match="transcript search timed out"):
        feedback.trace(str(result["feedback_id"]))


def test_feedback_fallback_transcript_scan_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feedback_id = "fb-000000000001"
    chats = tmp_path / "projects" / "workspace" / "chats"
    chats.mkdir(parents=True)
    all_files = []
    for index in range(3):
        path = chats / f"t{index}.jsonl"
        path.write_text(json.dumps({"message": feedback_id}) + "\n", encoding="utf-8")
        all_files.append(path)

    def missing(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("rg")

    monkeypatch.setattr(feedback.subprocess, "run", missing)

    assert set(feedback._candidate_transcripts(feedback_id, tmp_path)) == set(all_files)

    monkeypatch.setattr(feedback, "FALLBACK_SCAN_FILE_LIMIT", 2)
    with pytest.warns(UserWarning, match="stopped after"):
        bounded = feedback._candidate_transcripts(feedback_id, tmp_path)

    assert len(bounded) == 2
    assert set(bounded) <= set(all_files)


@pytest.mark.parametrize("message", ["", " " * 3, "x" * 2001])
def test_feedback_cli_add_validates_the_message(cache: Path, message: str) -> None:
    args = build_parser().parse_args(["feedback", "add", message])

    with pytest.raises(
        ValueError,
        match=r"at least 1 character|text must not be blank|at most 2000 characters",
    ):
        run_feedback(args)

    assert not feedback.storage_path().exists()


def test_feedback_cli_closes_filters_and_reopens_records(
    cache: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = append_feedback(tool="task_context", message="Reviewed item")
    second = append_feedback(tool="web_fetch", message="Pending item")

    close_args = build_parser().parse_args(["feedback", "close", str(first["feedback_id"])[-8:]])
    assert run_feedback(close_args) == 0
    assert capsys.readouterr().out == "Closed 1 feedback record.\n"
    closed_record = feedback.find(str(first["feedback_id"]))
    assert closed_record["status"] == "closed"
    assert closed_record["resolution"] == {"disposition": "addressed"}
    assert closed_record["closed_at"].endswith("Z")
    assert [record["feedback_id"] for record in feedback.compact_records()] == [
        second["feedback_id"]
    ]

    closed_args = build_parser().parse_args(["feedback", "ls", "--status", "closed", "--json"])
    assert run_feedback(closed_args) == 0
    closed = json.loads(capsys.readouterr().out)
    assert [record["feedback_id"] for record in closed] == [first["feedback_id"]]
    summary = feedback.feedback_summary()
    assert summary["open"] == {
        "records": 1,
        "sources": [{"source": "web_fetch", "records": 1}],
    }
    assert summary["closed"] == {
        "records": 1,
        "sources": [
            {
                "source": "task_context",
                "records": 1,
                "dispositions": [{"disposition": "addressed", "records": 1}],
            }
        ],
    }

    reopen_args = build_parser().parse_args(["feedback", "reopen", str(first["feedback_id"])])
    assert run_feedback(reopen_args) == 0
    assert capsys.readouterr().out == "Reopened 1 feedback record.\n"
    reopened = feedback.find(str(first["feedback_id"]))
    assert "resolution" not in reopened
    assert "closed_at" not in reopened
    assert [record["feedback_id"] for record in feedback.compact_records()] == [
        second["feedback_id"],
        first["feedback_id"],
    ]


def test_feedback_close_records_requested_disposition_and_note(cache: Path) -> None:
    result = append_feedback()

    changed = feedback.set_closed(
        [str(result["feedback_id"])],
        closed=True,
        disposition="external",
        note="Requires an upstream client fix",
    )

    assert changed == [result["feedback_id"]]
    record = feedback.find(str(result["feedback_id"]))
    assert record["resolution"] == {
        "disposition": "external",
        "note": "Requires an upstream client fix",
    }


@pytest.mark.parametrize(
    ("disposition", "note"),
    [
        ("external", None),
        ("addressed", "Second review note"),
    ],
)
def test_feedback_close_rejects_conflicting_resolution_on_both_surfaces(
    cache: Path, disposition: str, note: str | None
) -> None:
    result = append_feedback(message="Close once, reject conflicting re-closes")
    feedback.resolve_records(
        [{"ref": result["ref"], "disposition": "addressed", "note": "First review note"}]
    )
    path = feedback.storage_path()
    stored = path.read_bytes()
    original_closed_at = feedback.find(str(result["feedback_id"]))["closed_at"]

    # Positional close rejects a conflicting disposition or note.
    with pytest.raises(ValueError, match="different resolution; reopen it first"):
        feedback.set_closed([result["ref"]], closed=True, disposition=disposition, note=note)
    assert path.read_bytes() == stored

    # The CLI positional surface reports the identical shared rule.
    cli_command = ["feedback", "close", result["ref"], "--disposition", disposition]
    if note is not None:
        cli_command.extend(["--note", note])
    with pytest.raises(ValueError, match="different resolution; reopen it first"):
        run_feedback(build_parser().parse_args(cli_command))
    assert path.read_bytes() == stored

    # The --input surface rejects the same mutation with the identical rule.
    input_item: dict[str, str] = {"ref": result["ref"], "disposition": disposition}
    if note is not None:
        input_item["note"] = note
    with pytest.raises(ValueError, match="different resolution; reopen it first"):
        feedback.resolve_records([input_item])
    assert path.read_bytes() == stored

    unchanged = feedback.find(str(result["feedback_id"]))
    assert unchanged["status"] == "closed"
    assert unchanged["resolution"] == {
        "disposition": "addressed",
        "note": "First review note",
    }
    assert unchanged["closed_at"] == original_closed_at

    # Re-closing with the identical resolution remains a no-op and keeps closed_at.
    assert (
        feedback.set_closed(
            [result["ref"]],
            closed=True,
            disposition="addressed",
            note="First review note",
        )
        == []
    )
    assert feedback.find(str(result["feedback_id"]))["closed_at"] == original_closed_at

    # The documented reopen-first flow recovers the conflicting close.
    feedback.set_closed([result["ref"]], closed=False)
    expected_resolution: dict[str, str] = {"disposition": disposition}
    if note is not None:
        expected_resolution["note"] = note
    assert feedback.set_closed(
        [result["ref"]], closed=True, disposition=disposition, note=note
    ) == [result["feedback_id"]]
    assert feedback.find(str(result["feedback_id"]))["resolution"] == expected_resolution


def test_feedback_resolve_applies_mixed_dispositions_atomically(cache: Path) -> None:
    first = append_feedback(message="Local correction")
    second = append_feedback(message="Upstream limitation")

    with mock.patch.object(feedback, "_rewrite", wraps=feedback._rewrite) as rewrite:
        result = feedback.resolve_records(
            [
                {
                    "ref": first["ref"],
                    "disposition": "addressed",
                    "note": "Validated local correction",
                },
                {"ref": second["ref"], "disposition": "external"},
            ]
        )
    assert rewrite.call_count == 1

    assert result["changed"] == 2
    assert result["unchanged"] == 0
    assert [item["ref"] for item in result["resolved"]] == [first["ref"], second["ref"]]
    assert feedback.find(str(first["ref"]))["resolution"] == {
        "disposition": "addressed",
        "note": "Validated local correction",
    }
    assert feedback.find(str(second["ref"]))["resolution"] == {"disposition": "external"}

    repeated = feedback.resolve_records(
        [
            {
                "ref": first["ref"],
                "disposition": "addressed",
                "note": "Validated local correction",
            }
        ]
    )
    assert repeated["changed"] == 0
    assert repeated["unchanged"] == 1


@pytest.mark.parametrize(
    ("resolutions", "expected"),
    [
        ([{"ref": "missing1", "disposition": "addressed"}], "not found"),
        ([{"ref": "12345678", "disposition": "unsupported"}], "disposition"),
        ([{"ref": "12345678", "disposition": "addressed", "note": " "}], "not be blank"),
    ],
)
def test_feedback_resolve_validation_preserves_store(
    cache: Path, resolutions: list[dict[str, str]], expected: str
) -> None:
    append_feedback(message="Keep unchanged")
    path = feedback.storage_path()
    original = path.read_bytes()

    with pytest.raises(ValueError, match=expected):
        feedback.resolve_records(resolutions)

    assert path.read_bytes() == original


def test_feedback_resolve_rejects_duplicate_and_conflicting_records(cache: Path) -> None:
    result = append_feedback(message="One decision")
    duplicate = [
        {"ref": result["ref"], "disposition": "addressed"},
        {"ref": result["feedback_id"], "disposition": "addressed"},
    ]
    path = feedback.storage_path()
    original = path.read_bytes()
    with pytest.raises(ValueError, match="duplicate"):
        feedback.resolve_records(duplicate)
    assert path.read_bytes() == original

    feedback.resolve_records([{"ref": result["ref"], "disposition": "addressed"}])
    resolved = path.read_bytes()
    with pytest.raises(ValueError, match="different resolution"):
        feedback.resolve_records([{"ref": result["ref"], "disposition": "external"}])
    assert path.read_bytes() == resolved


def test_feedback_close_cli_accepts_resolution_list(
    cache: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = append_feedback(message="Resolve through CLI")
    request = json.dumps([{"ref": result["ref"], "disposition": "not-actionable"}])
    args = build_parser().parse_args(["feedback", "close", "--input", request, "--json"])

    assert run_feedback(args) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["changed"] == 1
    assert output["resolved"][0]["ref"] == result["ref"]

    conflicting = build_parser().parse_args(
        ["feedback", "close", str(result["ref"]), "--input", request]
    )
    with pytest.raises(ValueError, match="cannot be combined"):
        run_feedback(conflicting)

    wrapped = build_parser().parse_args(
        [
            "feedback",
            "close",
            "--input",
            json.dumps({"resolutions": [{"ref": result["ref"], "disposition": "addressed"}]}),
        ]
    )
    with pytest.raises(ValueError, match="resolutions must be a non-empty array"):
        run_feedback(wrapped)


def test_feedback_close_cli_accepts_inline_json_longer_than_a_filename(
    cache: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = append_feedback(message="Resolve a large atomic request")
    note = ("validated context " * 20).strip()
    request = json.dumps([{"ref": result["ref"], "disposition": "addressed", "note": note}])
    assert len(request) > 255
    args = build_parser().parse_args(["feedback", "close", "--input", request, "--json"])

    assert run_feedback(args) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["changed"] == 1
    assert feedback.find(str(result["ref"]))["resolution"]["note"] == note


def test_feedback_legacy_payloads_are_scrubbed_atomically(cache: Path) -> None:
    path = feedback.storage_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "feedback_id": "fb-123456789abc",
                "timestamp": "2026-09-02T12:00:00Z",
                "message": "The tool rejected an object",
                "tool": "task_manage",
                "arguments": {
                    "action": "complete",
                    "report": {"patient_name": "Example Person"},
                },
                "response": "Example Person was rejected",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    record = feedback.read_records()[0]

    assert record["status"] == "open"
    assert record["origin"] == {
        "failure_kind": "legacy",
        "invocation": {
            "argument_types": {"action": "string", "report": "object"},
            "selectors": {"action": "complete"},
            "omitted": ["report"],
            "complete": False,
        },
    }
    stored = path.read_text(encoding="utf-8")
    assert "Example Person" not in stored
    assert '"arguments"' not in stored
    assert '"response"' not in stored


def test_feedback_summary_supports_inclusive_normalized_cutoff(cache: Path) -> None:
    first = append_feedback(message="First")
    second = append_feedback(message="Second")
    feedback.set_closed([str(first["feedback_id"])], closed=True)
    records = feedback.read_records()
    records[0]["timestamp"] = "2026-09-01T12:00:00Z"
    records[1]["timestamp"] = "2026-09-02T12:00:00Z"
    feedback._rewrite(feedback.storage_path(), records)

    summary = feedback.feedback_summary(cutoff="2026-09-02T08:00:00-04:00")

    assert summary["scope"]["cutoff"] == "2026-09-02T12:00:00Z"
    assert summary["open"]["records"] == 1
    assert summary["closed"]["records"] == 0
    assert summary["storage"] == {
        "bytes": feedback._encoded_size(records[1]),
        "average_record_bytes": feedback._encoded_size(records[1]),
        "largest_record_bytes": feedback._encoded_size(records[1]),
    }
    assert (
        feedback.compact_records(status="all", cutoff="2026-09-02T12:00:00", limit=None)[0][
            "feedback_id"
        ]
        == second["feedback_id"]
    )
    empty_summary = feedback.feedback_summary(cutoff="2027-01-01")
    assert empty_summary["range"] == {
        "oldest": None,
        "newest": None,
    }
    assert empty_summary["storage"] == {
        "bytes": 0,
        "average_record_bytes": 0,
        "largest_record_bytes": 0,
    }
    with pytest.raises(ValueError, match="ISO-8601"):
        feedback.feedback_summary(cutoff="yesterday")


def test_feedback_invalid_timestamp_is_quarantined_without_blocking_summary(cache: Path) -> None:
    path = feedback.storage_path()
    path.parent.mkdir(parents=True)
    good = {
        "feedback_id": "fb-000000000001",
        "timestamp": "2026-09-02T12:00:00Z",
        "status": "open",
        "message": "Readable record",
        "tool": "task_manage",
    }
    bad = {
        "feedback_id": "fb-000000000002",
        "timestamp": "not-a-date",
        "status": "open",
        "message": "Blocked-looking record",
        "tool": "task_manage",
    }
    path.write_text(json.dumps(good) + "\n" + json.dumps(bad) + "\n", encoding="utf-8")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        records = feedback.read_records()
        summary = feedback.feedback_summary()
        listed = feedback.list_records(cutoff="2026-09-02T00:00:00Z", status="all")
        compact = feedback.compact_records(cutoff="2026-09-02T00:00:00Z", status="all")

    assert [record["feedback_id"] for record in records] == ["fb-000000000001"]
    assert any(
        "fb-000000000002" in str(warning.message) and "invalid timestamp" in str(warning.message)
        for warning in caught
    )
    assert summary["open"]["records"] == 1
    assert summary["range"] == {"oldest": "2026-09-02T12:00:00Z", "newest": "2026-09-02T12:00:00Z"}
    assert [record["feedback_id"] for record in listed] == ["fb-000000000001"]
    assert [item["feedback_id"] for item in compact] == ["fb-000000000001"]


@pytest.mark.parametrize("timestamp", ["not-a-date", 1234567890, None])
def test_feedback_non_parseable_timestamp_variants_are_quarantined(
    cache: Path, timestamp: object
) -> None:
    path = feedback.storage_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "feedback_id": "fb-000000000003",
                "timestamp": timestamp,
                "status": "open",
                "message": "Unreadable timestamp",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match=r"fb-000000000003.*invalid timestamp"):
        assert feedback.read_records() == []

    assert feedback.feedback_summary()["open"]["records"] == 0
    assert feedback.feedback_summary()["range"] == {"oldest": None, "newest": None}


def test_feedback_record_time_error_identifies_the_record() -> None:
    with pytest.raises(ValueError, match="fb-000000000004 has an invalid timestamp"):
        feedback._record_time({"feedback_id": "fb-000000000004", "timestamp": "not-a-date"})


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        ("30m", "2026-09-08T11:30:00Z"),
        ("24h", "2026-09-07T12:00:00Z"),
        ("30d", "2026-08-09T12:00:00Z"),
        ("4w", "2026-08-11T12:00:00Z"),
    ],
)
def test_feedback_relative_cutoff_accepts_compact_ages(age: str, expected: str) -> None:
    now = feedback.dt.datetime(2026, 9, 8, 12, tzinfo=feedback.dt.UTC)
    assert feedback.relative_cutoff(age, now=now) == expected


@pytest.mark.parametrize("age", ["", "0d", "-1d", "1month", "yesterday", "1000000000d"])
def test_feedback_relative_cutoff_rejects_ambiguous_ages(age: str) -> None:
    with pytest.raises(ValueError, match="positive age"):
        feedback.relative_cutoff(age)


def test_feedback_list_and_summary_expose_relative_since_only() -> None:
    parser = build_parser()
    assert parser.parse_args(["feedback", "ls", "--since", "30d"]).since == "30d"
    assert parser.parse_args(["feedback", "summary", "--since", "4w"]).since == "4w"


def test_feedback_cli_replaces_stats_and_sources_with_summary() -> None:
    parser = build_parser()
    assert parser.parse_args(["feedback", "summary"]).feedback_command == "summary"
    with pytest.raises(SystemExit):
        parser.parse_args(["feedback", "stats"])
    with pytest.raises(SystemExit):
        parser.parse_args(["feedback", "sources"])


def test_feedback_trace_uses_durable_locator_and_progressive_detail(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = append_feedback(
        tool="task_manage",
        origin=None,
        provenance={
            "conversation": {
                "client": "qwen",
                "session_id": "session-1",
                "prompt_id": "prompt-1",
                "mcp_request_id": "request-1",
            }
        },
    )
    feedback_id = str(result["feedback_id"])
    qwen_home = tmp_path / "qwen-home"
    transcript = (
        qwen_home
        / "projects"
        / "-workspace"
        / "subagents"
        / "session-1"
        / "agent-worker-call.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    rows = [
        {
            "type": "user",
            "timestamp": "2026-09-02T11:59:58Z",
            "message": {"role": "user", "parts": [{"text": "Complete the report"}]},
        },
        {
            "type": "assistant",
            "message": {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {
                            "id": "protected-call",
                            "name": "read_file",
                            "args": {"file_path": "/workspace/data/private.csv"},
                        }
                    }
                ],
            },
        },
        {
            "type": "tool_result",
            "timestamp": "2026-09-02T11:59:58Z",
            "message": {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "id": "protected-call",
                            "name": "read_file",
                            "response": {"output": "private", "token": "secret"},
                        }
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-09-02T11:59:59Z",
            "message": {
                "role": "model",
                "parts": [
                    {"thought": "private chain of thought"},
                    {"text": "I will submit the report."},
                    {
                        "functionCall": {
                            "id": "origin-call",
                            "name": "mcp__github_workflows__task_manage",
                            "args": {"action": "complete", "report": "wrong type"},
                        }
                    },
                ],
            },
        },
        {
            "type": "tool_result",
            "timestamp": "2026-09-02T12:00:00Z",
            "sessionId": "session-1",
            "agentId": "worker-call",
            "message": {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "id": "origin-call",
                            "name": "mcp__github_workflows__task_manage",
                            "response": {
                                "output": "report must be an object\x1b[2J\x9b31m",
                                "isError": True,
                            },
                        }
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {
                            "id": "feedback-call",
                            "name": "mcp__github_workflows__workflow_feedback",
                            "args": {"message": "The report type was unclear"},
                        }
                    }
                ],
            },
        },
        {
            "type": "tool_result",
            "timestamp": "2026-09-02T12:00:01Z",
            "sessionId": "session-1",
            "agentId": "worker-call",
            "message": {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "id": "feedback-call",
                            "name": "mcp__github_workflows__workflow_feedback",
                            "response": {
                                "output": json.dumps({"recorded": True, "feedback_id": feedback_id})
                            },
                        }
                    }
                ],
            },
        },
    ]
    transcript.write_text(
        "\n".join(json.dumps(row) for row in rows) + '\n{"type":"partial"',
        encoding="utf-8",
    )
    incidental = qwen_home / "projects" / "-workspace" / "chats" / "incidental.jsonl"
    incidental.parent.mkdir(parents=True)
    incidental.write_text(json.dumps({"message": feedback_id}) + "\n", encoding="utf-8")
    monkeypatch.setenv("QWEN_HOME", str(qwen_home))

    attached = feedback.attach_qwen_locator(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "mcp__github_workflows__workflow_feedback",
            "session_id": "session-1",
            "transcript_path": str(transcript),
            "agent_id": "worker-call",
            "tool_use_id": "tool-use-1",
            "tool_call_id": "feedback-call",
            "tool_response": {"structuredContent": {"feedback_id": feedback_id}},
        }
    )
    assert attached == {"attached": True, "feedback_id": feedback_id}
    assert feedback.find(feedback_id)["provenance"]["conversation"] == {
        "client": "qwen",
        "session_id": "session-1",
        "prompt_id": "prompt-1",
        "mcp_request_id": "request-1",
        "transcript": (
            "$QWEN_HOME/projects/-workspace/subagents/session-1/agent-worker-call.jsonl"
        ),
        "agent_id": "worker-call",
        "tool_use_id": "tool-use-1",
        "tool_call_id": "feedback-call",
    }

    with mock.patch.object(feedback, "_candidate_transcripts", side_effect=AssertionError):
        traced = feedback.trace(feedback_id)

    assert traced == {
        "feedback_id": feedback_id,
        "detail": "tools",
        "matches": [
            {
                "timestamp": "2026-09-02T12:00:01Z",
                "session_id": "session-1",
                "prompt_id": "prompt-1",
                "agent_id": "worker-call",
                "mcp_request_id": "request-1",
                "feedback_tool_use_id": "tool-use-1",
                "feedback_tool_call_id": "feedback-call",
                "transcript": (
                    "$QWEN_HOME/projects/-workspace/subagents/session-1/agent-worker-call.jsonl"
                ),
                "tools": [
                    {
                        "tool": "read_file",
                        "tool_call_id": "protected-call",
                        "timestamp": "2026-09-02T11:59:58Z",
                        "status": "success",
                    },
                    {
                        "tool": "mcp__github_workflows__task_manage",
                        "tool_call_id": "origin-call",
                        "timestamp": "2026-09-02T12:00:00Z",
                        "status": "error",
                    },
                ],
            }
        ],
    }
    contextual = feedback.trace(feedback_id, detail="context")
    assert contextual["matches"][0]["messages"] == [
        {
            "role": "user",
            "timestamp": "2026-09-02T11:59:58Z",
            "text": "Complete the report",
        },
        {
            "role": "assistant",
            "timestamp": "2026-09-02T11:59:59Z",
            "text": "I will submit the report.",
        },
    ]
    assert "private chain of thought" not in json.dumps(contextual)
    detailed = feedback.trace(feedback_id, detail="data")
    protected = detailed["matches"][0]["tools"][0]
    omitted = {"omitted": "interaction references a protected path"}
    assert protected["input"] == omitted
    assert protected["response"] == omitted
    assert "private" not in json.dumps(protected)
    assert detailed["matches"][0]["tools"][1]["input"] == {
        "action": "complete",
        "report": "wrong type",
    }
    assert detailed["matches"][0]["tools"][1]["response"] == {
        "output": "report must be an object\x1b[2J\x9b31m",
        "isError": True,
    }
    rendered = feedback.format_trace(detailed)
    assert "\x1b" not in rendered
    assert "\x9b" not in rendered
    assert r"\u001b[2J\u009b31m" in rendered


def test_feedback_locator_rejects_wrong_session_and_non_qwen_path(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = append_feedback(
        origin=None,
        provenance={
            "conversation": {
                "client": "qwen",
                "session_id": "expected-session",
                "prompt_id": "prompt-1",
            }
        },
    )
    qwen_home = tmp_path / "qwen-home"
    transcript = qwen_home / "projects" / "workspace" / "chats" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("QWEN_HOME", str(qwen_home))
    base = {
        "hook_event_name": "PostToolUse",
        "tool_name": "mcp__github_workflows__workflow_feedback",
        "session_id": "expected-session",
        "tool_use_id": "tool-use-1",
        "tool_response": {"output": json.dumps({"feedback_id": result["feedback_id"]})},
    }

    with pytest.raises(ValueError, match="under QWEN_HOME"):
        feedback.attach_qwen_locator({**base, "transcript_path": str(outside)})
    with pytest.raises(ValueError, match="does not match"):
        feedback.attach_qwen_locator(
            {**base, "session_id": "wrong-session", "transcript_path": str(transcript)}
        )


def test_private_feedback_locator_hook_command(
    cache: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = append_feedback(origin=None, provenance=None)
    qwen_home = tmp_path / "qwen-home"
    transcript = qwen_home / "projects" / "workspace" / "chats" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "mcp__github_workflows__workflow_feedback",
        "session_id": "session-1",
        "transcript_path": str(transcript),
        "tool_use_id": "tool-use-1",
        "tool_response": {"content": [{"text": json.dumps(result)}]},
    }
    monkeypatch.setenv("QWEN_HOME", str(qwen_home))
    monkeypatch.setattr("sys.argv", ["agent-workflows", "_feedback-locator-hook"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    assert main() == 0
    assert capsys.readouterr().out == "{}\n"
    assert feedback.find(str(result["feedback_id"]))["provenance"]["conversation"] == {
        "client": "qwen",
        "session_id": "session-1",
        "transcript": "$QWEN_HOME/projects/workspace/chats/session.jsonl",
        "tool_use_id": "tool-use-1",
    }


def test_feedback_trace_falls_back_for_legacy_unlinked_record(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = append_feedback(origin=None, provenance=None)
    feedback_id = str(result["feedback_id"])
    qwen_home = tmp_path / "qwen-home"
    transcript = qwen_home / "projects" / "workspace" / "chats" / "legacy.jsonl"
    transcript.parent.mkdir(parents=True)
    rows = [
        {
            "message": {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {
                            "id": "feedback-call",
                            "name": "mcp__github_workflows__workflow_feedback",
                            "args": {"message": "Legacy feedback"},
                        }
                    }
                ],
            }
        },
        {
            "timestamp": "2026-09-02T12:00:01Z",
            "sessionId": "legacy-session",
            "message": {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "id": "feedback-call",
                            "name": "mcp__github_workflows__workflow_feedback",
                            "response": {"output": json.dumps({"feedback_id": feedback_id})},
                        }
                    }
                ],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    monkeypatch.setenv("QWEN_HOME", str(qwen_home))

    traced = feedback.trace(feedback_id)

    assert traced["matches"][0]["session_id"] == "legacy-session"
    assert traced["matches"][0]["transcript"] == (
        "$QWEN_HOME/projects/workspace/chats/legacy.jsonl"
    )


def _write_synthetic_transcript(path: Path, feedback_id: str, filler_rows: int) -> None:
    """Write a transcript whose feedback call sits near the start, padded after."""
    call = {
        "type": "assistant",
        "message": {
            "role": "model",
            "parts": [
                {
                    "functionCall": {
                        "id": "feedback-call",
                        "name": "mcp__github_workflows__workflow_feedback",
                        "args": {"message": "The probe was unclear"},
                    }
                }
            ],
        },
    }
    result = {
        "type": "tool_result",
        "timestamp": "2026-09-02T12:00:01Z",
        "sessionId": "memory-session",
        "message": {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "id": "feedback-call",
                        "name": "mcp__github_workflows__workflow_feedback",
                        "response": {
                            "output": json.dumps({"recorded": True, "feedback_id": feedback_id})
                        },
                    }
                }
            ],
        },
    }
    filler = json.dumps(
        {
            "type": "user",
            "timestamp": "2026-09-02T11:00:00Z",
            "message": {"role": "user", "parts": [{"text": "x" * 4000}]},
        }
    )
    rows = [json.dumps(call), json.dumps(result)]
    rows.extend([filler] * filler_rows)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_feedback_trace_peak_memory_stays_bounded_as_transcript_grows(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = append_feedback(
        origin=None,
        provenance={
            "conversation": {
                "client": "qwen",
                "session_id": "memory-session",
                "transcript": "$QWEN_HOME/projects/workspace/chats/memory.jsonl",
            }
        },
    )
    feedback_id = str(result["feedback_id"])
    transcript = tmp_path / "qwen-home" / "projects" / "workspace" / "chats" / "memory.jsonl"
    transcript.parent.mkdir(parents=True)
    monkeypatch.setenv("QWEN_HOME", str(tmp_path / "qwen-home"))

    for filler_rows in (256, 1024):
        _write_synthetic_transcript(transcript, feedback_id, filler_rows)
        assert transcript.stat().st_size > 1024 * 1024
        tracemalloc.start()
        try:
            traced = feedback.trace(feedback_id)
        finally:
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

        assert len(traced["matches"]) == 1
        assert traced["matches"][0]["transcript"] == (
            "$QWEN_HOME/projects/workspace/chats/memory.jsonl"
        )
        assert peak < 3 * 1024 * 1024


def test_feedback_trace_lookback_is_bounded_by_the_window(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = append_feedback(origin=None, provenance=None)
    feedback_id = str(result["feedback_id"])
    transcript = tmp_path / "qwen-home" / "projects" / "workspace" / "chats" / "bounded.jsonl"
    transcript.parent.mkdir(parents=True)
    rows = [
        {
            "message": {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {
                            "id": "tool-a",
                            "name": "run_shell_command",
                            "args": {"command": "echo hi"},
                        }
                    }
                ],
            }
        },
        {
            "message": {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "id": "tool-a",
                            "name": "run_shell_command",
                            "response": {"output": "hi"},
                        }
                    }
                ],
            }
        },
    ]
    rows.extend(
        {"message": {"role": "user", "parts": [{"text": f"filler {index}"}]}} for index in range(12)
    )
    rows.append(
        {
            "message": {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {
                            "id": "feedback-call",
                            "name": "mcp__github_workflows__workflow_feedback",
                            "args": {"message": "Bounded probe"},
                        }
                    }
                ],
            }
        }
    )
    rows.append(
        {
            "message": {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "id": "feedback-call",
                            "name": "mcp__github_workflows__workflow_feedback",
                            "response": {
                                "output": json.dumps({"recorded": True, "feedback_id": feedback_id})
                            },
                        }
                    }
                ],
            }
        }
    )
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    monkeypatch.setenv("QWEN_HOME", str(tmp_path / "qwen-home"))

    with mock.patch.object(feedback, "TRACE_WINDOW_ROWS", 3):
        traced = feedback.trace(feedback_id)

    match = traced["matches"][0]
    assert match["feedback_tool_call_id"] == "feedback-call"
    assert match["tools"] == []


def test_feedback_fallback_transcript_scan_streams_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feedback_id = "fb-000000000001"
    transcript = tmp_path / "projects" / "workspace" / "chats" / "streamed.jsonl"
    transcript.parent.mkdir(parents=True)
    rows = [
        json.dumps({"message": f"filler {index}", "padding": "x" * 4000}) for index in range(512)
    ]
    rows.append(json.dumps({"message": feedback_id}))
    transcript.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def missing(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("rg")

    monkeypatch.setattr(feedback.subprocess, "run", missing)
    tracemalloc.start()
    try:
        found = feedback._candidate_transcripts(feedback_id, tmp_path)
    finally:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    assert found == [transcript]
    assert peak < 1536 * 1024


def test_trace_rejects_unknown_detail() -> None:
    with pytest.raises(ValueError, match="tools, context, or data"):
        feedback.trace("12345678", detail="everything")


def test_feedback_cli_filters_and_counts_normalized_sources(
    cache: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    append_feedback(tool="task_context", message="Legacy source")
    append_feedback(tool="mcp__github_workflows__task_context", message="Qualified source")
    append_feedback(tool="web_fetch", message="Native source")
    append_feedback(tool=None, message="General feedback")

    parsed = build_parser().parse_args(
        ["feedback", "ls", "--source", "task_context", "--source", "web_fetch"]
    )
    assert parsed.feedback_command == "list"
    assert parsed.sources == ["task_context", "web_fetch"]
    parsed.json_output = True
    assert run_feedback(parsed) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [feedback.source_name(record) for record in listed] == [
        "web_fetch",
        "task_context",
        "task_context",
    ]

    summary_args = build_parser().parse_args(["feedback", "summary"])
    assert run_feedback(summary_args) == 0
    table = capsys.readouterr().out
    assert "Records: 4 total" in table
    assert "Open (4)" in table
    assert "task_context      2" in table
    assert "general           1" in table
    assert "web_fetch         1" in table
    assert "Closed (0)" in table


def test_feedback_table_formats_short_refs_and_complete_summaries() -> None:
    assert feedback.format_table([], width=120) == "No feedback recorded."
    message = "A long explanation " * 20
    table = feedback.format_table(
        [
            {
                "feedback_id": "fb-20260901232545-5f009f5df7",
                "ref": "009f5df7",
                "timestamp": "2026-09-01T23:25:45.610861Z",
                "repository": "example/repository-with-a-long-name",
                "workflow": "gh-audit-repo",
                "tool": "glob",
                "status": "open",
                "message": message,
            }
        ],
        width=120,
    )

    assert "009f5df7" in table
    assert "fb-20260901232545-5f009f5df7" not in table
    assert feedback._display_time("2026-09-01T23:25:45Z") in table
    assert "glob" in table
    assert "Summary: A long explanation" in table
    assert " ".join(table.split()).count("A long explanation") == 20
    assert len(table.splitlines()) > 4


def test_feedback_display_time_uses_system_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    original = feedback.dt.datetime

    class LocalDatetime(original):
        def astimezone(self, tz: object = None) -> LocalDatetime:
            if tz is None:
                tz = feedback.dt.timezone(feedback.dt.timedelta(hours=2), name="CEST")
            return super().astimezone(tz)  # type: ignore[arg-type, return-value]

    monkeypatch.setattr(feedback.dt, "datetime", LocalDatetime)

    assert feedback._display_time("2026-09-01T12:00:00Z") == "2026-09-01 14:00:00 CEST"


def test_feedback_display_time_falls_back_to_edt(monkeypatch: pytest.MonkeyPatch) -> None:
    original = feedback.dt.datetime

    class NoLocalTimezoneDatetime(original):
        def astimezone(self, tz: object = None) -> NoLocalTimezoneDatetime:
            if tz is None:
                raise OSError("local timezone unavailable")
            return super().astimezone(tz)  # type: ignore[arg-type, return-value]

    monkeypatch.setattr(feedback.dt, "datetime", NoLocalTimezoneDatetime)

    assert feedback._display_time("2026-09-01T12:00:00Z") == "2026-09-01 08:00:00 EDT"


def test_feedback_table_labels_records_without_sources_as_general() -> None:
    table = feedback.format_table(
        [
            {
                "feedback_id": "fb-123456789abc",
                "timestamp": "2026-09-01T23:25:45Z",
                "repository": "example/repo",
                "workflow": "gh-audit-repo",
                "source": "general",
                "status": "open",
                "message": "The active instruction was ambiguous",
            }
        ],
        width=120,
    )

    assert "general" in table


def test_feedback_table_escapes_terminal_controls() -> None:
    table = feedback.format_table(
        [
            {
                "feedback_id": "fb-123456789abc",
                "timestamp": "2026-09-01T23:25:45Z",
                "repository": "example/repo",
                "source": "tool\x1b]8;;https://example.invalid\x07",
                "status": "open",
                "message": "warning\x1b[2J\x9b31m hidden\u200btext",
            }
        ],
        width=200,
    )

    assert "\x1b" not in table
    assert "\x9b" not in table
    assert "\u200b" not in table
    assert r"\x1b[2J\x9b31m" in table
    assert r"hidden\u200btext" in table


def test_feedback_reader_quarantines_malformed_and_torn_lines(cache: Path) -> None:
    path = feedback.storage_path()
    path.parent.mkdir(parents=True)
    valid = {
        "feedback_id": "fb-123456789abc",
        "timestamp": "2026-09-02T12:00:00Z",
        "status": "open",
        "message": "Surviving record",
    }
    path.write_bytes(
        (json.dumps(valid) + "\n").encode()
        + b"{}\n"
        + b"\xff\xfe broken line\n"
        + b'{"feedback_id": "fb-torn0000000000", "timestamp": "2026-'
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        records = feedback.read_records()

    assert [record["feedback_id"] for record in records] == ["fb-123456789abc"]
    messages = [str(warning.message) for warning in caught]
    assert "line 2" in messages[0]
    assert "missing required fields" in messages[0]
    assert "line 3" in messages[1]
    assert "UTF-8" in messages[1]
    assert "line 4" in messages[2]
    assert "feedback JSON" in messages[2]

    # Subsequent reads and appends work without manual file editing.
    assert len(feedback.read_records()) == 1
    appended = append_feedback(message="Recorded after corruption")
    stored_ids = [record["feedback_id"] for record in feedback.read_records()]
    assert stored_ids == ["fb-123456789abc", appended["feedback_id"]]


def test_feedback_reader_quarantines_store_line_when_json_decoder_recurses(
    cache: Path,
) -> None:
    path = feedback.storage_path()
    path.parent.mkdir(parents=True)
    valid = {
        "feedback_id": "fb-123456789abc",
        "timestamp": "2026-09-02T12:00:00Z",
        "status": "open",
        "message": "Surviving record",
    }
    path.write_bytes((json.dumps(valid) + "\n" + "deeply nested store line\n").encode())
    real_loads = json.loads

    def loads(value: str, *args: object, **kwargs: object) -> object:
        if value.strip() == "deeply nested store line":
            raise RecursionError
        return real_loads(value, *args, **kwargs)

    with mock.patch.object(feedback.json, "loads", side_effect=loads):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            records = feedback.read_records()

    assert [record["feedback_id"] for record in records] == ["fb-123456789abc"]
    messages = [str(warning.message) for warning in caught]
    assert "line 2" in messages[0]
    assert "feedback JSON" in messages[0]

    # Subsequent operations complete without manual file editing.
    assert len(feedback.read_records()) == 1
    appended = append_feedback(message="Recorded after the deep line")
    stored_ids = [record["feedback_id"] for record in feedback.read_records()]
    assert stored_ids == ["fb-123456789abc", appended["feedback_id"]]


def test_feedback_transcript_windows_skip_row_when_json_decoder_recurses(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"a": 1}\ndeeply nested transcript row\n{"b": 2}\n', encoding="utf-8")
    real_loads = json.loads

    def loads(value: str, *args: object, **kwargs: object) -> object:
        if value.strip() == "deeply nested transcript row":
            raise RecursionError
        return real_loads(value, *args, **kwargs)

    with mock.patch.object(feedback.json, "loads", side_effect=loads):
        windows = list(feedback._iter_transcript_windows(transcript))

    assert [value for _window, value in windows] == [{"a": 1}, {"b": 2}]
    assert [len(window) for window, _value in windows] == [0, 1]


def test_feedback_hook_id_returns_none_when_string_decode_recurses() -> None:
    payload = json.dumps({"recorded": True, "feedback_id": "fb-0123456789ab"})
    with mock.patch.object(feedback.json, "loads", side_effect=RecursionError):
        assert feedback._hook_feedback_id(payload) is None


def test_feedback_append_recovers_torn_trailing_line(cache: Path) -> None:
    first = append_feedback(message="Surviving record")
    path = feedback.storage_path()
    original = path.read_bytes()
    path.write_bytes(original + b'{"feedback_id": "fb-torn0000000000", "timestamp": "2026-09-')

    with pytest.warns(UserWarning, match="line 2"):
        second = append_feedback(message="Recorded after the torn write")

    records = feedback.read_records()
    assert [record["feedback_id"] for record in records] == [
        first["feedback_id"],
        second["feedback_id"],
    ]
    stored = path.read_bytes()
    assert stored.endswith(b"\n")
    assert b"fb-torn0000000000" not in stored


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
