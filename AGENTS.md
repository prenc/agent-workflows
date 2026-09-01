# Agent Workflows Project Guidance

## Environment

Use this shared HPC login node only for inspection, lightweight development,
formatting, and small unit tests. Do not run Slurm jobs or heavy computation
unless explicitly requested. Never read `.envrc` or secret-bearing files.

## Structure and runtime

Python source lives under `src/github_workflows/`; tests live under `tests/`.
The native Qwen extension lives under `extensions/github-workflows/` and owns
its `skills/`, `agents/`, `hooks/`, `references/`, and `qwen-extension.json`.
Codex skills live under `codex/skills/`.

Use `uv` and the root `.venv`. Runtime dependencies belong in the main project
dependencies; development-only dependencies belong in the `dev` extra. Ask
before changing dependencies. Do not create another environment, generate an
`uv.lock`, or use `uv sync`; install with `uv pip install`. Always invoke
project executables through `uv run`, including pytest, pre-commit, and the
`agent-workflows` CLI. Do not call executables from `.venv/bin` directly. Direct
`uv` environment and packaging operations such as `uv venv`, `uv pip install`,
and `uv build` are the only exceptions.

The public CLI is `agent-workflows`. Its `install` subcommand owns Qwen, Codex,
and third-party skill integration; `workflow` is the recovery interface; `mcp`
is reserved for the Qwen extension. Do not add shell launchers.

## Validation workflow

Running tests and pre-commit is pre-authorized. During implementation, run the
fastest coherent test group for the behavior being changed; do not run
pre-commit between edits. Use `-q` or `--tb=line` by default and increase
verbosity only to diagnose a failure. If a process-based test appears to stall
only in the sandbox, rerun that same test outside the sandbox before treating
it as a product failure.

Run `uv run pre-commit run --all-files` once implementation and tests are
complete. Always use `--all-files`. Never invoke Ruff directly; use the Ruff
hooks configured in pre-commit. If hooks modify files, inspect the changes,
rerun affected tests when appropriate, and finish with another full pre-commit
run. Report missing tools clearly rather than silently changing dependencies.

## Workflow invariants

Keep the twelve high-level MCP tools as the agent-facing contract. Public calls
use flat arguments, never JSON encoded inside a `request` wrapper. Supervisors
own workflow mutations. Named workers receive a namespaced task reference and
may use only `mcp__github_workflows__task_context` from this server.

In Qwen named-subagent frontmatter, `tools:` entries are exact names; wildcards
do not expand. MCP patterns apply only to `fork_tools` and `disallowedTools`.
Keep workflow state, GitHub history, knowledge, and probe artifacts outside the
repository under `QWEN_CODE_PROJECT_DIR`. Tests must use temporary directories,
mock commands, and avoid the real home, network, sudo, and package installation.

Keep behavioral rules in system prompts and mechanical interface details in
tool descriptions and argument schemas. User messages carry the current task
and server-owned state. Preserve native structured tool-call history: every
assistant tool call must precede its matching tool result, and no workflow may
finish with an unresolved call. Do not encode tool results as assistant prose or
manually construct provider-specific tool-call wrappers.

When compacting workflow history, preserve assistant messages, tool names, call
IDs, and arguments exactly. Mask only explicitly configured fields in copied
tool-result payloads. Never mask a pending result; retain the latest accepted
state change and the latest relevant rejection or no-op. Keep original trace
and audit records unchanged.

## Logging

Keep lifecycle output concise and non-redundant. At normal verbosity, prioritize
run identity, workflow status, task counts, timings, artifacts, and failures;
leave detailed paths, payloads, and execution plans for verbose or debug output.

## Validation

Run:

```sh
uv venv
uv pip install --python .venv/bin/python -e '.[dev]'
uv run pytest
uv run pre-commit run --all-files
uv build
```

Use pytest fixtures, parametrization, `pytest.raises`, and `pytest.mark.asyncio`;
do not add unittest-style tests. Preserve helper-integrity, concurrency,
resumability, publication, and context-isolation coverage when changing the
runtime.

Organize tests by workflow or subsystem responsibility. Add regressions to the
group that owns the behavior, use one or two explicit files only for a narrow
check, and run the broader group afterward. Use doctests sparingly for small,
public, deterministic helpers.

Do not assert exact wording in prompts, tool descriptions, argument help, or
other agent-facing prose, and do not assert only that such prose contains a
substring. Prefer consumed behavior, schemas, required and optional fields,
validation outcomes, state transitions, and end-to-end effects.

## Commit Messages

Use a concise title. Add a blank-line-separated body with a few bullets only
when extra context is useful. Avoid exhaustive file lists and low-level
implementation detail.

```text
Improve history synchronization

- Keep bulk GitHub records outside model context
- Preserve transactional cache validation
```
