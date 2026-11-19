# Spec: cap on concurrent connections

## Purpose

A flood of connections cannot make git-stream start an unbounded number of
threads.  Today each connection is a thread for as long as it lasts (at most
`read_timeout` when silent, `body_timeout` when trickling), and nothing limits
how many there are.  This is the S1 item in `docs/backlog.md`.

## Scenarios

1. Fewer open connections than `max_connections`: every request is handled
   exactly as today.
2. A connection arrives while `max_connections` are open: it is refused at
   once, not queued, and no handler thread is started for it.
3. A refused client gets `503 Service Unavailable` with `Retry-After: 1`,
   `Connection: close` and a plain-text body that says the server is busy and
   to retry.  The refusal is not logged above debug level per connection.
4. A client that drops mid-request (reset, broken pipe, 408 timeout, or a
   handler error) frees its slot when its handler ends.  A slot is held per
   connection, not per request, so a keep-alive connection holds one slot for
   its whole life and releases it when it closes.
5. When a slot is freed, the next new connection is accepted.  Connections
   already open are never closed because the cap is full.
6. `max_connections` below 1, or not a number, is a configuration error that
   names the option, like the other `[server]` limits.
7. A cap that is full does not stop Zuul's webhooks being sent (those are
   outgoing and use no slot).

## Fires on

Every accepted connection on the HTTP server, whatever the endpoint: the API,
`/api/stream`, the HTML pages and git smart HTTP.  The cap is on the
connection, so it applies before the request is read.

`/api/stream` connections are long-lived and count against the cap for as
long as they last.  The default of 64 assumes the demo opens only a handful of
them (unverified).

Configured like `read_timeout`:

- `[server] max_connections` in the TOML file, default **64**.
- Environment `GIT_STREAM_MAX_CONNECTIONS`, CLI `--max-connections N`
  (precedence as for every setting: defaults < file < env < CLI).
- Validated as at least 1.

## Decisions

All five are settled.

| Decision | Choice | Why |
|----------|--------|-----|
| Reject or queue | **Reject at once** | Bounds threads, sockets and waiting; no new timeout setting; Zuul's GitLab REST client retries GETs on a 503 with `Retry-After` (verified by the contract test, REST client only; git smart HTTP does not retry, see `docs/backlog.md`) |
| Default value | **64** | Room for Zuul's executors, mergers, scheduler and the demo UI, well under typical thread limits |
| Name and config | **`max_connections`**, `[server]`, env, CLI flag, at least 1 | Follows `max_body` and `read_timeout`; no unit in the name; no "0 means off" so the validation rule stays the same |
| Refusal | **503 + `Retry-After` + close** | Overload of the whole server is a 503, not a per-client 429; plain clients and Zuul understand it; a bare reset would look like a network fault |
| Dropped client | **Slot freed when the handler ends** | One release path covers reset, timeout and error; counting connections, not requests, keeps keep-alive simple |

If the contract test shows that Zuul does not retry a 503 with `Retry-After`,
the default and the docs must be revisited before the S1 item is closed.

## Out of scope

A thread pool or async rewrite, a per-client (per-IP) limit, a queue or wait
time before refusing, a hard maximum connection lifetime, and a timeout for
the `git http-backend` subprocess.  Request-line and header trickling and the
subprocess timeout are already in `docs/backlog.md` (S1, "Request limits"
item); a per-IP limit goes there too.  The S1 item for the cap itself is
deleted when this is done.

## Done means

- Tests for each scenario above: under the cap, refused at the cap (with a
  deterministic count of held connections, not sleeps), the 503 headers and
  body, a slot freed after a client reset, a slot freed after a 408, a slot
  freed after a handler error, a keep-alive connection holding one slot, a
  full cap leaving open connections working, a slot released when starting
  the handler thread fails, and each bad option value (file, env, CLI).
  Tests use port 0 and `tmp_path` and are safe under
  `-n auto`.
- A Zuul contract test in `tests/test_zuul_contract.py` that drives Zuul's
  real `GitlabAPIClient` against a server at its cap, to check that its GET
  retry honors the 503 and `Retry-After` and then succeeds once a slot is
  free.  No API field or webhook changes, so the existing contract tests stay
  unchanged and passing.
- `docs/security.md` (Request limits table and text), the `config.py`
  docstring and defaults, the README settings, `devenv/git-stream.toml` (a
  commented `# max_connections = 64`) and the CLI help updated; the S1
  backlog item removed from `docs/backlog.md`.
- The full gate (`scripts/check.sh`) green.

## Constraints

- Stdlib only; Zuul unchanged.
- The refusal must never block the accept loop: the 503 is written with a
  short send timeout, and a client that will not read it is dropped.
- A slot is released exactly once, in a path that runs on every exit of the
  connection, including exceptions and a failure to start the handler thread.
- Behavior below the cap must not change, including keep-alive and the
  existing 408/413 handling.
- The default of 64 is a deliberate change in behavior for installs that
  expect more than 64 concurrent connections; the README says how to raise it.
