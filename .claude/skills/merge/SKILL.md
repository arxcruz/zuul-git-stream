---
name: merge
description: Merge the current branch into main with --no-ff after the full gate, delete it with the safe branch -d, and run the gate on main. Typing /merge is the approval to merge. It never pushes.
argument-hint: "[branch]"
disable-model-invocation: true
allowed-tools: Bash(git branch --show-current) Bash(git status --short) Bash(git log --oneline main..HEAD) Bash(git log --oneline -3) Bash(git merge-tree --write-tree main HEAD) Bash(git diff --name-only --no-renames main...HEAD) Bash(scripts/check.sh) Bash(git switch main) Bash(git merge --no-ff -m *) Bash(git branch -d --end-of-options *)
---

Merge the current branch into `main`.  The user typing `/merge` is their
approval of this merge.

- Current branch: !`git branch --show-current`
- Uncommitted changes (empty means a clean tree): !`git status --short`
- Commits on this branch that are not on main: !`git log --oneline main..HEAD`
- Argument: `$ARGUMENTS`

## 1. Refuse, or go on

Refuse and say why, doing nothing else, if any of these holds:

- the current branch is `main` or `master`, or empty (a detached HEAD);
- the tree is dirty (uncommitted changes);
- the argument is not empty and does not equal the current branch: it must
  equal the current branch, so if it names another one, tell the user to
  switch to that branch first;
- the branch has no commits ahead of `main`: there is nothing to merge;
- the merge would not be clean: run `git merge-tree --write-tree main HEAD`.
  Exit status 1 means a conflict; name the files from its output.  Any other
  failure is an error to report;
- the branch changes a protected path, which the sandbox cannot rewrite, so
  `git switch main` would fail halfway.  Run
  `git diff --name-only --no-renames main...HEAD` and compare each line with
  the table below.  If any line matches, refuse, list the matching paths, and
  say: "merge from a terminal outside Claude Code" (the manual steps are in
  AGENTS.md, "Permissions and sandbox").  Do not run them.  If that command
  fails, report the error and stop: never assume the branch is safe.

| Protected path | Matches |
|----------------|---------|
| `.claude/settings*.json` | `*` matches within one path segment, never across `/` |
| `.mcp.json` | exactly that path |
| `.claude/skills/` | everything under it |
| `.claude/agents/` | everything under it |
| `.claude/hooks/` | everything under it |

The paths are relative to the repository root, as the command prints them.  A
nested path such as `sub/.mcp.json` does not match, and neither do case
variants.  The reasons, and what is observed versus inferred, are in
`docs/specs/merge-protected-path-check.md`.

In every command below, `<branch>` is the current branch name from above,
never the argument.

## 2. Gate on the branch

Run `scripts/check.sh` with the Bash tool's `timeout` set to 600000: it takes
minutes, and the default timeout would cut it off.  If it is red, show the
end of its output and stop.  The branch is not merged.

## 3. Merge

1. `git switch main`
2. `git merge --no-ff -m "Merge <branch>: <summary>" --end-of-options <branch>`
   where `<summary>` is one line, in your own words, of what the branch does.
   If your instructions give an attribution line for commits, add it as a
   further `-m` argument.
   If the merge fails, stop and report the error; do not try to repair it.

## 4. Delete the branch

`git branch -d --end-of-options <branch>`.  If git refuses, stop and report
it.  Never use `-D`, `-f` or `--force`.

## 5. Gate on main

Run `scripts/check.sh` again, with `timeout` 600000.  If it is red, report
only: show the end of its output and say the merge is on `main` and the gate is
red.  Do not undo the merge or change anything else; the user decides.

## 6. Report and stop

Show the result line of each gate run, `git log --oneline -3` and
`git status --short`.  Say the branch is merged and deleted and that nothing
was pushed.  Never push; that is the user's decision.
