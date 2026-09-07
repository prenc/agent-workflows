# GitHub implementation pull request template

Use this description for every pull request produced or continued by the
GitHub implementation workflows. Begin with the exact provenance marker when
the active workflow defines one; otherwise begin with `## Summary`.

```markdown
## Summary

<What changed and why, concisely.>

## Issue coverage

### #<issue>: <title>

- [x] <Required outcome> — <implementation evidence>

### #<other-issue>: <title>

- [x] <Required outcome> — <implementation evidence>

## Changes

<Important implementation changes and relevant paths or components.>

Closes #<issue>
Closes #<other-issue>
```

Insert either of these optional sections after `Changes` only when it has
material content:

```markdown
## Deviations / Non-goals

<Accepted scope correction or excluded work.>
```

```markdown
## Risks or follow-up

<Remaining risk or follow-up.>
```

Create one `Issue coverage` subsection for every covered issue. Map its task
checkboxes to that issue's current `Required outcome` requirements, incorporating
any authoritative managed reassessment correction. Cite concrete evidence for
each checkbox. A complete PR has every required outcome checked. A usable
incomplete draft retains unchecked outcomes.

Use prose for a single statement and bullets when multiple distinct items are
easier to scan. Issue coverage remains a task list because each checkbox tracks
a separate accepted requirement.

Omit `Deviations / Non-goals` when there is no material deviation or excluded
scope. Omit `Risks or follow-up` when there is no material risk or follow-up.
Never include empty sections or placeholders such as `None` or `None identified`.

Do not include validation information anywhere in the PR description. Omit
test commands, test counts, check results, CI status, validation summaries, and
dedicated validation sections. Implementation evidence in `Issue coverage`
describes the delivered code or behavior, not how it was tested. Validation
belongs in private workflow state and the user-facing completion report.

When source locations materially clarify implementation evidence, cite only a
repository-relative file and, optionally, a named symbol, such as
`src/package/module.py` or `Package.method`. Never include line numbers, line
ranges, or commit-pinned line links in the PR description. Keep exact lines and
immutable SHAs in private workflow evidence.

Add one exact `Closes #N` line for each issue whose complete required scope is
implemented. Preserve compatible existing closing references. These lines
create the issue-to-PR relationship represented by GitHub's Development
section.
