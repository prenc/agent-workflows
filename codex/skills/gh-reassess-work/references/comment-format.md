# Work reassessment comment convention

## Purpose

The public comment is a concise maintainer note, not an audit record. It should
answer only:

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

The title and opening paragraph are required. Include `What remains` only when
something material remains. Include `Validation` only when its result adds
useful confidence or explains uncertainty. Include `Next step` only when an
action remains; for completed issue scope, a short sentence such as “No further
work is needed for this issue” may close the opening paragraph instead.

When all requirements are satisfied, summarize the implementation once rather
than listing every satisfied requirement. When scope is corrected, state the
correction and its reason once. When a PR is blocked by another linked issue,
name that issue and its concrete remaining problem without discussing label
mechanics.

## Unlinked PR comment

For a supplied PR with no attached issue, use:

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

## Style and length

- Target 80–180 words and never exceed 300 words.
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

An identical rendered body is a no-op. Updating the managed comment replaces
its prior snapshot while GitHub edit history remains the audit trail.
