"""The message log: every stream message is recorded, with its outcome.

GET /api/stream/messages reads it (newest first, filtered, paged) with the
same token as the stream.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from git_stream.forge import Forge, MESSAGE_BODY_MAX

TOKENS = {'build-system': 'tok-b', 'release-manager': 'tok-r'}


def log(forge, token=None, **query):
    headers = {'X-Stream-Token': token} if token else {}
    url = forge.url + '/api/stream/messages'
    if query:
        url += '?' + urllib.parse.urlencode(query)
    with urllib.request.urlopen(urllib.request.Request(
            url, headers=headers)) as r:
        return json.load(r)


def open_mr(forge, key, **extra):
    return forge.send(dict({'action': 'open_mr', 'project': 'bundles',
                            'title': 'T', 'key': key,
                            'data': {'a': key}}, **extra))


def refused(forge, msg, token=None):
    with pytest.raises(urllib.error.HTTPError) as e:
        forge.send(msg, token=token)
    return e.value.code


def test_an_applied_message_is_recorded_with_its_details(forge):
    open_mr(forge, 'b-1', id='m-1')
    row = log(forge)['messages'][0]
    assert row['action'] == 'open_mr'
    assert row['project'] == 'bundles'
    assert row['key'] == 'b-1'
    assert row['message_id'] == 'm-1'
    assert row['sender'] == 'git-stream'
    assert row['outcome'] == 'applied'
    assert row['status'] == 200
    assert row['time']
    assert row['body']['title'] == 'T'


def test_the_log_is_newest_first(forge):
    open_mr(forge, 'b-1')
    open_mr(forge, 'b-2')
    keys = [m['key'] for m in log(forge)['messages']]
    assert keys[:2] == ['b-2', 'b-1']


def test_a_resent_message_is_recorded_as_a_duplicate(forge):
    open_mr(forge, 'b-1', id='m-1')
    open_mr(forge, 'b-1', id='m-1')
    outcomes = [m['outcome'] for m in log(forge, key='b-1')['messages']]
    assert outcomes == ['duplicate', 'applied']


def test_a_rejected_message_is_recorded_and_changes_nothing(forge):
    before = len(forge.forge.state['projects']['bundles']['mrs'])
    assert refused(forge, {'action': 'open_mr', 'project': 'bundles'}) == 400
    assert refused(forge, {'action': 'nope', 'project': 'bundles'}) == 400
    rows = log(forge, outcome='rejected')['messages']
    assert [r['action'] for r in rows] == ['nope', 'open_mr']
    assert rows[0]['status'] == 400
    assert 'unknown action' in rows[0]['detail']
    assert len(forge.forge.state['projects']['bundles']['mrs']) == before


def test_a_message_that_is_not_an_object_is_recorded(forge):
    assert refused(forge, 5) == 400
    row = log(forge)['messages'][0]
    assert row['outcome'] == 'rejected'
    assert row['action'] is None
    assert row['body'] == {'message': 5}


def test_a_refused_message_is_recorded_as_refused(make_forge):
    forge = make_forge(stream_tokens=TOKENS)
    forge.send({'action': 'create_project', 'project': 'bundles'},
               token='tok-b')
    status = refused(forge, {'action': 'comment', 'project': 'bundles',
                             'mr': 1, 'note': 'x', 'author': 'release-manager'},
                     token='tok-b')
    assert status == 403
    row = log(forge, token='tok-b', outcome='refused')['messages'][0]
    assert row['sender'] == 'build-system'
    assert row['status'] == 403


def test_the_log_needs_the_stream_token(make_forge):
    forge = make_forge(stream_tokens=TOKENS)
    with pytest.raises(urllib.error.HTTPError) as e:
        log(forge)
    assert e.value.code == 401
    with pytest.raises(urllib.error.HTTPError) as e:
        log(forge, token='wrong')
    assert e.value.code == 401
    assert log(forge, token='tok-r')['messages'] == []


def test_the_log_filters(forge):
    open_mr(forge, 'b-1')
    open_mr(forge, 'b-2')
    forge.send({'action': 'comment', 'project': 'bundles', 'mr': 'b-1',
                'note': 'hi'})
    assert [m['action'] for m in log(forge, action='comment')['messages']] == [
        'comment']
    assert {m['key'] for m in log(forge, key='b-2')['messages']} == {'b-2'}
    assert log(forge, project='other')['messages'] == []
    everything = log(forge)['messages']
    assert log(forge, since='9999')['messages'] == []
    assert log(forge, until='0001')['messages'] == []
    assert log(forge, since='0001', until='9999')['messages'] == everything


def test_the_log_is_paged_newest_first(forge):
    for n in range(5):
        open_mr(forge, 'b-%d' % n)
    seen = []
    page = log(forge, limit=2, action='open_mr')
    while True:
        assert len(page['messages']) <= 2
        seen += [m['key'] for m in page['messages']]
        if page['next'] is None:
            break
        page = log(forge, limit=2, action='open_mr', before=page['next'])
    assert seen == ['b-4', 'b-3', 'b-2', 'b-1', 'b-0']


def test_a_bad_page_parameter_is_a_400(forge):
    for query in ({'limit': 'x'}, {'limit': '0'}, {'before': '-1'}):
        with pytest.raises(urllib.error.HTTPError) as e:
            log(forge, **query)
        assert e.value.code == 400


def test_a_huge_body_is_logged_truncated(forge):
    open_mr(forge, 'b-big', description='x' * (MESSAGE_BODY_MAX + 1))
    body = log(forge, key='b-big')['messages'][0]['body']
    assert body['truncated'] is True
    assert body['size'] > MESSAGE_BODY_MAX


def test_the_log_survives_a_restart(forge):
    open_mr(forge, 'b-1')
    again = Forge(forge.forge.data_dir, forge.url)
    try:
        rows = again.messages({'key': 'b-1'})
        assert [r['outcome'] for r in rows] == ['applied']
    finally:
        again.close()


def test_the_state_mirror_does_not_rewrite_the_log(forge):
    open_mr(forge, 'b-1')
    first = log(forge, key='b-1')['messages'][0]
    forge.send({'action': 'comment', 'project': 'bundles', 'mr': 'b-1',
                'note': 'x'})  # saves the state mirror
    assert log(forge, key='b-1')['messages'][-1] == first
