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
NON_BLANK_PATTERN = r"\S"
FULL_SHA_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
REPOSITORY_PATTERN = r"^[^/\s]+/[^/\s]+$"

type NonBlankString = Annotated[str, Field(pattern=NON_BLANK_PATTERN)]
type PositiveInteger = Annotated[int, Field(strict=True, ge=1)]
type HistoryLimit = Annotated[int, Field(strict=True, ge=1, le=100)]
type FullSha = Annotated[str, Field(pattern=FULL_SHA_PATTERN)]
type RepositoryName = Annotated[str, Field(pattern=REPOSITORY_PATTERN)]
type FeedbackMessage = Annotated[
    str,
    Field(min_length=1, max_length=2000, pattern=NON_BLANK_PATTERN),
]
type FeedbackTaskRef = Annotated[
    str,
    Field(min_length=1, max_length=400, pattern=NON_BLANK_PATTERN),
]
type FeedbackToolName = Annotated[
    str,
    Field(min_length=1, max_length=200, pattern=NON_BLANK_PATTERN),
]

RUN_ACTION_FIELDS: dict[str, frozenset[str]] = {
    "start": frozenset(
        {
            "repository",
            "n",
            "targets",
            "instructions",
            "refresh_history",
            "regression_sweep",
            "dry_run",
            "separate",
            "confirmed_source_sha",
            "acknowledge_pending_publication",
            "note",
        }
    ),
    "resume": frozenset({"n", "note"}),
    "checkpoint": frozenset({"pending", "note"}),
    "directive": frozenset({"n", "instructions", "note"}),
    "pause": frozenset({"note"}),
    "abort": frozenset({"note"}),
    "finish": frozenset({"outcome", "note"}),
}

RUN_START_WORKFLOW_FIELDS: dict[WorkflowName, frozenset[str]] = {
    "gh-audit-repo": frozenset(
        {
            "instructions",
            "refresh_history",
            "regression_sweep",
            "dry_run",
            "confirmed_source_sha",
            "acknowledge_pending_publication",
        }
    ),
    "gh-curate-issues": frozenset({"targets", "refresh_history", "dry_run"}),
    "gh-implement-issue": frozenset({"targets", "separate"}),
}


class StrictRequest(BaseModel):
    """Reject misspelled or obsolete public API fields."""

    model_config = ConfigDict(extra="forbid")


class ExtensibleRecord(BaseModel):
    """Typed required fields with room for evidence owned by the workflow."""

    model_config = ConfigDict(extra="allow")


class WorkflowFeedbackRequest(StrictRequest):
    """One concise workflow or instruction observation with optional tool context."""

    message: FeedbackMessage = Field(
        description=(
            "Observed friction and consequence. For instruction friction, name the known "
            "layer or project-relative section without copying the complete instruction context."
        ),
    )
    task_ref: FeedbackTaskRef | None = Field(
        default=None,
        description="Exact worker task reference returned by task_context, when available.",
    )
    tool: FeedbackToolName | None = Field(
        default=None,
        description=(
            "Native or external tool name for a confusing interaction; never put PHI, PII, "
            "prompts, or payload values in the feedback message."
        ),
    )

    @field_validator("message", "tool", mode="before")
    @classmethod
    def non_blank_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("text must not be blank")
            return value.strip()
        return value


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
    repository: RepositoryName | None = None
    n: PositiveInteger | None = None
    targets: list[str] = Field(default_factory=list)
    instructions: NonBlankString | None = None
    refresh_history: bool = False
    regression_sweep: bool = False
    dry_run: bool = False
    separate: bool = False
    pending: list[str] = Field(default_factory=list)
    confirmed_source_sha: FullSha | None = Field(
        default=None,
        description=(
            "Exact full local HEAD approved during audit preflight. The runtime rejects "
            "a changed source before creating run state or a worktree."
        ),
    )
    acknowledge_pending_publication: bool = False
    outcome: Literal["complete", "blocked"] | None = None
    note: str | None = None

    @field_validator("n", mode="before")
    @classmethod
    def positive_integer(cls, value: Any, info: Any) -> Any:
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            raise ValueError(f"{info.field_name} must be a positive integer")
        return value

    @field_validator("targets")
    @classmethod
    def non_blank_targets(cls, value: list[str]) -> list[str]:
        if any(not target.strip() for target in value):
            raise ValueError("targets must contain only non-blank references")
        return value

    @model_validator(mode="after")
    def validate_action_fields(self) -> RunManageRequest:
        supplied = self.model_fields_set - {"action", "workflow"}
        if self.outcome is not None:
            if self.action != "finish":
                raise ValueError("outcome is accepted only when finishing a workflow")
            if self.workflow == "gh-audit-repo":
                raise ValueError("audit workflows do not accept a finish outcome")
            if self.outcome == "blocked" and (self.note is None or not self.note.strip()):
                raise ValueError("blocked finish requires a non-blank note")
        if self.action == "start":
            if not self.repository:
                raise ValueError("start requires repository in OWNER/REPO form")
        elif self.action == "checkpoint":
            if self.workflow == "gh-audit-repo" and "pending" in supplied:
                raise ValueError("audit pending state must use audit_record")
        elif self.action == "directive":
            if self.workflow != "gh-audit-repo":
                raise ValueError("directives are supported only by repository audits")
        allowed = RUN_ACTION_FIELDS[self.action]
        unexpected = supplied - allowed
        if unexpected:
            fields = ", ".join(sorted(unexpected))
            raise ValueError(f"action={self.action} does not accept {fields}")
        if self.action == "start":
            common = {"repository", "n", "note"}
            irrelevant = supplied - common - RUN_START_WORKFLOW_FIELDS[self.workflow]
            if irrelevant:
                fields = ", ".join(sorted(irrelevant))
                raise ValueError(f"workflow={self.workflow} does not accept {fields}")
            if self.workflow == "gh-implement-issue" and not self.targets:
                raise ValueError("gh-implement-issue start requires at least one target")
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


