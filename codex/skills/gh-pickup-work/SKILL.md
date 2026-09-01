---
name: gh-pickup-work
description: >-
  Pick up one cohesive GitHub implementation work unit selected by issue, pull
  request, list, or open-issue label filter. The unit always uses one branch,
  one worktree, and one pull request, but may solve multiple compatible issues.
  Reuse and verify existing Qwen or Codex work when available, or start
  confirmed scratch work. Always rebase onto the latest base; merging and
  automatic reassessment remain outside this workflow.
metadata:
  short-description: Continue one cohesive GitHub work unit
---

# Pick Up GitHub Work

Implement one explicitly selected GitHub work unit. The unit always owns one
branch, one worktree, and one eventual pull request, while it may cover several
compatible issues. Prefer reusable Qwen/Codex work unless the user explicitly
requests fresh implementation or a new PR.

An invocation authorizes resolution of the selected issue set, temporary
`in-progress` activity state on every issue and the unit PR, the PR-only
`ready-to-merge` lifecycle, and read-only implementation discovery in reuse
mode. It authorizes scoped edits, commits, pushes, and PR creation/update only
after one reusable candidate is selected or scratch work is explicitly
confirmed.

Read [references/issue-conventions.md](references/issue-conventions.md),
[references/pr-template.md](references/pr-template.md),
[references/mcp-suspension.md](references/mcp-suspension.md), and all applicable
repository instructions before changing files. Read
[references/runtime-policy.md](references/runtime-policy.md) and apply its
reviewed-execution boundary.

## Invariants

- Use GitHub MCP for GitHub reads and mutations and local `git` for repository
  and transport operations. Apply
  [references/mcp-suspension.md](references/mcp-suspension.md) whenever a
  required MCP operation cannot establish or retain availability.
- Produce exactly one branch, one worktree, and one PR for a compatible
  selection. Treat the selection atomically; incompatible selections stop for
  user narrowing before publication.
- Every included issue must fit one cohesive change that can be implemented,
  validated, reviewed, and merged as one PR. Stop before claiming when the
  selected set is incompatible and ask the user to narrow or replace it.
- In reuse mode, search the complete selected set and reuse one matching branch
  and worktree when ownership is unambiguous. In explicit scratch mode, create
  one fresh branch/worktree/PR without implementation discovery.
- Before any implementation edit, rebase the selected or new branch onto the
  exact latest remote base SHA. Resolve defensible in-scope conflicts and
  report each resolution.
- Verify the complete inherited diff and every issue's accepted scope
  independently, using Qwen markers and prior validation as supporting
  evidence.
- Validate area/type/priority labels and report drift without normalizing them.
  Maintain only `in-progress`, `partial`, and PR-only `ready-to-merge` under the
  shared convention. Construct/report labels as area, type, priority, status.
- Never merge, close/reopen issues, apply terminal status labels, approve a
  review, mutate unrelated metadata, or expand into nearby cleanup.
- Never access secrets or confidential data. Preserve scientific and research
  semantics unless accepted scope and current user authority explicitly permit
  a change.
- Use the project `.venv`, `uv`, and lightweight login-node checks. Do not add
  dependencies or run Slurm/GPU/heavy work without explicit authority.
- Never create or execute a temporary orchestration script. Repository source,
  tests, and scripts may be created or run only when genuinely required by the
  accepted implementation scope. Prefer existing project commands and visible
  inline checks under the shared runtime policy.
- Never query CI, GitHub Actions, checks, check runs, commit statuses, or status
  rollups. Local validation and confirmed pushed SHA are the completion gates.

## 1. Resolve the work selection

Accept one or more of:

- issue URLs, `owner/repo#N`, `#N`, or bare issue numbers resolved through the
  current repository;
- PR URLs or explicit PR numbers;
- `label:<name>`, `--label <name>`, or equivalent natural language such as
  “all open issues with the dead-code label.”

All targets must resolve to one repository and default base branch. Label
selectors include only open issues. Repeated label selectors use intersection
semantics: every result carries every requested label. Follow pagination to
completion, deduplicate results, and record the selectors, retrieval time,
resolved issue numbers, and exclusions. Re-resolve a label selection once
immediately before claiming. A second membership change marks the set unstable
and stops the unit. Zero results stop as a no-op.

Resolve PR inputs to their accepted open issues through native relationships
and exact closing references. An explicitly supplied issue or PR that is
closed, belongs to another repository, or cannot be resolved stops the unit.

Determine implementation mode before discovery:

- “start from scratch,” “create a new PR,” “create a new branch and PR,” or an
  equivalent explicit direction selects **scratch mode** for the whole unit;
- otherwise select **reuse mode**.

Scratch mode skips Section 3. It still requires safe local/remote name checks.

Call `get_me`. Refresh every selected issue and stop the entire unit if any has
`in-progress`; another workflow owns part of the requested atomic work. Keep
the selected set intact. Read each issue body, labels, maintainer clarifications,
relationships, and implementation evidence needed for scope and compatibility.

