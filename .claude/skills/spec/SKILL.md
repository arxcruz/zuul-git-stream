---
name: spec
description: Turn a feature idea into a spec in docs/specs/<name>.md. Reads what the repo already says, drafts the spec, asks the open decisions one at a time, and saves it on a feat/<name> branch. Writes no code and no tests.
argument-hint: "<feature idea>"
disable-model-invocation: true
allowed-tools: Bash(git branch --show-current) Bash(git status --short) Bash(ls docs/specs) Bash(git switch -c feat/*) Read AskUserQuestion Write(docs/specs/*)
---

Write a spec for this feature idea: `$ARGUMENTS`

- Current branch: !`git branch --show-current`
- Uncommitted changes (empty means a clean tree): !`git status --short`
- Specs in the repository: !`ls docs/specs`

If `$ARGUMENTS` is empty, ask what the idea is and stop until you have it.

Refuse, and say why, if the current branch is not `main` or the tree is dirty
(uncommitted changes).  A spec branches from `main`; tell the user to switch to
`main` or to commit or stash their work, and do nothing else.  Do this before
anything below.

## 1. Read what the repo already says

Read `AGENTS.md` (the hard rules and the definition of done), then
`docs/roadmap.md`, `docs/backlog.md`, `docs/decisions.md`, `tests/pending.txt`
and every file in `docs/specs/` that touches the idea.

Summarize in plain words what they say about it: already planned, partly
specified, ruled out by a decision, or in conflict with a hard rule (no Zuul
changes, no Kafka, stdlib only, GitLab-compatible).  Name the file for each
point.  If they say nothing, say so.

## 2. Draft the spec

Use the format of `docs/specs/callback-url.md`, with the title
`# Spec: <short title>` and these sections, in this order:

- Purpose
- Scenarios (numbered, concrete, including the failure cases)
- Fires on (what triggers it; or the scope when nothing fires)
- Out of scope (what goes to `docs/backlog.md` instead)
- Done means (tests per scenario; a contract test in
  `tests/test_zuul_contract.py` if Zuul sees any change; the docs to update;
  the full gate green)
- Constraints

Keep the draft in the conversation.  Do not save it yet.

## 3. Decide the open points with the user

Ask each open decision as its own question with `AskUserQuestion`: 2 to 4
options, the one you recommend first and marked "(Recommended)", and a
sentence of reasoning.  One question at a time: never ask several at once, and
wait for the answer before the next.  After each answer, update the draft.

Do not ask what `AGENTS.md` or `docs/decisions.md` already settles; state it as
a constraint.

## 4. Save it

Choose `<name>`: a short kebab-case slug of the idea; confirm it with the user
if it is not obvious.  The branch is `feat/<name>` and the file is
`docs/specs/<name>.md` (the branch name without its `<type>/` prefix, which is
how `/review` finds the spec).

1. If `docs/specs/<name>.md` already exists, do not overwrite it; ask for
   another name.
2. `git switch -c feat/<name>`.
3. Write `docs/specs/<name>.md`.

Do not commit: the user reviews the spec first.  Anything you found that is
outside this idea goes to `docs/backlog.md` only if the user says so.

## 5. Stop

Say which branch and file you wrote, and offer to ask for a plan.  No code, no
tests, no plan until the user asks.
