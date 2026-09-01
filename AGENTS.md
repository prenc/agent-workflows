# Repository Instructions

## Structure and Runtime

Python source lives under `src/github_workflows/`; tests live under `tests/`.
The native Qwen extension lives under `extensions/github-workflows/` and owns
its `skills/`, `agents/`, `hooks/`, `references/`, and `qwen-extension.json`.
Codex skills live under `codex/skills/`.

Runtime dependencies belong in the main project dependencies and development
dependencies in the `dev` extra. Preserve the root `.venv`; do not generate an
`uv.lock` or use `uv sync`. Use `uv pip install` for approved environment
changes. Invoke project executables through `uv run`; direct `uv` environment
and packaging operations such as `uv venv`, `uv pip install`, and `uv build`
are the exceptions.

The public CLI is `agent-workflows`. Its `install` subcommand owns Qwen, Codex,
and third-party skill integration; `workflow` is the recovery interface; `mcp`
is reserved for the Qwen extension. Keep these as native CLI interfaces rather
than adding shell launchers.

## Workflow Invariants

Keep the thirteen high-level MCP tools as the agent-facing contract. Public calls
use flat arguments rather than JSON encoded inside a `request` wrapper.
Supervisors own workflow mutations. Named workers receive a namespaced task
reference and may use only `mcp__github_workflows__task_context` and the
write-only `mcp__github_workflows__workflow_feedback` from this server.

In Qwen named-subagent frontmatter, `tools:` entries are exact names; wildcards
do not expand. MCP patterns apply only to `fork_tools` and `disallowedTools`.
Keep workflow state, GitHub history, knowledge, and probe artifacts outside the
repository under `QWEN_CODE_PROJECT_DIR`. Tests use temporary directories and
mock commands, avoiding the real home, network, sudo, and package installation.

When compacting workflow history, preserve assistant messages, tool names,
call IDs, and arguments exactly. Mask only explicitly configured fields in
copied tool-result payloads. Preserve a pending result, the latest accepted
state change, and the latest relevant rejection or no-op. Keep original trace
and audit records unchanged.

## Logging

Keep lifecycle output concise and non-redundant. At normal verbosity,
prioritize run identity, workflow status, task counts, timings, artifacts, and
failures; leave detailed paths, payloads, and execution plans for verbose or
debug output.

## Environment Bootstrap

When environment creation or dependency installation is explicitly requested,
use:

```sh
uv venv
uv pip install --python .venv/bin/python -e '.[dev]'
```

## Validation

Use:

```sh
uv run pytest
uv run pre-commit run --all-files
uv build
```

Use pytest fixtures, parametrization, `pytest.raises`, and
`pytest.mark.asyncio`; keep tests in pytest style rather than unittest style.
Preserve helper-integrity, concurrency, resumability, publication, and
context-isolation coverage when changing the runtime.
