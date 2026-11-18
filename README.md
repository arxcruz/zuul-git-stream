# zuul-git-stream

A small GitLab-compatible forge whose events come from a stream.  Other
systems (a build system, a release tool, a bot) send it events over
HTTP; it turns them into git commits, merge requests and
comments, and sends [Zuul](https://zuul-ci.org) the webhooks a real
GitLab would send.

Zuul uses its **stock GitLab driver**: no plugin, no patch, upstream
images.  Everything Zuul can do with GitLab works, including reporting
back with comments and approvals and testing with `Depends-On:`.

```
build system ───HTTP events──▶ git-stream ──GitLab webhooks──▶ Zuul
                                  ▲                              │
                                  └── REST API + git over HTTP ◀─┘
                                      (reads MRs, clones, comments, approves)
```

## Quick start: run the demo

Everything runs in containers.  The host only needs **podman** (with
**podman-compose**), **git** and **curl**.

```sh
# macOS
brew install podman podman-compose
# Fedora / RHEL
sudo dnf install podman podman-compose git curl
```

```sh
cd devenv
./deploy.sh --open
```

The first run builds the Zuul images natively from Zuul's source (about
25 minutes; upstream images are amd64 only).  Later runs take about 20
seconds.  Then open:

| URL | What |
|-----|------|
| http://localhost:8080 | Demo UI: announce bundles, re-run failures, test with a fix |
| http://localhost:8090 | git-stream: projects, merge requests, Zuul's comments |
| http://localhost:9000 | Zuul web (status, builds, live consoles) |
| http://localhost:8000 | Job logs |

```sh
./demo.sh                  # guided walkthrough in the terminal (for presentations)
./stop.sh                  # pause: stop everything, keep all data
./deploy.sh                # start again, with history intact
./clean.sh                 # remove containers, volumes, images and the Zuul source
./clean.sh --keep-images   # same, but keep the images for a fast redeploy
```

The demo is also reachable from other computers on the network;
`deploy.sh` prints the URLs (set `DEMO_HOST` to override the address).

### The scenario

- **A bundle is announced.**  The build system sends one event to
  git-stream.  git-stream opens a merge request on the
  `bundles` project that adds `bundle.yaml`, and Zuul runs 10 bundle
  tests.  They fail at random, like a flaky network, and
  `bundle-security` always fails because `ci-tool` has a bug.
- **Failures are retried, then escalated.**  When Zuul reports a failure,
  git-stream comments `rerun failed` by itself, up to 3 runs per job.
  The flaky jobs pass on a retry; `bundle-security` keeps failing, so
  git-stream escalates it: labels the merge request `escalated`, comments
  a summary, and shows it on http://localhost:8090/escalations, on the
  merge request's page and in the demo UI.
- **Re-run by hand.**  A `rerun failed` comment on the merge request
  triggers the `bundle-rerun` pipeline.  `bundle-dispatch` reads the
  comment back from git-stream's API and tells Zuul which tests to run
  (`zuul_return` `child_jobs`).  `rerun all` and `rerun api auth` work too.
- **Test with an unmerged fix.**  A ci-tool merge request fixes the bug.
  Adding `Depends-On: <its URL>` to the bundle's description makes Zuul
  test the bundle with the fix speculatively merged.  When everything
  passes, Zuul approves the merge request.
- **Merge the fix** (a push to ci-tool's master) or **break ci-tool
  again** (another push) from the UI.

Zuul's configuration is in `devenv/etc_zuul` (an ordinary `[connection
gitlab]` pointing at git-stream) and `devenv/repos/zuul-config`.

Zuul's links to merge requests use the name Zuul knows git-stream by,
`http://git-stream:8090`.  The demo UI rewrites them; to follow them from
Zuul's web UI, add `127.0.0.1 git-stream` to `/etc/hosts`.

## The stream

An event is a JSON object with an `action`.  Send it to `POST
/api/stream` (a list of events works too).

| action | fields | GitLab event Zuul gets |
|--------|--------|------------------------|
| `create_project` | `project`, `files` | none |
| `push` | `project`, `files`, `branch`, `message` | push |
| `tag` | `project`, `tag`, `rev` | tag push |
| `open_mr` | `project`, `title`, `files`, `description`, `target_branch`, `key`, `labels`, `max_attempts` | merge request opened |
| `update_mr` | `project`, `mr`, `files`, `title`, `description` | merge request changed |
| `comment` | `project`, `mr`, `note` | comment |
| `label` | `project`, `mr`, `add`, `remove` | merge request labeled |
| `approve`, `unapprove` | `project`, `mr` | approved, unapproved |
| `close`, `reopen` | `project`, `mr` | closed, reopened |
| `merge` | `project`, `mr`, `squash` | push and merged |

- `files` maps paths to text; `null` deletes a file.
- Senders don't have to keep track of merge request numbers: a merge
  request can be given a `key` when it's opened (e.g. a bundle ID) and
  addressed by it later (`"mr": "b-0042"`).  Opening a merge request with a key that is still
  open updates it instead.
- An event `id` makes a resent event a no-op.
- Every event may carry `author` (default `git-stream`).

```sh
curl -X POST localhost:8090/api/stream -d '{
  "action": "open_mr", "project": "bundles", "key": "b-0042",
  "title": "Bundle b-0042", "files": {"bundle.yaml": "id: b-0042\n"}}'
curl -X POST localhost:8090/api/stream -d '{
  "action": "comment", "project": "bundles", "mr": "b-0042", "note": "recheck"}'
```

