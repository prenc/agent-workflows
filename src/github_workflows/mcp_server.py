"""MCP tool registration for the GitHub workflow runtime."""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from . import feedback
from .models import (
    AreaDefinition,
    AuditRecordRequest,
    CandidateRecordValue,
    HistoryArtifact,
    HistoryManageRequest,
    HistoryQueryRequest,
    HistoryRecord,
    InventoryContextFact,
    InventoryRequest,
    KnowledgeFinding,
    KnowledgeRequest,
    PhaseRecord,
    ProbeRequest,
    ProgramProbe,
    PublishRequest,
    RunManageRequest,
    ShardRecordValue,
    SupervisorActivityValue,
    TaskManageRequest,
    TaskPlan,
    VerdictRecordValue,
    WorkflowFeedbackRequest,
    WorkflowName,
)
from .runtime import WorkflowRuntime

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
APPEND_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
CONTROL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)
LOGGER = logging.getLogger(__name__)
SDK_PREFIX = re.compile(r"^Error executing tool [^:]+:\s*")

type JsonObjectArgument[T] = Annotated[
    T,
    Field(description="Send a JSON object value; do not JSON-encode it as a string."),
]
type JsonArrayArgument[T] = Annotated[
    T,
    Field(description="Send a JSON array value; do not JSON-encode it as a string."),
]

ACTION_REQUIREMENTS: dict[str, tuple[str, dict[str, tuple[str, ...]]]] = {
    "run_manage": ("action", {"start": ("repository",)}),
    "task_manage": (
        "action",
        {
            "plan": ("task",),
            "retry": ("task_id",),
            "mark_running": ("task_id",),
            "checkpoint": ("task_id", "report"),
            "complete": ("task_id", "report"),
            "fail": ("task_id",),
            "abandon": ("task_id",),
            "integration_begin": ("task_id",),
            "integration_end": ("task_id",),
        },
    ),
    "audit_inventory": (
        "action",
        {
            "program": ("programs",),
            "record_declared": ("facts",),
            "record_context": ("fact",),
        },
    ),
    "audit_knowledge": (
        "action",
        {
            "reconcile": ("areas",),
            "update": ("area", "findings"),
            "context": ("area",),
        },
    ),
    "audit_probe": ("kind", {"pytest": ("selectors",), "python": ("code",)}),
    "audit_record": (
        "action",
        {
            "phase": ("phase",),
            "shard": ("shard",),
            "candidate": ("candidate",),
            "verdict": ("verdict",),
            "limitation": ("limitation",),
            "pending": ("pending",),
            "head_drift": ("head_drift",),
            "supervisor_start": ("activity",),
        },
    ),
    "audit_publish": (
        "action",
        {"begin": ("operation",), "finish": ("receipt",)},
    ),
}


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


def _request_provenance(context: Context[Any, Any] | None) -> dict[str, Any]:
    """Return metadata the MCP server can derive without agent input."""
    try:
        server_version = version("agent-workflows")
    except PackageNotFoundError:  # pragma: no cover - editable installs provide metadata
        server_version = "unknown"
    result: dict[str, Any] = {"server_version": server_version}
    if context is None:
        return result
    request_context = context.request_context
    result["protocol_version"] = request_context.protocol_version
    params = request_context.session.client_params
    if params is not None:
        result["client"] = {
            "name": params.client_info.name,
            "version": params.client_info.version,
        }
    return result


def _action_requirement(
    discriminator: str, action: str, required: tuple[str, ...]
) -> dict[str, Any]:
    return {
        "if": {
            "properties": {discriminator: {"const": action}},
            "required": [discriminator],
        },
        # Qwen's draft-07 validator renders a dependency as one grouped error,
        # rather than reporting only the first missing member of ``required``.
        "then": {"dependencies": {discriminator: list(required)}},
    }


