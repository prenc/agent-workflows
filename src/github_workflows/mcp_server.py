"""MCP tool registration for the GitHub workflow runtime."""

from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from difflib import get_close_matches
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from .models import (
    RUN_ACTION_FIELDS,
    RUN_START_WORKFLOW_FIELDS,
    AreaDefinition,
    AuditRecordRequest,
    CandidateRecordValue,
    FeedbackMessage,
    FeedbackTaskRef,
    FeedbackToolName,
    FullSha,
    HistoryArtifact,
    HistoryLimit,
    HistoryManageRequest,
    HistoryQueryRequest,
    HistoryRecord,
    InventoryContextFact,
    InventoryRequest,
    KnowledgeFinding,
    KnowledgeRequest,
    LinkedRecord,
    NonBlankString,
    PhaseRecord,
    PositiveInteger,
    ProbeRequest,
    ProgramProbe,
    PublishRequest,
    RepositoryName,
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

ACTION_REQUEST_MODELS: dict[str, type[Any]] = {
    "task_manage": TaskManageRequest,
    "history_manage": HistoryManageRequest,
    "audit_inventory": InventoryRequest,
    "audit_knowledge": KnowledgeRequest,
    "audit_probe": ProbeRequest,
    "audit_record": AuditRecordRequest,
    "audit_publish": PublishRequest,
}
SIMPLE_REQUEST_MODELS: dict[str, type[Any]] = {
    "workflow_feedback": WorkflowFeedbackRequest,
    "run_manage": RunManageRequest,
    "history_query": HistoryQueryRequest,
}
SCHEMA_CONSTRAINT_KEYS = (
    "exclusiveMaximum",
    "exclusiveMinimum",
    "maxItems",
    "maxLength",
    "maxProperties",
    "maximum",
    "minItems",
    "minLength",
    "minProperties",
    "minimum",
    "pattern",
)


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
        elif kind == "string_pattern_mismatch":
            pattern = context.get("pattern")
            if pattern == r"\S":
                requirement = "must not be blank"
            elif pattern == r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$":
                requirement = "must be a full 40- or 64-character lowercase hexadecimal SHA"
            elif pattern == r"^[^/\s]+/[^/\s]+$":
                requirement = "must use OWNER/REPO form"
            elif pattern == r"^err-[0-9a-f]{12}$":
                requirement = "must use err- followed by 12 lowercase hexadecimal characters"
            else:
                requirement = "has an invalid format"
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
    issues = _validation_issues(error, arguments)
    discriminator = "action" if "action" in arguments else "kind" if "kind" in arguments else None
    discriminator_value = arguments.get(discriminator) if discriminator is not None else None
    missing = [issue.field for issue in issues if issue.kind == "missing"]
    rejected = [issue.field for issue in issues if issue.kind == "extra_forbidden"]
    messages: list[str] = []
    if missing:
        fields = ", ".join(missing)
        messages.append(
            f"{discriminator}={discriminator_value} requires {fields}"
            if discriminator_value is not None
            else f"required: {fields}"
        )
    if rejected:
        fields = ", ".join(rejected)
        messages.append(
            f"{discriminator}={discriminator_value} does not accept {fields}"
            if discriminator_value is not None
            else f"not accepted: {fields}"
        )
    messages.extend(
        f"{issue.field} {issue.requirement}".strip()
        for issue in issues
        if issue.kind not in {"missing", "extra_forbidden"}
    )
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
    meta = request_context.meta
    raw = meta.get("qwen-code/invocation") if isinstance(meta, dict) else None
    if isinstance(raw, dict) and raw.get("version") == 1:
        session_id = raw.get("sessionId")
        prompt_id = raw.get("promptId")
        if all(isinstance(value, str) and value.strip() for value in (session_id, prompt_id)):
            conversation = {
                "client": "qwen",
                "session_id": session_id,
                "prompt_id": prompt_id,
            }
            if request_context.request_id is not None:
                conversation["mcp_request_id"] = str(request_context.request_id)
            result["conversation"] = conversation
    return result


def _request_invocation_id(context: Context[Any, Any] | None) -> str | None:
    """Return an opaque identity for the Qwen user-prompt invocation."""
    if context is None:
        return None
    meta = context.request_context.meta
    raw = meta.get("qwen-code/invocation") if isinstance(meta, dict) else None
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return None
    session_id = raw.get("sessionId")
    prompt_id = raw.get("promptId")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    if not isinstance(prompt_id, str) or not prompt_id.strip():
        return None
    material = f"{session_id}\0{prompt_id}".encode()
    return hashlib.sha256(material).hexdigest()


def _action_requirement(
    discriminator: str,
    action: str,
    required: list[str],
    constraints: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    then: dict[str, Any] = {}
    if required:
        # Qwen's draft-07 validator renders a dependency as one grouped error,
        # rather than reporting only the first missing member of ``required``.
        then["dependencies"] = {discriminator: required}
    if constraints:
        then["properties"] = constraints
    return {
        "if": {
            "properties": {discriminator: {"const": action}},
            "required": [discriminator],
        },
        "then": then,
    }


def _non_null_schema(schema: dict[str, Any]) -> dict[str, Any]:
    variants = schema.get("anyOf")
    if not isinstance(variants, list):
        return deepcopy(schema)
    non_null = [
        variant
        for variant in variants
        if isinstance(variant, dict) and variant.get("type") != "null"
    ]
    if len(non_null) == 1 and len(non_null) != len(variants):
        return deepcopy(non_null[0])
    return deepcopy(schema)


def _direct_argument_schema(schema: dict[str, Any], definitions: dict[str, Any]) -> dict[str, Any]:
    selected = _non_null_schema(schema)
    description = schema.get("description")
    visited: set[str] = set()
    reference = selected.get("$ref")
    while (
        isinstance(reference, str) and reference.startswith("#/$defs/") and reference not in visited
    ):
        visited.add(reference)
        resolved = definitions.get(reference.removeprefix("#/$defs/"))
        if not isinstance(resolved, dict):
            break
        if description is None:
            description = resolved.get("description")
        selected = _non_null_schema(resolved)
        reference = selected.get("$ref")
    kind = selected.get("type")
    if kind in {"object", "array"}:
        requirement = f"Send a JSON {kind} value; do not JSON-encode it as a string."
        selected["description"] = (
            description
            if isinstance(description, str) and requirement in description
            else f"{description} {requirement}"
            if description
            else requirement
        )
    elif description is not None:
        selected["description"] = description
    if schema.get("default") is not None:
        selected["default"] = schema["default"]
    return selected


def _schema_constraints(schema: dict[str, Any]) -> dict[str, Any]:
    direct = _non_null_schema(schema)
    return {key: direct[key] for key in SCHEMA_CONSTRAINT_KEYS if key in direct}


def _merge_model_constraints(properties: dict[str, Any], request_model: type[Any]) -> None:
    model_properties = request_model.model_json_schema().get("properties", {})
    for field in properties.keys() & model_properties.keys():
        properties[field].update(_schema_constraints(model_properties[field]))


def _action_variants(request_model: type[Any]) -> tuple[str, dict[str, dict[str, Any]]]:
    schema = request_model.model_json_schema()
    discriminator = schema["discriminator"]["propertyName"]
    definitions = schema.get("$defs", {})
    variants: dict[str, dict[str, Any]] = {}
    for action, reference in schema["discriminator"]["mapping"].items():
        variants[action] = definitions[reference.removeprefix("#/$defs/")]
    return discriminator, variants


def _add_field_dependencies(
    result: dict[str, Any],
    discriminator: str,
    action_fields: dict[str, set[str] | frozenset[str]],
) -> None:
    all_actions = set(action_fields)
    field_actions: defaultdict[str, set[str]] = defaultdict(set)
    for action, fields in action_fields.items():
        for field in fields:
            field_actions[field].add(action)
    dependencies = result.setdefault("dependencies", {})
    for field, actions in sorted(field_actions.items()):
        if field not in result.get("properties", {}) or actions == all_actions:
            continue
        allowed = sorted(actions)
        discriminator_schema = {"const": allowed[0]} if len(allowed) == 1 else {"enum": allowed}
        dependencies[field] = {
            "properties": {discriminator: discriminator_schema},
            "required": [discriminator],
        }


def _add_action_model_contract(result: dict[str, Any], request_model: type[Any]) -> None:
    discriminator, variants = _action_variants(request_model)
    global_required = set(result.get("required", []))
    action_fields: dict[str, set[str]] = {}
    for action, variant in variants.items():
        variant_properties = variant.get("properties", {})
        action_fields[action] = set(variant_properties) - {discriminator}
        required = sorted(set(variant.get("required", [])) - global_required - {discriminator})
        constraints = {
            field: selected
            for field, property_schema in variant_properties.items()
            if field in result.get("properties", {})
            and (selected := _schema_constraints(property_schema))
        }
        if required or constraints:
            result.setdefault("allOf", []).append(
                _action_requirement(discriminator, action, required, constraints)
            )
    _add_field_dependencies(result, discriminator, action_fields)


def _field_value_dependency(
    result: dict[str, Any], field: str, discriminator: str, values: set[str]
) -> None:
    allowed = sorted(values)
    value_schema = {"const": allowed[0]} if len(allowed) == 1 else {"enum": allowed}
    dependency = result.setdefault("dependencies", {}).setdefault(
        field, {"properties": {}, "required": []}
    )
    dependency.setdefault("properties", {})[discriminator] = value_schema
    required = dependency.setdefault("required", [])
    if discriminator not in required:
        required.append(discriminator)


def _prune_definitions(schema: dict[str, Any]) -> None:
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        return

    def references(value: Any) -> set[str]:
        if isinstance(value, dict):
            found = {
                reference.removeprefix("#/$defs/").split("/", 1)[0]
                for reference in [value.get("$ref")]
                if isinstance(reference, str) and reference.startswith("#/$defs/")
            }
            for key, nested in value.items():
                if key != "$defs":
                    found.update(references(nested))
            return found
        if isinstance(value, list):
            found: set[str] = set()
            for nested in value:
                found.update(references(nested))
            return found
        return set()

    retained = references(schema)
    pending = list(retained)
    while pending:
        name = pending.pop()
        for dependency in references(definitions.get(name)) - retained:
            retained.add(dependency)
            pending.append(dependency)
    if retained:
        schema["$defs"] = {
            name: definition for name, definition in definitions.items() if name in retained
        }
    else:
        schema.pop("$defs", None)


def _public_input_schema(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Add compact client-side checks for statically knowable request mistakes."""
    result = deepcopy(schema)
    result["additionalProperties"] = False
    properties = result.get("properties", {})
    definitions = result.get("$defs", {})
    for field, property_schema in list(properties.items()):
        properties[field] = _direct_argument_schema(property_schema, definitions)
    request_model = SIMPLE_REQUEST_MODELS.get(name)
    if request_model is not None:
        _merge_model_constraints(properties, request_model)
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
    action_model = ACTION_REQUEST_MODELS.get(name)
    if action_model is not None:
        _add_action_model_contract(result, action_model)
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
        _add_field_dependencies(result, "action", RUN_ACTION_FIELDS)
        field_workflows: defaultdict[str, set[str]] = defaultdict(set)
        for workflow, fields in RUN_START_WORKFLOW_FIELDS.items():
            for field in sorted(fields):
                field_workflows[field].add(workflow)
        field_workflows["pending"].update({"gh-curate-issues", "gh-implement-issue"})
        field_workflows["outcome"].update({"gh-curate-issues", "gh-implement-issue"})
        for field, workflows in sorted(field_workflows.items()):
            _field_value_dependency(result, field, "workflow", workflows)
        conditions.append(_action_requirement("action", "start", ["repository"], {}))
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
        conditions.extend(
            (
                {
                    "if": {
                        "properties": {"action": {"const": "directive"}},
                        "required": ["action"],
                    },
                    "then": {
                        "properties": {"workflow": {"const": "gh-audit-repo"}},
                        "required": ["workflow"],
                    },
                },
                {
                    "if": {
                        "properties": {"outcome": {"const": "blocked"}},
                        "required": ["outcome"],
                    },
                    "then": {
                        "required": ["note"],
                        "properties": {
                            "note": {"type": "string", "minLength": 1, "pattern": r"\S"}
                        },
                    },
                },
            )
        )
    if not conditions:
        result.pop("allOf", None)
    _prune_definitions(result)
    return result


class WorkflowMCPServer(MCPServer[Any]):
    """Keep SDK and model diagnostics out of agent-facing tool results."""

    def _failure_message(
        self,
        name: str,
        message: str,
    ) -> str:
        if name == "workflow_feedback":
            return message
        return f'{message} If unclear, call workflow_feedback(message="what was confusing").'

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
                        field = unknown[0]
                        suggestion = get_close_matches(field, properties, n=1, cutoff=0.75)
                        hint = f"; did you mean {suggestion[0]}?" if suggestion else ""
                        raise ToolError(f"{field} is not accepted{hint}")
                    raise ToolError(f"not accepted: {', '.join(unknown)}")
            return await super().call_tool(name, arguments, context)
        except UnexpectedToolError as error:
            message = self._failure_message(
                name,
                "Internal tool failure; inspect server logs.",
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
                    message,
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
        name: value
        for name, value in values.items()
        if value is not None and name not in {"runtime", "context", "invocation_id"}
    }
    return _public_call(lambda: handler(request_model.model_validate(payload)))


def create_server(runtime: WorkflowRuntime) -> MCPServer:
    """Create a server whose tools operate on one validated workspace."""
    mcp = WorkflowMCPServer("github-workflows")

    @mcp.tool(annotations=APPEND_WRITE, structured_output=True)
    def workflow_feedback(
        message: FeedbackMessage,
        task_ref: FeedbackTaskRef | None = None,
        tool: FeedbackToolName | None = None,
        context: Context[Any, Any] | None = None,
    ) -> dict[str, Any]:
        """Record PHI-free friction; send a message and optional task_ref or tool name."""
        values = {
            "message": message,
            "task_ref": task_ref,
            "tool": tool,
        }

        def record() -> dict[str, Any]:
            request = WorkflowFeedbackRequest.model_validate(
                {name: value for name, value in values.items() if value is not None}
            )
            return runtime.workflow_feedback(
                request,
                provenance=_request_provenance(context),
            )

        return _public_call(record)

    @mcp.tool(annotations=CONTROL_WRITE, structured_output=True)
    def run_manage(
        action: Literal["start", "resume", "checkpoint", "directive", "pause", "abort", "finish"],
        workflow: WorkflowName,
        repository: RepositoryName | None = None,
        n: PositiveInteger | None = None,
        targets: JsonArrayArgument[list[str] | None] = None,
        instructions: NonBlankString | None = None,
        refresh_history: bool | None = None,
        regression_sweep: bool | None = None,
        dry_run: bool | None = None,
        separate: bool | None = None,
        pending: JsonArrayArgument[list[str] | None] = None,
        confirmed_source_sha: FullSha | None = None,
        outcome: Literal["complete", "blocked"] | None = None,
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
        task_id: NonBlankString | None = None,
        task: JsonObjectArgument[TaskPlan | None] = None,
        report: JsonObjectArgument[dict[str, Any] | None] = None,
        note: str | None = None,
        context: Context[Any, Any] | None = None,
    ) -> dict[str, Any]:
        """Plan or transition a task; repeated matching lifecycle calls are idempotent."""
        invocation_id = _request_invocation_id(context)
        return _request_call(
            lambda request: runtime.task_manage(request, invocation_id=invocation_id),
            TaskManageRequest,
            **locals(),
        )

    @mcp.tool(annotations=READ_ONLY, structured_output=True)
    def task_context(
        task_ref: NonBlankString, history_cursor: NonBlankString | None = None
    ) -> dict[str, Any]:
        """Resolve an exact returned task ref and optionally continue with an exact cursor."""
        return _public_call(runtime.task_context, task_ref, history_cursor)

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=True)
    def history_manage(
        action: Literal["status", "prepare", "ingest", "commit", "abort"],
        workflow: WorkflowName = "gh-audit-repo",
        records: JsonArrayArgument[list[HistoryRecord] | None] = None,
        artifacts: JsonArrayArgument[list[HistoryArtifact] | None] = None,
        source: NonBlankString | None = None,
        fetched_at: str | None = None,
        full_history_complete: bool | None = None,
        default_sha: FullSha | None = None,
    ) -> dict[str, Any]:
        """Manage a resumable compact GitHub index and report typed staging recovery state."""
        return _request_call(runtime.history_manage, HistoryManageRequest, **locals())

    @mcp.tool(annotations=READ_ONLY, structured_output=True)
    def history_query(
        workflow: WorkflowName = "gh-audit-repo",
        terms: NonBlankString | None = None,
        kind: Literal["issue", "pull"] | None = None,
        state: Literal["open", "closed"] | None = None,
        cutoff: NonBlankString | None = None,
        linked: JsonArrayArgument[list[LinkedRecord] | None] = None,
        limit: HistoryLimit | None = None,
    ) -> dict[str, Any]:
        """Search a bounded, selector-based GitHub history view."""
        return _request_call(runtime.history_query, HistoryQueryRequest, **locals())

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=True)
    def audit_inventory(
        action: Literal[
            "initialize", "refresh", "status", "program", "record_declared", "record_context"
        ],
        programs: JsonArrayArgument[list[ProgramProbe] | None] = None,
        request_id: NonBlankString | None = None,
        facts: JsonObjectArgument[dict[str, Any] | None] = None,
        fact: JsonObjectArgument[InventoryContextFact | None] = None,
    ) -> dict[str, Any]:
        """Manage audit inventory; pass program probes together in `programs`."""
        return _request_call(runtime.audit_inventory, InventoryRequest, **locals())

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=True)
    def audit_knowledge(
        action: Literal["reconcile", "update", "context", "show"],
        area: NonBlankString | None = None,
        areas: JsonArrayArgument[list[AreaDefinition] | None] = None,
        findings: JsonArrayArgument[list[KnowledgeFinding] | None] = None,
        versions: JsonObjectArgument[dict[str, str] | None] = None,
    ) -> dict[str, Any]:
        """Manage area knowledge with server-derived identities and fingerprints."""
        return _request_call(runtime.audit_knowledge, KnowledgeRequest, **locals())

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=True)
    def audit_probe(
        kind: Literal["pytest", "python"],
        probe_id: NonBlankString,
        candidate_id: NonBlankString,
        selectors: JsonArrayArgument[list[str] | None] = None,
        code: NonBlankString | None = None,
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
        limitation: NonBlankString | None = None,
        pending: JsonArrayArgument[list[str] | None] = None,
        head_drift: JsonObjectArgument[dict[str, Any] | None] = None,
        activity: JsonObjectArgument[SupervisorActivityValue | None] = None,
    ) -> dict[str, Any]:
        """Record one typed audit fact or supervisor activity."""
        return _request_call(runtime.audit_record, AuditRecordRequest, **locals())

    @mcp.tool(annotations=CONTROL_WRITE, structured_output=True)
    def audit_publish(
        action: Literal["begin", "finish", "uncertain", "failed"],
        candidate_id: NonBlankString,
        operation: Literal["create", "update", "no-op", "close", "dry-run"] | None = None,
        receipt: JsonObjectArgument[dict[str, Any] | None] = None,
        error: NonBlankString | None = None,
    ) -> dict[str, Any]:
        """Begin publication or record its typed finish, uncertain, or failed outcome."""
        return _request_call(runtime.audit_publish, PublishRequest, **locals())

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=True)
    def audit_metrics() -> dict[str, Any]:
        """Summarize and persist task, timing, validation, and mutation telemetry."""
        return _public_call(runtime.audit_metrics)

    return mcp
