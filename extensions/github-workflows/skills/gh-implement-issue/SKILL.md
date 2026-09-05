---
name: gh-implement-issue
description: Immediately resolve and lock supplied GitHub issues, then supervise them or supplied implementation PRs as automatically grouped one-PR implementation units with bounded fresh-context workers, reusable isolated worktrees, verified rebases, worker-owned draft PR publication, native issue linkage, and supervisor-owned promotion and finalization.
priority: 20
argument-hint: '[-n <N>] [--resume | [--separate] <issue-or-PR> ...]'
allowedTools:

  - task
  - send_message
  - list_agents
  - run_shell_command
  - grep_search
  - read_file
  - write_file
  - glob
  - mcp__github_workflows__run_manage
  - mcp__github_workflows__run_status
  - mcp__github_workflows__task_manage
  - mcp__github_workflows__workflow_feedback
  - mcp__github__add_issue_comment
  - mcp__github__get_me
  - mcp__github__issue_read
  - mcp__github__issue_write
  - mcp__github__label_write
  - mcp__github__list_branches
  - mcp__github__list_commits
  - mcp__github__list_issues
  - mcp__github__list_label
  - mcp__github__list_pull_requests
  - mcp__github__pull_request_read
  - mcp__github__search_pull_requests
  - mcp__github__update_pull_request
---

# Implement GitHub Issues

Implement explicitly supplied GitHub issues or existing implementation pull
requests. Read `../../references/github-issue-conventions.md`,
`../../references/github-pr-template.md`, and
`../../references/github-mcp-suspension.md`, plus repository instructions before changing
state. Read `../../references/github-runtime-policy.md` and
apply its reviewed-execution boundary to supervisors and workers. The
supervisor owns scope resolution, worktrees, rebases, issue state, scheduling,
independent verification, draft-to-ready promotion, and finalization.
Fresh-context workers implement, validate, commit, push, and maintain the draft
PR for one logical unit at a time.

`-n N` limits simultaneously active implementation units. It
must be positive and defaults to 3. Effective concurrency is the minimum of
`N`, unresolved units, and available capacity. One unit owns exactly one
branch, worktree, worker task, and eventual PR, but may cover multiple
compatible issues. Each issue belongs to exactly one active unit.

Automatically group compatible supplied issues into units. `--separate`
places each supplied unimplemented issue in its own new-PR unit while keeping
issues already covered by the same existing PR together.

`--resume` uses the original targets and grouping mode recorded in the current
unfinished run and may be combined only with `-n`. Use
`mcp__github_workflows__run_manage` with action `resume`, then reconcile claims, issues,
pull requests, branches, worktrees, and pending mutations before continuing.

An invocation authorizes scoped assignment, temporary issue/PR `in-progress`,
evidence-backed `partial`, removal of stale PR `ready-to-merge` when resuming
changes, worktree/branch reuse or creation, commits, pushes, and creation or
update of the resolved PRs and their derived taxonomy labels. Merge, issue closure, issue taxonomy normalization,
dependency changes, and heavy computation require separate authority.

## Operating model

Use GitHub MCP for GitHub records and mutations and local Git for repository
and transport operations. `get_me` establishes the user used for assignments
and safe branch ownership. Apply `../../references/github-mcp-suspension.md` when a required
MCP operation cannot establish or retain availability. Qwen's MCP status
display is informational.

Workers receive a separate server-enforced GitHub MCP connection for targeted
verification and the narrow creation/update of their assigned draft PR. They
must establish the required read and draft-PR tools before analysis or edits.
`MCP_UNAVAILABLE` from any worker
suspends all units under `../../references/github-mcp-suspension.md`; supervisor snapshots
are resume context rather than a fallback. The supervisor remains authoritative
for claims, scheduling, issue/label state, final live verification, and every
draft-to-ready transition.