def _direct_structured_schema(
    schema: dict[str, Any], definitions: dict[str, Any]
) -> dict[str, Any] | None:
    candidates = [schema]
    selected: dict[str, Any] | None = None
    kind: Literal["object", "array"] | None = None
    description = schema.get("description")
    visited_references: set[str] = set()
    while candidates:
        candidate = candidates.pop(0)
        if description is None:
            description = candidate.get("description")
        nested = candidate.get("anyOf")
        if isinstance(nested, list):
            candidates.extend(item for item in nested if isinstance(item, dict))
            continue
        candidate_kind = candidate.get("type")
        if candidate_kind in {"object", "array"}:
            selected = deepcopy(candidate)
            kind = candidate_kind
            break
        reference = candidate.get("$ref")
        if (
            isinstance(reference, str)
            and reference.startswith("#/$defs/")
            and reference not in visited_references
        ):
            visited_references.add(reference)
            resolved = definitions.get(reference.removeprefix("#/$defs/"))
            if isinstance(resolved, dict):
                candidates.append(resolved)
    if selected is None or kind is None:
        return None
    requirement = f"Send a JSON {kind} value; do not JSON-encode it as a string."
    selected["description"] = (
        description
        if isinstance(description, str) and requirement in description
        else f"{description} {requirement}"
        if description
        else requirement
    )
    return selected


