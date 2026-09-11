## Open-record reconciliation

With `--reconcile-open`, run this phase after complete-history synchronization
and before structure or discovery. Capture the identifiers, update timestamps,
relationships, and exact PR head SHAs of every issue and pull request then open.
Partition that snapshot into connected issue/PR graphs. Plan one `reconcile`
worker task per graph, include every member as a canonical `history_links`
entry, and account for every snapshot record exactly once. The same `-n`
material-work limit and result-first integration rules apply.
Reconcile tasks have `requires_integration: false`: consume their completed
reports directly. Open an integration window only for tasks in the returned
`scheduler.integration_queue`.

Workers fully read each graph, inspect the immutable default-branch source and
the exact PR changes, and return per-record classifications plus candidates for
any proposed mutation. Age and inactivity are never disposition evidence.
Classify uncertain, concurrently changed, or inaccessible records as skipped.
An `in-progress` label anywhere in a graph protects the graph from mutation.

### Objective dispositions

For issues, use the existing independently verified `close-completed`,
`close-invalid`, and `close-duplicate` gates. Apply `wontfix` only when an
explicit maintainer decision establishes it. Maintain `partial` only when
usable implementation is durably published at a pushed immutable SHA while
accepted scope remains incomplete.

For pull requests:

- close-completed when the complete outcome already exists on the default branch;
- close-invalid when current code disproves the premise or the change no longer applies;
- close-duplicate when another canonical PR covers the same outcome;
- close-wontfix only from an explicit maintainer decision;
- partial when usable pushed work exists but accepted scope remains incomplete;
- retain-open for valid complete or actionable work.

Use the same `partial` classification for issues and PRs; label removal is a
separate action, not a record disposition. Propagate `partial` to the PR and
each incompletely covered open issue. Remove
stale `partial` from affected artifacts and remove `ready-to-merge` whenever a
PR is partial or being closed. Never apply `ready-to-merge`. Preserve unrelated
labels and leave an issue open when its work remains necessary after its PR is
closed.

### Verification, probes, and publication

Give every mutation candidate to a fresh verifier. Reopen live state before
mutation and skip the graph if an issue lock, PR head change, relationship
change, or conflicting implementation appears. Use `audit_probe` when a
bounded side-effect-free probe could materially confirm the premise or
implementation. Default-branch candidates run at the audit SHA. A PR candidate
stores `artifact_kind: pull`, `pull_number`, and its captured full `head_sha`
on the candidate registered with `audit_record`, not on the `audit_probe` call.
Pass that candidate's ID to `audit_probe`; the
runtime verifies and fetches that GitHub pull ref into a detached managed
worktree and records the probe source SHA. Install nothing and do not query CI.

Serialize mutations through `audit_publish`. Before each closure, add exactly
one comment headed `Repository audit reconciliation at <full-audit-sha>` with
the disposition, current evidence, remaining issue scope, and canonical
replacement when applicable. Comment failure blocks closure. Apply status-only
label changes without public chatter. Read back every mutation and refresh the
shared history cache.

Dry-run records the exact proposed comments, labels, and closures and uses a
projected state overlay for later duplicate and publication decisions. Perform
no GitHub writes.

Complete the phase only after every snapshot record is classified or skipped,
all tasks and verifications are integrated, and mutations are reconciled.
Store issue/PR totals, classifications, skips, probes, mutations, snapshot
watermark, and a coverage digest in the phase summary. Then continue the normal
audit against the refreshed history view.

Send this shape as `phase.summary` to `audit_record(action="phase")` with
`phase.name="reconciliation"` and `phase.status="complete"`:

```json
{
  "snapshot_open_issues": 0,
  "snapshot_open_pulls": 0,
  "classified_issues": 0,
  "classified_pulls": 0,
  "skipped_issues": 0,
  "skipped_pulls": 0,
  "probes": 0,
  "mutations": 0,
  "snapshot_watermark": "<captured snapshot timestamp>",
  "coverage_digest": "<64 lowercase hexadecimal SHA-256 characters>"
}
```

Classified plus skipped must equal the corresponding snapshot total. Compute
the digest over UTF-8 JSON of per-record `{kind, number, disposition}` objects,
sorted by kind then number, using sorted object keys and compact separators
(`,` and `:`). Retain that disposition list as evidence. `probes` and `mutations`
are summary counts; they do not substitute for publication read-backs.

An unknown PR mergeability value is inconclusive. Compare changed regions and
current source for substantive obsolescence; overlap alone is not proof that a
PR cannot apply. Skip a disposition when a mechanical apply result is essential
and unavailable rather than inferring it from static comparison.
