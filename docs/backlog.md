# Backlog

Everything that was found, specified or decided but is **not done yet**.
Product features (Jira adapter, notifying the announcing system, downstream
pipelines) and the long-term production work (durable queues, TLS) stay in
[roadmap.md](roadmap.md); this file is the engineering follow-up list: bugs,
hardening, specs waiting for code, tooling.

Severity tags: **S1** could break Zuul's contract, leak a secret, let someone
act as someone else, or hang the server (act soon); **S2** a real gap with
limited impact or an easy workaround; **S3** polish, speed or nice-to-have.
**(unverified)** means the tag is a judgment from reading the item, not from
reproducing the problem or confirming its impact; check it before acting.  A
sub-item has the tag of the item it is under.

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

- [S2 (unverified)] **webhook delivery** (`tests/test_webhooks.py`): one ordered queue per URL,
  so a dead URL never delays another (and `stop()` ends every thread); no
  `X-Gitlab-Token` header when the token is empty.  A 4xx stays "kept as
  failed, not retried" (already true; pinned).
- [S2 (unverified)] **retry policy** (`tests/test_retry.py`): any Zuul result that is not
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
- [S2] **legacy default branch** (`tests/test_legacy_state.py`): a stored project
  whose default branch is invalid (state from before branch validation) is
  warned about once at startup, naming project and branch; operations that
  need the default branch (push without `branch`, tag without `rev`, open_mr
  without `target_branch`) are 409 "project 'x' has an invalid default
  branch '-x'; recreate it"; everything else keeps working.
