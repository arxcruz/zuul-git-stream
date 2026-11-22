"""The forge, as seen by Zuul's own GitLab driver code.

These tests drive Zuul's GitlabAPIClient against the forge and feed the
webhooks it emits to Zuul's GitlabEventConnector handlers, so a change
that Zuul would not understand fails here rather than in a demo.
"""

import json
import re
import socket
import threading
import types

import pytest

from conftest import API_TOKEN, wait_for
from git_stream import stream

# These tests are the safety net for "Zuul must not need changes", so a
# missing Zuul is a failure, not a skip.  Only a missing `zuul` itself gets
# the friendly message; any other import error (a broken Zuul install)
# propagates untouched.  The failure is raised by a fixture rather than at
# import so that collection still works and `-m "not contract"` can
# deselect these tests.
ZUUL_MISSING = None
try:
    import zuul.driver.gitlab.gitlabconnection as gitlab
    from zuul.driver.gitlab.gitlabsource import GitlabSource
except ModuleNotFoundError as e:
    if (e.name or '').split('.')[0] != 'zuul':
        raise
    ZUUL_MISSING = (
        'Zuul is not importable, and the contract tests need it: they '
        "drive Zuul's real GitLab driver against the forge.\n"
        '  Install it:  uv pip install -p .venv/bin/python '
        "'git+https://opendev.org/zuul/zuul'\n"
        '  Or skip them for a quick local run:  pytest -m "not contract"')

pytestmark = pytest.mark.contract


@pytest.fixture(autouse=True)
def zuul_installed():
    if ZUUL_MISSING:
        pytest.fail(ZUUL_MISSING, pytrace=False)


@pytest.fixture
def client(forge):
    return gitlab.GitlabAPIClient(forge.url, API_TOKEN, 60, False)


def parser_for(forge):
    """Zuul's webhook parser, with just enough of a connection under it."""
    conn = types.SimpleNamespace(
        event_queue=None, connection_name='gitlab',
        getMRUrl=lambda project, number: '%s/%s/merge_requests/%s' % (
            forge.url, project, number),
        clearConnectionCacheOnBranchEvent=lambda event: None)
    connector = gitlab.GitlabEventConnector(conn)

    def parse(payload):
        return connector.event_handler_mapping[payload['object_kind']](
            payload)
    return parse


@pytest.fixture
def connector(forge):
    return parser_for(forge)


def open_mr(forge, **kw):
    msg = {'action': 'open_mr', 'project': 'bundles', 'title': 'Bundle b-1',
           'key': 'b-1', 'data': {'bundle.yaml': 'id: b-1\n'}}
    msg.update(kw)
    return forge.send(msg)


def test_mr_as_read_by_zuul(forge, client):
    mr = open_mr(forge, description='Depends-On: x')
    got = client.get_mr('bundles', mr['iid'])
    assert got['diff_refs']['head_sha'] == got['sha'] == mr['sha']
    assert got['diff_refs']['base_sha'] == got['diff_refs']['start_sha']
    assert got['state'] == 'opened'
    assert got['merge_status'] == 'can_be_merged'
    assert got['target_branch'] == 'master'
    assert got['description'] == 'Depends-On: x'
    assert got['author']['username'] == 'git-stream'
    approvals = client.get_mr_approvals_status('bundles', mr['iid'])
    assert approvals['approved'] is False


