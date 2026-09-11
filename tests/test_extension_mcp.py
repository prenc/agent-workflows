from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from jsonschema import Draft7Validator
from mcp import Client
from pydantic import ValidationError

from github_workflows import feedback
from github_workflows import runtime as runtime_module
from github_workflows.mcp_server import (
    _render_validation_error,
    _request_invocation_id,
    _validation_issues,
    create_server,
)
from github_workflows.models import (
    AuditRecordRequest,
    InventoryProgramRequest,
    KnowledgeRequest,
    RunManageRequest,
    TaskManageRequest,
)
from github_workflows.runtime import WorkflowRuntime


def skill_text(path: Path) -> str:
    """Return a skill's always-loaded instructions and local stage references."""
    documents = [path]
    references = path.parent / "references"
    if references.is_dir():
        documents.extend(
            sorted(
                candidate
                for candidate in references.glob("*.md")
                if candidate.is_file() and not candidate.is_symlink()
            )
        )
    return "\n".join(document.read_text(encoding="utf-8") for document in documents)


ROOT = Path(__file__).parents[1]
EXTENSION = ROOT / "extensions/github-workflows"


class TestExtensionMcp:
    @staticmethod
    def curation_assignment(runtime: WorkflowRuntime, issue: int, **extra: Any) -> dict[str, Any]:
        artifacts = runtime.current("gh-curate-issues") / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        bundle = artifacts / f"bundle-{issue}.json"
        (artifacts / f"issue-{issue}.json").write_text("{}\n", encoding="utf-8")
        bundle.write_text(
            json.dumps(
                {
                    "selected_issue": {
                        "kind": "issue",
                        "number": issue,
                        "state": "open",
                        "snapshot": f"artifacts/issue-{issue}.json",
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
            "issue_snapshot": f"artifacts/issue-{issue}.json",
            "candidate_bundle": f"artifacts/{bundle.name}",
            **extra,
        }

    @staticmethod
    def implementation_assignment(issue: int) -> dict[str, Any]:
        return {
            "issues": [
                {"number": issue, "snapshot": f"issue-{issue}.json", "accepted_scope": "scope"}
            ],
            "pull_request": {"state": "none"},
            "worktree": ".worktrees/unit",
            "branch": "work/unit",
            "rebased_base_sha": "a" * 40,
            "remote_lease": {"state": "absent"},
            "round_objective": "Complete the accepted scope",
            "acceptance_condition": "Focused validation passes",
            "repository_instructions": ["AGENTS.md"],
            "validation_plan": ["pytest"],
            "execution_environment": {"mode": "shared", "pythonpath": ["src"]},
        }

    @staticmethod
    def invocation_context(session_id: str, prompt_id: str) -> SimpleNamespace:
        request_context = SimpleNamespace(
            meta={
                "qwen-code/invocation": {
                    "version": 1,
                    "sessionId": session_id,
                    "promptId": prompt_id,
                }
            }
        )
        return SimpleNamespace(request_context=request_context)

    def test_qwen_invocation_identity_is_stable_only_within_one_prompt(self) -> None:
        first = _request_invocation_id(self.invocation_context("session-a", "prompt-a"))
        repeated = _request_invocation_id(self.invocation_context("session-a", "prompt-a"))
        later = _request_invocation_id(self.invocation_context("session-a", "prompt-b"))

        assert first is not None
        assert first == repeated
        assert first != later
        assert _request_invocation_id(None) is None

    def test_inventory_program_request_bounds_and_probe_contract(self) -> None:
        over_cap = [{"name": "python"} for _ in range(101)]
        with pytest.raises(ValidationError) as validation:
            InventoryProgramRequest.model_validate({"action": "program", "programs": over_cap})
        issues = _validation_issues(validation.value, {"action": "program"})
        assert [(issue.field, issue.requirement) for issue in issues] == [
            ("programs", "must contain at most 100 item(s)")
        ]

        at_cap = InventoryProgramRequest.model_validate(
            {"action": "program", "programs": [{"name": "python"} for _ in range(100)]}
        )
        assert len(at_cap.programs) == 100

        with pytest.raises(ValidationError) as validation:
            InventoryProgramRequest.model_validate(
                {"action": "program", "programs": [{"name": "bad name"}]}
            )
        issues = _validation_issues(validation.value, {"action": "program"})
        assert [(issue.field, issue.kind) for issue in issues] == [
            ("programs[].name", "string_pattern_mismatch")
        ]

        with pytest.raises(ValidationError) as validation:
            InventoryProgramRequest.model_validate(
                {
                    "action": "program",
                    "programs": [{"name": "python", "arguments": ["--rc-file"]}],
                }
            )
        issues = _validation_issues(validation.value, {"action": "program"})
        assert [(issue.field, issue.kind) for issue in issues] == [
            ("programs[].arguments", "value_error")
        ]
        assert issues[0].requirement == "program probes accept only version/help arguments"

        with pytest.raises(ValidationError) as validation:
            InventoryProgramRequest.model_validate(
                {
                    "action": "program",
                    "programs": [{"name": "python", "arguments": ["--version"] * 101}],
                }
            )
        issues = _validation_issues(validation.value, {"action": "program"})
        assert [(issue.field, issue.requirement) for issue in issues] == [
            ("programs[].arguments", "must contain at most 100 item(s)")
        ]

    async def test_execution_blocked_retry_uses_mcp_invocation_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-invocation-") as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            runtime = WorkflowRuntime(workspace, root / "qwen-project")
            first_meta = self.invocation_context("session-a", "prompt-a").request_context.meta
            later_meta = self.invocation_context("session-a", "prompt-b").request_context.meta
            async with Client(create_server(runtime), raise_exceptions=False) as client:
                await client.call_tool(
                    "run_manage",
                    {
                        "action": "start",
                        "workflow": "gh-curate-issues",
                        "repository": "example/repo",
                    },
                )
                planned = await client.call_tool(
                    "task_manage",
                    {
                        "action": "plan",
                        "workflow": "gh-curate-issues",
                        "task": {
                            "logical_id": "unit-a",
                            "assignment": self.curation_assignment(runtime, 1),
                        },
                    },
                    meta=first_meta,
                )
                task_id = planned.structured_content["task_id"]
                await client.call_tool(
                    "task_manage",
                    {
                        "action": "mark_running",
                        "workflow": "gh-curate-issues",
                        "task_id": task_id,
                    },
                    meta=first_meta,
                )
                failed = await client.call_tool(
                    "task_manage",
                    {
                        "action": "fail",
                        "workflow": "gh-curate-issues",
                        "task_id": task_id,
                        "note": "execution-blocked",
                    },
                    meta=first_meta,
                )
                assert not failed.is_error

                same_invocation = await client.call_tool(
                    "task_manage",
                    {"action": "retry", "workflow": "gh-curate-issues", "task_id": task_id},
                    meta=first_meta,
                )
                assert same_invocation.is_error
                retried = await client.call_tool(
                    "task_manage",
                    {"action": "retry", "workflow": "gh-curate-issues", "task_id": task_id},
                    meta=later_meta,
                )
                assert not retried.is_error
                assert retried.structured_content["task"]["attempt"] == 2

    async def test_execution_blocked_retry_without_mcp_metadata_requires_resume(self) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-no-invocation-") as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            runtime = WorkflowRuntime(workspace, root / "qwen-project")
            async with Client(create_server(runtime), raise_exceptions=False) as client:
                await client.call_tool(
                    "run_manage",
                    {
                        "action": "start",
                        "workflow": "gh-curate-issues",
                        "repository": "example/repo",
                    },
                )
                planned = await client.call_tool(
                    "task_manage",
                    {
                        "action": "plan",
                        "workflow": "gh-curate-issues",
                        "task": {
                            "logical_id": "unit-a",
                            "assignment": self.curation_assignment(runtime, 1),
                        },
                    },
                )
                task_id = planned.structured_content["task_id"]
                await client.call_tool(
                    "task_manage",
                    {
                        "action": "mark_running",
                        "workflow": "gh-curate-issues",
                        "task_id": task_id,
                    },
                )
                failed = await client.call_tool(
                    "task_manage",
                    {
                        "action": "fail",
                        "workflow": "gh-curate-issues",
                        "task_id": task_id,
                        "note": "execution-blocked",
                    },
                )
                assert not failed.is_error
                retry = await client.call_tool(
                    "task_manage",
                    {"action": "retry", "workflow": "gh-curate-issues", "task_id": task_id},
                )
                assert retry.is_error
                await client.call_tool(
                    "run_manage", {"action": "pause", "workflow": "gh-curate-issues"}
                )
                await client.call_tool(
                    "run_manage", {"action": "resume", "workflow": "gh-curate-issues"}
                )
                retry = await client.call_tool(
                    "task_manage",
                    {"action": "retry", "workflow": "gh-curate-issues", "task_id": task_id},
                )
                assert not retry.is_error
                assert retry.structured_content["task"]["attempt"] == 2

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
                    "tool",
                }
                assert tools["workflow_feedback"].annotations.idempotent_hint is False
                context_properties = tools["task_context"].input_schema["properties"]
                assert "task_ref" in context_properties
                run_properties = tools["run_manage"].input_schema["properties"]
                assert "n" in run_properties
                assert "repository" in run_properties
                assert "instructions" in run_properties
                assert "implement" in run_properties
                assert "reconcile_open" in run_properties
                assert "newly created issues" in run_properties["implement"]["description"]
                assert "confirmed_source_sha" in run_properties
                assert "acknowledge_pending_publication" in run_properties
                assert set(run_properties["outcome"]["enum"]) == {
                    "complete",
                    "blocked",
                }
                for tool in tools.values():
                    for property_schema in tool.input_schema.get("properties", {}).values():
                        assert "anyOf" not in property_schema
                        assert property_schema.get("default", object()) is not None
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
                assert "workflow" in tools["task_manage"].input_schema["required"]
                task_schema = json.dumps(tools["task_manage"].input_schema["properties"]["task"])
                assert "snapshot and accepted_scope are non-empty compact strings" in task_schema
                assert 'pull_request={\\"state\\":\\"none\\"}' in task_schema
                report_schema = tools["task_manage"].input_schema["properties"]["report"]
                assert report_schema["type"] == "object"
                assert "anyOf" not in report_schema
                assert "report" not in tools["task_manage"].input_schema["required"]
                assert "candidate_id" in tools["audit_probe"].input_schema["required"]
                run_properties = tools["run_manage"].input_schema["properties"]
                assert "required and non-empty" in run_properties["targets"]["description"]
                assert run_properties["targets"]["type"] == "array"
                assert run_properties["targets"]["items"]["pattern"] == r"\S"
                assert "External mutations" in run_properties["pending"]["description"]
                history_properties = tools["history_manage"].input_schema["properties"]
                assert "records" in history_properties
                assert "artifacts" in history_properties
                assert "At most 100 compact records" in history_properties["records"]["description"]
                assert (
                    "combined issue and pull contents"
                    in history_properties["artifacts"]["description"]
                )
                inventory_properties = tools["audit_inventory"].input_schema["properties"]
                assert "facts" in inventory_properties
                assert "fact" in inventory_properties
                audit_record_properties = tools["audit_record"].input_schema["properties"]
                assert "candidate" in audit_record_properties
                assert "phase" in audit_record_properties
                assert audit_record_properties["activity"]["properties"]["kind"]["description"]
                assert audit_record_properties["activity"]["properties"]["unit"]["description"]
                structured_arguments = {
                    "run_manage": {"targets": "array", "pending": "array"},
                    "task_manage": {"task": "object", "report": "object"},
                    "history_manage": {"records": "array", "artifacts": "array"},
                    "history_query": {"linked": "array"},
                    "audit_inventory": {
                        "programs": "array",
                        "facts": "object",
                        "fact": "object",
                    },
                    "audit_knowledge": {
                        "areas": "array",
                        "findings": "array",
                        "versions": "object",
                    },
                    "audit_probe": {"selectors": "array"},
                    "audit_record": {
                        "phase": "object",
                        "shard": "object",
                        "candidate": "object",
                        "verdict": "object",
                        "pending": "array",
                        "head_drift": "object",
                        "activity": "object",
                    },
                    "audit_publish": {"receipt": "object"},
                }
                for tool_name, fields in structured_arguments.items():
                    properties = tools[tool_name].input_schema["properties"]
                    for field, kind in fields.items():
                        assert properties[field]["type"] == kind
                        assert "anyOf" not in properties[field]
                        assert properties[field]["description"].endswith(
                            f"Send a JSON {kind} value; do not JSON-encode it as a string."
                        )
                query_schema = tools["history_query"].input_schema
                linked_property = query_schema["properties"]["linked"]
                linked_record = query_schema["$defs"]["LinkedRecord"]
                assert linked_property["items"]["$ref"] == "#/$defs/LinkedRecord"
                assert linked_property["maxItems"] == 100
                assert linked_record["required"] == ["kind", "number"]
                assert linked_record["additionalProperties"] is False
                assert linked_record["properties"]["kind"]["enum"] == ["issue", "pull"]
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
                assert conditional_required["audit_publish"]["failed"] == {"error"}
                inventory_program_then = next(
                    condition["then"]
                    for condition in tools["audit_inventory"].input_schema["allOf"]
                    if condition.get("if", {}).get("properties", {}).get("action", {}).get("const")
                    == "program"
                )
                assert inventory_program_then["properties"]["programs"]["maxItems"] == 100
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
                    if "oneOf" in condition.get("then", {})
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
                chained_audit = RunManageRequest(
                    action="start",
                    workflow="gh-audit-repo",
                    repository="example/repo",
                    n=5,
                    implement=True,
                )
                assert chained_audit.invocation()["implement"] is True
                assert chained_audit.invocation()["n"] == 5
                reconciled_audit = RunManageRequest(
                    action="start",
                    workflow="gh-audit-repo",
                    repository="example/repo",
                    reconcile_open=True,
                )
                assert reconciled_audit.invocation()["reconcile_open"] is True
                with pytest.raises(ValueError, match="incompatible with dry_run"):
                    RunManageRequest(
                        action="start",
                        workflow="gh-audit-repo",
                        repository="example/repo",
                        dry_run=True,
                        implement=True,
                    )
                with pytest.raises(ValidationError):
                    RunManageRequest(
                        action="start",
                        workflow="gh-audit-repo",
                        repository="example/repo",
                        confirmed_source_sha="abc",
                    )
                with pytest.raises(ValueError, match="does not accept"):
                    RunManageRequest(
                        action="start",
                        workflow="gh-curate-issues",
                        repository="example/repo",
                        confirmed_source_sha="0" * 40,
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
                            "assignment": self.curation_assignment(
                                runtime,
                                12,
                                source_kind="python-library",
                                accepted_scope="Normalize the public API issue",
                            ),
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
                            "assignment": self.curation_assignment(
                                runtime,
                                12,
                                source_kind="python-library",
                                accepted_scope="Revised scope",
                            ),
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
                assigned_artifacts = context.structured_content["run_context"]["assigned_artifacts"]
                assert set(assigned_artifacts) == {"candidate_bundle", "issue_snapshot"}
                assert "run_dir" not in context.structured_content["run_context"]
                for field, path in assigned_artifacts.items():
                    resolved = Path(path)
                    assert resolved.is_absolute()
                    assert resolved.is_file()
                    assert (
                        resolved
                        == (
                            runtime.current("gh-curate-issues")
                            / context.structured_content["assignment"][field]
                        ).resolve()
                    )
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

    async def test_public_schemas_reject_static_argument_mistakes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-schema-") as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            runtime = WorkflowRuntime(workspace, root / "qwen-project")
            tools = {tool.name: tool for tool in await create_server(runtime).list_tools()}

        invalid = [
            (
                "workflow_feedback",
                {
                    "message": "Conflicting context",
                    "error_ref": "err-0123456789ab",
                },
            ),
            (
                "run_manage",
                {
                    "action": "start",
                    "workflow": "gh-implement-issue",
                    "repository": "example/repo",
                    "targets": ["#1"],
                    "pending": [],
                },
            ),
            (
                "run_manage",
                {
                    "action": "finish",
                    "workflow": "gh-implement-issue",
                    "outcome": "blocked",
                },
            ),
            (
                "run_manage",
                {
                    "action": "start",
                    "workflow": "gh-audit-repo",
                    "repository": "example/repo",
                    "separate": True,
                },
            ),
            (
                "run_manage",
                {
                    "action": "start",
                    "workflow": "gh-audit-repo",
                    "repository": "example/repo",
                    "n": 0,
                },
            ),
            (
                "run_manage",
                {
                    "action": "start",
                    "workflow": "gh-audit-repo",
                    "repository": "example/repo",
                    "confirmed_source_sha": "abc",
                },
            ),
            (
                "run_manage",
                {
                    "action": "start",
                    "workflow": "gh-audit-repo",
                    "repository": "example/repo",
                    "note": None,
                },
            ),
            (
                "task_manage",
                {"action": "plan", "task": {"logical_id": "task-1"}, "report": {}},
            ),
            (
                "history_manage",
                {"action": "commit", "records": []},
            ),
            (
                "history_manage",
                {
                    "action": "ingest",
                    "records": [{"kind": "issue", "number": 1}],
                    "artifacts": [{"kind": "issue", "path": "issue.json"}],
                },
            ),
            ("history_manage", {"action": "commit", "default_sha": "abc"}),
            ("history_query", {}),
            ("history_query", {"state": None}),
            ("history_query", {"state": "open", "limit": 0}),
            (
                "history_query",
                {"linked": [{"kind": "issue", "number": n} for n in range(101)]},
            ),
            ("audit_inventory", {"action": "status", "facts": {}}),
            (
                "audit_inventory",
                {"action": "program", "programs": [{"name": "python"} for _ in range(101)]},
            ),
            ("audit_knowledge", {"action": "show", "findings": []}),
            (
                "audit_probe",
                {
                    "kind": "python",
                    "probe_id": "probe-1",
                    "candidate_id": "candidate-1",
                    "code": "pass",
                    "selectors": ["tests"],
                },
            ),
            (
                "audit_record",
                {"action": "supervisor_finish", "phase": {"name": "source"}},
            ),
            (
                "audit_publish",
                {
                    "action": "failed",
                    "candidate_id": "candidate-1",
                    "error": "publication failed",
                    "receipt": {},
                },
            ),
        ]
        valid = [
            ("workflow_feedback", {"message": "Clear workflow friction"}),
            (
                "run_manage",
                {
                    "action": "start",
                    "workflow": "gh-implement-issue",
                    "repository": "example/repo",
                    "targets": ["#1"],
                },
            ),
            (
                "run_manage",
                {"action": "checkpoint", "workflow": "gh-curate-issues", "pending": []},
            ),
            (
                "run_manage",
                {
                    "action": "finish",
                    "workflow": "gh-implement-issue",
                    "outcome": "blocked",
                    "note": "Maintainer input is required",
                },
            ),
            (
                "task_manage",
                {
                    "action": "plan",
                    "workflow": "gh-audit-repo",
                    "task": {"logical_id": "task-1"},
                },
            ),
            (
                "history_manage",
                {"action": "ingest", "records": [{"kind": "issue", "number": 1}]},
            ),
            (
                "history_manage",
                {
                    "action": "ingest",
                    "records": [{"kind": "issue", "number": 1}],
                    "artifacts": [],
                },
            ),
            (
                "history_manage",
                {
                    "action": "ingest",
                    "artifacts": [{"kind": "issue", "path": "issue.json"}],
                    "records": [],
                },
            ),
            ("history_manage", {"action": "commit", "default_sha": "a" * 40}),
            ("history_query", {"state": "open", "limit": 100}),
            ("audit_inventory", {"action": "program", "programs": [{"name": "python"}]}),
            ("audit_knowledge", {"action": "update", "area": "area/core", "findings": []}),
            (
                "audit_probe",
                {
                    "kind": "python",
                    "probe_id": "probe-1",
                    "candidate_id": "candidate-1",
                    "code": "pass",
                },
            ),
            ("audit_record", {"action": "pending", "pending": []}),
            (
                "audit_publish",
                {"action": "uncertain", "candidate_id": "candidate-1", "receipt": {}},
            ),
        ]
        for tool in tools.values():
            Draft7Validator.check_schema(tool.input_schema)
        for tool_name, arguments in invalid:
            assert list(Draft7Validator(tools[tool_name].input_schema).iter_errors(arguments)), (
                tool_name,
                arguments,
            )
        for tool_name, arguments in valid:
            assert not list(
                Draft7Validator(tools[tool_name].input_schema).iter_errors(arguments)
            ), (tool_name, arguments)
        with pytest.raises(ValidationError):
            TaskManageRequest.model_validate({"action": "plan", "task": {"logical_id": "task-1"}})

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
                assert unknown.content[0].text.startswith(
                    "target is not accepted; did you mean targets?"
                )
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
                    "action=start does not accept pending"
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
                        "task": {
                            "logical_id": "issue-5",
                            "assignment": self.implementation_assignment(5),
                        },
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

    async def test_rendered_corrections_do_not_repeat_field_names(self) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-error-dup-") as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            runtime = WorkflowRuntime(workspace, root / "qwen-project")
            cases = [
                (
                    "run_manage",
                    {
                        "action": "start",
                        "workflow": "gh-implement-issue",
                        "repository": "example/repo",
                        "targets": [""],
                    },
                    "targets must contain only non-blank references",
                ),
                (
                    "run_manage",
                    {
                        "action": "start",
                        "workflow": "gh-audit-repo",
                        "repository": "example/repo",
                        "instructions": "   ",
                    },
                    "instructions must not be blank",
                ),
                (
                    "audit_record",
                    {
                        "action": "verdict",
                        "verdict": {"id": "C-1", "candidate_id": "C-1"},
                    },
                    "verdict uses candidate_id as its sole identity; id is not accepted",
                ),
                (
                    "history_manage",
                    {"action": "ingest"},
                    "ingest requires exactly one of records or artifacts",
                ),
            ]
            async with Client(
                create_server(runtime),
                raise_exceptions=False,
                read_timeout_seconds=0.1,
            ) as client:
                for tool_name, arguments, correction in cases:
                    result = await client.call_tool(tool_name, arguments)
                    assert result.is_error, (tool_name, arguments)
                    assert result.content[0].text.startswith(correction), (
                        tool_name,
                        result.content[0].text,
                    )

        # The n value_error is masked end-to-end by the tool's own PositiveInteger
        # parameter check, so pin its rendered form at the MCP rendering layer.
        with pytest.raises(ValidationError) as validation:
            RunManageRequest(
                action="start",
                workflow="gh-curate-issues",
                repository="example/repo",
                n=0,
            )
        rendered = _render_validation_error(validation.value, {"action": "start"})
        assert rendered == "n must be a positive integer"

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
                mismatched = await client.call_tool(
                    "audit_probe",
                    {
                        "kind": "python",
                        "probe_id": "probe-1",
                        "candidate_id": "candidate-1",
                        "selectors": ["tests"],
                    },
                )
                assert mismatched.content[0].text.startswith(
                    "kind=python requires code; kind=python does not accept selectors"
                )
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
                for tool_name, arguments in invalid_calls:
                    result = await client.call_tool(tool_name, arguments)
                    assert result.is_error
                    assert len(result.content) == 1
                    message = result.content[0].text
                    assert "\n" not in message
                    assert internal_diagnostic.search(message) is None
                    assert "Consider workflow_feedback" not in message
                    if tool_name == "workflow_feedback":
                        assert "If unclear" not in message
                    else:
                        assert message.endswith(
                            'If unclear, call workflow_feedback(message="what was confusing").'
                        )

                qwen_meta = self.invocation_context(
                    "session-feedback", "prompt-feedback"
                ).request_context.meta
                recorded = await client.call_tool(
                    "workflow_feedback",
                    {"message": "The rejected request was difficult to correct"},
                    meta=qwen_meta,
                )
                assert not recorded.is_error
                assert recorded.structured_content["context_attached"] is True
                assert (
                    recorded.structured_content["ref"]
                    == recorded.structured_content["feedback_id"][-8:]
                )
                stored = feedback.find(recorded.structured_content["feedback_id"])
                assert stored["provenance"]["client"]["name"]
                assert stored["provenance"]["server_version"]
                conversation = stored["provenance"]["conversation"]
                assert conversation["client"] == "qwen"
                assert conversation["session_id"] == "session-feedback"
                assert conversation["prompt_id"] == "prompt-feedback"
                assert conversation["mcp_request_id"]
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
                assert public_crash.endswith(
                    'If unclear, call workflow_feedback(message="what was confusing").'
                )
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
                        task={
                            "logical_id": "issue-12",
                            "assignment": (
                                self.curation_assignment(runtime, 12)
                                if workflow == "gh-curate-issues"
                                else self.implementation_assignment(12)
                            ),
                        },
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

    def test_task_context_references_fail_loudly_outside_checkout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-references-") as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            runtime = WorkflowRuntime(workspace, root / "qwen-project")
            runtime.run_manage(
                RunManageRequest(
                    action="start",
                    workflow="gh-curate-issues",
                    repository="example/repo",
                    n=1,
                )
            )
            receipt = runtime.task_manage(
                TaskManageRequest(
                    action="plan",
                    workflow="gh-curate-issues",
                    task={
                        "logical_id": "issue-12",
                        "assignment": self.curation_assignment(runtime, 12),
                    },
                )
            )
            site_packages = root / "venv" / "lib" / "python3" / "site-packages"
            (site_packages / "github_workflows").mkdir(parents=True)
            with mock.patch.object(
                runtime_module,
                "__file__",
                str(site_packages / "github_workflows" / "runtime.py"),
            ):
                with pytest.raises(RuntimeError, match="extension references root is missing"):
                    runtime.task_context(receipt["task_ref"])

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
                            "assignment": self.curation_assignment(
                                runtime, int(task_id.split("-")[1])
                            ),
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

    def test_generic_assignments_are_validated_before_attempt_creation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-assignment-") as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            curator = WorkflowRuntime(workspace, root / "qwen-curator")
            curator.run_manage(
                RunManageRequest(
                    action="start",
                    workflow="gh-curate-issues",
                    repository="example/repo",
                )
            )
            assignment = self.curation_assignment(curator, 1)
            bundle = curator.current("gh-curate-issues") / assignment["candidate_bundle"]
            payload = json.loads(bundle.read_text(encoding="utf-8"))
            payload["matches"] = [
                {"kind": "issue", "number": 2, "state": "open", "snapshot": "issue-2"},
                {"kind": "issue", "number": 2, "state": "closed", "snapshot": "issue-2"},
            ]
            bundle.write_text(json.dumps(payload), encoding="utf-8")
            with pytest.raises(ValueError, match="contradictory issue #2"):
                curator.task_manage(
                    TaskManageRequest(
                        action="plan",
                        workflow="gh-curate-issues",
                        task={"logical_id": "issue-1", "assignment": assignment},
                    )
                )
            assert curator.state("gh-curate-issues")["tasks"] == {}

            implementer = WorkflowRuntime(workspace, root / "qwen-implementer")
            implementer.run_manage(
                RunManageRequest(
                    action="start",
                    workflow="gh-implement-issue",
                    repository="example/repo",
                    targets=["#1"],
                )
            )
            incomplete = self.implementation_assignment(1)
            incomplete.pop("rebased_base_sha")
            with pytest.raises(ValueError, match="rebased_base_sha"):
                implementer.task_manage(
                    TaskManageRequest(
                        action="plan",
                        workflow="gh-implement-issue",
                        task={"logical_id": "unit-1", "assignment": incomplete},
                    )
                )
            assert implementer.state("gh-implement-issue")["tasks"] == {}

            for index, pull_request in enumerate(
                ({"state": "draft"}, {"state": "none", "number": 1})
            ):
                invalid_pull = self.implementation_assignment(1)
                invalid_pull["pull_request"] = pull_request
                with pytest.raises(ValueError, match="pull_request"):
                    implementer.task_manage(
                        TaskManageRequest(
                            action="plan",
                            workflow="gh-implement-issue",
                            task={
                                "logical_id": f"invalid-pull-{index}",
                                "assignment": invalid_pull,
                            },
                        )
                    )

            existing_pull_fields = {
                "state": "open",
                "number": 44,
                "head_sha": "b" * 40,
                "initial_draft": True,
                "pr_round_mode": "implementation",
                "pr_expected_end_state": "draft",
                "required_worker_draft": True,
            }
            for field in (
                "initial_draft",
                "pr_round_mode",
                "pr_expected_end_state",
                "required_worker_draft",
            ):
                invalid_pull = self.implementation_assignment(1)
                invalid_pull["pull_request"] = {
                    key: value for key, value in existing_pull_fields.items() if key != field
                }
                with pytest.raises(ValueError, match=field):
                    implementer.task_manage(
                        TaskManageRequest(
                            action="plan",
                            workflow="gh-implement-issue",
                            task={
                                "logical_id": f"missing-{field}",
                                "assignment": invalid_pull,
                            },
                        )
                    )

            for logical_id, field, value in (
                ("invalid-initial-draft", "initial_draft", "false"),
                ("invalid-required-draft", "required_worker_draft", 1),
                ("invalid-round-mode", "pr_round_mode", "review"),
                ("invalid-end-state", "pr_expected_end_state", "ready"),
            ):
                invalid_pull = self.implementation_assignment(1)
                invalid_pull["pull_request"] = {**existing_pull_fields, field: value}
                with pytest.raises(ValueError, match=field):
                    implementer.task_manage(
                        TaskManageRequest(
                            action="plan",
                            workflow="gh-implement-issue",
                            task={"logical_id": logical_id, "assignment": invalid_pull},
                        )
                    )

            for logical_id, changes, message in (
                (
                    "invalid-implementation-state",
                    {"required_worker_draft": False},
                    "implementation PR rounds",
                ),
                (
                    "invalid-verification-draft",
                    {
                        "initial_draft": True,
                        "pr_round_mode": "verification-only",
                        "pr_expected_end_state": "unchanged",
                        "required_worker_draft": False,
                    },
                    "verification-only PR rounds",
                ),
            ):
                invalid_pull = self.implementation_assignment(1)
                invalid_pull["pull_request"] = {**existing_pull_fields, **changes}
                with pytest.raises(ValueError, match=message):
                    implementer.task_manage(
                        TaskManageRequest(
                            action="plan",
                            workflow="gh-implement-issue",
                            task={"logical_id": logical_id, "assignment": invalid_pull},
                        )
                    )

            existing_pull = self.implementation_assignment(1)
            existing_pull["pull_request"] = existing_pull_fields
            assert (
                implementer.task_manage(
                    TaskManageRequest(
                        action="plan",
                        workflow="gh-implement-issue",
                        task={"logical_id": "existing-pull", "assignment": existing_pull},
                    )
                )["task"]["assignment"]["pull_request"]["number"]
                == 44
            )

            ready_pull = self.implementation_assignment(1)
            ready_pull["pull_request"] = {**existing_pull_fields, "initial_draft": False}
            ready = implementer.task_manage(
                TaskManageRequest(
                    action="plan",
                    workflow="gh-implement-issue",
                    task={"logical_id": "ready-existing-pull", "assignment": ready_pull},
                )
            )
            assert ready["task"]["assignment"]["pull_request"]["initial_draft"] is False

            verification_only = self.implementation_assignment(1)
            verification_only["pull_request"] = {
                **existing_pull_fields,
                "initial_draft": False,
                "pr_round_mode": "verification-only",
                "pr_expected_end_state": "unchanged",
                "required_worker_draft": False,
            }
            verified = implementer.task_manage(
                TaskManageRequest(
                    action="plan",
                    workflow="gh-implement-issue",
                    task={
                        "logical_id": "verification-only-pull",
                        "assignment": verification_only,
                    },
                )
            )
            assert verified["task"]["assignment"]["pull_request"]["pr_round_mode"] == (
                "verification-only"
            )

            worktree = workspace / ".worktrees" / "unit"
            (worktree / "src").mkdir(parents=True)
            outside = root / "outside-src"
            outside.mkdir()
            (worktree / "escape").symlink_to(outside, target_is_directory=True)
            for index, pythonpath in enumerate(
                (
                    ["/tmp/src"],
                    ["../../other"],
                    ["src/../other"],
                    [r"src\other"],
                    ["missing"],
                    ["escape"],
                )
            ):
                invalid = self.implementation_assignment(1)
                invalid["execution_environment"]["pythonpath"] = pythonpath
                with pytest.raises(ValueError, match="pythonpath"):
                    implementer.task_manage(
                        TaskManageRequest(
                            action="plan",
                            workflow="gh-implement-issue",
                            task={
                                "logical_id": f"invalid-path-{index}",
                                "assignment": invalid,
                            },
                        )
                    )

            valid = self.implementation_assignment(1)
            valid["validation_plan"] = []
            planned = implementer.task_manage(
                TaskManageRequest(
                    action="plan",
                    workflow="gh-implement-issue",
                    task={"logical_id": "no-safe-validation", "assignment": valid},
                )
            )
            assert planned["task"]["assignment"]["validation_plan"] == []

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
                    task={
                        "logical_id": "unit-1",
                        "assignment": self.implementation_assignment(1),
                    },
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
                    task={
                        "logical_id": "optional",
                        "assignment": self.implementation_assignment(2),
                        "required": False,
                    },
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

    async def test_mcp_reconcile_preserves_an_omitted_existing_custom_title(self) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-knowledge-mcp-") as directory:
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

            runtime = WorkflowRuntime(workspace, root / "qwen-project")
            async with Client(
                create_server(runtime), raise_exceptions=True, read_timeout_seconds=0.1
            ) as client:
                started = await client.call_tool(
                    "run_manage",
                    {
                        "action": "start",
                        "workflow": "gh-audit-repo",
                        "repository": "example/repo",
                    },
                )
                assert not started.is_error
                definition = {
                    "area": "area/core",
                    "title": "Custom Core",
                    "description": "Core behavior.",
                    "paths": ["module.py"],
                }
                created = await client.call_tool(
                    "audit_knowledge", {"action": "reconcile", "areas": [definition]}
                )
                assert not created.is_error

                definition.pop("title")
                unchanged = await client.call_tool(
                    "audit_knowledge", {"action": "reconcile", "areas": [definition]}
                )
                assert unchanged.structured_content == {
                    "created": [],
                    "invalidated": [],
                    "unchanged": ["area/core"],
                }
                shown = await client.call_tool(
                    "audit_knowledge", {"action": "show", "area": "area/core"}
                )
                assert shown.structured_content["area"]["title"] == "Custom Core"

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
                        "default_sha": state["sha"],
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
                        "workflow": "gh-audit-repo",
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

    def test_audit_worktree_adds_private_exclude_and_uses_local_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-worktree-root-") as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            self.git("init", "-b", "main", cwd=workspace)
            self.git("config", "core.excludesFile", "/dev/null", cwd=workspace)
            exclude = workspace / ".git" / "info" / "exclude"
            exclude.write_text("# existing local rules\n", encoding="utf-8")

            runtime = WorkflowRuntime(workspace, root / "qwen-project")
            cache = root / "cache"

            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}):
                worktree_root = runtime._worktree_root()
                repeated = runtime._worktree_root()

            assert worktree_root == (workspace / ".worktrees").resolve()
            assert repeated == worktree_root
            assert stat.S_IMODE(worktree_root.stat().st_mode) == 0o700
            assert exclude.read_text(encoding="utf-8") == ("# existing local rules\n.worktrees/\n")
            assert not cache.exists()
            ignored = subprocess.run(
                [
                    "git",
                    "-C",
                    str(workspace),
                    "check-ignore",
                    "-q",
                    "--no-index",
                    ".worktrees/probe",
                ],
                check=False,
            )
            assert ignored.returncode == 0
            status = subprocess.run(
                ["git", "-C", str(workspace), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            )
            assert status.stdout == ""

    def test_audit_worktree_rejects_symlinked_private_exclude(self) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-worktree-exclude-") as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            self.git("init", "-b", "main", cwd=workspace)
            self.git("config", "core.excludesFile", "/dev/null", cwd=workspace)
            exclude = workspace / ".git" / "info" / "exclude"
            exclude.unlink()
            target = root / "untrusted-exclude"
            target.write_text("", encoding="utf-8")
            exclude.symlink_to(target)
            runtime = WorkflowRuntime(workspace, root / "qwen-project")

            with pytest.raises(PermissionError, match="owned regular file"):
                runtime._worktree_root()
            assert target.read_text(encoding="utf-8") == ""

    def test_audit_worktree_rejects_symlinked_git_info_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-worktree-info-") as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            self.git("init", "-b", "main", cwd=workspace)
            self.git("config", "core.excludesFile", "/dev/null", cwd=workspace)
            info = workspace / ".git" / "info"
            exclude = info / "exclude"
            existing = exclude.read_bytes()
            target = root / "untrusted-info"
            target.mkdir()
            info.rename(workspace / ".git" / "original-info")
            info.symlink_to(target, target_is_directory=True)
            runtime = WorkflowRuntime(workspace, root / "qwen-project")

            with pytest.raises(PermissionError, match="info directory"):
                runtime._worktree_root()
            assert not (target / "exclude").exists()
            assert (workspace / ".git" / "original-info" / "exclude").read_bytes() == existing

    def test_audit_worktree_appends_to_non_utf8_private_exclude(self) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-worktree-bytes-") as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            self.git("init", "-b", "main", cwd=workspace)
            self.git("config", "core.excludesFile", "/dev/null", cwd=workspace)
            exclude = workspace / ".git" / "info" / "exclude"
            exclude.write_bytes(b"# local \xffrule")
            runtime = WorkflowRuntime(workspace, root / "qwen-project")

            assert runtime._worktree_root() == (workspace / ".worktrees").resolve()
            assert exclude.read_bytes() == b"# local \xffrule\n.worktrees/\n"

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
        locator_hook = hooks["hooks"]["PostToolUse"][0]
        assert locator_hook["matcher"] == "^mcp__github_workflows__workflow_feedback$"
        assert locator_hook["hooks"][0]["command"].endswith(
            "agent-workflows _feedback-locator-hook"
        )
        assert (EXTENSION / "hooks/guard-audit-boundary.py").stat().st_mode & 0o111
        assert "${extensionPath}" in server["command"]
        assert (
            manifest["mcpServers"]["github_workflows"]["command"]
            == "${extensionPath}${/}..${/}..${/}.venv${/}bin${/}agent-workflows"
        )
        canonical_references = (EXTENSION / "references").resolve()
        for discovered in (EXTENSION / "skills", EXTENSION / "agents", EXTENSION / "references"):
            for path in discovered.rglob("*"):
                if path.is_symlink():
                    resolved = path.resolve(strict=True)
                    assert path.parent.name == "references"
                    assert path.parent.parent.parent == EXTENSION / "skills"
                    assert resolved.is_file()
                    assert resolved.parent == canonical_references

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
            for term in (
                "`issue_write`",
                "`issue_number`",
                "complete desired",
                "omitted label field",
                "skip the",
            )
        )
        assert "### Pull request taxonomy" in issue_conventions
        assert re.search(r"multiple area\s+or type labels", issue_conventions)
        assert "exactly one priority label" in issue_conventions
        priority_positions = [
            issue_conventions.index(f"`{name}`", 4000) for name in ("high", "medium", "low")
        ]
        assert priority_positions == sorted(priority_positions)
        assert "no fixed maximum label count" in issue_conventions.lower()

    def test_workflows_consistently_handle_derived_pr_taxonomy(self) -> None:
        documents = {
            "curator": EXTENSION / "skills/gh-curate-issues/SKILL.md",
            "curator worker": EXTENSION / "agents/gh-curate-issues-worker.md",
            "implementation": EXTENSION / "skills/gh-implement-issue/SKILL.md",
            "pickup": ROOT / "codex/skills/gh-pickup-work/SKILL.md",
        }

        for name, path in documents.items():
            content = skill_text(path)
            assert "highest" in content, name
            assert "area" in content, name
            assert "type" in content, name
            assert "priority" in content, name

    def test_codex_workflows_avoid_non_actionable_public_chatter(self) -> None:
        pickup = skill_text(ROOT / "codex/skills/gh-pickup-work/SKILL.md")
        assessment = (ROOT / "codex/skills/gh-pickup-work/references/assessment.md").read_text(
            encoding="utf-8"
        )
        comment_format = (
            ROOT / "codex/skills/gh-pickup-work/references/comment-format.md"
        ).read_text(encoding="utf-8")
        pickup = " ".join(pickup.split())
        assessment = " ".join(assessment.split())
        comment_format = " ".join(comment_format.split())

        assert all(
            term in assessment
            for term in (
                "transactionally claim every open graph node",
                "compare every non-lifecycle snapshot fact",
                "This applies to linked and unlinked PRs",
                "sole durable GitHub mutation in the ordinary clean case is adding a missing PR",
                "Delete this skill's existing managed PR comment",
                "deletion of an obsolete owned managed comment",
                "Finalize PRs before issues",
            )
        )
        assert all(
            term in comment_format
            for term in ("clean result", "Never post", "Delete an owned managed PR comment")
        )
        assert all(
            term in pickup
            for term in (
                "Never publish a clean-result comment",
                "do not manufacture an implementation change",
                "apply only a missing `ready-to-merge` label",
                "attempt a read-only verification fast path",
                "complete GitHub no-op",
                "transactionally claim every issue and the PR",
                "replace PR `in-progress` with `ready-to-merge`",
            )
        )

    def test_pickup_owns_assessment_and_implementation_modes(self) -> None:
        skill = ROOT / "codex/skills/gh-pickup-work"
        source = (skill / "SKILL.md").read_text(encoding="utf-8")
        source = " ".join(source.split())
        assessment = (skill / "references/assessment.md").read_text(encoding="utf-8")

        assert not (ROOT / "codex/skills/gh-reassess-work").exists()
        assert all(
            term in source
            for term in (
                "`--assess-only` ends after assessment",
                "complete it before any implementation mutation",
                "publish the managed finding",
                "stop before implementation",
                "may assess a non-cohesive",
            )
        )
        assert all(
            term in assessment
            for term in (
                "transactionally claim every open graph node",
                "Create or update an issue-specific managed reassessment comment",
                "Delete this skill's existing managed PR comment",
                "Apply PR `ready-to-merge`",
            )
        )
        assert (skill / "scripts/update_managed_comment.py").is_file()

    def test_large_skills_route_to_stage_references(self) -> None:
        expected = {
            ROOT / "codex/skills/gh-pickup-work/SKILL.md": (
                "assessment.md",
                "workspace-and-implementation.md",
                "publication.md",
            ),
            EXTENSION / "skills/gh-audit-repo/SKILL.md": (
                "run-and-context.md",
                "discovery-and-verification.md",
                "validation-and-publication.md",
            ),
            EXTENSION / "skills/gh-implement-issue/SKILL.md": (
                "intake-and-worktrees.md",
                "implementation-rounds.md",
                "promotion-and-finalization.md",
            ),
            EXTENSION / "skills/gh-curate-issues/SKILL.md": (
                "history-and-workers.md",
                "reconciliation-and-publication.md",
            ),
        }

        for source, reference_names in expected.items():
            content = source.read_text(encoding="utf-8")
            assert len(content.splitlines()) < 500
            for name in reference_names:
                assert f"references/{name}" in content
                assert (source.parent / "references" / name).is_file()

    def test_qwen_workflows_skip_non_actionable_github_mutations(self) -> None:
        implementation = skill_text(EXTENSION / "skills/gh-implement-issue/SKILL.md")
        worker = (EXTENSION / "agents/gh-implement-issue-worker.md").read_text(encoding="utf-8")
        curator = skill_text(EXTENSION / "skills/gh-curate-issues/SKILL.md")
        implementation = " ".join(implementation.split())
        worker = " ".join(worker.split())
        curator = " ".join(curator.split())

        assert "publish no issue comment" in implementation
        assert "never propose a public comment" in worker
        assert "complete desired state per issue and linked PR" in curator
        assert "Never resubmit an identical complete label set" in curator
        assert "record a true no-op and perform no GitHub write" in curator

    def test_audit_can_handoff_new_and_partial_issues_for_implementation(self) -> None:
        audit = skill_text(EXTENSION / "skills/gh-audit-repo/SKILL.md")
        handoff = (
            EXTENSION / "skills/gh-audit-repo/references/validation-and-publication.md"
        ).read_text(encoding="utf-8")
        implementation = skill_text(EXTENSION / "skills/gh-implement-issue/SKILL.md")

        assert "--dry-run | --implement" in audit
        assert "same\n`-n`" in audit
        assert "successful create receipts" in handoff
        assert "exact `partial` label" in handoff
        assert "continue independent units without asking the user" in handoff
        assert "preflight and\nauthorization carry into this workflow" in implementation

    def test_audit_can_reconcile_every_open_issue_and_pull_before_discovery(self) -> None:
        audit = skill_text(EXTENSION / "skills/gh-audit-repo/SKILL.md")
        reconciliation = (
            EXTENSION / "skills/gh-audit-repo/references/open-reconciliation.md"
        ).read_text(encoding="utf-8")

        assert "--reconcile-open" in audit
        assert "connected issue/PR graphs" in reconciliation
        assert "Age and inactivity are never disposition evidence" in reconciliation
        assert "Never apply `ready-to-merge`" in reconciliation
        assert "detached managed\nworktree" in reconciliation

    def test_implementation_pr_template_omits_empty_and_validation_sections(self) -> None:
        template = (EXTENSION / "references/github-pr-template.md").read_text(encoding="utf-8")
        normalized = " ".join(template.split())
        core_template = template.split("```markdown", 1)[1].split("```", 1)[0]

        assert "Omit `Deviations / Non-goals`" in normalized
        assert "Omit `Risks or follow-up`" in normalized
        assert "Do not include validation information anywhere" in normalized
        assert "Never include empty sections" in normalized
        assert "None identified" not in core_template
        assert "Closes #<issue>" in core_template
        assert "repository-relative file" in normalized
        assert "Never include line numbers, line ranges" in normalized
        assert (ROOT / "codex/skills/gh-pickup-work/references/pr-template.md").resolve() == (
            EXTENSION / "references/github-pr-template.md"
        ).resolve()

    def test_public_github_evidence_uses_stable_files_and_symbols(self) -> None:
        issue_conventions = (EXTENSION / "references/github-issue-conventions.md").read_text(
            encoding="utf-8"
        )
        reassessment_comments = (
            ROOT / "codex/skills/gh-pickup-work/references/comment-format.md"
        ).read_text(encoding="utf-8")
        audit_worker = (EXTENSION / "agents/gh-audit-repo-worker.md").read_text(encoding="utf-8")

        for document in (issue_conventions, reassessment_comments, audit_worker):
            normalized = " ".join(document.split())
            assert "line numbers" in normalized
            assert "line ranges" in normalized
            assert "commit-pinned line links" in normalized
        assert "`src/package/module.py` or `Package.method`" in issue_conventions
        assert "immutable SHAs in private workflow evidence" in issue_conventions

    def test_implementation_guidance_requires_evidence_based_validation_and_drafts(self) -> None:
        supervisor = skill_text(EXTENSION / "skills/gh-implement-issue/SKILL.md")
        worker = (EXTENSION / "agents/gh-implement-issue-worker.md").read_text(encoding="utf-8")
        runtime_policy = (EXTENSION / "references/github-runtime-policy.md").read_text(
            encoding="utf-8"
        )

        for document in (supervisor, worker):
            assert all(
                term in document
                for term in (
                    "initial_draft",
                    "required_worker_draft",
                    "pr_round_mode",
                    "pr_expected_end_state",
                    "verification-only",
                    "shebang",
                    "pre-edit SHA",
                    "baseline limitation",
                )
            )

        assert "Omit optional" in runtime_policy
        assert "explicit `null` is not accepted" in runtime_policy
        assert "no workflow call occurred" in runtime_policy
        assert all(
            term in supervisor
            for term in ("GIT_TERMINAL_PROMPT=0", "push --dry-run", "exact remote")
        )
        codex_skill = skill_text(ROOT / "codex/skills/gh-pickup-work/SKILL.md")
        assert all(
            term in codex_skill
            for term in ("GIT_TERMINAL_PROMPT=0", "push --dry-run", "exact remote")
        )
        worker_frontmatter = worker.split("---", 2)[1]
        assert "  - edit" in worker_frontmatter
        assert all(
            term in worker
            for term in (
                "`verification-only` round is valid only",
                "Never change draft state",
                "before any commit",
                "PR unchanged",
                "potentially mutating",
            )
        )
        assert all(
            term in supervisor
            for term in (
                "inherited draft always uses implementation mode",
                "inherited ready PR",
                "never assign pre-commit",
                "Finalize that already-ready PR unchanged",
            )
        )

    def test_audit_guidance_handles_provider_and_assignment_failures(self) -> None:
        supervisor = skill_text(EXTENSION / "skills/gh-audit-repo/SKILL.md")
        worker = (EXTENSION / "agents/gh-audit-repo-worker.md").read_text(encoding="utf-8")

        for document in (supervisor, worker):
            assert all(term in document for term in ("provider quota", "unavailable", "official"))
        assert all(term in supervisor for term in ("every assigned path exists", "closing-PR"))
        discover_mode = worker.split("## Discover mode", 1)[1].split("## Verify mode", 1)[0]
        assert "check conclusions" in discover_mode

    def test_github_access_falls_back_before_stopping(self) -> None:
        policy = (EXTENSION / "references/github-access.md").read_text(encoding="utf-8")
        normalized = " ".join(policy.split())

        assert all(
            term in normalized
            for term in (
                "Prefer GitHub MCP as the primary interface",
                "connected status badge",
                "authenticated `git`/`gh` fallback",
                "neither route",
                "both failures",
            )
        )

    def test_github_skills_prefer_mcp_with_cli_fallback(self) -> None:
        skills = (
            ROOT / "codex/skills/gh-pickup-work/SKILL.md",
            EXTENSION / "skills/gh-propose-enhancement/SKILL.md",
            EXTENSION / "skills/gh-curate-issues/SKILL.md",
            EXTENSION / "skills/gh-implement-issue/SKILL.md",
            EXTENSION / "skills/gh-audit-repo/SKILL.md",
        )

        for path in skills:
            document = skill_text(path) + (EXTENSION / "references/github-access.md").read_text(
                encoding="utf-8"
            )
            normalized = " ".join(document.split())
            assert "Prefer" in normalized
            assert "GitHub MCP" in normalized
            assert "authenticated `gh`" in normalized
            assert any(
                term in normalized
                for term in ("neither route", "neither access path", "both routes")
            )

    def test_extension_skills_use_skill_local_references(self) -> None:
        for path in (EXTENSION / "skills").glob("*/SKILL.md"):
            assert "../../references/" not in path.read_text(encoding="utf-8")

    def test_skills_route_directly_to_canonical_contracts(self) -> None:
        for name in (
            "gh-propose-enhancement",
            "gh-curate-issues",
            "gh-implement-issue",
            "gh-audit-repo",
        ):
            skill = EXTENSION / "skills" / name
            source = (skill / "SKILL.md").read_text(encoding="utf-8")
            assert "references/github-access.md" in source
            assert "references/github-runtime-policy.md" in source
            assert "references/workflow-policy.md" not in source
            assert not (skill / "references/workflow-policy.md").exists()

    def test_skill_packages_follow_portable_structure(self) -> None:
        roots = (ROOT / "codex/skills", EXTENSION / "skills")
        name_pattern = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
        link_pattern = re.compile(r"\[[^]]+\]\(([^)#]+)(?:#[^)]+)?\)")

        for root in roots:
            for skill in (path for path in root.iterdir() if path.is_dir()):
                source = skill / "SKILL.md"
                assert source.is_file()
                content = source.read_text(encoding="utf-8")
                assert content.startswith("---\n")
                frontmatter = content.split("---", 2)[1]
                name = re.search(r"^name:\s*(\S+)\s*$", frontmatter, re.MULTILINE)
                description = re.search(r"^description:\s*(?:>|>-)?", frontmatter, re.MULTILINE)
                assert name is not None
                assert description is not None
                assert name.group(1) == skill.name
                assert name_pattern.fullmatch(name.group(1))

                resources = [source]
                references = skill / "references"
                if references.is_dir():
                    resources.extend(
                        path
                        for path in references.iterdir()
                        if path.is_file() and not path.is_symlink()
                    )
                for resource in resources:
                    for raw_link in link_pattern.findall(resource.read_text(encoding="utf-8")):
                        relative = Path(raw_link)
                        assert not relative.is_absolute()
                        assert ".." not in relative.parts
                        target = (resource.parent / relative).resolve()
                        assert target.exists()
                        assert target.is_relative_to(ROOT.resolve())

        manifest = json.loads((EXTENSION / "qwen-extension.json").read_text(encoding="utf-8"))
        assert manifest["skills"] == "skills"

    def test_skill_local_canonical_references_do_not_drift(self) -> None:
        references = {
            "gh-propose-enhancement": (
                "github-access.md",
                "github-issue-conventions.md",
                "github-runtime-policy.md",
            ),
            "gh-curate-issues": (
                "github-access.md",
                "github-issue-conventions.md",
                "github-runtime-policy.md",
            ),
            "gh-implement-issue": (
                "github-access.md",
                "github-issue-conventions.md",
                "github-pr-template.md",
                "github-runtime-policy.md",
            ),
            "gh-audit-repo": (
                "github-access.md",
                "github-issue-conventions.md",
                "github-runtime-policy.md",
            ),
        }

        for skill, names in references.items():
            for name in names:
                canonical = EXTENSION / "references" / name
                local = EXTENSION / "skills" / skill / "references" / name
                assert local.is_symlink()
                assert local.resolve(strict=True) == canonical.resolve(strict=True)
                assert local.read_bytes() == canonical.read_bytes()

    def test_audit_guidance_uses_server_owned_candidate_fingerprints(self) -> None:
        supervisor = skill_text(EXTENSION / "skills/gh-audit-repo/SKILL.md")
        worker = (EXTENSION / "agents/gh-audit-repo-worker.md").read_text(encoding="utf-8")

        for document in (supervisor, worker):
            assert "candidate_fingerprint" in document
            assert "server-owned" in document
            assert "never calculate" in document

    def test_audit_guidance_makes_inventory_authoritative_for_host_facts(self) -> None:
        supervisor = skill_text(EXTENSION / "skills/gh-audit-repo/SKILL.md")
        worker = (EXTENSION / "agents/gh-audit-repo-worker.md").read_text(encoding="utf-8")

        for document in (supervisor, worker):
            assert "authoritative" in document
            assert "audit host" in document or "audit-host" in document
            assert "deployment constraints" in document
            assert "standard-library root" in document

    def test_audit_guidance_handles_notebooks_and_full_issue_reads_explicitly(self) -> None:
        supervisor = skill_text(EXTENSION / "skills/gh-audit-repo/SKILL.md")
        worker = (EXTENSION / "agents/gh-audit-repo-worker.md").read_text(encoding="utf-8")
        policy = (EXTENSION / "references/github-runtime-policy.md").read_text(encoding="utf-8")

        assert all(term in supervisor for term in ("`.ipynb` path", "cell index", "text anchor"))
        assert all(term in worker for term in ("exact-file `grep_search`", "offset/limit"))
        assert all(
            term in policy
            for term in (
                "`issue_read` does not expose `fields` or `minimal_output`",
                "preliminary filtering",
                "exact full issue read",
                "Do not replace it with broad pagination",
            )
        )

    def test_worktree_environment_contract_is_consistent_across_agents(self) -> None:
        workflow_documents = [
            skill_text(ROOT / "codex/skills/gh-pickup-work/SKILL.md"),
            (EXTENSION / "references/github-runtime-policy.md").read_text(encoding="utf-8"),
            skill_text(EXTENSION / "skills/gh-implement-issue/SKILL.md"),
            (EXTENSION / "agents/gh-implement-issue-worker.md").read_text(encoding="utf-8"),
        ]

        for document in workflow_documents:
            assert all(
                contract in document
                for contract in (
                    "shared",
                    "isolated",
                    "UV_NO_SYNC",
                    "PYTHONPATH",
                    "`.venv`",
                    "directly",
                )
            )
            assert "$PWD" not in document
            assert "PYTHONPATH=src" not in document
            assert all(term in document for term in ("assigned worktree", "absolute"))

        assert all("uv run" not in document for document in workflow_documents)

        assert all("UV_NO_SYNC=1" in document for document in workflow_documents)
        assert all(
            re.search(r"known to invoke\s+nested uv", document) for document in workflow_documents
        )
        assert all("UV_NO_SYNC=1` on every" not in document for document in workflow_documents)

        supervisor_documents = workflow_documents[:3]
        for document in supervisor_documents:
            assert all(
                contract in document
                for contract in ("uv.lock", "offline", "frozen", "tracked", "ignored")
            )

        for document in supervisor_documents:
            normalized = document.lower()
            assert all(
                contract in normalized for contract in ("uv lock --check", "unlink", "symlink")
            )

        runtime_policy = workflow_documents[1]
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

    def test_worktree_workflows_use_repository_private_excludes(self) -> None:
        documents = (
            skill_text(ROOT / "codex/skills/gh-pickup-work/SKILL.md"),
            skill_text(EXTENSION / "skills/gh-implement-issue/SKILL.md"),
            skill_text(EXTENSION / "skills/gh-audit-repo/SKILL.md"),
        )

        for document in documents:
            document = " ".join(document.split())
            assert ".git/info/exclude" in document
            assert any(
                phrase in document
                for phrase in ("repository-private `.git/info/exclude`", "local rule only")
            )
            assert "<project>/.worktrees" in document
            assert "fallback root:" not in document

    def test_isolated_environment_preflights_every_validation_executable(self) -> None:
        supervisor = skill_text(EXTENSION / "skills/gh-implement-issue/SKILL.md")

        assert all(
            term in supervisor
            for term in (
                "complete repository-owned validation plan",
                "documented development",
                "every environment-owned executable",
                ".venv/bin/pre-commit",
                "block the unit before worker launch",
                "Never substitute",
            )
        )

    def test_runtime_policy_requires_explicit_workflow_after_session_restore(self) -> None:
        policy = (EXTENSION / "references/github-runtime-policy.md").read_text(encoding="utf-8")

        assert all(
            term in policy
            for term in (
                "exact `workflow`",
                "parent session is restored",
                "idempotent `run_manage` action `resume`",
            )
        )

    def test_worktree_workers_expose_reliable_search_tools(self) -> None:
        search_tools = {"run_shell_command", "grep_search", "read_file", "glob"}
        expected = {
            "gh-audit-repo-worker.md": {"run_shell_command", "grep_search", "read_file"},
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

    def test_audit_worker_documents_guarded_search_fallback(self) -> None:
        worker = (EXTENSION / "agents/gh-audit-repo-worker.md").read_text(encoding="utf-8")
        policy = (EXTENSION / "references/github-runtime-policy.md").read_text(encoding="utf-8")

        for document in (worker, policy):
            assert "references.rg_excludes" in document
            assert "parent" in document
            assert "direct `rg`" in document
            assert "task_context" in document
        assert "audit_worktree" in worker
        assert "authoritative" in policy
        assert "audit worktree" in policy
        assert "symlink" in policy
        assert "private workflow/run storage" in policy

    def test_audit_context_guidance_uses_server_evidence_and_candidate_verdict_identity(
        self,
    ) -> None:
        supervisor = skill_text(EXTENSION / "skills/gh-audit-repo/SKILL.md")
        worker = (EXTENSION / "agents/gh-audit-repo-worker.md").read_text(encoding="utf-8")

        assert "`candidate_id` as its sole identity" in supervisor
        assert "matching `audit_sha` and `audit_worktree_head`" in worker
        assert "bounded stdout/stderr excerpts" in worker

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

    def test_named_workers_use_unattended_modes_with_bounded_tools(self) -> None:
        frontmatters = {
            filename: (EXTENSION / "agents" / filename)
            .read_text(encoding="utf-8")
            .split("---", 2)[1]
            for filename in (
                "gh-audit-repo-worker.md",
                "gh-curate-issues-worker.md",
                "gh-implement-issue-worker.md",
            )
        }

        assert all("approvalMode: yolo" in value for value in frontmatters.values())
        assert "  - run_shell_command" in frontmatters["gh-audit-repo-worker.md"]
        assert "  - run_shell_command" in frontmatters["gh-curate-issues-worker.md"]
        assert "EXECUTION_BLOCKED" in (EXTENSION / "references/github-runtime-policy.md").read_text(
            encoding="utf-8"
        )

    def test_github_workers_fallback_without_changing_ownership(self) -> None:
        workers = tuple(
            (EXTENSION / "agents" / name).read_text(encoding="utf-8")
            for name in (
                "gh-audit-repo-worker.md",
                "gh-curate-issues-worker.md",
                "gh-implement-issue-worker.md",
            )
        )

        normalized_workers = tuple(" ".join(worker.split()) for worker in workers)
        for worker in normalized_workers:
            assert "authenticated `gh" in worker
            assert "both" in worker
            assert "fail" in worker
            assert "Never inspect or inject tokens" in worker
        implementation = normalized_workers[2]
        assert "worker-owned draft PR" in implementation
        assert "Keep all writes within" in implementation

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
                    "make the smallest set of bounded reads",
                    "skip that preliminary call",
                    "batches of at most five records",
                    "Reuse the resulting records",
                    "agent-feedback summary",
                    "`agent-feedback show <ref>...`",
                    "every explicitly approved",
                    "non-conflicting root-cause group",
                    "consolidate overlapping owning tests",
                    "agent-feedback close --input <JSON|file|->",
                    "do not wrap it in a `resolutions` object",
                    "Prefer `--input -` with stdin",
                    "defaults to the three preceding tool interactions",
                    "Ask the user before",
                    "using `--detail data`",
                    "permanent removal",
                    "commits, pushes, installation",
                    "MCP restart",
                    "`addressed`",
                    "`duplicate`",
                    "`external`",
                    "`not-actionable`",
                )
            )

        assert "agent-feedback add" in codex
        assert "mcp__github_workflows__workflow_feedback" in qwen

    def test_documented_schema_introspection_shape_is_executable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="github-workflows-schema-recipe-") as directory:
            root = Path(directory)
            workspace = root / "workspace"
            project_state = root / "project-state"
            workspace.mkdir()
            project_state.mkdir()
            server = create_server(WorkflowRuntime(workspace, project_state))
            tools = asyncio.run(server.list_tools())
        schema = next(tool.input_schema for tool in tools if tool.name == "audit_publish")
        Draft7Validator.check_schema(schema)
        assert "action" in schema["properties"]
