# Spec: per-MR callback_url

## Purpose

The system that announces a bundle learns the final outcome of its MR without
polling.

## Scenarios

1. `open_mr` with a `callback_url`, and the MR passes (Zuul approves it):
   git-stream POSTs the outcome "passed" to that URL.
2. `open_mr` with a `callback_url`, and the retries run out: git-stream POSTs
   the outcome "escalated" to that URL.
3. `open_mr` without a `callback_url` behaves exactly as today.
4. A `callback_url` that is not http or https (or is not a string) gets a 400
   before anything is created.
5. A dead `callback_url`: one attempt, the failure is recorded on the MR as
   "failed: <cause>", and nothing else is delayed.

## Fires on

Final outcomes only (passed, escalated), not on each failed attempt or rerun.

## Out of scope

An auth header on callbacks, retries, durable queues, an allowlist of URLs,
and per-failure events.  The last two go to `docs/backlog.md`.

## Done means

- Tests for each scenario above.
- The Zuul contract tests unchanged and passing (Zuul sees no new field).
- `docs/security.md`, the README events table and the `stream.py` docstring
  updated.
- The full gate green.

## Constraints

- Stdlib only.
- Validation happens before any side effect.
- Delivery runs in the background so it can never delay Zuul or other MRs.
- Behavior without `callback_url` must not change.
