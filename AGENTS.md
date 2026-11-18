# AGENTS.md

Guide for AI agents (and humans) continuing work on this repository.
Read this first, then the doc that matches your task:

| Task | Read |
|------|------|
| Understand the design | [docs/architecture.md](docs/architecture.md) |
| Change anything Zuul talks to (API, webhooks, git) | [docs/zuul-contract.md](docs/zuul-contract.md) |
| Add or change an endpoint, token or other exposed surface | [docs/security.md](docs/security.md) |
| Work on the podman demo | [docs/demo.md](docs/demo.md) |
| Know why things are the way they are | [docs/decisions.md](docs/decisions.md) |
| Pick the next thing to do (features, production work) | [docs/roadmap.md](docs/roadmap.md) |
| Find what is left to fix or build, and what the pending tests specify | [docs/backlog.md](docs/backlog.md) |
| Port the checks to a CI platform | [docs/ci.md](docs/ci.md) |
| Use git-stream (events, config) | [README.md](README.md) |

## What this is

git-stream is a small **GitLab-compatible fake forge**.  Other systems
(a build system announcing a container bundle, a release tool) send it
JSON events over HTTP; it turns them into git commits and merge requests
and sends Zuul the **webhooks a real GitLab would send**.  Zuul talks to
it with its **stock GitLab driver**, so Zuul stays completely unmodified.

On top of that, git-stream has a **retry policy**: when Zuul reports a
failure, git-stream re-runs the failing jobs itself (by commenting
`rerun failed`), and after `max_attempts` runs it **escalates** (records
it, shows it on `/escalations`, labels the MR, optionally POSTs it).

The use case: replace periodic (timer) Zuul pipelines with event-driven
ones.  Jobs run when a new bundle is announced, failures are retried,
persistent failures are escalated (Jira later).

## Hard rules

- **Zuul must not need changes.**  No Zuul patches, no Zuul plugins, no
  custom Zuul images.  If a feature seems to need Zuul changes, find a
  way through Zuul's existing GitLab driver and job config, or ask.
- **No Kafka.**  An earlier approach (sibling repo `../zuul-kafka-driver`)
  used a Kafka driver; the user explicitly removed Kafka from this one.
  Events come in over HTTP only.
