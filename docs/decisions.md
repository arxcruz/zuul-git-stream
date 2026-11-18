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
Bundle data lives in git because the GitLab driver passes no arbitrary
payload to jobs.  These MRs live only in git-stream, not in the user's
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
asking Zuul.  Opt-in: no `[metrics] path` = off.  Stdlib `sqlite3`, single
writer (the retry policy), so no locking.

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
- **Fails open**: a missing venv or ruff, bad input or a check over 90 s
  (`STOP_HOOK_TIMEOUT`) prints a note and lets the agent stop; a broken hook
  must not trap it.
- **Not the gate**: it skips the slow tests; `scripts/check.sh` decides
  "done".
