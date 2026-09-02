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

Infer one operation from the user's request:

- **record** requires an explicit request to report an observed friction;
- **analyze** is the default and is read-only;
- **implement** requires `fix`, `implement`, `address`, or equally explicit
  authorization;
- **resolve** changes feedback status only when explicitly requested or after
  an authorized implementation has been validated.

An invocation never authorizes commits, pushes, installation into user config,
MCP restart, extension reload, or permanent feedback deletion. Perform those
only when separately requested.

## Record feedback

Use `mcp__github_workflows__workflow_feedback`. General or instruction friction
needs only `message`; a named worker also supplies its exact `task_ref`. When a
failed github-workflows call offers `error_ref`, pass it instead of repeating
the payload. Use `tool` only for a Qwen-native or external tool, or a confusing
successful call the server could not observe. Never combine `error_ref` and
`tool`.

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

Preserve the current worktree and begin with compact views:

```sh
agent-workflows feedback stats
agent-workflows feedback sources
agent-workflows feedback ls
```

Apply any requested feedback ID or suffix, source, repository, or workflow
filter. Use `feedback show <id>` for each selected record. Do not load all
closed feedback merely for context and do not change status during default
analysis.

Verify each report against current source, tests, documented interfaces, and
installed-versus-source state. Classify it as locally actionable, duplicate,
external, not actionable, already addressed, or unresolved. Group records only
when they share a demonstrated root cause and required correction. Report each
group's IDs, evidence, confidence, consequence, proposed change, and expected
disposition. A zero-item queue is a successful no-op.

If the sanitized record is insufficient, explain the missing evidence.
Ask the user before calling `feedback trace` or opening any Qwen transcript. After
permission, inspect only rows tied to the exact feedback and origin call IDs.
Never scan, reproduce, or summarize the complete conversation.

## Implement one coherent group

For an authorized implementation, choose exactly one coherent root-cause group.
Preserve unrelated changes. Reproduce the behavior or establish direct proof,
then inspect callers, schemas, error paths, agent clients, resumability,
confidentiality, context-size impact, and installed-versus-source behavior.
Make the smallest complete correction and add behavior-focused regression
coverage. Do not encode legacy absence assertions or exact prose unless it is a
consumed compatibility contract.

Run focused tests, the lightweight owning test group, and the repository's
required pre-commit checks. Review the complete diff. If validation is partial
or the cause remains uncertain, leave the records open and report the remaining
evidence gap.

## Resolve without deleting

After validation, close every proven record in the group with one concise note:

- `addressed` for a validated local correction;
- `duplicate` when another identified record covers the same proven cause;
- `external` for verified upstream behavior after appropriate local mitigation;
- `not-actionable` for a false positive, ordinary caller mistake, stale
  observation, or unsupported premise.

Use `agent-workflows feedback close`; never use `feedback remove` as routine
cleanup. Leave partial or ambiguous records open. Reopen a record when later
review invalidates its resolution, then repair and revalidate it.

Finish by reporting validation, records closed or retained, and whether the
change needs `agent-workflows install`, an extension reload, a new Codex
session, or an MCP process restart. Do not perform those actions without the
user's explicit request.
