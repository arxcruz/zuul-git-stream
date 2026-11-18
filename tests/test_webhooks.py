"""Webhook delivery to Zuul: headers, order, and what a failing receiver
does to everyone else.

Receivers are real local HTTP servers; the sender runs for real.
"""

import http.server
import json
import logging
import threading

import pytest

from conftest import FAST_POLL, wait_for
from git_stream.metrics import Metrics
from git_stream.webhooks import WebhookSender


class Receiver:
    """Records what it is sent; respond(n, payload) -> status decides the
    reply, where n counts the requests this receiver has seen."""

    def __init__(self, respond=lambda n, payload: 200):
        self.received = []   # [(headers, payload)] in arrival order
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                payload = json.loads(body)
                outer.received.append((dict(self.headers), payload))
                self.send_response(respond(len(outer.received), payload))
                self.send_header('Content-Length', '0')
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
                                                      Handler)
        self.url = 'http://127.0.0.1:%d/hook' % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever,
                         kwargs=FAST_POLL, daemon=True).start()

    @property
    def ns(self):
        """The 'n' of each payload received, in order."""
        return [payload['n'] for _, payload in self.received]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def event(n):
    return {'object_kind': 'push', 'n': n, 'ref': 'refs/heads/master',
            'after': '%08d' % n + '0' * 32,  # summaries show after[:8]
            'project': {'path_with_namespace': 'bundles'}}


@pytest.fixture
def receivers():
    made = []

    def make(respond=lambda n, payload: 200):
        made.append(Receiver(respond))
        return made[-1]
    yield make
    for receiver in made:
        receiver.stop()


@pytest.fixture
def sender():
    made = []

    def make(urls, token='tok', **kw):
        s = WebhookSender(urls, token, max_backoff=1, **kw)
        s.start()
        made.append(s)
        return s
    yield make
    for s in made:
        s.stop()


# -- what is sent --------------------------------------------------------------

def test_headers_name_the_event_and_carry_the_token(receivers, sender):
    r = receivers()
    sender([r.url]).emit(event(1))
    assert wait_for(lambda: r.ns == [1])
    headers = {k.lower(): v for k, v in r.received[0][0].items()}
    assert headers['x-gitlab-event'] == 'Push Hook'
    assert headers['x-gitlab-token'] == 'tok'
    assert headers['content-type'] == 'application/json'


def test_no_token_means_no_token_header(receivers, sender):
    r = receivers()
    sender([r.url], token='').emit(event(1))
    assert wait_for(lambda: r.ns == [1])
    assert 'x-gitlab-token' not in {k.lower() for k in r.received[0][0]}


# -- order and retries ---------------------------------------------------------

def test_events_arrive_in_the_order_they_happened(receivers, sender):
    r = receivers()
    s = sender([r.url])
    for n in range(1, 8):
        s.emit(event(n))
    assert wait_for(lambda: len(r.ns) == 7)
    assert r.ns == list(range(1, 8))


@pytest.mark.slow
def test_a_server_error_is_retried_and_later_events_wait(receivers, sender):
    r = receivers(lambda n, payload: 503 if n == 1 else 200)
    s = sender([r.url])
    s.emit(event(1))
    s.emit(event(2))
    assert wait_for(lambda: len(r.ns) == 3)
    assert r.ns == [1, 1, 2]  # event 1 again before event 2
    [first] = [e for e in s.recent if e['summary'].endswith('00000001')]
    assert first['status'] == 200 and first['attempts'] == 2


# -- a 4xx is kept as failed, not retried, and does not stop the flow ----------

def test_a_4xx_is_kept_as_failed_and_not_retried(
        receivers, sender, tmp_path, caplog):
    r = receivers(lambda n, payload: 401 if payload['n'] == 1 else 200)
    metrics = Metrics(str(tmp_path / 'metrics.db'))
    s = sender([r.url], metrics=metrics)
    with caplog.at_level(logging.ERROR, logger='git_stream.webhooks'):
        s.emit(event(1))
        s.emit(event(2))
        assert wait_for(lambda: len(metrics.deliveries()) == 2)
    assert r.ns == [1, 2]  # event 1 was tried once, event 2 still flowed
    failed = [e for e in s.recent if e['status'] == 401]
    assert len(failed) == 1 and failed[0]['attempts'] == 1
    assert failed[0]['url'] == r.url
    assert any('401' in m and r.url in m for m in caplog.messages)
    by_status = {int(d['status']): d['ok'] for d in metrics.deliveries()}
    assert by_status == {401: 0, 200: 1}
    metrics.close()


# -- one URL per queue ---------------------------------------------------------

@pytest.mark.slow
def test_a_dead_url_does_not_delay_a_healthy_one(receivers, sender):
    dead = receivers(lambda n, payload: 503)
    healthy = receivers()
    s = sender([dead.url, healthy.url])
    for n in range(1, 4):
        s.emit(event(n))
    assert wait_for(lambda: healthy.ns == [1, 2, 3], timeout=3)
    assert dead.ns and set(dead.ns) == {1}  # still retrying event 1, only


@pytest.mark.slow
def test_each_url_keeps_its_own_order(receivers, sender):
    flaky = receivers(lambda n, payload: 503 if n == 1 else 200)
    steady = receivers()
    s = sender([flaky.url, steady.url])
    for n in range(1, 4):
        s.emit(event(n))
    assert wait_for(lambda: steady.ns == [1, 2, 3])
    assert wait_for(lambda: flaky.ns == [1, 1, 2, 3])
    assert [e['url'] for e in s.recent].count(flaky.url) == 3
    assert [e['url'] for e in s.recent].count(steady.url) == 3


@pytest.mark.slow
def test_a_later_event_reaches_a_recovered_url(receivers, sender):
    state = {'up': False}
    r = receivers(lambda n, payload: 200 if state['up'] else 503)
    s = sender([r.url])
    s.emit(event(1))
    assert wait_for(lambda: len(r.ns) >= 2)  # retrying
    state['up'] = True
    s.emit(event(2))
    assert wait_for(lambda: r.ns[-2:] == [1, 2])


@pytest.mark.slow
def test_stopping_ends_every_delivery_thread_even_mid_retry(receivers, sender):
    def webhook_threads():
        return {t for t in threading.enumerate()
                if t.name.startswith('webhooks') and t.is_alive()}
    before = webhook_threads()  # other tests' leftovers are not ours
    dead = receivers(lambda n, payload: 503)
    other = receivers(lambda n, payload: 503)
    s = sender([dead.url, other.url])
    s.emit(event(1))
    assert wait_for(lambda: dead.ns and other.ns)  # both retrying
    s.stop()
    assert wait_for(lambda: not (webhook_threads() - before), timeout=5)
