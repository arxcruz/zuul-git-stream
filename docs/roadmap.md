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

## Next, by priority

Engineering follow-ups (bugs, hardening, specs waiting for code, tooling) are
in [backlog.md](backlog.md); this list is the product and production roadmap.

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

5. TLS / reverse proxy guidance, request size limits,
   (metrics: see "Done" above), structured logs.
6. Optional read token for the API and git.

### Features
- **Jira adapter** (user wants it later): consume `[retry] escalate`
  POSTs and open/update a Jira issue; store the issue key back on the
  escalation (needs an API to attach it) and show it on the pages.
- **Notify the announcing system** of outcomes: a per-MR `callback_url` is
  done (README, "Callbacks"); what is left is generic outgoing event
  subscriptions, and the hardening in the backlog (retries, durable
  delivery, an auth header, an allowlist).
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
