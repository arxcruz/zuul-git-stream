# Architecture

## The picture

```
 sender (build system, tool, person)
   │  POST /api/stream  {"action": "open_mr", "key": "b-0042", ...}
   ▼
 ┌──────────────────────── git-stream ─────────────────────────┐
 │ stream.py ──▶ forge.py ──▶ repos.py (bare git repos)         │
 │                  │                                           │
 │                  │ emit(payload)                             │
 │                  ├──▶ webhooks.py ──── GitLab webhooks ──────┼──▶ zuul-web
 │                  └──▶ retry.py (watches Zuul's reports)      │      │
 │                                                              │      ▼
 │ server.py: GitLab API v4 ◀── REST (MRs, notes, approve) ─────┼── zuul-scheduler
 │            git smart HTTP ◀── git clone/fetch ───────────────┼── zuul-merger/executor
 │            HTML pages, /api/stream, /api/stream/escalations, metrics  │
 └──────────────────────────────────────────────────────────────┘
                  retry.py ──▶ Zuul builds API (how often did each job fail?)
                  retry.py ──▶ [retry] escalate URLs (optional)
```

To Zuul, git-stream is a GitLab server: configured with a normal
`[connection ...] driver=gitlab`, triggered by normal `gl_merge_request`
/ `gl_push` events, reporting with normal comments and approvals.

## Modules

### `forge.py`: state and the GitLab model
- One `Forge` object owns all state, guarded by a re-entrant lock
  (`forge.lock`).  The HTTP server holds it for every API/page request.
- State lives in a database in the data directory (SQLite
  `<data>/state.db`, or a JSON `<data>/state.json` fallback when the
  `sqlite3` module is unavailable).  The in-memory view is authoritative
  while the process runs; the database is a persistence mirror written on
  every change.  Repositories are bare repos in `<data>/repos/<name>.git`.
- State layout (the in-memory view the stores share):
  ```
  projects: {name: {id, name, default_branch, description, created_at,
                    next_iid, keys: {key: iid},
                    mrs: {iid: MR}}}
  mrs: {(project_id, iid): MR}    # global MR index the store iterates
  keys: {(project_id, key): iid}
  next_project_id, next_mr_id, next_note_id
  seen: [message ids]            # last 1000, for de-duplication
  MR = {id, iid, title, description, state (opened|closed|merged),
        target_branch, source_branch ("mr-<iid>", never created),
        sha, start_sha, author, labels, approved_by, notes: [...],
        created_at, updated_at, merged_at, closed_at, merge_commit_sha,
        key, max_attempts, escalated_sha, escalation}
  ```
- Each operation (`create_project`, `push`, `tag`, `open_mr`,
  `update_mr`, `comment`, `approve`, `unapprove`, `merge`) changes git
  first, then state, then calls `emit(payload)` with the GitLab webhook
  payload.  Git first means the ref exists before Zuul hears about it.
- `*_json` methods render GitLab API representations.  MR JSON has extra
  non-GitLab fields: `stream_key`, `max_attempts`, `escalated`,
  `escalation`.
- `merge_status` is computed on read with `git merge-tree`.
- MRs never create a source branch: extra branches would make Zuul load
  config from them.  The MR head lives only at
  `refs/merge-requests/<iid>/head`, like GitLab.

### `repos.py`: git without a work tree
Commits are built with a temporary index (`read-tree`, `update-index`,
`write-tree`, `commit-tree`) and refs are moved with compare-and-swap
`update-ref`.  `commits()` produces GitLab push-hook commit entries
(added/modified/removed).

### `stream.py`: the event vocabulary
`apply(forge, msg)` validates one event and calls the forge.  Events
address MRs by iid or by a sender-chosen `key`; `open_mr` with a key
that's still open updates that MR (idempotent announcements).  An event
`id` is remembered so resending is a no-op.

### `server.py`: HTTP
`ThreadingHTTPServer` with one handler class built by `make_handler`.
Routes:
- `/api/v4/projects/...`: GitLab API subset (see
  [zuul-contract.md](zuul-contract.md)).  GETs are anonymous; writes need
  `Authorization: Bearer <token>` or `PRIVATE-TOKEN`, mapped to a user
  by `[api_tokens]`.  With no tokens configured, any token acts as `zuul`.
