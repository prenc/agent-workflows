"""Typed public request models for the workflow MCP server."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
    model_validator,
)

WorkflowName = Literal["gh-audit-repo", "gh-curate-issues", "gh-implement-issue"]


class StrictRequest(BaseModel):
    """Reject misspelled or obsolete public API fields."""

    model_config = ConfigDict(extra="forbid")


class ExtensibleRecord(BaseModel):
    """Typed required fields with room for evidence owned by the workflow."""

    model_config = ConfigDict(extra="allow")


class ActionRequest:
    """Expose discriminated action models as one convenient request object."""

    def __init__(self, **data: Any) -> None:
        super().__init__(data)

    def __getattr__(self, name: str) -> Any:
        try:
            return super().__getattr__(name)
        except AttributeError:
            return getattr(self.root, name)


class RunManageRequest(StrictRequest):
    """Start or change one workflow run through explicit invocation fields."""

    action: Literal["start", "resume", "checkpoint", "directive", "pause", "abort", "finish"]
    workflow: WorkflowName
    repository: str | None = None
    n: int | None = None
    targets: list[str] = Field(default_factory=list)
    instructions: str | None = None
    refresh_history: bool = False
    regression_sweep: bool = False
    dry_run: bool = False
    separate: bool = False
    pending: list[str] = Field(default_factory=list)
    source_confirmed: bool = False
    note: str | None = None

    @field_validator("n", mode="before")
    @classmethod
    def positive_integer(cls, value: Any, info: Any) -> Any:
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            raise ValueError(f"{info.field_name} must be a positive integer")
        return value

    @field_validator("instructions")
    @classmethod
    def non_empty_instructions(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("instructions must not be blank")
        return value

    @model_validator(mode="after")
    def validate_action_fields(self) -> RunManageRequest:
        control = {"action", "workflow", "note"}
        supplied = self.model_fields_set - control
        if self.action == "start":
            if not self.repository:
                raise ValueError("start requires repository in OWNER/REPO form")
            allowed = {
                "repository",
                "n",
                "targets",
                "instructions",
                "refresh_history",
                "regression_sweep",
                "dry_run",
                "separate",
                "pending",
                "source_confirmed",
            }
        elif self.action == "resume":
            allowed = {"n"}
        elif self.action == "directive":
            if self.workflow != "gh-audit-repo":
                raise ValueError("directives are supported only by repository audits")
            allowed = {"n", "instructions"}
        else:
            allowed = set()
        unexpected = supplied - allowed
        if unexpected:
            raise ValueError(f"{self.action} does not accept fields: {sorted(unexpected)}")
        if self.action == "start":
            workflow_fields = {
                "gh-audit-repo": {
                    "instructions",
                    "refresh_history",
                    "regression_sweep",
                    "dry_run",
                },
                "gh-curate-issues": {
                    "targets",
                    "refresh_history",
                    "dry_run",
                    "pending",
                },
                "gh-implement-issue": {"targets", "separate", "pending"},
            }[self.workflow]
            common = {"repository", "n", "source_confirmed"}
            irrelevant = supplied - common - workflow_fields
            if irrelevant:
                raise ValueError(f"{self.workflow} does not accept fields: {sorted(irrelevant)}")
        return self

    def invocation(self) -> dict[str, Any]:
        """Return the canonical user inputs persisted with a new run."""
        common: dict[str, Any] = {"n": self.n or 3}
        if self.workflow == "gh-audit-repo":
            return {
                **common,
                "instructions": self.instructions,
                "refresh_history": self.refresh_history,
                "regression_sweep": self.regression_sweep,
                "dry_run": self.dry_run,
            }
        if self.workflow == "gh-curate-issues":
            return {
                **common,
                "targets": self.targets,
                "history_days": 365,
                "refresh_history": self.refresh_history,
                "dry_run": self.dry_run,
            }
        return {**common, "targets": self.targets, "separate": self.separate}

    def directive(self) -> dict[str, Any]:
        """Return only explicitly supplied audit directive values."""
        values: dict[str, Any] = {}
        if self.n is not None:
            values["concurrency"] = self.n
        for name in ("instructions",):
            if name in self.model_fields_set:
                values[name] = getattr(self, name)
        if self.note is not None:
            values["note"] = self.note
        return values


class TaskPlanRequest(StrictRequest):
    action: Literal["plan"]
    workflow: WorkflowName = "gh-audit-repo"
    task_id: str
    logical_id: str | None = None
    agent_id: str | None = None
    role: str | None = None
    unit: str | None = None
    assignment: dict[str, Any] = Field(default_factory=dict)
    required: bool = True


class TaskRetryRequest(StrictRequest):
    action: Literal["retry"]
    workflow: WorkflowName = "gh-audit-repo"
    task_id: str
    agent_id: str | None = None
    role: str | None = None
    unit: str | None = None
    assignment: dict[str, Any] = Field(default_factory=dict)
    required: bool = True


class TaskTransitionRequest(StrictRequest):
    action: Literal["dispatch", "checkpoint", "fail", "abandon"]
    workflow: WorkflowName = "gh-audit-repo"
    task_id: str
    note: str | None = None


class TaskReportRequest(StrictRequest):
    action: Literal["report"]
    workflow: WorkflowName = "gh-audit-repo"
    task_id: str
    report: dict[str, Any] = Field(
        description="Complete structured worker report to retain atomically."
    )
    note: str | None = None


class TaskIntegrationRequest(StrictRequest):
    action: Literal["integrate_start", "integrate_finish"]
    workflow: WorkflowName = "gh-audit-repo"
    task_id: str


TaskAction = Annotated[
    TaskPlanRequest
    | TaskRetryRequest
    | TaskTransitionRequest
    | TaskReportRequest
    | TaskIntegrationRequest,
    Field(discriminator="action"),
]


class TaskManageRequest(ActionRequest, RootModel[TaskAction]):
    """Register or transition one task through an action-specific schema."""


class HistoryRecord(ExtensibleRecord):
    """Compact issue or pull-request metadata returned by the GitHub MCP server."""

    number: int
    state: str = "unknown"
    title: str = ""
    labels: list[Any] = Field(default_factory=list)
    assignees: list[Any] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    closed_at: str | None = None
    merged_at: str | None = None
    url: str | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    head_sha: str | None = None


class HistoryPrepareRequest(StrictRequest):
    action: Literal["prepare"]
    workflow: WorkflowName = "gh-audit-repo"


class HistoryStatusRequest(StrictRequest):
    action: Literal["status"]
    workflow: WorkflowName = "gh-audit-repo"


class HistoryIngestRequest(StrictRequest):
    action: Literal["ingest"]
    workflow: WorkflowName = "gh-audit-repo"
    kind: Literal["issue", "pull"]
    records: list[HistoryRecord] = Field(default_factory=list, max_length=100)
    artifacts: list[str] = Field(default_factory=list, max_length=100)
    source: str = "github-mcp"
    fetched_at: str | None = None

    @model_validator(mode="after")
    def validate_ingest_source(self) -> HistoryIngestRequest:
        if bool(self.records) == bool(self.artifacts):
            raise ValueError("ingest requires exactly one of records or artifacts")
        return self


class HistoryCommitRequest(StrictRequest):
    action: Literal["commit"]
    workflow: WorkflowName = "gh-audit-repo"
    fetched_at: str | None = None
    full_history_complete: bool | None = None


class HistoryAbortRequest(StrictRequest):
    action: Literal["abort"]
    workflow: WorkflowName = "gh-audit-repo"


HistoryAction = Annotated[
    HistoryStatusRequest
    | HistoryPrepareRequest
    | HistoryIngestRequest
    | HistoryCommitRequest
    | HistoryAbortRequest,
    Field(discriminator="action"),
]


class HistoryManageRequest(ActionRequest, RootModel[HistoryAction]):
    """Inspect, prepare, ingest, commit, or abort a history transaction."""


class LinkedRecord(StrictRequest):
    kind: Literal["issue", "pull"]
    number: int


class HistoryQueryRequest(StrictRequest):
    """Search the committed GitHub history cache."""

    workflow: WorkflowName = "gh-audit-repo"
    terms: str = ""
    kind: Literal["issue", "pull"] | None = None
    state: Literal["open", "closed"] | None = None
    cutoff: str | None = None
    linked: list[LinkedRecord] = Field(default_factory=list)
    limit: int = Field(default=25, ge=1, le=100)


class InventorySimpleRequest(StrictRequest):
    action: Literal["initialize", "refresh", "status"]


class ProgramProbe(StrictRequest):
    name: str
    arguments: list[str] = Field(default_factory=list)
    request_id: str | None = None


class InventoryProgramRequest(StrictRequest):
    action: Literal["program"]
    programs: list[ProgramProbe] = Field(min_length=1)


class InventoryDeclaredRequest(StrictRequest):
    action: Literal["record_declared"]
    value: dict[str, Any] = Field(
        description="Declared versions, constraints, and configuration facts."
    )


class InventoryContextRequest(StrictRequest):
    action: Literal["record_context"]
    request_id: str
    value: dict[str, Any] = Field(description="Resolved documentation or capability fact.")


InventoryAction = Annotated[
    InventorySimpleRequest
    | InventoryProgramRequest
    | InventoryDeclaredRequest
    | InventoryContextRequest,
    Field(discriminator="action"),
]


class InventoryRequest(ActionRequest, RootModel[InventoryAction]):
    """Inspect or update inventory through an action-specific schema."""


def _normalize_boundaries(value: Any) -> Any:
    return [value] if isinstance(value, str) else value


BoundaryList = Annotated[
    list[str],
    BeforeValidator(_normalize_boundaries, json_schema_input_type=list[str] | str),
]


class AreaDefinition(ExtensibleRecord):
    area: str = Field(description="Canonical area/<slug> identifier.")
    title: str | None = None
    description: str
    paths: list[str]
    entrypoints: list[str] = Field(default_factory=list)
    boundaries: BoundaryList = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_identity(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        area = normalized.get("area")
        legacy_id = normalized.pop("id", None)
        normalized.pop("fingerprint", None)
        if area and legacy_id and area != legacy_id:
            raise ValueError("area and legacy id must match")
        inferred = area or legacy_id
        title = normalized.get("title")
        if inferred is None and isinstance(title, str) and title.startswith("area/"):
            inferred = title
            title = None
        if inferred is None:
            return normalized
        normalized["area"] = inferred
        if title == inferred:
            title = None
        if not title:
            title = inferred.removeprefix("area/").replace("-", " ").replace("_", " ").capitalize()
        normalized["title"] = title
        return normalized


class KnowledgeShowRequest(StrictRequest):
    action: Literal["show"]
    area: str | None = None


class KnowledgeStatusRequest(StrictRequest):
    action: Literal["status"]


class KnowledgeReconcileRequest(StrictRequest):
    action: Literal["reconcile"]
    areas: list[AreaDefinition]


class KnowledgeContextRequest(StrictRequest):
    action: Literal["context"]
    area: str
    versions: dict[str, str] = Field(default_factory=dict)


class KnowledgeUpdateRequest(StrictRequest):
    action: Literal["update"]
    area: str
    findings: list[dict[str, Any]]


KnowledgeAction = Annotated[
    KnowledgeShowRequest
    | KnowledgeStatusRequest
    | KnowledgeReconcileRequest
    | KnowledgeContextRequest
    | KnowledgeUpdateRequest,
    Field(discriminator="action"),
]


class KnowledgeRequest(ActionRequest, RootModel[KnowledgeAction]):
    """Read or update durable area knowledge through an explicit action schema."""


class PytestProbeRequest(StrictRequest):
    kind: Literal["pytest"]
    probe_id: str
    selectors: list[str] = Field(min_length=1)


class PythonProbeRequest(StrictRequest):
    kind: Literal["python"]
    probe_id: str
    code: str = Field(min_length=1)


ProbeAction = Annotated[PytestProbeRequest | PythonProbeRequest, Field(discriminator="kind")]


class ProbeRequest(ActionRequest, RootModel[ProbeAction]):
    """Run one bounded probe with kind-specific required inputs."""


class PhaseRecordValue(StrictRequest):
    phase: Literal["source", "history", "structure", "discovery", "verification", "publication"]
    value: dict[str, Any] = Field(description="Phase details including required lifecycle status.")


class IdentifiedAuditValue(ExtensibleRecord):
    id: str


class ShardRecordValue(IdentifiedAuditValue):
    area: str
    status: Literal["pending", "running", "partial", "complete", "skipped", "failed"]


class CandidateRecordValue(IdentifiedAuditValue):
    status: str


class ValidationRecordValue(IdentifiedAuditValue):
    status: str
    probe_id: str | None = None
    candidate_id: str | None = None
    artifact: str | None = None


class VerdictRecordValue(IdentifiedAuditValue):
    candidate_id: str | None = None


class AuditPhaseRequest(StrictRequest):
    action: Literal["phase"]
    value: PhaseRecordValue


class AuditShardRequest(StrictRequest):
    action: Literal["shard"]
    value: ShardRecordValue


class AuditCandidateRequest(StrictRequest):
    action: Literal["candidate"]
    value: CandidateRecordValue


class AuditValidationRequest(StrictRequest):
    action: Literal["validation"]
    value: ValidationRecordValue


class AuditVerdictRequest(StrictRequest):
    action: Literal["verdict"]
    value: VerdictRecordValue


class LimitationValue(StrictRequest):
    limitation: str = Field(min_length=1)


class AuditLimitationRequest(StrictRequest):
    action: Literal["limitation"]
    value: LimitationValue


class PendingValue(StrictRequest):
    pending: list[str]


class AuditPendingRequest(StrictRequest):
    action: Literal["pending"]
    value: PendingValue


class AuditObjectRequest(StrictRequest):
    action: Literal["head_drift", "metrics"]
    value: dict[str, Any]


class SupervisorActivityValue(StrictRequest):
    kind: str = Field(min_length=1)
    unit: str | None = None


class AuditSupervisorStartRequest(StrictRequest):
    action: Literal["supervisor_start"]
    value: SupervisorActivityValue


class AuditSupervisorFinishRequest(StrictRequest):
    action: Literal["supervisor_finish"]
    value: dict[str, Any] = Field(default_factory=dict, max_length=0)


AuditRecordAction = Annotated[
    AuditPhaseRequest
    | AuditShardRequest
    | AuditCandidateRequest
    | AuditValidationRequest
    | AuditVerdictRequest
    | AuditLimitationRequest
    | AuditPendingRequest
    | AuditObjectRequest
    | AuditSupervisorStartRequest
    | AuditSupervisorFinishRequest,
    Field(discriminator="action"),
]


class AuditRecordRequest(ActionRequest, RootModel[AuditRecordAction]):
    """Record one audit fact through an action-specific value schema."""


class PublishBeginRequest(StrictRequest):
    action: Literal["begin"]
    candidate_id: str
    mutation: str


class PublishFinishRequest(StrictRequest):
    action: Literal["finish"]
    candidate_id: str
    mutation: str
    receipt: dict[str, Any] = Field(min_length=1)


class PublishUncertainRequest(StrictRequest):
    action: Literal["uncertain"]
    candidate_id: str
    mutation: str
    receipt: dict[str, Any] = Field(default_factory=dict)


PublishAction = Annotated[
    PublishBeginRequest | PublishFinishRequest | PublishUncertainRequest,
    Field(discriminator="action"),
]


class PublishRequest(ActionRequest, RootModel[PublishAction]):
    """Bracket publication with action-specific receipt requirements."""