def test_an_mr_with_a_callback_url_looks_the_same_to_zuul(make_forge):
    """callback_url is for the announcer: Zuul reads, reports, approves and
    parses webhooks exactly as for any other MR, and never sees the URL."""
    forge = make_forge(report_users=['zuul'])
    forge.send({'action': 'create_project', 'project': 'bundles'})
    client = gitlab.GitlabAPIClient(forge.url, API_TOKEN, 60, False)
    parse = parser_for(forge)
    url = 'http://127.0.0.1:9/hook?token=s3cret-callback-token'
    plain = open_mr(forge, key='plain')
    mr = open_mr(forge, key='b-1', callback_url=url)

    got, got_plain = (client.get_mr('bundles', m['iid']) for m in (mr, plain))
    assert set(got) == set(got_plain)  # no field Zuul could trip over
    assert got['diff_refs']['head_sha'] == got['sha'] == mr['sha']
    assert got['state'] == 'opened'
    assert got['merge_status'] == 'can_be_merged'

    client.comment_mr('bundles', mr['iid'], 'Build succeeded.')
    assert client.approve_mr('bundles', mr['iid'], mr['sha'])['approved']
    assert client.approve_mr('bundles', mr['iid'], mr['sha']) is None
    assert client.get_mr_approvals_status('bundles', mr['iid'])['approved']

    kinds = set()
    for hook in forge.hooks:
        event = parse(hook)  # every webhook still parses
        kinds.add(event.type)
        text = json.dumps(hook)
        assert 'callback' not in text and 's3cret' not in text
    assert 'gl_merge_request' in kinds
    approved = [parse(h) for h in forge.hooks_of('merge_request')
                if h['object_attributes']['action'] == 'approved']
    assert [e.action for e in approved] == ['approved']
    assert 's3cret' not in json.dumps(got)


def test_a_message_only_mr_looks_the_same_to_zuul(make_forge):
    """An MR made from data alone (no files) is an ordinary MR to Zuul: a
    sha with diff_refs, a head ref a clone can fetch, webhooks that parse,
    and a changed-MR event when its data changes."""
    import subprocess
    forge = make_forge(report_users=['zuul'])
    forge.send({'action': 'create_project', 'project': 'bundles'})
    client = gitlab.GitlabAPIClient(forge.url, API_TOKEN, 60, False)
    parse = parser_for(forge)
    plain = open_mr(forge, key='plain')
    mr = forge.send({'action': 'open_mr', 'project': 'bundles', 'title': 'T',
                     'key': 'd-1', 'data': {'id': 'd-1', 'flaky': 0.3}})

    got, got_plain = (client.get_mr('bundles', m['iid']) for m in (mr, plain))
    assert set(got) == set(got_plain)
    assert got['diff_refs']['head_sha'] == got['sha'] == mr['sha']
    assert got['merge_status'] == 'can_be_merged'
    assert 'flaky' not in json.dumps(got)  # the data is not Zuul's business

    ref = 'refs/merge-requests/%d/head' % mr['iid']
    repo = forge.forge.repo(forge.forge.project('bundles'))
    assert repo.resolve(ref) == mr['sha']
    clone = subprocess.run(
        ['git', 'ls-remote', '%s/bundles.git' % forge.url, ref],
        capture_output=True, text=True, check=True).stdout
    assert clone.split()[0] == mr['sha']

    updated = forge.send({'action': 'update_mr', 'project': 'bundles',
                          'mr': 'd-1', 'data': {'id': 'd-1', 'flaky': 0.0}})
    assert client.get_mr('bundles', mr['iid'])['sha'] == updated['sha']
    events = [parse(h) for h in forge.hooks_of('merge_request')
              if h['object_attributes']['iid'] == mr['iid']]
    assert [e.action for e in events] == ['opened', 'changed']
    assert events[-1].patch_number == updated['sha']
    for hook in forge.hooks:
        assert 'flaky' not in json.dumps(hook)

    client.comment_mr('bundles', mr['iid'], 'Build succeeded.')
    assert client.approve_mr('bundles', mr['iid'], updated['sha'])['approved']


def test_branches_as_read_by_zuul(forge, client):
    forge.send({'action': 'push', 'project': 'bundles', 'branch': 'stable',
                'data': {'x': '1'}})
    assert client.get_project_branches('bundles', False) == [
        'master', 'stable']
    assert client.get_project_branches('bundles', True) == ['master']
    sha = client.get_project_branch('bundles', 'stable')['commit']['id']
    assert len(sha) == 40
    assert client.get_project_branch('bundles', 'nope') == {}


