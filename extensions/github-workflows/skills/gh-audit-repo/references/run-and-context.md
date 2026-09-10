## 1. Establish the snapshot and run state

During preflight, use read-only Git inspection to capture the primary
worktree's branch and full committed `HEAD`. `main` and `master` need no user
confirmation. For another branch or detached `HEAD`, ask once whether that
exact source is intended and collect any scope guidance in the same question.
After an answer, refresh branch and `HEAD`; if either changed, remain in
preflight and confirm the new source rather than carrying the old answer
forward.

Call `mcp__github_workflows__run_manage` with action `start`, workflow
`gh-audit-repo`, `repository` as `OWNER/REPO`, and parsed invocation fields
as top-level tool arguments. For example, `-n 4 --dry-run prioritize the CLI`
becomes `n: 4`, `dry_run: true`, and `instructions: "prioritize the CLI"`.
Never add an `inputs`
or `request` wrapper, never stringify tool arguments as JSON, and do not use
`concurrency`; the typed field is `n`. It resolves the
primary worktree, current branch, exact local `HEAD`, upstream divergence,
excluded dirty-state counts, and immutable detached audit worktree.
The audit source is the committed local `HEAD`; tracked modifications and
untracked files in the primary worktree are outside the snapshot. Record their
presence without opening excluded files. If the primary worktree is on a
branch other than `main` or `master`, or is detached, pass the
preflight-approved full SHA as `confirmed_source_sha`. The runtime compares it
with a fresh local `HEAD` before creating run state or the detached worktree. A
mismatch returns to preflight; never treat it as approval of the new source.
The server adds `.worktrees/` to the repository-private `.git/info/exclude`
or linked worktree's common Git `info/exclude` when needed, verifies the local
rule, and uses `<project>/.worktrees`. On
`--resume`, call the same tool with action `resume` and pass `n`
only when the user supplied it; the tool applies that change atomically.
The immutable audit worktree may link the primary project `.venv`, but neither
the supervisor nor workers resolve `uv.lock`, synchronize, or install there.
Only the reviewed read-only inventory and probe helpers may execute against
that environment.

The server owns the run record and initializes canonical phases, shards, tasks,
candidates, validations, verdicts, mutations, limitations, pending work,
scheduler state, and metrics.
Register and transition tasks through `mcp__github_workflows__task_manage`.
Record phases, shards, candidates, verdicts, limitations, pending work, drift,
and supervisor activity through `mcp__github_workflows__audit_record`. Probe
validation and metrics are persisted by their respective tools. Call
`mcp__github_workflows__run_status` before launching work and after every task
result; its scheduler is authoritative. The server owns revisions, artifacts,
atomic writes, and lifecycle validation.
Each verdict uses its required `candidate_id` as its sole identity. Do not send
a separate verdict `id`; the runtime stores at most one current verdict per
candidate while preserving its evidence fields.
Before finalization, follow `run_status.finish_blockers` and its structured
allowed actions. Call `finish` only when `finish_ready` is true; do not memorize
or reconstruct finish-gate invariants.

The supervisor records every completed, failed, or abandoned attempt before
interpreting its result, starts the corresponding integration event, validates
and synthesizes available output, runs any material probe, and completes
integration before launching more work.
The normal worker sequence is `plan`, launch, immediately `mark_running` after
the launch is accepted and before waiting, record the returned result, then
`integration_begin`, integrate, and `integration_end`. If launch is rejected,
record `abandon` while the task is still queued. These lifecycle calls are safe
to repeat with the same task and report after an uncertain response.
`mcp__github_workflows__run_manage` with action `finish` enforces that tasks and candidates
are terminal, completed reports are integrated, validation files exactly match
registered artifacts, pending work is empty, publication
history is committed, and HEAD drift is reconciled.

