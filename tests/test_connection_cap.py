"""The cap on concurrent connections (`[server] max_connections`): once that
many connections are open, a new one is refused at once with a 503 and
`Retry-After`, no handler thread is started for it, and a slot is released
whenever a connection's handler ends, however it ends.

A held connection does one full request and waits for the reply, so the test
knows the server has accepted it and counted its slot; nothing here sleeps to
wait for that.  Where a slot is released after the client sees the end (a
close, a 408), `wait_for` polls until a fresh connection is served.
"""

import http.server
import json
import socket
import socketserver
import struct
import threading
from unittest import mock

import pytest

from conftest import API_TOKEN, FAST_POLL, wait_for
from git_stream import stream
from git_stream.webhooks import WebhookSender

STALLED_POST = (b'POST /api/stream HTTP/1.1\r\nHost: x\r\n'
                b'Authorization: Bearer ' + API_TOKEN.encode() + b'\r\n'
                b'Content-Type: application/json\r\n'
                b'Content-Length: 100\r\n\r\n')


class Conn:
    """A raw keep-alive connection."""

    def __init__(self, forge, timeout=5):
        port = int(forge.url.rsplit(':', 1)[1])
        self.sock = socket.create_connection(('127.0.0.1', port),
                                             timeout=timeout)

    def send(self, data):
        self.sock.sendall(data)

    def get(self, path=b'/health'):
        self.send(b'GET ' + path + b' HTTP/1.1\r\nHost: x\r\n\r\n')
        return self.read()

    def read(self):
        """(status, {lower-case header: value}, body) of one response."""
        data = b''
        while b'\r\n\r\n' not in data:
            more = self.sock.recv(4096)
            if not more:
                raise ConnectionError('closed without a response')
            data += more
        head, _, body = data.partition(b'\r\n\r\n')
        lines = head.decode('latin-1').split('\r\n')
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(':')
            headers[name.lower()] = value.strip()
        if 'content-length' in headers:
            while len(body) < int(headers['content-length']):
                body += self.sock.recv(4096)
        else:
            while True:
                more = self.sock.recv(4096)
                if not more:
                    break
                body += more
        return int(lines[0].split(' ', 2)[1]), headers, body

    def closed(self):
        """True if the server closes the connection rather than leaving it
        open for the socket's patience."""
        try:
            return self.sock.recv(4096) == b''
        except TimeoutError:
            return False

    def reset(self):
        """Close so that the server sees a reset, not an orderly close."""
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                             struct.pack('ii', 1, 0))
        self.sock.close()

    def close(self):
        self.sock.close()


@pytest.fixture
def capped(make_forge, monkeypatch):
    """capped(n, **settings): a server serving at most n connections, whose
    handler threads are counted in `.started` and whose errors are silent."""
    def make(cap, **settings):
        forge = make_forge(max_connections=cap, **settings)
        server = forge.server
        forge.started = []
        real = server.process_request_thread

        def counted(request, client_address):
            forge.started.append(client_address)
            return real(request, client_address)
        monkeypatch.setattr(server, 'process_request_thread', counted)
        monkeypatch.setattr(server, 'handle_error', lambda *args: None)
        return forge
    return make


@pytest.fixture
def conns():
    """Connections opened by a test, closed when it ends."""
    opened = []
    yield opened
    for c in opened:
        c.close()


def hold(forge, conns, n=1):
    """n connections that have each been served one request and stay open."""
    held = []
    for _ in range(n):
        c = Conn(forge)
        conns.append(c)
        assert c.get()[0] == 200
        held.append(c)
    return held


def refusal(forge, conns):
    """What a connection gets when it connects and says nothing:
    (status, headers, body), or (None, {}, b'') if the server stays silent."""
    c = Conn(forge, timeout=2)
    conns.append(c)
    try:
        return c.read()
    except TimeoutError:
        return None, {}, b''


