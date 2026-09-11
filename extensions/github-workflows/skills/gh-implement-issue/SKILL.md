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
requests. Read [GitHub access](references/github-access.md),
[issue conventions](references/github-issue-conventions.md), the
[PR template](references/github-pr-template.md), the
[runtime policy](references/github-runtime-policy.md), and repository
instructions before changing state. The
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

Complete one interactive preflight before starting or resuming material work.
Resolve the invocation, every currently discoverable grouping, scope,
dependency, network, scientific, security, or data-authority question, and the
active approval mode there. Resolve any conflict between the requested schema
change and a repository instruction that a file “must remain unmodified”; the
worker treats that phrase as a literal prohibition and never guesses an
exception. Durable execution requires the parent Qwen session
to be in YOLO mode; Plan mode may be used to discuss the preflight, but do not
call `run_manage` `start` or `resume`, claim issues, prepare worktrees, or launch
workers from Plan, default, auto-edit, or auto mode. Refresh target and claim
state after any user delay and before the first mutation. The successful start
or resume closes the question window; apply conservative runtime-policy
defaults rather than asking during execution.

When an audit `--implement` handoff supplies the targets, its preflight and
authorization carry into this workflow. Do not repeat the interactive
preflight. Skip targets or units that cannot proceed under existing authority
or conservative defaults, clean up workflow-owned claims, and continue the
independent work.

## Operating model

Follow the GitHub access policy. `get_me`, or the fallback's
authenticated identity, establishes assignment and branch ownership. Qwen's MCP
status display is informational.

Workers receive a separate server-enforced GitHub MCP connection for targeted
verification and the narrow creation/update of their assigned draft PR. They
must establish the required read and draft-PR tools before analysis or edits.
Workers use their own authenticated `gh` fallback without changing ownership;
snapshots are context, not proof. The supervisor remains authoritative
for claims, scheduling, issue/label state, final live verification, and every
draft-to-ready transition.

Use the supervisor-selected environment, `uv`, and lightweight login-node
checks. Keep secrets, unrelated changes, CI/check APIs, merges, reviews, and
reassessment outside this workflow. Use repository data only when relevant to
the accepted scope, and never publish large or raw datasets. Validate issue
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

## Workflow stages

Read [intake and worktrees](references/intake-and-worktrees.md) before stages
1-3. Read [implementation rounds](references/implementation-rounds.md) before
stage 4. Read
[promotion and finalization](references/promotion-and-finalization.md) before
stages 5-7 and the final report.