class TaskPlan(StrictRequest):
    """One logical worker assignment; attempt identity is server-owned."""

    logical_id: NonBlankString
    role: str | None = None
    unit: str | None = None
    assignment: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Workflow-specific assignment object; extension fields are allowed. Curator "
            "assignments require issue, issue_snapshot, and candidate_bundle. Implementation "
            "assignments require issues whose snapshot and accepted_scope are non-empty "
            'compact strings. They require pull_request={"state":"none"} for new work, '
            'or an extensible pull_request object with state="open" for existing work, plus '
            "worktree, branch, rebased_base_sha, "
            "remote_lease, round_objective, acceptance_condition, repository_instructions, "
            "validation_plan, and execution_environment. Audit assignments use canonical "
            "history_links objects with kind issue/pull and a positive number; history_issues "
            "and history_pulls remain compatibility aliases. Audit verify assignments may "
            "include validation_ids. New plans and replacement retries are validated before "
            "an attempt is created."
        ),
    )
    required: bool = True


class TaskPlanRequest(StrictRequest):
    action: Literal["plan"]
    workflow: WorkflowName = "gh-audit-repo"
    task: TaskPlan


class TaskRetryRequest(StrictRequest):
    action: Literal["retry"]
    workflow: WorkflowName = "gh-audit-repo"
    task_id: NonBlankString
    task: TaskPlan | None = None
    note: str | None = None


class TaskTransitionRequest(StrictRequest):
    action: Literal["mark_running", "fail", "abandon"]
    workflow: WorkflowName = "gh-audit-repo"
    task_id: NonBlankString
    note: str | None = None


class TaskCheckpointRequest(StrictRequest):
    action: Literal["checkpoint"]
    workflow: WorkflowName = "gh-audit-repo"
    task_id: NonBlankString
    report: dict[str, Any] = Field(description="Compact continuation report to retain atomically.")
    note: str | None = None


class TaskReportRequest(StrictRequest):
    action: Literal["complete"]
    workflow: WorkflowName = "gh-audit-repo"
    task_id: NonBlankString
    report: dict[str, Any] = Field(
        description="Complete structured worker report to retain atomically."
    )
    note: str | None = None


class TaskIntegrationRequest(StrictRequest):
    action: Literal["integration_begin", "integration_end"]
    workflow: WorkflowName = "gh-audit-repo"
    task_id: NonBlankString
    note: str | None = None


TaskAction = Annotated[
    TaskPlanRequest
    | TaskRetryRequest
    | TaskTransitionRequest
    | TaskCheckpointRequest
    | TaskReportRequest
    | TaskIntegrationRequest,
    Field(discriminator="action"),
]


