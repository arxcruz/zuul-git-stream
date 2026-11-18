import http.server
import json
import threading
import time
import urllib.parse
import urllib.request

import pytest

from conftest import API_TOKEN, FAST_POLL, wait_for
from git_stream.retry import RetryPolicy


class FakeZuul:
    """Serves /builds from a list, and records escalations POSTed to it.

    read_status(query) and post_status(attempt) choose the HTTP status of a
    read or of the nth POST; post_delay holds each POST that long.
    """

    def __init__(self):
        self.builds = []
        self.queries = []
        self.escalations = []   # POSTs that were answered 2xx
        self.post_attempts = 0  # every POST, whatever the answer
        self.read_status = lambda query: 200
        self.post_status = lambda attempt: 200
        self.post_delay = 0
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, data, status=200):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(url.query))
                fake.queries.append(query)
                status = fake.read_status(query)
                self._reply(fake.builds if status == 200 else {}, status)

            def do_POST(self):
                length = int(self.headers['Content-Length'])
                event = json.loads(self.rfile.read(length))
                fake.post_attempts += 1
                time.sleep(fake.post_delay)
                status = fake.post_status(fake.post_attempts)
                if 200 <= status < 300:
                    fake.escalations.append(event)
                self._reply({}, status)

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
                                                      Handler)
        self.url = 'http://127.0.0.1:%d' % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever,
                         kwargs=FAST_POLL, daemon=True).start()

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
    assert wait_for(lambda: len(zuul.escalations) == 1)
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
    time.sleep(0.3)
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
    time.sleep(0.3)
    assert notes(forge, off['iid']) == [] and not zuul.escalations


def test_custom_limit_escalates_on_first_failure(forge, zuul, policy):
    mr = open_bundle(forge, max_attempts=1)
    zuul.run(**{'bundle-api': 'FAILURE'})
    report(forge, policy, mr['iid'], 'Build failed.')
    assert notes(forge, mr['iid'])[0].startswith('Escalated')
    assert wait_for(lambda: len(zuul.escalations) == 1)


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


# -- which results are failures ------------------------------------------------

@pytest.mark.parametrize('result', [
    'RETRY_LIMIT', 'ERROR', 'DISK_FULL', 'A_RESULT_ZUUL_ADDS_LATER'])
def test_any_result_that_is_not_known_good_is_a_failure(
        forge, zuul, policy, result):
    mr = open_bundle(forge)
    zuul.run(**{'bundle-api': result, 'bundle-auth': 'SUCCESS'})
    report(forge, policy, mr['iid'], 'Build failed.')
    assert notes(forge, mr['iid']) == ['rerun failed']  # not silently dropped

    for _ in range(2):
        zuul.run(**{'bundle-api': result})
    report(forge, policy, mr['iid'], 'Build failed.')
    assert wait_for(lambda: len(zuul.escalations) == 1)
    [job] = zuul.escalations[0]['failing']
    assert (job['job'], job['attempts'], job['result']) == (
        'bundle-api', 3, result)


@pytest.mark.parametrize('result', ['SKIPPED', 'RETRY', 'CANCELED', 'ABORTED'])
def test_results_that_are_no_verdict_are_not_failures(
        forge, zuul, policy, result):
    mr = open_bundle(forge)
    zuul.run(**{'bundle-api': result, 'bundle-auth': 'SUCCESS'})
    report(forge, policy, mr['iid'], 'Build failed.')
    time.sleep(0.3)
    assert notes(forge, mr['iid']) == [] and not zuul.escalations


# -- Zuul's builds API cannot be read ------------------------------------------

@pytest.fixture
def running(forge, zuul):
    """Start the policy's own thread, as `git-stream` does."""
    made = []

    def start(**kw):
        kw.setdefault('max_attempts', 3)
        kw.setdefault('escalate_urls', [zuul.url + '/escalations'])
        kw.setdefault('lookup_backoff', 0.05)
        kw.setdefault('escalate_backoff', 0.05)
        api = kw.pop('zuul_api', zuul.url)
        p = RetryPolicy(forge.forge, api, settle=0, **kw)
        p.start()
        made.append(p)
        return p
    return start


def report_later(forge, policy, iid, body='Build failed.'):
    """Zuul comments; the running policy picks it up by itself."""
    req = urllib.request.Request(
        '%s/api/v4/projects/bundles/merge_requests/%s/notes' % (
            forge.url, iid),
        data=urllib.parse.urlencode({'body': body}).encode(),
        headers={'Authorization': 'Bearer ' + API_TOKEN}, method='POST')
    urllib.request.urlopen(req).close()
    policy.observe(forge.hooks[-1])


def test_a_briefly_unreachable_zuul_is_retried(forge, zuul, running):
    mr = open_bundle(forge)
    zuul.run(**{'bundle-api': 'FAILURE'})
    zuul.read_status = lambda query: 503 if len(zuul.queries) < 3 else 200
    report_later(forge, running(), mr['iid'])
    assert wait_for(lambda: notes(forge, mr['iid']) == ['rerun failed'])
    assert len(zuul.queries) == 3  # two failed reads, then the good one
    assert forge.forge.escalations() == []