On `--resume`, load repository, branch, SHA, confirmation, instructions, and
dry-run state from `mcp__github_workflows__run_status`. Reconcile every recorded issue
mutation against live GitHub, reuse safe same-run work, and continue only
pending work. Never repeat an uncertain mutation;
stop and report it for manual reconciliation. Refresh through
`mcp__github_workflows__audit_inventory`;
when its declared or Python environment changed, invalidate affected same-run
conclusions and validation while preserving environment-independent progress.
Reconcile every recorded task attempt with `list_agents`. Resume a retained
task once. Before treating a timeout or delayed notification as failure, consume
any final response already delivered by the worker and record its structured
report. Only when no usable result exists should you mark that attempt failed or
abandoned and create a new numbered attempt from its checkpoint. Never recover
a worker report from an arbitrary temporary path, and never leave an old task
recorded as running after replacement.

User messages preempt the scheduler even when all material lanes are occupied.
For a status question, reply immediately without changing lane accounting. For
pause or suspension, stop launching, cancel the fallback wakeup, ask active
workers for a compact checkpoint, and set the run to `suspended`. Record late
checkpoint, completion, failure, or abandonment reports from workers that were
already active; every operation that could start or integrate work remains
blocked until resume. For a directive change, record it before more
material work; changes to concurrency apply after current tasks are reconciled,
and scope/focus changes invalidate only affected unintegrated shards. Control
messages never wait for a worker slot.

## 2. Synchronize complete repository history

Use `mcp__github_workflows__history_manage` to inspect status or prepare,
ingest, commit, and abort synchronization; use
`mcp__github_workflows__history_query` only for bounded record views. Action
`status`, and the response from action `prepare`, provide generation, record
count, completeness, last successful synchronization, and audited default SHA.
Do not query records merely to infer cache metadata. Storage paths and
transaction files are private server details.

Prepare a staging copy with action `prepare`; repeating it resumes a valid
staging transaction without discarding ingested records, and automatically
recreates an empty interrupted transaction. If status reports an invalid
transaction, call `abort` and then `prepare`. On first use or automatic
recovery, enumerate every open and closed issue and every open,
closed-unmerged, and merged pull request. Mark
`full_history_complete` only after every page succeeds.

A normal run requires the completeness marker, refreshes all open records plus
records changed since five minutes before the successful watermark, and
reads every plausible match in full from GitHub. `--refresh-history` re-fetches
complete history.
Every open issue and pull request returned by the live refresh must be ingested,
even when its timestamps appear unchanged; do not compare or normalize bulk
responses locally to decide whether ingestion can be skipped. The server owns
normalization and refresh timestamps.

When a GitHub MCP response reports `<persisted-output>`, pass every reported
tool-result path as a typed `{kind, path}` entry in the `artifacts` list of one or more
`history_manage` ingest calls. Never read, copy, split, summarize, or
re-transcribe those files for ingestion; the history server validates and
reduces them to compact metadata without returning their contents. Use inline
`records` only when the GitHub response itself remained inline. Each record
carries its own `kind`, so an ingest call may contain both issues and pulls, with at most
100 records per call. For artifacts, this limit applies to the combined issue
and pull record count after every supplied file is expanded, not to the number
of artifact paths. `records` and `artifacts` are mutually exclusive.

Commit with action `commit` after all pages are ingested, passing the full
immutable default-branch SHA from the live repository read as `default_sha`.
The server supplies the transaction generation and run timestamp, takes a short lock, and rejects a
changed live generation. On conflict, prepare from the newer database, repeat
incremental synchronization once, and retry. A second conflict blocks
publication. Use the returned staging classification and recovery action rather
than inferring transaction state from a prior interruption.

Using paginated MCP list tools, maintain a compact inventory containing number,
URL, title, labels, state, assignees, timestamps, and relevant pull-request refs.
When a pull request is relevant to a finding or existing issue, inspect its
labels and report drift from the shared covered-issue taxonomy without mutating
the PR.
Bulk list calls must omit bodies, comments, commits, relationships, and other detail
collections. The history tool derives a rough summary from title and labels and
discards detail fields even when a provider returns them. Use `history_query` for
exact identifiers and indexed duplicate candidates, and include plausible records
in worker assignments. Use targeted semantic GitHub search only to discover
conceptual, paraphrased, or possible body-only matches; a zero result is
inconclusive. Then call `issue_read` or `pull_request_read` for every plausible
match and obtain relevant comments, commits/SHAs, native relationships, and
resolution evidence live. Do not ingest those detail payloads into history. Use
the full-timeline `gh api` fallback only when MCP relationships are incomplete
or contradictory. It requires the `gh` CLI to be installed and on PATH; when
it is unavailable, record the unresolved capability gap as a limitation.

