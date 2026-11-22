import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from git_stream.forge import Forge
from git_stream.server import serve

API_TOKEN = 'secret-api-token'

# serve_forever() notices shutdown() only between polls, 0.5 seconds apart by
# default, which made every test pay up to that much to stop its server.
FAST_POLL = {'poll_interval': 0.01}


def wait_for(condition, timeout=8):
    """Poll until condition() is true; False if it never is."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False

# Zuul's HTTP adapter sets TCP_KEEPIDLE, which macOS calls TCP_KEEPALIVE.
if not hasattr(socket, 'TCP_KEEPIDLE') and hasattr(socket, 'TCP_KEEPALIVE'):
    socket.TCP_KEEPIDLE = socket.TCP_KEEPALIVE


class Running:
    """A forge served on a free local port, recording its webhooks."""

    def __init__(self, data_dir, stream_tokens=None, report_users=None,
                 users=None, insecure=True, **serve_options):
        self.hooks = []
        extra = {}
        if report_users is not None:
            extra['report_users'] = report_users
        extra['insecure'] = insecure
        # users=None means the usual one token; pass {} for "none configured".
        self.forge = Forge(str(data_dir), 'http://placeholder',
                           emit=self.hooks.append,
                           users={API_TOKEN: 'zuul'} if users is None
                           else users,
                           stream_tokens=stream_tokens, **extra)
        self.server = serve(self.forge, None, '127.0.0.1', 0,
                            **serve_options)
        self.url = 'http://127.0.0.1:%d' % self.server.server_address[1]
        self.forge.baseurl = self.url
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs=FAST_POLL, daemon=True)
        self.thread.start()

    def send(self, msg, token=None):
        headers = {'Content-Type': 'application/json'}
        if token is not None:
            headers['X-Stream-Token'] = token
        req = urllib.request.Request(
            self.url + '/api/stream', data=json.dumps(msg).encode(),
            headers=headers, method='POST')
        with urllib.request.urlopen(req) as r:
            return json.load(r)

    def hooks_of(self, kind):
        return [h for h in self.hooks if h['object_kind'] == kind]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.forge.close()


def _pending():
    """{test or exact case: group} from tests/pending.txt."""
    pending, group = {}, None
    with open(os.path.join(os.path.dirname(__file__), 'pending.txt')) as f:
        for line in f:
            line = line.strip()
            if line.startswith('# group:'):
                group = line.split(':', 1)[1].strip()
            elif line and not line.startswith('#'):
                pending[line] = group
    return pending


def pytest_collection_modifyitems(items):
    """Tests that specify behavior not built yet are strict xfails.

    The full check stays green while they are pending; once one passes it
    fails ("XPASS(strict)") until its line is removed from pending.txt.
    """
    pending = _pending()
    for item in items:
        group = (pending.get(item.nodeid) or
                 pending.get(item.nodeid.split('[', 1)[0]))
        if group:
            item.add_marker(pytest.mark.xfail(strict=True, reason=(
                'specified, not implemented (%s): see docs/backlog.md; '
                'delete its line in tests/pending.txt once it passes'
                % group)))


def rejected(forge, msg, token=None):
    """Send msg, expect an HTTP error, return (status, message)."""
    with pytest.raises(urllib.error.HTTPError) as e:
        forge.send(msg, token=token)
    return e.value.code, json.load(e.value)['message']


@pytest.fixture
def forge(tmp_path):
    running = Running(tmp_path / 'data')
    running.send({'action': 'create_project', 'project': 'bundles'})
    yield running
    running.stop()


@pytest.fixture
def make_forge(tmp_path):
    """Start a forge with its own settings: make_forge(stream_tokens=...)."""
    made = []

    def make(**settings):
        running = Running(tmp_path / ('data%d' % len(made)), **settings)
        made.append(running)
        return running
    yield make
    for running in made:
        running.stop()