- **Nothing public.**  Do not create a GitHub repo, push, or publish
  anything from this repository unless the user asks.  Commit on a branch as
  described in [Workflow](#workflow).
- **Don't touch `../zuul-kafka-driver`.**  It has uncommitted work the
  user wants left as it is.
- **Stay GitLab-compatible.**  Any API/webhook change must keep Zuul's
  GitLab driver working; `tests/test_zuul_contract.py` must pass.
- **Standard library only** for `git_stream/` (plus `git` >= 2.38 on the
  host).  Python >= 3.11 (`tomllib`).
- Jira integration is wanted **later**, not now.  Escalations are shown
  on pages; `[retry] escalate` URLs are the future hook.

## Workflow

- **Never commit directly to `main`.**  Start every change on a branch named
  `<type>/<short-topic>`, for example `fix/git-option-injection` or
  `feat/stream-labels`.
- **One branch per task.**  Commit in small steps, with the tests in the same
  commit as the code they cover.
- **Findings outside the current task go to [docs/backlog.md](docs/backlog.md)**
  (where it is, why it matters), not into the current branch.
- **Before saying a task is done, run the full check command**
  (`scripts/check.sh`, see [The gate](#the-gate-run-before-saying-done)).  A
  branch is ready only when it is green.
- **Before a branch is put up for approval, run `/review`** (the read-only
  `branch-reviewer`).  A finding marked *unverified* must be checked or
  reproduced before anyone acts on it; it may be a false positive.  Each
  *verified* finding is triaged as fix now, backlog (add it to
  [docs/backlog.md](docs/backlog.md)) or ignore, and the summary you present
  says which.
- **Merge to `main` only after the human explicitly approves that branch in
  the current conversation.**  Never merge on your own judgment, and never push
  unless asked.  Present the branch and a summary of the diff, then wait.

A Claude Code hook enforces the first rule: `.claude/hooks/no-commit-on-main.py`
(a `PreToolUse` hook on the Bash tool, configured in `.claude/settings.json`)
blocks `git commit` and `git push` while the repository is on `main` or
`master`, and says to run `git switch -c <type>/<topic>`.  `git switch -c` and
`git checkout -b` are never blocked.  It is a guardrail, not a security
boundary (it does not catch aliases, scripts that call git themselves, or
`git push origin main` from another branch), and it is covered by
`tests/test_branch_guard_hook.py`.  A session that started before
`.claude/settings.json` existed does not load it until you open `/hooks` or
restart.

## Security rules

- **No bare user strings as git arguments.**  Anything from a request (branch,
  tag, ref, revision, path) goes to `git` after `--end-of-options`, or is
  validated first; a leading `-` must never become an option.  Known
  exceptions are listed in [docs/backlog.md](docs/backlog.md).
- **Identity comes from the token, never from the message body.**
  `stream.apply(forge, msg, sender)` decides who a message acts as
  (`stream._author`): with `[stream] tokens` the token's user, and naming
  another is 403; on an open stream any author except `[retry] report_users`.
  Don't read `author` anywhere else.
- **Bound every body read and parse** (size, time, JSON shape); malformed input
  is a 4xx, never a 500 or a hang.
- **No real tokens in the repo**, tests and docs included; demo values only.
- **Document every new endpoint, token or exposed surface in
  [docs/security.md](docs/security.md)**, and give it a test.

## Definition of done for a stream action

A new `POST /api/stream` action is done when it has:

1. its handler in `git_stream/stream.py`, taking `by` from `apply` (don't
   read the message's `author`);
2. its entry in `ACTIONS`;
3. docs: the `stream.py` docstring, the README events table, and
   `docs/zuul-contract.md` if Zuul sees it;
4. a test in `tests/`, plus a **contract test** in
   `tests/test_zuul_contract.py` if Zuul sees any change (webhook, API field).

## Repository layout

```
git_stream/               the service (stdlib only)
  __main__.py             CLI entry point (`git-stream`), seeding, wiring
  config.py               settings: defaults < TOML file < env < CLI
  forge.py                state: projects, MRs, notes, approvals; emits webhooks
  repos.py                bare git repos changed without a work tree
  server.py               HTTP: GitLab API v4 subset, git smart HTTP,
                          /api/stream, HTML pages
  stream.py               the event vocabulary (POST /api/stream)
  webhooks.py             ordered webhook delivery with retries
  retry.py                retry/escalation policy (talks to Zuul's builds API)
  callbacks.py            per-MR callback_url: POSTs the final outcome
  metrics.py              SQLite log of builds, escalations and deliveries
tests/
  conftest.py             `forge` fixture: a real server on a free port
  test_forge.py           git over HTTP, keys/ids, merges, pages, seeding
  test_zuul_contract.py   Zuul's own GitlabAPIClient + webhook parser vs git-stream
  test_retry.py           retry/escalation with a fake Zuul builds API
  test_callbacks.py       callback_url: validation, outcomes, failures, migration
  test_config.py          config file / env / CLI precedence and errors
  test_metrics.py         the metrics store and its endpoint
  test_webhooks.py        webhook delivery: headers, order, failing receivers
  test_server_input.py    malformed requests are 4xx; receive-pack refusal
  test_git_read_only.py   git over HTTP is read-only however a push is spelled
  test_safety_net.py      contract tests without Zuul; scripts/check.sh
  test_pending.py         the tests/pending.txt mechanism
  test_branch_guard_hook.py  the hook that blocks commits on main
  test_review_hook.py     the hook that keeps the branch-reviewer read-only
  test_stream_identity.py who a stream message acts as (token, not "author")
  test_stream_apply.py    stream.apply's choke point without HTTP
  test_server_limits.py, test_insecure_defaults.py,
  test_legacy_state.py, test_wiring.py
                          mostly specs for behavior not built yet (the backlog)
  pending.txt             the tests that are strict xfails until built
scripts/check.sh          the "run before saying done" gate (lint + all tests)
.claude/                  project Claude Code config: settings.json, hooks/,
                          agents/branch-reviewer.md (read-only reviewer),
                          skills/review/ (the /review [base] command)
devenv/                   podman demo (Zuul + git-stream + demo UI)
  deploy.sh stop.sh clean.sh demo.sh check.sh common.sh
  compose.yaml Containerfile git-stream.toml
  .env.example            secrets for the demo; deploy.sh makes the ignored .env
  etc_zuul/               zuul.conf.in (rendered to zuul.conf), tenant
  repos/                  seed projects: zuul-config, bundles, ci-tool
  demo-ui/                demo web page (stdlib Python + one HTML file)
docs/                     the documentation listed above
```

## Setting up and testing

The contract tests import Zuul, so it must be installed in the venv.  It
does not need a checkout next to this repo: install it from git.

```sh
uv venv -p 3.12 .venv
uv pip install -p .venv/bin/python -e '.[test,lint]'
uv pip install -p .venv/bin/python 'git+https://opendev.org/zuul/zuul'
```

(To test against a local Zuul patch, install a checkout instead:
`uv pip install -p .venv/bin/python -e /path/to/zuul`.)

### Commands

Run from the repo root, with the venv from above (`.venv/bin/python`).

| Purpose | Command |
|---------|---------|
| **Fast loop** (whole suite in parallel, skipping the slow tests) | `.venv/bin/python -m pytest -n auto -m "not slow" -x -q` (about 10 seconds on 18 cores; includes the contract tests) |
| Whole suite in parallel, slow tests included | `.venv/bin/python -m pytest -n auto -q` (about 16 seconds; 320 passed, 27 xfailed) |
| Fast loop, serial | `.venv/bin/python -m pytest -m "not slow" -x -q` (about 1 minute; readable output, exact `-x`, for debugging) |
| Narrower loop | the tests for the area you're changing (e.g. `.venv/bin/python -m pytest tests/test_stream_*.py -q`), plus the contract tests below (about 7 seconds) |
| One test | `.venv/bin/python -m pytest tests/test_stream_apply.py::test_the_sender_is_the_default_author` |
| Contract tests only | `.venv/bin/python -m pytest -m contract` |
| Lint | `.venv/bin/python -m ruff check .` |
| **Full gate** | `scripts/check.sh` (ruff, then every test serially, slow ones included; ~1.5 min) |

**Only the full gate counts for "done".**  The fast loop, the narrower loop
and a single test are for iterating; never report work finished on them.

**pytest-xdist is a test-only dependency.**  It is in the `test` extra of
`pyproject.toml` (so the setup command above installs it) and nothing in
`git_stream/` uses it; the service stays standard library only.  `-n auto`
starts one worker process per core, and each imports Zuul, so on a small
machine use `-n 4` (about 34 seconds for the whole suite).  The gate,
`scripts/check.sh`, stays serial on purpose: it decides "done", and a
timing-sensitive test is least likely to flake when it has the machine to
itself.

**New tests must be safe to run in parallel.**  Workers run tests at the
same time on the same machine and checkout, so a test must not share a
file, a port or a database with any other test:

- use `tmp_path` (or `make_forge` / `Running`, which do) for every data
  directory, SQLite file and repository; never a fixed path, and never
  write into the repository tree;
- bind servers to port 0 and read the port back, as `tests/conftest.py`
  does; never a fixed port;
- don't change process-wide state (environment variables, module globals,
  the working directory) without restoring it; use `monkeypatch`, or a
  subprocess as the proxy test in `tests/test_callbacks.py` does;
- don't depend on the order tests run in or on another test's leftovers.

A test that passes alone and fails under `-n auto` is sharing something:
fix the test, don't drop `-n`.  The suite was run 10 times in a row with
`-n auto` (all green, 320 passed and 27 xfailed each time) when this was
added.

Tests marked `slow` (registered in `pyproject.toml`) are the ones that take
about a second or more because they wait on purpose: timeouts, retry
backoff, "nothing arrives" checks.  Only the fast loop skips them; the full
gate runs everything.  The marker is `@pytest.mark.slow` on the test; mark a
new test when its call phase takes about a second or more
(`pytest --durations=25`), and never use it to hide a test that fails.  The
contract tests are never slow-marked, so the fast loop always runs them.

### The gate: run before saying done

The gate is what CI would run (see [docs/ci.md](docs/ci.md)); there is no CI
config in the repo.  Run it before saying a task is done, and report its
result as it is.

- **Contract tests must pass, and must run.**  Never skip, deselect
  (`-m "not contract"`) or xfail them to claim done.  They import Zuul, so
  Zuul must be installed in the venv; if it isn't, they **fail** with install
  instructions rather than skipping.
- A red gate means not done, whatever else passes.
- **Test integrity.**  Never weaken, delete or edit an assertion to make a test
  pass.  If a test looks wrong, stop and explain why.  Remove a line from
  `tests/pending.txt` only when its test passes.
- CI-like runs use Zuul's latest commit while the demo stays pinned in
  `devenv/common.sh`.

Some tests specify behavior that isn't built yet.  They are strict xfails,
listed in `tests/pending.txt` and described in section 1 of
[docs/backlog.md](docs/backlog.md), so the gate is green while they are
pending.  When you implement one, it starts passing and the run fails with
`XPASS(strict)` until you delete its line, which is the intended reminder.
Never add a line to `pending.txt` to hide a test that fails for another
reason.

### The backlog

[docs/backlog.md](docs/backlog.md) is the list of everything found or decided
but not done: the specs waiting for code, the follow-ups that go with them,
audit findings with no test yet, and tooling.  Check it before choosing work.
Keep it current: delete an item when you finish it, and when you find
something you are not fixing now, add it there (with where it is and why it
matters) instead of leaving it in a chat or a code comment.  The roadmap is
for product features and long-term production work.

The demo (needs podman + podman-compose; first run builds Zuul images,
~25 min; later ~1 min):

```sh
cd devenv && ./deploy.sh          # then http://localhost:8080
./stop.sh                         # pause, keep data
./clean.sh -y --keep-images       # wipe state, keep images
```

Exercise it end to end without a browser (see docs/demo.md for more):

The demo runs with `insecure = true` and its ports on `127.0.0.1`, so these
need no token:

```sh
curl -s -X POST localhost:8080/api/bundle -d '{"action":"new","flaky":0.3}'
curl -s localhost:8090/api/stream/escalations
curl -s 'localhost:8090/api/v4/projects/bundles/merge_requests?per_page=1'
```

Quote URLs containing `?` in zsh, or the shell globs them.

## Conventions

- Match the surrounding style: short modules, docstrings that explain
  *why*, comments only where something is non-obvious, PEP 8, no type
  annotations.
- Every behavior change gets a test.  Changes to what Zuul sees get a
  contract test that drives Zuul's real code.
- Keep the README (user docs), `git_stream/config.py` docstring (every
  option), `devenv/git-stream.toml` (demo config) and these docs in sync
  when adding settings or events.
- Error messages are plain and say what to do.

## Gotchas already hit (don't rediscover them)

- `git update-index --force-remove` needs a work tree; deleting a file
  in a bare repo uses `--index-info` with mode 0 (`repos.py`).
- Merges use `git merge-tree --write-tree` (git >= 2.38).
- Don't run Python (e.g. `py_compile`) inside `devenv/repos/`: it leaves
  `__pycache__`, which the seeder would have committed.  Seeding now
  skips `__pycache__` and reads files as bytes.
- `podman compose up --build` did not rebuild the changed git-stream
  image; `deploy.sh` builds it explicitly and force-recreates the
  `git-stream` and `demo-ui` containers.
- Zuul's HTTP adapter uses `socket.TCP_KEEPIDLE`, missing on macOS; the
  test `conftest.py` aliases it.
- Zuul's own report comments come back to Zuul as note webhooks (real
  GitLab does that too).  Pipeline comment triggers must not match
  Zuul's report text, and the retry policy only reacts to comments from
  `report_users` matching `failed_pattern`.
- The retry policy waits `settle` seconds before reading Zuul's builds
  API so every build of the reported buildset is in Zuul's database.
- An independent pipeline ignores an event for an MR that is already
  running in it; the demo UI refuses actions on busy bundles.
- `base_url` (`http://git-stream:8090`) is the container name; browsers
  can't resolve it.  The demo UI rewrites links; Zuul's web UI needs
  `127.0.0.1 git-stream` in `/etc/hosts`.

## Current state

Working and verified live in the demo: announce → test → results as MR
comments and approval → automatic retries → escalation shown on pages →
test with an unmerged fix via `Depends-On` → approval.  The forge keeps
its state in `<data>/state.db` and logs job activity in
`<data>/metrics.db` (both SQLite, both survive a restart; the in-memory
view is authoritative while running).  Not done: see
[docs/roadmap.md](docs/roadmap.md) (durable webhook/retry queues, Jira
adapter, notifying the announcing system) and [docs/backlog.md](docs/backlog.md)
(fixes and specs waiting for code).

Known gap: the seeder is not fault-tolerant when starting with git repos
on disk but no forge state (e.g. an old instance's `state.json`, which is
no longer read unless SQLite is unavailable, or an empty `state.db`): it
tries to re-create projects whose repos already exist, so `update-ref`
crash-loops.  Reset the forge volume (`clean.sh`) or make the seeder skip
existing repos before upgrading an instance this way.
