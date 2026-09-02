# Agent workflows

Agent extensions, skills, and supervised workflows for auditing and maintaining
GitHub repositories. The repository is both an installable Python project and a
native Qwen Code extension under `extensions/github-workflows/`; it also owns
the corresponding Codex skills.

Windows is not supported. Linux and macOS are the intended platforms.

## Installation

Install `uv`, Qwen Code, and/or Codex first, then run:

```sh
uv run agent-workflows install
```

The command installs the project into `.venv`, renders the user-level Codex and
Qwen instruction files, links this checkout as the Qwen extension, links the
Codex skills, and installs the official Polars skill for both agents.
The neutral source artifacts live under `user-policies/`; their filenames do
not trigger repository instruction discovery before installation.
It is safe to rerun. Use `--dry-run` to inspect changes, `--yes` for unattended
installation, and `--verbose` to include unchanged integrations. The default
`--machine-role local` omits cluster-specific compute instructions; use
`--machine-role remote` when installing on a shared remote or HPC machine.

For development setup, including the Git pre-commit hook:

```sh
uv run agent-workflows install --dev
```

After changing extension code, run `/reload-plugins` in Qwen. An unfinished
workflow whose helper bundle changed must be aborted and restarted. Persistent
history and knowledge under `QWEN_CODE_PROJECT_DIR` remain reusable.

## Development

```sh
uv venv
uv pip install --python .venv/bin/python -e '.[dev]'
uv run pytest
uv run pre-commit run --all-files
uv build
```

Manual workflow recovery is available through `agent-workflows workflow --help`. Qwen starts the MCP server through the private `agent-workflows mcp`
subcommand declared in `qwen-extension.json`.

Material skill, MCP, workflow, or active-instruction friction reported by
supervisors and named workers is kept locally in
`${XDG_CACHE_HOME:-~/.cache}/agent-workflows/feedback.jsonl`.
Review it with `agent-workflows feedback list` and inspect one complete record
with `agent-workflows feedback show <feedback-id>`. The list prints compact
metadata followed by each complete wrapped summary; use
`agent-workflows feedback list --json` for machine-readable records. After review,
remove selected records atomically with
`agent-workflows feedback remove <feedback-id> [<feedback-id> ...]`.
