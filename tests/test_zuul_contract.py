"""The forge, as seen by Zuul's own GitLab driver code.

These tests drive Zuul's GitlabAPIClient against the forge and feed the
webhooks it emits to Zuul's GitlabEventConnector handlers, so a change
that Zuul would not understand fails here rather than in a demo.
"""

import re
import types

import pytest

from conftest import API_TOKEN

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


@pytest.fixture
def connector(forge):
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


def open_mr(forge, **kw):
    msg = {'action': 'open_mr', 'project': 'bundles', 'title': 'Bundle b-1',
           'key': 'b-1', 'files': {'bundle.yaml': 'id: b-1\n'}}
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


def test_branches_as_read_by_zuul(forge, client):
    forge.send({'action': 'push', 'project': 'bundles', 'branch': 'stable',
                'files': {'x': '1'}})
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
    again = open_mr(forge, files={'bundle.yaml': 'id: b-1\nv: 2\n'})
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
    res = forge.send({'action': 'push', 'project': 'bundles',
                      'files': {'a.txt': 'a', 'README.md': None},
                      'message': 'Add a, drop README'})
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
