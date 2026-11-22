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
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

import pytest

from conftest import API_TOKEN, FAST_POLL, Running, rejected, wait_for
from git_stream.retry import RetryPolicy
from test_retry import FakeZuul, report

SECRET_URL = '/hook?token=s3cret-callback-token'


class Receiver:
    """The announcer's endpoint: records every POST it gets.

    status answers every POST with that HTTP status (reason is its status
    line text, location a Location header); delay holds each one that long
    before answering.
    """

    def __init__(self, status=200, delay=0, reason=None, location=None):
        self.bodies = []
        self.headers = []
        self.attempts = 0
        self.status = status
        self.delay = delay
        self.reason = reason
        self.location = location
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
                self.send_response(me.status, me.reason)
                if me.location:
                    self.send_header('Location', me.location)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'{}')

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
                                                      Handler)
        self.url = 'http://127.0.0.1:%d%s' % (
            self.server.server_address[1], SECRET_URL)
        threading.Thread(target=self.server.serve_forever,
                         kwargs=FAST_POLL, daemon=True).start()

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
    running.send({'action': 'create_project', 'project': 'bundles'})
    return running


@pytest.fixture
def zuul():
    fake = FakeZuul()
    yield fake
    fake.server.shutdown()


def open_bundle(forge, key='b-1', **kw):
    msg = {'action': 'open_mr', 'project': 'bundles', 'key': key,
           'title': 'Bundle %s' % key,
           'data': {'bundle.yaml': 'id: %s\n' % key}}
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

def test_a_passing_mr_posts_passed_to_its_callback(cb, receiver):
    r = receiver()
    mr = open_bundle(cb, callback_url=r.url)
    zuul_calls(cb, mr['iid'], 'approve', mr['sha'])
    assert wait_for(lambda: r.attempts == 1)
    [body] = r.bodies
    assert body['event'] == 'outcome' and body['outcome'] == 'passed'
    assert body['project'] == 'bundles' and body['mr'] == mr['iid']
    assert body['key'] == 'b-1' and body['title'] == 'Bundle b-1'
    assert body['url'].endswith('/bundles/merge_requests/%s' % mr['iid'])
    assert body['sha'] == mr['sha'] and body['approved_by'] == ['zuul']
    assert isinstance(body['time'], float)
    assert r.headers[0]['Content-Type'] == 'application/json'


def test_a_human_approval_is_not_a_pass(cb, receiver):
    r = receiver()
    open_bundle(cb, callback_url=r.url)
    cb.send({'action': 'approve', 'project': 'bundles', 'mr': 'b-1',
             'author': 'alice'})
    time.sleep(0.4)
    assert r.attempts == 0 and mr_json(cb)['callback'] == []


# -- scenario 2: escalated -----------------------------------------------

def test_an_escalation_posts_escalated_to_its_callback(cb, receiver, zuul):
    r = receiver()
    mr = open_bundle(cb, callback_url=r.url, max_attempts=1)
    zuul.run(**{'bundle-api': 'SUCCESS', 'bundle-security': 'FAILURE'})
    policy = RetryPolicy(cb.forge, zuul.url, max_attempts=3, settle=0)
    report(cb, policy, mr['iid'], 'Build failed.')
    assert wait_for(lambda: r.attempts == 1)
    [body] = r.bodies
    # The recorded escalation, as is, plus the outcome.
    [recorded] = cb.forge.escalations()
    assert body['event'] == 'outcome' and body['outcome'] == 'escalated'
    for field in ('project', 'mr', 'key', 'title', 'url', 'sha', 'time',
                  'max_attempts', 'failing'):
        assert body[field] == recorded[field], field
    assert [(j['job'], j['attempts']) for j in body['failing']] == [
        ('bundle-security', 1)]


