# GitHub issue and label conventions

This is the user-level source of truth shared by Codex and Qwen GitHub
workflows. Apply the universal rules to every repository. Apply a named project
area mapping only when the resolved `OWNER/REPO` matches that section.

Workflow markers record provenance for later Codex/Qwen handoff. Ownership and
edit authority come from the active workflow and explicit user authorization.
Workflow-specific publication rules and status lifecycles operate within the
taxonomy, sizing, duplicate, title, and body rules here.

## Canonical label catalog

Preserve these names, colors, and descriptions exactly when they apply.

### Area

Area labels are project-specific and use `area/<slug>`. Infer exclusive areas
from declared entrypoints and repository structure when no project mapping is
listed below. Tests and configuration inherit the behavior they serve; code
shared by multiple entrypoints belongs to `area/shared-core`.

#### `ipolharvard/mgb-notes`

| Label                | Color     | Description                                             |
| -------------------- | --------- | ------------------------------------------------------- |
| `area/build-schemas` | `#5319e7` | `build_schemas pipeline, profiler, reconciler`          |
| `area/refine`        | `#5319e7` | `refine planner/writers/validators`                     |
| `area/deploy-vllm`   | `#5319e7` | `deploy_vllm entrypoint and deployment config`          |
| `area/shared-core`   | `#5319e7` | `Shared runtime modules (agent, IO, assignment, usage)` |

Use neutral purple `#5319e7` for every `area/*` label in every repository.
Differentiate areas by their names and concise entrypoint/domain descriptions,
not by color. A named project mapping supplies the exact names and descriptions.

### Type

| Label           | Color     | Description                                         |
| --------------- | --------- | --------------------------------------------------- |
| `bug`           | `#d73a4a` | `Something isn't working`                           |
| `antipattern`   | `#bc53c4` | `Design flaw or unsafe pattern`                     |
| `dead-code`     | `#cccccc` | `Unused code: unreachable functions, dead branches` |
| `redundancy`    | `#f9d015` | `Duplicated code or configuration`                  |
| `config`        | `#0075ca` | `Hydra/config file issue`                           |
| `performance`   | `#fbca04` | `Unnecessary CPU or memory cost`                    |
| `documentation` | `#0075ca` | `Improvements or additions to documentation`        |
| `enhancement`   | `#a2eeef` | `New feature or request`                            |

### Priority

| Label    | Color     | Description                                     |
| -------- | --------- | ----------------------------------------------- |
| `high`   | `#b60205` | `Likely incorrect results or data loss`         |
| `medium` | `#fe7d3d` | `Degraded behavior, fragility, or notable cost` |
| `low`    | `#d4c5f9` | `Cosmetic, minor cleanup, or unlikely impact`   |

### Status

Status labels are cataloged for repository consistency but are never applied to
new audit issues.

| Label              | Color     | Description                                                   |
| ------------------ | --------- | ------------------------------------------------------------- |
| `duplicate`        | `#cfd3d7` | `This issue or pull request already exists`                   |
| `invalid`          | `#e4e669` | `This doesn't seem right`                                     |
| `wontfix`          | `#ffffff` | `This will not be worked on`                                  |
| `question`         | `#d876e3` | `Further information is requested`                            |
| `help wanted`      | `#008672` | `Extra attention is needed`                                   |
| `good first issue` | `#7057ff` | `Good for newcomers`                                          |
| `in-progress`      | `#fbca04` | `Work is actively underway`                                   |
| `partial`          | `#c5def5` | `Implementation exists but accepted scope is incomplete`      |
| `ready-to-merge`   | `#d876e3` | `Available evidence indicates the pull request can be merged` |

`in-progress` is an exclusive, temporary activity lock. A workflow acquires it
only after refreshing an issue that does not already carry it, and proceeds
only after the claim is confirmed. A retained supervisor/worker continuation
may reuse its recorded claim while its ledger or task identity proves that the
same workflow is actively continuing the unit.

