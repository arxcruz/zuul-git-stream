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

The first `./deploy.sh` creates `devenv/.env` with random database
passwords and tokens (from `devenv/.env.example`; it is not committed) and
writes `etc_zuul/zuul.conf` from `zuul.conf.in`.  The ports are published
on `127.0.0.1` only, because the demo's stream is open
(`[server] insecure = true`).  To reach it from other computers, run
`DEMO_BIND=0.0.0.0 ./deploy.sh` (or set `DEMO_BIND` in `.env`; `DEMO_HOST`
sets the printed URLs), knowing that
anyone who can reach the demo can then drive it.  See
[docs/security.md](docs/security.md).

### The scenario

- **A bundle is announced.**  The build system sends one event to
  git-stream.  git-stream opens a merge request on the
  `bundles` project carrying the bundle as data, and Zuul runs 10 bundle
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
| `create_project` | `project`, `branch`, `description` | none (a placeholder: one commit, empty tree) |
| `push` | `project`, `data`, `branch`, `message` | push |
| `tag` | `project`, `tag`, `rev` | tag push |
| `open_mr` | `project`, `title`, `description`, `target_branch`, `key`, `labels`, `max_attempts`, `callback_url`, `data` | merge request opened (Zuul sees no `callback_url`) |
| `update_mr` | `project`, `mr`, `title`, `description`, `data` | merge request changed |
| `comment` | `project`, `mr`, `note` | comment |
| `label` | `project`, `mr`, `add`, `remove` | merge request labeled |
| `approve`, `unapprove` | `project`, `mr` | approved, unapproved |
| `close`, `reopen` | `project`, `mr` | closed, reopened |
| `merge` | `project`, `mr`, `squash` | push and merged |

- Senders send messages, never files: `files` in any message is a 400 that
  says to send `data`.  git-stream holds no job definitions or Zuul config;
  those live in real repositories on other Zuul connections.
- `open_mr` creates a project that does not exist yet, as a placeholder
  (one commit with an empty tree, on its `target_branch`).  So does
  `create_project`.  Every other action on a missing project,
  `push` included, is a 404.
- `data` is a JSON object (at most 64 KiB and 8 levels deep) that travels
  with the version it was sent with, instead of a file.  Every change of it
  is a new commit, so Zuul sees a changed merge request; an unchanged `data`
  changes nothing.  Jobs read it back, anonymously, from
  `GET /api/v4/projects/<project>/merge_requests/<iid>/data` (add `?sha=`
  for an older version).  Merging the merge request, or a `push` of `data`
  to the default branch, makes it the project's current data at
  `GET /api/v4/projects/<project>/data`.  Needs `sqlite3`.
- Senders don't have to keep track of merge request numbers: a merge
  request can be given a `key` when it's opened (e.g. a bundle ID) and
  addressed by it later (`"mr": "b-0042"`).  Opening a merge request with a key that is still
  open updates it instead.
- An event `id` makes a resent event a no-op.
- Branch names (`push` `branch`, `open_mr` `target_branch`, `create_project`
  `branch`) and `tag` names must be names git accepts: a bad one is a 400
  ("invalid branch name" / "invalid tag name"), and tagging a name that
  exists is a 409.  `push`'s `start` is a revision, not a branch name.
- The stream is secure by default: with no `[stream] tokens` it answers 401
  ("no stream tokens are configured"), and with no `[api_tokens]` the REST
  API refuses writes.  `[server] insecure = true` (demos only) accepts any
  API token and opens the stream.  Configured tokens are always enforced.
- An event acts as the sender, not as whatever its `author` says.  With
  `[stream] tokens` (`user = token`) set, it acts as the user its token
  names (sent as `X-Stream-Token` or `Authorization: Bearer`); `author` may
  repeat that user, and naming anyone else is a 403.
  On an open (insecure) stream it acts as `git-stream`; `author` may be
  any name except `[retry] report_users` (default `zuul`), which is a 403,
  because a forged `Build failed` from Zuul would drive the retries.

```sh
curl -X POST localhost:8090/api/stream -d '{
  "action": "open_mr", "project": "bundles", "key": "b-0042",
  "title": "Bundle b-0042", "data": {"id": "b-0042"}}'
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
merge request (new `data`) starts the count over.  The re-runs rely on a
pipeline that turns a `rerun ...` comment into the right jobs; see
`bundle-rerun` and `bundle-dispatch` in `devenv/repos/zuul-config`.

## Callbacks

An `open_mr` may carry `"callback_url"` (an `http` or `https` URL of at most
2048 characters, with no spaces or control characters, else a 400 before
anything is created).  When the merge request reaches a final outcome,
git-stream POSTs it there as JSON, so the system that announced the bundle
needn't poll:

- `passed`: Zuul approved the merge request;
- `escalated`: the retries ran out (see above).

Each outcome is sent at most once per version of the merge request, so
`escalated` followed by a passing rerun sends both, and a new version starts
over.  Failed attempts and reruns send nothing.

```json
{"event": "outcome", "outcome": "passed", "project": "bundles", "mr": 1,
 "key": "b-0042", "title": "Bundle b-0042",
 "url": "http://git-stream:8090/bundles/merge_requests/1",
 "sha": "...", "time": 1794916534.7, "approved_by": ["zuul"]}
```

`escalated` is the escalation record above with `event` set to `outcome` and
`outcome` added.  Delivery is one attempt with a 10 second timeout, in the
background; a redirect counts as a failure, and the proxy environment
variables (`HTTP_PROXY` and the like) are ignored.  The result is in the merge
request's JSON, as `callback` (no page shows it): a list of `{outcome, sha,
time, status}`, `status` being `delivered`, `failed: <cause>`, or null while
the delivery is under way.  There are
no retries and no authentication header, and the URL is never shown back.
Callbacks of one merge request can arrive out of order; use `time`.

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
- **Message log** in `state.db`: every message sent to `POST /api/stream` is
  recorded with its sender, action, project, key, time, body (up to 16 KiB)
  and outcome (`applied`, `duplicate`, `refused`, `rejected` or `error`).
  `GET /api/stream/messages` (with a stream token) lists it newest first;
  filter with `project`, `key`, `action`, `outcome`, `since` and `until`,
  page with `limit` (at most 200) and `before` (the `next` of the last page).
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
insecure = false                      # true: demos only, see docs/security.md
max_body = 10485760                   # bytes; a larger request body is a 413
read_timeout = 30                     # seconds of silence before a 408
body_timeout = 120                    # seconds to receive a whole body
max_connections = 64                  # open connections; one more gets a 503

[api_tokens]                          # user = token, for API writes
zuul = "API_TOKEN"

[stream.tokens]                       # user = token, for POST /api/stream
build-system = "STREAM_TOKEN"

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
...) override the file; tokens can live there instead of in it:
`GIT_STREAM_API_TOKENS="zuul=..."`, `GIT_STREAM_STREAM_TOKENS="user=..."`,
`GIT_STREAM_WEBHOOK_TOKEN`, and command line options (`git-stream --help`)
override both.  `git` (2.38 or newer) must be installed.

`max_connections` (default 64) bounds how many connections are served at once;
beyond it a client gets `503` with `Retry-After: 1` and Zuul's REST client
retries.  To raise it, set `[server] max_connections`, `GIT_STREAM_MAX_CONNECTIONS`
or `--max-connections N`.  A long-lived `/api/stream` connection holds a slot
for as long as it is open.

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
