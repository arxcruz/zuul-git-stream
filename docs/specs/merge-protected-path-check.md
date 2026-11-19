# Spec: /merge refuses a branch that changes a protected path

## Purpose

`/merge` runs in a sandboxed session, and the sandbox cannot rewrite its
protected paths, so `git switch main` fails halfway on a branch that changes
one.  Make the skill notice this before the gate and tell the human to merge
from a terminal outside Claude Code, instead of running minutes of tests and
then failing.

Observed: `/merge` of `chore/sandbox-settings` failed at `git switch main`
because the sandbox refused to unlink `.claude/settings.json`; nothing was
merged (AGENTS.md "Permissions and sandbox", decision 15, backlog S2).

## Scenarios

1. The branch's diff against `main` touches `.claude/settings.json`: `/merge`
   refuses before the gate, does nothing else, lists the matching path, and
   says "merge from a terminal outside Claude Code".  (Observed failure.)
2. The diff touches several protected paths: all matching paths are listed,
   one per line.
3. The diff touches none: the skill goes on exactly as before (merge-tree
   check, gate, merge, delete, gate on `main`).
4. The branch renames a file out of a protected path (`git mv .mcp.json x`):
   the old path is listed, so it is refused.  This is why the command has
   `--no-renames`.
5. The branch deletes a protected path: refused, the same as a change.
6. The diff command itself fails (for example `main` does not exist): report
   the error and stop; do not guess that the branch is safe.
7. Another refusal also applies (dirty tree, on `main`, conflict): the
   existing refusals are unchanged; the protected-path refusal adds one more
   condition, it replaces none.  If any existing refusal applies, the skill
   stops; which one is reported first is not specified.

## The protected paths

The skill matches each line of the diff against this list.  A path ending in
`/` matches everything under it; an entry with `*` is a glob, and `*` matches
within one path segment (never across a `/`); any other entry matches that
exact path.  The table is relative to the repository root, which is what
`git diff --name-only` prints.  A nested path such as `sub/.mcp.json` is not
matched (out of scope: it is unknown whether the sandbox protects it), and
case variants such as `.MCP.json` are out of scope too.

| Path | Status |
|------|--------|
| `.claude/settings*.json` | `.claude/settings.json` is **observed**: blocked `git switch main` (Nov 19).  The glob comes from the sandbox docs as quoted in AGENTS.md, so any other name it matches (such as `.claude/settings.local.json`) is inferred |
| `.mcp.json` | Inferred: protected per the sandbox docs, not probed |
| `.claude/skills/` | **Observed** (Nov 19): `touch` of a new file and `mv` of `merge/SKILL.md` both failed with "Operation not permitted"; inferred only that `/merge`'s `git switch main` fails on it, not probed under `/merge` |
| `.claude/agents/` | **Observed** (Nov 19): `touch` of a new file and `mv` of `branch-reviewer.md` both failed with "Operation not permitted"; `/merge` not run on it |
| `.claude/hooks/` | **Observed** (Nov 19): `touch` of a new file and `mv` of `no-commit-on-main.py` both failed with "Operation not permitted"; inferred only that `/merge`'s `git switch main` fails on it, not probed under `/merge` |

An inferred entry may refuse a branch that would have merged.  That is the
safe side: the human merges it by hand, as the manual workaround already
says.  Probing an entry and moving it to "observed" is welcome and goes in
this table.

**`.git/hooks` and `.git/config` are not in the list.**  They were in the
request, but git never lists anything under `.git/` in a diff, so
`git diff --name-only` cannot see them and an entry would never match.  The
related known case, the `git branch -d` config-lock warning, has its own
backlog item.

This branch edits `.claude/skills/merge/SKILL.md`, so by the rule above its
own merge must be done by hand.  That is expected, and the first real use of
the check.  Observed (Nov 19): the Edit tool wrote
`.claude/skills/merge/SKILL.md` in a sandboxed session, so the sandbox's write
protection of `.claude/skills/`, `hooks/` and `agents/` applies to Bash, not
to the Edit tool.  The implementer therefore edits the skill with Edit.