Apply the same label to an implementation PR while that PR is actively being
changed or corrected. Finalization removes `in-progress` from every issue and
PR in the unit when the workflow publishes complete work, hands off usable
partial work, determines that no implementation should be made, blocks or
terminates, or observes the PR close. Immediate continuation within the same
supervised run may retain the claim between worker rounds. Refresh each artifact
and confirm removal before reporting the unit finalized. Authentication,
authorization, or process interruption that prevents cleanup is reported as a
stale claim requiring manual repair.

`partial` means usable implementation is durably published at an immutable
commit SHA on a pushed branch or pull request while accepted issue scope
remains incomplete. An implementation or pickup handoff applies it to every
covered issue and the implementation PR. It may coexist with `in-progress`
during an immediately continuing run; a completed handoff retains `partial`
and releases `in-progress`. Remove `partial` when accepted scope is complete or
the remote implementation is no longer usable. Local-only, uncommitted, and
unpushed work does not qualify. Curation and reassessment may maintain the issue
status from durable evidence.

`ready-to-merge` is a PR-only status applied by Codex `$gh-pickup-work` or
`$gh-reassess-work` when available evidence indicates it can be merged:
linked accepted scope is complete—or an unlinked PR has a coherent and fully
delivered objective—the changes themselves make sense, required local
validation passes, the remote PR points to the verified pushed SHA, and no
known blocking review or correctness issue remains. Workflows never query CI,
check runs, commit statuses, or status rollups. The label is deliberately based
on what the workflow can establish; it does not claim knowledge of uninspected
external checks. It cannot coexist on the same PR with `in-progress` or
`partial`. Before any workflow resumes
edits on such a PR,
remove `ready-to-merge` and apply `in-progress`. Codex completion releases
issue and PR `in-progress`, removes `partial`, and then applies
`ready-to-merge`. Qwen completion releases `in-progress` and `partial` without
applying `ready-to-merge`. A short-lived work reassessment releases its
temporary issue and PR claims before reconciling evidence labels.

### Label order

Whenever a workflow constructs a label list, sends labels to GitHub, or reports
canonical labels, order them as: area first, type second, priority third, then
status labels. Preserve unrelated labels after the canonical labels. GitHub's
UI may render labels in a different order; workflows control only their request
and report order.

### Pull request taxonomy

An implementation pull request carries the useful canonical taxonomy of the
issues it currently covers. Include each distinct justified `area/*` and type
label from those issues; unlike an issue, a PR may therefore have multiple area
or type labels. Include exactly one priority label: the highest priority among
the covered issues, ordered `high` over `medium` over `low`. Recompute that
priority whenever issue coverage changes, and remove lower canonical priority
labels from the PR.

There is no fixed maximum label count, but labels must remain discriminating.
Do not add every repository label, labels inferred only from nearby code, or
duplicate concepts merely for completeness. Preserve genuinely unrelated
labels already on the PR unless current evidence makes them incompatible. PR
status labels follow their separate lifecycle and do not count as taxonomy
labels.

Pull requests use the issue-label API. With the GitHub MCP tools, read the
current PR labels through `issue_read` method `get_labels`, then call
`issue_write` method `update` with the PR number in `issue_number` and the
complete desired label list, preserving unrelated labels. Read the labels back
through `issue_read` before recording success. `label_write` manages repository
label definitions, while `update_pull_request` does not mutate labels; neither
is the PR-label assignment interface.

## Label responsibility

- The issue-curation workflow owns correction of canonical label definitions,
  existing open-issue taxonomy and semantic status labels, and taxonomy labels
  on pull requests linked to inspected issues. It derives PR taxonomy from all
  covered issues, not merely the issue that caused the PR to be inspected. It
  never mutates `in-progress`.
- The `gh-audit-repo` workflow may create a missing canonical label required by
  a verified finding and may directly refine a matching open issue only when it
  has neither `in-progress` nor `partial`. It does not repair existing label
  definitions or mutate protected/closed issues. In every non-dry-run audit it
  adds one evidence comment and closes each independently code-verified
  completed, invalid, or duplicate issue that has neither a lock nor an open
  implementation pull request. Dry-run reports those operations without
  applying them.
