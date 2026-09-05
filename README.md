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
It also detects missing GitHub and Context7 MCP registrations for each client
and presents pending work in separate Codex, Qwen, and Shared sections. Press
Enter to install everything, enter numbers or ranges such as `2 4-6` to exclude
those entries, or enter `A` to exclude everything. Existing named MCP servers
are preserved. Use `--skip-mcp` to leave MCP configuration unchanged; `--yes`
selects every listed integration without prompting.
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
Qwen records feedback through the extension's `workflow_feedback` tool. Codex
and other local callers can record the same concise observation with
`agent-workflows feedback add "<message>" [--tool <name>]`; repository identity,
time, storage, and CLI provenance are derived automatically.
Use `agent-workflows feedback summary --json` for aggregate state: separate
open and closed source counts, closed dispositions by source, timestamp range,
and storage size. Use `feedback list` (`ls`) for records, with `--limit 1` for the
newest record or `--all` for every match. Both commands accept `--repository`,
`--workflow`, and an inclusive creation-time lower bound via `--cutoff`.
Each listed record has a short collision-free `ref` for routine commands and
retains its canonical `fb-` ID for storage and transcript correlation.
`feedback show <ref>...` accepts one or more records. Repeat `--source <name>`
on `ls` to include one or more normalized tool sources.
Use `feedback trace <feedback-id>`
to locate the exact Qwen session and tool call without printing conversation
content. Feedback stores PHI-free summaries and bounded call shapes only; raw
tool arguments, responses, prompts, and source-data excerpts remain exclusively
in the Qwen transcript.
The legacy `--tool` spelling remains an alias for `--source`. Close reviewed
records with `feedback close <feedback-id> [<feedback-id> ...]`, optionally
selecting a disposition and a short PHI-free note; default lists
show only open feedback, while `feedback ls --closed` inspects the retained
closed set. Use `feedback reopen` to restore a
closed record. Apply mixed dispositions atomically with
`feedback close --input <JSON-list|file|->`; `feedback remove` permanently
deletes records.

The `workflow-feedback` skill is available to both Codex and Qwen. With no
explicit action it performs read-only inventory, investigation, classification,
and grouping from one reusable snapshot. An explicit implementation request may
authorize multiple planned, non-conflicting feedback groups; validated records
are closed with evidence-based dispositions rather than deleted. Raw Qwen
transcript tracing, commits, pushes, installation, reloads, and MCP restarts
remain separately authorized actions.
