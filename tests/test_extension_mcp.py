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

from github_workflows import feedback
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
                    "workflow_feedback",
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
                assert all(
                    tool.input_schema["additionalProperties"] is False for tool in tools.values()
                )
                feedback_properties = tools["workflow_feedback"].input_schema["properties"]
                assert tools["workflow_feedback"].input_schema["required"] == ["message"]
                assert set(feedback_properties) == {
                    "message",
                    "task_ref",
                    "error_ref",
                    "tool",
                }
                assert tools["workflow_feedback"].annotations.idempotent_hint is False
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
                report_schema = tools["task_manage"].input_schema["properties"]["report"]
                assert report_schema["type"] == "object"
                assert "anyOf" not in report_schema
                assert "report" not in tools["task_manage"].input_schema["required"]
                assert "candidate_id" in tools["audit_probe"].input_schema["required"]
                run_properties = tools["run_manage"].input_schema["properties"]
                assert "required and non-empty" in run_properties["targets"]["description"]
                targets_array = next(
                    variant
                    for variant in run_properties["targets"]["anyOf"]
                    if variant.get("type") == "array"
                )
                assert targets_array["items"]["pattern"] == r"\S"
                assert "External mutations" in run_properties["pending"]["description"]
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
                assert conditional_required["audit_publish"]["begin"] == {"operation"}
                assert conditional_required["audit_publish"]["finish"] == {"receipt"}
                implementation_start = next(
                    condition["then"]
                    for condition in tools["run_manage"].input_schema["allOf"]
                    if condition.get("if", {}).get("allOf")
                )
                assert implementation_start["required"] == ["targets"]
                assert implementation_start["properties"]["targets"]["minItems"] == 1
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
                with pytest.raises(ValueError, match="must use audit_record"):
                    RunManageRequest(
                        action="checkpoint",
                        workflow="gh-audit-repo",
                        pending=[],
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
                assert re.fullmatch(r"curate:[0-9a-f]{12}:issue-12-1", task_ref)
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

    @pytest.mark.parametrize("targets", [[""], ["   "], ["#5", "\t"]])
    def test_run_manage_rejects_blank_target_references(self, targets: list[str]) -> None:
        with pytest.raises(ValueError, match="targets must contain only non-blank references"):
            RunManageRequest(
                action="start",
                workflow="gh-implement-issue",
                repository="example/repo",
                targets=targets,
            )

    async def test_run_manage_validates_targets_and_pending_lifecycle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-run-manage-") as directory:
            root = Path(directory)
            monkeypatch.setenv("XDG_CACHE_HOME", str(root / "cache"))
            workspace = root / "repo"
            workspace.mkdir()
            runtime = WorkflowRuntime(workspace, root / "qwen-project")
            workflow = "gh-implement-issue"
            base = {
                "action": "start",
                "workflow": workflow,
                "repository": "example/repo",
            }
            async with Client(
                create_server(runtime),
                raise_exceptions=False,
                read_timeout_seconds=0.1,
            ) as client:
                unknown = await client.call_tool("run_manage", {**base, "target": ["#5"]})
                assert unknown.is_error
                assert unknown.content[0].text.startswith("target is not accepted")
                assert not runtime.current(workflow).exists()

                malformed = await client.call_tool("run_manage", {**base, "targets": 5})
                assert malformed.is_error
                assert malformed.content[0].text.startswith("targets must be a list")
                assert not runtime.current(workflow).exists()

                for targets in ([""], ["   "], ["#5", "\t"]):
                    blank = await client.call_tool("run_manage", {**base, "targets": targets})
                    assert blank.is_error
                    assert not runtime.current(workflow).exists()

                missing = await client.call_tool("run_manage", base)
                assert missing.is_error
                assert missing.content[0].text.startswith(
                    "gh-implement-issue start requires at least one target"
                )
                assert not runtime.current(workflow).exists()

                start_pending = await client.call_tool(
                    "run_manage",
                    {**base, "targets": ["#5"], "pending": []},
                )
                assert start_pending.is_error
                assert start_pending.content[0].text.startswith(
                    "start does not accept fields: ['pending']"
                )
                assert not runtime.current(workflow).exists()

                started = await client.call_tool("run_manage", {**base, "targets": ["#5"]})
                assert not started.is_error
                assert runtime.state(workflow)["inputs"]["targets"] == ["#5"]
                assert started.structured_content["next_actions"] == ["plan-tasks"]

                revision = runtime.state(workflow)["revision"]
                premature = await client.call_tool(
                    "run_manage", {"action": "finish", "workflow": workflow}
                )
                assert premature.is_error
                assert "target work has not been planned" in premature.content[0].text
                assert runtime.state(workflow)["revision"] == revision

                pending = await client.call_tool(
                    "run_manage",
                    {
                        "action": "checkpoint",
                        "workflow": workflow,
                        "pending": ["issue #5 claim read-back"],
                    },
                )
                assert not pending.is_error
                assert pending.structured_content["next_actions"] == ["resolve-pending"]
                status = await client.call_tool("run_status", {"workflow": workflow})
                assert status.structured_content["pending"] == ["issue #5 claim read-back"]
                assert status.structured_content["scheduler"]["worker_slots"] == 0
                journal = runtime.current(workflow) / "journal.jsonl"
                assert json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])[
                    "event"
                ] == ("pending_updated")

                cleared = await client.call_tool(
                    "run_manage",
                    {"action": "checkpoint", "workflow": workflow, "pending": []},
                )
                assert not cleared.is_error
                assert cleared.structured_content["next_actions"] == ["plan-tasks"]

                planned = await client.call_tool(
                    "task_manage",
                    {
                        "action": "plan",
                        "workflow": workflow,
                        "task": {"logical_id": "issue-5"},
                    },
                )
                task_id = planned.structured_content["task_id"]
                await client.call_tool(
                    "task_manage",
                    {"action": "mark_running", "workflow": workflow, "task_id": task_id},
                )
                await client.call_tool(
                    "task_manage",
                    {
                        "action": "complete",
                        "workflow": workflow,
                        "task_id": task_id,
                        "report": {"status": "complete"},
                    },
                )
                await client.call_tool(
                    "task_manage",
                    {
                        "action": "integration_begin",
                        "workflow": workflow,
                        "task_id": task_id,
                    },
                )
                integrated = await client.call_tool(
                    "task_manage",
                    {
                        "action": "integration_end",
                        "workflow": workflow,
                        "task_id": task_id,
                    },
                )
                assert integrated.structured_content["scheduler"]["next_action"] == (
                    "ready-to-finish"
                )
                finished = await client.call_tool(
                    "run_manage", {"action": "finish", "workflow": workflow}
                )
                assert not finished.is_error

    async def test_expected_runtime_failure_is_actionable_tool_error(
        self, caplog: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-errors-") as directory:
            root = Path(directory)
            monkeypatch.setenv("XDG_CACHE_HOME", str(root / "cache"))
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
                    ("workflow_feedback", {}),
                ]
                internal_diagnostic = re.compile(
                    r"validation errors?|input_(?:value|type)|errors\.pydantic|Traceback"
                )
                failure_reference = None
                for tool_name, arguments in invalid_calls:
                    result = await client.call_tool(tool_name, arguments)
                    assert result.is_error
                    assert len(result.content) == 1
                    message = result.content[0].text
                    assert "\n" not in message
                    assert internal_diagnostic.search(message) is None
                    references = re.findall(r'error_ref="(err-[0-9a-f]{12})"', message)
                    if tool_name == "workflow_feedback":
                        assert references == []
                    else:
                        assert len(references) == 1
                        failure_reference = failure_reference or references[0]

                assert failure_reference is not None
                recorded = await client.call_tool(
                    "workflow_feedback",
                    {
                        "message": "The rejected request was difficult to correct",
                        "error_ref": failure_reference,
                    },
                )
                assert not recorded.is_error
                assert recorded.structured_content["context_attached"] is True
                assert (
                    recorded.structured_content["ref"]
                    == recorded.structured_content["feedback_id"][-8:]
                )
                stored = feedback.find(recorded.structured_content["feedback_id"])
                assert stored["tool"]
                assert stored["provenance"]["client"]["name"]
                assert stored["provenance"]["server_version"]

                expired = await client.call_tool(
                    "workflow_feedback",
                    {
                        "message": "A failure reference was no longer available",
                        "error_ref": "err-000000000000",
                    },
                )
                assert not expired.is_error
                assert expired.structured_content["context_attached"] is False
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
                assert re.search(r'error_ref="err-[0-9a-f]{12}"', public_crash)
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
                        targets=["#12"] if workflow == "gh-implement-issue" else [],
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
            prefixes = {
                "gh-curate-issues": "curate",
                "gh-implement-issue": "implement",
            }
            for workflow, task_ref in references.items():
                assert re.fullmatch(rf"{prefixes[workflow]}:[0-9a-f]{{12}}:issue-12-1", task_ref)
                context = runtime.task_context(task_ref)
                assert context["workflow"] == workflow
                assert context["task_id"] == "issue-12-1"
                # Legacy references remain accepted only while runs created before
                # short task references may still need to resume. Remove this case
                # with the corresponding compatibility branches in runtime.py.
                legacy_ref = f"{workflow}:{context['run_id']}:{context['task_id']}"
                assert runtime.task_context(legacy_ref)["task_ref"] == legacy_ref

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
            with pytest.raises(ValueError, match="task_ref is stale"):
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
                    targets=["#1"],
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
                    targets=["#2"],
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
                )
            )
            pending_runtime.run_manage(
                RunManageRequest(
                    action="checkpoint",
                    workflow="gh-curate-issues",
                    pending=["issue mutation read-back"],
                )
            )
            assert (
                pending_runtime.run_status("gh-curate-issues")["scheduler"]["next_action"]
                == "resolve-pending"
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

                revision = runtime.state("gh-audit-repo")["revision"]
                premature = await client.call_tool(
                    "audit_publish",
                    {
                        "action": "uncertain",
                        "candidate_id": "candidate-mcp-1",
                    },
                )
                assert premature.is_error
                assert premature.content[0].text.startswith("no publication is pending")
                assert runtime.state("gh-audit-repo")["revision"] == revision

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
                assert probe_result["artifact"] == "validation/probe-mcp-1/result.json"
                assert "ok" in probe_result["stdout_excerpt"]
                validation = runtime.state("gh-audit-repo")["validations"]["probe-mcp-1"]
                assert validation["candidate_id"] == candidate_id
                assert validation["status"] == "succeeded"
                assert validation["artifact"] == "validation/probe-mcp-1/result.json"

                verify_task = await client.call_tool(
                    "task_manage",
                    {
                        "action": "plan",
                        "task": {
                            "logical_id": "verify-mcp-1",
                            "assignment": {
                                "mode": "verify",
                                "candidate": {
                                    "id": candidate_id,
                                    "observation": "ready to probe",
                                },
                            },
                        },
                    },
                )
                assert not verify_task.is_error
                fingerprint = verify_task.structured_content["task"]["assignment"][
                    "candidate_fingerprint"
                ]
                assert len(fingerprint) == 64
                verify_context = await client.call_tool(
                    "task_context", {"task_ref": verify_task.structured_content["task_ref"]}
                )
                assert not verify_context.is_error
                assert (
                    verify_context.structured_content["assignment"]["candidate_fingerprint"]
                    == fingerprint
                )

                begun = await client.call_tool(
                    "audit_publish",
                    {
                        "action": "begin",
                        "candidate_id": candidate_id,
                        "operation": "no-op",
                    },
                )
                assert not begun.is_error
                finished = await client.call_tool(
                    "audit_publish",
                    {
                        "action": "finish",
                        "candidate_id": candidate_id,
                        "receipt": {"reason": "no publication needed"},
                    },
                )
                assert not finished.is_error
                assert runtime.state("gh-audit-repo")["candidates"][candidate_id]["status"] == (
                    "no-op"
                )

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

    def test_shared_guidance_defines_github_pagination_and_pr_label_calls(self) -> None:
        runtime_policy = (EXTENSION / "references/github-runtime-policy.md").read_text(
            encoding="utf-8"
        )
        issue_conventions = (EXTENSION / "references/github-issue-conventions.md").read_text(
            encoding="utf-8"
        )

        assert all(
            term in runtime_policy
            for term in ("`get_commit`", "`page`", "`perPage`", "first page alone")
        )
        assert all(
            term in issue_conventions
            for term in ("`issue_write`", "`issue_number`", "`get_labels`", "complete desired")
        )

    def test_implementation_guidance_requires_evidence_based_validation_and_drafts(self) -> None:
        supervisor = (EXTENSION / "skills/gh-implement-issue/SKILL.md").read_text(encoding="utf-8")
        worker = (EXTENSION / "agents/gh-implement-issue-worker.md").read_text(encoding="utf-8")

        for document in (supervisor, worker):
            assert all(
                term in document
                for term in (
                    "initial_draft",
                    "required_worker_draft",
                    "current state",
                    "shebang",
                    "pre-edit SHA",
                    "baseline limitation",
                )
            )

    def test_audit_guidance_uses_server_owned_candidate_fingerprints(self) -> None:
        supervisor = (EXTENSION / "skills/gh-audit-repo/SKILL.md").read_text(encoding="utf-8")
        worker = (EXTENSION / "agents/gh-audit-repo-worker.md").read_text(encoding="utf-8")

        for document in (supervisor, worker):
            assert "candidate_fingerprint" in document
            assert "server-owned" in document
            assert "never calculate" in document

    def test_audit_guidance_makes_inventory_authoritative_for_host_facts(self) -> None:
        supervisor = (EXTENSION / "skills/gh-audit-repo/SKILL.md").read_text(encoding="utf-8")
        worker = (EXTENSION / "agents/gh-audit-repo-worker.md").read_text(encoding="utf-8")

        for document in (supervisor, worker):
            assert "authoritative" in document
            assert "audit host" in document or "audit-host" in document
            assert "deployment constraints" in document
            assert "standard-library root" in document

    def test_worktree_environment_contract_is_consistent_across_agents(self) -> None:
        documents = [
            (ROOT / "user-policies/codex.md").read_text(encoding="utf-8"),
            (ROOT / "user-policies/qwen.md").read_text(encoding="utf-8"),
            (ROOT / "codex/skills/gh-pickup-work/SKILL.md").read_text(encoding="utf-8"),
            (EXTENSION / "references/github-runtime-policy.md").read_text(encoding="utf-8"),
            (EXTENSION / "skills/gh-implement-issue/SKILL.md").read_text(encoding="utf-8"),
            (EXTENSION / "agents/gh-implement-issue-worker.md").read_text(encoding="utf-8"),
        ]

        for document in documents:
            assert all(
                contract in document
                for contract in ("shared", "isolated", "UV_NO_SYNC", "PYTHONPATH")
            )

        supervisor_documents = documents[:5]
        for document in supervisor_documents:
            assert all(
                contract in document
                for contract in ("uv.lock", "offline", "frozen", "tracked", "ignored")
            )

        for document in documents[2:5]:
            normalized = document.lower()
            assert all(
                contract in normalized for contract in ("uv lock --check", "unlink", "symlink")
            )

        runtime_policy = documents[3]
        assert all(
            contract in runtime_policy
            for contract in (
                "execution_environment",
                "CORRECTION_NEEDED",
                "private `0700` cache",
                "stale tracked lock",
            )
        )
        assert runtime_policy.index("uv lock --check") < runtime_policy.index("unlink .venv")
        assert runtime_policy.index("unlink .venv") < runtime_policy.index(
            "\nuv lock --offline --no-python-downloads"
        )

    def test_worktree_workers_expose_reliable_search_tools(self) -> None:
        search_tools = {"run_shell_command", "grep_search", "read_file", "glob"}
        expected = {
            "gh-audit-repo-worker.md": {"grep_search", "read_file"},
            "gh-implement-issue-worker.md": {
                "run_shell_command",
                "grep_search",
                "read_file",
            },
        }

        for filename, tools in expected.items():
            frontmatter = (
                (EXTENSION / "agents" / filename).read_text(encoding="utf-8").split("---", 2)[1]
            )
            configured_tools = frontmatter.split("tools:", 1)[1].split("disallowedTools:", 1)[0]
            configured = {
                line.removeprefix("  - ")
                for line in configured_tools.splitlines()
                if line.startswith("  - ")
            }
            assert configured & search_tools == tools

    @pytest.mark.parametrize(
        "filename",
        [
            "gh-audit-repo-worker.md",
            "gh-curate-issues-worker.md",
            "gh-implement-issue-worker.md",
        ],
    )
    def test_named_workers_can_fetch_public_documentation(self, filename: str) -> None:
        frontmatter = (
            (EXTENSION / "agents" / filename).read_text(encoding="utf-8").split("---", 2)[1]
        )
        configured_tools = frontmatter.split("tools:", 1)[1].split("disallowedTools:", 1)[0]
        configured = {
            line.removeprefix("  - ")
            for line in configured_tools.splitlines()
            if line.startswith("  - ")
        }
        assert "web_fetch" in configured

    def test_feedback_skill_contract_is_consistent_across_clients(self) -> None:
        codex = (ROOT / "codex/skills/workflow-feedback/SKILL.md").read_text(encoding="utf-8")
        qwen = (EXTENSION / "skills/workflow-feedback/SKILL.md").read_text(encoding="utf-8")

        assert codex.startswith("---\nname: workflow-feedback\n")
        assert qwen.startswith("---\nname: workflow-feedback\n")
        for document in (codex, qwen):
            assert all(
                contract in document
                for contract in (
                    "does not change existing queue records",
                    "secondary-record exception",
                    "one primary operation",
                    "qualifying workflow friction",
                    "record needs no separate user request",
                    "does not expand the approved",
                    "Record and analyze feedback from the active",
                    "Only implementation work",
                    "requires a writable `agent-workflows` checkout",
                    "make one read call that matches the request",
                    "skip that preliminary call",
                    "Reuse the resulting records",
                    "feedback summary --json",
                    "`feedback show <ref>...`",
                    "every explicitly approved",
                    "non-conflicting root-cause group",
                    "consolidate overlapping owning tests",
                    "feedback close --input <JSON|file|->",
                    "Prefer `--input -` with stdin",
                    "Ask the user before calling `feedback trace`",
                    "never use `feedback remove`",
                    "commits, pushes, installation",
                    "MCP restart",
                    "`addressed`",
                    "`duplicate`",
                    "`external`",
                    "`not-actionable`",
                )
            )

        assert "agent-workflows feedback add" in codex
        assert "mcp__github_workflows__workflow_feedback" in qwen