Build an area-aware GitHub history view from compact summaries, then use live full reads
to establish root cause, paths/symbols, failure mode, requested outcome, required outcomes,
state, labels, and delivered/rejected/superseded status. Cached summaries select
candidates and never support publication or mutation conclusions. Closed issues and all
PRs are read-only evidence. For every matching
open issue, classify it as:

- `update-existing` when the candidate describes the same root cause/outcome
  and the issue has neither `in-progress` nor `partial`;
- `protected-existing` when it matches but carries either lock label;
- `duplicate-existing` when it overlaps without a coherent same-issue update;
- `new` only when no existing issue covers the finding.

An update must preserve the issue's accepted intent while correcting stale
evidence, scope, required outcomes, title, or taxonomy. Maintain one canonical
issue for `update-existing`, `protected-existing`, or `duplicate-existing`. Record
unrelated stale/fixed/duplicate observations for closure reconciliation or the
final report.

The server supplies each audit worker with a bounded compact history view through
`task_context`. Its cache metadata includes `last_sync_at`, the successful cache
watermark; it is a freshness hint rather than proof that an individual record
still matches live GitHub. Use canonical
`history_links: [{"kind": "issue"|"pull", "number": <positive integer>}]`
in assignments so that view contains relevant open records and only the resolved
records selected by the targeted gate. Without
`--regression-sweep`, select a resolved record only when its affected paths
changed after resolution, a current lead matches its root cause, or it is needed
for duplicate or closure reasoning. Do not assign every historical fix as a
mandatory regression gate. With `--regression-sweep`, include all resolved
records relevant to the areas selected under the user's instructions and
report the additional count.

Issue/PR history supplies scope and duplicate evidence, not proof that current
code has the reported behavior. Trace every candidate into the immutable audit
worktree and verify its reachable implementation path before it can survive.

## 3. Reconcile per-area knowledge

After inferring the exclusive area map, call
`mcp__github_workflows__audit_knowledge` with actions `show` and `reconcile`.
The tool is the sole interface to durable area knowledge; its storage is
private.

Pass one canonical `area/<slug>` value, description, and owned paths for each area;
title, entrypoints, and boundaries are optional. The server derives a missing title
for a new area, retains the stored title when an existing area's title is omitted,
and derives the complete identity fingerprint. Supply a different title explicitly
only to rename the area. Never calculate or submit fingerprints.
Ordinary source changes preserve the document; a boundary change,
rename, split, merge, addition, or removal archives invalidated knowledge and
bootstraps overlapping new areas with explicitly marked leads. The tool is
the only writer of these files.

Every selected area still receives fresh current-SHA discovery and every
surviving candidate receives independent verification. Code findings are leads
requiring current-source proof. Documentation and capability conclusions may
be reused only when every recorded version dependency exactly matches the
current inventory; otherwise recheck them. Persist only successfully obtained,
conclusive findings with `confirmed` or `disproved` disposition. Store the
question, applicable versions, method and evidence source, observed result,
conclusion, source SHA, and timestamp. Failures, unavailable tools, timeouts,
and inconclusive checks remain in the current run and final limitations.

## 4. Infer areas and prepare shared context

Infer exclusive areas from entrypoints and repository structure. Tests and
configuration inherit the behavior they serve; cross-entrypoint runtime code
belongs to `area/shared-core`. Apply a named project mapping from the shared
convention when available. If ownership cannot be exclusive, stop before
publication.

Separate publication areas from audit shards. Split an area when one worker
would receive unrelated subsystems, more than roughly 40 files, or more than
roughly 12,000 source lines. Each shard owns one cohesive module or entrypoint
slice and maps back to exactly one canonical `area/*` label. Shards are
exclusive for discovery; candidates are consolidated at the publication-area
level. A large `area/shared-core` is never itself sufficient reason to give one
worker datasets, metrics, packaging, and unrelated runtime utilities together.

