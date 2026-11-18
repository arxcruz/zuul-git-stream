# AGENTS.md

Guide for AI agents (and humans) continuing work on this repository.
Read this first, then the doc that matches your task:

| Task | Read |
|------|------|
| Understand the design | [docs/architecture.md](docs/architecture.md) |
| Change anything Zuul talks to (API, webhooks, git) | [docs/zuul-contract.md](docs/zuul-contract.md) |
| Work on the podman demo | [docs/demo.md](docs/demo.md) |
| Know why things are the way they are | [docs/decisions.md](docs/decisions.md) |
| Pick the next thing to do | [docs/roadmap.md](docs/roadmap.md) |
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
  anything from this repository unless the user asks.  Commits only when
  asked.
- **Don't touch `../zuul-kafka-driver`.**  It has uncommitted work the
  user wants left as it is.
- **Stay GitLab-compatible.**  Any API/webhook change must keep Zuul's
  GitLab driver working; `tests/test_zuul_contract.py` must pass.
- **Standard library only** for `git_stream/` (plus `git` >= 2.38 on the
  host).  Python >= 3.11 (`tomllib`).
- Jira integration is wanted **later**, not now.  Escalations are shown
  on pages; `[retry] escalate` URLs are the future hook.

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
tests/
  conftest.py             `forge` fixture: a real server on a free port
  test_forge.py           git over HTTP, keys/ids, merges, pages, seeding
  test_zuul_contract.py   Zuul's own GitlabAPIClient + webhook parser vs git-stream
  test_retry.py           retry/escalation with a fake Zuul builds API
  test_config.py          config file / env / CLI precedence and errors
devenv/                   podman demo (Zuul + git-stream + demo UI)
  deploy.sh stop.sh clean.sh demo.sh check.sh common.sh
  compose.yaml Containerfile git-stream.toml
  etc_zuul/               zuul.conf (GitLab connection to git-stream), tenant
  repos/                  seed projects: zuul-config, bundles, ci-tool
  demo-ui/                demo web page (stdlib Python + one HTML file)
docs/                     the documentation listed above
```

## Setting up and testing

Zuul's source is expected next to this repo (`../zuul`); the contract
tests import it.

```sh
uv venv -p 3.12 .venv
uv pip install -p .venv/bin/python -e '.[test]' ../zuul
.venv/bin/python -m pytest -q tests        # 35 tests, ~20 s
```

`tests/test_zuul_contract.py` is skipped if Zuul isn't importable; make
sure it actually runs (it should report as passed, not skipped).

The demo (needs podman + podman-compose; first run builds Zuul images,
~25 min; later ~1 min):

```sh
cd devenv && ./deploy.sh          # then http://localhost:8080
./stop.sh                         # pause, keep data
./clean.sh -y --keep-images       # wipe state, keep images
```

Exercise it end to end without a browser (see docs/demo.md for more):

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
adapter, notifying the announcing system).

Known gap: the seeder is not fault-tolerant when starting against a
pre-existing `state.json` and git repos (an empty `state.db` makes it try
to re-create projects whose repos already exist, so `update-ref`
crash-loops).  Reset the forge volume (`clean.sh`) or make the seeder
skip existing repos before upgrading an instance this way.
