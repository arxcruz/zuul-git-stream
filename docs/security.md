# Security

Goal: a real deploy of git-stream with no secret "in an open format" — no
token in a committed config file, no plaintext on the wire, no unauthenticated
channel that an attacker can reach.

## Where secrets live, and the gaps

| Secret | Who uses it | Status |
|--------|-------------|---------|
| `[api_tokens]` (Zuul → git-stream REST writes) | Zuul `GitlabAPIClient` sends `Authorization: Bearer` | `GIT_STREAM_API_TOKENS`; none configured means writes are refused |
| `[webhooks] token` (git-stream → Zuul) | git-stream sends `X-Gitlab-Token` | `GIT_STREAM_WEBHOOK_TOKEN` |
| `[stream] tokens` (`POST /api/stream`) | the announcing system | `GIT_STREAM_STREAM_TOKENS`; none configured means the stream is refused (401); the message acts as the token's user (it cannot claim another) |
| git clone/fetch | Zuul executor/merger | anonymous by default; a token is a deployment option (below) |
| `[retry] zuul_api` URL | git-stream reads Zuul's builds API | unauthenticated; gate it at the network (below) |

Tokens (`[api_tokens]`, `[stream] tokens`) are compared with
`hmac.compare_digest` against every configured token, without stopping at the
first match, so response time does not tell a client how much of a token it
guessed (`_match_token` in `forge.py`).  Token length is not hidden.

## Secure by default

