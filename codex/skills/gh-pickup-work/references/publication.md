# Publish and Finalize GitHub Work

Use this stage only in default implementation mode.

## 7. Publish the one PR safely

Immediately before pushing, refresh the remote branch/PR, every issue, and the
base. Stop rather than overwrite another actor when a remote head differs from
the recorded lease. If base advanced, rebase again and rerun affected checks.

Push an existing branch only with an exact explicit lease:

```text
git push --force-with-lease=refs/heads/<head>:<lease-sha> \
  <remote> HEAD:refs/heads/<head>
```

For a new branch, recheck that the remote ref is absent and perform a normal
first push. Update the one selected PR or create exactly one new PR. As soon as
a new PR exists, apply PR `in-progress` and confirm it.

Build the PR description from [the PR template](pr-template.md).
Preserve the Qwen marker when continuing a Qwen-owned PR. Begin a new Codex PR
with `## Summary`. Omit optional sections that have no material content, and
keep all validation information out of the PR description.

Refresh every covered issue and confirm native linkage through
`closed_by_pull_requests.references` or its MCP equivalent. When unavailable,
record `verified-closing-reference` only after confirming the exact closing
line and default-branch target. Correct the PR body once when linkage is
missing; a remaining failure blocks final publication.

Confirm through MCP that the PR points to the pushed SHA. Never query CI or
status rollups. Before publication, obtain complete current PR labels and
reconcile the derived taxonomy from the complete covered-issue set under the
shared PR-label convention. If complete membership or read-back is unavailable,
preserve labels and report that mutation as blocked. A usable incomplete handoff stays draft: apply `partial` to the
PR and each issue whose accepted scope has usable pushed but incomplete work,
and retain unchecked coverage in the PR body. Do not split the unit during
publication.

Only when every selected issue is complete, local validation passes, linkage
is verified, the PR points to the pushed SHA, and no known blocking review or
correctness issue remains: mark the PR ready for review, remove `partial` from
the PR and every issue, remove all issue and PR `in-progress`, and apply PR
`ready-to-merge`.

If inherited work already satisfies all of those conditions, do not manufacture
an implementation change or publish a success comment. Preserve matching PR
content and metadata and apply only a missing `ready-to-merge` label after
releasing this run's temporary claims.

For a usable incomplete handoff, retain appropriate `partial` labels and remove
all `in-progress` labels. Immediate continuation within the same active run may
retain its recorded claims. Finalization removes every activity lock created by
the run.

## Stop conditions and report

Suspend under the shared MCP policy on an availability,
authentication, or authorization failure. Stop without pushing on unstable or
incompatible selection, active issue ownership, ambiguous candidate ownership,
absent scratch confirmation after no reusable work is found, unsafe local
state, changed remote head, unresolved authority-sensitive conflict, required
dependency/scientific decision, or unsafe compute requirements.

On every terminal path after claiming, refresh every issue and the PR, release
this run's `in-progress`, reconcile `partial` from usable pushed work, and read
the result back. Report any cleanup blocked by authentication, authorization,
interruption, or another actor.

Keep the user-facing report concise and omit empty categories. Report selectors and complete resolution, mode, full issue set and anchor,
cohesion decision, reuse searches and candidate selection, issue/PR URLs,
branch/worktree reuse or creation, confirmed remote-head state, accepted
scope and completion per issue, inherited-work verification, changes, local
validation, lease result, conflicts/resolutions, every issue and PR status-label
lifecycle, native linkage per issue, retained artifacts, and remaining risks.
Claim completion only after every selected issue is complete, required local
validation passes, and the one PR points to the confirmed pushed SHA.

Keep exact original, base, rebased, and pushed SHAs in private workflow state.
In the user-facing report, state that the remote PR head was verified. Include a
short SHA when it materially helps identify a commit, and provide a full SHA
when the user explicitly requests it.
