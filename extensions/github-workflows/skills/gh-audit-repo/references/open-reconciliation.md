## Open-record reconciliation

With `--reconcile-open`, run this phase after complete-history synchronization
and before structure or discovery. Capture the identifiers, update timestamps,
relationships, and exact PR head SHAs of every issue and pull request then open.
Partition that snapshot into connected issue/PR graphs. Plan one `reconcile`
worker task per graph, include every member as a canonical `history_links`
entry, and account for every snapshot record exactly once. The same `-n`
material-work limit and result-first integration rules apply.

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
- keep-partial when usable pushed work exists but accepted scope remains incomplete;
- clear-partial when scope is complete or the remote implementation is unusable;
- retain-open for valid complete or actionable work.

Propagate `partial` to the PR and each incompletely covered open issue. Remove
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
uses `artifact_kind: pull`, `pull_number`, and its captured full `head_sha`; the
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
