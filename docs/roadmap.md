# Status and roadmap

## Done (verified live in the demo, Nov 17 2026)

- GitLab API subset, webhooks and git over HTTP that Zuul's stock GitLab
  driver works with (contract tests drive Zuul's own code).
- Event API: projects, pushes, tags, MRs (keys, idempotent opens),
  comments, labels, approvals, close/reopen, merge; message ids.
- Retry policy: automatic `rerun failed` / `rerun <jobs>` up to
  `max_attempts` per job per MR version; per-MR override.
- Escalation recorded once per version; shown on `/escalations`, the MR
  page, the demo UI; optional POST to URLs.
- TOML config file for every setting, env/CLI overrides.
- Podman demo with announce, retries, escalation, Depends-On fix,
  approval, ci-tool merge/break, interactive `demo.sh`.
- SQLite metrics store: records every completed build, escalation and
  webhook delivery; `GET /api/stream/metrics` (JSON) and the `/metrics`
  page.  Opt-in via `[metrics] path`.

## Specified, not implemented

These behaviors are specified by tests that are strict xfails today (listed in
`tests/pending.txt`, one group per heading).  The tests are the spec; when a
group is built its tests start passing, the run fails with `XPASS(strict)`,
and you delete that group's lines.

- **stream identity** (`test_stream_identity.py`): with stream tokens, a
  message acts as its token's user and naming another user is 403; on an
  open stream any author works except `[retry] report_users` (default
  `zuul`), which is 403.  Needs `Forge(report_users=)`.
- **request limits** (`test_server_limits.py`): body over `[server] max_body`
  (10 MiB) is 413 without reading it; a client silent for
  `[server] read_timeout` (30 s) gets 408 or is dropped.  Needs
  `serve(max_body=, read_timeout=)`.
- **secure by default** (`test_insecure_defaults.py`): no `[api_tokens]` means
  REST writes are 401, no stream tokens means `POST /api/stream` is 401, and
  `[server] insecure = true` restores today's behavior without weakening
  configured tokens.  Needs `Forge(insecure=)`; `tests/conftest.py` must then
  default `insecure=True` for the existing tests.
- **webhook delivery** (`test_webhooks.py`): one ordered queue per URL, so a
  dead URL never delays another; no `X-Gitlab-Token` header when the token is
  empty.
- **retry policy** (`test_retry.py`): any Zuul result that is not SUCCESS or
  no-verdict is a failure; an unreadable Zuul builds API is retried then
  escalated with an `error`; escalation POSTs run in the background and their
  outcome is recorded as `escalation['notifications']`.  Needs
  `RetryPolicy(lookup_attempts=, lookup_backoff=, escalate_attempts=,
  escalate_backoff=)`.
- **legacy default branch** (`test_legacy_state.py`): a stored project with an
  invalid default branch is warned about at startup, and operations needing
  the default branch are 409.
- **config wiring** (`test_wiring.py`): `build_forge`, `build_sender`,
  `build_policy` and `build_server` in `git_stream/__main__.py`, so settings
  demonstrably reach the objects `main()` builds.
- **config tokens** (`test_config.py`): `GIT_STREAM_STREAM_TOKENS` must work
  (it crashes config loading today, though `security.md` documents it) and
  config defaults must not be shared between loads.
- **malformed requests** (`test_server_input.py`): bad JSON, non-numeric
  paging or merge request ids, and bad chunk framing on the REST API are
  4xx, not 500.
- **branch and tag names**: invalid names for a push's `branch`, an MR's
  `target_branch` and a tag are 400; tagging the same name twice is 409.

The receive-pack gap below is marked inline instead
(`tests/test_git_read_only.py`).

## Next, by priority

### Production hardening (needed before real use)
1. ~~**Authenticate `POST /api/stream`**~~ **done** — per-sender tokens via
   `[stream]`; open (with a loud warning) when unset. See
   [security.md](security.md).
2. **Durable webhook queue**: pending deliveries are in memory and lost
   on restart.  Persist them (with the state) and add a redeliver API.
3. **Durable retry queue**: same for `retry.py` (a restart between
   Zuul's report and the rerun loses the retry).  On startup, scan
   opened MRs whose last Zuul report failed.
4. ~~**SQLite instead of `state.json`**~~ **done** — forge state (projects,
    MRs, notes) is in a SQLite database (`<data>/state.db`, with a JSON
    fallback); it is rewritten on every change but no longer grows with the
    unbounded notes list.  The metrics store (`metrics.py`) is a separate
    database for job activity.

5. **git read-only check matches raw text.**  `server.py:_git` refuses
   receive-pack by looking for the literal text in the raw path and query, so
   a percent-encoded `service=git%2Dreceive%2Dpack` gets past it.  No push is
   possible (http-backend refuses without `REMOTE_USER`, and the write POST is
   still caught), but with `REMOTE_USER` set in the server's environment the
   advertisement is served.  Fix: check the URL-decoded path and the parsed
   `service` parameter.  Tracked by nine strict-xfail tests in
   `tests/test_git_read_only.py`; remove their `KNOWN_GAP` marker when fixed.
6. TLS / reverse proxy guidance, request size limits,
   (metrics: see "Done" above), structured logs.
7. Optional read token for the API and git.

### Features
- **Jira adapter** (user wants it later): consume `[retry] escalate`
  POSTs and open/update a Jira issue; store the issue key back on the
  escalation (needs an API to attach it) and show it on the pages.
- **Notify the announcing system** of outcomes (passed/approved,
  escalated), e.g. a per-MR `callback_url` in `open_mr`, or generic
  outgoing event subscriptions.
- **Downstream pipelines on approval**: example Zuul config triggering
  e.g. package builds on `gl_merge_request` action `approved`, replacing
  timer pipelines.
- **Close/cleanup** of old bundle MRs (age or count based).
- Escalation de-duplication across bundles (same job failing for every
  bundle → one escalation?).
- Moving the user's existing periodic jobs: pipeline triggers from
  `timer` to git-stream events, `required-projects` for real GitLab
  repos, bundle data via `bundle-dispatch` variables.

### Maintenance
- Re-check [zuul-contract.md](zuul-contract.md) against each Zuul
  upgrade; run the contract tests against the new Zuul.
- The demo pins Zuul `708e1bb2` (`devenv/common.sh`).

## Not planned

- Kafka input (removed on purpose).
- Accepting `git push` (receive-pack): changes come through events so
  every change has a webhook.
- GitHub or Gerrit emulation.