class TaskManageRequest(ActionRequest, RootModel[TaskAction]):
    """Register or transition one task through an action-specific schema."""


class HistoryRecord(ExtensibleRecord):
    """Compact issue or pull-request metadata returned by the GitHub MCP server."""

    kind: Literal["issue", "pull"]
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


class HistoryArtifact(StrictRequest):
    kind: Literal["issue", "pull"]
    path: NonBlankString


class HistoryPrepareRequest(StrictRequest):
    action: Literal["prepare"]
    workflow: WorkflowName = "gh-audit-repo"


class HistoryStatusRequest(StrictRequest):
    action: Literal["status"]
    workflow: WorkflowName = "gh-audit-repo"


class HistoryIngestRequest(StrictRequest):
    action: Literal["ingest"]
    workflow: WorkflowName = "gh-audit-repo"
    records: list[HistoryRecord] = Field(default_factory=list, max_length=100)
    artifacts: list[HistoryArtifact] = Field(default_factory=list, max_length=100)
    source: NonBlankString = "github-mcp"
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
    default_sha: FullSha | None = Field(
        default=None,
        description="Full immutable default-branch SHA represented by this committed history.",
    )


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
    terms: NonBlankString = ""
    kind: Literal["issue", "pull"] | None = None
    state: Literal["open", "closed"] | None = None
    cutoff: NonBlankString | None = None
    linked: list[LinkedRecord] = Field(default_factory=list)
    limit: HistoryLimit = 25


class InventorySimpleRequest(StrictRequest):
    action: Literal["initialize", "refresh", "status"]


class ProgramProbe(StrictRequest):
    name: NonBlankString
    arguments: list[str] = Field(default_factory=list)
    request_id: NonBlankString | None = None


class InventoryProgramRequest(StrictRequest):
    action: Literal["program"]
    programs: list[ProgramProbe] = Field(min_length=1)


class InventoryDeclaredRequest(StrictRequest):
    action: Literal["record_declared"]
    facts: dict[str, Any] = Field(
        description="Declared versions, constraints, and configuration facts."
    )


class InventoryContextFact(ExtensibleRecord):
    kind: NonBlankString
    name: NonBlankString
    detail: NonBlankString
    observed: str | None = None
    disposition: Literal["confirmed", "disproved", "unavailable"] | None = None


class InventoryContextRequest(StrictRequest):
    action: Literal["record_context"]
    request_id: NonBlankString | None = None
    fact: InventoryContextFact


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


class AreaDefinition(StrictRequest):
    area: NonBlankString = Field(description="Canonical area/<slug> identifier.")
    title: str | None = None
    description: str
    paths: list[str]
    entrypoints: list[str] = Field(default_factory=list)
    boundaries: BoundaryList = Field(default_factory=list)

    @model_validator(mode="after")
    def derive_title(self) -> AreaDefinition:
        if not self.title:
            self.title = (
                self.area.removeprefix("area/").replace("-", " ").replace("_", " ").capitalize()
            )
        return self


class KnowledgeShowRequest(StrictRequest):
    action: Literal["show"]
    area: NonBlankString | None = None


class KnowledgeReconcileRequest(StrictRequest):
    action: Literal["reconcile"]
    areas: list[AreaDefinition]


class KnowledgeFinding(StrictRequest):
    title: NonBlankString
    question: NonBlankString
    kind: NonBlankString
    method: NonBlankString
    observed_result: NonBlankString
    conclusion: NonBlankString
    disposition: Literal["confirmed", "disproved"]
    evidence_paths: list[str] = Field(default_factory=list)
    dependencies: dict[str, str] = Field(default_factory=dict)


class KnowledgeContextRequest(StrictRequest):
    action: Literal["context"]
    area: NonBlankString
    versions: dict[str, str] = Field(default_factory=dict)


class KnowledgeUpdateRequest(StrictRequest):
    action: Literal["update"]
    area: NonBlankString
    findings: list[KnowledgeFinding]


KnowledgeAction = Annotated[
    KnowledgeShowRequest
    | KnowledgeReconcileRequest
    | KnowledgeContextRequest
    | KnowledgeUpdateRequest,
    Field(discriminator="action"),
]


class KnowledgeRequest(ActionRequest, RootModel[KnowledgeAction]):
    """Read or update durable area knowledge through an explicit action schema."""


