# GitHub MCP suspension and resume policy

This policy applies to the supervisor and every GitHub-aware worker. Each such
worker must establish its own GitHub MCP read capability before analysis or
implementation. Supervisor snapshots and cached records provide context, while
the worker's live MCP access remains the required evidence source.

## Availability gate

Use the first required GitHub MCP read as the availability gate in place of a
separate authentication preflight. A successful required operation establishes
availability for that point in the run. Treat any of the following as an MCP
availability failure:

- the configured GitHub MCP tools are absent from the session;
- a worker definition that explicitly requires a server-enforced read-only MCP
  surface receives mutation-capable tools, violating that declared contract;
- the server is offline, unreachable, or fails protocol initialization;
- a required MCP call cannot be dispatched or repeatedly returns a transport or
  server-availability error;
- the server reports authentication or authorization failure.

A connected status badge is not evidence that tools are declared or callable.
When tools are absent from a restored session, do not spend turns searching for
or re-registering them in-session and do not spawn workers. Checkpoint and end
the run under this policy, reporting that a new session or corrected client tool
registration is required.

Tool exposure alone is not a failure for a runtime such as Codex where
subagents inherit the parent session's complete MCP schema. In that runtime,
`read-only worker` constrains authorized tool use: the worker calls only GitHub
read operations even when mutation tools are visible. Its availability gate is
a successful required read. The stricter exposed-tool check applies only to a
worker definition that explicitly configures and requires a server-enforced
read-only MCP surface.

## Repository feature diagnostic on errors

When any GitHub MCP issue or pull-request operation errors, perform one
repository-feature diagnostic for that repository and error episode before
classifying the failure or retrying:

- query the exact repository with `search_repositories` and
  `minimal_output: false`;
- inspect `has_issues` for an issue-operation error and `has_pull_requests` for
  a pull-request-operation error;
- when a worker reports the error, the supervisor performs this diagnostic,
  keeping repository-discovery permissions centralized.

If the relevant flag is `false`, treat the failure as a repository-settings
blocker rather than an MCP authentication or availability failure. Stop the
affected workflow before retries, preserve its recoverable state, and report
which repository feature must be enabled. Repository setting changes remain
user-owned. After the user confirms the feature is enabled, refresh repository
metadata and retry the original operation once.

If the flag is `true`, or repository metadata cannot be read, retain the
original error classification and continue with the applicable workflow or
suspension rule. Record a failed metadata diagnostic alongside the original
error. Run this query once per identical-error episode, then run it again only
after a reported settings change or at the start of a new resume attempt.

An availability failure in the supervisor or any worker suspends the whole
workflow. Ask active workers to checkpoint immediately and stop; perform no
further analysis, source edits, tests, GitHub reads or writes, worker launches,
commits, or pushes. Preserve completed local work and do not use
`gh`, REST, GraphQL, another connector, or cached records as an availability
fallback. A separately documented `gh api` capability-gap fallback remains
usable only while GitHub MCP itself is available and authenticated.

## Prepare a resumable suspension

Checkpoint through the workflow's existing run state when it has one.
Otherwise create a user-private resume capsule in a temporary workflow
directory and report its absolute path. Record:

- workflow name, original invocation, repository, targets, options, and dry-run
  state;
- completed stages and the last successful MCP operation with its timestamp;
- GitHub object identifiers and immutable SHAs already verified;
- every confirmed mutation, every pending mutation, and every operation whose
  outcome is ambiguous;
- known `in-progress`, `partial`, and `ready-to-merge` state, including claims
  created by this run that could not be reconciled;
- branch, worktree, local HEAD, pushed SHA, dirty state, worker checkpoints, and
  validation already completed when applicable;
- the worker whose availability gate failed and the exact missing tool, MCP
  error, or declared server-enforced read-only contract violation;
- the earliest safe resume step and the evidence that must be refreshed first.

Keep worktrees, branches, ledgers, cache staging, drafts, and worker reports
needed for continuation. Report the suspension, failure, retained external
state, resume capsule or run ID, and the exact invocation the user can repeat.

## Resume

On a resumed request, load the matching ledger or reported capsule and perform
the required GitHub MCP refresh identified there. Continue only after that MCP
operation succeeds. Reconcile live issue, PR, label, relationship, assignment,
and immutable-SHA state before new work. Preserve confirmed idempotent progress,
repeat stale reads, and resolve ambiguous mutation outcomes by reading current
state before deciding whether a write remains pending.
