"""MCP tool registration for the GitHub workflow runtime."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import ToolAnnotations
from pydantic import ValidationError

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
LOGGER = logging.getLogger(__name__)
SDK_PREFIX = re.compile(r"^Error executing tool [^:]+:\s*")


@dataclass(frozen=True)
class ValidationIssue:
    """One normalized public correction derived from internal validation."""

    field: str
    kind: str
    requirement: str


def _field_path(location: tuple[Any, ...], arguments: dict[str, Any]) -> str:
    parts = list(location)
    discriminator = arguments.get("action", arguments.get("kind"))
    if len(parts) > 1 and parts[0] == discriminator:
        parts.pop(0)
    rendered: list[str] = []
    for part in parts:
        if isinstance(part, int):
            if rendered:
                rendered[-1] += "[]"
            else:
                rendered.append("[]")
        else:
            rendered.append(str(part))
    return ".".join(rendered) or "request"


def _validation_issues(error: ValidationError, arguments: dict[str, Any]) -> list[ValidationIssue]:
    """Return distinct field requirements without rejected values or framework prose."""
    issues: list[ValidationIssue] = []
    seen: set[ValidationIssue] = set()
    for detail in error.errors(include_url=False, include_input=False):
        field = _field_path(detail["loc"], arguments)
        kind = detail["type"]
        context = detail.get("ctx") or {}
        if kind == "missing":
            requirement = "is required"
        elif kind == "extra_forbidden":
            requirement = "is not accepted"
        elif kind in {"list_type", "list_parsing"}:
            requirement = "must be a list"
        elif kind in {"dict_type", "mapping_type"}:
            requirement = "must be an object"
        elif kind == "string_type":
            requirement = "must be a string"
        elif kind in {"int_type", "int_parsing"}:
            requirement = "must be an integer"
        elif kind == "bool_type":
            requirement = "must be a boolean"
        elif kind == "literal_error":
            requirement = f"must be one of: {context.get('expected', 'the supported values')}"
        elif kind == "union_tag_invalid":
            discriminator = str(context.get("discriminator", "action")).strip("'\"")
            field = discriminator
            requirement = f"must be one of: {context.get('expected_tags', 'the supported values')}"
        elif kind == "union_tag_not_found":
            field = str(context.get("discriminator", "action")).strip("'\"")
            requirement = "is required"
        elif kind == "greater_than_equal":
            boundary = context.get("ge")
            requirement = f"must be greater than or equal to {boundary}"
        elif kind == "greater_than":
            requirement = f"must be greater than {context.get('gt')}"
        elif kind == "less_than_equal":
            boundary = context.get("le")
            requirement = f"must be less than or equal to {boundary}"
        elif kind == "less_than":
            requirement = f"must be less than {context.get('lt')}"
        elif kind == "too_short":
            requirement = f"must contain at least {context.get('min_length', 1)} item(s)"
        elif kind == "too_long":
            requirement = f"must contain at most {context.get('max_length')} item(s)"
        elif kind == "string_too_short":
            requirement = f"must contain at least {context.get('min_length', 1)} characters"
        elif kind == "string_too_long":
            requirement = f"must contain at most {context.get('max_length')} characters"
        elif kind in {"value_error", "assertion_error"}:
            requirement = detail["msg"].removeprefix("Value error, ")
            if field == "request":
                field = ""
        else:
            requirement = "is invalid"
        issue = ValidationIssue(field=field, kind=kind, requirement=requirement)
        if issue not in seen:
            seen.add(issue)
            issues.append(issue)
    return issues


def _render_validation_error(error: ValidationError, arguments: dict[str, Any]) -> str:
    messages = [
        f"{issue.field} {issue.requirement}".strip()
        for issue in _validation_issues(error, arguments)
    ]
    return "; ".join(messages) or "request is invalid"


def _validation_cause(error: BaseException) -> ValidationError | None:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ValidationError):
            return current
        current = current.__cause__
    return None


def _expected_message(error: ToolError) -> str:
    current: BaseException = error
    seen: set[int] = set()
    while (
        isinstance(current.__cause__, ToolError)
        and not isinstance(current.__cause__, UnexpectedToolError)
        and id(current) not in seen
    ):
        seen.add(id(current))
        current = current.__cause__
    message = " ".join(str(current).split())
    while SDK_PREFIX.match(message):
        message = SDK_PREFIX.sub("", message, count=1)
    return message or "tool request failed"


class WorkflowMCPServer(MCPServer[Any]):
    """Keep SDK and model diagnostics out of agent-facing tool results."""

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Any | None = None
    ) -> Any:
        try:
            return await super().call_tool(name, arguments, context)
        except UnexpectedToolError as error:
            raise UnexpectedToolError("Internal tool failure; inspect server logs") from error
        except ToolError as error:
            validation = _validation_cause(error)
            if validation is not None:
                LOGGER.debug(
                    "Tool %s validation details: %r",
                    name,
                    validation.errors(include_url=False, include_input=False),
                )
                raise ToolError(_render_validation_error(validation, arguments)) from validation
            raise ToolError(_expected_message(error)) from error


def _public_call(operation: Any, *arguments: Any) -> Any:
    """Return actionable domain failures without leaking implementation errors."""
    try:
        return operation(*arguments)
    except ValidationError as error:
        raise ToolError("invalid request") from error
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
    mcp = WorkflowMCPServer("github-workflows")

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
        programs: list[dict[str, Any]] | None = None,
        request_id: str | None = None,
        value: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Manage audit inventory; pass program probes together in `programs`."""
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