With no `[api_tokens]`, REST writes are 401 ("no API tokens are
configured"); with no `[stream] tokens`, `POST /api/stream` is 401 ("no
stream tokens are configured").  Startup logs a warning that says which is
refused and how to allow it.  Reads (GET) stay open.

`[server] insecure = true` (or `GIT_STREAM_INSECURE=true`, `--insecure`) is
the explicit opt-in for demos and throwaway setups: with no tokens, any API
token is accepted and acts as `zuul`, and the stream is open and acts as
`git-stream`.  It never weakens tokens that are configured, and startup
logs a loud warning.  Never use it where anyone untrusted can reach the
port.

## Request limits

Every body read and parse is bounded, so a client cannot fill memory or pin
a handler thread:

| Setting | Default | Effect |
|---------|---------|--------|
| `[server] max_body` | 10 MiB | a larger body is **413**, refused from the declared length (or, chunked, from the chunk size) before it is read |
| `[server] read_timeout` | 30 s | a client silent this long while sending is **408** and dropped; also drops idle keep-alive connections and half-sent request lines |
| `[server] body_timeout` | 120 s | a whole body must arrive within this, however steadily it trickles (**408**) |
| `[server] max_connections` | 64 | at most this many connections are served at once; another is refused at once with **503**, `Retry-After: 1` and `Connection: close`, and no thread is started for it |

The cap counts connections, not requests: a slot is taken when a connection
is accepted and given back when its handler ends, however it ends (a close, a
reset, a 408, an error, or a thread that cannot be started), so a keep-alive
connection holds one slot for as long as it is open.  `/api/stream`
connections are long-lived and count against the cap while they last; the
default of 64 assumes the demo opens only a handful of them (unverified).
The refusal is written with a 1 second send timeout on the accept thread, so
a client that will not read it cannot stall the accept loop.  Zuul's REST
client retries that 503 because of `Retry-After` (verified by the contract
test `test_zuul_retries_a_503_from_a_full_connection_cap`; without the header
it raises).  Git smart HTTP does not: a refused `git fetch` or `clone` fails
(`docs/backlog.md`).  A flood of silent clients can still fill the cap and
refuse legitimate ones until its connections time out (`read_timeout`); a
per-client limit is not built.

A client that vanishes mid-request (reset, broken pipe) is dropped quietly,
and the listen backlog is 128, not the standard 5, so a burst of connections
is not refused.  The body is read before the forge's lock is taken, so a slow client never
blocks the other clients.  A refusal closes the connection, because the rest
of the body is still in the stream.  Malformed input is a 4xx and never
echoes an exception: a bad `Content-Length` (not a plain number), a bad chunk
size or chunk framing, a body that is not UTF-8 or valid JSON, a JSON body
that is not an object, and a `page`, `per_page` or merge request id that is
not a positive number are all 400.  An unexpected error is a 500 whose body
says only `internal error`; the details are in the log.  What is not covered
yet (request-line and header trickling, the `git http-backend` subprocess) is
in `docs/backlog.md`.

## Callbacks: git-stream POSTs to a URL the sender chooses

`open_mr` may carry a `callback_url`, and git-stream then POSTs the outcome
of the merge request to it (README, "Callbacks").  This is the one place
where git-stream makes a request to an address a sender picked, so:

- **Anyone allowed to use the stream can aim git-stream at any http(s)
  host**, including addresses only git-stream can reach (SSRF).  There is no
  allowlist yet (backlog).  Give `[stream] tokens` only to senders you trust,
  and keep git-stream off networks it doesn't need.
- **No authentication header** is sent, so a receiver can't tell a real
  callback from a forged one.  Don't act on one without checking it with
  the sender's own system.
- **The URL is never echoed**: it may hold a secret in its query.  The merge
  request JSON shows only the outcome, the sha, the time and `delivered` or
  `failed: <cause>` (the URL is removed from the cause); the pages, the
  escalation list and the webhooks to Zuul show none of it.  The URL is stored in `state.db`, so
  protect that file like the tokens.
- **Bounded and validated**: only `http` and `https` with a host pass, at
  most 2048 characters, no whitespace or control characters (DEL included),
  checked before anything is created.  One
  attempt, redirects refused, the body is fixed JSON, and a hung receiver
  can't delay Zuul or other merge requests.  The 10 second timeout applies to
  each socket operation (connect, each read), not to the whole delivery, so a
  receiver that sends a byte every few seconds can keep its thread for much
  longer (backlog).
- **No proxy**: `HTTP_PROXY`, `HTTPS_PROXY` and friends are ignored, so a
  callback goes straight to its URL and can't be sent through a proxy that
  was set up for something else.
- Zuul never sees the URL.  The `callback` status list is in the merge
  request JSON of the REST API (Zuul ignores it) but not in the webhooks;
  see [zuul-contract.md](zuul-contract.md).  The pages don't show the
  callback at all (backlog).

## The demo's secrets

The demo runs with `insecure = true`, so it is protected by where it
listens, not by tokens:

- `compose.yaml` publishes every port on `127.0.0.1` (`DEMO_BIND`, in
  the environment of `deploy.sh` or in `devenv/.env`, changes that; do it only on a network you trust).
- No secret is committed.  `deploy.sh` creates `devenv/.env` (mode 600,
  git-ignored) from `devenv/.env.example`, with a random value for each of
  `MYSQL_ROOT_PASSWORD`, `MYSQL_PASSWORD`, `ZUUL_KEYSTORE_PASSWORD`,
  `ZUUL_API_TOKEN` and `WEBHOOK_TOKEN`; an existing `.env` is kept.
  Compose passes them to the containers as environment variables, and
  git-stream reads the API and webhook tokens from
  `GIT_STREAM_API_TOKENS` / `GIT_STREAM_WEBHOOK_TOKEN`, not from
  `git-stream.toml`.
- Zuul's config has no environment substitution, so `deploy.sh` renders the
  git-ignored `etc_zuul/zuul.conf` from `etc_zuul/zuul.conf.in`.
- The secrets belong to the volumes: `clean.sh` removes `.env` and
  `zuul.conf` with them, and changing a secret by hand needs `clean.sh`
  first (the database and Zuul's keystore keep the old one).
- To run the demo with a stream token instead of `insecure`, set
  `GIT_STREAM_STREAM_TOKENS` and make the demo UI send it (not done: the
  demo UI sends none).

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
  the most dangerous surface: anyone who can reach git-stream could open MRs
  and trigger Zuul jobs.  It is refused unless tokens are configured or
  `insecure` is set (`tests/test_insecure_defaults.py`).
- **Demo secrets out of the repo** (see above).
- **Stream identity** (code + `tests/test_stream_identity.py`): a message
  acts as its token's user, never as the `author` it claims.  On an open
  (insecure) stream the `[retry] report_users` names are refused, so nobody can forge
  Zuul's `Build failed` and drive the retry and escalation path.
- **Bounded bodies** (code + `tests/test_server_limits.py`,
  `tests/test_server_input.py`): see Request limits above.

Planned / deployment (one step each):

1. **TLS + private network.** git-stream behind a TLS reverse proxy; internal
   Zuul↔git-stream network; gate `*.git` paths at the proxy.
2. **Builds-API read.** Gate Zuul's web API at the proxy (or a Zuul-side auth
   in front of it) so git-stream's retry reads aren't open.
3. **Docs.** a production deploy section in the README pointing here.

## Not done (later, from the roadmap)

- Callbacks: an allowlist of URLs, an authentication header, retries and
  durable delivery (see the backlog).
- **Durable webhook and retry queues.**  The forge *state* is already in
  SQLite (`state.db`) and the metrics log is in `metrics.db`, but the
  *delivery* of webhooks (to Zuul) and the retry policy run in-process on a
  single worker each, and a failed send is retried in memory only.  A
  durable queue (SQLite-backed) is what the roadmap means by this — it is
  about *resilience* of the network, not just privacy.
- A read-only token that can fetch git but never write the API, if you want to
  split "who can clone" from "who can drive the API."
