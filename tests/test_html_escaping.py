"""Hostile text in any value that reaches a web page is escaped.

The pages (server.py: index_page, project_page, mr_page, escalations_page,
metrics_page) are built by string concatenation, so every user-controlled
value has to go through html.escape.  Each test puts a payload into one value
through the real stream API (or the REST API, or the retry policy with a fake
Zuul), fetches every page that could show it and checks two things: the raw
payload is nowhere, and where the value is meant to be visible its escaped
form is there (so a pass is not just "the value never reached the page").

Names (projects, branches) cannot hold a space, so they use their own
payloads; the markup in them is the same.
"""

import html
import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from conftest import API_TOKEN, Running, wait_for
from git_stream.metrics import Metrics
from git_stream.retry import RetryPolicy
from test_callbacks import Receiver
from test_retry import FakeZuul, report

SCRIPT = '<script>alert(1)</script>'
TEXTS = [SCRIPT, '"><img src=x onerror=alert(1)>']
NAMES = [SCRIPT, '"><img/src=x/onerror=alert(1)>']


def fetch(forge, path):
    """(status, headers, body) of GET path; HTTP errors are returned too."""
    url = forge.url + urllib.parse.quote(path, safe='/?=&')
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.headers, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode()


def pages(forge, project='bundles', iid=1):
    """Every page that can show a value, by path."""
    paths = ['/', '/escalations', '/' + project,
             '/%s/merge_requests/%s' % (project, iid)]
    found = {}
    for path in paths:
        status, headers, body = fetch(forge, path)
        assert status == 200, (path, status)
        assert headers['Content-Type'] == 'text/html; charset=utf-8'
        found[path] = body
    return found


def assert_escaped(found, payload, visible_on):
    for path, body in found.items():
        assert payload not in body, '%s has the raw payload' % path
        assert '<script' not in body.lower(), '%s has a script tag' % path
        assert '<img' not in body.lower(), '%s has an img tag' % path
    for path in visible_on:
        assert html.escape(payload) in found[path], (
            '%s does not show the escaped value' % path)


def open_mr(forge, **kw):
    msg = {'action': 'open_mr', 'project': 'bundles', 'key': 'b-1',
           'title': 'Bundle b-1', 'data': {'bundle.yaml': 'id: b-1\n'}}
    msg.update(kw)
    return forge.send(msg)


MR = '/bundles/merge_requests/1'


# -- the MR's own fields -------------------------------------------------

@pytest.mark.parametrize('payload', TEXTS)
def test_an_mr_title_is_escaped(forge, payload):
    open_mr(forge, title=payload)
    assert_escaped(pages(forge), payload, ['/bundles', MR])


@pytest.mark.parametrize('payload', TEXTS)
def test_an_mr_description_is_escaped(forge, payload):
    open_mr(forge, description=payload)
    assert_escaped(pages(forge), payload, [MR])


@pytest.mark.parametrize('payload', TEXTS)
def test_a_note_is_escaped(forge, payload):
    open_mr(forge)
    forge.send({'action': 'comment', 'project': 'bundles', 'mr': 'b-1',
                'note': payload})
    assert_escaped(pages(forge), payload, [MR])


@pytest.mark.parametrize('payload', TEXTS)
def test_a_note_posted_through_the_rest_api_is_escaped(forge, payload):
    # What Zuul does: its report comes in as a note through the GitLab API.
    open_mr(forge)
    req = urllib.request.Request(
        forge.url + '/api/v4/projects/bundles/merge_requests/1/notes',
        data=urllib.parse.urlencode({'body': payload}).encode(),
        headers={'Authorization': 'Bearer ' + API_TOKEN}, method='POST')
    urllib.request.urlopen(req).close()
    assert_escaped(pages(forge), payload, [MR])


