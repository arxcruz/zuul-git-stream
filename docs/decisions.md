# Decisions and their reasons

Newest last.  Dates are 2026.

## Background: the Kafka driver (Nov 11–12)

The first approach was `../zuul-kafka-driver`: an out-of-tree Zuul
trigger driver that enqueued items when a JSON message arrived on a
Kafka topic, plus a podman demo with the same bundle scenario (refs
`refs/bundles/<id>`, `bundle-dispatch` reading the message back from
Kafka, "re-run failed").  It worked, but a custom driver won't be
accepted upstream and has to be kept in step with Zuul internals.  That
repo has uncommitted work (testing a change URL with Depends-On, a
GitHub `ci-tool`) the user wants left as is.

## 1. Fake a forge instead of writing a driver (Nov 17)

Zuul already knows how to react to forges.  A small service that speaks
a forge's protocol lets **stock Zuul** do the work: triggers, reporting,
Depends-On, speculative merging.  Nothing in Zuul changes.

## 2. GitLab, not GitHub or Gerrit

- GitLab's driver uses plain REST (about 10 endpoints) and a shared
  secret header for webhooks; easy to implement faithfully.
- Zuul's tests ship an HTTP fake GitLab (`tests/fakegitlab.py`), which
  confirmed the surface.
- GitHub's driver uses github3.py, GraphQL, app auth and checks: much
  larger.  Gerrit needs SSH stream-events and a different review model.

## 3. Merge requests as the unit of work

