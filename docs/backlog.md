# Backlog

Everything that was found, specified or decided but is **not done yet**.
Product features (Jira adapter, notifying the announcing system, downstream
pipelines) and the long-term production work (durable queues, TLS) stay in
[roadmap.md](roadmap.md); this file is the engineering follow-up list: bugs,
hardening, specs waiting for code, tooling.

Agents: read this before picking work, and keep it current.  Remove an item
when it is done, add one when you find something you are not fixing now.

## 1. Specified, not implemented (tests exist)

These behaviors are specified by tests that are **strict xfails**, so the
gate (`scripts/check.sh`) is green while they are pending.

How it works: the tests are listed in `tests/pending.txt`, one `# group:`
heading each; `tests/conftest.py` marks them `xfail(strict=True)`.  When you
build a group its tests start passing, the run fails with
`[XPASS(strict)] specified, not implemented (<group>)`, and you **delete that
group's lines** from `pending.txt` (partly built groups: delete only the
lines that pass).  The receive-pack cases are marked inline instead (see the
last group).

Not yet checked against any implementation (the other groups each passed
against a throwaway prototype): the branch-and-tag-names tests, the
malformed-requests tests, and the git-read-only fix.  Prototype or implement
those first, and treat a failing test there as possibly a test bug.

- **request limits** (`tests/test_server_limits.py`, `test_request_limits` in
  `test_config.py`): a body over `[server] max_body` (10 MiB) is 413 without
  being read (stream, REST API, git POST, chunked, huge chunk size); a client
  silent for `[server] read_timeout` (30 s) gets 408, or is dropped if it
  never finished the request line or is idle on keep-alive.  Needs
  `serve(max_body=, read_timeout=)`.
- **webhook delivery** (`tests/test_webhooks.py`): one ordered queue per URL,
  so a dead URL never delays another (and `stop()` ends every thread); no
  `X-Gitlab-Token` header when the token is empty.  A 4xx stays "kept as
  failed, not retried" (already true; pinned).
- **retry policy** (`tests/test_retry.py`): any Zuul result that is not
  SUCCESS and not no-verdict (SKIPPED, RETRY, CANCELED, ABORTED) is a failure,
  so `RETRY_LIMIT`, `ERROR`, `DISK_FULL` and future results are retried and
  escalated instead of vanishing; an unreadable builds API is retried
  (non-blocking backoff) then escalated with
  `error: "could not read Zuul's results: ..."`; an outdated version gives up
  quietly; escalation POSTs run in a background worker and each outcome is
  recorded as `escalation['notifications'][url]` = `delivered` |
  `failed: <cause>`.  Needs `RetryPolicy(lookup_attempts=3,
  lookup_backoff=2.0, escalate_attempts=5, escalate_backoff=1.0)`
  (constructor tunables only, no config options).
- **legacy default branch** (`tests/test_legacy_state.py`): a stored project
  whose default branch is invalid (state from before branch validation) is
  warned about once at startup, naming project and branch; operations that
  need the default branch (push without `branch`, tag without `rev`, open_mr
  without `target_branch`) are 409 "project 'x' has an invalid default
  branch '-x'; recreate it"; everything else keeps working.
- **config wiring** (`tests/test_wiring.py`): only
  `test_request_limits_reach_the_server` is left; `build_server` in
  `git_stream/__main__.py` must pass `[server] max_body` and `read_timeout`
  to `serve` (with the request-limits work above).
- **malformed requests** (`tests/test_server_input.py`): bad JSON bodies,
  non-numeric `page`, `per_page` or merge request id, and bad chunk framing on
  the REST API are 4xx, not a 500 that leaks the exception text.
