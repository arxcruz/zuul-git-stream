"""A per-MR callback_url: the announcer hears the final outcome of its MR.

The spec is docs/specs/callback-url.md.  `open_mr` may carry a
"callback_url"; when the MR passes (Zuul approves it) or is escalated,
git-stream POSTs the outcome there, once per outcome per head sha.  The
record of each delivery is the MR's "callback" field in mr_json: None
without a callback_url, else a list of
{"outcome", "sha", "time", "status"} in the order they fired, status being
"delivered" or "failed: <cause>".  The URL itself is never shown.
"""

import http.server
import json
import socket
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

import pytest

from conftest import API_TOKEN, Running, rejected
from test_retry import FakeZuul

SECRET_URL = '/hook?token=s3cret-callback-token'


class Receiver:
    """The announcer's endpoint: records every POST it gets.

    status answers every POST with that HTTP status; delay holds each one
    that long before answering.
    """

    def __init__(self, status=200, delay=0):
        self.bodies = []
        self.headers = []
        self.attempts = 0
        self.status = status
        self.delay = delay
        me = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers['Content-Length'])
                body = json.loads(self.rfile.read(length))
                me.attempts += 1
                me.headers.append(dict(self.headers))
                time.sleep(me.delay)
                me.bodies.append(body)
                self.send_response(me.status)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'{}')

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
                                                      Handler)
        self.url = 'http://127.0.0.1:%d%s' % (
            self.server.server_address[1], SECRET_URL)
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()

    def outcomes(self):
        return [b['outcome'] for b in self.bodies]


@pytest.fixture
def receiver():
    made = []

    def make(**kw):
        made.append(Receiver(**kw))
        return made[-1]
    yield make
    for r in made:
        r.server.shutdown()


@pytest.fixture
def cb(make_forge):
    """A forge where "zuul" is Zuul's own name, with a "bundles" project."""
    running = make_forge(report_users=['zuul'])
    running.send({'action': 'create_project', 'project': 'bundles',
                  'files': {'README.md': 'bundles\n'}})
    return running


@pytest.fixture
def zuul():
    fake = FakeZuul()
    yield fake
    fake.server.shutdown()


def open_bundle(forge, key='b-1', **kw):
    msg = {'action': 'open_mr', 'project': 'bundles', 'key': key,
           'title': 'Bundle %s' % key,
           'files': {'bundle.yaml': 'id: %s\n' % key}}
    msg.update(kw)
    return forge.send(msg)


def mr_json(forge, key='b-1'):
    return forge.send({'action': 'label', 'project': 'bundles', 'mr': key})


def zuul_calls(forge, iid, verb, sha=None):
    """What Zuul's GitLab driver does: approve/unapprove through the API."""
    data = urllib.parse.urlencode({'sha': sha} if sha else {}).encode()
    req = urllib.request.Request(
        '%s/api/v4/projects/bundles/merge_requests/%s/%s' % (
            forge.url, iid, verb),
        data=data, headers={'Authorization': 'Bearer ' + API_TOKEN},
        method='POST')
    urllib.request.urlopen(req).close()


def delivered(forge, key='b-1', count=1):
    """True once `count` callbacks of the MR have a recorded status."""
    def done():
        records = mr_json(forge, key)['callback'] or []
        return len(records) >= count and all(r['status'] for r in records)
    return done


# -- scenario 1: passed --------------------------------------------------





# -- scenario 2: escalated -----------------------------------------------





# -- scenario 3: no callback_url behaves as today ------------------------

def test_without_a_callback_url_nothing_changes(cb, receiver, zuul):
    r = receiver()
    mr = open_bundle(cb)
    assert mr['callback'] is None
    zuul_calls(cb, mr['iid'], 'approve', mr['sha'])
    open_bundle(cb, key='b-2', max_attempts=1)
    time.sleep(0.4)
    assert r.attempts == 0 and mr_json(cb)['callback'] is None


# -- scenario 4: validation, before anything is created ------------------

@pytest.mark.parametrize('value', [
    'ftp://example.com/x', 'file:///etc/passwd', 'javascript:alert(1)',
    'example.com/hook', 'http://', 'https:///x', '', 123, True,
    ['http://example.com/x'], {'url': 'http://example.com/x'}],
    ids=['ftp', 'file', 'javascript', 'no-scheme', 'no-host-http',
         'no-host-https', 'empty', 'int', 'bool', 'list', 'dict'])
