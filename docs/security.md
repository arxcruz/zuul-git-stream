# Security

Goal: a real deploy of git-stream with no secret "in an open format" — no
token in a committed config file, no plaintext on the wire, no unauthenticated
channel that an attacker can reach.

## Where secrets live, and the gaps

| Secret | Who uses it | Gap today |
|--------|-------------|-----------|
| `[api_tokens]` (Zuul → git-stream REST writes) | Zuul `GitlabAPIClient` sends `Authorization: Bearer` | token is in the mounted, world-readable `git-stream.toml` |
| `[webhooks] token` (git-stream → Zuul) | git-stream sends `X-Gitlab-Token` | same: in the TOML file |
| `[retry] zuul_api` URL | git-stream reads Zuul's builds API | **no auth at all** |
| git clone/fetch | Zuul executor/merger | **anonymous** — no `api_token_name`, no server-side check |
| `POST /api/stream` | the announcing system | **unauthenticated** — anyone who can reach git-stream can open MRs and trigger Zuul jobs |

## The layers

1. **Secrets out of the file.** The config file keeps non-sensitive settings;
   tokens come from the environment, injected by the container runtime. The
   code already overrides via `GIT_STREAM_*` (`config.py`); the demo file and
   `zuul.conf` get stripped of the actual tokens.
2. **Private network.** git-stream publishes no ports except through a
   TLS-terminating reverse proxy; Zuul↔git-stream is on an internal network.
   Reaching git-stream requires being on the network / presenting a client
   cert (mTLS optional), so a token in transit is not "in an open format."
3. **TLS on every hop.** git-stream and Zuul web behind a proxy with TLS.
4. **Auth the open channels.** git-over-HTTP requires a credential
   (`[git]`); the builds-API call uses optional basic-auth; `/api/stream`
   requires per-sender tokens (`[stream_tokens]`).
5. **Minimize token value.** A long-lived token in a private, TLS-only
   network exposing only projects/MRs/notes is low-value if leaked.

## Implementation (one step per commit)

Each step: code + test, then keep README / `config.py` docstring /
`devenv/git-stream.toml` / `docs/` in sync.

1. **`[git]` auth for git-over-HTTP.** `server.py:_git` checks a credential
   (Basic auth via `git http-backend`'s `REMOTE_USER`, or a token) and
   `update-ref`/receive stay read-only. Zuul sets `api_token_name` so its
   clones authenticate. Test: anonymous clone 401, with-credential clone 200.
2. **Sender tokens on `POST /api/stream`.** `[stream_tokens]` = user → token;
   `server.py:_stream` requires one (off = open, with a loud warning). Test.
3. **Basic-auth for the builds-API call.** `[retry] basic_auth = "user:token"`
   applied in `retry.py:results()`. Test with the fake builds API.
4. **Strip the demo file of tokens.** `devenv/git-stream.toml` and
   `devenv/etc_zuul/zuul.conf` use env placeholders; tokens set via
   `environment:` in `compose.yaml` (or a secrets file outside the repo).
5. **TLS + private network.** `devenv` profile: git-stream no longer publishes
   8090 directly; a reverse proxy in front; internal Zuul↔git-stream network;
   optional mTLS. Document the production topology in this file.
6. **Docs.** production deploy section in the README pointing here.

## Not done (later)

- Durable webhook/retry queues and SQLite (roadmap) — needed for the network
  to be *resilient* too, not just private.
- A read-only token that can fetch git but never write the API, if you want to
  split "who can clone" from "who can drive the API."