Resolve accepted scope from the issue body plus explicit maintainer
clarifications. Then overlay any explicit scope correction or remaining-work
statement from the authenticated user's unique managed reassessment comment
beginning with `<!-- codex:github-work-reassessment:v1 -->` or the legacy
`<!-- codex:github-issue-reevaluation:v1 -->`; stop on more than one matching
managed comment. The concise reassessment comment supplements the issue and
does not repeat or replace requirements it leaves unchanged.

Proceed directly when no reassessment exists. Stop when any issue needs
unresolved product, scientific, security, data, or dependency authority.

## 2. Form exactly one cohesive unit

The complete resolved issue set is the proposed unit. It may proceed only when:

- every issue can target the same repository and default base;
- required outcomes and scope boundaries do not conflict;
- the issues affect one component, one shared documentation/configuration
  surface, or one clearly named maintenance outcome;
- one reasonably sized diff and validation path can complete every issue;
- merging the one PR can independently satisfy every included issue.

Related dead code, redundancy, documentation, configuration, and small
performance issues may share a PR even when issue labels differ, provided the
implementation remains cohesive. Keep unrelated areas, independent validation
paths, prerequisites, and separately reviewable behavior out of one unit. A
label selector is selection, not evidence of cohesion. If the full selection
cannot be one PR, report the incompatible pairings and ask for a narrower
selection; keep the complete selection together in the single authorized PR.

Choose the primary functional outcome as the anchor issue; break ties by the
lowest issue number. The anchor affects naming only. Record the full issue set,
anchor, shared outcome, compatibility rationale, accepted scope per issue, and
validation path before any claim.

## 3. Discover one reusable implementation (reuse mode only)

Search before creating any branch, worktree, commit, or PR. Search current
remote metadata and local state for every selected issue, recording immutable
SHAs:

1. open PRs carrying the Qwen implementation marker or legacy worker marker
   and explicitly addressing any selected issue;
2. native linked PRs and PRs with exact closing references for selected issues;
3. local and remote branches matching `issue-<N>-*`,
   `issues-<anchor>-*`, or explicitly naming selected issues;
4. commits explicitly referencing selected issues, traced to a safe branch;
5. registered worktrees whose repository, branch, and task identity match.

Build candidate ownership across the complete unit. One existing PR/branch may
be extended to remaining unimplemented selected issues when it is the unique
candidate, the user selected them as one unit, the shared scope remains
cohesive, and no other candidate owns those issues. Preserve its existing PR
head branch; later add exact closing references for every fully covered issue.

Stop for user selection when multiple candidates could own the combined unit,
different existing PRs own different selected issues, a candidate's branch
ownership is unsafe, or combining histories would require discarding work. A
commit evidence establishes ownership only together with the selected
repository, branch, worktree, and issue/PR provenance.

If exactly one candidate exists, report and select it. If none exists, report
the complete search and ask whether to start the entire unit from scratch. No
GitHub or repository state is mutated before this confirmation. A later
confirmation selects scratch mode; repeat discovery only if remote state has
changed.

## 4. Claim and prepare the workspace

Refresh every issue immediately before claiming. If any issue now has
`in-progress`, stop without claiming the others. Otherwise create the exact
canonical label only if missing, report definition drift without repairing it,
and apply `in-progress` sequentially to every issue. Record each mutation.
Proceed only after all claims read back correctly. If any claim fails, release
every claim added by this run, verify rollback, and stop.

For an existing PR, remove `ready-to-merge` before applying PR `in-progress`.
Keep the issue-level atomic claim authoritative over PR status.

Use these canonical worktree paths when no safe reusable worktree exists:

```text
single new issue:     <project>/.worktrees/issue-<N>-<slug>
new issue bundle:    <project>/.worktrees/issues-<anchor>-<shared-outcome-slug>
single existing PR:  <project>/.worktrees/issue-<N>-pr-<P>-continue
bundle existing PR:  <project>/.worktrees/issues-<anchor>-pr-<P>-continue
single branch no PR: <project>/.worktrees/issue-<N>-continue
bundle branch no PR: <project>/.worktrees/issues-<anchor>-continue
```

Use `issue-<N>-<slug>` as a new single-issue branch and
`issues-<anchor>-<shared-outcome-slug>` as a new bundle branch. Slugs describe
the shared implementation outcome, for example
`issues-14-remove-obsolete-parsers`, rather than merely repeating a label.
Confirm the exact local and remote branch name is unused. An existing PR keeps
its current head branch name.

Require `.worktrees/` ignored. Inspect `git worktree list --porcelain`, branch
state, Git-operation state, and local changes. Reuse a worktree only when it
demonstrably belongs to the selected repository, unit, and branch. Never reset,
clean, stash, overwrite, or repurpose unrelated state. Link `.venv` to the main
project `.venv` when present.

