"""Request size and patience limits: a body over `max_body` is refused (413)
without being read, and a client that stops sending for `read_timeout`
seconds is answered 408 and dropped, so neither can pin a handler thread or
fill memory.  The tests run with a 1000-byte limit and a 1 second timeout.
"""

import json
import socket
import urllib.request

import pytest

from conftest import API_TOKEN

LIMIT = 1000
NOTES = b'/api/v4/projects/bundles/merge_requests/1/notes'
MSG = {'action': 'comment', 'project': 'bundles', 'mr': 'k', 'note': 'hi'}


class Conn:
    """A raw connection, for requests urllib wouldn't send."""

    def __init__(self, forge):
        port = int(forge.url.rsplit(':', 1)[1])
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=5)

    def send(self, data):
        self.sock.sendall(data)

    def response(self):
        """(status, body); (None, b'') if the server closed without one."""
        data = b''
        while b'\r\n\r\n' not in data:
            more = self.sock.recv(4096)
            if not more:
                return None, b''
            data += more
        head, _, body = data.partition(b'\r\n\r\n')
        status = int(head.split(b' ', 2)[1])
        for line in head.split(b'\r\n')[1:]:
            name, _, value = line.partition(b':')
            if name.lower() == b'content-length':
                while len(body) < int(value):
                    body += self.sock.recv(4096)
        return status, body

    def closed(self):
        """True if the server closes the connection (within the socket's
        5 second patience) rather than leaving it open."""
        try:
            return self.sock.recv(4096) == b''
        except socket.timeout:
            return False

    def close(self):
        self.sock.close()


def head(path=b'/api/stream', length=None, chunked=False):
    framing = (b'Transfer-Encoding: chunked\r\n' if chunked else
               b'Content-Length: %d\r\n' % length)
    return (b'POST ' + path + b' HTTP/1.1\r\nHost: x\r\n'
            b'Authorization: Bearer ' + API_TOKEN.encode() + b'\r\n'
            b'Content-Type: application/json\r\n' + framing + b'\r\n')


def padded(size):
    body = json.dumps(MSG).encode()
    return body + b' ' * (size - len(body))


@pytest.fixture
def limited(make_forge):
    forge = make_forge(max_body=LIMIT, read_timeout=1)
    forge.send({'action': 'create_project', 'project': 'bundles'})
    forge.send({'action': 'open_mr', 'project': 'bundles', 'title': 'T',
                'key': 'k', 'data': {'a.yaml': 'a: 1\n'}})
    return forge


@pytest.fixture
def conn(limited):
    c = Conn(limited)
    yield c
    c.close()


def notes(forge):
    url = forge.url + NOTES.decode()
    with urllib.request.urlopen(url) as r:
        return json.load(r)


def health(forge):
    with urllib.request.urlopen(forge.url + '/health', timeout=5) as r:
        return r.status


# -- size --------------------------------------------------------------------

def test_a_body_exactly_at_the_limit_is_accepted(limited, conn):
    conn.send(head(length=LIMIT) + padded(LIMIT))
    assert conn.response()[0] == 200
    assert len(notes(limited)) == 1


def test_a_body_over_the_limit_is_refused(limited, conn):
    conn.send(head(length=LIMIT + 1) + padded(LIMIT + 1))
    status, body = conn.response()
    assert status == 413
    message = json.loads(body)['message']
    assert 'too large' in message and str(LIMIT) in message
    assert notes(limited) == []  # nothing was applied


def test_a_declared_length_is_refused_before_the_body_is_read(limited, conn):
    conn.send(head(length=10 ** 9))  # and never send it
    assert conn.response()[0] == 413  # not the 408 a stall would earn
    assert health(limited) == 200


@pytest.mark.parametrize('path', [
    b'/api/stream', NOTES, b'/bundles.git/git-upload-pack'])
def test_every_endpoint_that_reads_a_body_has_the_limit(limited, conn, path):
    conn.send(head(path, length=LIMIT + 1))
    assert conn.response()[0] == 413


def test_chunked_bodies_count_towards_the_limit(limited, conn):
    chunk = b'x' * LIMIT
    conn.send(head(chunked=True) + b'%x\r\n' % len(chunk) + chunk + b'\r\n'
              + b'1\r\nx\r\n0\r\n\r\n')
    assert conn.response()[0] == 413
    assert notes(limited) == []


def test_a_huge_chunk_size_is_refused_before_it_is_read(limited, conn):
    conn.send(head(chunked=True) + b'ffffffff\r\n')
    assert conn.response()[0] == 413


# -- patience ----------------------------------------------------------------