@pytest.mark.parametrize('payload', TEXTS)
def test_an_author_name_is_escaped(forge, payload):
    # On an open stream a message may name any author; it shows as the MR's
    # author, as a note's author and in "approved by".
    open_mr(forge, author=payload)
    forge.send({'action': 'comment', 'project': 'bundles', 'mr': 'b-1',
                'note': 'hello', 'author': payload})
    forge.send({'action': 'approve', 'project': 'bundles', 'mr': 'b-1',
                'author': payload})
    assert_escaped(pages(forge), payload, [MR])


@pytest.mark.parametrize('payload', TEXTS)
def test_labels_are_escaped(forge, payload):
    open_mr(forge, labels=[payload])
    forge.send({'action': 'label', 'project': 'bundles', 'mr': 'b-1',
                'add': [payload + 'more']})
    assert_escaped(pages(forge), payload, [MR])


@pytest.mark.parametrize('payload', TEXTS)
def test_changed_file_names_are_escaped(forge, payload):
    # The "Changes" box shows `git diff --name-status`.
    open_mr(forge)  # a file name needs real content: no message has one
    forge.forge.update_mr('bundles', 'b-1', 'me', files={payload: 'x\n'})
    assert_escaped(pages(forge), payload, [MR])


# -- names ---------------------------------------------------------------

@pytest.mark.parametrize('payload', NAMES)
def test_a_project_name_is_escaped(forge, payload):
    forge.send({'action': 'create_project', 'project': payload})
    open_mr(forge, project=payload)
    found = pages(forge, project=payload)
    assert_escaped(found, payload, ['/', '/' + payload,
                                    '/%s/merge_requests/1' % payload])


@pytest.mark.parametrize('payload', NAMES)
def test_a_branch_name_is_escaped(forge, payload):
    forge.send({'action': 'push', 'project': 'bundles', 'branch': payload,
                'data': {'a.txt': 'x\n'}})
    open_mr(forge, target_branch=payload)
    assert_escaped(pages(forge), payload, ['/bundles', MR])


# -- the retry policy's escalation ---------------------------------------

@pytest.fixture
def zuul():
    fake = FakeZuul()
    yield fake
    fake.server.shutdown()


def escalate(forge, zuul, title='Bundle b-1', job='bundle-api',
             log_url='http://logs/x/'):
    open_mr(forge, title=title, max_attempts=1)
    zuul.builds = [{'job_name': job, 'result': 'FAILURE', 'uuid': 'u-1',
                    'log_url': log_url}]
    policy = RetryPolicy(forge.forge, zuul.url, max_attempts=3, settle=0)
    report(forge, policy, 1, 'Build failed.')
    assert forge.forge.escalations(), 'the MR was not escalated'


@pytest.mark.parametrize('payload', TEXTS)
def test_an_escalated_title_is_escaped(forge, zuul, payload):
    escalate(forge, zuul, title=payload)
    assert_escaped(pages(forge), payload, ['/escalations', MR])


@pytest.mark.parametrize('payload', TEXTS)
def test_a_failing_job_name_from_zuul_is_escaped(forge, zuul, payload):
    escalate(forge, zuul, job=payload)
    assert_escaped(pages(forge), payload, ['/escalations', MR])


@pytest.mark.parametrize('payload', TEXTS)
def test_a_log_url_from_zuul_is_escaped(forge, zuul, payload):
    escalate(forge, zuul, log_url=payload)
    assert_escaped(pages(forge), payload, ['/escalations', MR])


def test_a_log_url_is_never_a_script_link(forge, zuul):
    # An href that starts "javascript:" runs when clicked, escaped or not.
    # Zuul's builds API is the source, so this is defence in depth.
    escalate(forge, zuul, log_url='javascript:alert(1)//')
    for path, body in pages(forge).items():
        assert 'href="javascript:' not in body.lower(), path


@pytest.mark.parametrize('log_url', [
    'http://logs.example.com/x/', 'https://logs.example.com/x/',
    'HTTPS://logs.example.com/x/'])
def test_an_http_log_url_is_a_link(forge, zuul, log_url):
    escalate(forge, zuul, log_url=log_url)
    for path in ('/escalations', MR):
        assert '<a href="%sjob-output.txt">log</a>' % log_url in pages(
            forge)[path]