def served(forge):
    """True if a fresh connection is served now; its slot is freed again."""
    try:
        c = Conn(forge)
    except OSError:
        return False
    try:
        return c.get()[0] == 200
    except OSError:
        return False
    finally:
        c.close()


def refused(forge, conns):
    return refusal(forge, conns)[0] == 503


# -- under and at the cap ----------------------------------------------------

def test_under_the_cap_requests_behave_as_today(capped, conns):
    forge = capped(2)
    (first,) = hold(forge, conns)
    second = Conn(forge)
    conns.append(second)
    status, headers, _ = second.get()
    assert status == 200
    assert 'retry-after' not in headers
    assert first.get()[0] == 200


@pytest.mark.parametrize('cap', [1, 2])
def test_at_the_cap_the_next_connection_gets_a_503(capped, conns, cap):
    forge = capped(cap)
    hold(forge, conns, cap)
    status, headers, body = refusal(forge, conns)
    assert status == 503
    assert headers['retry-after'] == '1'
    assert headers['connection'] == 'close'
    assert headers['content-type'].startswith('text/plain')
    assert b'busy' in body


def test_a_refused_connection_is_closed(capped, conns):
    forge = capped(1)
    hold(forge, conns)
    c = Conn(forge, timeout=2)
    conns.append(c)
    try:
        c.read()
    except TimeoutError:
        pytest.fail('the server stayed silent instead of refusing')
    assert c.closed()


def test_no_handler_thread_starts_for_a_refused_connection(capped, conns):
    forge = capped(1)
    hold(forge, conns)
    assert len(forge.started) == 1
    assert refused(forge, conns)
    assert refused(forge, conns)
    assert len(forge.started) == 1


def test_a_keep_alive_connection_holds_one_slot_across_requests(
        capped, conns):
    forge = capped(1)
    (c,) = hold(forge, conns)
    assert refused(forge, conns)
    assert c.get()[0] == 200       # a second request on the same connection
    assert refused(forge, conns)   # it still holds exactly its one slot
    assert c.get()[0] == 200


def test_a_full_cap_does_not_close_open_connections(capped, conns):
    forge = capped(2)
    first, second = hold(forge, conns, 2)
    for _ in range(3):
        assert refused(forge, conns)
    assert first.get()[0] == 200
    assert second.get()[0] == 200


# -- slots are freed ---------------------------------------------------------

def test_a_slot_is_freed_when_a_client_closes(capped, conns):
    forge = capped(1)
    (c,) = hold(forge, conns)
    assert refused(forge, conns)
    c.close()
    assert wait_for(lambda: served(forge))


def test_a_slot_is_freed_when_a_client_resets(capped, conns):
    forge = capped(1)
    (c,) = hold(forge, conns)
    c.reset()
    assert wait_for(lambda: served(forge))


def test_a_slot_is_freed_once_not_twice(capped, conns):
    forge = capped(2)
    first, _second = hold(forge, conns, 2)
    first.close()
    fresh = []

    def take():
        c = Conn(forge)
        conns.append(c)
        try:
            taken = c.get()[0] == 200
        except OSError:
            taken = False
        if taken:
            fresh.append(c)
        return taken
    assert wait_for(take)          # the one freed slot is reusable ...
    assert refused(forge, conns)   # ... and there is no second one


@pytest.mark.slow
def test_a_slot_is_freed_after_a_408(capped, conns):
    forge = capped(1, read_timeout=1)
    c = Conn(forge)
    conns.append(c)
    c.send(STALLED_POST)           # the body never comes
    assert c.read()[0] == 408
    assert wait_for(lambda: served(forge))


def test_a_slot_is_freed_when_the_handler_fails(capped, conns, monkeypatch):
    forge = capped(1)
    server = forge.server
    real = server.finish_request
    failed = []

    def finish(request, client_address):
        if not failed:
            failed.append(client_address)
            raise RuntimeError('the handler failed')
        return real(request, client_address)
    monkeypatch.setattr(server, 'finish_request', finish)
    c = Conn(forge)
    conns.append(c)
    assert c.closed()              # the failed handler's connection is closed
    assert failed
    assert wait_for(lambda: served(forge))


