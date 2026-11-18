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
against a throwaway prototype): the git-read-only fix.  Prototype or
implement it first, and treat a failing test there as possibly a test bug.

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

- There is no cap on concurrent connections: each one is a thread for as
  long as it lasts (at most `read_timeout` when silent, `body_timeout` when
  trickling).  60 silent clients cost 60 threads for 5 seconds in a real
  attack run, and all were released.  A cap, or a pool, would bound it.
- Request limits (done) leave these open: the total deadline covers the body,
  not the request line and headers, which the standard library reads with
  only the per-receive `read_timeout` (a client can trickle them); `git
  http-backend` (`server.py:_git`) runs with no timeout and its whole output
  is buffered in memory; a 413 closes the connection without draining the
  rest of the upload, so a client still sending may see a reset instead of
  the response.
- `repos.GitError` is never caught, so a git failure (for example a revision
  the client named that does not exist) is a 500 `internal error`; it should
  be a 4xx where the cause is the client's (branch and tag names and an
  existing tag are done; other causes remain).  Its text, which includes git's arguments and stderr, no
  longer reaches the client.
- Found reviewing branch and tag name validation (all unspecified):
  - Falsy non-string values (`0`, `false`, `[]`) for `branch`, `target_branch`
    and `tag`'s `rev` fall back to the default branch silently, because the
    handlers use `x or default`; only truthy values are validated.  A decision
    is needed: keep the fallback, or reject any explicit non-string.
  - A tag that already exists but does not peel to a commit (for example one
    pointing at a tree or blob) is not seen by the existing-tag check, which
    uses `Repo.resolve` (`^{commit}`), so tagging it again is a 500 (from
    review, not reproduced).
  - Ref name collisions are a 500: with tag `v1` present, tag `v1/x` (or the
    other way round) cannot be created, and the same goes for branches.  git
    refuses the file/directory clash and the `GitError` is uncaught.
  - A non-string `rev` on `tag` (for example `5`) is a 500: `Repo.resolve`
    calls `ref.startswith`.  The same holds for a non-string `start` on
    `push`.

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
- Fields that are not GitLab's reach Zuul's webhooks: `mr_json` also builds the
  webhook attributes, so `stream_key`, `max_attempts`, `escalated` and the
  whole `escalation` record are sent to Zuul.  Only `callback` is removed.
  Check this against [zuul-contract.md](zuul-contract.md) and the contract
  tests, then drop them from `_hook_mr_attrs` if Zuul doesn't need them.
- Callbacks, found in review, not done:
  - A slow-drip receiver can keep a delivery thread for far longer than the
    10 s timeout, which is per socket operation.  Add a total deadline.
  - A callback stays `status: null` forever if git-stream exits before the
    delivery ends, and is not retried on restart (it needs the durable queue).
  - The `failed: <cause>` text comes from the receiver (its status line).  It
    is plain text in the JSON; if a page ever shows it, escape it.
  - A page showing the callback status (the merge request page, the demo UI):
    today only `mr_json` has it.
  - `tests/test_callbacks.py` leans on sleeps and wall-clock bounds (the
    hanging receiver, "nothing arrives" waits), which may be flaky on a slow
    or loaded machine.
- `callback_url` deliveries (`git_stream/callbacks.py`) run one thread each, so
  there is no limit on how many run at once, and two callbacks of the same MR
  (say `escalated`, then `passed`) are not guaranteed to arrive in the order
  they fired, so receivers should order by the `time` field.  A per-MR queue
  or a bounded pool would fix both.  Also not
  done, by decision: an allowlist of callback URLs (any stream sender can make
  git-stream POST to any http(s) host, internal ones included), an auth header
  on callbacks, retries, durable delivery, and per-failure events.
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
