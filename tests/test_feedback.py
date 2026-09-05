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
from github_workflows.cli import build_parser, run_feedback
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
            failure_kind="validation",
        )
        stored = registry.resolve(first)
        assert stored is not None
        assert stored["origin"] == {
            "failure_kind": "validation",
            "invocation": {
                "argument_types": {
                    "action": "string",
                    "candidate_id": "object",
                    "instructions": "string",
                    "records": "array",
                },
                "selectors": {"action": "start"},
                "omitted": ["candidate_id", "instructions", "records"],
                "complete": False,
                "unknown_argument_count": 2,
            },
        }

        registry.record(tool="run_status", arguments={}, failure_kind="domain")
        third = registry.record(tool="task_manage", arguments={}, failure_kind="internal")
        assert registry.resolve(first) is None
        assert registry.resolve(third) is not None

        clock = 16.0
        assert registry.resolve(third) is None


def test_failure_registry_omits_argument_values(tmp_path: Path) -> None:
    registry = feedback.FailureRegistry([(tmp_path, "<workspace>")])
    reference = registry.record(
        tool="task_manage",
        arguments={"report": "x" * feedback.MAX_FAILURE_BYTES},
        failure_kind="validation",
    )

    stored = registry.resolve(reference)
    assert stored is not None
    assert stored["origin"]["invocation"] == {
        "argument_types": {"report": "string"},
        "complete": False,
        "omitted": ["report"],
    }


def test_failure_registry_bounds_the_complete_snapshot(tmp_path: Path) -> None:
    registry = feedback.FailureRegistry([(tmp_path, "<workspace>")])
    reference = registry.record(
        tool="task_manage",
        arguments={"action": "complete"},
        failure_kind="validation",
        provenance={"client": {"name": "qwen"}},
    )

    stored = registry.resolve(reference)
    assert stored is not None
    assert feedback._encoded_size(stored) <= feedback.MAX_FAILURE_BYTES
    assert stored["origin"]["invocation"]["complete"] is True


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
        sources=None,
        closed=False,
        status=None,
        cutoff=None,
        limit=50,
        all_records=False,
        json_output=False,
    )
    assert run_feedback(list_args) == 0
    table = capsys.readouterr().out
    assert "WHEN (LOCAL)" in table
    assert "example/repo" in table
    assert "run-1" not in table
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

    conflicting = build_parser().parse_args(["feedback", "ls", "--closed", "--status", "all"])
    with pytest.raises(ValueError, match="cannot be combined"):
        run_feedback(conflicting)


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

    closed_args = build_parser().parse_args(["feedback", "ls", "--closed", "--json"])
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


def test_feedback_cli_replaces_stats_and_sources_with_summary() -> None:
    parser = build_parser()
    assert parser.parse_args(["feedback", "summary"]).feedback_command == "summary"
    with pytest.raises(SystemExit):
        parser.parse_args(["feedback", "stats"])
    with pytest.raises(SystemExit):
        parser.parse_args(["feedback", "sources"])


def test_feedback_trace_locates_exact_worker_call_without_content(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = append_feedback(
        tool="task_manage",
        origin={
            "error_ref": "err-123456789abc",
            "failure_kind": "validation",
            "invocation": {"argument_types": {"report": "string"}, "complete": False},
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
                                "output": 'report must be an object; error_ref="err-123456789abc"'
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
                            "args": {
                                "message": "The report type was unclear",
                                "error_ref": "err-123456789abc",
                            },
                        }
                    }
                ],
            },
        },
        {
            "type": "tool_result",
            "message": {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "id": "rejected-feedback-call",
                            "name": "mcp__github_workflows__workflow_feedback",
                            "response": {
                                "output": "error_ref cannot be combined with tool: err-123456789abc"
                            },
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

    traced = feedback.trace(feedback_id)

    assert traced == {
        "feedback_id": feedback_id,
        "matches": [
            {
                "timestamp": "2026-09-02T12:00:01Z",
                "session_id": "session-1",
                "agent_id": "worker-call",
                "feedback_tool_call_id": "feedback-call",
                "transcript": (
                    "$QWEN_HOME/projects/-workspace/subagents/session-1/agent-worker-call.jsonl"
                ),
                "origin": {
                    "match": "exact-error-ref",
                    "tool": "mcp__github_workflows__task_manage",
                    "tool_call_id": "origin-call",
                },
            }
        ],
    }
    assert "report must be an object" not in feedback.format_trace(traced)


def test_feedback_cli_filters_and_counts_normalized_sources(
    cache: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    append_feedback(tool="task_context", message="Legacy source")
    append_feedback(tool="mcp__github_workflows__task_context", message="Qualified source")
    append_feedback(tool="web_fetch", message="Native source")
    append_feedback(tool=None, message="General feedback")

    parsed = build_parser().parse_args(
        ["feedback", "ls", "--source", "task_context", "--tool", "web_fetch"]
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


def test_feedback_table_formats_short_refs_and_empty_results() -> None:
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
                "message": message,
                "provenance": {"task": {"id": "discover-core-1"}},
            }
        ],
        width=100,
    )

    assert "009f5df7" in table
    assert "fb-20260901232545-5f009f5df7" not in table
    expected_time = feedback._display_time("2026-09-01T23:25:45Z")
    assert expected_time in table
    assert "example/repository-with-a-long-name" in table
    assert "discover-core" in table
    assert "glob" in table
    assert "Summary:" in table
    assert "\n           " in table
    assert " ".join(message.split()) in " ".join(table.split())


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


def test_feedback_table_labels_records_without_tools_as_general() -> None:
    table = feedback.format_table(
        [
            {
                "feedback_id": "fb-123456789abc",
                "timestamp": "2026-09-01T23:25:45Z",
                "repository": "example/repo",
                "workflow": "gh-audit-repo",
                "tool": None,
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