class PytestProbeRequest(StrictRequest):
    kind: Literal["pytest"]
    probe_id: NonBlankString
    candidate_id: NonBlankString
    selectors: list[str] = Field(min_length=1)


class PythonProbeRequest(StrictRequest):
    kind: Literal["python"]
    probe_id: NonBlankString
    candidate_id: NonBlankString
    code: NonBlankString


ProbeAction = Annotated[PytestProbeRequest | PythonProbeRequest, Field(discriminator="kind")]


class ProbeRequest(ActionRequest, RootModel[ProbeAction]):
    """Run one bounded probe with kind-specific required inputs."""


PhaseName = Literal["source", "history", "structure", "discovery", "verification", "publication"]
PhaseStatus = Literal["pending", "in-progress", "complete", "skipped", "partial", "failed"]
ShardStatus = Literal["pending", "running", "partial", "complete", "skipped", "failed"]
CandidateStatus = Literal[
    "discovered",
    "consolidated",
    "validation-pending",
    "verification-pending",
    "verified",
    "published",
    "updated",
    "no-op",
    "closed",
    "protected",
    "duplicate",
    "rejected",
    "dry-run",
]


class PhaseRecord(StrictRequest):
    name: PhaseName
    status: PhaseStatus | None = None
    summary: dict[str, Any] = Field(default_factory=dict)


class IdentifiedAuditValue(ExtensibleRecord):
    id: str


class ShardRecordValue(IdentifiedAuditValue):
    area: str | None = None
    status: ShardStatus | None = None
    paths: list[str] = Field(default_factory=list)


class CandidateRecordValue(IdentifiedAuditValue):
    status: CandidateStatus | None = None


class VerdictRecordValue(ExtensibleRecord):
    candidate_id: NonBlankString

    @model_validator(mode="before")
    @classmethod
    def reject_redundant_id(cls, value: Any) -> Any:
        if isinstance(value, dict) and "id" in value:
            raise ValueError("verdict uses candidate_id as its sole identity; id is not accepted")
        return value


class AuditPhaseRequest(StrictRequest):
    action: Literal["phase"]
    phase: PhaseRecord


class AuditShardRequest(StrictRequest):
    action: Literal["shard"]
    shard: ShardRecordValue


class AuditCandidateRequest(StrictRequest):
    action: Literal["candidate"]
    candidate: CandidateRecordValue


class AuditVerdictRequest(StrictRequest):
    action: Literal["verdict"]
    verdict: VerdictRecordValue


class AuditLimitationRequest(StrictRequest):
    action: Literal["limitation"]
    limitation: NonBlankString


class AuditPendingRequest(StrictRequest):
    action: Literal["pending"]
    pending: list[str]


class AuditObjectRequest(StrictRequest):
    action: Literal["head_drift"]
    head_drift: dict[str, Any]


class SupervisorActivityValue(StrictRequest):
    """One exclusive material activity performed by the audit supervisor."""

    kind: NonBlankString = Field(
        description="Stable short name for the supervisor activity being started.",
    )
    unit: str | None = Field(
        default=None,
        description="Optional audit unit owned by this supervisor activity.",
    )


class AuditSupervisorStartRequest(StrictRequest):
    action: Literal["supervisor_start"]
    activity: SupervisorActivityValue


class AuditSupervisorFinishRequest(StrictRequest):
    action: Literal["supervisor_finish"]


AuditRecordAction = Annotated[
    AuditPhaseRequest
    | AuditShardRequest
    | AuditCandidateRequest
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
    candidate_id: NonBlankString
    operation: Literal["create", "update", "no-op", "close", "dry-run"]


class PublishFinishRequest(StrictRequest):
    action: Literal["finish"]
    candidate_id: NonBlankString
    receipt: dict[str, Any] = Field(min_length=1)


class PublishUncertainRequest(StrictRequest):
    action: Literal["uncertain"]
    candidate_id: NonBlankString
    receipt: dict[str, Any] = Field(default_factory=dict)


class PublishFailedRequest(StrictRequest):
    action: Literal["failed"]
    candidate_id: NonBlankString
    error: NonBlankString


PublishAction = Annotated[
    PublishBeginRequest | PublishFinishRequest | PublishUncertainRequest | PublishFailedRequest,
    Field(discriminator="action"),
]


class PublishRequest(ActionRequest, RootModel[PublishAction]):
    """Bracket publication with action-specific receipt requirements."""