- [S2] **git read-only check** (`tests/test_git_read_only.py`, 9 inline
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

- [S3] Sync the docs with each fix, as `AGENTS.md` requires: README, the
  `git_stream/config.py` docstring, `devenv/git-stream.toml`,
  `docs/security.md`, `docs/zuul-contract.md`.
- [S3] `main()` itself is still untested glue (ordering: seeding happens before the
  webhook sender starts; the retry policy observes the forge's webhooks).
  Everything it builds goes through the `build_*` functions in
  `git_stream/__main__.py`.
- [S3] `GIT_STREAM_GIT_TOKENS` is read by `config.load` but there is no `git`
  section, so setting it would fail; git tokens are not wired anywhere.
  Either remove the variable or implement git tokens.
- [S3] Stale statements: the comment in `git_stream/repos.py` (`Repo.create`) says
  git-stream "enforces the git token"; it does not.  Re-read `docs/security.md`
  ("Done", "Planned") against the code once the items above land.
- [S3] Starting with an invalid project directory in the seed directory ends in a
  traceback; it should be a plain message that says what to do.

## 3. Audit findings with no test or spec yet

From the audit; none of these has a test.  Specify with a test first, as for
section 1.

- [S1] Request limits (done) leave these open: the total deadline covers the body,
  not the request line and headers, which the standard library reads with
  only the per-receive `read_timeout` (a client can trickle them); `git
  http-backend` (`server.py:_git`) runs with no timeout and its whole output
  is buffered in memory; a 413 closes the connection without draining the
  rest of the upload, so a client still sending may see a reset instead of
  the response.
- [S2] The connection cap (`[server] max_connections`, default 64) refuses with a
  503 that Zuul's REST client retries, but git does not: a refusal during a
  `git fetch` or `clone` by Zuul's mergers or executors would fail the job.
  Measure it with a real demo run under load, and check the refusal reaches a
  client in a Linux container (it was checked with curl on macOS only).  The
  options are a higher default or exempting git smart HTTP from the cap.
- [S3] A refused connection (`max_connections`) is only a debug log line, so an
  operator cannot see that the cap is being hit.  The server has a metrics
  object; a refusal counter there would show it.
- [S2] `repos.GitError` is never caught, so a git failure (for example a revision
  the client named that does not exist) is a 500 `internal error`; it should
  be a 4xx where the cause is the client's (branch and tag names and an
  existing tag are done; other causes remain).  Its text, which includes git's arguments and stderr, no
  longer reaches the client.
- [S2] Found reviewing branch and tag name validation (all unspecified):
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

- [S3] A non-string `log_url` in Zuul's builds API (a number, say) makes
  `/escalations` and the MR page a 500: `_log_link` and `html.escape` both
  assume a string (checked with `log_url: 5`; it failed the same way before
  the `javascript:` fix).  Zuul sends strings, so only a misbehaving Zuul
  triggers it; `retry.py` could drop or `str()` the value when it records it.
- [S3] No `Content-Security-Policy` or `X-Content-Type-Options: nosniff`
  header on any response: a second layer if an escape is ever missed (the
  pages escape every value they show, which `tests/test_html_escaping.py`
  checks; a `javascript:` log URL from Zuul is now shown as text).  The demo
  UI (`devenv/demo-ui/index.html`) builds its HTML in the browser and was
  only reviewed by reading, never driven in a browser with hostile text; its
  `log_url` links (`j.log_url`, `b.log_url`) still have the `javascript:`
  gap the server pages had.
- [S2] `seen` (message ids) is a list capped at 1000 with an O(n) membership test
  and a database write per id, so a replay older than 1000 messages is
  accepted again.
- [S2] `mr_json` runs `merge_tree` on every call, so listing N merge requests
  spawns N git processes while holding the forge lock; `_dispatch` also holds
  the lock around git subprocesses and page rendering.
- [S1] Subprocess calls (`git http-backend` and the `Repo` helpers) have no
  timeout, so a hung git process holds a request thread, and the forge lock if
  it is held.
- [S2] Growth is unbounded: notes, the `seen` ids, the metrics database and webhook
  history (retention policy, or a cleanup of old bundle MRs, which the roadmap
  already lists).
- [S2 (unverified)] Fields that are not GitLab's reach Zuul's webhooks: `mr_json` also builds the
  webhook attributes, so `stream_key`, `max_attempts`, `escalated` and the
  whole `escalation` record are sent to Zuul.  Only `callback` is removed.
  Check this against [zuul-contract.md](zuul-contract.md) and the contract
  tests, then drop them from `_hook_mr_attrs` if Zuul doesn't need them.
- [S2] Callbacks, found in review, not done:
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
- [S2] `callback_url` deliveries (`git_stream/callbacks.py`) run one thread each, so
  there is no limit on how many run at once, and two callbacks of the same MR
  (say `escalated`, then `passed`) are not guaranteed to arrive in the order
  they fired, so receivers should order by the `time` field.  A per-MR queue
  or a bounded pool would fix both.  Also not
  done, by decision: an allowlist of callback URLs (any stream sender can make
  git-stream POST to any http(s) host, internal ones included), an auth header
  on callbacks, retries, durable delivery, and per-failure events.
- [S2] The seeder is not fault-tolerant against pre-existing state (the "Known
  gap" in `AGENTS.md`): an empty `state.db` makes it recreate projects whose
  repositories exist, and `update-ref` crash-loops.
- [S1 (unverified)] `retry.results` assumes Zuul returns builds newest first and that `change`
  and `patchset` mean the MR id and sha; only the parameter and field names
  are checked against Zuul's code, not the ordering.  Escalation URLs get no
  authentication header, and `[retry] zuul_api` accepts any scheme with no TLS
  check.
- [S3] `devenv/common.sh` runs `git clone "$ZUUL_REPO"` and
  `git checkout "$ZUUL_REF"` without `--`; the values come from that file
  (operator-controlled), so the risk is low.
- [S1] Two users can be given the same stream token and the first one in the map
  silently wins (`stream_user_for_token`), so the second user's messages act
  as the first.  Refuse duplicate tokens at startup (`config.py` or
  `build_forge`) and say which users share one.
- [S3] Duplicated or inconsistent patterns: the stream handlers `_approve`,
  `_unapprove`, `_merge` and `_state` each repeat `_require(msg, 'mr')`; token
  parsing is duplicated between `_user` and `_stream_user` with inverted
  token/user maps; `server.py` imports the private `stream._labels`; status
  and message conventions differ (`'404 Project Not Found'` against
  `'invalid project name'`, double approval is 401).
- [S3] The demo UI (`devenv/demo-ui/app.py`, 357 lines) has no tests.
- [S3] Missing docs: operations (backup, restore, upgrade, migration, data
  retention), a changelog, a sender-side spec for `/api/stream` (a schema or OpenAPI
  document; today the vocabulary lives in the `stream.py` docstring and the
  README), and a note in `docs/zuul-contract.md` for the retry policy's use of
  Zuul's builds API.

## 4. Tooling and process

- [S2] No CI configuration yet; the two jobs (lint, test) are written up in
  [ci.md](ci.md).  When a platform is chosen, port them.  Optionally add a
  scheduled run so contract drift against Zuul's latest commit shows up
  without a push.
- [S3] Optionally run `scripts/check.sh` from a pre-commit hook so the gate is
  automatic.
- [S3] Lint: `line-length` is 88 because some lines exceed 79 (`AGENTS.md` asks for
  PEP 8); wrap them and tighten it.  Import sorting (`I`) is off because it
  would reorder most files.  `pyproject.toml` ignores `F401` in
  `git_stream/metrics.py` and `tests/test_metrics.py` (unused imports); delete
  the imports and the ignores.
- [S3] The serial gate is slow because many tests wait on purpose (timeouts, retry
  backoff).  The `slow` marker lets the fast loop skip the slowest of them, but
  the rest still take most of the serial time (`pytest --durations=25` shows
  where); trimming those waits would shorten both.
- [S3] Stop hook (`.claude/hooks/stop-checks.py`), from the branch review:
  `fingerprint()` reads every changed file whole, so a very large untracked
  file not covered by `.gitignore` slows every Stop (hash a size and mtime
  above a threshold); the `git` calls have no timeout (only the ruff probe and
  the checks do); and no test runs the hook against the real ruff and pytest,
  only a stub (the wiring was checked by hand on this branch).
- [S3] `/spec` skill (`.claude/skills/spec/SKILL.md`), from the branch review:
  the `allowed-tools` patterns (`Write(docs/specs/*)`, `Bash(git switch -c
  feat/*)`) were never run in a live session, so whether Claude Code accepts
  the space-separated list, rejects a `..` path, or refuses a start point
  after the branch name is unchecked; do one manual dry run of `/spec` on
  `main` and tighten the patterns if any of these slips through.  Several
  tests in `tests/test_spec_skill.py` grep the prose for keywords ("never",
  "recommend", "plan"), so they show the skill mentions a behavior, not that
  it demands it.
- [S3] `/merge` skill (`.claude/skills/merge/SKILL.md`): its `allowed-tools`
  patterns (exact for the log, merge-tree and gate commands; `git merge
  --no-ff -m *` and `git branch -d --end-of-options *` for the merge and the
  delete) were tried in a complete dry run on a scratch branch: Claude Code
  accepts the exact patterns, and the skill refuses on `main`, with a dirty
  tree, with nothing ahead of `main` and with an argument that is not the
  current branch.  The merge (a commit with two parents), the delete (`git
  branch -d`) and the second gate on `main` also ran live and passed.  Still
  unchecked: that `disable-model-invocation` really stops the model from
  starting `/merge`.  Compound-command splitting is verified for deny rules
  by two probes (`echo ok; git push origin main` was refused by
  `Bash(git push *)`, decision 15) and still unchecked for this skill's
  allow patterns: whether `git merge --no-ff -m x b && git push` is split
  before matching, so that the push is not covered by the
  `git merge --no-ff -m *` pattern.
  The gate on the branch does not include what `main` gained since the branch
  started; the gate on `main` after the merge covers that, and a red result
  there leaves the merge on local `main` for the user to deal with.  Check
  the two open points and tighten the patterns if either slips.
- [S2] Push guard: the deny rule `Bash(git push *)` does not match
  `git -C . push origin main` or `bash -c 'git push origin main'` (probed
  Nov 19, decision 15), so only the sandbox stands between the agent and a
  push.  When a remote is added, extend `.claude/hooks/no-commit-on-main.py`
  (it already parses `-C` and `bash -c`) to refuse the agent's `git push` in
  those spellings too, with a test in `tests/test_branch_guard_hook.py`; and
  test a push through the sandbox against a real remote (run in an agent
  session under `.claude/settings.json`, with a scratch remote and branch),
  to learn whether the sandbox's network rules stop it.
- [S2] `/merge` in a worktree leaves the session on `main`
  (`.claude/skills/merge/SKILL.md`, steps 3 to 6): the merge does
  `git switch main` and never switches back, and git lets only one worktree
  have `main`, so the other worktree's `/merge` fails at `git switch main`
  until this session leaves it.  It also needs the original checkout to be
  detached (decision 14), or `git switch main` fails at once.  Workaround:
  `git switch --detach` in the session after a merge.  Fix: have the skill
  detach (or switch to a spare branch) after step 5, and refuse early, before
  the gate runs, when `main` is checked out in another worktree (`git worktree
  list --porcelain`).  The skill's `allowed-tools` need the new commands:
  `tests/test_merge_skill.py` compares them with an exact list (`TOOLS`), so
  that list must be edited on purpose too, and it checks with `fnmatch` that
  every command the skill runs matches a pattern.  `fnmatch` only
  approximates Claude Code's own matching, which is what the open points
  above are about.
- [S3] `/merge` and the `git branch -d` config-lock warning
  (`.claude/skills/merge/SKILL.md`, AGENTS.md "Permissions and sandbox"):
  observed twice (original checkout, linked worktree), `git branch -d` printed
  "could not lock config file .../.git/config" and "update of config-file
  failed" yet deleted the branch.  In the worktree run the exit status was 0
  and the line "Deleted branch ... (was 76314c7)" followed; the status is
  observed once, not in the first run.  The mechanism (`.git/config` being a
  protected path) is inferred.  Make the skill treat the warning as harmless
  only when that status is 0 (and still stop and report otherwise).
- [S2] `/spec` fails in a worktree (`.claude/skills/spec/SKILL.md`): it runs only
  from `main` with a clean tree, and no worktree can have `main` (decision 14).
  Workaround: write `docs/specs/<name>.md` by hand, in the skill's format, on
  the feature branch.  Fix: let `/spec` run on a fresh feature branch or on a
  detached HEAD at `main`.
- [S3] Probe `.mcp.json` and the other `.claude/settings*.json` names under a
  real `/merge` (`docs/specs/merge-protected-path-check.md`).  The skill now
  refuses a branch whose diff touches them, but only `.claude/settings.json`
  is observed to break `git switch main`; the other names are inferred from
  the sandbox docs.  `.claude/skills/`, `.claude/hooks/` and
  `.claude/agents/` are observed write-protected for Bash (create and rename
  blocked, Nov 19), but `/merge` has not been run on a branch that changes
  them.  Run it on a throwaway branch for each, then move the entry to
  observed in the spec's table, or drop an entry that does not break.