def test_no_callback_for_a_failed_attempt_or_a_rerun(cb, receiver, zuul):
    r = receiver()
    mr = open_bundle(cb, callback_url=r.url)  # 3 attempts: first is a rerun
    zuul.run(**{'bundle-security': 'FAILURE'})
    policy = RetryPolicy(cb.forge, zuul.url, max_attempts=3, settle=0)
    report(cb, policy, mr['iid'], 'Build failed.')
    time.sleep(0.4)
    assert r.attempts == 0


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
    ['http://example.com/x'], {'url': 'http://example.com/x'},
    'http://example.com/a\x7fb', 'http://example.com/a\x00b',
    'http://example.com/' + 'a' * 2030],
    ids=['ftp', 'file', 'javascript', 'no-scheme', 'no-host-http',
         'no-host-https', 'empty', 'int', 'bool', 'list', 'dict', 'del',
         'nul', 'too-long'])
def test_a_bad_callback_url_is_a_400_and_creates_nothing(cb, value):
    hooks_before = len(cb.hooks)
    repo = cb.forge.repo(cb.forge.project('bundles'))
    master_before = repo.resolve('refs/heads/master')
    objects_before = repo.out('count-objects', '-v')
    code, message = rejected(cb, {
        'action': 'open_mr', 'project': 'bundles', 'key': 'b-1',
        'title': 'T', 'data': {'a.yaml': 'a: 1\n'}, 'callback_url': value})
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


def test_the_length_limit_is_2048_characters(cb):
    url = 'http://example.com/'
    assert open_bundle(cb, key='ok', callback_url=url + 'a' * (
        2048 - len(url)))['iid']
    code, message = rejected(cb, {
        'action': 'open_mr', 'project': 'bundles', 'key': 'long',
        'title': 'T', 'data': {'a.yaml': 'a: 1\n'},
        'callback_url': url + 'a' * (2049 - len(url))})
    assert code == 400 and '2048' in message


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


@pytest.mark.slow
def test_a_failing_receiver_gets_one_attempt_and_is_recorded(cb, receiver):
    r = receiver(status=500)
    mr = open_bundle(cb, callback_url=r.url)
    zuul_calls(cb, mr['iid'], 'approve', mr['sha'])
    assert wait_for(delivered(cb))
    time.sleep(1.5)  # a retry, if there were one, would show by now
    assert r.attempts == 1
    [record] = mr_json(cb)['callback']
    assert record['outcome'] == 'passed' and record['sha'] == mr['sha']
    assert record['status'].startswith('failed: ') and (
        len(record['status']) > len('failed: '))
    assert isinstance(record['time'], float)


def test_a_refused_connection_is_recorded_as_failed(cb):
    mr = open_bundle(cb, callback_url='http://127.0.0.1:%d/x' % dead_port())
    zuul_calls(cb, mr['iid'], 'approve', mr['sha'])
    assert wait_for(delivered(cb))
    [record] = mr_json(cb)['callback']
    assert record['status'].startswith('failed: ')


def test_a_successful_delivery_is_recorded(cb, receiver):
    r = receiver()
    mr = open_bundle(cb, callback_url=r.url)
    zuul_calls(cb, mr['iid'], 'approve', mr['sha'])
    assert wait_for(delivered(cb))
    [record] = mr_json(cb)['callback']
    assert record['status'] == 'delivered'


@pytest.mark.parametrize('status', [301, 302, 307, 308])
def test_a_redirect_is_a_failure_and_is_not_followed(cb, receiver, status):
    elsewhere = receiver()
    r = receiver(status=status, location=elsewhere.url)
    mr = open_bundle(cb, callback_url=r.url)
    zuul_calls(cb, mr['iid'], 'approve', mr['sha'])
    assert wait_for(delivered(cb))
    [record] = mr_json(cb)['callback']
    assert record['status'].startswith('failed: ') and str(status) in (
        record['status'])
    time.sleep(0.5)
    assert r.attempts == 1 and elsewhere.attempts == 0


def test_the_url_is_removed_from_the_error_text(cb, receiver):
    """The receiver chooses the status line, so it can put the URL, secret
    and all, in the very text the recorded cause is made of."""
    r = receiver(status=500)
    r.reason = r.url  # the status line of the response is the URL
    mr = open_bundle(cb, callback_url=r.url)
    zuul_calls(cb, mr['iid'], 'approve', mr['sha'])
    assert wait_for(delivered(cb))
    [record] = mr_json(cb)['callback']
    assert 's3cret-callback-token' not in record['status']
    assert record['status'] == 'failed: HTTP Error 500: <callback_url>'


