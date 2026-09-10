# GitHub access

Prefer GitHub MCP as the primary interface. If it is unavailable, each GitHub-aware
agent uses its authenticated `gh` session for the same authorized operations;
local `git` remains the repository and transport interface. Never inspect,
print, copy, or inject tokens.

## Access gate

Use the first required MCP read as the gate. Missing tools, connection or
protocol failure, repeated server errors, and authentication or authorization
errors make MCP unavailable. A connected status badge is not proof that a tool
works. Continue through the authenticated `git`/`gh` fallback.

A worker with a declared read-only surface receives only that surface. Codex
workers may see more tools but remain authorized for reads only. Every worker
uses the fallback within its existing ownership and mutation boundary. Cached
records and supervisor snapshots provide context, not live proof.

For an issue or PR error, inspect the repository's `has_issues` or
`has_pull_requests` flag once. A disabled feature is a repository-setting
blocker. Otherwise retain the original error classification.

Preserve workflow authorization, dry-run, mutation, and verification limits
through the fallback. Suspend only when neither route can complete a required
operation. Record both failures and follow the runtime policy's checkpoint and
resume procedure.