- The `gh-propose-enhancement` workflow may create one new proposal-derived
  enhancement issue and any missing exact canonical label it requires. It does
  not edit existing issues or apply status labels; existing label-definition
  drift is routed to curation.
- Work reassessment validates whether issue scope and PR changes make sense and
  reports taxonomy drift without normalizing taxonomy. It may create/apply
  `in-progress` for its run and must release only its own claims. From verified
  implementation evidence it may maintain issue `partial` and PR
  `ready-to-merge`, including an unlinked PR whose objective and changes are
  independently justified. Issue implementation and issue pickup may also
  reconcile their implementation PR's derived taxonomy while maintaining the
  workflow-owned `in-progress` and `partial` states. Qwen issue implementation
  may remove `ready-to-merge` only when explicitly resuming changes and then
  applies PR `in-progress`. Codex issue pickup additionally owns applying and
  removing PR `ready-to-merge`. These workflows may create their owned exact
  labels when missing; definition drift is left for curation.
- Preserve unknown and unrelated labels. Status mutation requires an explicit
  curation workflow or an implementation workflow's
  `in-progress`/`partial`/`ready-to-merge` lifecycle; never infer it from an
  ordinary request to inspect code.

## Pull request issue linkage

Every implementation PR targets the repository default branch and contains one
exact `Closes #N` line in its description for each fully covered issue. These
closing references create the native relationship represented by GitHub's
Development section and close the issues when the PR merges. One coherent PR
may close multiple compatible issues.

An incomplete implementation remains a draft PR intended to finish the same
accepted issue scope. When the work requires independent PRs that could be
merged separately, split the issue through curation before publication.

After creating or updating the PR, refresh every covered issue and read
`closed_by_pull_requests.references` or its GitHub MCP equivalent. Record:

- `verified-native` when the relationship includes the implementation PR;
- `verified-closing-reference` when the relationship field is unavailable but
  the PR targets the default branch and contains the exact closing reference;
- `failed` when an available relationship excludes the PR or its closing
  reference/base is incorrect.

Correct a failed PR description once and refresh the relationship. A remaining
failure blocks finalized publication and is reported with the exact issue and
PR. PR bodies follow the centralized
the adjacent `github-pr-template.md` specification.

## Workflow provenance markers

Use role-based markers so persistent GitHub artifacts survive workflow renames
and later Codex skills can identify which Qwen workflow produced them:

- Qwen audit-created or materially audit-revised issue:
  `<!-- qwen:managed-issue:v1 -->`;
- Qwen conversation-derived enhancement proposal:
  `<!-- qwen:proposed-enhancement:v1 -->`;
- Qwen issue implementation PR: `<!-- qwen:issue-implementation:v1 -->`;
- Qwen curator-created/revised issue or curation comment:
  `<!-- qwen:issue-curation:v1 -->`.

Markers are provenance, not ownership gates. Recognize
`<!-- qwen:codebase-audit-issue:v1 -->` and
`<!-- qwen:github-issue-worker:v1 -->` as legacy markers indefinitely. Migrate
a legacy audit marker when audit or curation next materially edits that issue
body. Migrate a legacy PR marker when implementation next edits the PR body.

Submit a required HTML provenance marker at most once as part of the intended
final artifact body. Some clients may omit HTML comments from their returned
body representation. If read-back preserves the intended visible semantic
content but omits the marker, treat marker verification as unavailable: do not
retry, insert a probe marker, move the marker, or perform another live write
solely to diagnose serialization. Never claim the service stripped a marker
unless an independent raw representation proves it. Marker verification alone
is never a completion gate, and a workflow must report the exact number of API
write calls rather than only the number of affected artifacts.

## Classification rules

Every created issue has exactly one area label, one type label, and one priority
label, supplied in that order. New audit or split issues have no status label.
Confidence is separate from priority:

- **High confidence:** a reproduction, focused test failure, or direct logical
  proof establishes the finding.
- **Medium confidence:** a reachable code path plus a concrete failure mode or
  measurable cost establishes the finding without runtime reproduction.
- Reject low-confidence speculation and style preferences.

Priority measures impact, not certainty. A low-priority issue must still have
high or medium confidence.

