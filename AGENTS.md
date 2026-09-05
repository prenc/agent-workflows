# Repository Instructions

## Structure and Runtime

Python source lives under `src/github_workflows/`; tests live under `tests/`.
The native Qwen extension lives under `extensions/github-workflows/` and owns
its `skills/`, `agents/`, `hooks/`, `references/`, and `qwen-extension.json`.
Codex skills live under `codex/skills/`.

After changing extension code, run `/reload-plugins` in Qwen. An unfinished
workflow whose helper bundle changed must be aborted and restarted. Persistent
history and knowledge under `QWEN_CODE_PROJECT_DIR` remain reusable.

Runtime dependencies belong in the main dependencies and development
dependencies in the `dev` extra. Preserve the root `.venv`; do not generate
`uv.lock` or use `uv sync` in the root or a shared worktree. Use
`uv pip install` for approved root-environment changes. An active workflow may
provision isolation only under its own documented safeguards.

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
Keep workflow state, GitHub history, knowledge, and probe artifacts under
`QWEN_CODE_PROJECT_DIR`, outside the repository. Tests use temporary
directories and mock commands; they must not use the real home, network, sudo,
or package installation. Preserve helper-integrity, concurrency, resumability,
publication, and context-isolation coverage when changing the runtime.

## Logging

Keep lifecycle output concise and non-redundant. At normal verbosity,
prioritize run identity, workflow status, task counts, timings, artifacts, and
failures; leave detailed paths, payloads, and execution plans for verbose or
debug output.

## Development Workflow

When environment creation or dependency installation is explicitly requested,
use this bootstrap; otherwise preserve the existing root `.venv`:

```sh
uv venv
uv pip install --python .venv/bin/python -e '.[dev]'
```

Then install the managed integrations and Git pre-commit hook:

```sh
uv run --no-sync agent-workflows install --dev
```

For implementation work:

1. Add or update tests in the owning subsystem. Run focused selectors first:
   `uv run --no-sync pytest -q <selectors>`.
2. Run the broader affected group after focused checks pass. Run the
   full suite with `uv run --no-sync pytest -q` when the change crosses
   subsystems or affects shared runtime behavior.
3. Run `uv run --no-sync pre-commit run --all-files` once code and
   documentation edits are complete. Inspect hook changes and rerun affected
   tests if a hook modifies files.
4. Run `uv build` when changing packaging, dependencies, entry points, or
   release artifacts. Do not generate or commit `uv.lock`.