Perform a lightweight technology survey from manifests, pinned versions,
imports, and configuration. Call `mcp__github_workflows__audit_inventory`
with action `initialize` to
create a revisioned inventory record through the tool. It records installed
distribution versions from the repository's `.venv` in one bulk snapshot without
importing packages (falling back to the selected system interpreter only when no project
`.venv` exists), repository manifest
identities, and whether the selected interpreter is `project-venv` or `system`.
Reuse that snapshot for Python-library versions; do not issue one inventory call per
library.
When planning a task whose conclusions depend on installed Python versions, list
only those distribution names in `assignment.python_packages`. `task_context`
returns the selected installed versions plus the total package count, rather than
copying the complete environment into every worker context. When no names are
listed, it returns a deterministic bounded default view and says whether that
view contains the complete inventory. A worker requests any unexpected missing
package through `CONTEXT_REQUEST`.
The latest inventory revision is authoritative for audit-host package availability
and installed versions; do not let speculative or stale assignment prose override
it. Keep declared deployment constraints separate because they describe target
environments rather than the current host. The inventory also supplies the selected
interpreter prefix and standard-library root as version-matched local evidence paths;
workers may inspect them but must not publish their absolute host paths.
Then use action `program` with one `programs` list containing the relevant configured
programs and their optional version/help arguments. The tool runs the bounded sandboxed
probes as one batch and records them in one inventory revision. Record
declared target constraints separately from current-host facts; the audit host
does not represent every deployment target.

Attempt lightweight inventory probes for relevant configured programs. Record
`not-found` separately from an executable whose version/help probe failed.
Missing programs, modules, or capabilities reduce runtime coverage but do not
stop general static review, trigger dependency installation, or justify an
ad-hoc substitute.

Assign relevant installed skills and enabled documentation MCPs to each shard,
including guidance required by repository instructions. Add their read-only MCP
patterns with `fork_tools`; do not silently omit a project-required service such
as `ask_polars` merely because it is absent from the worker's static frontmatter.

For installed programs and editors, prefer version-matched bundled help, man
pages, runtime documentation such as Vim `:help`, or other documentation shipped
with the program. Use official upstream documentation when bundled documentation
is absent, and use Context7 as complementary evidence for best practices or
cross-version comparison. Read dependency source only when those sources do not
answer an implementation-dependent question.

For Python libraries, prefer the assigned domain skill or specialized MCP,
then Context7 and official documentation. Read installed dependency source only
when documentation cannot answer a pinned-version question, and record why the
fallback was necessary. Project source remains mandatory for establishing the
repository's own reachability and impact.

Each worker may make 12 successful Context7 `query-docs` calls per assignment.
Track library-resolution attempts separately and cache successful facts by
provider, library ID, version/constraint, and normalized question. A worker that
still has a material documentation question returns a `CONTEXT_REQUEST` for a
supervisor-approved extension of five queries; the extension and use count are
recorded in state. A provider quota or authentication rejection makes Context7
unavailable for the rest of that assignment: record one limitation, do not
repeat resolution calls or grant a query extension, and continue through the
established bundled-help, official-source, and installed-source fallbacks.

Store the completed structure, technology, ownership, and guidance map as phase
`structure` for this run. Its observation includes concise conclusions only for
successfully completed environment checks that may inform later audits, rather
than inventory keys, artifact paths, or failed attempts.

Workers may return `CONTEXT_REQUEST` with a stable request ID, requested fact
kind (`program-version`, `program-help`, `program-doc`, `python-package`,
`capability`, or `documentation-budget`),
name, and reason. Pause only that assignment. The supervisor deduplicates the
request, uses the inventory tool or relevant documentation MCP, records the
result through a batched `program` call or `record_context`, atomically updates the inventory
and event stream, then resumes the same task with
`send_message`. Notify other active workers of the new revision. Before
consolidation, resume any completed worker whose version-dependent conclusion
may be affected. Unavailable facts remain explicit limitations and never
trigger dependency installation.
