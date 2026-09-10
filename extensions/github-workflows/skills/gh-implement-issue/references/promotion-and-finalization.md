## Stage 5: supervisor verification of the draft

For a verification-only round on an inherited ready PR, accept
`NO_IMPLEMENTATION` only after independently confirming the existing PR,
branch, and worktree remained unchanged and every accepted outcome is correct.
Finalize that already-ready PR unchanged through Stage 7; do not send it through
draft promotion. A proven gap must return `CORRECTION_NEEDED`; create an
implementation round and perform its required draft transition and push
preflight before editing.

For implementation rounds and new work, require the worker's pushed draft PR
and independently inspect the complete rebased diff, callers, tests,
configuration, and required outcomes. Treat PR coverage and worker validation
as assertions. Preserve correct work and send the smallest bounded correction
back to the same worker for any missing, incorrect, unrelated, or regressive
change. The worker commits, pushes, and updates the same draft before
verification repeats.

Do not invent a task reference or substitute an inline assignment for any worker
round. If a correction or additional inspection is delegated, register it through
`task_manage` and pass only the exact returned `task_ref`; a worker whose
`task_context` call fails performs no work.

Draft-to-ready promotion requires:

- every required outcome for every covered issue is complete;
- focused tests and the repository's pre-commit command pass under the assigned
  environment using the appropriate no-sync form;
- the complete diff is cohesive and contains no unrelated or sensitive work;
- no unresolved authority-sensitive decision remains;
- the worktree has no active Git operation.
- the draft PR body follows the shared template and its head matches the pushed commit.

## Stage 6: promote centrally

Refresh the worker-created draft PR, its head/base, and the remote branch. Assign
the PR to the authenticated user when the MCP surface supports it. Obtain its
complete current labels through the shared PR-label convention, reconcile the
shared derived PR taxonomy, and apply PR `in-progress` during active supervisor
verification only when complete membership and read-back are available. Confirm the body follows
`github-pr-template.md`, begins with
`<!-- qwen:issue-implementation:v1 -->`. Send body corrections to the worker so
the draft remains worker-maintained.

Refresh each fully covered issue after the PR body is current. Require the PR
in `closed_by_pull_requests.references`, or—when that field is unavailable—
verify the exact closing reference and default-branch target. Correct a failed
body through one bounded worker correction; a remaining failure blocks finalized publication. For each
incomplete unit issue, confirm that its closing reference is absent and its
coverage remains unchecked. Confirm the PR head equals the pushed SHA. Remote
CI is outside the completion gate.

When every promotion requirement is satisfied, the supervisor updates the PR
from draft to ready for review. A usable incomplete handoff remains a draft.
Workers keep every created or updated PR in draft state; only the supervisor
performs a draft-to-ready transition.

## Stage 7: finalize every unit

Finalization is mandatory on every normal exit and is serialized by the
supervisor. Refresh each artifact, apply the transition, and read it back:

| Outcome                                       | PR state          | Issue labels                                                                  | PR labels                                                          |
| --------------------------------------------- | ----------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| continuing now                                | current           | `in-progress`; add `partial` only to each issue with durable incomplete scope | `in-progress`; add `partial` when any issue is incomplete          |
| complete Qwen publication                     | ready for review  | remove `in-progress` and `partial`                                            | remove `in-progress` and `partial`                                 |
| usable incomplete handoff                     | draft             | remove `in-progress`; apply `partial` only to incomplete issues               | remove `in-progress`; apply `partial` when any issue is incomplete |
| blocked/terminated without usable remote work | unchanged or none | remove this workflow's `in-progress`; remove unsupported `partial`            | same                                                               |
| `NO_IMPLEMENTATION`                           | none or unchanged | remove this workflow's `in-progress`; remove unsupported `partial`            | same                                                               |

Qwen leaves `ready-to-merge` to Codex `$gh-pickup-work`. Use
`$gh-pickup-work --assess-only` for reassessment. Assignments remain responsibility metadata after a
successful publication; release workflow-added assignment for abandoned or
no-implementation units.

Use `NO_IMPLEMENTATION` only when no issue in the unit requires a code change.
When it applies to only part of a multi-issue unit, process it as a
`SPLIT_REQUESTED` per-issue disposition and continue the remaining cohesive
issues. Verify the worker's evidence, publish no issue comment, and route any
premise, scope, invalidity, or obsolescence conclusion to reassessment or
curation in the final report. Issue state and terminal labels remain
curator/maintainer responsibilities.

A unit is finalized only after issue and PR reads confirm the intended status
and derived taxonomy label state. If authentication, authorization, interruption, or a conflicting actor
prevents cleanup, report each retained label and URL prominently as manual
repair state.

After every task attempt is terminal and integrated and scheduler/pending work
is empty, finish the run normally only when every required logical task has an
integrated completion. If at least one required logical task cannot succeed,
call `run_manage` with action `finish`, outcome `blocked`, and a concise
non-empty note. A blocked run is terminal and cannot resume.

## Final report

Report resolved inputs, early-ledger path, claim order/read-backs/rollbacks and
time from resolution to confirmed lock, automatic grouping and rejected pairings, anchors,
units, effective concurrency, issue/PR URLs, worktrees/branches, reused or
created state, accepted scope sources, worker GitHub evidence source and MCP
limitations, rounds/checkpoints, inherited-work verification, per-issue
coverage, changes, conflicts, validation and confirmed remote-head state,
lease outcomes, PR body and native linkage for every issue, assignments,
complete status transitions, finalization read-back, retained artifacts,
external mutations, and blockers.

Keep exact original, base, rebased, and pushed SHAs in the private ledger for
verification, recovery, and handoff. In the user-facing report, state that the
remote PR head was verified. Include a short SHA when it materially helps
identify a commit, and provide a full SHA when the user explicitly requests it.
