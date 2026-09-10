## Stage 4: run bounded implementation rounds

Use the round's validation plan built during environment selection and confirm
it still matches inspected evidence before registering the assignment. Copy
repository-owned validation commands exactly; do not add
files to their argument lists. Derive any file-specific check from the file's
actual shebang, language configuration, and syntax rather than its extension or
name. Run a cheap supervisor-added syntax or static check against the recorded
pre-edit SHA before making it a completion gate. A check that already fails at
that SHA is an informational baseline limitation, not required worker
validation. Do not invent broad test, formatter, interpreter, or compiler
commands when the repository does not establish them.
For verification-only, include only commands proven not to rewrite the
worktree; never assign pre-commit or another auto-fixing command. If adequate
validation requires a potentially mutating command, use implementation mode.

Before launching any round that must push, perform a non-mutating,
non-interactive authenticated push preflight for the exact remote, refspec, and
lease, using `GIT_TERMINAL_PROMPT=0` and `git push --dry-run --no-verify`. Do not
inspect or inject tokens. Distinguish authentication failure from a ref/lease
rejection; if no sanctioned push mechanism is available, stop before assigning
implementation rather than letting the worker discover it after editing.

Put `execution_environment` in every assignment. It contains `mode: native`,
`mode: isolated`, or `mode: shared` plus `pythonpath`, an ordered list of
existing project-relative source roots. Do not put absolute paths, lock
contents, or environment details the worker can derive from the mode in the
assignment. Record mode, lock ownership, and selected sync groups in the
supervisor ledger. Workers invoke the assigned `.venv` interpreter and tools
directly. Prefix `UV_NO_SYNC=1` only for a documented wrapper known to invoke
nested uv. In shared mode, the worker expands each assigned project-relative
root against the verified assigned worktree and sets the resulting absolute
`PYTHONPATH`; isolated mode has no `PYTHONPATH` override.

Register the complete round assignment with
`mcp__github_workflows__task_manage` using action `plan` and a typed `task`.
Include non-empty `issues` entries with `number`, non-empty compact string
`snapshot`, and `accepted_scope`. For new work use
`pull_request: {"state": "none"}`; for an existing PR use an extensible object
whose `state` is `open`, whose four round fields follow the Stage 3 contract,
and which contains the available PR evidence. Also include `worktree`,
`branch`, full
`rebased_base_sha`, `remote_lease`, `round_objective`, `acceptance_condition`,
`repository_instructions`, `validation_plan`, and `execution_environment`.
Use an empty `validation_plan` only when preflight found no safe repository-owned
test, formatter, interpreter, or compiler command, and record that limitation;
never invent a command merely to make the list non-empty.
Repository identity, documentation, and reference paths are server-derived.
Use its returned server-generated task ID and task reference, then launch
`gh-implement-issue-worker` with fresh context and:

```text
Task ref: <task-ref-returned-by-task-manage>
```

The spawn message contains exactly that line. Do not add the assignment, task ID,
or instructions to call supervisor-only workflow tools. Workers return their
recoverable checkpoint in the final report; the supervisor alone persists that
report through `task_manage` action `checkpoint` or `complete`.
Copy the returned task reference exactly. Immediately after an accepted launch,
call `task_manage` action `mark_running` before waiting; if launch fails, record
`abandon` while the task is queued. Persist the returned result before
interpreting it, then use `integration_begin` and `integration_end` around
supervisor synthesis. Identical lifecycle retries are safe.

Maintain a ledger containing unit, semantic task ID, task reference, anchor,
issues, grouping rationale,
branch/worktree, PR, round, scope sources, original/base/current SHAs, claim
ownership, per-issue coverage, checkpoint, and finalization state. One worker
owns a unit at a time.

Each round has 128 turns: 120 working turns and eight reserved checkpoint
turns. Workers return `DRAFT_READY_FOR_SUPERVISOR`, `CONTINUE_REQUESTED`,
`SPLIT_REQUESTED`, `CORRECTION_NEEDED`, `BLOCKED`, `MCP_UNAVAILABLE`,
`EXECUTION_BLOCKED`, or `NO_IMPLEMENTATION`. `EXECUTION_BLOCKED` is reserved
for a Qwen approval denial. `MCP_UNAVAILABLE` suspends the complete workflow before
any further unit work.
Review every checkpoint against every issue's accepted scope, unit cohesion,
the complete base diff, validation, research semantics, and confidential-data
safety.

The worker performs a cohesion preflight before edits. For a
`SPLIT_REQUESTED` checkpoint with no changes, independently verify the proposed
partition, release no claims, create separate unit ledgers/worktrees, and then
launch one worker per new unit within the concurrency limit. If the current
diff already represents some issues, retain those issues and that diff in the
current unit and split only untouched issues. Never copy or share an edited
worktree between units. When changes entangle the proposed partitions, send a
correction that restores one cohesive unit. If a safe partition requires a
product, scientific, or scope decision not authorized during preflight,
preserve valid partial work, block that unit without asking, and continue
independent units.

Continue the same task with one bounded objective, return an exact correction,
advance to draft verification, or terminate and finalize. Two rounds blocked by the
same explained cause terminate the affected unit without another question and
suspend only when no safe independent progress remains. `EXECUTION_BLOCKED`
does not receive a second round in the same invocation: record the attempt with
`task_manage` action `fail` and note `execution-blocked`, continue independent
units, then pause once when they are exhausted. A later YOLO invocation
reconciles state before creating at most one new numbered attempt.