def test_a_zuul_that_stays_unreachable_is_escalated_not_lost(
        forge, zuul, running):
    mr = open_bundle(forge)
    zuul.run(**{'bundle-api': 'FAILURE'})
    zuul.read_status = lambda query: 503
    report_later(forge, running(lookup_attempts=3), mr['iid'])
    assert wait_for(lambda: len(forge.forge.escalations()) == 1)
    [esc] = forge.forge.escalations()
    assert esc['failing'] == []
    assert "could not read Zuul's results" in esc['error']
    assert '503' in esc['error']
    assert len(zuul.queries) == 3  # exactly lookup_attempts reads
    assert wait_for(lambda: notes(forge, mr['iid']) and notes(
        forge, mr['iid'])[0].startswith('Escalated'))
    assert "could not read Zuul's results" in notes(forge, mr['iid'])[0]
    assert forge.send({'action': 'label', 'project': 'bundles',
                       'mr': 'b-1'})['labels'] == ['escalated']
    assert wait_for(lambda: len(zuul.escalations) == 1)
    assert "could not read" in zuul.escalations[0]['error']


def test_a_refused_connection_is_escalated_too(forge, zuul, running):
    mr = open_bundle(forge)
    report_later(forge, running(zuul_api='http://127.0.0.1:1',
                                lookup_attempts=2), mr['iid'])
    assert wait_for(lambda: len(forge.forge.escalations()) == 1)
    assert "could not read Zuul's results" in forge.forge.escalations()[0][
        'error']


def test_a_stuck_lookup_does_not_delay_other_merge_requests(
        forge, zuul, running):
    stuck = open_bundle(forge)
    other = open_bundle(forge, key='b-2')
    zuul.run(**{'bundle-api': 'FAILURE'})
    zuul.read_status = lambda query: (
        503 if query['change'] == str(stuck['iid']) else 200)
    policy = running(lookup_backoff=1.0, lookup_attempts=3)
    report_later(forge, policy, stuck['iid'])
    assert wait_for(lambda: len(zuul.queries) >= 1)  # it is backing off now
    report_later(forge, policy, other['iid'])
    # b-2 is handled long before b-1's backoff (1 s, then 1 s more) is over.
    assert wait_for(lambda: notes(forge, other['iid']) == ['rerun failed'],
                    timeout=0.8)
    assert notes(forge, stuck['iid']) == []


def test_a_lookup_for_an_outdated_version_gives_up_quietly(
        forge, zuul, running):
    mr = open_bundle(forge)
    zuul.run(**{'bundle-api': 'FAILURE'})
    zuul.read_status = lambda query: 503
    report_later(forge, running(lookup_backoff=0.3), mr['iid'])
    assert wait_for(lambda: len(zuul.queries) >= 1)
    forge.send({'action': 'update_mr', 'project': 'bundles', 'mr': 'b-1',
                'files': {'bundle.yaml': 'id: b-1\nv: 2\n'}})  # new version
    time.sleep(1.5)  # long enough for every remaining attempt
    assert forge.forge.escalations() == []
    assert notes(forge, mr['iid']) == []


# -- escalation notices go out in the background ------------------------------

def escalate_one(forge, zuul, policy, key='b-1'):
    """Open a bundle whose first failure escalates, and report it."""
    mr = open_bundle(forge, key=key, max_attempts=1)
    zuul.run(**{'bundle-api': 'FAILURE'})
    report_later(forge, policy, mr['iid'])
    return mr


def test_a_dead_escalation_url_does_not_delay_other_retries(
        forge, zuul, running):
    zuul.post_status = lambda attempt: 503
    policy = running(escalate_attempts=5, escalate_backoff=1.0)
    escalate_one(forge, zuul, policy)
    assert wait_for(lambda: len(forge.forge.escalations()) == 1)
    other = open_bundle(forge, key='b-2')  # three attempts: will be rerun
    report_later(forge, policy, other['iid'])
    # While b-1's notice is still failing and backing off for seconds.
    assert wait_for(lambda: notes(forge, other['iid']) == ['rerun failed'],
                    timeout=0.8)
    assert len(zuul.escalations) == 0


def test_the_outcome_of_each_escalation_notice_is_recorded(
        forge, zuul, running):
    good = FakeZuul()
    try:
        zuul.post_status = lambda attempt: 503
        policy = running(escalate_urls=[zuul.url + '/e', good.url + '/e'],
                         escalate_attempts=3)
        escalate_one(forge, zuul, policy)
        assert wait_for(lambda: len(good.escalations) == 1)  # not held up
        assert wait_for(lambda: zuul.post_attempts == 3)  # tried 3 times
        notices = {}

        def recorded():
            [esc] = forge.forge.escalations()
            notices.update(esc.get('notifications') or {})
            return len(notices) == 2
        assert wait_for(recorded)
        assert notices[good.url + '/e'] == 'delivered'
        assert notices[zuul.url + '/e'].startswith('failed')
        assert '503' in notices[zuul.url + '/e']
    finally:
        good.server.shutdown()
