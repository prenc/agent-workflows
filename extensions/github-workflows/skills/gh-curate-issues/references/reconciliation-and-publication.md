## Stage 3: reconcile globally

After all per-issue reports are available, reconcile them against the current
bounded supervisor history view and each other. Resolve cross-issue duplicates,
shared PR coverage, split collisions, taxonomy consistency, and status
conflicts. For each linked PR, establish its complete covered-issue set before
deriving labels; reconcile it once even when several selected issues link to it.
Refresh and read any newly plausible eligible match in full. Older records enter
reconciliation only through the explicit-link rule.

The supervisor decides the final operation set. Worker proposals are evidence
records rather than publication authority. Every final decision records one of
these evidence sources:

- `worker-live-mcp`;
- `supervisor-reconciliation`;
- `blocked` with the exact missing evidence.

## Stage 4: determine canonical operations

### Eligibility and locks

Every open issue is eligible for curation regardless of author, assignee,
selection mode, or provenance. `in-progress` and `partial` lock title/body
revision, splitting, and closure. A locked issue remains eligible for canonical
label-definition repair, area/type/priority correction, and compatible
semantic-status maintenance. Report suspected stale workflow locks.

Preserve `<!-- qwen:managed-issue:v1 -->` as audit provenance. When materially
revising a legacy managed audit issue, migrate
`<!-- qwen:codebase-audit-issue:v1 -->` to the canonical audit marker. Include
`<!-- qwen:issue-curation:v1 -->` once in each body materially created or
revised by this workflow, after any distinct audit marker.

### Labels

Ensure canonical label definitions have their exact names, colors, and
descriptions. Give each inspected issue exactly one area, type, and priority
label while preserving unrelated labels. Apply semantic statuses only when the
shared convention and the evidence gates below support them.

Check every pull request linked to an inspected issue. Derive its taxonomy from
all issues it covers: the distinct justified area and type labels, plus exactly
one priority label representing the highest-priority covered issue. Preserve
unrelated PR labels and current implementation-owned statuses. Do not add
speculative, adjacent, or redundant labels, and do not impose a numeric cap.

Construct, submit, and report labels as area, type, priority, then status.
GitHub controls stored and displayed label order, so successful curation is
defined by label membership rather than UI ordering.

### Canonical issue text

For each unlocked issue, normalize a deviating title or body while preserving
the accepted root cause and maintainer intent. After provenance markers, use:

1. `## Problem`
2. optional `## Example`
3. `## Evidence`
4. `## Required outcome`
5. optional `## Scope boundaries`

Write Problem as concise human-readable prose. Use Example for an input/output,
before/after, or concrete scenario that materially improves clarity. Relocate
useful accepted content into canonical sections. Format each section according
to its content: use prose for a single statement and bullets when multiple
distinct items are easier to scan. Keep routine test-passing expectations in
workflow validation rather than Required outcome.
When migrating an older `Done when` section, preserve its accepted requirements
in Required outcome and discard checkbox state because progress belongs in the
implementation PR. Use Scope boundaries for short, concise
affirmative prose naming the implementation surface needed for the required
outcome. A distinct Scope boundaries section is useful when Required outcome
does not already make that surface clear.
Translate internal guidance into repository-facing behavior and remove references
to agent instructions, skills, workers, routing, or tool mechanics from published
text. Stay within the shared size limits.
Represent unresolved code facts as audit or reassessment routing rather than a
new assertion. For a locked noncanonical issue, retain an exact deferred text
proposal in the report.

### Safe splits

Split an unlocked issue when GitHub-record evidence establishes multiple
independently deliverable root causes or outcomes that do not form one cohesive
PR. Select one primary scope for the original and prepare complete canonical
children, including taxonomy, required outcomes, duplicate checks, and
reciprocal relationships.

Refresh the original and repeat the exact-match search immediately before
publication. Create all confirmed children before narrowing the original. A
partial publication retains all untransferred scope in the original and reports
created child URLs as resumable state.

### Semantic status and closure

Use these evidence gates:

- `duplicate`: the same root cause, failure mode, and desired outcome; link the
  canonical issue in the managed curation comment and close as not planned.