## Retries and escalation

With `[retry] zuul_api` set, git-stream watches Zuul's reports.  When Zuul comments
`Build failed` on a merge request, git-stream asks Zuul's builds API how
many times each failing job failed on the merge request's current
version, then:

- jobs that failed fewer than `max_attempts` times (default 3, counting
  the first run) are re-run: git-stream comments `rerun failed`, or
  `rerun <jobs>` when only some of them have attempts left;
- when nothing is left to retry, the merge request is escalated once per
  version: the escalation is recorded and shown on the `/escalations`
  page and the merge request's page (`GET /api/stream/escalations` returns
  them as JSON), the merge request is labeled `escalated` and commented,
  and each `[retry] escalate` URL, if any, gets a POST like this (for
  example, something that opens Jira tickets):

```json
{"event": "escalation", "project": "bundles", "mr": 1, "key": "b-0042",
 "title": "Bundle b-0042", "url": "http://git-stream:8090/bundles/merge_requests/1",
 "sha": "...", "time": 1794916534.7, "max_attempts": 3,
 "failing": [{"job": "bundle-security", "attempts": 3, "result": "FAILURE",
              "log_url": "...", "build_url": "..."}]}
```

A bundle can set its own limit with `"max_attempts"` in `open_mr`
(`0` turns retries and escalation off for it).  A new version of the
merge request (new files) starts the count over.  The re-runs rely on a
pipeline that turns a `rerun ...` comment into the right jobs; see
`bundle-rerun` and `bundle-dispatch` in `devenv/repos/zuul-config`.

## What git-stream serves

- **GitLab REST API (v4)**, the part Zuul uses: projects, branches,
  merge requests, approvals, notes, approve/unapprove, merge, labels,
  plus raw files.  Reads are anonymous; writes need an API token.
- **git over HTTP**, read only: `git clone http://host:8090/bundles.git`.
  Merge requests are at `refs/merge-requests/<iid>/head`, like GitLab.
  Changes come in through the stream, not `git push`.
- **Webhooks** to every configured URL, in order, retried until accepted,
  with the `X-Gitlab-Token` header.  Recent deliveries are at
  `GET /api/stream/deliveries`.
- **Web pages** at `/`, `/escalations`, `/metrics` and
  `/<project>/merge_requests/<iid>`.
- **Metrics** in a SQLite database (opt-in, `[metrics] path`): one row per
  completed build, escalation and webhook delivery.  `GET
  /api/stream/metrics` returns per-job run/pass/fail counts; the `/metrics`
  page shows them.  It is a log of what git-stream saw, not a source of
  truth (Zuul is), and it is separate from the forge state in `state.db`.

State is a JSON file next to bare repositories in the data directory.

## Running it

```sh
pip install zuul-git-stream
git-stream --config /etc/git-stream/git-stream.toml
```

All settings live in one TOML file: `--config`, or `$GIT_STREAM_CONFIG`,
or `/etc/git-stream/git-stream.toml`.  `devenv/git-stream.toml` is a
complete example, and `git_stream/config.py` describes every option:

```toml
[server]
listen = "0.0.0.0:8090"
base_url = "http://git-stream:8090"   # how Zuul reaches git-stream
data = "/srv/git-stream"              # repositories and state
seed = "/repos"                       # optional: create projects from these dirs

[api_tokens]                          # user = token, for API writes
zuul = "API_TOKEN"

[webhooks]
urls = ["http://zuul-web:9000/api/connection/gitlab/payload"]
token = "WEBHOOK_TOKEN"

[retry]                               # off without zuul_api
zuul_api = "http://zuul-web:9000/api/tenant/TENANT"
max_attempts = 3
escalate = []                         # URLs to POST escalations to

[metrics]                             # what the jobs did, in a database
path = "/srv/git-stream/metrics.db"    # "" leaves it off
build_url = "http://zuul-web:9000/build"
```

Environment variables (`GIT_STREAM_MAX_ATTEMPTS`, `GIT_STREAM_WEBHOOKS`,
...) override the file, and command line options (`git-stream --help`)
override both.  `git` (2.38 or newer) must be installed.

And in `zuul.conf`:

```ini
[connection gitlab]
driver=gitlab
server=git-stream
baseurl=http://git-stream:8090
api_token=API_TOKEN
webhook_token=WEBHOOK_TOKEN
```

## Tests

```sh
pip install -e '.[test]' 'git+https://opendev.org/zuul/zuul'
pytest
```

`tests/test_zuul_contract.py` drives Zuul's own `GitlabAPIClient` against
git-stream and feeds the webhooks it sends to Zuul's own webhook parser,
so a change Zuul wouldn't understand fails there.  It fails (it is not
skipped) when Zuul isn't installed; `pytest -m "not contract"` leaves it out
for a quick local run.

## Documentation

- [docs/architecture.md](docs/architecture.md): components, state, flows
- [docs/zuul-contract.md](docs/zuul-contract.md): exactly what Zuul's GitLab driver needs
- [docs/demo.md](docs/demo.md): the podman demo, its Zuul config and APIs
- [docs/security.md](docs/security.md): secrets, tokens, and the production deploy
- [docs/decisions.md](docs/decisions.md): why it's built this way
- [docs/roadmap.md](docs/roadmap.md): status and what's next
- [docs/backlog.md](docs/backlog.md): fixes and specs still to build
- [AGENTS.md](AGENTS.md): how to continue the work (for AI agents and people)

## License

Apache 2.0