def test_a_bad_callback_url_is_a_400_and_creates_nothing(cb, value):
    hooks_before = len(cb.hooks)
    repo = cb.forge.repo(cb.forge.project('bundles'))
    master_before = repo.resolve('refs/heads/master')
    objects_before = repo.out('count-objects', '-v')
    code, message = rejected(cb, {
        'action': 'open_mr', 'project': 'bundles', 'key': 'b-1',
        'title': 'T', 'files': {'a.yaml': 'a: 1\n'}, 'callback_url': value})
    assert code == 400 and 'callback_url' in message
    assert not cb.forge.project('bundles')['mrs']
    assert cb.forge.project('bundles')['next_iid'] == 1
    assert len(cb.hooks) == hooks_before
    # No commit either: no MR ref, no new object, the branch untouched.
    assert repo.out('for-each-ref', 'refs/merge-requests') == ''
    assert repo.out('count-objects', '-v') == objects_before
    assert repo.resolve('refs/heads/master') == master_before
    # The key is free: the sender can try again with a good URL.
    assert open_bundle(cb, callback_url='http://example.com/x')['iid'] == 1


def test_http_and_https_are_accepted(cb):
    assert open_bundle(cb, key='a', callback_url='http://h:1/x')['iid']
    assert open_bundle(cb, key='b', callback_url='https://h/x?y=1')['iid']


# -- scenario 5: a dead callback -----------------------------------------

def dead_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port










# -- once per outcome per head sha ---------------------------------------







# -- a redelivered open_mr keeps the original ----------------------------



# -- the URL is never echoed, and Zuul sees nothing new ------------------



# -- the small migration: an old state.db --------------------------------

# The mr table as it was before callback_url (forge.py, SqliteStore.SCHEMA).
OLD_MR_TABLE = '''CREATE TABLE mr (
      id INTEGER PRIMARY KEY, iid INTEGER, project_id INTEGER,
      title TEXT, description TEXT, state TEXT,
      target_branch TEXT, source_branch TEXT, sha TEXT, start_sha TEXT,
      author TEXT, labels TEXT, approved_by TEXT,
      created_at TEXT, updated_at TEXT, merged_at TEXT, closed_at TEXT,
      merge_commit_sha TEXT, key TEXT, max_attempts INTEGER,
      escalated_sha TEXT, escalation TEXT)'''
OLD_MR_COLUMNS = ', '.join(
    'id iid project_id title description state target_branch source_branch '
    'sha start_sha author labels approved_by created_at updated_at '
    'merged_at closed_at merge_commit_sha key max_attempts escalated_sha '
    'escalation'.split())

def test_an_old_state_db_gains_the_columns_and_loses_nothing(
        tmp_path, receiver):
    path = tmp_path / 'data'
    first = Running(path, report_users=['zuul'])
    first.send({'action': 'create_project', 'project': 'bundles',
                'files': {'README.md': 'x\n'}})
    old = open_bundle(first, labels=['keep'], max_attempts=2)
    first.send({'action': 'comment', 'project': 'bundles', 'mr': 'b-1',
                'note': 'hello'})
    zuul_calls(first, old['iid'], 'approve', old['sha'])
    first.stop()

    # Make it the database an older version left: the mr table exactly as
    # it was before the callback columns, with the same rows.
    db = sqlite3.connect(str(path / 'state.db'))
    db.execute('ALTER TABLE mr RENAME TO mr_current')
    db.execute(OLD_MR_TABLE)
    db.execute('INSERT INTO mr SELECT %s FROM mr_current' % OLD_MR_COLUMNS)
    db.execute('DROP TABLE mr_current')
    db.commit()
    columns = [r[1] for r in db.execute('PRAGMA table_info(mr)')]
    assert 'callback_url' not in columns and 'callback' not in columns
    assert db.execute('SELECT count(*) FROM mr').fetchone()[0] == 1
    db.close()

    second = Running(path, report_users=['zuul'])
    try:
        db = sqlite3.connect(str(path / 'state.db'))
        columns = [r_[1] for r_ in db.execute('PRAGMA table_info(mr)')]
        db.close()
        assert 'callback_url' in columns and 'callback' in columns
        kept = mr_json(second)
        assert kept['sha'] == old['sha'] and kept['labels'] == ['keep']
        assert kept['max_attempts'] == 2 and kept['callback'] is None
        assert kept['title'] == 'Bundle b-1'
        p = second.forge.project('bundles')
        mr = second.forge.mr(p, 'b-1')
        assert [n['body'] for n in mr['notes']] == ['hello']
        assert second.forge.approvals_json(p, mr)['approved']
    finally:
        second.stop()


