"""Who a stream message acts as.

With stream tokens configured, a sender acts as the user its token names
and may not claim another; with an open stream, any author is allowed
except the names the retry policy trusts as Zuul's own (`report_users`),
because a forged "Build failed." from "zuul" would drive the retry and
escalation path.
"""

import json
import urllib.error
import urllib.request

import pytest

TOKENS = {'build-system': 'tok-b', 'release-manager': 'tok-r'}
MR = '/api/v4/projects/bundles/merge_requests/1'


def get(forge, path):
    with urllib.request.urlopen(forge.url + path) as r:
        return json.load(r)


def authors(forge):
    return [n['author']['username'] for n in
            sorted(get(forge, MR + '/notes?sort=asc'), key=lambda n: n['id'])]


def refused(forge, msg, token=None):
    """Send msg, expect an HTTP error, return (status, message)."""
    with pytest.raises(urllib.error.HTTPError) as e:
        forge.send(msg, token=token)
    return e.value.code, json.load(e.value)['message']


def seed(forge, token=None):
    forge.send({'action': 'create_project', 'project': 'bundles',
                'files': {'README.md': 'x\n'}}, token=token)
    forge.send({'action': 'open_mr', 'project': 'bundles', 'title': 'T',
                'key': 'k', 'files': {'a.yaml': 'a: 1\n'}}, token=token)


@pytest.fixture
def authed(make_forge):
    forge = make_forge(stream_tokens=TOKENS)
    seed(forge, token='tok-b')
    return forge


@pytest.fixture
def open_stream(make_forge):
    forge = make_forge(report_users=('zuul', 'zuul-ci'))
    seed(forge)
    return forge


# -- stream tokens configured ------------------------------------------------

def test_a_token_acts_as_its_user_by_default(authed):
    assert get(authed, MR)['author']['username'] == 'build-system'
    authed.send({'action': 'comment', 'project': 'bundles', 'mr': 'k',
                 'note': 'one'}, token='tok-b')
    authed.send({'action': 'comment', 'project': 'bundles', 'mr': 'k',
                 'note': 'two'}, token='tok-r')
    assert authors(authed) == ['build-system', 'release-manager']


def test_a_token_may_name_itself_as_author(authed):
    authed.send({'action': 'comment', 'project': 'bundles', 'mr': 'k',
                 'note': 'hi', 'author': 'build-system'}, token='tok-b')
    assert authors(authed) == ['build-system']


ACTIONS_WITH_AN_AUTHOR = {
    'comment': {'action': 'comment', 'project': 'bundles', 'mr': 'k',
                'note': 'Build failed.'},
    'approve': {'action': 'approve', 'project': 'bundles', 'mr': 'k'},
    'label': {'action': 'label', 'project': 'bundles', 'mr': 'k',
              'add': ['x']},
    'close': {'action': 'close', 'project': 'bundles', 'mr': 'k'},
    'push': {'action': 'push', 'project': 'bundles',
             'files': {'b.yaml': 'b: 1\n'}},
    'tag': {'action': 'tag', 'project': 'bundles', 'tag': 'v1'},
    'open_mr': {'action': 'open_mr', 'project': 'bundles', 'title': 'T2',
                'key': 'k2', 'files': {'c.yaml': 'c: 1\n'}},
    'create_project': {'action': 'create_project', 'project': 'other'},
}


@pytest.mark.parametrize('claimed', ['zuul', 'release-manager'])
@pytest.mark.parametrize('name', sorted(ACTIONS_WITH_AN_AUTHOR))
def test_a_token_cannot_act_as_someone_else(authed, name, claimed):
    msg = dict(ACTIONS_WITH_AN_AUTHOR[name], author=claimed)
    hooks_before = len(authed.hooks)
    code, message = refused(authed, msg, token='tok-b')
    assert code == 403, (code, message)
    assert 'may not act as' in message
    assert 'build-system' in message and repr(claimed) in message
    assert len(authed.hooks) == hooks_before  # nothing happened
    assert authors(authed) == []


def test_in_a_batch_only_the_allowed_messages_are_applied(authed):
    note = {'action': 'comment', 'project': 'bundles', 'mr': 'k'}
    results = authed.send([
        dict(note, note='one'),
        dict(note, note='Build failed.', author='zuul'),
        dict(note, note='three', author='build-system')], token='tok-b')
    assert 'error' not in results[0] and 'error' not in results[2]
    assert 'may not act as' in results[1]['error']
    assert authors(authed) == ['build-system', 'build-system']


# -- open stream -------------------------------------------------------------

def test_open_stream_allows_other_authors(open_stream):
    note = {'action': 'comment', 'project': 'bundles', 'mr': 'k'}
    open_stream.send(dict(note, note='1', author='release-manager'))
    open_stream.send(dict(note, note='2', author='ci-tool-dev'))
    open_stream.send(dict(note, note='3'))
    assert authors(open_stream) == ['release-manager', 'ci-tool-dev',
                                    'git-stream']


@pytest.mark.parametrize('name', ['zuul', 'zuul-ci'])
@pytest.mark.parametrize('action', sorted(ACTIONS_WITH_AN_AUTHOR))
def test_open_stream_refuses_the_names_zuul_reports_under(
        open_stream, action, name):
    msg = dict(ACTIONS_WITH_AN_AUTHOR[action], author=name)
    hooks_before = len(open_stream.hooks)
    code, message = refused(open_stream, msg)
    assert code == 403, (code, message)
    assert 'reserved' in message and repr(name) in message
    assert len(open_stream.hooks) == hooks_before
    assert authors(open_stream) == []


def test_without_report_users_every_author_is_allowed(make_forge):
    forge = make_forge()  # nothing configured, nothing reserved
    seed(forge)
    forge.send({'action': 'comment', 'project': 'bundles', 'mr': 'k',
                'note': 'x', 'author': 'zuul'})
    assert authors(forge) == ['zuul']
