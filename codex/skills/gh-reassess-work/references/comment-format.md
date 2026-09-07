# Work reassessment comment convention

## Purpose

The public comment is an exceptional, concise maintainer note for an actionable
finding, not an audit record. Issue comments carry only premise or accepted
scope corrections; PR comments carry only implementation findings. Do not
create or update one when reassessment finds no actionable problem. A clean
result is expressed only through the applicable `ready-to-merge` label. When a
comment is warranted, it should answer only:

1. Does the issue or proposed change make sense, and why?
2. What does the implementation resolve or fail to resolve?
3. What, if anything, should happen next?

Keep detailed requirement matrices, immutable SHAs, label transitions, tool
activity, validation commands, worker comparisons, and workflow limitations in
the internal assessment and final run report.

## Managed markers

The canonical first line is the hidden marker:

```html
<!-- codex:github-work-reassessment:v1 -->
```

Recognize this legacy marker when locating an existing issue comment:

```html
<!-- codex:github-issue-reevaluation:v1 -->
```

Only comments owned by the authenticated user are managed. Treat either marker
as the same managed slot and stop when one artifact has multiple matching
comments. Every created or updated comment uses the canonical marker exactly
once as its first line.

## Issue comment

Use an issue comment only for an actionable premise or accepted-scope
correction not already represented in the issue body. Do not put PR-specific
implementation defects or progress here.

Use this adaptive format:

```markdown
<!-- codex:github-work-reassessment:v1 -->
## Issue reassessment

<Two to four sentences explaining whether the issue makes sense, what the linked PR resolves, and any material caveat. Mention PRs as #N.>

### What remains

- <Only an unresolved requirement, concrete defect, or necessary scope correction.>

### Validation

<One short result, such as “Relevant dataset tests: 63 passed.”>

**Next step:** <One specific action.>
```

The title and opening paragraph are required. Every published comment must
identify a material remaining requirement, defect, scope correction, or
uncertainty requiring maintainer action. Include `What remains` only when it
clarifies that finding. Include `Validation` only when its result explains the
finding or uncertainty. Include `Next step` when an action remains. Never post
or update a comment solely with “no further work,” successful validation, or an
equivalent positive status report.

When all requirements are satisfied, summarize the implementation once rather
than listing every satisfied requirement. When scope is corrected, state the
correction and its reason once. When a PR is blocked by another linked issue,
name that issue and its concrete remaining problem without discussing label
mechanics.

## Pull request comment

For an actionable implementation finding on a linked or unlinked PR, use:

```markdown
<!-- codex:github-work-reassessment:v1 -->
## Pull request reassessment

<Two to four sentences explaining what the changes do, whether they make sense, and the material correctness result.>

### What remains

- <Only a concrete defect or necessary correction.>

### Validation

<One short result.>

**Next step:** <One specific action.>
```

Apply the same omission rules as the issue comment.

## Resolved comments

Delete an owned managed PR comment after its implementation finding is fully
resolved. Delete an owned managed issue comment only after its correction is
obsolete or incorporated into the issue body. Until then, retain that issue
comment because implementation workflows consume it as accepted scope. Never
edit a resolved comment into a success note.

## Style and length

- Prefer the fewest words that communicate the actionable finding; never exceed
  300 words.
- Use direct project language and ordinary maintainer phrasing.
- Mention a PR once as `#N`; keep commit hashes in the internal report.
- State conclusions with their reason: “This makes sense because …” or “This
  does not address … because …”.
- Include only facts that change understanding, confidence, or the next action.
- Combine overlapping evidence instead of repeating it across sections.
- Keep status-label disposition, workflow names, agent/tool details, MCP/CI
  policy, repeated test runs, and internal classifications out of the public
  comment.
- Use file paths, symbols, and test counts only when they materially clarify the
  conclusion. Prefer a result summary over a long command.
- Cite source locations only as repository-relative files and, optionally,
  named symbols. Never publish line numbers, line ranges, or commit-pinned line
  links; keep exact lines and immutable SHAs in the private assessment.

An identical rendered body is a no-op. A clean reassessment with no obsolete
managed comment is also a comment no-op. Updating a warranted managed comment
replaces its prior snapshot while GitHub edit history remains the audit trail.
