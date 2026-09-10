## 5. Discover by shard

Register each complete assignment through
`mcp__github_workflows__task_manage` with action `plan` and one typed `task`.
Set `assignment.mode` to `discover` or `verify`; do not send a redundant task
role. A discovery assignment's `shard_id`, `area`, and `paths` register the
linked shard atomically. Task transitions then maintain the shard lifecycle, so
do not repeat running, partial, complete, or failed states through
`audit_record`. Use an explicit shard record only for skipped or supervisor-owned
work without a task.
Before registration, verify every assigned path exists in the immutable audit
worktree and still belongs to the intended shard. Derive test surfaces from the
current inventory and repository configuration rather than remembered or
guessed filenames. Reconcile every issue lead against the current supervisor
history snapshot and live native closing-PR relationships; never assert that no
implementation PR exists from stale lead notes.
For notebook evidence, identify the repository-relative `.ipynb` path, stable
cell index, and a unique symbol or text anchor. Do not assign serialized notebook
line ranges: `read_file` cannot page notebooks by offset, and JSON line numbers
are not stable source locations.
Use the returned server-generated task ID and task reference, then launch one
`gh-audit-repo-worker` per selected shard according to
`mcp__github_workflows__run_status`, with only this prompt:

```text
Task ref: <task-ref-returned-by-task-manage>
```

Copy the returned task reference exactly; never shorten or reconstruct it.
After an accepted launch, immediately mark that task running before waiting.
If the worker returns before that receipt is recorded, persist its result
directly and let the server recover the missing start transition.

Workers inspect the whole assigned shard and return structured candidates plus
rejected leads and coverage gaps. A finding that belongs to shared runtime is
transferred to the `area/shared-core` queue and never published under the
entrypoint area. Candidates identify any matching open issue and recommend
`update-existing`, `protected-existing`, `duplicate-existing`, or `new`.
`MCP_UNAVAILABLE` from any discovery or verification worker suspends every
worker and the complete run under
`github-access.md`; preserve all
completed observations and pending assignments for resume.
`EXECUTION_BLOCKED` is an approval-denial circuit breaker. Record the current
attempt with `task_manage` action `fail` and note `execution-blocked`, do not retry it
during this invocation, and continue independent queued shards or validations.
When no independent material work remains, pause the run once with the blocking
reason. A later YOLO invocation reconciles live state before creating at most
one new numbered attempt.
Every returned actionable candidate must cite current-SHA source symbols plus
callers, tests/configuration, or other code evidence sufficient to prove
reachability and impact.
Workers reread the inventory before every version-dependent conclusion. They
may request missing context or propose structured runtime validation but remain unable to execute
commands. A proposal states a hypothesis, small synthetic setup, observable
assertion, confirming/disproving outcomes, and focused pytest selectors or a
Python-probe design rather than a general shell command.

Workers have 56 working turns and eight reserved reporting turns within their
64-turn limit. Return a compact structured result containing status
(`complete`, `partial`, `CONTEXT_REQUEST`, `MCP_UNAVAILABLE`, or
`EXECUTION_BLOCKED`), coverage
cursor, remaining scope, candidates, rejected leads, gaps, documentation use,
and validation proposals. Do not emit publication-ready issue bodies. If the
shard cannot be completed safely within the budget, return `partial`; the
supervisor records it and creates a continuation attempt rather than risking a
truncated report.

After a worker completes, record the attempt and integrate its result before
backfilling the lane. Existing workers continue within the remaining material
budget. A periodic bounded wakeup reconciles task state if a completion
notification is delayed. Retain each complete or partial report as this run's
observation.

## 6. Consolidate and verify

Within each completed publication area, consolidate shard candidates by root
cause and desired outcome. Publish every independently verified high- or
medium-confidence finding, including low-priority cleanup. Apply the shared
grouping rules before verification: group only findings with the same area,
type, priority, named maintenance outcome, and cohesive one-PR implementation
surface. Preserve distinct evidence and observable required outcomes for every
member. Do not combine unrelated bugs merely to reduce issue count, and do not
emit tiny separate dead-code, redundancy, or documentation issues when one
coherent group is easier to implement and review. Compare every group against
the complete GitHub history view, prior observations, current run state, and
already published areas.

Before giving a consolidated survivor to a verifier, apply the supervisor-gated
discovery-validation procedure in section 7 when it is safe and material. Add
the resulting artifact path and interpretation, or the reason validation was
unsuitable, to the verifier envelope.

Give every consolidated survivor to a fresh `gh-audit-repo-worker` in `verify`
mode according to the result-first scheduler, with the candidate, immutable
worktree, area/shard contracts, compact GitHub history snapshot,
inventory, prior observations, focus/guidance, and relevant documentation MCPs.
Put the complete canonical candidate object in `assignment.candidate`. The
server derives `assignment.candidate_fingerprint`; never calculate or submit a
fingerprint. New verify assignments without one canonical candidate are
rejected before launch.
The verifier must independently confirm
the current-SHA code path, reachability, impact, confidence, taxonomy, one-PR sizing, required outcomes,
duplicate status, and the proposed existing/new disposition. Reject
already-fixed, covered-by-PR, speculative, style-only, or insufficiently
distinct findings. A matching protected issue remains coverage—not a new issue
or an update. Reject rather than publish/update when code is inaccessible,
ambiguous, or not independently verified.

If the verifier proposes a materially independent probe, apply section 7 again
before accepting its verdict and checkpoint the second result with the
candidate fingerprint.

Require the fresh verifier to copy the server-owned candidate fingerprint into
its completed report, and store the result under that unit key. Prior
observations may guide questions but never substitute for independent
current-SHA proof. Refresh every GitHub-dependent disposition.
The verifier returns evidence, disagreements, required outcomes, and concise
publication facts, not a rendered issue body. The supervisor renders the final
title and body centrally after accepting the verdict.
