# Prepare and Implement GitHub Work

Use these stages only in default implementation mode after the common
assessment gate accepts the selected work.

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
clarifications. Then overlay any explicit premise or accepted-scope correction
from the authenticated user's unique managed reassessment comment
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

For one existing PR that is already ready for review, attempt a read-only
verification fast path before claiming anything. It qualifies only when the PR
head already contains the exact latest base, its body, linkage, draft state,
and complete taxonomy already match the desired state, every accepted outcome
is complete, and proportionate non-mutating validation against the immutable
head passes. Use a detached or demonstrably matching clean worktree when local
inspection is required. Any needed rebase, edit, push, body correction, draft
transition, or taxonomy correction exits the fast path and proceeds through
the normal claim and implementation flow.

Immediately before completing the fast path, refresh every issue, the PR, base,
relationships, labels, and head SHA. Stop without mutation on any drift or new
`in-progress`. If `ready-to-merge` is already present, the pickup is a complete
GitHub no-op. Otherwise transactionally claim every issue and the PR with
`in-progress`, confirm the claims, and revalidate all non-lifecycle snapshot
facts while holding them. Roll back every run-owned claim on drift or failure.
After successful revalidation, replace PR `in-progress` with
`ready-to-merge` in one complete-label update, read it back, then release and
verify every issue claim. This finalization claim closes the race without
rewriting the PR body, toggling draft state, pushing, or publishing a comment.

## 4. Claim and prepare the workspace

Skip this section only for the completed read-only fast path. Otherwise refresh every issue immediately before claiming. If any issue now has
`in-progress`, stop without claiming the others. Otherwise create the exact
canonical label only if missing, report definition drift without repairing it,
and apply `in-progress` sequentially to every issue. Record each mutation.
Proceed only after all claims read back correctly. If any claim fails, release
every claim added by this run, verify rollback, and stop.

For an existing PR, remove `ready-to-merge` before applying PR `in-progress`.
Keep the issue-level atomic claim authoritative over PR status.

Use these canonical worktree paths when no safe reusable worktree exists:

```text
worktree root:       <project>/.worktrees
single new issue:     <root>/issue-<N>-<slug>
new issue bundle:    <root>/issues-<anchor>-<shared-outcome-slug>
single existing PR:  <root>/issue-<N>-pr-<P>-continue
bundle existing PR:  <root>/issues-<anchor>-pr-<P>-continue
single branch no PR: <root>/issue-<N>-continue
bundle branch no PR: <root>/issues-<anchor>-continue
```

Use `issue-<N>-<slug>` as a new single-issue branch and
`issues-<anchor>-<shared-outcome-slug>` as a new bundle branch. Slugs describe
the shared implementation outcome, for example
`issues-14-remove-obsolete-parsers`, rather than merely repeating a label.
Confirm the exact local and remote branch name is unused. An existing PR keeps
its current head branch name.

Use `git check-ignore --no-index` to confirm `.worktrees/` is ignored. If it is
not, resolve the common Git directory with
`git rev-parse --path-format=absolute --git-common-dir`, reject a symlinked or
foreign-owned `info/exclude`, append `.worktrees/` once, and confirm the rule with
`git check-ignore --no-index` before creating `<project>/.worktrees`. Store this
local rule only in `.git/info/exclude`.
Inspect `git worktree list --porcelain`, branch
state, Git-operation state, and local changes. Reuse a worktree only when it
demonstrably belongs to the selected repository, unit, and branch. Never reset,
clean, stash, overwrite, or repurpose unrelated state.

Choose and record `native`, `shared`, or `isolated` environment mode before
implementation. Use `native` for non-Python projects. Use `shared` only for
source-only Python work with unchanged dependency, packaging, entry-point,
compiled-extension, and import-layout inputs and unambiguous project-relative
source roots. Link `.venv` to the verified main project environment; never
overwrite an unexpected path or follow its target. Keep source roots
project-relative in records, but expand them against the verified assigned
worktree and set the resulting absolute `PYTHONPATH` on every implementation
and validation command. Invoke the linked `.venv` interpreter and tools
directly. Prefix `UV_NO_SYNC=1` only for a documented wrapper known to invoke
nested uv, and never run an environment writer in shared mode.

Otherwise stop concurrent work and use `isolated`. Classify `uv.lock` first and
non-mutatingly run `uv lock --check --offline --no-python-downloads` for a
tracked lock. Stop on staleness unless updating it is authorized; only then may
mutating `uv lock` run. Inspect `.venv` without following it, require any
symlink to resolve exactly to the verified main environment, and unlink only
that symlink before creating the local environment. Block on any other existing
target, and never run `uv venv` or `uv sync` through a symlink. Populate the
environment with a frozen offline sync and documented groups or extras. Respect
tracked, ignored, and intentionally omitted lockfile conventions; keep an unwanted
generated lock in a private `0700` cache beside the managed worktree root rather
than adding it to the change; reject symlinks and foreign ownership.
Reuse a lock only across directly identical dependency inputs. Restore the
prior lock and verified link state after a failed provision, and ask before
network access. After provisioning, invoke the worktree-local `.venv`
interpreter and tools directly and do not set `PYTHONPATH`. Prefix
`UV_NO_SYNC=1` only for a documented wrapper known to invoke nested uv. Refresh
the lock and environment yourself before validation whenever dependency inputs
change.

Fetch the exact latest base and selected remote head. Record the remote head
SHA as the future lease. Create scratch branches from the exact latest base.

## 5. Rebase and verify inherited work

Rebase the prepared branch onto the exact latest base SHA before analyzing or
editing it:

```text
git rebase <latest-base-sha>
```

When GitHub or local history indicates merge conflicts, the rebase is the first
mutation to the implementation branch. Do not make a code, test, PR-body, or
metadata correction before resolving the latest-base rebase. Reassess the
complete rebased result before deciding what further changes are necessary.

Use `--autostash` only for verified unit-scoped uncommitted changes in a reused
worktree; never create a manual stash. Stop when ownership of local changes or
history is ambiguous.

Before editing work that must be pushed, perform a non-mutating,
non-interactive authenticated push preflight for the exact remote, refspec, and
lease, using `GIT_TERMINAL_PROMPT=0` and `git push --dry-run --no-verify`. Do not
inspect or inject tokens. Distinguish authentication failure from a ref/lease
rejection and stop before implementation when no sanctioned push mechanism is
available.

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
discarding it. If new evidence requires a material reframe, return to the assessment outcome, publish only its managed evidence, and stop before further implementation.

## 6. Implement and validate

Implement the smallest cohesive change satisfying every selected issue. Track
coverage per issue throughout the work. Add focused tests where practical. Run
the fastest relevant checks under the recorded environment, invoking the
assigned `.venv` interpreter and tools directly. Prefix `UV_NO_SYNC=1` only for
a documented wrapper known to invoke nested uv. Shared mode also sets the
absolute `PYTHONPATH` derived from the assigned worktree and recorded relative
roots; isolated mode relies on its worktree-local editable install.
Run the repository's pre-commit command under the same environment when the
project uses pre-commit.

Review the complete diff from latest base for scope, secrets/data exposure,
unrelated files, and accidental research-semantic changes. Commit focused
changes without merge commits.