Use the supervisor-selected environment, `uv`, and lightweight login-node checks. Keep
repository-root `data/` contents, secrets, unrelated changes, CI/check APIs,
merges, reviews, and reassessment outside this workflow. Validate issue
taxonomy and report its drift. Reconcile each implementation PR to the distinct
justified area/type labels of all covered issues and exactly one priority label,
the highest among them; preserve unrelated labels and avoid speculative or
redundant additions. Other label mutations remain limited to `in-progress`,
`partial`, and removal of stale PR `ready-to-merge`.

Do not create or execute temporary orchestration scripts or invoke the
extension's Python modules. Workers may create
and run repository source, tests, or scripts only when required by the accepted
implementation scope. Prefer existing project commands and transparent shell
inspection; use visible `.venv/bin/python -c` checks only when simpler tools
are insufficient.

## Stage 1: resolve and lock requested issues immediately

The first GitHub work is minimal target resolution followed by the
`in-progress` claim. Before this gate, read only the target identity, state,
labels, and PR relationship fields required to make the claim safely. Hydrate
complete scope and implementation evidence after every claim succeeds.

Resolve repository and issue identity for every URL, `owner/repo#N`, `#N`, and
bare number. For a PR input, read only the PR's repository, state, body closing
references, and native issue relationships needed to obtain its open issue
numbers. Deduplicate issue identities. Stop an unresolved, closed, or
relationship-free target before claiming anything.

Before the first mutation, create private run state with
`mcp__github_workflows__run_manage`. Pass `repository`, `n`, `targets`, and
`separate` as top-level tool arguments; never wrap them in `request` or
`inputs`, and never stringify them as JSON. On resume,
pass only an explicitly supplied `n` in addition to the action and workflow.
The run is stored under:

```text
$QWEN_CODE_PROJECT_DIR/workflows/gh-implement-issue/current/
```

Record supplied inputs, repositories, minimally resolved issue numbers/URLs,
timestamps, pre-claim labels, every attempted mutation, successful claim,
read-back, rollback, and pending cleanup through `mcp__github_workflows__run_manage`
action `checkpoint`; keep detailed unit results declarative and journal significant
transitions. The `pending` field contains only external mutations awaiting read-back,
rollback, or reconciliation, never requested targets or unplanned units. Update it
atomically after every GitHub mutation so interruption leaves an actionable recovery
record.

Call `get_me`, then minimally refresh every resolved issue for state and labels.
If any issue already has `in-progress`, record its URL and stop before claiming
the remaining set; treat that lock as authoritative. Otherwise ensure
the exact canonical `in-progress` label exists, reporting definition drift
without repairing it. Apply `in-progress` sequentially to every requested issue
and read each issue back immediately. Preserve all unrelated labels.

The initial claim is transactional across the complete requested issue set. If
any application or read-back fails, release every `in-progress` claim added by
this run, verify each rollback, and stop. Only after every requested issue is
confirmed locked may the supervisor perform scope hydration, implementation
discovery, grouping, assignment, worktree preparation, or worker launch.

## Stage 2: hydrate, classify, group, and assign

With the early claims held, fetch complete issue scopes, labels, maintainer
clarifications, native relationships, plausible implementation PRs, and
existing PR bodies, commits, reviews, comments, heads/bases, and immutable SHAs.
Establish accepted scope from the issue record and explicit maintainer
clarifications. Overlay any explicit correction or remaining-work statement
from the authenticated user's unique current managed reassessment beginning
`<!-- codex:github-work-reassessment:v1 -->` or the legacy
`<!-- codex:github-issue-reevaluation:v1 -->`; stop on more than one matching
managed comment. The concise reassessment supplements unchanged issue
requirements rather than repeating or replacing them.

Classify targets through MCP:

- an issue with no unambiguous open implementation PR is eligible for a new-PR
  unit;
- an issue with one unambiguous open implementation PR continues that PR;
- a PR resolves through its native relationships and closing references to at
  least one accepted open issue;
- supplied issues already linked to or explicitly closed by the same PR form
  one fixed existing-PR unit;
- repeated inputs resolving to the same branch/PR form one unit.

Stop an ambiguous target without choosing between multiple PRs. Never add a
newly supplied issue to an existing PR unless that PR already links or
explicitly closes it.

