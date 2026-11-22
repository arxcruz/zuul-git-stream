# Spec: message-only streams, stored in SQLite

## Purpose

A sender announces work with a message only: it names a project and sends
`data`, never files or commits.  git-stream stores every message and its
outcome in SQLite, where it can be queried later, and gives Zuul the minimum
git its stock GitLab driver needs, as an internal detail the sender never
sees.  git-stream never holds job definitions or Zuul config: those live in
real repositories (in production, on other Zuul connections).  The podman
demo works the same way.

## Scenarios

1. `open_mr` with a `data` object and no `files`: the MR opens, Zuul sees an
   ordinary MR (sha, `diff_refs`, `refs/merge-requests/<iid>/head`), and the
   data is stored in SQLite with the MR.
2. A project that does not exist yet is created on first use by `open_mr`, and
   by `create_project` (which takes no files), as a placeholder repository
   (one empty-content commit on the default branch; for `open_mr`, the
   message's `target_branch`).  Nothing in it comes from a sender.  `push`,
   `tag`, `comment` and the other actions on a missing project stay a 404, so
   a mistyped name does not silently make a project.
3. Jobs read an MR's `data` from git-stream's API, also for a Depends-On MR,
   not from a file in git: `GET /api/v4/projects/:id/merge_requests/:iid/data`,
   optionally `?sha=` for an older version.  Like the other API GETs it is
   anonymous.
4. `update_mr` with new `data` gives the MR a new head sha, so Zuul sees a
   changed MR, and the old data stays queryable under the old sha.  Every
   MR commit is unique (its message carries the iid, a revision counter and a
   hash of the data), so two MRs opened in the same second never share a sha.
5. Every message received on `POST /api/stream` is recorded with its sender,
   action, project, key, time and outcome: applied, duplicate (same `id`),
   refused (403) or rejected (4xx).  A refused or malformed message is
   recorded without any side effect on state.
6. `GET /api/stream/messages` lists recorded messages, filterable by
   project, key, action, outcome and time range, paged (with a cap on the page
   size), newest first.  The log is an append-only `message` table in
   `state.db` that the state mirror's `save()` never rewrites; MR data lives
   in an `mr_data` table keyed by MR and sha.  Both need `sqlite3`; without it
   the endpoint answers 501 with a plain message.
7. `files` in `open_mr`, `update_mr`, `push` and `create_project` is a 400 that
   says to send `data`.  The `[server] seed` setting is removed, so a config
   that still sets it fails at startup with the usual unknown-option error.
   The Forge's Python methods keep an internal `files` argument for tests and
   the placeholder commit; no message can reach it.
8. The demo: `zuul-config` (pipelines, jobs, playbooks) and `ci-tool` move out
   of git-stream into real git repositories served by a small git HTTP
   container, used by Zuul through a `git` connection.  `bundles` and
   `ci-tool` in git-stream are placeholders.  The bundle flow
   (announce, test, retry, escalate, approve) works end to end.
9. The demo's `ci-tool` fix scenario becomes a data message: a fix MR in the
   placeholder `ci-tool` project carries `data` (for example `broken: false`),
   the bundle `Depends-On` it, and the bundle jobs read the depended-on MR's
   data from the API.
10. Merging a data MR (the `merge` action or Zuul's merge) makes its data the
    project's current data, read from
    `GET /api/v4/projects/:id/data`.  A job reads the depended-on MR's data if
    it has one, else the project's current data.  This is what keeps the
    ci-tool fix scenario meaningful after the merge.
11. Restart: messages, MR data and placeholder repos survive a restart.  A
    state with git repos on disk and no matching SQLite rows does not
    crash-loop (see the known gap in `AGENTS.md`).

## Fires on

Every message through `POST /api/stream`, accepted or not.  Not on Zuul's REST
calls or webhooks, which are not messages.

## Out of scope

Retention and cleanup of old messages, a message query page or UI, a read
token for the new endpoints, a migration of existing `bundle.yaml` content,
and Jira.  The first four go to `docs/backlog.md`.  Accepting `git push`
stays not planned (`docs/roadmap.md`).

## Done means

- A test for each scenario above, parallel-safe (`tmp_path`, port 0).
- The Zuul contract tests unchanged and passing, plus a contract test that
  runs Zuul's real client and webhook parser against a message-only MR
  (placeholder commit, `refs/merge-requests/<iid>/head` fetchable, `diff_refs`
  present).  The new read endpoint and message log carry no field Zuul reads.
- `docs/security.md` (the new endpoints, who may read messages, the 400 on
  `files`), the README events table, the `git_stream/config.py` docstring if a
  setting is added, the `stream.py` docstring, `docs/architecture.md`,
  `docs/decisions.md` (a new numbered decision, since this reverses decision
  3's "bundle data lives in git"), `docs/zuul-contract.md` ("Git"),
  `docs/demo.md` and `devenv/` updated together.
- The full gate (`scripts/check.sh`) green, and the demo run end to end.

## Constraints

- Zuul unchanged: no patches, plugins or custom images; the stock GitLab
  driver and Zuul's `git` driver only.
- Stdlib only (plus `git` >= 2.38 on the host).
- git-stream creates no job definitions, pipelines or Zuul config, ever.
- Identity comes from the token (`stream.apply`); the recorded sender is that
  identity, never the message's `author`.
- No bare user strings as git arguments; `data` is a JSON object bounded in
  size and nesting depth (a 400 above either), and malformed input is a 4xx,
  never a 500.
- The message log is written before side effects on state are visible to
  Zuul, and never delays a webhook.
- Stay GitLab-compatible: webhooks and the REST subset Zuul uses do not change.
- Behavior for a sender that sends no `files` is the only supported one; there
  is no fallback path.
