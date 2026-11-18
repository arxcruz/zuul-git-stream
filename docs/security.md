# Security

Goal: a real deploy of git-stream with no secret "in an open format" — no
token in a committed config file, no plaintext on the wire, no unauthenticated
channel that an attacker can reach.

## Where secrets live, and the gaps

| Secret | Who uses it | Status |
|--------|-------------|---------|
| `[api_tokens]` (Zuul → git-stream REST writes) | Zuul `GitlabAPIClient` sends `Authorization: Bearer` | token moves to the environment (step 4) |
| `[webhooks] token` (git-stream → Zuul) | git-stream sends `X-Gitlab-Token` | token moves to the environment (step 4) |
| `[stream] tokens` (`POST /api/stream`) | the announcing system | sender token required when configured, and the message acts as the token's user (it cannot claim another); open + warned by default, where `report_users` names are refused as authors |
| git clone/fetch | Zuul executor/merger | anonymous by default; a token is a deployment option (below) |
| `[retry] zuul_api` URL | git-stream reads Zuul's builds API | unauthenticated; gate it at the network (below) |

## The layers

1. **Secrets out of the file.** The config file keeps non-sensitive
   settings; tokens come from the environment (`GIT_STREAM_*`), injected by the
   container runtime. The demo file and `zuul.conf` stop containing tokens.
2. **Private network.** git-stream publishes no ports except through a
   TLS-terminating reverse proxy; Zuul↔git-stream and the builds-API reads are
   on an internal network. Reaching git-stream requires being on the network,
   so a token in transit is not "in an open format."
3. **TLS on every hop.** git-stream and Zuul web behind a proxy with TLS.
4. **Auth the open channels.** `POST /api/stream` requires per-sender tokens
   (`[stream]`), and a sender can only act as its own user (`stream.apply`
   refuses any other `author` with 403); the builds-API read is gated by the network (Zuul's web API
   is not token-scoped per caller, so a reverse-proxy auth in front of it is
   the clean lever).
5. **Minimize token value.** A long-lived token in a private, TLS-only network
   exposing only projects/MRs/notes is low-value if leaked.

## git over HTTP

git-stream serves git read-only; clones are **anonymous by default** (Zuul and
the tests rely on this). For a deploy where git must not be open to the
world, the cleanest lever is **network gating** (a reverse proxy / private
network) rather than per-clone tokens:

- `git http-backend` (what git-stream shells out to) does **no access
  control** — `http.uploadpack`/`http.receivepack` are *booleans* in its
  config, not tokens, so there is no stock per-repo token to check.
- A custom 401 in front of the backend does not reliably drive git's
  `user:token` URL credentials on some builds (Apple git reads `info/refs`
  first and does not always re-auth a second 401), so it is not wired up.
- The supported path: put git-stream behind a reverse proxy that requires a
  credential (or restrict by source address) for `*.git` paths, and have Zuul
  set `api_token_name` so its clones carry one.

In other words: for a private network, anonymous git is fine; for a
network-exposed git-stream, gate the `.git` paths at the proxy.

## Implementation

Done (committed):

- **`[stream]` sender tokens** on `POST /api/stream` (code + test). This is
  the most dangerous open surface: unauthenticated, anyone who can reach
  git-stream can open MRs and trigger Zuul jobs.
- **Stream identity** (code + `tests/test_stream_identity.py`): a message
  acts as its token's user, never as the `author` it claims.  On an open
  stream the `[retry] report_users` names are refused, so nobody can forge
  Zuul's `Build failed` and drive the retry and escalation path.
- **Robust `_body`** (no-Content-Length / closed-connection → empty body,
  not a 500).

Planned / deployment (one step each):

1. **Tokens out of the demo file.** `devenv/git-stream.toml` and
   `devenv/etc_zuul/zuul.conf` use env placeholders; tokens set via
   `environment:` in `compose.yaml` (out of the repo).
2. **TLS + private network.** git-stream behind a TLS reverse proxy; internal
   Zuul↔git-stream network; gate `*.git` paths at the proxy.
3. **Builds-API read.** Gate Zuul's web API at the proxy (or a Zuul-side auth
   in front of it) so git-stream's retry reads aren't open.
4. **Docs.** a production deploy section in the README pointing here.

## Not done (later, from the roadmap)

- **Durable webhook and retry queues.**  The forge *state* is already in
  SQLite (`state.db`) and the metrics log is in `metrics.db`, but the
  *delivery* of webhooks (to Zuul) and the retry policy run in-process on a
  single worker each, and a failed send is retried in memory only.  A
  durable queue (SQLite-backed) is what the roadmap means by this — it is
  about *resilience* of the network, not just privacy.
- A read-only token that can fetch git but never write the API, if you want to
  split "who can clone" from "who can drive the API."
