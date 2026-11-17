import json
import socket
import threading
import urllib.request

import pytest

from git_stream.forge import Forge
from git_stream.server import serve

API_TOKEN = 'secret-api-token'

# Zuul's HTTP adapter sets TCP_KEEPIDLE, which macOS calls TCP_KEEPALIVE.
if not hasattr(socket, 'TCP_KEEPIDLE') and hasattr(socket, 'TCP_KEEPALIVE'):
    socket.TCP_KEEPIDLE = socket.TCP_KEEPALIVE


class Running:
    """A forge served on a free local port, recording its webhooks."""

    def __init__(self, data_dir):
        self.hooks = []
        self.forge = Forge(str(data_dir), 'http://placeholder',
                           emit=self.hooks.append,
                           users={API_TOKEN: 'zuul'})
        self.server = serve(self.forge, None, '127.0.0.1', 0)
        self.url = 'http://127.0.0.1:%d' % self.server.server_address[1]
        self.forge.baseurl = self.url
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def send(self, msg):
        req = urllib.request.Request(
            self.url + '/api/stream', data=json.dumps(msg).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req) as r:
            return json.load(r)

    def hooks_of(self, kind):
        return [h for h in self.hooks if h['object_kind'] == kind]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def forge(tmp_path):
    running = Running(tmp_path / 'data')
    running.send({'action': 'create_project', 'project': 'bundles',
                  'files': {'README.md': 'bundles\n'}})
    yield running
    running.stop()