A bundle is an MR adding `bundle.yaml`, because MRs give everything for
free: comments (results, rerun requests), approvals (Zuul's verdict),
labels, Depends-On, and history per bundle in Zuul (`change` = iid).
Bundle data lived in git because the GitLab driver passes no arbitrary
payload to jobs (superseded by decision 16: it is now data in SQLite).  These MRs live only in git-stream, not in the user's
real GitLab (the user doesn't want dummy patches there).

## 4. Event-driven instead of periodic

The user's pipelines run daily on a timer whether or not there is a new
bundle.  The goal is: announce a bundle → test it → report → retry only
failures → escalate after N failures.  Downstream work (e.g. building
packages) can trigger on the approval event (`gl_merge_request` action
`approved`) instead of a timer; not implemented yet.

## 5. Two pipelines and a dispatch job for "run specific jobs"

Zuul can't select jobs per event.  The `bundle-dispatch` job runs first
and returns `zuul.child_jobs`; skipped jobs show as SKIPPED.
`bundle-test` runs everything; `bundle-rerun` (comment `rerun ...`)
runs what the comment asks.  Comments are read back from git-stream's
API because Zuul doesn't pass the comment text to jobs.

## 6. No Kafka (Nov 17)

The user didn't want Kafka in this approach.  Events arrive over HTTP
(`POST /api/stream`).  Keys and message ids were kept: they still help
any sender that doesn't track MR numbers or resends on errors.

## 7. Retries and escalation inside git-stream (Nov 17)

git-stream already receives Zuul's reports (as API comments), so it can
decide on retries without another service.  It counts failures per job
per MR version from Zuul's builds API (the source of truth), retries by
commenting like a human would, and escalates once per version.  A Zuul
job can't do this: jobs depending on a failed job never run.

## 8. Escalations shown on pages, Jira later (Nov 17)

The user uses Jira but doesn't want the integration now.  Escalations
are recorded in git-stream state and shown on `/escalations`, the MR
page and the demo UI.  `[retry] escalate` URLs (empty by default) are
the hook for a future Jira adapter.

## 9. One TOML config file (Nov 17)

All settings in one file (`tomllib`, stdlib, hence Python >= 3.11),
overridable by environment and CLI.  Unknown options are errors so typos
don't silently fall back to defaults.

## 10. Stdlib only, single process

Keeps the service trivially deployable and auditable.  The cost is the
production gaps listed in [roadmap.md](roadmap.md).

## 11. A SQLite metrics store, not a new state format (Nov 17)

`retry.py` already recomputes "how often did each job fail" from Zuul's
builds API on every report and then throws the numbers away.  A SQLite
file (`metrics.py`) records them as they happen: one row per completed
build (`run`), one per escalation, one per webhook delivery.  The
database is *additive* — it records job activity, separate from the forge
state in `state.db` (projects, MRs, notes).  It is a **log of what
git-stream saw**, not a
source of truth (Zuul is the source of truth); it exists to answer
"what failed, how often, over time" and to survive a restart without
asking Zuul.  Opt-in: no `[metrics] path` = off.  Stdlib `sqlite3`, one
writer (the retry policy) under a lock; the counting endpoints read through
their own connection (WAL, decision 17).

## 12. Forge state in SQLite, with a JSON fallback (Nov 18)

The forge's own state (projects, merge requests, notes, the deduplication
list, and the per-MR retry state) moved from a JSON file to a SQLite
database (`<data>/state.db`).  The JSON file was rewritten whole on every
change and the notes list grew without bound; a table per entity keeps it
flat and queryable.  The in-memory view remains authoritative while the
process runs and is persisted on every change, so the hot path is
unchanged.  `forge.py` has two interchangeable stores (`SqliteStore`,
`JsonStore`); SQLite is used when the `sqlite3` module is available and the
JSON file otherwise, so a host without it still works.  This is the forge
state, distinct from the metrics database in decision 11.

## 13. A Stop hook runs the fast checks (Nov 18)

`.claude/hooks/stop-checks.py` runs ruff and the fast parallel test loop when
the agent tries to finish a turn and code or test files changed, and blocks
the stop with the failure output.  Choices:

- **Skip on main/master**: commits there are already blocked, and the hook is
  about work in progress on a branch.
- **JSON `decision: "block"`** rather than exit 2: the reason is explicit
  and mandatory.
- **Loop guard by tree fingerprint** (HEAD, `git status`, changed files'
  contents) kept per session under `$TMPDIR`, on top of Claude Code's own cap
  of 8 consecutive continuations.  The `stop_hook_active` input is not read:
  the fingerprint says more.  A tree that passed is not re-checked (so a
  dirty branch does not cost 10 s on every chat turn); a tree that failed and
  did not change is let through, since another block cannot help.  Any change
  is re-checked.
- **Fails open**: a missing ruff, bad input or a check over 90 s
  (`STOP_HOOK_TIMEOUT`) prints a note and lets the agent stop; a broken hook
  must not trap it.
- **A missing venv blocks** (changed from fail-open when worktrees were
  added): `.venv` is git-ignored, so a fresh worktree has none, and an agent
  that finishes with no checks run looks like one that passed.  With a code
  change and no `.venv/bin/python` the hook blocks and names the AGENTS.md
  setup commands.  It blocks once per tree fingerprint, recorded as `no-venv`
  rather than `fail`, so once the venv exists the same tree is checked
  instead of being let through as "failed and unchanged"; the same tree again
  with no venv is let through with a note.  Docs-only changes, `main`/`master`
  and a detached HEAD stay silent.
- **Not the gate**: it skips the slow tests; `scripts/check.sh` decides
  "done".

## 14. Parallel sessions use sibling worktrees; the original stays detached (Nov 19)

Two Claude Code sessions in one folder trample each other's branch, index and
venv, so each gets its own git worktree.  Choices:

- **Sibling folders**, `../<repo>-<type>-<topic>`, made by
  `scripts/new-worktree.sh`.  Inside the repository a worktree would show up
  as an untracked directory in the original checkout (so `/merge` refuses on
  a dirty tree) and `ruff check .` there lints its files (both checked with
  a nested worktree); next to it, neither happens and `..` still means the
  same thing to the rules that name `../zuul-kafka-driver`.
- **One `.venv` per worktree.**  `.venv` is git-ignored, so a new worktree has
  none, and its editable install points at one checkout; a shared venv could
  silently test the wrong code.  The script makes it with the AGENTS.md setup
  commands (Zuul included, `NO_ZUUL` to skip), and the stop hook now blocks
  when it is missing (see decision 13).
- **The original checkout stays on a detached HEAD.**  Git lets only one
  worktree have `main` checked out, and `/merge` runs `git switch main`, so
  with `main` held by the original checkout no worktree could ever merge.
  Detaching it frees `main`.  The cost is that `no-commit-on-main.py` no
  longer protects that checkout, because it guarded only `main` and `master`:
  it now also refuses `git commit` on a detached HEAD.  A rebase, cherry-pick,
  merge or revert that stopped on a conflict is exempt, because git detaches
  HEAD for those on purpose and they are finished with `git commit`; the hook
  finds them by git's own marker files (`rebase-merge`, `rebase-apply`,
  `CHERRY_PICK_HEAD`, `MERGE_HEAD`, `REVERT_HEAD`) in each worktree's git
  directory.  `git push` is not guarded on a detached HEAD.