Fetch the exact latest base and selected remote head. Record the remote head
SHA as the future lease. Create scratch branches from the exact latest base.

## 5. Rebase and verify inherited work

Rebase the prepared branch onto the exact latest base SHA before analyzing or
editing it:

```text
git rebase <latest-base-sha>
```

Use `--autostash` only for verified unit-scoped uncommitted changes in a reused
worktree; never create a manual stash. Stop when ownership of local changes or
history is ambiguous.

For each conflict, inspect both sides, the new base, callers, tests, and every
affected issue scope. Preserve compatible base changes and selected intent;
never choose wholesale ours/theirs merely to finish. Stage only resolved files,
run `git diff --check`, continue one commit at a time, and validate afterward.
If resolution needs new authority, abort the rebase, verify restoration, retain
the workspace, and report the decision.

For reused work, inspect the complete rebased diff, commits, callers, tests,
configuration, reviews, and relevant history. Classify every required outcome
for every issue as correct, partial, missing, or incorrect. Identify unsafe
semantic changes, unrelated work, and ineffective tests. Preserve correct work
and make only the smallest cohesive corrections. If inherited work cannot
safely serve the whole selected unit, stop and ask before replacing or
discarding it. Never publish a reassessment comment.

## 6. Implement and validate

Implement the smallest cohesive change satisfying every selected issue. Track
coverage per issue throughout the work. Add focused tests where practical. Use
`PYTHONPATH=src` only when established by repository instructions. Run the
fastest relevant checks and always run
`.venv/bin/pre-commit run --all-files` when the project uses pre-commit.

Review the complete diff from latest base for scope, secrets/data exposure,
unrelated files, and accidental research-semantic changes. Commit focused
changes without merge commits.

## 7. Publish the one PR safely

Immediately before pushing, refresh the remote branch/PR, every issue, and the
base. Stop rather than overwrite another actor when a remote head differs from
the recorded lease. If base advanced, rebase again and rerun affected checks.

Push an existing branch only with an exact explicit lease:

```text
git push --force-with-lease=refs/heads/<head>:<lease-sha> \
  <remote> HEAD:refs/heads/<head>
```

For a new branch, recheck that the remote ref is absent and perform a normal
first push. Update the one selected PR or create exactly one new PR. As soon as
a new PR exists, apply PR `in-progress` and confirm it.

Build the PR description from [references/pr-template.md](references/pr-template.md).
Preserve the Qwen marker when continuing a Qwen-owned PR. Begin a new Codex PR
with `## Summary`.

Refresh every covered issue and confirm native linkage through
`closed_by_pull_requests.references` or its MCP equivalent. When unavailable,
record `verified-closing-reference` only after confirming the exact closing
line and default-branch target. Correct the PR body once when linkage is
missing; a remaining failure blocks final publication.

Confirm through MCP that the PR points to the pushed SHA. Never query CI or
status rollups. A usable incomplete handoff stays draft: apply `partial` to the
PR and each issue whose accepted scope has usable pushed but incomplete work,
and retain unchecked coverage in the PR body. Do not split the unit during
publication.

Only when every selected issue is complete, local validation passes, linkage
is verified, the PR points to the pushed SHA, and no known blocking review or
correctness issue remains: mark the PR ready for review, remove `partial` from
the PR and every issue, remove all issue and PR `in-progress`, and apply PR
`ready-to-merge`.

For a usable incomplete handoff, retain appropriate `partial` labels and remove
all `in-progress` labels. Immediate continuation within the same active run may
retain its recorded claims. Finalization removes every activity lock created by
the run.

## Stop conditions and report

Suspend under the shared MCP policy on an availability,
authentication, or authorization failure. Stop without pushing on unstable or
incompatible selection, active issue ownership, ambiguous candidate ownership,
absent scratch confirmation after no reusable work is found, unsafe local
state, changed remote head, unresolved authority-sensitive conflict, required
dependency/scientific decision, or unsafe compute requirements.

On every terminal path after claiming, refresh every issue and the PR, release
this run's `in-progress`, reconcile `partial` from usable pushed work, and read
the result back. Report any cleanup blocked by authentication, authorization,
interruption, or another actor.

Report selectors and complete resolution, mode, full issue set and anchor,
cohesion decision, reuse searches and candidate selection, issue/PR URLs,
branch/worktree reuse or creation, confirmed remote-head state, accepted
scope and completion per issue, inherited-work verification, changes, local
validation, lease result, conflicts/resolutions, every issue and PR status-label
lifecycle, native linkage per issue, retained artifacts, and remaining risks.
Claim completion only after every selected issue is complete, required local
validation passes, and the one PR points to the confirmed pushed SHA.

Keep exact original, base, rebased, and pushed SHAs in private workflow state.
In the user-facing report, state that the remote PR head was verified. Include a
short SHA when it materially helps identify a commit, and provide a full SHA
when the user explicitly requests it.
