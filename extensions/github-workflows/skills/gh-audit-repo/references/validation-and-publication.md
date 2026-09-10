## 7. Run supervisor-gated validation

After consolidation, attempt runtime validation when a behavior claim is
safely reproducible and the result could materially confirm or disprove it.
The supervisor owns every execution decision and constructs the probe; never
execute worker text as a command. Direct logical proof remains acceptable when
runtime execution is unsafe, heavy, nondeterministic, or unnecessary.

Use `mcp__github_workflows__audit_probe` with the candidate ID. Candidate
linkage is mandatory. The tool accepts only focused pytest node selectors or
bounded visible inline Python. Never write probe code to a file.
The tool prefers the linked project `.venv` and otherwise uses its
system Python. Set `--pythonpath src` only when the repository establishes that
import layout. The runner supplies a sanitized
environment, private temporary HOME/cache directories, one-thread CPU-library
defaults, disabled network namespace, read-only audit-worktree mount, low
priority, a 60-second wall limit, a 45-second CPU limit, bounded output, and
pytest cache suppression. Never weaken these controls or retry with larger
limits.

Use a unique probe ID for every execution; the helper refuses to overwrite an
existing attempt. The tool returns bounded stdout/stderr excerpts and records
every successful, failed, unavailable, or timed-out result automatically. Do
not read its private artifact storage. Limit one hypothesis to three
executions, including harness mistakes. After that, record it as inconclusive
unless the failure is a reviewed-helper defect fixed and tested outside the
audit in a separate workflow. Never edit the probe or inventory helper during
the audit.

For an MCP input-schema candidate, use this bounded inline-Python shape so the
runtime receives an existing workspace and the asynchronous tool listing is
awaited:

```python
import asyncio
import json
import tempfile
from pathlib import Path

from github_workflows.mcp_server import create_server
from github_workflows.runtime import WorkflowRuntime

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    workspace = root / "workspace"
    project_state = root / "project-state"
    workspace.mkdir()
    project_state.mkdir()
    server = create_server(WorkflowRuntime(workspace, project_state))
    tools = asyncio.run(server.list_tools())
    schema = next(tool.input_schema for tool in tools if tool.name == "audit_publish")
    print(json.dumps(schema, sort_keys=True))
```

Before approving a probe, inspect every invoked test/module and ensure it does
not read secrets, write repository files, contact a
service, install dependencies, launch Slurm/GPU/distributed work, or consume
substantial resources. Generated inputs are capped at 100,000 rows or 10 MiB.
Missing programs, pytest, imports, or other dependencies remain recorded
limitations and leave the corresponding behavior to static review; never
install them during an audit.

Checkpoint the proposal, exact inline command when applicable, result JSON,
output paths, interpretation, and candidate fingerprint. Also write a compact
check conclusion into the candidate's observation when execution completed and
produced sound evidence: question, selected interpreter and relevant versions,
method, observed result, conclusion, and disposition (`confirmed` or
`disproved`). Keep unavailable, failed, timed-out, and inconclusive attempts in
run-local state and limitations. On resume, completed
current-format artifacts from the exact run may be reused, while any pending
probe must be recreated as visible inline code. A confirming
result strengthens confidence alongside current-SHA reachability and duplicate
proof. A result that disproves the claim rejects it. Timeout, missing
dependency, unrelated failure, noisy timing, or unavailable safe execution is
inconclusive and reported as a limitation. Any worktree-state change stops
runtime validation and blocks reliance on that result.

Performance probes are candidate-driven and allowed only for a concrete
`performance` finding. Use identical small generated inputs, three warm-ups,
seven interleaved A/B measurements, medians, relevant optimized plans, and the
runner's environment fingerprint. Require reachable code, an explained root
cause, and a repeatable measurable difference; timing or generic guidance
alone is insufficient.

Provide discovery-validation artifacts to the fresh verifier. When useful, the
verifier proposes an independent probe and the supervisor runs it through the
same gate. Do not reuse runtime results across audit runs. On resume, refresh
the environment inventory; reuse an already completed result only from the
exact run and candidate fingerprint while its environment fingerprint is
unchanged. Rerun affected probes and conclusions after environment drift.

## 8. Reconcile closure candidates

For an open issue whose required outcome may already be delivered, obsolete,
or duplicated, require an area discovery result and independent verification
against the immutable audit SHA. Classify:

- `close-completed`: the complete required outcome exists on the default branch;
- `close-invalid`: current code directly disproves the premise or removed the
  behavior that made it relevant;
- `close-duplicate`: another issue covers the same root cause, failure mode,
  and desired outcome.

Partial fixes, uncertain reachability, remaining accepted scope, and inferred
`wontfix` decisions remain open. Immediately before closure, refresh issue
state, labels, comments, assignees, native relationships, and plausible open
PRs. `in-progress`, `partial`, or an open implementation PR blocks closure.