@pytest.mark.slow
def test_a_stalled_body_gets_408(limited, conn):
    conn.send(head(length=100) + b'x' * 10)  # 90 bytes never arrive
    status, body = conn.response()
    assert status == 408
    assert 'timed out' in json.loads(body)['message']
    assert health(limited) == 200


@pytest.mark.slow
def test_a_stalled_chunk_gets_408(limited, conn):
    conn.send(head(chunked=True) + b'10\r\nabc')  # 13 bytes never arrive
    assert conn.response()[0] == 408


@pytest.mark.slow
def test_a_stalled_request_line_is_dropped(limited, conn):
    conn.send(b'POST /api/str')
    status, _ = conn.response()
    assert status in (None, 408)


@pytest.mark.slow
def test_an_idle_keep_alive_connection_is_closed(limited, conn):
    conn.send(b'GET /health HTTP/1.1\r\nHost: x\r\n\r\n')
    assert conn.response()[0] == 200
    assert conn.closed()  # after read_timeout, not left open forever


def test_a_slow_but_live_sender_is_not_cut_off(limited, conn):
    import time
    body = padded(200)
    conn.send(head(length=len(body)) + body[:100])
    time.sleep(0.4)  # well under the 1 second timeout
    conn.send(body[100:])
    assert conn.response()[0] == 200


def test_one_stalled_client_does_not_block_others(limited, conn):
    conn.send(head(length=100) + b'x')
    assert health(limited) == 200


# -- what the limits leave behind ----------------------------------------------

def test_a_refusal_closes_the_connection(limited, conn):
    """The rest of the refused body is still in the stream; keep-alive
    would read it as the next request."""
    conn.send(head(length=LIMIT + 1))
    assert conn.response()[0] == 413
    assert conn.closed()


@pytest.mark.parametrize('length', [b'abc', b'-1', b'1_0', b' 5 5'])
def test_an_invalid_content_length_is_a_client_error(limited, conn, length):
    conn.send(b'POST /api/stream HTTP/1.1\r\nHost: x\r\n'
              b'Content-Type: application/json\r\n'
              b'Content-Length: ' + length + b'\r\n\r\n')
    status, body = conn.response()
    assert status == 400
    assert 'invalid Content-Length' in json.loads(body)['message']


@pytest.mark.slow
def test_a_trickling_body_hits_the_total_deadline(make_forge):
    """One byte now and then never trips read_timeout; body_timeout does."""
    import time
    forge = make_forge(max_body=LIMIT, read_timeout=5, body_timeout=1)
    c = Conn(forge)
    try:
        c.send(head(length=100))
        start = time.monotonic()
        status = None
        while status is None and time.monotonic() - start < 4:
            c.send(b'x')
            c.sock.settimeout(0.3)
            try:
                status, body = c.response()
            except socket.timeout:
                continue
            break
        assert status == 408
        assert 'timed out' in json.loads(body)['message']
        assert time.monotonic() - start < 3
    finally:
        c.close()


def test_a_stalled_body_does_not_hold_the_forge_lock(make_forge):
    """Writes take the forge's lock; reading a body must happen before."""
    import time
    forge = make_forge(max_body=LIMIT, read_timeout=30)
    forge.send({'action': 'create_project', 'project': 'bundles'})
    stalled = Conn(forge)
    try:
        stalled.send(head(length=100) + b'x')
        start = time.monotonic()
        forge.send({'action': 'open_mr', 'project': 'bundles',
                    'title': 'T', 'key': 'k', 'data': {'a.yaml': 'a: 1\n'}})
        assert time.monotonic() - start < 5
    finally:
        stalled.close()


# -- clients that vanish ---------------------------------------------------------

def test_a_client_that_resets_mid_body_is_not_an_error(
        limited, caplog, capfd):
    import struct
    import time
    with caplog.at_level('DEBUG', logger='git_stream'):
        c = Conn(limited)
        c.send(head(length=100) + b'abc')
        c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                          struct.pack('ii', 1, 0))  # close sends a reset
        c.close()
        time.sleep(0.5)
    assert health(limited) == 200
    assert [r for r in caplog.records if r.levelname == 'ERROR'] == []
    assert 'Traceback' not in capfd.readouterr().err


def test_the_listen_backlog_holds_a_burst(limited):
    """With the standard backlog of 5, a separate process connecting 60
    clients at once got resets (checked by hand against a real server; an
    in-process client and the accept loop share a GIL and never overflow
    it, so this checks the setting itself)."""
    assert limited.server.request_queue_size >= 64


def test_a_body_that_ends_early_is_a_client_error(limited, conn):
    conn.send(head(length=100) + b'abc')
    conn.sock.shutdown(socket.SHUT_WR)  # no more is coming
    status, body = conn.response()
    assert status == 400
    assert 'incomplete' in json.loads(body)['message']