- **branch and tag names** (`test_forge.py`): invalid names for a push's
  `branch`, an MR's `target_branch` and a tag are 400 "invalid branch name" /
  "invalid tag name"; tagging an existing name is 409 "already exists".  A
  push's `start` is a revision, not a branch name, so it is not validated as
  one.  (`create_project`'s branch is already validated.)
- **git read-only check** (`tests/test_git_read_only.py`, 9 inline
  `KNOWN_GAP` markers): `server.py:_git` refuses receive-pack by matching the
  literal text in the raw path and query, so a percent-encoded
  `service=git%2Dreceive%2Dpack` (and `%2d`, partial, extra-parameter
  spellings) gets past it.  No push is possible: without `REMOTE_USER`
  `http-backend` refuses (403), and even with it set in the server's
  environment the write POST is still caught and path variants 404 (checked
  with a real `git push` and a crafted request).  With `REMOTE_USER` set, the
  encoded advertisement is served (refs only).  Fix: check the URL-decoded
  path and the parsed `service` parameter; then remove the markers.

## 2. Follow-ups that come with those fixes

- Sync the docs with each fix, as `AGENTS.md` requires: README, the
  `git_stream/config.py` docstring, `devenv/git-stream.toml`,
  `docs/security.md`, `docs/zuul-contract.md`.
- `main()` itself is still untested glue (ordering: seeding happens before the
  webhook sender starts; the retry policy observes the forge's webhooks).
  Everything it builds goes through the `build_*` functions in
  `git_stream/__main__.py`.
- `GIT_STREAM_GIT_TOKENS` is read by `config.load` but there is no `git`
  section, so setting it would fail; git tokens are not wired anywhere.
  Either remove the variable or implement git tokens.
- Stale statements: the comment in `git_stream/repos.py` (`Repo.create`) says
  git-stream "enforces the git token"; it does not.  Re-read `docs/security.md`
  ("Done", "Planned") against the code once the items above land.
- Starting with an invalid project directory in the seed directory ends in a
  traceback; it should be a plain message that says what to do.

## 3. Audit findings with no test or spec yet

From the audit; none of these has a test.  Specify with a test first, as for
section 1.

- HTML pages are built with string concatenation and `html.escape`; not every
  call site was reviewed, so one missed `e()` could be stored XSS from a note
  body or MR title.  Review all of them and add a test with hostile text.
- Token checks use `==` and a linear scan; use `hmac.compare_digest`.
- `seen` (message ids) is a list capped at 1000 with an O(n) membership test
  and a database write per id, so a replay older than 1000 messages is
  accepted again.
- `mr_json` runs `merge_tree` on every call, so listing N merge requests
  spawns N git processes while holding the forge lock; `_dispatch` also holds
  the lock around git subprocesses and page rendering.
- Subprocess calls (`git http-backend` and the `Repo` helpers) have no
  timeout, so a hung git process holds a request thread, and the forge lock if
  it is held.
- Growth is unbounded: notes, the `seen` ids, the metrics database and webhook
  history (retention policy, or a cleanup of old bundle MRs, which the roadmap
  already lists).
- The seeder is not fault-tolerant against pre-existing state (the "Known
  gap" in `AGENTS.md`): an empty `state.db` makes it recreate projects whose
  repositories exist, and `update-ref` crash-loops.
- `retry.results` assumes Zuul returns builds newest first and that `change`
  and `patchset` mean the MR id and sha; only the parameter and field names
  are checked against Zuul's code, not the ordering.  Escalation URLs get no
  authentication header, and `[retry] zuul_api` accepts any scheme with no TLS
  check.
- `devenv/common.sh` runs `git clone "$ZUUL_REPO"` and
  `git checkout "$ZUUL_REF"` without `--`; the values come from that file
  (operator-controlled), so the risk is low.
- Stream tokens are compared with `==` in `Forge.stream_user_for_token`
  (`forge.py`), which is not constant-time, and the same goes for
  `user_for_token`'s dict lookup; use `hmac.compare_digest` over every
  configured token.  Why: timing can leak a token to a client that can reach
  the stream.
- Two users can be given the same stream token and the first one in the map
  silently wins (`stream_user_for_token`), so the second user's messages act
  as the first.  Refuse duplicate tokens at startup (`config.py` or
  `build_forge`) and say which users share one.
- Duplicated or inconsistent patterns: the stream handlers `_approve`,
  `_unapprove`, `_merge` and `_state` each repeat `_require(msg, 'mr')`; token
  parsing is duplicated between `_user` and `_stream_user` with inverted
  token/user maps; `server.py` imports the private `stream._labels`; status
  and message conventions differ (`'404 Project Not Found'` against
  `'invalid project name'`, double approval is 401); pagination does unchecked
  `int()`.
- The demo UI (`devenv/demo-ui/app.py`, 357 lines) has no tests.
- Missing docs: operations (backup, restore, upgrade, migration, data
  retention), a changelog, a LICENSE file (`pyproject.toml` declares
  Apache-2.0), a sender-side spec for `/api/stream` (a schema or OpenAPI
  document; today the vocabulary lives in the `stream.py` docstring and the
  README), and a note in `docs/zuul-contract.md` for the retry policy's use of
  Zuul's builds API.

## 4. Tooling and process

- No CI configuration yet; the two jobs (lint, test) are written up in
  [ci.md](ci.md).  When a platform is chosen, port them.  Optionally add a
  scheduled run so contract drift against Zuul's latest commit shows up
  without a push.
- Optionally run `scripts/check.sh` from a pre-commit hook so the gate is
  automatic.
- Lint: `line-length` is 88 because 11 lines exceed 79 (`AGENTS.md` asks for
  PEP 8); wrap them and tighten it.  Import sorting (`I`) is off because it
  would reorder most files.  `pyproject.toml` ignores `F401` in
  `git_stream/metrics.py` and `tests/test_metrics.py` (unused imports); delete
  the imports and the ignores.
- The suite takes about two minutes, mostly timeout and retry tests; consider
  trimming waits or a marker for the slow ones.