Unless `--separate` is set, partition eligible new-PR issues using a
compatibility matrix. Issues may share a unit only when all of these hold:

- they use the same repository and default base branch;
- none has a competing implementation;
- they affect the same component, one shared documentation/configuration
  surface, or one cohesive behavior;
- their required outcomes and scope boundaries do not conflict;
- one reasonably sized diff and validation path can complete every issue;
- merging one PR can independently satisfy every included issue.

Use predicted file overlap as the primary partitioning signal. When two or
more eligible issues are likely to modify the same files or a tightly coupled
edit surface, group them by default to avoid serial PR merge conflicts,
provided their combined diff remains reasonably sized and cohesive. Infer the
likely paths from hydrated issue scope and repository structure; exact path
certainty is not required before worker investigation.

Group overlapping scopes when their combined size remains reasonably
reviewable. Keep individually large scopes separate when the combined burden is
large, their requirements conflict, or either change remains independently
reviewable without creating substantial merge-conflict risk. A shared broad
label such as `documentation` is only supporting evidence: issues in unrelated
documents or documentation areas remain separate, while small compatible
changes to the same files should normally share a unit.

Favor a small cohesive bundle over maximizing issue count. Labels may differ
when the implementation surface is genuinely shared; issue taxonomy remains
per issue. The convention's same-taxonomy rule governs grouping findings into
one issue, not grouping several existing issues into one implementation PR.
There is no fixed issue-count limit. Keep unrelated areas, independent
validation paths, prerequisites, and separately reviewable changes with low
overlap risk in separate units. Record predicted overlapping paths, the
compatibility decision, size judgment, and rejected pairings in the ledger.

For a new multi-issue unit, choose the issue representing the primary
functional outcome as its anchor; break ties by lowest issue number. The anchor
affects naming only and grants no priority or ownership over sibling issues.

For each completed unit plan, refresh every issue and confirm the exact early
claim remains present. Assign every issue sequentially and record each
mutation. If an assignment fails, release assignments added for that unit and
release this run's claims for every unit that has not launched, verify rollback,
and stop. Launch work only after all issue claims and assignments read back
correctly.

If hydration or classification determines that an issue is ambiguous,
unsuitable, requires unresolved authority, or cannot enter any coherent unit,
release this run's `in-progress` claim on that issue immediately and verify the
result before continuing or stopping. Release unsuitable early claims before
waiting for user clarification. Update the early ledger with the disposition
and cleanup result.

Require each resulting unit to fit one coherent eventual PR. Route an existing
issue whose own accepted scope needs independently mergeable PRs to
`/gh-curate-issues` for splitting.

## Stage 3: prepare or reuse the worktree

Use:

```text
preferred root:   <project>/.worktrees
fallback root:    ${XDG_CACHE_HOME:-~/.cache}/agent-workflows/worktrees/<project-id>
single new issue: <root>/issue-<N>-<slug>
new issue bundle: <root>/issues-<anchor>-<slug>
existing PR:      <root>/issue-<anchor>-pr-<P>-continue
```

Use the preferred root only when `git check-ignore --no-index` confirms
`.worktrees/` is ignored. Otherwise use the project-namespaced fallback root;
do not modify ignore files merely to place a worktree. Inspect registered worktrees, branch ownership, Git
operation state, local changes, and remote refs. Reuse matching durable Qwen or
Codex state when repository, unit, and branch ownership are unambiguous.

Create a new branch `issue-<N>-<slug>` for a single issue or
`issues-<anchor>-<slug>` for a bundle from the exact latest default-branch SHA.
Confirm the chosen local and remote name is unused. For an existing PR, fetch
its exact head and latest base, record the remote head as the lease SHA, align
or reuse the local head branch, and rebase onto the latest base before
implementation edits.

Resolve defensible conflicts from issue scope, both sides, callers, and tests.
Record each conflict and focused validation. Abort and restore the rebase when
resolution requires new product, scientific, dependency, security, or data
authority.