# -- a thread that cannot be started ----------------------------------------

def test_a_thread_that_cannot_start_frees_its_slot(
        capped, conns, monkeypatch):
    forge = capped(1)
    server = forge.server
    real = socketserver.ThreadingMixIn.process_request
    failed = []

    def process_request(self, request, client_address):
        if failed:
            return real(self, request, client_address)
        failed.append(client_address)
        with mock.patch.object(threading.Thread, 'start', side_effect=
                               RuntimeError("can't start new thread")):
            return real(self, request, client_address)
    monkeypatch.setattr(socketserver.ThreadingMixIn, 'process_request',
                        process_request)

    c = Conn(forge)
    conns.append(c)
    assert c.closed()              # its socket is closed, not leaked
    assert failed
    # With a cap of 1 the failed connection must not still hold the slot ...
    (held,) = hold(forge, conns)
    # ... and must not have released one it never took.
    assert refused(forge, conns)
    assert held.get()[0] == 200

    # The stdlib keeps the unstarted thread to join on close; that must not
    # raise.
    server.shutdown()
    server.server_close()


# -- outgoing webhooks take no slot --------------------------------------------

def test_an_outgoing_webhook_is_delivered_with_the_cap_full(capped, conns):
    forge = capped(1)
    received = []

    class Hook(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers['Content-Length'])
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def log_message(self, *args):
            pass
    receiver = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Hook)
    threading.Thread(target=receiver.serve_forever, kwargs=FAST_POLL,
                     daemon=True).start()
    sender = WebhookSender(
        ['http://127.0.0.1:%d/hook' % receiver.server_address[1]], 'tok',
        max_backoff=1)
    sender.start()
    forge.forge.emit = sender.emit
    try:
        # In-process, so that setting up takes no slot either.
        stream.apply(forge.forge, {'action': 'create_project',
                                   'project': 'bundles'},
                     'git-stream')
        (held,) = hold(forge, conns)
        assert refused(forge, conns)           # the cap really is full

        stream.apply(forge.forge, {
            'action': 'open_mr', 'project': 'bundles', 'title': 'T',
            'key': 'k', 'data': {'a.yaml': 'a: 1\n'}}, 'git-stream')
        assert wait_for(lambda: received), 'the webhook was never delivered'
        assert received[0]['object_kind'] == 'merge_request'

        assert refused(forge, conns)           # and the hook used no slot
        assert held.get()[0] == 200
    finally:
        sender.stop()
        receiver.shutdown()
        receiver.server_close()


# -- a refusal that cannot be written ----------------------------------------

class FakeSocket:
    """A client socket whose refusal cannot be written."""

    def __init__(self, error):
        self.error = error
        self.timeouts = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def sendall(self, data):
        raise self.error

    send = sendall

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


@pytest.mark.parametrize('error', [TimeoutError, BrokenPipeError,
                                   ConnectionResetError])
def test_a_refusal_that_cannot_be_written_does_not_block_or_leak(
        capped, conns, error):
    forge = capped(1)
    server = forge.server
    (held,) = hold(forge, conns)
    fake = FakeSocket(error('the client will not read'))

    server.process_request(fake, ('127.0.0.1', 1))  # returns; no exception

    assert fake.timeouts, 'the refusal is written with a send timeout'
    assert 0 < fake.timeouts[0] <= 5
    assert fake.closed
    assert len(forge.started) == 1                  # no thread for it
    # The slot count is unchanged: the held connection still has its slot
    # (a new connection is refused), and its release frees exactly one.
    assert refused(forge, conns)
    held.close()
    assert wait_for(lambda: served(forge))