- `/<project>.git/...`: `git http-backend` as CGI, upload-pack only;
  receive-pack returns 403 (changes come through the stream).
- `/api/stream` (POST): the sender is resolved from the token
  (`stream_user_for_token`) and passed to `stream.apply(forge, msg, sender)`,
  the one place that decides who a message acts as (`stream._author`): the
  token's user, or with `author` set, only that same user; on an open stream
  any author except `report_users`.  A refusal is a 403, per message in a
  batch.  Also served: `/api/stream/deliveries`, `/api/stream/escalations`, `/api/stream/metrics` (when metrics is on).
- HTML: `/`, `/escalations`, `/metrics`, `/<project>`, `/<project>/merge_requests/<iid>`.
- `/health`.

### `webhooks.py`: delivery
One thread, one queue: payloads are delivered in order to every URL with
`X-Gitlab-Token` and `X-Gitlab-Event`.  Connection errors and 5xx are
retried with exponential backoff (cap `max_backoff`); 4xx is logged and
dropped.  The queue is in memory (lost on restart; see roadmap).

### `retry.py`: retry and escalation
`RetryPolicy.observe()` is attached to the forge's emit fan-out.  For a
note from one of `report_users` matching `failed_pattern`, it queues
`(project, iid, sha)`; its thread then:
1. waits `settle` seconds, skips the MR if closed or if its sha moved;
2. reads Zuul's `/builds?project=&change=&patchset=<sha>&complete=true`
   and computes per job: latest result and number of failures;
   with metrics enabled it also logs the completed builds;
3. failing jobs with failures < `max_attempts` → comments
   `rerun failed` (all failing retryable) or `rerun <jobs>` (some);
4. otherwise escalates once per sha: stores `mr['escalation']` (and, when metrics is on, a metrics row), comments
   a summary, adds label `escalated`, POSTs the event to `escalate` URLs.
The policy's own comments come from `author` (not a report user), so it
never reacts to itself.  Stream senders cannot post as a report user
(see `/api/stream` above), so only Zuul's own REST comments can start it.  The rerun comment is interpreted by the
**Zuul job config**, not by git-stream: a pipeline triggered by
`rerun` comments, whose first job (`bundle-dispatch` in the demo) reads
the comment back from git-stream's API and sets `zuul.child_jobs`.

### `metrics.py`: what the jobs did, in a database
An optional SQLite file (`[metrics] path`) records, as events happen,
one row per completed build (`run`), per escalation, and per webhook
delivery.  The retry policy writes `run` and `escalation` rows; the
webhook sender writes `delivery` rows.  Read-mostly: `GET
`/api/stream/metrics` and the `/metrics` page aggregate the `run` table.
It is a log of what git-stream saw, not a source of truth; it is additive
and does not replace the forge state in `state.db`.

### `config.py`: settings
`SCHEMA` maps section → option → (default, type, env var).  `load()`
merges defaults, the TOML file (`--config`, `$GIT_STREAM_CONFIG`, or
`/etc/git-stream/git-stream.toml`), environment, then CLI overrides.
Unknown sections/options and bad types are errors.

### `__main__.py`: wiring
Parses CLI, loads config, builds `WebhookSender`, `Forge` (with an emit
fan-out list), optional `RetryPolicy`, and
serves forever.

## Flows

**Announce:** `open_mr` → commit on target branch head → ref
`refs/merge-requests/N/head` → state → `merge_request`/`open` webhook →
Zuul fetches MR + approvals via API, clones over HTTP, fetches the MR
ref, enqueues → reports start/result as notes (POST notes) and
approve/unapprove.

**Retry:** Zuul's "Build failed" note → `retry.py` → builds API → note
`rerun failed` by `git-stream` → note webhook → Zuul `bundle-rerun`
pipeline → `bundle-dispatch` reads the note → only failing jobs run.

**Escalate:** same, but no job has attempts left → record + label +
comment (+ optional POST) → visible on `/escalations` and the MR page.

**Depends-On:** an MR description line `Depends-On: <git-stream MR URL>`
→ Zuul resolves the URL to the GitLab connection (hostname matches
`server`/`baseurl`) and tests with that MR speculatively merged.