def _public_input_schema(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Add compact client-side checks for statically knowable request mistakes."""
    result = deepcopy(schema)
    result["additionalProperties"] = False
    properties = result.get("properties", {})
    definitions = result.get("$defs", {})
    for field, property_schema in list(properties.items()):
        direct = _direct_structured_schema(property_schema, definitions)
        if direct is not None:
            properties[field] = direct
    if name == "run_manage":
        properties["targets"]["description"] = (
            "Requested issue or pull-request references; required and non-empty when starting "
            "gh-implement-issue. Send a JSON array value; do not JSON-encode it as a string."
        )
        properties["targets"].setdefault("items", {})["pattern"] = r"\S"
        properties["pending"]["description"] = (
            "External mutations awaiting read-back, rollback, or reconciliation; accepted only "
            "by generic workflow checkpoints. Send a JSON array value; do not JSON-encode it "
            "as a string."
        )
    conditions = result.setdefault("allOf", [])
    contract = ACTION_REQUIREMENTS.get(name)
    if contract is not None:
        discriminator, requirements = contract
        conditions.extend(
            _action_requirement(discriminator, action, required)
            for action, required in requirements.items()
        )
    if name == "history_manage":
        conditions.append(
            {
                "if": {
                    "properties": {"action": {"const": "ingest"}},
                    "required": ["action"],
                },
                "then": {
                    "oneOf": [
                        {
                            "required": ["records"],
                            "properties": {"records": {"type": "array", "minItems": 1}},
                            "not": {"required": ["artifacts"]},
                        },
                        {
                            "required": ["artifacts"],
                            "properties": {"artifacts": {"type": "array", "minItems": 1}},
                            "not": {"required": ["records"]},
                        },
                    ]
                },
            }
        )
    if name == "history_query":
        conditions.append(
            {
                "anyOf": [
                    {
                        "required": ["terms"],
                        "properties": {"terms": {"type": "string", "minLength": 1}},
                    },
                    {"required": ["kind"]},
                    {"required": ["state"]},
                    {
                        "required": ["cutoff"],
                        "properties": {"cutoff": {"type": "string", "minLength": 1}},
                    },
                    {
                        "required": ["linked"],
                        "properties": {"linked": {"type": "array", "minItems": 1}},
                    },
                ]
            }
        )
    if name == "run_manage":
        conditions.append(
            {
                "if": {
                    "allOf": [
                        {
                            "properties": {"action": {"const": "start"}},
                            "required": ["action"],
                        },
                        {
                            "properties": {"workflow": {"const": "gh-implement-issue"}},
                            "required": ["workflow"],
                        },
                    ]
                },
                "then": {
                    "required": ["targets"],
                    "properties": {"targets": {"type": "array", "minItems": 1}},
                },
            }
        )
    if not conditions:
        result.pop("allOf", None)
    return result


class WorkflowMCPServer(MCPServer[Any]):
    """Keep SDK and model diagnostics out of agent-facing tool results."""

    def __init__(self, name: str, *, failures: feedback.FailureRegistry) -> None:
        super().__init__(name)
        self.failures = failures

    def _failure_message(
        self,
        name: str,
        arguments: dict[str, Any],
        message: str,
        context: Context[Any, Any] | None,
        *,
        failure_kind: str,
    ) -> str:
        if name == "workflow_feedback":
            return message
        try:
            reference = self.failures.record(
                tool=name,
                arguments=arguments,
                failure_kind=failure_kind,
                provenance=_request_provenance(context),
            )
        except Exception:  # pragma: no cover - feedback must never obscure the original failure
            LOGGER.exception("Unable to retain MCP failure context")
            return message
        return (
            f'{message} Consider workflow_feedback(message="what was confusing", '
            f'error_ref="{reference}") if this is confusing or repeated API friction.'
        )

    async def list_tools(self) -> list[Any]:
        tools = await super().list_tools()
        return [
            tool.model_copy(
                update={"input_schema": _public_input_schema(tool.name, tool.input_schema)}
            )
            for tool in tools
        ]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context[Any, Any] | None = None,
    ) -> Any:
        try:
            tool = self._tool_manager.get_tool(name)
            if tool is not None:
                properties = tool.parameters.get("properties", {})
                unknown = sorted(set(arguments) - set(properties))
                if unknown:
                    if len(unknown) == 1:
                        raise ToolError(f"{unknown[0]} is not accepted")
                    raise ToolError(f"fields are not accepted: {', '.join(unknown)}")
            return await super().call_tool(name, arguments, context)
        except UnexpectedToolError as error:
            message = self._failure_message(
                name,
                arguments,
                "Internal tool failure; inspect server logs.",
                context,
                failure_kind="internal",
            )
            raise UnexpectedToolError(message) from error
        except ToolError as error:
            validation = _validation_cause(error)
            if validation is not None:
                LOGGER.debug(
                    "Tool %s validation details: %r",
                    name,
                    validation.errors(include_url=False, include_input=False),
                )
                message = _render_validation_error(validation, arguments)
            else:
                message = _expected_message(error)
            raise ToolError(
                self._failure_message(
                    name,
                    arguments,
                    message,
                    context,
                    failure_kind="validation" if validation is not None else "domain",
                )
            ) from error


def _public_call(operation: Any, *arguments: Any) -> Any:
    """Return actionable domain failures without leaking implementation errors."""
    try:
        return operation(*arguments)
    except ValidationError as error:
        raise ToolError("invalid request") from error
    except (ValueError, RuntimeError) as error:
        raise ToolError(str(error)) from error


def _request_call(handler: Any, request_model: type[Any], /, **values: Any) -> Any:
    """Validate a flat public call through the internal action model."""
    payload = {
        name: value for name, value in values.items() if value is not None and name != "runtime"
    }
    return _public_call(lambda: handler(request_model.model_validate(payload)))


def create_server(runtime: WorkflowRuntime) -> MCPServer:
    """Create a server whose tools operate on one validated workspace."""
    mcp = WorkflowMCPServer(
        "github-workflows",
        failures=feedback.FailureRegistry(runtime.feedback_private_paths()),
    )

    @mcp.tool(annotations=APPEND_WRITE, structured_output=True)
    def workflow_feedback(
        message: str,
        task_ref: str | None = None,
        error_ref: str | None = None,
        tool: str | None = None,
        context: Context[Any, Any] | None = None,
    ) -> dict[str, Any]:
        """Record PHI-free friction; send a message, task_ref, error_ref, or tool name."""
        values = {
            "message": message,
            "task_ref": task_ref,
            "error_ref": error_ref,
            "tool": tool,
        }

        def record() -> dict[str, Any]:
            request = WorkflowFeedbackRequest.model_validate(
                {name: value for name, value in values.items() if value is not None}
            )
            failure_context = mcp.failures.resolve(error_ref) if error_ref is not None else None
            return runtime.workflow_feedback(
                request,
                provenance=_request_provenance(context),
                failure_context=failure_context,
            )

        return _public_call(record)

    @mcp.tool(annotations=CONTROL_WRITE, structured_output=True)
    def run_manage(
        action: Literal["start", "resume", "checkpoint", "directive", "pause", "abort", "finish"],
        workflow: WorkflowName,
        repository: str | None = None,
        n: int | None = None,
        targets: JsonArrayArgument[list[str] | None] = None,
        instructions: str | None = None,
        refresh_history: bool | None = None,
        regression_sweep: bool | None = None,
        dry_run: bool | None = None,
        separate: bool | None = None,
        pending: JsonArrayArgument[list[str] | None] = None,
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
            "mark_running",
            "checkpoint",
            "complete",
            "fail",
            "abandon",
            "retry",
            "integration_begin",
            "integration_end",
        ],
        workflow: WorkflowName = "gh-audit-repo",
        task_id: str | None = None,
        task: JsonObjectArgument[TaskPlan | None] = None,
        report: JsonObjectArgument[dict[str, Any] | None] = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Plan a task or transition it; checkpoint and complete accept structured reports."""
        return _request_call(runtime.task_manage, TaskManageRequest, **locals())

    @mcp.tool(annotations=READ_ONLY, structured_output=True)
    def task_context(task_ref: str) -> dict[str, Any]:
        """Resolve the exact short task_ref returned by task_manage."""
        return _public_call(runtime.task_context, task_ref)

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=True)
    def history_manage(
        action: Literal["status", "prepare", "ingest", "commit", "abort"],
        workflow: WorkflowName = "gh-audit-repo",
        records: JsonArrayArgument[list[HistoryRecord] | None] = None,
        artifacts: JsonArrayArgument[list[HistoryArtifact] | None] = None,
        source: str | None = None,
        fetched_at: str | None = None,
        full_history_complete: bool | None = None,
    ) -> dict[str, Any]:
        """Manage a compact GitHub index; details are discarded and read live when needed."""
        return _request_call(runtime.history_manage, HistoryManageRequest, **locals())

    @mcp.tool(annotations=READ_ONLY, structured_output=True)
    def history_query(
        workflow: WorkflowName = "gh-audit-repo",
        terms: str | None = None,
        kind: Literal["issue", "pull"] | None = None,
        state: Literal["open", "closed"] | None = None,
        cutoff: str | None = None,
        linked: JsonArrayArgument[list[dict[str, Any]] | None] = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Search a bounded, selector-based GitHub history view."""
        return _request_call(runtime.history_query, HistoryQueryRequest, **locals())

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=True)
    def audit_inventory(
        action: Literal[
            "initialize", "refresh", "status", "program", "record_declared", "record_context"
        ],
        programs: JsonArrayArgument[list[ProgramProbe] | None] = None,
        request_id: str | None = None,
        facts: JsonObjectArgument[dict[str, Any] | None] = None,
        fact: JsonObjectArgument[InventoryContextFact | None] = None,
    ) -> dict[str, Any]:
        """Manage audit inventory; pass program probes together in `programs`."""
        return _request_call(runtime.audit_inventory, InventoryRequest, **locals())

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=True)
    def audit_knowledge(
        action: Literal["reconcile", "update", "context", "show"],
        area: str | None = None,
        areas: JsonArrayArgument[list[AreaDefinition] | None] = None,
        findings: JsonArrayArgument[list[KnowledgeFinding] | None] = None,
        versions: JsonObjectArgument[dict[str, str] | None] = None,
    ) -> dict[str, Any]:
        """Manage area knowledge with server-derived identities and fingerprints."""
        return _request_call(runtime.audit_knowledge, KnowledgeRequest, **locals())

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=True)
    def audit_probe(
        kind: Literal["pytest", "python"],
        probe_id: str,
        candidate_id: str,
        selectors: JsonArrayArgument[list[str] | None] = None,
        code: str | None = None,
    ) -> dict[str, Any]:
        """Run and record one candidate probe, returning bounded output directly."""
        return _request_call(runtime.audit_probe, ProbeRequest, **locals())

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=True)
    def audit_record(
        action: Literal[
            "phase",
            "shard",
            "candidate",
            "verdict",
            "limitation",
            "pending",
            "head_drift",
            "supervisor_start",
            "supervisor_finish",
        ],
        phase: JsonObjectArgument[PhaseRecord | None] = None,
        shard: JsonObjectArgument[ShardRecordValue | None] = None,
        candidate: JsonObjectArgument[CandidateRecordValue | None] = None,
        verdict: JsonObjectArgument[VerdictRecordValue | None] = None,
        limitation: str | None = None,
        pending: JsonArrayArgument[list[str] | None] = None,
        head_drift: JsonObjectArgument[dict[str, Any] | None] = None,
        activity: JsonObjectArgument[SupervisorActivityValue | None] = None,
    ) -> dict[str, Any]:
        """Record one typed audit fact or supervisor activity."""
        return _request_call(runtime.audit_record, AuditRecordRequest, **locals())

    @mcp.tool(annotations=CONTROL_WRITE, structured_output=True)
    def audit_publish(
        action: Literal["begin", "finish", "uncertain"],
        candidate_id: str,
        operation: Literal["create", "update", "no-op", "close", "dry-run"] | None = None,
        receipt: JsonObjectArgument[dict[str, Any] | None] = None,
    ) -> dict[str, Any]:
        """Begin publication or finish it atomically with its receipt and disposition."""
        return _request_call(runtime.audit_publish, PublishRequest, **locals())

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=True)
    def audit_metrics() -> dict[str, Any]:
        """Summarize and persist task, timing, validation, and mutation telemetry."""
        return _public_call(runtime.audit_metrics)

    return mcp
