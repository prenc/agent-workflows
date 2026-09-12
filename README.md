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

The `gh` CLI supports the `gh-pickup-work` managed-comment helper and is the
fallback when GitHub MCP is unavailable.

The command installs the project into `.venv`, links this checkout as the Qwen
extension, links the Codex skills, and installs the official Polars skill for
both agents.
It also detects missing GitHub and Context7 MCP registrations for each client
and presents pending work in separate Codex, Qwen, and Shared sections. Press
Enter to install everything, enter numbers or ranges such as `2 4-6` to exclude
those entries, or enter `A` to exclude everything. Existing named MCP servers
are preserved. Use `--skip-mcp` to leave MCP configuration unchanged; `--yes`
selects every listed integration without prompting.
It is safe to rerun. Use `--dry-run` to inspect changes, `--yes` for unattended
installation, and `--verbose` to include unchanged integrations. The default

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
and storage size. `feedback list` (`ls`) returns the 50 newest open records by
default, with `--limit 1` for only the newest record or `--all` for every match.
Select another state with `--status closed|all`. Both commands accept
`--repository`, `--workflow`, and a recent-age filter such as `--since 30d`.
Each listed record has a short collision-free `ref` for routine commands and
retains its canonical `fb-` ID for storage and transcript correlation.
`feedback show <ref>...` returns complete records. Repeat `--source <name>` on
`ls` to include one or more normalized tool sources.
Use `feedback trace <feedback-id>`
to locate the exact Qwen session, prompt, transcript, and feedback call. It
shows the three preceding tool interactions without payloads by default;
`--detail context` adds bounded visible conversation text and `--detail data`
adds bounded sanitized payloads. Hidden reasoning is never returned. Feedback
stores only the PHI-free summary and durable locator; transcript content remains
in the Qwen transcript. Codex and manual CLI feedback remain unlinked until
their clients expose reliable conversation metadata.
Close reviewed records with `feedback close <feedback-id> [<feedback-id> ...]`, optionally
selecting a disposition and a short PHI-free note; default lists
show bounded metadata and summaries with the local date as one record per line;
JSON retains the full timestamp and includes closed-record dispositions. Use
`feedback reopen` to restore a closed record. Apply mixed dispositions atomically with
`feedback close --input <JSON-list|file|->`; `feedback remove` permanently
deletes records.

The `workflow-feedback` skill is available to both Codex and Qwen. With no
explicit action it performs read-only inventory, investigation, classification,
and grouping from one reusable snapshot. An explicit implementation request may
authorize multiple planned, non-conflicting feedback groups; validated records
are closed with evidence-based dispositions rather than deleted. Raw Qwen
transcript tracing, commits, pushes, installation, reloads, and MCP restarts
remain separately authorized actions.
