# Global Agent Instructions

## Project Tools and Python

Determine the language and commands from repository instructions and root
configuration; a `.venv` alone does not make a project Python. Use the native
toolchain for non-Python projects.

For Python projects, use `uv` for dependency management and the project-root
`.venv`. Do not create another environment unless asked. If a dependency is
missing, report it and suggest the appropriate repository or `uv` command.

With a root `.venv` and `uv`, run Python commands and project executables via
`uv run --no-sync`. Prefer documented repository wrappers; set `UV_NO_SYNC=1`
for wrappers such as Make that may invoke uv. Never combine that environment
variable with `uv run --no-sync`.

If either `.venv` or `uv` is unavailable, follow the repository instructions
and use the available project or system command without creating an
environment solely to run it.

## Documentation Audience

Keep READMEs and user documentation focused on purpose, installation, public
interfaces, examples, and operation. Put agent-only development policy in the
applicable `AGENTS.md`; keep specialized procedures in their skills instead of
duplicating them.

## Validation

Run the fastest coherent checks during implementation: focused tests before
broader validation, with concise output unless diagnosing. Repository-standard
tests, formatters, linters, and hooks are authorized. Report unavailable tools
or skipped coverage; do not install dependencies silently or claim unrun checks.

After implementation and affected tests, run configured pre-commit hooks once
across all files. Inspect hook changes, rerun affected tests when warranted,
and finish with a passing run. Use the hooks for Ruff instead of invoking it
directly.

If an asynchronous or process-based test appears to stall only in the sandbox,
rerun that same focused test outside the sandbox before treating the stall as a
product failure.

## Test Design

Organize tests by product or subsystem responsibility. Put regressions with the
owning group; use isolated files only for genuinely narrow checks, then run the
broader group. For pytest, default to `-q` or `--tb=line`.

Use doctests sparingly for small, public, deterministic examples. Keep error
cases, edge cases, regressions, parametrized behavior, orchestration, and
integration coverage in the project's normal test framework.

Test consumed behavior, schemas, validation, state transitions, and rendered
structure. Assert exact prose or defaults only when they are compatibility
contracts, and avoid duplicate protection.

## Parallel Requests and Progress

For multiple parallel Python requests, use one `tqdm` progress bar on the
shared queue. Route concurrent logging through `tqdm.write` or
`logging_redirect_tqdm` so progress output remains readable.

## Agent Design

When implementing an agent, keep durable task policy, constraints, sequencing,
and output contracts in its system prompt. User messages carry the current
task, evidence, and server-owned state. Tool descriptions and argument schemas
define mechanical behavior, input shape, validation, and return shape. Keep
main-agent, nested-agent, prompt, and tool responsibilities distinct.

Use the provider's native chat template and preserve structured message
history. Every assistant tool call must precede its matching tool result, and a
workflow may finish only after every tool call has a result. Represent results
as tool-role messages tied to their call IDs; keep them out of ordinary
assistant prose and do not manually construct provider-specific wrappers.

When compacting history, preserve assistant messages, tool names, call IDs, and
arguments exactly. Mask only explicitly configured fields in copied tool-result
payloads. Preserve the pending result, the latest accepted state change, and
the latest relevant rejection or no-op. Keep original trace and audit records
unchanged.

## GitHub Interaction

Use the configured GitHub MCP server for GitHub operations and local `git` for
repository operations and Git transport. Never inspect, print, copy, or
persist `GH_TOKEN`. If the MCP server reports an authentication or
authorization failure, stop GitHub work rather than bypassing it through
another client. Follow the active workflow or skill for narrower GitHub rules.

Never query, inspect, or poll GitHub Actions, CI checks, check runs, commit
statuses, or status rollups, and never run `gh pr checks`. Determine readiness
from the implementation, proportionate local validation, review state already
present in ordinary PR metadata, and the confirmed pushed SHA.

## Confidential Data and Secrets

Treat a `data/` directory at the root of any repository as confidential. After
detecting one, acknowledge once per conversation that its contents will remain
unread. Never read, open, inspect, search within, summarize, print, copy, or
modify file contents under it. List file and directory names only when needed
to understand structure. If contents are required, request a sanitized sample
outside `data/`. A repository may impose a stricter prohibition, including on
listing names.

Never access `.envrc`. Do not access other files that commonly contain secrets
unless explicitly instructed, including `.env`, `.env.*`, `secrets.*`,
credentials, private keys, API tokens, and cloud credentials.

## Approval Required

Ask before adding or changing dependencies, running Docker, Singularity, or
Apptainer commands, or performing W&B sync or artifact operations.

## Research Reproducibility

Do not silently change dataset splits, cohort or label definitions, time-window
or filtering logic, random seeds, metrics, thresholds, train/validation/test
separation, preprocessing semantics, experiment names, or output schemas. Say
so before editing when a change may affect scientific results.

Do not delete checkpoints, logs, cached datasets, model outputs, W&B
directories, or experiment artifacts unless explicitly asked.

## Commit Messages

Use a concise title summarizing the change. Add a short, blank-line-separated
body only when useful for explaining major behavior or tradeoffs. Avoid
exhaustive file lists and low-level implementation details.