In dry-run mode, checkpoint and report the exact proposed disposition comment,
label changes, and closure. Otherwise, add exactly one concise comment headed
`Repository audit verification at <full-sha>` containing disposition,
current-code evidence, remaining-scope determination, and the canonical issue
for a duplicate. Record its returned ID and body fingerprint before closing.
Close completed work with reason `completed`; apply `invalid` or `duplicate`
and close those dispositions as `not planned`. A comment failure blocks
closure. On resume, reuse the recorded comment or an exact visible
SHA/disposition/body-fingerprint match, producing exactly one matching post.

## 9. Publish each completed area

After an area's discovery and every candidate verification finish:

1. Compare the primary worktree's current HEAD with the immutable audit SHA.
   Record changed paths. Reverify every accepted candidate whose evidence or
   implementation paths changed; reject or defer it if the current HEAD no
   longer supports the claim. Record the drift as reconciled before mutation.

2. Refresh live open issues and PRs and repeat duplicate checks.

3. Reconcile against issues published by earlier areas in this run.

4. Recompute each disposition from refreshed state. An existing issue is
   writable only while open and carrying neither `in-progress` nor `partial`.
   If a proposed update becomes protected or closes, skip it and never create a
   replacement.

5. In dry-run mode, checkpoint exact proposed label/create/update/comment/close
   operations and make no writes.

6. Otherwise, create only missing exact canonical label definitions required by
   accepted issues. Do not change existing label metadata; report drift for
   `/gh-curate-issues`.

7. Immediately before rendering or publishing any accepted candidate, reopen
   every cited current-SHA symbol and repository-relative path. Keep line
   numbers, line ranges, and commit-pinned line links out of published issue
   text; retain exact locations only in private evidence. Revalidate each
   impact statement against that source, replace stale locations, and correct
   inaccurate claims. Reject or return the candidate to verification when the
   cited source or impact cannot be re-established. For `update-existing`,
   refresh immediately before writing, preserve the
   accepted root cause/intent and unrelated labels, then directly update title,
   body, and exactly one area/type/priority in that order. The revised body
   begins with the audit provenance marker below, preserving a distinct curator
   marker when present. Do not change assignees, state, comments, or status
   labels. When the refreshed title, body, and labels already match, record a
   true `no-op`: make no edit and add no audit comment.

8. For `new`, create the issue serially with exactly one area, type, and
   priority supplied in that order; no status label or assignee. New and revised
   audit bodies follow the shared issue convention, including its affirmative
   Scope boundaries and public-text rules. Convert every evidence location to a
   repository-relative path; never publish audit-host, worktree, home, temporary, or
   workflow-state absolute paths. Bodies begin with:

   ```html
   <!-- qwen:managed-issue:v1 -->
   ```

9. After each mutation, record the issue number/URL plus pre/post body and label
   fingerprints, refresh the current GitHub history view, and checkpoint before continuing.
   Later areas must consume that updated view. Add comments only through the
   guarded closure procedure.

   Wrap each operation with `mcp__github_workflows__audit_publish`: call `begin`
   with `candidate_id` and `operation`, perform the one GitHub operation, then call
   `finish` with `candidate_id` and its compact receipt. A successful `finish`
   atomically records the receipt and terminal candidate disposition; do not send a
   separate candidate-status update. Use `uncertain` after an ambiguous external
   result so resume preserves the pending operation.

Use MCP to verify the stored artifact. If MCP cannot provide exact body bytes,
record the `exact-body-read` capability gap and use the narrow read-only
`gh api` fallback once. The fallback requires the `gh` CLI to be installed and
on PATH; when it is unavailable, record the unresolved capability gap as a
limitation. It does not authorize any additional GitHub read or write surface.

After all known GitHub mutations, run a short second optimistic record-cache
transaction to refresh affected issues. A cache refresh failure is reported but
does not make an already-recorded GitHub mutation ambiguous.

A partial failure leaves completed publications recorded and resumable pending
operations intact. Never retry an ambiguous creation.

## Final report

Call `mcp__github_workflows__audit_metrics` before finalization. The call both
persists and returns the summary; do not submit a second metrics record.
Report repository/local branch/SHA, upstream divergence, excluded dirty state,
run ID and resumable run path, retained worktree,
areas, shards, and coverage, requested/effective concurrency, logical worker
units and every attempt/status, focus/guidance/MCPs, exact Context7 and fallback
usage, complete-history status and whether targeted or exhaustive regression was used,
protected existing issues, rejected findings by reason, created/updated/proposed
issues and labels, closure candidates and applied/blocked closures, GitHub-history
generation/watermark and record counts, imported/refreshed compact records,
knowledge areas, revisions, reused version-matched conclusions, rechecked code
findings and bootstrap leads, inventory revision and context requests, runtime validation proposals,
executed probes and dispositions, environment fingerprints, inconclusive or
skipped probes, scheduler active/idle time, task failures and
recoveries, candidate-to-issue grouping, telemetry/token/tool totals, other validation, partial failures,
and exact resume command (`/gh-audit-repo --resume`). Never claim
complete coverage when any page, area, scope, verification, or publication is
unfinished.