@pytest.mark.parametrize('log_url', [
    'javascript:alert(1)//', 'JavaScript:alert(1)//', ' javascript:alert(1)',
    'data:text/html,x', 'ftp://logs/x/'])
def test_any_other_log_url_is_text_not_a_link(forge, zuul, log_url):
    escalate(forge, zuul, log_url=log_url)
    for path, body in pages(forge).items():
        assert 'job-output.txt">log</a>' not in body, path
    assert html.escape(log_url) in pages(forge)['/escalations']


# -- text that is recorded but not shown ---------------------------------

@pytest.mark.parametrize('payload', TEXTS)
def test_a_callback_status_from_the_receiver_is_escaped(tmp_path, payload):
    # "failed: <cause>" is the receiver's own status line.  No page shows
    # it today; this keeps it that way, or escaped, if one ever does.
    forge = Running(tmp_path / 'data', report_users=['zuul'])
    forge.send({'action': 'create_project', 'project': 'bundles'})
    r = Receiver(status=500, reason=payload.replace(' ', '_'))
    try:
        open_mr(forge, callback_url=r.url)
        req = urllib.request.Request(
            forge.url + '/api/v4/projects/bundles/merge_requests/1/approve',
            data=b'', headers={'Authorization': 'Bearer ' + API_TOKEN},
            method='POST')
        urllib.request.urlopen(req).close()

        def recorded():
            records = forge.send({'action': 'label', 'project': 'bundles',
                                  'mr': 'b-1'})['callback']
            return records and records[0]['status']
        assert wait_for(recorded)
        shown = payload.replace(' ', '_')
        assert shown in recorded()  # kept as text in the JSON record
        found = pages(forge)
        for path, body in found.items():
            assert shown not in body, path
    finally:
        r.server.shutdown()
        forge.stop()


# -- other responses that carry user text --------------------------------

@pytest.mark.parametrize('payload', NAMES)
def test_an_error_message_is_json_not_html(forge, payload):
    with pytest.raises(urllib.error.HTTPError) as e:
        forge.send({'action': 'create_project', 'project': payload + '.git'})
    assert e.value.headers['Content-Type'] == 'application/json'
    assert payload in json.load(e.value)['message']  # as JSON text


@pytest.mark.parametrize('payload', NAMES)
def test_an_unknown_path_is_not_echoed(forge, payload):
    status, headers, body = fetch(forge, '/' + payload)
    assert status == 404 and payload not in body
    assert headers['Content-Type'].startswith('text/plain')


@pytest.mark.parametrize('payload', TEXTS)
def test_a_raw_file_is_served_as_plain_text(forge, payload):
    forge.forge.push('bundles', {'page.html': payload}, None, 'me')
    status, headers, body = fetch(
        forge, '/api/v4/projects/bundles/repository/files/page.html/raw'
        '?ref=master')
    assert status == 200 and body == payload
    assert headers['Content-Type'].startswith('text/plain')


# -- the metrics page ----------------------------------------------------

@pytest.mark.parametrize('payload', TEXTS)
def test_the_metrics_page_escapes_its_filter_and_job_names(tmp_path, payload):
    metrics = Metrics(str(tmp_path / 'metrics.db'))
    forge = Running(tmp_path / 'data', metrics=metrics)
    try:
        metrics.record_results('bundles', 1, 'sha', [
            {'job_name': payload, 'result': 'FAILURE', 'uuid': 'u'}], 'http://z')
        for query in ('', '?project=bundles', '?project=' + payload):
            status, _, body = fetch(forge, '/metrics' + query)
            assert status == 200, query
            assert payload not in body, query
            assert '<script' not in body.lower()
            assert '<img' not in body.lower()
        # The job name is shown (escaped) when the filter allows it.
        assert html.escape(payload) in fetch(forge, '/metrics')[2]
        # The filter value is reflected, escaped.
        assert html.escape(payload) in fetch(
            forge, '/metrics?project=' + payload)[2]
    finally:
        forge.stop()
        metrics.close()
