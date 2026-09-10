# Assess Current GitHub Work

Use this stage in both pickup modes. Resolve current evidence before any
implementation mutation. The main skill defines the authorized mutations for
each mode.

## 1. Resolve the work graph

1. Resolve the repository and explicit artifacts with required MCP reads. Call
   `get_me` because managed-comment ownership must be established.
2. Build a bipartite graph of issues and implementation PRs. Add an edge only
   for a native Development relationship or an exact closing reference from a
   PR targeting the repository default branch. Ordinary mentions, related
   links, branch names, and similar wording are candidate evidence, not graph
   edges.
3. Starting from every explicit artifact, expand through those implementation
   edges. For an issue, include all attached open, closed, merged, and
   superseded PRs needed to understand its implementation chain. For a PR,
   include every issue it claims to resolve. If one of those PRs claims other
   issues, include them so the PR-wide judgment covers its complete declared
   scope.
4. Deduplicate artifacts and record why each edge exists. Resolve competing or
   superseded PRs into one authoritative implementation chain only when the
   GitHub record and immutable history establish it. Otherwise retain the
   ambiguity and classify the affected result as `Unverifiable`.
5. An issue with no authoritative PR enters issue-only mode. A PR with no
   attached issue enters PR-only mode; missing issue linkage does not prevent a
   direct assessment of the changes.

Use the full issue timeline through `gh` when native relationship fields and
MCP search are incomplete or contradictory, or as part of the general fallback
when MCP is unavailable:

```bash
gh api repos/OWNER/REPO/issues/N/timeline --paginate
```

Use the CLI's existing authenticated session; never inspect or inject
`GH_TOKEN`.

## 2. Capture and protect a stable snapshot

Record every issue and PR state, labels, update time, base, relationships, and
immutable head or merge SHA. A pre-existing `in-progress` on any required open
node means an implementation workflow owns part of the graph; stop before
implementation inspection or mutation. Perform the assessment read-only from
that snapshot and first compute the complete desired mutation set.

Default mode with a sound, actionable implementation defers assessment
mutations to the implementation lifecycle and continues without an additional
claim. The remaining reconciliation applies to `--assess-only` and to a
default-mode reframe that stops implementation.

If the desired state already matches, refresh every node once and finish as a
true no-op without claiming. If any mutation is required, refresh every node,
stop on snapshot drift, and transactionally claim every open graph node with
`in-progress`. For a PR carrying `ready-to-merge`, replace it with
`in-progress` in one complete-label update and record the prior state. Confirm
all claims, then re-read and compare every non-lifecycle snapshot fact before
publishing. On claim or revalidation failure, release all run-owned claims,
restore prior `ready-to-merge` where safe, verify rollback, and stop. Closed issues and
closed or merged PRs remain read-only.

## 3. Assess whether the work makes sense

### Issue assessment

For every issue, independently determine:

- whether the stated problem is current, evidence-backed, and meaningful;
- whether its required outcomes actually address that problem;
- whether the scope is coherent, independently deliverable, and free of
  incorrect premises or unnecessary prescriptions;
- whether maintainer clarifications materially correct the accepted scope;
- whether current code or merged work made the issue obsolete or invalid.

Classify the issue as `Sound`, `Needs scope correction`, `Obsolete or invalid`,
or `Unverifiable`. The assessment reports corrections in its managed comment; it
does not edit or close the issue.

### Pull-request assessment

For every PR, inspect its complete diff at an immutable SHA, commits, ordinary
reviews and comments, available non-CI validation, and enough surrounding code,
callers, configuration, and tests to establish behavior. Always determine:

- the actual purpose of the changes and whether that purpose is worthwhile;
- whether the chosen changes are necessary, correct, cohesive, and
  proportionate;
- whether they introduce regressions, unsafe semantics, unrelated work, or
  ineffective tests;
- whether the PR description accurately represents its implementation and
  accepted scope;
- for linked work, whether each issue requirement is satisfied without relying
  on a flawed issue premise.

A literal match to an issue is insufficient when the issue or implementation
does not make sense. Conversely, a reasonable implementation does not silently
expand accepted issue scope.

For a PR-only graph, derive the proposed objective from the PR title, body,
commits, and diff, then verify it against current code. Assess the changes on
their own merits. Use `Unverifiable` when no coherent objective or sufficient
evidence can be established.

### Requirement mapping

For each issue, classify every accepted requirement as `Satisfied`,
`Remaining`, `Corrected`, or `Unverifiable`. Put incorrect premises,
out-of-scope follow-up, and essential corrections in the scope-correction
record. Add an essential requirement only when the stated outcome or a concrete
regression introduced by the implementation requires it. Keep the assessment bounded to the selected work graph.

When collaboration is available, use an independent read-only subagent for
multiple PRs, scientific or high-impact behavior, materially incomplete
validation, ambiguous implementation chains, or a proposed incorrect-premise,
regression, or ready-to-merge conclusion. The subagent must establish its own
GitHub access under the shared access policy. Give it identifiers,
immutable SHAs, baseline, and evidence rather than a tentative verdict.
Codex subagents may inherit the parent session's complete GitHub MCP schema.
For them, read-only is an authorization boundary rather than a tool-visibility
test: instruct the subagent to call only GitHub read operations. Visible
mutation tools alone do not trigger suspension. A missing or failed required
read does.

