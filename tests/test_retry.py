import http.server
import json
import threading
import urllib.parse
import urllib.request

import pytest

from conftest import API_TOKEN
from git_stream.retry import RetryPolicy


class FakeZuul:
    """Serves /builds from a list, and records escalations POSTed to it."""

    def __init__(self):
        self.builds = []
        self.queries = []
        self.escalations = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, data):
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                fake.queries.append(dict(urllib.parse.parse_qsl(url.query)))
                self._reply(fake.builds)

            def do_POST(self):
                length = int(self.headers['Content-Length'])
                fake.escalations.append(json.loads(self.rfile.read(length)))
                self._reply({})

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
                                                      Handler)
        self.url = 'http://127.0.0.1:%d' % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()

    def run(self, **results):
        """Record one buildset: job name -> result (newest first)."""
        self.builds[:0] = [{'job_name': job, 'result': result,
                            'uuid': '%s-%d' % (job, len(self.builds)),
                            'log_url': 'http://logs/%s/' % job}
                           for job, result in results.items()]


@pytest.fixture
def zuul():
    fake = FakeZuul()
    yield fake
    fake.server.shutdown()


@pytest.fixture
def policy(forge, zuul):
    return RetryPolicy(forge.forge, zuul.url, max_attempts=3,
                       escalate_urls=[zuul.url + '/escalations'], settle=0)


def report(forge, policy, iid, body):
    """Zuul comments on the MR; the policy reacts to it."""
    req = urllib.request.Request(
        '%s/api/v4/projects/bundles/merge_requests/%s/notes' % (
            forge.url, iid),
        data=urllib.parse.urlencode({'body': body}).encode(),
        headers={'Authorization': 'Bearer ' + API_TOKEN}, method='POST')
    urllib.request.urlopen(req).close()
    policy.observe(forge.hooks[-1])
    while not policy.queue.empty():
        policy.handle(*policy.queue.get_nowait())


def notes(forge, iid):
    mr = forge.forge.mr(forge.forge.project('bundles'), iid)
    return [n['body'] for n in mr['notes'] if n['author'] == 'git-stream']


def open_bundle(forge, **kw):
    msg = {'action': 'open_mr', 'project': 'bundles', 'key': 'b-1',
           'title': 'Bundle b-1', 'files': {'bundle.yaml': 'id: b-1\n'}}
    msg.update(kw)
    return forge.send(msg)


def test_retries_then_escalates(forge, zuul, policy):
    mr = open_bundle(forge)
    zuul.run(**{'bundle-dispatch': 'SUCCESS', 'bundle-api': 'FAILURE',
                'bundle-auth': 'SUCCESS', 'bundle-security': 'FAILURE'})
    report(forge, policy, mr['iid'], 'Build failed.\n- ...')
    assert notes(forge, mr['iid']) == ['rerun failed']
    assert zuul.queries[-1]['patchset'] == mr['sha']

    # api passes on the retry; security keeps failing.
    zuul.run(**{'bundle-dispatch': 'SUCCESS', 'bundle-api': 'SUCCESS',
                'bundle-auth': 'SKIPPED', 'bundle-security': 'FAILURE'})
    report(forge, policy, mr['iid'], 'Build failed.')
    assert notes(forge, mr['iid'])[-1] == 'rerun failed'

    zuul.run(**{'bundle-dispatch': 'SUCCESS', 'bundle-security': 'FAILURE'})
    report(forge, policy, mr['iid'], 'Build failed.')
    assert notes(forge, mr['iid'])[-1].startswith(
        'Escalated: still failing after 3 attempts.')
    [event] = zuul.escalations
    assert event['key'] == 'b-1'
    assert [(j['job'], j['attempts']) for j in event['failing']] == [
        ('bundle-security', 3)]
    got = forge.send({'action': 'label', 'project': 'bundles', 'mr': 'b-1'})
    assert got['labels'] == ['escalated'] and got['escalated']

    # Recorded, and shown on the pages.
    [recorded] = forge.forge.escalations()
    assert recorded['key'] == 'b-1' and recorded['current']
    with urllib.request.urlopen(forge.url + '/api/stream/escalations') as r:
        assert json.load(r)[0]['failing'][0]['job'] == 'bundle-security'
    for page in ('/escalations', '/bundles/merge_requests/%s' % mr['iid']):
        with urllib.request.urlopen(forge.url + page) as r:
            html = r.read().decode()
        assert 'Still failing after 3 runs' in html
        assert 'bundle-security' in html

    # Escalated once per version, however often Zuul reports again.
    report(forge, policy, mr['iid'], 'Build failed.')
    assert len(zuul.escalations) == 1


def test_only_jobs_with_attempts_left_are_rerun(forge, zuul, policy):
    mr = open_bundle(forge)
    for _ in range(3):
        zuul.run(**{'bundle-security': 'FAILURE'})
    zuul.run(**{'bundle-api': 'FAILURE', 'bundle-security': 'FAILURE'})
    report(forge, policy, mr['iid'], 'Build failed.')
    assert notes(forge, mr['iid']) == ['rerun bundle-api']
    assert not zuul.escalations


def test_ignores_other_comments_and_off_switch(forge, zuul, policy):
    mr = open_bundle(forge)
    zuul.run(**{'bundle-api': 'FAILURE'})
    report(forge, policy, mr['iid'], 'Starting bundle-test jobs.')
    report(forge, policy, mr['iid'], 'Build succeeded.')
    assert notes(forge, mr['iid']) == []

    off = open_bundle(forge, key='b-2', max_attempts=0)
    report(forge, policy, off['iid'], 'Build failed.')
    assert notes(forge, off['iid']) == [] and not zuul.escalations


def test_custom_limit_escalates_on_first_failure(forge, zuul, policy):
    mr = open_bundle(forge, max_attempts=1)
    zuul.run(**{'bundle-api': 'FAILURE'})
    report(forge, policy, mr['iid'], 'Build failed.')
    assert notes(forge, mr['iid'])[0].startswith('Escalated')
    assert len(zuul.escalations) == 1


def test_metrics_records_runs_and_escalation(forge, zuul, tmp_path):
    from git_stream.metrics import Metrics
    metrics = Metrics(str(tmp_path / 'metrics.db'))
    policy = RetryPolicy(forge.forge, zuul.url, max_attempts=2,
                         escalate_urls=[zuul.url + '/escalations'],
                         settle=0, metrics=metrics)
    try:
        mr = open_bundle(forge)
        zuul.run(**{'bundle-security': 'FAILURE', 'bundle-api': 'SUCCESS'})
        report(forge, policy, mr['iid'], 'Build failed.')
        # The whole buildset of the version was logged, successes included.
        s = metrics.summary(project='bundles')
        by_job = {j['job']: j for j in s['jobs']}
        assert by_job['bundle-security']['fail'] == 1
        assert by_job['bundle-security']['runs'] == 1
        assert by_job['bundle-api']['pass'] == 1
        # One run per escalation.
        zuul.run(**{'bundle-security': 'FAILURE'})  # 3rd failure of the version
        report(forge, policy, mr['iid'], 'Build failed.')
        s = metrics.summary(project='bundles')
        # Zuul returns every build of the version, so failures accumulate.
        assert {j['job']: j for j in s['jobs']}['bundle-security']['fail'] == 3
        [esc] = metrics.escalations()
        assert esc['key'] == 'b-1' and esc['max_attempts'] == 2
    finally:
        metrics.close()