def test_reporting_as_done_by_zuul(forge, client):
    mr = open_mr(forge)
    iid = mr['iid']
    client.comment_mr('bundles', iid, 'Build succeeded.')
    note = forge.hooks_of('note')[-1]
    assert note['object_attributes']['note'] == 'Build succeeded.'
    assert note['user']['username'] == 'zuul'

    assert client.approve_mr('bundles', iid, mr['sha'])['approved'] is True
    # Approving twice (401) and approving a stale sha (409) are expected.
    assert client.approve_mr('bundles', iid, mr['sha']) is None
    assert client.approve_mr('bundles', iid, '0' * 40) is None
    assert client.get_mr_approvals_status('bundles', iid)['approved']
    assert client.approve_mr('bundles', iid, None, approve=False)
    assert client.approve_mr('bundles', iid, None, approve=False) is None

    client.update_mr('bundles', iid, add_labels='gated,verified',
                     remove_labels='')
    assert client.get_mr('bundles', iid)['labels'] == ['gated', 'verified']

    merged = client.merge_mr('bundles', iid, 'merge')
    assert merged['state'] == 'merged'
    assert merged['merge_commit_sha']


def test_writes_need_a_token(forge):
    client = gitlab.GitlabAPIClient(forge.url, 'wrong', 60, False)
    mr = open_mr(forge)
    with pytest.raises(gitlab.GitlabAPIClientException, match='401'):
        client.comment_mr('bundles', mr['iid'], 'hi')


def test_webhooks_as_parsed_by_zuul(forge, connector):
    mr = open_mr(forge, description='first')
    event = connector(forge.hooks_of('merge_request')[-1])
    assert (event.type, event.action) == ('gl_merge_request', 'opened')
    assert event.project_name == 'bundles'
    assert event.change_number == mr['iid']
    assert event.ref == 'refs/merge-requests/%s/head' % mr['iid']
    assert event.patch_number == mr['sha']
    assert event.branch == 'master'

    # Same key again: a new commit on the same MR.
    again = open_mr(forge, data={'bundle.yaml': 'id: b-1\nv: 2\n'})
    assert again['iid'] == mr['iid'] and again['sha'] != mr['sha']
    event = connector(forge.hooks_of('merge_request')[-1])
    assert event.action == 'changed'
    assert event.patch_number == again['sha']

    forge.send({'action': 'update_mr', 'project': 'bundles', 'mr': 'b-1',
                'description': 'second'})
    event = connector(forge.hooks_of('merge_request')[-1])
    assert event.action == 'changed'
    assert event.merge_request_description_changed

    forge.send({'action': 'label', 'project': 'bundles', 'mr': 'b-1',
                'add': ['urgent']})
    event = connector(forge.hooks_of('merge_request')[-1])
    assert (event.action, event.labels) == ('labeled', ['urgent'])

    forge.send({'action': 'comment', 'project': 'bundles', 'mr': 'b-1',
                'note': 'recheck', 'author': 'alice'})
    event = connector(forge.hooks_of('note')[-1])
    assert (event.type, event.action) == ('gl_merge_request', 'comment')
    assert event.comment == 'recheck'
    assert event.change_number == mr['iid']

    forge.send({'action': 'approve', 'project': 'bundles', 'mr': 'b-1'})
    assert connector(forge.hooks_of('merge_request')[-1]).action == \
        'approved'

    forge.send({'action': 'merge', 'project': 'bundles', 'mr': 'b-1'})
    push = connector(forge.hooks_of('push')[-1])
    assert (push.type, push.branch) == ('gl_push', 'master')
    assert push.newrev != push.oldrev
    assert connector(forge.hooks_of('merge_request')[-1]).action == 'merged'


