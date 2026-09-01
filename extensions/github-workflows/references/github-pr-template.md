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

## Deviations / Non-goals

<Accepted scope correction, excluded work, or `None`.>

## Risks or follow-up

<Remaining risk or `None identified`.>

Closes #<issue>
Closes #<other-issue>
```

Create one `Issue coverage` subsection for every covered issue. Map its task
checkboxes to that issue's current `Required outcome` requirements, incorporating
any authoritative managed reassessment correction. Cite concrete evidence for
each checkbox. A complete PR has every required outcome checked. A usable
incomplete draft retains unchecked outcomes.

Use prose for a single statement and bullets when multiple distinct items are
easier to scan. Issue coverage remains a task list because each checkbox tracks
a separate accepted requirement.

When a PR includes a `Validation` section, record checks actually run and their
observed results. Routine test-passing expectations remain implicit.

Add one exact `Closes #N` line for each issue whose complete required scope is
implemented. Preserve compatible existing closing references. These lines
create the issue-to-PR relationship represented by GitHub's Development
section.