def test_a_hanging_callback_delays_neither_zuul_nor_other_mrs(cb, receiver):
    slow, fast = receiver(delay=4), receiver()
    a = open_bundle(cb, key='a', callback_url=slow.url)
    b = open_bundle(cb, key='b', callback_url=fast.url)
    started = time.time()
    zuul_calls(cb, a['iid'], 'approve', a['sha'])
    assert time.time() - started < 1.5  # Zuul's call is not held
    zuul_calls(cb, b['iid'], 'approve', b['sha'])
    assert wait_for(lambda: fast.attempts == 1, timeout=2)
    assert time.time() - started < 3  # b did not wait for a


def test_a_callback_ignores_the_proxy_environment(receiver):
    """It connects to the URL it was given, not through HTTP(S)_PROXY.

    Run in a fresh interpreter: urllib reads the environment when it is
    imported, so setting it in this process would prove nothing.
    """
    proxy, target = receiver(), receiver()
    proxy_url = proxy.url.split('/hook')[0]
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy',
                                'no_proxy')}
    env.update(http_proxy=proxy_url, HTTP_PROXY=proxy_url,
               https_proxy=proxy_url, HTTPS_PROXY=proxy_url)
    code = ('import sys; from git_stream import callbacks; '
            'print(callbacks.deliver(sys.argv[1], {"outcome": "passed"}))')
    done = subprocess.run([sys.executable, '-c', code, target.url], env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.stdout.strip() == 'delivered', done.stderr
    assert target.outcomes() == ['passed']
    assert proxy.attempts == 0


# -- once per outcome per head sha ---------------------------------------

def test_escalated_then_passed_sends_both_in_order(cb, receiver, zuul):
    r = receiver()
    mr = open_bundle(cb, callback_url=r.url, max_attempts=1)
    zuul.run(**{'bundle-security': 'FAILURE'})
    policy = RetryPolicy(cb.forge, zuul.url, max_attempts=3, settle=0)
    report(cb, policy, mr['iid'], 'Build failed.')
    assert wait_for(lambda: r.outcomes() == ['escalated'])
    zuul_calls(cb, mr['iid'], 'approve', mr['sha'])
    assert wait_for(lambda: r.outcomes() == ['escalated', 'passed'])
    assert [x['outcome'] for x in mr_json(cb)['callback']] == [
        'escalated', 'passed']


def test_a_new_version_during_a_retry_keeps_the_escalated_sha(
        cb, receiver, zuul):
    """The escalation is of the version Zuul reported, even if a newer one
    lands while the builds API is being read."""
    r = receiver()
    mr = open_bundle(cb, callback_url=r.url, max_attempts=1)
    zuul.run(**{'bundle-security': 'FAILURE'})
    landed = []

    def lookup(query):
        if not landed:
            landed.append(cb.send({
                'action': 'update_mr', 'project': 'bundles', 'mr': 'b-1',
                'data': {'bundle.yaml': 'id: newer\n'}}))
        return 200
    zuul.read_status = lookup
    policy = RetryPolicy(cb.forge, zuul.url, max_attempts=3, settle=0)
    report(cb, policy, mr['iid'], 'Build failed.')
    [newer] = landed
    assert newer['sha'] != mr['sha']

    assert wait_for(lambda: r.outcomes() == ['escalated'])
    assert r.bodies[0]['sha'] == mr['sha']
    assert [(x['outcome'], x['sha']) for x in mr_json(cb)['callback']] == [
        ('escalated', mr['sha'])]

    # The new version is its own: it can still pass, and say so.
    zuul_calls(cb, mr['iid'], 'approve', newer['sha'])
    assert wait_for(lambda: r.outcomes() == ['escalated', 'passed'])
    assert r.bodies[1]['sha'] == newer['sha']


def test_approve_unapprove_approve_sends_passed_once(cb, receiver):
    r = receiver()
    mr = open_bundle(cb, callback_url=r.url)
    zuul_calls(cb, mr['iid'], 'approve', mr['sha'])
    zuul_calls(cb, mr['iid'], 'unapprove')
    zuul_calls(cb, mr['iid'], 'approve', mr['sha'])
    assert wait_for(lambda: r.attempts == 1)
    time.sleep(0.5)
    assert r.outcomes() == ['passed']


def test_a_new_sha_resets_what_has_fired(cb, receiver):
    r = receiver()
    mr = open_bundle(cb, callback_url=r.url)
    zuul_calls(cb, mr['iid'], 'approve', mr['sha'])
    assert wait_for(lambda: r.attempts == 1)
    updated = cb.send({'action': 'update_mr', 'project': 'bundles',
                       'mr': 'b-1', 'data': {'bundle.yaml': 'id: new\n'}})
    assert updated['sha'] != mr['sha']  # new commits drop the approval
    zuul_calls(cb, mr['iid'], 'approve', updated['sha'])
    assert wait_for(lambda: r.attempts == 2)
    assert [b['sha'] for b in r.bodies] == [mr['sha'], updated['sha']]


# -- a redelivered open_mr keeps the original ----------------------------

def test_a_redelivered_open_mr_keeps_the_original_callback(cb, receiver):
    first, second = receiver(), receiver()
    mr = open_bundle(cb, callback_url=first.url)
    again = open_bundle(cb, callback_url=second.url)
    assert again['iid'] == mr['iid']
    zuul_calls(cb, mr['iid'], 'approve', again['sha'])
    assert wait_for(lambda: first.attempts == 1)
    time.sleep(0.4)
    assert second.attempts == 0


# -- the URL is never echoed, and Zuul sees nothing new ------------------

def test_the_url_is_never_shown_and_zuul_sees_no_new_field(
        cb, receiver, zuul):
    r = receiver(status=500)
    mr = open_bundle(cb, callback_url=r.url, max_attempts=1)
    zuul_calls(cb, mr['iid'], 'approve', mr['sha'])
    zuul.run(**{'bundle-security': 'FAILURE'})
    policy = RetryPolicy(cb.forge, zuul.url, max_attempts=3, settle=0)
    report(cb, policy, mr['iid'], 'Build failed.')
    assert wait_for(delivered(cb, count=2))
    assert 'callback' not in json.dumps(cb.hooks)  # webhooks to Zuul
    seen = [json.dumps(mr_json(cb))]
    for path in ('/api/stream/escalations', '/escalations',
                 '/bundles/merge_requests/%s' % mr['iid'], '/',
                 '/api/v4/projects/bundles/merge_requests/%s' % mr['iid']):
        with urllib.request.urlopen(cb.url + path) as resp:
            seen.append(resp.read().decode())
    seen.append(json.dumps(cb.hooks))
    for text in seen:
        assert 's3cret-callback-token' not in text
        assert '/hook' not in text


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
    first.send({'action': 'create_project', 'project': 'bundles'})
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


def test_a_migrated_db_keeps_callback_state_across_restarts(
        tmp_path, receiver):
    path = tmp_path / 'data'
    first = Running(path, report_users=['zuul'])
    first.send({'action': 'create_project', 'project': 'bundles'})
    first.stop()
    db = sqlite3.connect(str(path / 'state.db'))
    db.execute('ALTER TABLE mr RENAME TO mr_current')
    db.execute(OLD_MR_TABLE)
    db.execute('DROP TABLE mr_current')
    db.commit()
    db.close()

    r = receiver()
    second = Running(path, report_users=['zuul'])
    try:
        new = open_bundle(second, key='b-2', callback_url=r.url)
        zuul_calls(second, new['iid'], 'approve', new['sha'])
        assert wait_for(lambda: r.attempts == 1)
        assert wait_for(delivered(second, 'b-2'))
    finally:
        second.stop()
    third = Running(path, report_users=['zuul'])
    try:
        [record] = mr_json(third, 'b-2')['callback']
        assert record['outcome'] == 'passed'
        assert record['status'] == 'delivered'
    finally:
        third.stop()
