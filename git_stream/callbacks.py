"""Tell the system that announced a merge request how it ended.

An MR opened with a "callback_url" gets one POST per final outcome and per
head sha: "passed" (Zuul approved it) or "escalated" (the retries ran out).
Each delivery is one attempt, made in its own thread so a slow or dead
receiver can never hold up Zuul's API calls, the webhooks, or the callbacks
of other merge requests.  How it went is recorded on the MR as
"delivered" or "failed: <cause>" (see Forge.notify_outcome).
"""

import json
import logging
import threading
import urllib.request

log = logging.getLogger('git_stream.callbacks')

TIMEOUT = 10


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is a failure: the URL the sender chose is the only one."""

    def redirect_request(self, *args, **kw):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def deliver(url, body):
    """POST body as JSON to url, once.  'delivered' or 'failed: <cause>'."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode('utf8'), method='POST',
        headers={'Content-Type': 'application/json'})
    try:
        with _opener.open(req, timeout=TIMEOUT):
            return 'delivered'
    except Exception as e:
        # The URL may carry a secret, so it is not repeated in the cause.
        cause = str(e).replace(url, '<callback_url>')
        return 'failed: %s' % (cause[:200] or type(e).__name__)


def send(url, body, done):
    """Deliver in the background, then call done(status)."""
    def run():
        try:
            done(deliver(url, body))
        except Exception:
            log.exception('Callback bookkeeping failed')
    threading.Thread(target=run, daemon=True, name='callback').start()
