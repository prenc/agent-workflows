"""MCP tool registration for the GitHub workflow runtime."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .models import (
    AuditRecordRequest,
    HistoryManageRequest,
    HistoryQueryRequest,
    InventoryRequest,
    KnowledgeRequest,
    ProbeRequest,
    PublishRequest,
    RunManageRequest,
    TaskManageRequest,
    WorkflowName,
)
from .runtime import WorkflowRuntime

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
CONTROL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)


def _public_call(operation: Any, *arguments: Any) -> Any:
    """Return actionable domain failures without leaking implementation errors."""
    try:
        return operation(*arguments)
    except (ValueError, RuntimeError) as error:
        raise ToolError(str(error)) from error


def _request_call(operation: Any, model: type[Any], **values: Any) -> Any:
    """Validate a flat public call through the internal action model."""
    payload = {
        name: value for name, value in values.items() if value is not None and name != "runtime"
    }
    return _public_call(lambda: operation(model.model_validate(payload)))


def create_server(runtime: WorkflowRuntime) -> MCPServer:
    """Create a server whose tools operate on one validated workspace."""
    mcp = MCPServer("github-workflows")

    @mcp.tool(annotations=CONTROL_WRITE, structured_output=True)
    def run_manage(
        action: Literal["start", "resume", "checkpoint", "directive", "pause", "abort", "finish"],
        workflow: WorkflowName,
        repository: str | None = None,
        n: int | None = None,
        targets: list[str] | None = None,
        instructions: str | None = None,
        refresh_history: bool | None = None,
        regression_sweep: bool | None = None,
        dry_run: bool | None = None,
        separate: bool | None = None,
        pending: list[str] | None = None,
        source_confirmed: bool | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Start, resume, checkpoint, direct, pause, abort, or finish a workflow run."""
        return _request_call(runtime.run_manage, RunManageRequest, **locals())

    @mcp.tool(annotations=READ_ONLY, structured_output=True)
    def run_status(workflow: WorkflowName) -> dict[str, Any]:
        """Read a compact current-run, scheduler, task, and pending-work summary."""
        return _public_call(runtime.run_status, workflow)

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=True)
    def task_manage(
        action: Literal[
            "plan",
            "dispatch",
            "checkpoint",
            "report",
            "fail",
            "abandon",
            "retry",
            "integrate_start",
            "integrate_finish",
        ],
        task_id: str,
        workflow: WorkflowName = "gh-audit-repo",
        logical_id: str | None = None,
        agent_id: str | None = None,
        role: str | None = None,
        unit: str | None = None,
        assignment: dict[str, Any] | None = None,
        report: dict[str, Any] | None = None,
        note: str | None = None,
        required: bool | None = None,
    ) -> dict[str, Any]:
        """Register or transition one supervised task without a request wrapper."""
        return _request_call(runtime.task_manage, TaskManageRequest, **locals())

    @mcp.tool(annotations=READ_ONLY, structured_output=True)
    def task_context(task_ref: str) -> dict[str, Any]:
        """Resolve one namespaced task reference to its read-only worker assignment."""
        return _public_call(runtime.task_context, task_ref)

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=True)
    def history_manage(
        action: Literal["status", "prepare", "ingest", "commit", "abort"],
        workflow: WorkflowName = "gh-audit-repo",
        kind: Literal["issue", "pull"] | None = None,
        records: list[dict[str, Any]] | None = None,
        artifacts: list[str] | None = None,
        source: str | None = None,
        fetched_at: str | None = None,
        full_history_complete: bool | None = None,
    ) -> dict[str, Any]:
        """Manage history; ingest large results by Qwen persisted-output artifact path."""
        return _request_call(runtime.history_manage, HistoryManageRequest, **locals())

    @mcp.tool(annotations=READ_ONLY, structured_output=True)
    def history_query(
        workflow: WorkflowName = "gh-audit-repo",
        terms: str | None = None,
        kind: Literal["issue", "pull"] | None = None,
        state: Literal["open", "closed"] | None = None,
        cutoff: str | None = None,
        linked: list[dict[str, Any]] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Search a bounded, selector-based GitHub history view."""
        return _request_call(runtime.history_query, HistoryQueryRequest, **locals())

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=True)
    def audit_inventory(
        action: Literal[
            "initialize", "refresh", "status", "program", "record_declared", "record_context"
        ],
        name: str | None = None,
        arguments: list[str] | None = None,
        request_id: str | None = None,
        value: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Initialize, refresh, inspect, or update the audit environment inventory."""
        return _request_call(runtime.audit_inventory, InventoryRequest, **locals())

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=True)
    def audit_knowledge(
        action: Literal["status", "reconcile", "update", "context", "show"],
        area: str | None = None,
        areas: list[dict[str, Any]] | None = None,
        findings: list[dict[str, Any]] | None = None,
        versions: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Reconcile, read, or update durable per-area audit knowledge."""
        return _request_call(runtime.audit_knowledge, KnowledgeRequest, **locals())

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=True)
    def audit_probe(
        kind: Literal["pytest", "python"],
        probe_id: str,
        selectors: list[str] | None = None,
        code: str | None = None,
    ) -> dict[str, Any]:
        """Run one bounded probe with a read-only worktree and disabled network."""
        return _request_call(runtime.audit_probe, ProbeRequest, **locals())

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=True)
    def audit_record(
        action: Literal[
            "phase",
            "shard",
            "candidate",
            "validation",
            "verdict",
            "limitation",
            "pending",
            "head_drift",
            "metrics",
            "supervisor_start",
            "supervisor_finish",
        ],
        value: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record one typed audit fact or supervisor activity."""
        return _request_call(runtime.audit_record, AuditRecordRequest, **locals())

    @mcp.tool(annotations=CONTROL_WRITE, structured_output=True)
    def audit_publish(
        action: Literal["begin", "finish", "uncertain"],
        candidate_id: str,
        mutation: str,
        receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist publication intent and record its GitHub receipt."""
        return _request_call(runtime.audit_publish, PublishRequest, **locals())

    @mcp.tool(annotations=READ_ONLY, structured_output=True)
    def audit_metrics() -> dict[str, Any]:
        """Summarize task, timing, documentation, validation, and mutation telemetry."""
        return _public_call(runtime.audit_metrics)

    return mcp
