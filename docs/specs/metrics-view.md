# Spec: a metrics view in the demo UI

## Purpose

The demo UI gets a page of charts and filters over everything git-stream
records about its work: the builds Zuul ran, the escalations, the webhook
deliveries and the stream messages.  The numbers on it are correct, which
they are not today (scenarios 1 and 2).

## Scenarios

1. A build is recorded once.  `metrics.record_results` runs on every Zuul
   report and re-inserts every build of the merge request version, so a live
   database holds up to three rows per build uuid (observed: 43 rows for 17
   uuids).  A run is stored once per uuid; a repeated report adds nothing; a
   database that already has duplicates is de-duplicated when it opens, and
   keeps one row per uuid.
2. A run has its times.  Zuul's builds API gives `start_time` and `end_time`
   as ISO strings and `duration` as a number, and `_times()` accepts only
   numbers, so every run has `finished_at` and `elapsed_ms` NULL (observed:
   43 of 43) and `?days=` matches nothing.  A run records start, end and
   duration from the ISO strings or `duration`.  A build with no usable
   times is still counted, and is left out of time and duration charts; old
   rows keep their NULLs and are counted in totals.
3. The aggregate endpoints answer with counts, not rows, so a page does not
   download the log: `GET /api/stream/metrics/runs`, `/escalations`,
   `/deliveries` and `/messages`.  Each takes the same query shape:
   `group` (what to count by, from a fixed list per endpoint), `bucket`
   (`hour` or `day`, for a time series) and filters:
   - runs: `project`, `job`, `result`, `pipeline`, `mr` (key or number),
     `since`, `until`;
   - escalations: `project`, `mr`, `job` (a failing job), `since`, `until`;
   - deliveries: `project`, `kind`, `action`, `ok`, `since`, `until`;
   - messages: `project`, `action`, `sender`, `outcome`, `since`, `until`.
   An unknown `group`, `bucket` or filter value is a 400 that lists the valid
   ones; a bad time is a 400.  Nothing in a filter reaches SQL except as a
   bound parameter.
4. The view (`/metrics` on the demo UI) shows, all following the filters:
   - jobs: pass, fail and other per job (stacked bars), fail rate per job,
     runs over time (lines, by result), duration per job (average and
     maximum), results by pipeline;
   - bundles: attempts per bundle, escalations over time, and the failing
     jobs of each escalation;
   - webhooks: deliveries over time, ok versus failed, attempts, by kind and
     action;
   - messages: messages over time, by action, by sender and by outcome
     (applied, duplicate, refused, rejected, error);
   - totals at the top: runs, pass rate, escalations, deliveries failed,
     messages rejected.
5. Filters: project, job, result, pipeline, bundle (key), time range (quick
   ranges and custom), and, for webhooks and messages, their own fields
   (scenario 3).  They live in the page's URL, so a view can be bookmarked and
   shared, and a refresh keeps them.  The page refreshes itself like the
   other demo panels without losing the filters or the scroll position.
6. With metrics turned off, the page says so and names the setting
   (`[metrics] path`); the message charts still work.  With no data in range,
   each chart shows an empty state, not a blank canvas.  If the charting
   library cannot be loaded (no internet), the page shows the numbers as
   tables and says why.
7. A refused or failing call (401 on messages, 404 with metrics off, a 400 for
   a bad filter, git-stream down) shows a message on the affected section and
   leaves the others working.
8. The existing `GET /api/stream/metrics` and the server-rendered `/metrics`
   page behave as before, except that `days` now matches runs that have times
   (scenario 2).

## Fires on

Opening the page, changing a filter, and the page's own refresh.  Nothing in
git-stream's behavior changes except what it records (scenarios 1 and 2).

## Out of scope

Retention or trimming of old rows, exporting data (CSV, images), alerting,
a read token for the aggregate endpoints, and drill-down pages per build.
The first, second and fourth go to `docs/backlog.md` (retention is already
there).  Backfilling times for rows recorded before this change is not done.

## Done means

- A test per scenario above: `tests/test_metrics.py` (recording once, the
  times from ISO strings and `duration`, de-duplication on open), a new
  `tests/test_metrics_api.py` (each endpoint, each filter, each group, bad
  input, metrics off, paging bounds), and `tests/test_demo_scripts.py` for the
  demo UI's proxy and page.  The page's own JavaScript is checked by loading
  it in the test against recorded JSON for the structure of the filters and
  the URL state; the charts themselves are looked at in the demo.
- The Zuul contract tests unchanged and passing: Zuul sees nothing new.
- `docs/security.md` (the new endpoints, who may read them, what they never
  return), `docs/demo.md`, the README, `docs/architecture.md`,
  `git_stream/config.py` if a setting is added, and `git_stream/server.py`'s
  docstring updated.
- The full gate green, and the page opened in the running demo against real
  activity.

## Constraints

- No Zuul changes; `git_stream/` stays standard library only; the demo UI
  stays stdlib Python plus HTML and JavaScript.
- Charts use Chart.js loaded from a CDN, pinned to an exact version with a
  Subresource Integrity hash, and nothing else is loaded from the network.
  The demo then needs internet access for the charts; scenario 6 is what the
  page does without it.
- The aggregate endpoints for runs, escalations and deliveries are anonymous,
  like the existing `/api/stream/metrics`.  `/messages` needs a stream token,
  like `GET /api/stream/messages`; the demo UI sends one from
  `STREAM_TOKEN` when it is set (the demo stream is open, so it is not).
- They never return a webhook URL (it may hold a secret) or a message body.
- Every query is bounded: a page cap, a cap on groups, a cap on buckets
  (a time range that would make more is a 400).
- Malformed input is a 4xx, never a 500; validate before any query.
- Behavior with metrics off does not change.