Choose the worktree environment after the rebase and before worker assignment.
Use `native` for a non-Python project. For Python, use `shared` only when the
unit cannot affect dependency inputs, packaging, entry points, compiled
extensions, or import layout and the repository establishes unambiguous
project-relative source roots. Link the worktree `.venv` to the verified main
project `.venv`; do not overwrite an unexpected path or follow its target.

Use `isolated` for every other Python unit and when a repository command may
write the environment. Stop any worker before changing modes. Classify the
worktree `uv.lock` first. Check a tracked lock with
`uv lock --check --offline --no-python-downloads` and stop on staleness unless
its update is authorized; only then may mutating `uv lock` run. Inspect `.venv`
without following it. Unlink it only when it is a symlink resolving exactly to
the verified main `.venv`; block on any other existing target. Create the now
absent worktree `.venv` with the main environment's interpreter and populate it
only with `UV_OFFLINE=1 uv sync --frozen --no-python-downloads` plus the
documented groups or extras. Never run either command through a `.venv`
symlink. The supervisor alone runs these environment writers.

Respect the repository's lock convention: preserve a tracked lock, keep an
ignored lock local, or retain an otherwise unwanted generated lock in private
cache beside the managed worktree root and remove its temporary worktree copy
after synchronization. Make the cache directory `0700`; reject symlinks and
foreign ownership. Do not
update a stale tracked lock without scope or user authority. Reuse one lock
only for worktrees whose dependency inputs are directly confirmed identical.
On failure, remove only an incomplete worktree environment, restore the prior
lock and verified `.venv` link state, preserve resumable state, and ask before
network access. Retain isolated environments across rounds and suspension;
validate them on resume and remove them with their owning worktrees.

Before each active round, refresh the unit and confirm its recorded claim. For
an existing PR implementation round, remove `ready-to-merge`, apply PR
`in-progress`, and preserve `partial` while accepted scope remains incomplete.
Do not mutate labels or draft state before a verification-only round.

Every existing-PR assignment records `initial_draft`, `pr_round_mode`,
`pr_expected_end_state`, and `required_worker_draft`. Use `implementation`,
`draft`, and `true` for an editing round; explicitly keep an existing draft or
change a ready PR to draft before editing because only the supervisor may
restore ready status. An inherited draft always uses implementation mode so it
can complete normal validation and supervisor promotion, even when no edit is
expected. Use `verification-only`, `unchanged`, and `false` only for an
inherited ready PR undergoing non-mutating review; a proven gap returns
`CORRECTION_NEEDED` and requires a new implementation assignment. Never say to
keep the PR in its "current state." If the user's instruction forbids a required
draft transition, stop for direction instead of launching a contradictory
round.

## Stage 4: run bounded implementation rounds

Build the round's validation plan from inspected evidence before registering
the assignment. Copy repository-owned validation commands exactly; do not add
files to their argument lists. Derive any file-specific check from the file's
actual shebang, language configuration, and syntax rather than its extension or
name. Run a cheap supervisor-added syntax or static check against the recorded
pre-edit SHA before making it a completion gate. A check that already fails at
that SHA is an informational baseline limitation, not required worker
validation. Do not invent broad test, formatter, interpreter, or compiler
commands when the repository does not establish them.
For verification-only, include only commands proven not to rewrite the
worktree; never assign pre-commit or another auto-fixing command. If adequate
validation requires a potentially mutating command, use implementation mode.

Before launching any round that must push, perform a non-mutating,
non-interactive authenticated push preflight for the exact remote, refspec, and
lease, using `GIT_TERMINAL_PROMPT=0` and `git push --dry-run --no-verify`. Do not
inspect or inject tokens. Distinguish authentication failure from a ref/lease
rejection; if no sanctioned push mechanism is available, stop before assigning
implementation rather than letting the worker discover it after editing.

