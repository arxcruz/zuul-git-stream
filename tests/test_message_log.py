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


# -- a host without sqlite3: the JSON fallback store ------------------------

@pytest.fixture
def json_forge(make_forge, monkeypatch):
    """A forge on the JSON fallback store, as on a host with no sqlite3."""
    import os
    from git_stream import forge as forge_module
    monkeypatch.setattr(
        forge_module, '_load_store',
        lambda data_dir: forge_module.JsonStore(
            os.path.join(data_dir, 'state.json')))
    running = make_forge()
    running.send({'action': 'create_project', 'project': 'bundles'})
    return running


def status_of(request):
    try:
        with urllib.request.urlopen(request) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_without_sqlite_the_message_log_answers_501(json_forge):
    request = urllib.request.Request(json_forge.url + '/api/stream/messages')
    assert status_of(request) == 501


def test_without_sqlite_data_is_a_501_and_creates_nothing(json_forge):
    for msg in ({'action': 'open_mr', 'project': 'bundles', 'title': 'T',
                 'data': {'a': 1}},
                {'action': 'push', 'project': 'bundles', 'data': {'a': 1}}):
        assert refused(json_forge, msg) == 501
    assert json_forge.forge.state['projects']['bundles']['mrs'] == {}
    json_forge.send({'action': 'open_mr', 'project': 'bundles',
                     'title': 'T'})  # no data: fine without sqlite
    for path in ('/api/v4/projects/bundles/data',
                 '/api/v4/projects/bundles/merge_requests/1/data'):
        assert status_of(urllib.request.Request(json_forge.url + path)) == 501


def test_without_sqlite_the_rest_of_the_forge_still_works(json_forge):
    mr = json_forge.send({'action': 'open_mr', 'project': 'bundles',
                          'title': 'T', 'key': 'k'})
    json_forge.send({'action': 'label', 'project': 'bundles', 'mr': 'k',
                     'add': ['x']})
    json_forge.send({'action': 'comment', 'project': 'bundles', 'mr': 'k',
                     'note': 'hi'})
    merged = json_forge.send({'action': 'merge', 'project': 'bundles',
                              'mr': mr['iid']})
    assert merged['state'] == 'merged'


def test_the_log_is_written_before_zuul_can_hear_about_the_change(
        make_forge):
    """The row exists, as "received", when the webhook goes out."""
    seen = []
    forge = make_forge()
    inner = forge.forge.emit

    def emit(payload):
        seen.append([m['outcome'] for m in forge.forge.messages(
            {'action': 'open_mr'})])
        inner(payload)
    forge.forge.emit = emit
    forge.send({'action': 'create_project', 'project': 'bundles'})
    open_mr(forge, 'b-1')
    assert seen == [['received']]
    assert [m['outcome'] for m in forge.forge.messages(
        {'action': 'open_mr'})] == ['applied']