- `invalid`: current GitHub records directly disprove the premise; close as not
  planned.
- `wontfix`: an explicit maintainer decision declines implementation; close as
  not planned.
- completed closure: a merged attached PR and accepted issue record establish
  delivery of the complete requested outcome.
- `question`: one exact missing decision blocks an implementable scope; maintain
  that question in the managed curation comment.
- `help wanted`: the unlocked, unassigned issue is actionable for outside
  contribution under repository practice.
- `good first issue`: the work is self-contained, low-risk, testable, and free
  of unresolved architecture, scientific, security, data, or infrastructure
  decisions.
- `partial`: usable implementation exists at a pushed immutable SHA while the
  accepted scope remains incomplete.

Active implementation workflows own `in-progress` and PR-only
`ready-to-merge`. Curation validates and reports those states. Preserve
compatible custom labels and resolve incompatible curator-owned semantic
statuses according to their current evidence.

Use one managed curation comment per issue when duplicate handling or a blocking
question requires it. Its first line is:

```html
<!-- qwen:issue-curation:v1 -->
```

## Stage 5: refresh and apply serially

In dry-run mode, render the exact ordered operation set and finish with zero
GitHub writes; the shared cache may still commit a completed synchronization.
In an applying run, refresh the issue, labels, comments, locks, and linked PRs
immediately before each operation and recompute when relevant state changed.

Compute one complete desired state per issue and linked PR before mutation.
When the live title, rendered body, labels, semantic status, and linked-PR
taxonomy already match, record a true no-op and perform no GitHub write. Ignore
semantically irrelevant whitespace and do not rewrite solely to reorder already
accepted sections, re-submit a provenance marker, or probe marker visibility.
For a materially differing unlocked issue, perform at most one final body write.
Verify title, visible canonical sections, accepted content, required outcomes,
labels, and state once through MCP.
GitHub MCP issue-body reads may omit HTML comments; record marker verification
as `unavailable-through-mcp-readback` while accepting matching visible
semantics. Marker diagnostics do not create additional writes or probes.

Apply only differing issue and deduplicated PR-label mutations serially. Never
resubmit an identical complete label set. Read each PR's labels back after
mutation and verify the complete desired membership. Track every
write with its issue or PR, operation, purpose, and outcome. Commit the initial synchronized cache before worker
analysis, including in dry-run mode. After successful GitHub mutations, use a
short second optimistic transaction to refresh affected records. A failed
post-write cache refresh is reported but does not make a recorded GitHub write
ambiguous. Keep run artifacts user-private and retain the run directory as
recovery and audit state. Persistent history files are retained by design.

After every task attempt is terminal and integrated and scheduler/pending work
is empty, finish the run normally only when every required logical task has an
integrated completion. If at least one required logical task cannot succeed,
call `run_manage` with action `finish`, outcome `blocked`, and a concise
non-empty note. A blocked run is terminal and cannot resume.

## Stage 6: report

Report:

- repository, default branch, and immutable SHA;
- selected, inspected, unchanged, refined, split, closed, and blocked issues;
- every per-issue evidence source and worker capability failure;
- documentation MCP queries, public sources used, and research limitations;
- configured cutoff, history-days, history path, old/new watermark, and
  bounded-history coverage;
- GitHub-history generation and compact records reused, refreshed, and imported;
- explicit older-record exceptions and issue-specific candidate counts;
- open and bounded closed pull-request history coverage and relationships established;
- created children and retained original scopes;
- format, issue/PR taxonomy, label-definition, status, and managed-comment changes;
- duplicate canonicals and terminal evidence;
- requested/effective concurrency and one-worker-per-issue coverage;
- code-dependent audit or reassessment routing;
- dry-run state and exact GitHub write event history;
- marker read-back state using `unavailable-through-mcp-readback` where needed;
- external mutations, partial failures, and resumable state;
- GitHub-controlled label display ordering as a presentation limitation.

The report distinguishes actual MCP operation failures from Qwen's
informational offline display. It describes coverage as the complete configured
history window plus explicit relationships, and labels it as a bounded view of
repository history.
