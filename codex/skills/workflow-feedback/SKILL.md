---
name: workflow-feedback
description: >-
  Record, analyze, group, implement, and resolve agent-workflows feedback.
  Use when asked to report workflow friction, inspect the feedback queue,
  investigate feedback by ID or source, plan improvements, address a coherent
  feedback group, or close reviewed feedback.
metadata:
  short-description: Maintain agent-workflows feedback
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

Use:

```sh
agent-workflows feedback add "<concise PHI-free observation>" [--tool TOOL_NAME]
```

Record distinct friction caused or obscured by a workflow, tool API, active
instruction, or agent interface: missing capabilities, confusing schemas,
misleading errors, forced workarounds, repeated no-progress retries, or
avoidable context growth. State observed behavior and consequence separately
from any hypothesis.

Do not report ordinary caller mistakes, repository defects, progress,
findings, unavailable dependencies, or transient external failures unless the
workflow made them confusing or unnecessarily costly. Do not include prompts,
conversation text, tool payloads, issue bodies, source-data excerpts, secrets,
PHI, or PII. Use `--tool` only when naming the related native or external tool
helps identify the interface.

## Analyze the queue

Preserve the current worktree and make one read call that matches the request.
Use `feedback summary --json` for an aggregate overview. When record-level
analysis is required, skip that preliminary call and use `feedback ls --all --json` directly (or `--limit 1` for only the newest record). From an
`agent-workflows` checkout use its documented no-sync invocation; elsewhere use
the installed executable, for example:

```sh
uv run --no-sync agent-workflows feedback summary --json
agent-workflows feedback summary --json
```

Apply requested source, repository, workflow, status, and cutoff filters. If
both views are genuinely needed, carry the same `--cutoff` on summary and list
so their scopes agree. The cutoff is an inclusive lower bound on record
creation time. Reuse the resulting records
throughout the contiguous task; refresh only when scope changes, the store may
have changed, or resolution reports a conflict. Use one batched
`feedback show <ref>...` call only when direct ID lookup is needed. Do not load
closed feedback without a reason or change status during default analysis. Do
not set a custom uv cache or synchronize the environment.

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
`agent-workflows feedback close --input <JSON|file|->` request. The request
contains a `resolutions` array whose entries have `ref`, `disposition`, and an
optional `note`. Use positional `feedback close` for a simple group sharing one
disposition and note; never use `feedback remove` as routine cleanup. Leave
partial or ambiguous records open. Reopen a record when later review invalidates
its resolution, then repair and revalidate it.

Finish by reporting validation, records closed or retained, and whether the
change needs `agent-workflows install`, an extension reload, a new Codex
session, or an MCP process restart. Do not perform those actions without the
user's explicit request.
