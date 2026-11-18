---
name: review
description: Review the current branch with the read-only branch-reviewer subagent. Optional argument: the base branch or commit to diff against (default main).
argument-hint: "[base-branch-or-commit]"
disable-model-invocation: true
context: fork
agent: branch-reviewer
allowed-tools: Bash(git branch --show-current) Bash(git rev-parse *) Bash(ls docs/specs)
---

Review this branch before it is merged.

- Current branch: !`git branch --show-current`
- HEAD: !`git rev-parse --short HEAD`
- Specs in the repository: !`ls docs/specs`
- Base to diff against: `$ARGUMENTS` (if that is empty, use `main`)

Follow your instructions: read AGENTS.md, then the spec for this branch if
one exists (its name is the branch name without the `<type>/` prefix), then
review `git diff <base>..HEAD`.  Report the findings ordered by importance,
and say "nothing found" for each clean category.  Change nothing.