## The command

`git diff --name-only --no-renames main...HEAD`

- Three dots: the diff from the merge base, i.e. what the branch changes.
- `--no-renames`: a rename lists both the old and new path (and so a move out
  of a protected path is caught).  Without it only the new name is listed.
- It is read-only.  Run it in section 1 of the skill ("Refuse, or go on"),
  before the gate, as one more condition.

## Refusal text

The skill says, in its own words but containing the phrase "merge from a
terminal outside Claude Code", that the branch changes protected paths the
sandbox cannot rewrite, lists the matching paths, and gives the manual
steps already in AGENTS.md (gate on the branch, then in the original
checkout `git switch main`, `git merge --no-ff <branch>`, `git branch -d
<branch>`, the gate on `main`, `git switch --detach`) or points to that
section.  It does not run them.

## Fires on

Every `/merge` run, in section 1, after the existing refusals have passed and
before `scripts/check.sh`.

## Out of scope

- Probing `.mcp.json` and the other `settings*.json` names under a real
  `/merge` remains a backlog item.
- Making the merge itself work for such branches (for example by excluding
  the paths from the sandbox's protection); the sandbox is the boundary, see
  decision 15.
- `.git/hooks` and `.git/config` (above), and other protected paths the
  sandbox may add later; the list is edited by hand.
- Any change to `/spec`, `/review` or the hooks.
- The `git branch -d` warning item: untouched.

## Done means

- `.claude/skills/merge/SKILL.md`: the new condition in section 1, the path
  table (or list), the refusal text, and the new command in `allowed-tools`.
- **`tests/test_merge_skill.py` changes deliberately, not by loosening:**
  - `TOOLS` gets one new entry, `'Bash(git diff --name-only --no-renames
    main...HEAD)'`, inserted right after the `git merge-tree` entry (the
    skill lists it in the same order as the flow).  The exact-list test
    `test_its_tools_are_exactly_the_approved_ones` stays an exact comparison.
  - A `DIFF` constant for the command, added to
    `test_every_command_of_the_flow_is_allowed` and to the hook test's
    parametrized list (the branch guard must let it through).
  - `test_none_of_its_tools_can_push_or_destroy` runs unchanged; the new
    pattern contains none of its forbidden words.
  - New text tests: the skill names the command, the phrase "merge from a
    terminal outside Claude Code", every path of the table, and the command
    comes before the first `scripts/check.sh` in the body.
  - New real-git tests in a temporary repository: the command lists a changed
    protected path (scenario 1), several (2), a rename's old path (4), a
    deleted one (5), and nothing for an unrelated branch (3).
  - The docstring's "exactly the tools approved" stays true; its mention of
    the manual dry run stays.
- Docs, in the same branch: remove the S2 item from `docs/backlog.md`; in
  AGENTS.md "Permissions and sandbox" change "the skill does not check for it
  yet (backlog)" to say the skill now checks, keeping observed and inferred
  apart, and add the probe result there: `.claude/skills/` and
  `.claude/hooks/` are observed write-protected for Bash (creating a file and
  renaming an existing one both failed with "Operation not permitted",
  Nov 19), while that `/merge` fails on them is still inferred; the same
  sentence at the end of decision 15's "Consequence" in
  `docs/decisions.md`.  Check `tests/test_permissions_docs.py` for assertions
  on the old wording and update them deliberately, saying which.
- No contract test: Zuul sees no change.
- `scripts/check.sh` green.

## Constraints

- No new tool beyond the one read-only `git diff` pattern; nothing that can
  write, push or destroy.
- Never weaken a test assertion; `TOOLS` stays an exact list.
- The branch name in commands is still the current branch, never the
  argument.
- A failure of the check is a refusal, never a pass.
- Standard library only applies to `git_stream/`; this change touches none of
  it.
