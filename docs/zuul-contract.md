# What Zuul's GitLab driver needs from git-stream

This is the contract git-stream must honor.  It was derived by reading
Zuul's source (`zuul/driver/gitlab/`: `gitlabconnection.py`,
`gitlabsource.py`, `gitlabmodel.py`, `gitlabreporter.py`,
`gitlabtrigger.py`) at Zuul commit `708e1bb25` (the version the demo
builds).  `tests/test_zuul_contract.py` enforces most of it by running
Zuul's own `GitlabAPIClient` and `GitlabEventConnector` against
git-stream.  **When upgrading Zuul, re-read those files and re-run the
contract tests against the new Zuul.**

The demo is pinned to that commit, but the CI recipe (see [ci.md](ci.md)) installs
Zuul's **latest** commit on every run, so contract drift shows up as a red
`test` job.  Reproduce locally with
`uv pip install -p .venv/bin/python 'git+https://opendev.org/zuul/zuul'`.

## Connection settings (zuul.conf)

```ini
[connection gitlab]
driver=gitlab
server=git-stream                 # hostname; also matched for Depends-On URLs
baseurl=http://git-stream:8090    # API is <baseurl>/api/v4; MR URLs <baseurl>/<project>/merge_requests/<n>
api_token=...                     # sent as "Authorization: Bearer ..."
webhook_token=...                 # must equal X-Gitlab-Token on webhooks
# cloneurl defaults to baseurl: clones <cloneurl>/<project>.git
```

Webhooks go to `http://<zuul-web>/api/connection/<connection name>/payload`.

## REST API calls Zuul makes

| Call | Zuul uses | Notes |
|------|-----------|-------|
| `GET /projects/:id/merge_requests/:iid` | `sha`, `diff_refs.base_sha/head_sha`, `target_branch`, `author.username`, `title`, `state` (`opened`/`merged`), `merge_commit_sha`, `merge_status` (`can_be_merged`), `blocking_discussions_resolved`, `description` (→ Depends-On, `zuul.message`), `labels`, `updated_at` | Retries with sleeps up to 4 times if `diff_refs` is missing; always return it |
| `GET .../merge_requests/:iid/approvals` | `approvals_left` if present, else `approved` | git-stream returns `approved` |
| `GET /projects/:id/repository/branches?per_page=100&page=N` | `name`, `protected` | Zuul pages until an **empty list** |
| `GET /projects/:id/repository/branches/:name` | `commit.id`, `protected` | 404 is treated as "no branch" |
| `POST .../merge_requests/:iid/notes` (form `body`) | reporting | |
| `POST .../merge_requests/:iid/approve` (form `sha`) | reporter `approval: true` | 401 = already approved, 409 = sha mismatch (both tolerated) |
| `POST .../merge_requests/:iid/unapprove` | reporter `approval: false` | 404 = not approved (tolerated) |
| `PUT .../merge_requests/:iid/merge` (`squash`) | reporter `merge: true` | Response `state` must be `merged`; 406 on conflict |
| `PUT .../merge_requests/:iid` (`add_labels`, `remove_labels`) | reporter `label`/`unlabel` | |

Project ids are `quote_plus(project_name)`, so `group/name` arrives as
`group%2Fname`.  git-stream also accepts numeric ids.

## Webhooks Zuul understands

Validated by `X-Gitlab-Token`.  `object_kind` selects the handler:

- `merge_request`: needs `project.path_with_namespace`,
  `object_attributes.{iid, title, target_branch, last_commit.id, action,
  created_at, updated_at}`, and a `changes` object.  Action mapping:
  `open`→opened, `close`→closed, `merge`→merged, `reopen`→reopened,
  `approved`/`unapproved`, `update` + `oldrev`→changed (new commits),
  `update` + `changes.description`→changed (message changed),
  `update` + `changes.labels.{previous,current}[].title`→labeled.
  Other actions are ignored.
- `note`: `object_attributes.note` (the comment) and `merge_request.{iid,
  title, last_commit.id, target_branch}`; becomes a `gl_merge_request`
  event with action `comment`.
- `push`: `ref`, `before`, `after`, `commits[].{added, modified,
  removed}`.  `before` all zeros = new branch (clears branch cache).
- `tag_push`: `ref` (`refs/tags/...`), `after`.

After a merge-request event, Zuul immediately GETs the MR, so state must
be committed before the webhook is sent (it is: emit happens last).

## What Zuul does not see

An MR opened with a `callback_url` (README, "Callbacks") looks like any other
to Zuul.  The `callback_url` is stored but never sent anywhere Zuul reads.
The MR JSON the REST API returns has one extra field, `callback` (a list of
what was sent to the announcer, or `null`), which Zuul's driver ignores, as
it does the other non-GitLab fields (`stream_key`, `escalation`).  The
webhooks carry no `callback` at all: `_hook_mr_attrs` removes it.
`test_an_mr_with_a_callback_url_looks_the_same_to_zuul` drives Zuul's client
and webhook parser against such an MR.

## Git

- Zuul clones `<cloneurl>/<project>.git` over HTTP and fetches
  `refs/merge-requests/<iid>/head` to test an MR.  Those refs must be
  advertised (git-stream serves every ref).
- Zuul loads config from **every branch** of untrusted projects; don't
  create stray branches (git-stream never creates MR source branches).

## Behaviors worth knowing

- `Depends-On:` URLs are matched with
  `/(.*?)/(?:-/)?merge_requests/(\d+)` and routed to the connection whose
  `canonical_hostname`, `server` or `baseurl` host matches.
- The GitLab driver has no reverse dependency lookup
  (`getChangesDependingOn` returns `[]`).
- `isUpdateOf` compares `updated_at` in whole seconds.
- Zuul's report comments come back as note webhooks; pipeline comment
  regexes must not match them (`Build succeeded.`, `Build failed.`,
  `Starting <pipeline> jobs.`).
- Zuul job variables from an MR: `zuul.change` (iid), `zuul.patchset`
  (sha), `zuul.message` (base64 description), `zuul.items` (includes
  Depends-On items ahead).
