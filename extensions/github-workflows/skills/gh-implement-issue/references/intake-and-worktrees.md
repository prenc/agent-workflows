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
clarifications. Overlay any explicit premise or accepted-scope correction
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
result before continuing or stopping. Do not ask during execution: record the
blocked disposition, release unsuitable early claims, and continue independent
units. Update the early ledger with the disposition and cleanup result.

Require each resulting unit to fit one coherent eventual PR. Route an existing
issue whose own accepted scope needs independently mergeable PRs to
`/gh-curate-issues` for splitting.

## Stage 3: prepare or reuse the worktree

Use:

```text
worktree root:    <project>/.worktrees
single new issue: <root>/issue-<N>-<slug>
new issue bundle: <root>/issues-<anchor>-<slug>
existing PR:      <root>/issue-<anchor>-pr-<P>-continue
```

Use `git check-ignore --no-index` to confirm `.worktrees/` is ignored. When
needed, resolve the common Git directory and its repository-private
`.git/info/exclude` equivalent, reject symlinked or foreign-owned `info` or
`info/exclude`, append the ASCII rule without decoding existing
bytes, and verify it before creating the root. Inspect registered worktrees, branch ownership, Git
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
First build the complete repository-owned validation plan so environment
selection accounts for every executable it will invoke.
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
documented groups or extras, including the repository's documented development
group when the validation plan needs one of its tools. Never run either command through a `.venv`
symlink. The supervisor alone runs these environment writers.

After synchronization, verify every environment-owned executable in the
validation plan under the assigned worktree environment. In particular, do not
assign `<worktree>/.venv/bin/pre-commit` unless that exact executable exists.
If an approved documented group cannot be populated offline or a required
executable remains absent, block the unit before worker launch. Never substitute
the main checkout's `.venv` for validation assigned to an isolated environment.

Respect the repository's lock convention: preserve a tracked lock, keep an
ignored lock local, or retain an otherwise unwanted generated lock in private
cache beside the managed worktree root and remove its temporary worktree copy
after synchronization. Make the cache directory `0700`; reject symlinks and
foreign ownership. Do not
update a stale tracked lock without scope or user authority. Reuse one lock
only for worktrees whose dependency inputs are directly confirmed identical.
On failure, remove only an incomplete worktree environment, restore the prior
lock and verified `.venv` link state, and preserve resumable state. Network
access must have been authorized during interactive preflight; otherwise block
the affected unit without asking during execution. Retain isolated environments across rounds and suspension;
validate them on resume and remove them with their owning worktrees.

Before each active round, refresh the unit and confirm its recorded claim. For
an existing PR implementation round, remove `ready-to-merge`, apply PR
`in-progress`, and preserve `partial` while accepted scope remains incomplete.
Do not mutate labels or draft state before a verification-only round.

Every existing-PR assignment records `pull_request.initial_draft`,
`pull_request.pr_round_mode`, `pull_request.pr_expected_end_state`, and
`pull_request.required_worker_draft`. Use `implementation`,
`draft`, and `true` for an editing round; explicitly keep an existing draft or
change a ready PR to draft before editing because only the supervisor may
restore ready status. An inherited draft always uses implementation mode so it
can complete normal validation and supervisor promotion, even when no edit is
expected. Use `verification-only`, `unchanged`, and `false` only for an
inherited ready PR undergoing non-mutating review; a proven gap returns
`CORRECTION_NEEDED` and requires a new implementation assignment. Never say to
keep the PR in its "current state." If the user's instruction forbids a required
draft transition, do not launch a contradictory round. Record the affected unit
as blocked, continue independent units, and suspend only when no safe
independent progress remains.