## Issue sizing and grouping

Default to one issue per root cause. Combine small related findings when one
cohesive issue is easier to implement and review. Multiple findings may be
grouped only when all of these are true:

- they have the same area, type, and priority;
- they support one clearly named maintenance outcome;
- they can be completed and validated together in one reasonably sized PR;
- none is a prerequisite or blocker for another;
- each finding has concrete evidence and its own observable required outcome.

Grouping is particularly appropriate for related dead code, redundancy,
documentation gaps, or small performance costs within one component. Keep bugs
separate unless they demonstrably share one root cause and fix. Keep cross-area
work, different risk levels, unrelated behavior, and vague cleanup backlogs in
separate issues. Split an issue when reviewers could reasonably accept one part
and reject another. Represent one shared root cause with one issue rather than
repetitive per-entrypoint issues.

## Duplicate rule

Compare root cause, affected paths or symbols, failure mode, desired outcome,
and required outcomes—not merely title wording. Classify a candidate as one
of: new, duplicate, already fixed, covered by pull request, regression, or
insufficiently distinct. Read plausible matches in full. Return the existing
canonical record unchanged for a duplicate. A regression must cite the older
closed issue and show current evidence.

GitHub `search_issues` uses natural-language semantic matching. Treat it as a
discovery aid for conceptual, paraphrased, or possible body-only matches, not as
an exact or exhaustive index. Pass `owner` and `repo` separately and keep GitHub
search qualifiers out of `query`. A zero-result semantic search is inconclusive
and never proves absence. When the workflow provides a complete record inventory
or committed history, use it for indexed duplicate coverage, then read every
plausible issue or pull request in full.

## Title

Use a concise sentence-case title in the present tense that names the specific
symptom and impact. Prefer concrete wording over vague verbs such as “Improve,”
“Refactor,” or “Clean up.”

## Body

Keep the body concise and use this section structure:

```markdown
## Problem

<Concise description of the behavior, affected user or workflow, and concrete impact.>

## Example

<Optional short input/output, before/after, or concrete scenario.>

## Evidence

<Minimal reproducible evidence with paths/symbols, current-branch facts, relevant history, and explicit assumptions.>

## Required outcome

<Observable requirements and any non-routine validation evidence needed to define completion.>

## Scope boundaries

<Optional implementation surface: the file, symbol, component, or behavior that changes.>
```

For a conversation-derived enhancement proposal, `Evidence` records the
observed need and proposal context instead of claiming current-code proof.
Identify material assumptions explicitly. The issue remains a proposal until a
code-aware workflow verifies implementation details.

When a workflow requires one or more provenance markers, place them before
`## Problem`; markers are not part of the shared body format and grant no edit
authority. Preserve distinct existing provenance markers when another workflow
materially revises the same issue.

`Problem` is the concise human-readable entry point. Place `Example` immediately
after it when a concrete example explains the behavior more clearly. `Evidence`
and `Required outcome` primarily serve implementation
and verification agents. Format each section according to its content: use prose
for a single statement and bullets when multiple distinct items are easier to
scan. Include `Scope boundaries` when it adds useful implementation detail, and
describe the implementation surface
needed for the required outcome in short, concise affirmative prose. Translate
repository guidance into public behavior and implementation requirements.
References to agent instructions, `AGENTS.md`, skills, workers, internal routing,
or tool mechanics are prohibited in published issue text. Keep implementation progress, implementation
plans, repeated labels, exhaustive affected-file lists, and large logs/data/code
excerpts outside issue bodies. Implementation progress belongs in PR Issue
coverage checkboxes, with each checkbox mapped to one issue `Required outcome`.
Routine expectations such as passing relevant tests belong in workflow
validation rather than issue requirements.

Published issue titles, bodies, and comments must never expose absolute filesystem paths
from an audit host, worktree, home directory, temporary directory, or workflow state. Cite
source locations only as repository-relative paths, optionally with a symbol or line, such
as `src/package/module.py:42` or `Package.method`. Convert absolute evidence paths to that
form before publication; if a path cannot be expressed relative to the audited repository,
omit it from public text.