def test_push_and_tag_webhooks(forge, connector):
    # The push hook lists the files a commit added and removed; a message
    # carries none, so make one with the forge's own API.
    forge.forge.push('bundles', {'README.md': 'x\n'}, 'Add README', 'me')
    res = forge.forge.push('bundles', {'a.txt': 'a', 'README.md': None},
                           'Add a, drop README', 'me')
    hook = forge.hooks_of('push')[-1]
    event = connector(hook)
    assert (event.ref, event.oldrev, event.newrev) == (
        'refs/heads/master', res['before'], res['after'])
    assert hook['commits'][-1]['added'] == ['a.txt']
    assert hook['commits'][-1]['removed'] == ['README.md']

    forge.send({'action': 'tag', 'project': 'bundles', 'tag': 'v1'})
    event = connector(forge.hooks_of('tag_push')[-1])
    assert (event.tag, event.newrev) == ('v1', res['after'])


def test_depends_on_urls_resolve(forge):
    mr = open_mr(forge)
    source = GitlabSource.__new__(GitlabSource)
    source.change_re = re.compile(r"/(.*?)/(?:-/)?merge_requests/(\d+)")
    m = source.change_re.match(
        mr['web_url'][len(forge.url):])
    assert m.groups() == ('bundles', str(mr['iid']))


def test_builds_api_takes_the_parameters_the_retry_policy_sends():
    import inspect
    from zuul.web import ZuulWebAPI
    builds = getattr(ZuulWebAPI.builds, '__wrapped__', ZuulWebAPI.builds)
    sent = {'project', 'change', 'patchset', 'complete', 'limit'}  # retry.py
    assert sent <= set(inspect.signature(builds).parameters)


def test_builds_api_returns_the_fields_the_retry_policy_reads():
    from zuul.web import BuildConverter
    read = {'job_name', 'result', 'uuid', 'log_url'}  # retry.py
    assert read <= set(BuildConverter.schema().value)  # a Prop wrapping it


def test_zuul_retries_a_503_from_a_full_connection_cap(make_forge,
                                                      monkeypatch):
    """At max_connections git-stream refuses with 503 and Retry-After; Zuul's
    own client must ride that out, not raise, once a slot is free."""
    forge = make_forge(max_connections=1)
    # Set up in-process: over HTTP, each setup request's slot is released
    # just after its reply, which could race the next request into a 503.
    stream.apply(forge.forge, {'action': 'create_project',
                               'project': 'bundles'},
                 'git-stream')
    mr = stream.apply(forge.forge, {
        'action': 'open_mr', 'project': 'bundles', 'title': 'Bundle b-1',
        'key': 'b-1', 'data': {'bundle.yaml': 'id: b-1\n'}}, 'git-stream')
    refusals = []
    real = forge.server._refuse

    def counted(request, client_address):
        refusals.append(client_address)
        return real(request, client_address)
    monkeypatch.setattr(forge.server, '_refuse', counted)

    # A test connection holds the only slot, after one full request.
    holder = socket.create_connection(
        ('127.0.0.1', int(forge.url.rsplit(':', 1)[1])), timeout=5)
    try:
        holder.sendall(b'GET /health HTTP/1.1\r\nHost: x\r\n\r\n')
        assert b'200' in holder.recv(4096)

        client = gitlab.GitlabAPIClient(forge.url, API_TOKEN, 60, False)
        outcome = []

        def get():
            try:
                outcome.append(client.get_mr('bundles', mr['iid']))
            except Exception as e:  # the answer is what Zuul raises
                outcome.append(e)
        getter = threading.Thread(target=get, daemon=True)
        getter.start()
        # Free the slot only once Zuul's client has really been refused.
        assert wait_for(lambda: refusals), 'the client was never refused'
    finally:
        holder.close()
    getter.join(15)

    assert not getter.is_alive()
    assert not isinstance(outcome[0], Exception), repr(outcome[0])
    assert outcome[0]['iid'] == mr['iid']
    assert len(refusals) >= 1
