from __future__ import annotations

import json
import logging
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from mcp import Client
from pydantic import ValidationError

from github_workflows.mcp_server import _validation_issues, create_server
from github_workflows.models import (
    AuditRecordRequest,
    KnowledgeRequest,
    RunManageRequest,
    TaskManageRequest,
)
from github_workflows.runtime import WorkflowRuntime

ROOT = Path(__file__).parents[1]
EXTENSION = ROOT / "extensions/github-workflows"


class TestExtensionMcp:
    @staticmethod
    def git(*arguments: str, cwd: Path) -> None:
        subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    async def test_tool_contract_and_generic_worker_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-mcp-") as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            runtime = WorkflowRuntime(workspace, root / "qwen-project")
            async with Client(
                create_server(runtime), raise_exceptions=True, read_timeout_seconds=0.1
            ) as client:
                listed = await client.list_tools()
                tools = {tool.name: tool for tool in listed.tools}
                assert set(tools) == {
                    "run_manage",
                    "run_status",
                    "task_manage",
                    "task_context",
                    "history_manage",
                    "history_query",
                    "audit_inventory",
                    "audit_knowledge",
                    "audit_probe",
                    "audit_record",
                    "audit_publish",
                    "audit_metrics",
                }
                context_properties = tools["task_context"].input_schema["properties"]
                assert "task_ref" in context_properties
                run_properties = tools["run_manage"].input_schema["properties"]
                assert "n" in run_properties
                assert "repository" in run_properties
                assert "instructions" in run_properties
                for name in (
                    "task_manage",
                    "history_manage",
                    "audit_inventory",
                    "audit_knowledge",
                    "audit_probe",
                    "audit_record",
                    "audit_publish",
                ):
                    properties = tools[name].input_schema["properties"]
                    assert ("action" if name != "audit_probe" else "kind") in properties
                assert "task" in tools["task_manage"].input_schema["properties"]
                assert "candidate_id" in tools["audit_probe"].input_schema["required"]
                history_properties = tools["history_manage"].input_schema["properties"]
                assert "records" in history_properties
                assert "artifacts" in history_properties
                inventory_properties = tools["audit_inventory"].input_schema["properties"]
                assert "facts" in inventory_properties
                assert "fact" in inventory_properties
                audit_record_properties = tools["audit_record"].input_schema["properties"]
                assert "candidate" in audit_record_properties
                assert "phase" in audit_record_properties
                conditional_required = {
                    name: {
                        condition["if"]["properties"][discriminator]["const"]: set(
                            condition["then"].get("dependencies", {}).get(discriminator, [])
                        )
                        for condition in tools[name].input_schema["allOf"]
                        if "if" in condition
                        and "then" in condition
                        and discriminator in condition["if"].get("properties", {})
                    }
                    for name, discriminator in {
                        "run_manage": "action",
                        "task_manage": "action",
                        "audit_inventory": "action",
                        "audit_knowledge": "action",
                        "audit_probe": "kind",
                        "audit_record": "action",
                        "audit_publish": "action",
                    }.items()
                }
                assert conditional_required["run_manage"]["start"] == {"repository"}
                assert conditional_required["task_manage"]["checkpoint"] == {
                    "task_id",
                    "report",
                }
                assert conditional_required["audit_inventory"]["program"] == {"programs"}
                assert conditional_required["audit_knowledge"]["reconcile"] == {"areas"}
                assert conditional_required["audit_probe"]["python"] == {"code"}
                assert conditional_required["audit_record"]["candidate"] == {"candidate"}
                assert conditional_required["audit_publish"]["finish"] == {"receipt"}
                ingest_contract = next(
                    condition["then"]
                    for condition in tools["history_manage"].input_schema["allOf"]
                    if condition.get("if", {}).get("properties", {}).get("action", {}).get("const")
                    == "ingest"
                )
                assert [branch["required"] for branch in ingest_contract["oneOf"]] == [
                    ["records"],
                    ["artifacts"],
                ]
                query_contract = tools["history_query"].input_schema["allOf"][0]
                assert {branch["required"][0] for branch in query_contract["anyOf"]} == {
                    "terms",
                    "kind",
                    "state",
                    "cutoff",
                    "linked",
                }
                with pytest.raises(ValueError, match="Extra inputs are not permitted"):
                    RunManageRequest.model_validate(
                        {
                            "action": "start",
                            "workflow": "gh-curate-issues",
                            "repository": "example/repo",
                            "inputs": {"n": 2},
                        }
                    )
                audit_request = RunManageRequest(
                    action="start",
                    workflow="gh-audit-repo",
                    repository="example/repo",
                    instructions="Prioritize public CLI behavior",
                )
                assert audit_request.invocation()["instructions"] == (
                    "Prioritize public CLI behavior"
                )
                KnowledgeRequest.model_validate({"action": "show"})
                knowledge_request = KnowledgeRequest.model_validate(
                    {
                        "action": "reconcile",
                        "areas": [
                            {
                                "area": "area/core",
                                "description": "Core behavior",
                                "paths": ["src/"],
                                "boundaries": "Owns the core runtime",
                            }
                        ],
                    }
                )
                assert knowledge_request.areas[0].boundaries == ["Owns the core runtime"]
                assert knowledge_request.areas[0].area == "area/core"
                assert knowledge_request.areas[0].title == "Core"
                invalid_area = {
                    "area": "area/core",
                    "description": "Core behavior",
                    "paths": "src/",
                }
                with pytest.raises(ValidationError) as validation:
                    KnowledgeRequest.model_validate(
                        {"action": "reconcile", "areas": [invalid_area, invalid_area]}
                    )
                issues = _validation_issues(validation.value, {"action": "reconcile"})
                assert [(issue.field, issue.kind) for issue in issues] == [
                    ("areas[].paths", "list_type")
                ]
                assert tools["run_status"].annotations.read_only_hint
                assert not tools["task_manage"].annotations.read_only_hint
                assert not tools["audit_probe"].annotations.read_only_hint

                started = await client.call_tool(
                    "run_manage",
                    {
                        "action": "start",
                        "workflow": "gh-curate-issues",
                        "repository": "example/repo",
                        "n": 2,
                    },
                )
                assert not started.is_error
                assert started.structured_content["revision"] == 1

                planned = await client.call_tool(
                    "task_manage",
                    {
                        "action": "plan",
                        "workflow": "gh-curate-issues",
                        "task": {
                            "logical_id": "issue-12",
                            "role": "curate",
                            "unit": "issue/12",
                            "assignment": {
                                "issue": 12,
                                "source_kind": "python-library",
                                "accepted_scope": "Normalize the public API issue",
                            },
                        },
                    },
                )
                assert not planned.is_error
                task_id = planned.structured_content["task_id"]
                task_ref = planned.structured_content["task_ref"]
                assert task_ref.startswith("gh-curate-issues:")
                revised = await client.call_tool(
                    "task_manage",
                    {
                        "action": "plan",
                        "workflow": "gh-curate-issues",
                        "task": {
                            "logical_id": "issue-12",
                            "role": "curate",
                            "unit": "issue/12",
                            "assignment": {
                                "issue": 12,
                                "source_kind": "python-library",
                                "accepted_scope": "Revised scope",
                            },
                        },
                    },
                )
                assert revised.structured_content["task_id"] == task_id
                revised_context = await client.call_tool("task_context", {"task_ref": task_ref})
                assert revised_context.structured_content["assignment"]["accepted_scope"] == (
                    "Revised scope"
                )
                await client.call_tool(
                    "task_manage",
                    {
                        "action": "mark_running",
                        "workflow": "gh-curate-issues",
                        "task_id": task_id,
                    },
                )
                context = await client.call_tool("task_context", {"task_ref": task_ref})
                assert context.structured_content["workflow"] == "gh-curate-issues"
                assert context.structured_content["task_ref"] == task_ref
                assert context.structured_content["assignment"]["issue"] == 12
                assert context.structured_content["documentation"]["context7_query_budget"] == 12
                assert (
                    context.structured_content["documentation"]["source_priority"][0]
                    == "domain skill"
                )
                references = context.structured_content["references"]
                assert set(references) == {"runtime_policy", "issue_conventions"}
                for path in references.values():
                    assert Path(path).is_file()

                await client.call_tool(
                    "task_manage",
                    {
                        "action": "complete",
                        "workflow": "gh-curate-issues",
                        "task_id": task_id,
                        "report": {"disposition": "no-change"},
                    },
                )
                await client.call_tool(
                    "task_manage",
                    {
                        "action": "integration_begin",
                        "workflow": "gh-curate-issues",
                        "task_id": task_id,
                    },
                )
                await client.call_tool(
                    "task_manage",
                    {
                        "action": "integration_end",
                        "workflow": "gh-curate-issues",
                        "task_id": task_id,
                    },
                )
                status = await client.call_tool("run_status", {"workflow": "gh-curate-issues"})
                assert status.structured_content["tasks"][task_id]["integrated"]
                assert status.structured_content["tasks"][task_id]["task_ref"] == task_ref
                finished = await client.call_tool(
                    "run_manage",
                    {
                        "action": "finish",
                        "workflow": "gh-curate-issues",
                    },
                )
                assert not finished.is_error

    async def test_expected_runtime_failure_is_actionable_tool_error(self, caplog: Any) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-errors-") as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            runtime = WorkflowRuntime(workspace, root / "qwen-project")
            request = {
                "action": "start",
                "workflow": "gh-curate-issues",
                "repository": "example/repo",
            }
            async with Client(
                create_server(runtime),
                raise_exceptions=False,
                read_timeout_seconds=0.1,
            ) as client:
                wrapped = await client.call_tool("run_manage", {"request": request})
                assert wrapped.is_error
                encoded = await client.call_tool(
                    "run_manage",
                    {
                        "request": json.dumps(request),
                    },
                )
                assert encoded.is_error
                missing = await client.call_tool(
                    "audit_knowledge",
                    {"action": "reconcile"},
                )
                assert missing.is_error
                invalid_calls = [
                    ("run_manage", {}),
                    ("run_status", {}),
                    ("task_manage", {}),
                    ("task_context", {}),
                    ("history_manage", {"action": "unsupported"}),
                    ("history_query", {"limit": 0}),
                    ("audit_inventory", {}),
                    ("audit_knowledge", {"action": "reconcile"}),
                    ("audit_probe", {}),
                    ("audit_record", {}),
                    ("audit_publish", {}),
                    ("audit_metrics", {"unexpected": True}),
                ]
                internal_diagnostic = re.compile(
                    r"validation errors?|input_(?:value|type)|errors\.pydantic|Traceback"
                )
                for tool_name, arguments in invalid_calls:
                    result = await client.call_tool(tool_name, arguments)
                    assert result.is_error
                    assert len(result.content) == 1
                    message = result.content[0].text
                    assert "\n" not in message
                    assert internal_diagnostic.search(message) is None
                assert not (await client.call_tool("run_manage", request)).is_error
                replaced = await client.call_tool("run_manage", request)
                assert not replaced.is_error
                resumed = await client.call_tool(
                    "run_manage",
                    {"action": "resume", "workflow": request["workflow"]},
                )
                assert not resumed.is_error
                sentinel = "private-internal-detail"
                with (
                    caplog.at_level(logging.ERROR),
                    mock.patch.object(runtime, "run_status", side_effect=KeyError(sentinel)),
                ):
                    crashed = await client.call_tool("run_status", {"workflow": "gh-curate-issues"})
                assert crashed.is_error
                public_crash = " ".join(
                    item.text for item in crashed.content if hasattr(item, "text")
                )
                assert sentinel not in public_crash
                assert any(record.exc_info for record in caplog.records)

    def test_task_references_disambiguate_workflows_and_reject_stale_runs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-task-ref-") as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            runtime = WorkflowRuntime(workspace, root / "qwen-project")
            references = {}
            for workflow in ("gh-curate-issues", "gh-implement-issue"):
                runtime.run_manage(
                    RunManageRequest(
                        action="start",
                        workflow=workflow,
                        repository="example/repo",
                        n=1,
                    )
                )
                receipt = runtime.task_manage(
                    TaskManageRequest(
                        action="plan",
                        workflow=workflow,
                        task={"logical_id": "issue-12", "assignment": {"issue": 12}},
                    )
                )
                references[workflow] = receipt["task_ref"]

            assert references["gh-curate-issues"] != references["gh-implement-issue"]
            for workflow, task_ref in references.items():
                context = runtime.task_context(task_ref)
                assert context["workflow"] == workflow
                assert context["task_id"] == "issue-12-1"

            stale = references["gh-curate-issues"]
            runtime.run_manage(RunManageRequest(action="abort", workflow="gh-curate-issues"))
            runtime.run_manage(
                RunManageRequest(
                    action="start",
                    workflow="gh-curate-issues",
                    repository="example/repo",
                    n=1,
                )
            )
            with pytest.raises(ValueError, match="stale workflow run"):
                runtime.task_context(stale)

    def test_generic_scheduler_enforces_lanes_and_finish_gates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-scheduler-") as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            runtime = WorkflowRuntime(workspace, root / "qwen-project")
            workflow = "gh-curate-issues"
            runtime.run_manage(
                RunManageRequest(
                    action="start",
                    workflow=workflow,
                    repository="example/repo",
                    n=1,
                )
            )
            for task_id in ("issue-1-1", "issue-2-1"):
                runtime.task_manage(
                    TaskManageRequest(
                        action="plan",
                        workflow=workflow,
                        task={
                            "logical_id": task_id.rsplit("-", 1)[0],
                            "assignment": {"issue": task_id},
                        },
                    )
                )

            initial_revision = runtime.state(workflow)["revision"]
            with pytest.raises(ValueError, match="nonterminal tasks"):
                runtime.run_manage(RunManageRequest(action="finish", workflow=workflow))
            assert runtime.state(workflow)["revision"] == initial_revision
            assert runtime.state(workflow)["status"] == "in-progress"
            runtime.task_manage(
                TaskManageRequest(
                    action="mark_running",
                    workflow=workflow,
                    task_id="issue-1-1",
                )
            )
            revision = runtime.state(workflow)["revision"]
            with pytest.raises(ValueError, match="concurrency is saturated"):
                runtime.task_manage(
                    TaskManageRequest(
                        action="mark_running",
                        workflow=workflow,
                        task_id="issue-2-1",
                    )
                )
            assert runtime.state(workflow)["revision"] == revision

            runtime.task_manage(
                TaskManageRequest(
                    action="complete",
                    workflow=workflow,
                    task_id="issue-1-1",
                    report={"disposition": "complete"},
                )
            )
            with pytest.raises(ValueError, match="concurrency is saturated"):
                runtime.task_manage(
                    TaskManageRequest(
                        action="mark_running",
                        workflow=workflow,
                        task_id="issue-2-1",
                    )
                )
            with pytest.raises(ValueError, match="integration queue"):
                runtime.run_manage(RunManageRequest(action="finish", workflow=workflow))

            runtime.task_manage(
                TaskManageRequest(
                    action="integration_begin",
                    workflow=workflow,
                    task_id="issue-1-1",
                )
            )
            status = runtime.run_status(workflow)["scheduler"]
            assert status["worker_slots"] == 0
            assert status["next_action"] == "finish-integration"
            with pytest.raises(ValueError, match="concurrency is saturated"):
                runtime.task_manage(
                    TaskManageRequest(
                        action="mark_running",
                        workflow=workflow,
                        task_id="issue-2-1",
                    )
                )
            with pytest.raises(ValueError, match="supervisor material activity"):
                runtime.run_manage(RunManageRequest(action="finish", workflow=workflow))

            runtime.task_manage(
                TaskManageRequest(
                    action="integration_end",
                    workflow=workflow,
                    task_id="issue-1-1",
                )
            )
            dispatched = runtime.task_manage(
                TaskManageRequest(
                    action="mark_running",
                    workflow=workflow,
                    task_id="issue-2-1",
                )
            )
            assert dispatched["scheduler"]["running_workers"] == 1
            runtime.task_manage(
                TaskManageRequest(
                    action="checkpoint",
                    workflow=workflow,
                    task_id="issue-2-1",
                    report={"status": "partial", "remaining": "issue publication"},
                )
            )
            with pytest.raises(ValueError, match="nonterminal tasks"):
                runtime.run_manage(RunManageRequest(action="finish", workflow=workflow))
            runtime.task_manage(
                TaskManageRequest(
                    action="mark_running",
                    workflow=workflow,
                    task_id="issue-2-1",
                )
            )
            runtime.task_manage(
                TaskManageRequest(
                    action="complete",
                    workflow=workflow,
                    task_id="issue-2-1",
                    report={"disposition": "complete"},
                )
            )
            runtime.task_manage(
                TaskManageRequest(
                    action="integration_begin",
                    workflow=workflow,
                    task_id="issue-2-1",
                )
            )
            runtime.task_manage(
                TaskManageRequest(
                    action="integration_end",
                    workflow=workflow,
                    task_id="issue-2-1",
                )
            )
            finished = runtime.run_manage(RunManageRequest(action="finish", workflow=workflow))
            assert finished["status"] == "complete"

    def test_required_generic_task_needs_a_successful_integrated_attempt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-retry-") as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            runtime = WorkflowRuntime(workspace, root / "qwen-project")
            workflow = "gh-implement-issue"
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
                    task={"logical_id": "unit-1"},
                )
            )
            runtime.task_manage(
                TaskManageRequest(action="mark_running", workflow=workflow, task_id="unit-1-1")
            )
            runtime.task_manage(
                TaskManageRequest(action="fail", workflow=workflow, task_id="unit-1-1")
            )
            runtime.task_manage(
                TaskManageRequest(action="integration_begin", workflow=workflow, task_id="unit-1-1")
            )
            runtime.task_manage(
                TaskManageRequest(action="integration_end", workflow=workflow, task_id="unit-1-1")
            )
            assert runtime.run_status(workflow)["scheduler"]["next_action"] == "retry-required-task"
            with pytest.raises(ValueError, match="required logical tasks"):
                runtime.run_manage(RunManageRequest(action="finish", workflow=workflow))

            retry = runtime.task_manage(
                TaskManageRequest(
                    action="retry",
                    workflow=workflow,
                    task_id="unit-1-1",
                )
            )
            assert retry["task_id"] == "unit-1-2"
            runtime.task_manage(
                TaskManageRequest(action="mark_running", workflow=workflow, task_id="unit-1-2")
            )
            runtime.task_manage(
                TaskManageRequest(
                    action="complete",
                    workflow=workflow,
                    task_id="unit-1-2",
                    report={"status": "complete"},
                )
            )
            runtime.task_manage(
                TaskManageRequest(action="integration_begin", workflow=workflow, task_id="unit-1-2")
            )
            runtime.task_manage(
                TaskManageRequest(action="integration_end", workflow=workflow, task_id="unit-1-2")
            )
            assert (
                runtime.run_manage(RunManageRequest(action="finish", workflow=workflow))["status"]
                == "complete"
            )
            with pytest.raises(ValueError, match="not active"):
                runtime.task_manage(
                    TaskManageRequest(
                        action="plan",
                        workflow=workflow,
                        task={"logical_id": "late-task"},
                    )
                )

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
                    task={"logical_id": "optional", "required": False},
                )
            )
            runtime.task_manage(
                TaskManageRequest(action="mark_running", workflow=workflow, task_id="optional-1")
            )
            runtime.task_manage(
                TaskManageRequest(action="fail", workflow=workflow, task_id="optional-1")
            )
            runtime.task_manage(
                TaskManageRequest(
                    action="integration_begin", workflow=workflow, task_id="optional-1"
                )
            )
            runtime.task_manage(
                TaskManageRequest(action="integration_end", workflow=workflow, task_id="optional-1")
            )
            assert (
                runtime.run_manage(RunManageRequest(action="finish", workflow=workflow))["status"]
                == "complete"
            )

    def test_generic_concurrency_validation_and_default(self) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-concurrency-") as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            for invalid in (True, 0, -1, "2"):
                runtime = WorkflowRuntime(workspace, root / f"qwen-{invalid!s}")
                with pytest.raises(ValueError, match="positive integer"):
                    runtime.run_manage(
                        RunManageRequest(
                            action="start",
                            workflow="gh-curate-issues",
                            repository="example/repo",
                            n=invalid,
                        )
                    )
            runtime = WorkflowRuntime(workspace, root / "qwen-default")
            runtime.run_manage(
                RunManageRequest(
                    action="start",
                    workflow="gh-curate-issues",
                    repository="example/repo",
                )
            )
            assert runtime.run_status("gh-curate-issues")["scheduler"]["limit"] == 3
            runtime.run_manage(
                RunManageRequest(
                    action="resume",
                    workflow="gh-curate-issues",
                    n=5,
                )
            )
            resumed = runtime.run_status("gh-curate-issues")
            assert resumed["scheduler"]["limit"] == 5
            assert resumed["inputs"]["n"] == 5

            pending_runtime = WorkflowRuntime(workspace, root / "qwen-pending")
            pending_runtime.run_manage(
                RunManageRequest(
                    action="start",
                    workflow="gh-curate-issues",
                    repository="example/repo",
                    pending=["issue mutation read-back"],
                )
            )
            with pytest.raises(ValueError, match="pending operations"):
                pending_runtime.run_manage(
                    RunManageRequest(
                        action="finish",
                        workflow="gh-curate-issues",
                    )
                )
            assert pending_runtime.state("gh-curate-issues")["status"] == "in-progress"
            assert (
                pending_runtime.run_manage(
                    RunManageRequest(
                        action="abort",
                        workflow="gh-curate-issues",
                    )
                )["status"]
                == "aborted"
            )

    async def test_audit_adapter_derives_worktree_and_validation_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-audit-mcp-") as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            (workspace / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
            (workspace / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
            self.git("init", "-b", "main", cwd=workspace)
            self.git("config", "user.name", "MCP Test", cwd=workspace)
            self.git("config", "user.email", "mcp-test@example.invalid", cwd=workspace)
            self.git("add", ".gitignore", "module.py", cwd=workspace)
            self.git("commit", "-m", "fixture", cwd=workspace)

            project_dir = root / "qwen-project"
            runtime = WorkflowRuntime(workspace, project_dir)
            async with Client(
                create_server(runtime), raise_exceptions=True, read_timeout_seconds=0.1
            ) as client:
                started = await client.call_tool(
                    "run_manage",
                    {
                        "action": "start",
                        "workflow": "gh-audit-repo",
                        "repository": "example/repo",
                        "n": 1,
                    },
                )
                assert not started.is_error
                assert started.structured_content["next_actions"] == ["synchronize-history"]
                state = runtime.state("gh-audit-repo")
                worktree = Path(state["audit_worktree"])
                assert worktree.is_dir()
                assert worktree != workspace
                assert state["inputs"]["n"] == 1
                assert state["phases"]["source"]["status"] == "complete"
                runtime.run_manage(
                    RunManageRequest(
                        action="resume",
                        workflow="gh-audit-repo",
                        n=4,
                    )
                )
                state = runtime.state("gh-audit-repo")
                assert state["scheduler"]["limit"] == 4
                assert state["inputs"]["n"] == 4

                prepared = await client.call_tool(
                    "history_manage",
                    {
                        "action": "prepare",
                        "workflow": "gh-audit-repo",
                    },
                )
                assert not prepared.is_error
                assert prepared.structured_content["mode"] == "new"
                assert prepared.structured_content["history"]["sync_status"] == "prepared"

                assert prepared.structured_content["history"]["base_generation"] == 0
                assert prepared.structured_content["history"]["record_count"] == 0
                assert not prepared.structured_content["history"]["full_history_complete"]
                assert (
                    runtime.state("gh-audit-repo")["phases"]["history"]["status"] == "in-progress"
                )
                ingested = await client.call_tool(
                    "history_manage",
                    {
                        "action": "ingest",
                        "workflow": "gh-audit-repo",
                        "records": [
                            {
                                "kind": "issue",
                                "number": 1,
                                "state": "open",
                                "title": "Cached issue",
                                "body": "Body",
                                "updated_at": "2026-08-31T00:00:00Z",
                            },
                            {
                                "kind": "pull",
                                "number": 3,
                                "state": "merged",
                                "title": "Cached pull request",
                            },
                        ],
                    },
                )
                assert ingested.structured_content["accepted"] == 2
                assert ingested.structured_content["history"]["record_count"] == 2
                qwen_home = root / "qwen-home"
                artifact_dir = qwen_home / "tmp" / "session-1" / "tool-results"
                artifact_dir.mkdir(parents=True)
                artifact = artifact_dir / "github-page.txt"
                artifact.write_text(
                    json.dumps(
                        {
                            "issues": [
                                {
                                    "number": 2,
                                    "state": "closed",
                                    "title": "Artifact issue",
                                    "body": "Large body " * 3000,
                                    "updated_at": "2026-08-31T01:00:00Z",
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                artifact.chmod(0o600)
                with mock.patch.dict(os.environ, {"QWEN_HOME": str(qwen_home)}):
                    artifact_ingested = await client.call_tool(
                        "history_manage",
                        {
                            "action": "ingest",
                            "workflow": "gh-audit-repo",
                            "artifacts": [{"kind": "issue", "path": str(artifact)}],
                        },
                    )
                assert artifact_ingested.structured_content["accepted"] == 1
                assert artifact_ingested.structured_content["history"]["record_count"] == 3
                assert "Large body" not in json.dumps(artifact_ingested.structured_content)
                assert "Large body" not in json.dumps(runtime.state("gh-audit-repo"))
                journal = runtime.current("gh-audit-repo") / "journal.jsonl"
                assert "Large body" not in journal.read_text(encoding="utf-8")
                committed = await client.call_tool(
                    "history_manage",
                    {
                        "action": "commit",
                        "workflow": "gh-audit-repo",
                        "full_history_complete": True,
                    },
                )
                assert not committed.is_error
                assert committed.structured_content["generation"] == 1
                assert runtime.state("gh-audit-repo")["phases"]["history"]["status"] == "complete"
                cached = await client.call_tool(
                    "history_query",
                    {
                        "workflow": "gh-audit-repo",
                        "linked": [{"kind": "issue", "number": 2}],
                        "limit": 1,
                    },
                )
                compact = cached.structured_content["records"][0]
                assert compact["summary"] == "Artifact issue"
                assert "body" not in compact
                assert "comments" not in compact
                assert "relationships" not in compact
                assert "commits" not in compact
                history_status = await client.call_tool(
                    "history_manage",
                    {
                        "action": "status",
                        "workflow": "gh-audit-repo",
                    },
                )
                assert not history_status.structured_content["changed"]
                summary = history_status.structured_content["history"]
                assert summary["cache_source"] == "committed"
                assert summary["generation"] == 1
                assert summary["record_count"] == 3
                assert summary["full_history_complete"]
                assert summary["last_sync_at"]
                assert summary["default_sha"] == state["sha"]

                reused = await client.call_tool(
                    "history_manage",
                    {
                        "action": "prepare",
                        "workflow": "gh-audit-repo",
                    },
                )
                assert reused.structured_content["mode"] == "reuse"
                assert reused.structured_content["history"]["base_generation"] == 1
                assert reused.structured_content["history"]["generation"] == 1
                inherited = await client.call_tool(
                    "history_manage",
                    {
                        "action": "commit",
                        "workflow": "gh-audit-repo",
                    },
                )
                assert inherited.structured_content["generation"] == 2
                inherited_status = await client.call_tool(
                    "history_manage",
                    {"action": "status", "workflow": "gh-audit-repo"},
                )
                assert inherited_status.structured_content["history"]["full_history_complete"]

                candidate_id = "candidate-mcp-1"
                runtime.audit_record(
                    AuditRecordRequest(
                        action="candidate",
                        candidate={"id": candidate_id, "status": "discovered"},
                    )
                )
                patched = runtime.audit_record(
                    AuditRecordRequest(
                        action="candidate",
                        candidate={"id": candidate_id, "observation": "ready to probe"},
                    )
                )
                assert patched["operation"] == "updated"
                assert runtime.state("gh-audit-repo")["candidates"][candidate_id]["status"] == (
                    "discovered"
                )
                phase_patch = runtime.audit_record(
                    AuditRecordRequest(
                        action="phase",
                        phase={"name": "verification", "summary": {"planned": 1}},
                    )
                )
                assert phase_patch["operation"] == "updated"
                assert runtime.state("gh-audit-repo")["phases"]["verification"]["planned"] == 1
                probed = await client.call_tool(
                    "audit_probe",
                    {
                        "kind": "python",
                        "probe_id": "probe-mcp-1",
                        "candidate_id": candidate_id,
                        "code": "print('ok')",
                    },
                )
                assert not probed.is_error
                probe_result = probed.structured_content
                assert probe_result["status"] == "succeeded"
                assert probe_result["validation_recorded"]
                assert "ok" in probe_result["stdout_excerpt"]
                validation = runtime.state("gh-audit-repo")["validations"]["probe-mcp-1"]
                assert validation["candidate_id"] == candidate_id
                assert validation["status"] == "succeeded"
                assert validation["artifact"] == "validation/probe-mcp-1/result.json"

    def test_audit_worktree_uses_private_cache_when_local_root_is_not_ignored(self) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-worktree-root-") as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            self.git("init", "-b", "main", cwd=workspace)

            runtime = WorkflowRuntime(workspace, root / "qwen-project")
            cache = root / "cache"

            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}):
                worktree_root = runtime._worktree_root()

            assert worktree_root.parent == cache / "agent-workflows" / "worktrees"
            assert stat.S_IMODE(worktree_root.stat().st_mode) == 0o700
            assert stat.S_IMODE(worktree_root.parent.stat().st_mode) == 0o700

    def test_audit_worktree_rejects_symlinked_application_cache(self) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-worktree-cache-") as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            cache = root / "cache"
            cache.mkdir()
            target = root / "untrusted"
            target.mkdir()
            (cache / "agent-workflows").symlink_to(target, target_is_directory=True)
            runtime = WorkflowRuntime(workspace, root / "qwen-project")

            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}):
                with pytest.raises(PermissionError):
                    runtime._cache_worktree_root()

    def test_manifest_and_launcher_are_contained(self) -> None:
        manifest = json.loads((EXTENSION / "qwen-extension.json").read_text(encoding="utf-8"))
        server = manifest["mcpServers"]["github_workflows"]
        assert manifest["skills"] == "skills"
        assert manifest["agents"] == "agents"
        hooks = json.loads((EXTENSION / "hooks/hooks.json").read_text(encoding="utf-8"))
        configured_hook = hooks["hooks"]["PreToolUse"][0]
        assert "run_shell_command" in configured_hook["matcher"]
        hook_command = configured_hook["hooks"][0]["command"]
        assert "${extensionPath}" in hook_command
        assert (EXTENSION / "hooks/guard-audit-boundary.py").stat().st_mode & 0o111
        assert "${extensionPath}" in server["command"]
        assert (
            manifest["mcpServers"]["github_workflows"]["command"]
            == "${extensionPath}${/}..${/}..${/}.venv${/}bin${/}agent-workflows"
        )
        for discovered in (EXTENSION / "skills", EXTENSION / "agents", EXTENSION / "references"):
            for path in discovered.rglob("*"):
                if path.is_symlink():
                    pytest.fail(f"extension resource must not be a symlink: {path}")