## 4. Compose managed evidence

Follow the [managed comment format](comment-format.md).

- Create or update an issue-specific managed reassessment comment only for an
  actionable issue-premise or accepted-scope correction that requires
  maintainer attention and is not already represented by the issue body.
- Create or update one managed PR conversation comment for a concrete
  implementation defect, missing accepted requirement, or implementation
  uncertainty requiring action. This applies to linked and unlinked PRs; name
  affected issues once rather than duplicating the finding on their threads.
- Never publish a comment merely to say that an issue is sound, requirements
  are satisfied, validation passed, or a PR is ready to merge.

When every assessed issue is Sound and fully Satisfied and every assessed PR
meets the ready-to-merge criteria, create or update no success comment.
Reconcile only required lifecycle labels and deletion of an obsolete owned
managed comment; when no obsolete comment exists, the sole durable GitHub
mutation in the ordinary clean case is adding a missing PR `ready-to-merge`
label.

Delete this skill's existing managed PR comment when its actionable finding
is fully resolved. Delete a managed issue comment only when its correction is
obsolete or already incorporated into the issue body; retain an unincorporated
scope correction because implementation workflows consume it as accepted
scope. Never replace a deleted comment with a success note. Comment deletion
is a no-op when no owned managed comment exists.

Keep the complete evidence matrix, immutable SHAs, status-label reasoning,
worker results, exact commands, and workflow limitations in the private run
record and final report. The public comment follows the reference's 300-word
hard limit, uses `Issue reassessment` or `Pull request reassessment`, and never
uses a model or workflow name as its visible title.

Before a warranted comment mutation, discover comments owned by the
authenticated user whose first line is either
the canonical marker or the legacy issue marker defined in the comment
reference. Stop on multiple managed comments for one artifact. Create the
canonical marker when none exists. Replace one legacy managed comment in place
with the canonical format. An identical rendered body is a no-op.

MCP creates comments. For the MCP server's comment editing and deletion
capability gaps, use the reviewed helper only after resolving the exact owned
managed comment. Write an update body to a private temporary file and run:

```bash
scripts/update_managed_comment.py \
	--repo OWNER/REPO --artifact-number NUMBER --comment-id COMMENT_ID --body-file /absolute/comment.md
```

The helper never discovers or creates comments. Its update path requires
`--artifact-number` and re-verifies the target comment's owner, managed
marker, single marker, and artifact immediately before the PATCH; `--dry-run`
performs the same verification without mutation, and a foreign or unmanaged
target fails without a PATCH. To delete an obsolete managed comment, run:

```bash
scripts/update_managed_comment.py \
	--repo OWNER/REPO --artifact-number NUMBER --comment-id COMMENT_ID --delete
```

The delete path fetches the comment and authenticated user immediately before
mutation and rejects a foreign owner or unrecognized marker. Preserve
suspension artifacts and remove temporary bodies after successful completion.

## 5. Determine evidence-backed status

Refresh the complete graph and require every evaluated open PR to retain the
immutable head SHA used for assessment. Any snapshot drift or newly established
external `in-progress` ownership blocks all comment and status reconciliation.

A merge-conflicting PR cannot receive `ready-to-merge`. Report the conflict as
an actionable PR finding only when it adds new guidance. Resolve it through
default pickup mode, whose implementation mutation begins with a latest-base
rebase.

Determine the desired post-release status:

- Retain or apply issue `partial` when an authoritative pushed PR contains usable work
  but that issue's sound accepted scope remains incomplete. Remove it when the
  issue is complete, invalid/obsolete, or lacks usable remote implementation.
- Apply PR `ready-to-merge` only when the changes themselves make sense, are
  correct and cohesive, proportionate local validation passes, the remote SHA
  matches, and ordinary review evidence has no known blocker. For a linked PR,
  every issue it claims to resolve must also be Sound and fully Satisfied. For
  an unlinked PR, its independently established objective must be coherent and
  fully delivered.
- Leave PR `ready-to-merge` absent when any of those conditions fails. It cannot
  coexist with `in-progress` or `partial`.
- Create missing exact workflow-owned status labels when required and report
  definition drift without repairing it.

`ready-to-merge` expresses only what this skill can establish without CI.

## 6. Reconcile and report

While holding every required claim, compare each desired comment and label
state with the live artifact and perform only differing mutations. Apply
comment deletions or actionable comment updates first. Immediately before
status reconciliation, refresh PR heads, reviews, comments, relationships, and
all non-lifecycle facts again; rollback and stop on drift.

Finalize PRs before issues: use one complete-label update per PR to replace this
run's `in-progress` with the desired `ready-to-merge` or non-ready state while
preserving all other labels, then use one complete-label update per issue to
replace this run's `in-progress` with the desired `partial` or non-partial
state. Read back every transition. On failure, use the shared suspension and
rollback procedure and prominently report unreconciled claims.

Report:

- explicit inputs and the resolved issue/PR graph with relationship evidence;
- issue-soundness verdicts and per-requirement status;
- PR change-sense, correctness, scope, validation, and immutable-SHA verdicts;
- actionable managed comment URLs or no-comment/no-op state;
- managed-comment creation, update, deletion, or no-op state;
- `partial` and `ready-to-merge` reconciliation;
- ambiguity, taxonomy drift, limitations, every external mutation, and the
  smallest next step.