Put `execution_environment` in every assignment. It contains `mode: native`,
`mode: isolated`, or `mode: shared` plus `pythonpath`, an ordered list of
existing project-relative source roots. Do not put absolute paths, lock
contents, or environment details the worker can derive from the mode in the
assignment. Record mode, lock ownership, and selected sync groups in the
supervisor ledger. Every worker Python command sets `UV_NO_SYNC=1` so child uv
processes inherit it; direct uv commands use `uv run` without `--no-sync`.
In shared mode, the worker expands each assigned project-relative root against
the verified assigned worktree and sets the resulting absolute `PYTHONPATH`;
isolated mode has no `PYTHONPATH` override.

Register the complete round assignment with
`mcp__github_workflows__task_manage` using action `plan` and a typed `task`.
If client-side validation rejects `task` with an object/null `anyOf` error
before the MCP call starts, do not infer a payload-size limit or repeat the
identical call. Rebuild `task` once as a compact native structured object,
checking nested object and array boundaries while preserving the assignment
contract. If that corrected retry also fails, report the client validation
limitation and stop rather than progressively splitting the assignment.
Use its returned server-generated task ID and task reference, then launch
`gh-implement-issue-worker` with fresh context and:

```text
Task ref: <task-ref-returned-by-task-manage>
```

The spawn message contains exactly that line. Do not add the assignment, task ID,
or instructions to call supervisor-only workflow tools. Workers return their
recoverable checkpoint in the final report; the supervisor alone persists that
report through `task_manage` action `checkpoint` or `complete`.

Maintain a ledger containing unit, semantic task ID, task reference, anchor,
issues, grouping rationale,
branch/worktree, PR, round, scope sources, original/base/current SHAs, claim
ownership, per-issue coverage, checkpoint, and finalization state. One worker
owns a unit at a time.

Each round has 128 turns: 120 working turns and eight reserved checkpoint
turns. Workers return `DRAFT_READY_FOR_SUPERVISOR`, `CONTINUE_REQUESTED`,
`SPLIT_REQUESTED`, `CORRECTION_NEEDED`, `BLOCKED`, `MCP_UNAVAILABLE`, or
`NO_IMPLEMENTATION`. `MCP_UNAVAILABLE` suspends the complete workflow before
any further unit work.
Review every checkpoint against every issue's accepted scope, unit cohesion,
the complete base diff, validation, research semantics, and confidential-data
safety.

The worker performs a cohesion preflight before edits. For a
`SPLIT_REQUESTED` checkpoint with no changes, independently verify the proposed
partition, release no claims, create separate unit ledgers/worktrees, and then
launch one worker per new unit within the concurrency limit. If the current
diff already represents some issues, retain those issues and that diff in the
current unit and split only untouched issues. Never copy or share an edited
worktree between units. When changes entangle the proposed partitions, send a
correction that restores one cohesive unit or escalate to the user when a safe
partition requires a product, scientific, or scope decision.

Continue the same task with one bounded objective, return an exact correction,
advance to draft verification, or terminate and finalize. Two rounds blocked by the
same explained cause trigger termination or user escalation.

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
the PR to the authenticated user when the MCP surface supports it. Read its
labels through the issue-label API, reconcile the shared derived PR taxonomy,
and apply PR `in-progress` during active supervisor verification. Confirm the body follows
`../../references/github-pr-template.md`, begins with
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

Qwen leaves `ready-to-merge` to Codex `$gh-pickup-work` or
`$gh-reassess-work`. Assignments remain responsibility metadata after a
successful publication; release workflow-added assignment for abandoned or
no-implementation units.

Use `NO_IMPLEMENTATION` only when no issue in the unit requires a code change.
When it applies to only part of a multi-issue unit, process it as a
`SPLIT_REQUESTED` per-issue disposition and continue the remaining cohesive
issues. Verify the worker's evidence and optionally publish one concise comment
on each affected issue before finalization. Issue state and terminal labels
remain curator/maintainer responsibilities.

A unit is finalized only after issue and PR reads confirm the intended status
and derived taxonomy label state. If authentication, authorization, interruption, or a conflicting actor
prevents cleanup, report each retained label and URL prominently as manual
repair state.

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
