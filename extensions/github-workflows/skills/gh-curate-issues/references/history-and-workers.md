## Stage 1: synchronize the bounded relationship space

Resolve `OWNER/REPO`, the remote default branch, and its immutable latest SHA.
Resolve each selected target as an open issue rather than a pull request.
Before analysis or mutation, initialize the run with
`mcp__github_workflows__run_manage`. Pass `repository`, `n`, `targets`,
`refresh_history`, and `dry_run` as top-level tool arguments;
never wrap them in `request` or `inputs`, and never stringify them as JSON. On
resume, pass only an explicitly supplied `n` in
addition to the action and workflow. Maintain
supervisor-owned `state.json`, a concise `journal.jsonl`, `history.json`, and
declarative per-unit results, checkpointing after every worker result and
GitHub mutation.

Set the run timestamp once in UTC and calculate `cutoff = run-start - history-days`. Include:

- every open issue and open pull request, regardless of age;
- a closed issue when `closed_at >= cutoff`;
- a merged pull request when `merged_at >= cutoff`;
- an unmerged closed pull request when `closed_at >= cutoff`;
- an older same-repository issue or pull request directly referenced or
  natively linked from the current open inventory.

When the required closure timestamp is unavailable, `updated_at >= cutoff` may
prove inclusion; otherwise exclude the closed record unless the explicit-link
exception applies. Resolve explicit `#N`/URL references and native
relationships one hop from open records. Include the direct closing issue/PR
counterpart of an admitted relationship. Traverse explicit references and
native relationships one hop from open records, keeping historical search
within this configured space.

### Shared persistent GitHub history cache

Use `mcp__github_workflows__history_manage` for status and synchronization and
`mcp__github_workflows__history_query` for selector-based views of at most 100
records. Status and prepare responses provide cache generation, record count,
completeness, synchronization watermark, and default SHA; never query records
to infer those values. Storage is a private server detail. The curator applies
`history-days` as a query boundary while retaining older shared records for
other workflows.

Use this optimistic transaction order:

```text
prepare-records -> import/ingest -> query per issue -> commit-records
                                                  \-> abort on failure
```

The server owns transaction paths and generations. Pass the full immutable
default-branch SHA from the live repository read as `default_sha` on the
initial commit; later commits inherit it from their staging base unless the
live default SHA changed. Commit holds a short lock
and succeeds only when the live generation still matches the prepared base. On
conflict, abort, prepare from the new live generation, repeat the incremental
refresh once, and retry. A second conflict blocks GitHub mutation and is
reported. Use the returned staging classification and recovery action rather
than inferring transaction state from a prior interruption.

When a GitHub MCP response reports `<persisted-output>`, pass every reported
tool-result path as a typed `{kind, path}` entry in the `artifacts` list of a `history_manage` ingest
call. Never read, copy, split, summarize, or re-transcribe those files for
ingestion. Use inline `records` only for results that remained inline; each
record carries its own `kind`, with at most
100 compact records per call; never provide `records` and `artifacts` together.
Keep explicit-link sets as compact inline data. On first use or automatic
recovery, import a valid legacy
`curation-v1.sqlite3` and the newest compatible completed audit artifact when
available, then enumerate all issues and pull requests before marking
`full_history_complete`. Preserve legacy files. Imported summary, tombstoned,
or incomplete records require live hydration before supporting a relationship
conclusion.

On a normal run, require complete history and synchronize all open records plus
records changed since five minutes before the successful watermark. Directly
refresh plausible matches and every explicit older exception.
`--refresh-history` re-fetches records in the current bounded curator view.
Automatic recovery reconstructs complete history atomically. A changed
default-branch SHA updates metadata without invalidating unchanged records.
Bulk synchronization requests only number, URL, title, labels, state, assignees,
timestamps, and pull-request refs; omit bodies and all detail collections.

Advance the watermark only after every required page and direct fetch succeeds.
A partial synchronization blocks GitHub publication and reports the exact
missing page or record.

Before launching workers, create a current supervisor snapshot containing:

- every selected issue's live-read complete record, labels, assignees, relevant comments,
  workflow markers, and native linked-PR metadata;
- a compact view of every open issue;
- bounded cached metadata for eligible closed issues and pull requests;
- plausible closed-issue, duplicate, already-fixed, and implementation matches;
- repository label definitions and the immutable default-branch SHA.

The shared cache is a compact index of number, URL, title, labels, state, timestamps,
and pull-request refs; it never supplies bodies or relationship evidence. Search cached
titles and labels to select plausible matches. Also run targeted GitHub issue and
pull-request searches whose closed/merged date qualifiers enforce the same cutoff, so
body-only matches remain discoverable. Read every selected issue and plausible match in
full through MCP before including a duplicate, scope, body, or relationship conclusion;
do not ingest those detail payloads into history. Create each user-private
candidate bundle as UTF-8 JSON under the run's `artifacts/` directory. Use
top-level `selected_issue`, `matches`, `relationships`, `repository`, `cutoff`,
`watermark`, and `default_sha` fields. Each selected issue and match has `kind`,
positive `number`, `state`, and its snapshot reference. Include each
`(kind, number)` at most once; contradictory states are invalid. Include only
that issue, plausible matches read in full, relevant relationship records, the
repository summary, cutoff, watermark, and immutable default SHA.
Keep secrets and repository contents out of bundles.

Every plausible record named in the supervisor shortlist or match index must
appear in `matches` with its snapshot after the required full read. Do not omit
a plausible match merely because the supervisor expects the worker to rediscover
it.

GitHub issue and PR records are the curator's evidence boundary. Treat paths,
symbols, and implementation statements as claims from those records. Route a
decision requiring current-code proof to `/gh-audit-repo` or
`$gh-pickup-work --assess-only`.
Label every code-related “Key facts” entry as either audit-verified, with the
originating audit run and immutable SHA when available, or as an unverified
claim from a named GitHub record. Never instruct a curator worker to inspect
repository source to verify it.

## Stage 2: run one complete report per issue

Register one complete assignment per issue with
`mcp__github_workflows__task_manage` using action `plan` and a typed `task`.
Its assignment contains `issue`, `issue_snapshot`, and the run-relative
`candidate_bundle` path. Repository, default SHA, dry-run state,
documentation, and reference paths are server-derived task context and must
not be duplicated in the assignment.
Use its returned server-generated task ID and task reference, then queue exactly one fresh-context
`gh-curate-issues-worker` for every selected issue. Each worker receives only:

```text
Task ref: <task-ref-returned-by-task-manage>
```

Copy the task reference exactly. Immediately after the launch is accepted,
call `task_manage` action `mark_running` before waiting. If launch fails, use
`abandon` while the task remains queued. Record the worker result before
interpreting it, then bracket synthesis with `integration_begin` and
`integration_end`; identical lifecycle retries are safe.

Each worker performs a targeted live `issue_read`, consults its complete
candidate bundle, reads plausible GitHub matches as needed, and returns one
full curator report.
Each issue is assigned once. Concurrency changes scheduling only; it does not
change issue coverage or evidence requirements.

When a worker reports `MCP_UNAVAILABLE`, suspend every worker and the complete
run under `github-access.md`. Preserve all completed reports and
pending issue assignments for resume. An incomplete stored assignment may be
corrected and reassigned; other worker failures are reported without omitting
the issue.
When a worker reports `EXECUTION_BLOCKED`, record that attempt with
`task_manage` action `fail` and note `execution-blocked` and never retry it in
the same invocation. Continue other queued issues; once no independent work
remains, pause the run once. A later YOLO invocation reconciles GitHub and task
state before creating at most one new numbered attempt.