- **The base is `main`, resolved to a commit.**  In a clone with no local
  `main`, `git worktree add -b X PATH main` makes a local `main` and ignores
  `X`, so the script resolves local `main`, else `origin/main`, itself.
- **Not solved:** a session left on `main` after `/merge` blocks the other
  worktree's `git switch main` (backlog).  The demo's fixed ports allow it in
  one worktree only.

Trial, Nov 19: tried in a real worktree.  The script made the sibling folder
and its venv, and the editable install and the Zuul import resolve inside the
worktree.  The project hooks and skills load there; the stop hook ran ruff
there and blocked on a missing venv; the gate passed (625 passed, 27 xfailed);
the detached original refuses `git commit`.  `CLAUDE_PROJECT_DIR` is empty in
the Bash tool, but the hooks resolve their paths anyway.  Verified since:
`/merge` from a worktree works with the original checkout detached, and two
tasks (constant-time tokens, the connection cap) ran in parallel in two
worktrees and merged one after the other.  Memory carry-over is still
unchecked.

## 15. The agent runs in the Claude Code sandbox, with four deny rules (Nov 19)

The agent's Bash tool can run anything the user's account can.  The project
settings (`.claude/settings.json`) now confine it.  Choices:

- **The sandbox is on, and cannot be off by accident.**  `sandbox.enabled`
  limits writes to the working directory and `$TMPDIR`.  `failIfUnavailable`
  makes Bash fail when the sandbox cannot start, because a silent fallback to
  unsandboxed commands would look the same as a working setup.
  `allowUnsandboxedCommands: false` removes the retry outside the sandbox that
  `dangerouslyDisableSandbox` would otherwise offer after a failure.
- **`network.allowLocalBinding` is on.**  The tests start servers on port 0
  on localhost (the `forge` fixture), which the sandbox refuses by default;
  without it the gate cannot run in the agent's session.
- **Nothing in `excludedCommands`.**  `scripts/new-worktree.sh` (it writes
  outside the working directory) and the podman demo run from a terminal, not
  from the agent.  An excluded script runs unsandboxed, and the agent can edit
  a script in the repository, so excluding one would be a hole.
- **Four deny rules**: `git push *`, `git reset --hard *`, `git clean *` and
  `git branch -D *`.  They stop the commands that lose work or publish it
  (the hard rules already say "nothing public").
- **The deny rules are a speed bump, not the boundary.**  Probes run in a
  session under these settings.  Blocked by the permission rules, not the
  sandbox: the plain form, the bare form with no arguments, and the compound
  `echo ok; git push origin main`.  Not blocked: `git -C . push origin main`,
  `bash -c 'git push origin main'`, `git branch --delete --force x` and `git
  -c k=v reset --hard HEAD` (it ran; git rejected it only because `k` is not a
  valid config key, and that a valid `-c a.b=c` would have run the reset is an
  inference).  So the rules catch the plain spellings early and say why.  The
  gap was shown for `git branch -D` and `git reset --hard`; `git clean` should
  have it too, by the same mechanism, but was not probed.  The sandbox is the
  boundary.
- **Untested:** whether the sandbox stops a push to a real remote.  The probes
  could not tell, because this repository has no remote and git failed before
  any connection.  Network egress goes through the sandbox's filtering proxy,
  which should refuse an unlisted host, but that is unverified (backlog).
- **Settings load only at session start**, so a session that began before the
  file changed does not have the new rules until it is restarted.

**Consequence found at first merge (Nov 19).**  `/merge` of
`chore/sandbox-settings`, run in a sandboxed session, failed at
`git switch main`: the sandbox refused to unlink `.claude/settings.json`
because it is a protected path, and git aborted on that local change.
Nothing was merged; the merge was finished by hand from a normal terminal.
This was observed.  That the same holds for any branch whose diff touches
`.claude/settings*.json`, `.mcp.json` or the other protected paths is
inferred from the sandbox documentation, not probed.  So the sandbox that
protects these files also stops the agent from merging a change to them, and
that is acceptable: such a merge should be done by the human.  The skill
now checks the diff before the gate and refuses with "merge from a terminal
outside Claude Code" (spec: `docs/specs/merge-protected-path-check.md`); which
of the listed paths really break the merge, beyond `.claude/settings.json`,
is still to be probed (backlog).

