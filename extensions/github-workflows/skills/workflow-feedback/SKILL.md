---
name: workflow-feedback
description: Record, analyze, group, implement, and resolve agent-workflows feedback. Use when asked to report workflow friction, inspect the feedback queue, investigate feedback by ID or source, plan improvements, address a coherent feedback group, or close reviewed feedback.
priority: 20
argument-hint: '[feedback-id, source, or instructions]'
allowedTools:
  - run_shell_command
  - grep_search
  - read_file
  - write_file
  - mcp__github_workflows__workflow_feedback
---

# Workflow Feedback

Maintain the private `agent-workflows` feedback queue without turning ordinary
task failures into process work. Record and analyze feedback from the active
project so repository attribution remains accurate. Only implementation work
requires a writable `agent-workflows` checkout; if it is unavailable, report
the required location instead of modifying another project.

## Select the operation

Infer one primary operation from the user's request:

- **record** as the primary operation requires an explicit request to report an
  observed friction;
- **analyze** is the default and does not change existing queue records; the
  secondary-record exception below may append a new record;
- **implement** requires `fix`, `implement`, `address`, or equally explicit
  authorization;
- **resolve** changes feedback status only when explicitly requested or after
  an authorized implementation has been validated.

While analyzing, implementing, or resolving feedback, also record any new,
qualifying workflow friction encountered during that work. This secondary
record needs no separate user request, but it does not expand the approved
implementation scope. Confirm the behavior, sanitize and record it once, then
continue the primary operation. Do not turn expected validation failures,
ordinary repository defects, or the feedback tool's own failed recording
attempt into recursive feedback; report a recording failure to the user and do
not loop.

An invocation never authorizes commits, pushes, installation into user config,
MCP restart, extension reload, or permanent feedback deletion. Perform those
only when separately requested.

## Record feedback

Use `mcp__github_workflows__workflow_feedback`. General or instruction friction
needs only `message`; a named worker also supplies its exact `task_ref`. Qwen
attaches the conversation and tool-call locator automatically. Use `tool` only
for a Qwen-native or external tool, or a confusing successful call the server
could not identify from nearby transcript context.

Record distinct friction caused or obscured by a workflow, tool API, active
instruction, or agent interface: missing capabilities, confusing schemas,
misleading errors, forced workarounds, repeated no-progress retries, or
avoidable context growth. State observed behavior and consequence separately
from any hypothesis.

Do not report ordinary caller mistakes, repository defects, progress,
findings, unavailable dependencies, or transient external failures unless the
workflow made them confusing or unnecessarily costly. Do not include prompts,
conversation text, tool payloads, issue bodies, source-data excerpts, secrets,
PHI, or PII.

## Analyze the queue

Preserve the current worktree and make one read call that matches the request.
Use `agent-feedback summary` for an aggregate overview. When
record-level analysis is required, skip that preliminary call and use
`agent-feedback ls --all`, then fetch needed full records in one batched
`agent-feedback show <ref>...` call (or use
`--limit 1` for only the newest record). The agent interface always emits JSON; do not add
`--json` or select a Python environment. If the command is unavailable, report
that `agent-workflows install` must be run instead of falling back to `.venv`,
`uv`, or a Python path. For example:

```sh
agent-feedback summary
agent-feedback ls --all --since 30d
```

Apply requested source, repository, workflow, status, and cutoff filters. If
both views are genuinely needed, carry the same `--since` age on summary and
list so their scopes agree. Use compact ages such as `24h`, `30d`, or `4w`.
Reuse the resulting records throughout the contiguous task; refresh only when
scope changes, the store may have changed, or resolution reports a conflict.
Use one batched `agent-feedback show <ref>...` call only when
direct ID lookup is needed. Do not load
closed feedback without a reason or change status during default analysis. Do
not set a custom uv cache or synchronize the environment.

Verify each report against current source, tests, documented interfaces, and
installed-versus-source state. Classify it as locally actionable, duplicate,
external, not actionable, already addressed, or unresolved. Group records only
when they share a demonstrated root cause and required correction. Report each
group's IDs, evidence, confidence, consequence, proposed change, and expected
disposition. A zero-item queue is a successful no-op.

If the sanitized record is insufficient, run `agent-feedback trace <ref>`,
which defaults to the three preceding tool interactions without payloads. If
that is insufficient, retry with `--detail context` to include bounded visible
user and assistant text. Never expose hidden reasoning. Ask the user before
using `--detail data`; it adds bounded, sanitized payloads for those same tool
calls. Inspect only the returned window and never scan, reproduce, or summarize
the complete conversation.

## Implement authorized groups

For an authorized implementation, address every explicitly approved,
non-conflicting root-cause group in dependency order. Do not expand beyond the
approved groups merely to save calls. Preserve unrelated changes. For each
group, reproduce the behavior or establish direct proof, then inspect callers,
schemas, error paths, agent clients, resumability, confidentiality, context-size
impact, and installed-versus-source behavior. Make the smallest complete
correction and add behavior-focused regression coverage. Do not encode legacy
absence assertions or exact prose unless it is a consumed compatibility
contract.

Run focused validation per group, then consolidate overlapping owning tests and
repository-wide checks into one final pass while tracked files remain unchanged.
Review the complete diff. If one group remains uncertain or fails validation,
leave only that group's records open and continue with independent approved
groups.

## Resolve without deleting

After validation, close every proven record in the group with one concise note:

- `addressed` for a validated local correction;
- `duplicate` when another identified record covers the same proven cause;
- `external` for verified upstream behavior after appropriate local mitigation;
- `not-actionable` for a false positive, ordinary caller mistake, stale
  observation, or unsupported premise.

Apply mixed validated dispositions in one atomic
`agent-feedback close --input <JSON|file|->` request. The request
is a JSON array whose entries have `ref`, `disposition`, and an optional
`note`; do not wrap it in a `resolutions` object. Prefer `--input -` with stdin for generated JSON so it cannot
be mistaken for a file path. Use positional `agent-feedback close`
for a simple group sharing one disposition and note. The agent interface does
not expose permanent removal; leave
partial or ambiguous records open. Reopen a record when later review invalidates
its resolution, then repair and revalidate it.

Finish by reporting validation, records closed or retained, and whether the
change needs `agent-workflows install`, an extension reload, a new Codex
session, or an MCP process restart. Do not perform those actions without the
user's explicit request.