## 16. Senders send messages, not files; data lives in SQLite (Nov 22)

Spec: `docs/specs/message-only-streams.md`.  Decision 3 kept a bundle's data
in git (`bundle.yaml`) because Zuul's GitLab driver passes jobs no payload.
That tied every sender to git and put Zuul's own config in the same place
as the announcements.  In production the config and job repositories are real
repositories elsewhere, and git-stream should only trigger jobs.  So:

- **Messages carry `data`, never `files`.**  `files` in any message is a 400
  that says to send `data`.  `data` is a JSON object (64 KiB, 8 levels) kept
  per version in SQLite (`mr_data`, `project_data` in `state.db`) and read by
  jobs from `GET .../merge_requests/<iid>/data` and `GET .../projects/<p>/data`.
  Both are anonymous like the other API GETs: no secrets in it.
- **Git stays, as an internal detail.**  Zuul's stock driver still clones a
  project and fetches `refs/merge-requests/<iid>/head`, so every project is
  a placeholder (one commit, empty tree) and every MR version is a commit
  with the target's tree.  Its message ends with `Stream-*` trailers (the
  iid, a revision, a hash of the data) so two MRs never share a sha, and a
  change of the data is a new commit, which Zuul sees as a changed MR.  The
  alternative, serving git objects straight from SQLite, would be a hand
  written git server in the standard library; rejected as too large and
  risky for Zuul compatibility.
- **Only `open_mr` creates a project** (and `create_project`).  `push` does
  not: a mistyped name would silently make a project.  `create_project`
  adopts a repository already on disk, which fixed the seeder crash-loop.
- **A merge makes an MR's data the project's current data**, so a fix merged
  in a placeholder project (the demo's ci-tool) changes what later jobs read.
  A push of data to the default branch does too.
- **Every stream message is logged** in an append-only `message` table in
  `state.db` (logged as received before anything acts on it, then given its
  outcome) and read at `GET /api/stream/messages` with a stream token,
  because bodies can carry callback URLs.  It is never trimmed (backlog).
- **The demo follows.**  `zuul-config` is a real git repository served by a
  `gitserver` container and read through Zuul's plain `git` driver; `bundles`
  and `ci-tool` are placeholders announced by `seed.py` and the demo UI.
  `[server] seed` is gone.
- **Costs.**  The Forge keeps an internal `files` argument (the placeholder
  commit, tests that need file content); git-stream still needs `git`; the
  demo no longer shows Zuul speculatively merging real files from `ci-tool`,
  only the ordering and the data lookup of `Depends-On`.

## 17. The metrics view counts in git-stream and draws in the demo UI (Nov 23)

Spec: `docs/specs/metrics-view.md`.  Choices:

- **Counting endpoints, not raw rows.**  `GET /api/stream/metrics/<kind>`
  returns counts grouped by one field and bucketed by hour or day, so a page
  never downloads the log and the checks (fixed groups, bounded values,
  at most 500 buckets) are in one place (`aggregate.py`).
- **The data was wrong first.**  A live demo had 43 run rows for 17 builds
  (every Zuul report re-logged the whole version) and no times at all (Zuul
  sends ISO strings and a duration; only numbers were read), so `?days=` matched
  nothing.  A run is now stored once per build uuid and the times are read;
  old databases are de-duplicated when opened and keep NULL times.
- **Chart.js from a CDN, pinned.**  Chosen over hand-written SVG for the
  polish the page is for; the cost is that the page needs internet; the
  script carries a Subresource Integrity hash, so a version change means a
  new hash.  Without the library the page shows tables.
- **The page's logic is plain functions under node.**  `metrics.js` keeps the
  filters-in-the-URL, query building and error text free of the browser so
  tests can run them; those tests fail, with install instructions, when
  `node` is missing (like the contract tests without Zuul), so the gate never
  passes without them.
- **Messages are charted too**, through the same shape of endpoint but behind
  the stream token, and never with a body.
