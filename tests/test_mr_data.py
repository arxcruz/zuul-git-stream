"""Data attached to merge requests and pushes.

A sender sends `data`, not files; every change of it is a new commit (so Zuul
sees a changed MR), older versions stay readable, and a merge or a push to
the default branch makes it the project's current data.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from git_stream import stream
from git_stream.forge import Forge


def get(forge, path, **query):
    url = forge.url + path + ('?' + urllib.parse.urlencode(query)
                              if query else '')
    with urllib.request.urlopen(url) as r:
        return json.load(r)


def missing(forge, path, **query):
    with pytest.raises(urllib.error.HTTPError) as e:
        get(forge, path, **query)
    return e.value.code


def rejected(forge, msg):
    with pytest.raises(urllib.error.HTTPError) as e:
        forge.send(msg)
    return e.value.code, json.load(e.value)['message']


def open_mr(forge, key='b-1', data=None, **extra):
    msg = {'action': 'open_mr', 'project': 'bundles', 'title': 'T',
           'key': key, 'data': {'id': key} if data is None else data}
    msg.update(extra)
    return forge.send(msg)


def mr_data(forge, iid, **query):
    return get(forge, '/api/v4/projects/bundles/merge_requests/%s/data'
               % iid, **query)


def test_an_mr_opens_from_data_alone(forge):
    mr = open_mr(forge, data={'id': 'b-1', 'flaky': 0.3})
    assert mr['state'] == 'opened'
    assert mr['sha'] and mr['diff_refs']['head_sha'] == mr['sha']
    assert mr_data(forge, mr['iid']) == {
        'project': 'bundles', 'mr': mr['iid'], 'sha': mr['sha'],
        'data': {'id': 'b-1', 'flaky': 0.3}}
    assert [h['object_attributes']['action']
            for h in forge.hooks_of('merge_request')] == ['open']


def test_two_mrs_opened_at_once_get_different_shas(forge):
    shas = {open_mr(forge, key='k%d' % n, data={'same': 1})['sha']
            for n in range(5)}
    assert len(shas) == 5


def test_changed_data_is_a_new_version_and_the_old_one_stays(forge):
    first = open_mr(forge, data={'v': 1})
    second = forge.send({'action': 'update_mr', 'project': 'bundles',
                         'mr': 'b-1', 'data': {'v': 2}})
    assert second['sha'] != first['sha']
    assert mr_data(forge, 1)['data'] == {'v': 2}
    assert mr_data(forge, 1, sha=first['sha'])['data'] == {'v': 1}
    changed = forge.hooks_of('merge_request')[-1]['object_attributes']
    assert changed['action'] == 'update'
    assert changed['oldrev'] == first['sha']
    assert changed['last_commit']['id'] == second['sha']


def test_the_same_data_again_changes_nothing(forge):
    first = open_mr(forge, data={'v': 1})
    hooks = len(forge.hooks)
    again = open_mr(forge, data={'v': 1})  # a redelivered announcement
    same = forge.send({'action': 'update_mr', 'project': 'bundles',
                       'mr': 'b-1', 'data': {'v': 1}})
    assert again['sha'] == same['sha'] == first['sha']
    assert len(forge.hooks) == hooks


def test_an_unknown_version_has_no_data(forge):
    open_mr(forge)
    assert missing(forge, '/api/v4/projects/bundles/merge_requests/1/data',
                   sha='0' * 40) == 404
    assert missing(forge, '/api/v4/projects/bundles/merge_requests/9/data'
                   ) == 404


def test_an_mr_without_data_has_none_to_read(forge):
    forge.forge.open_mr('bundles', 'T', {'a': 'b'}, 'me')
    assert missing(forge, '/api/v4/projects/bundles/merge_requests/1/data'
                   ) == 404


def test_a_files_only_update_keeps_the_data(forge):
    open_mr(forge, data={'v': 1})
    new = forge.forge.update_mr('bundles', 'b-1', 'me', files={'x': '1'})
    assert mr_data(forge, 1)['sha'] == new['sha']
    assert mr_data(forge, 1)['data'] == {'v': 1}


def test_merging_makes_the_data_the_projects_current_data(forge):
    assert missing(forge, '/api/v4/projects/bundles/data') == 404
    open_mr(forge, data={'broken': False})
    assert missing(forge, '/api/v4/projects/bundles/data') == 404
    merged = forge.send({'action': 'merge', 'project': 'bundles', 'mr': 'b-1'})
    current = get(forge, '/api/v4/projects/bundles/data')
    assert current['data'] == {'broken': False}
    assert current['sha'] == merged['merge_commit_sha']
    assert current['branch'] == 'master'


def test_a_push_of_data_is_the_projects_current_data(forge):
    forge.send({'action': 'push', 'project': 'bundles',
                'data': {'broken': True}})
    first = get(forge, '/api/v4/projects/bundles/data')
    assert first['data'] == {'broken': True}
    forge.send({'action': 'push', 'project': 'bundles',
                'data': {'broken': False}})
    assert get(forge, '/api/v4/projects/bundles/data')['data'] == {
        'broken': False}
    assert get(forge, '/api/v4/projects/bundles/data', sha=first['sha'])[
        'data'] == {'broken': True}
    assert [h['object_kind'] for h in forge.hooks if
            h['object_kind'] == 'push'][-2:] == ['push', 'push']


def test_a_push_to_another_branch_is_not_the_current_data(forge):
    forge.send({'action': 'push', 'project': 'bundles', 'branch': 'stable',
                'start': 'master', 'data': {'x': 1}})
    assert missing(forge, '/api/v4/projects/bundles/data') == 404


def test_a_push_needs_data(forge):
    code, message = rejected(forge, {'action': 'push', 'project': 'bundles'})
    assert code == 400 and 'data' in message


@pytest.mark.parametrize('data', [[1], 'x', 5, True])
def test_data_must_be_an_object(forge, data):
    code, message = rejected(forge, {'action': 'open_mr',
                                     'project': 'bundles', 'title': 'T',
                                     'data': data})
    assert code == 400 and 'JSON object' in message
    assert forge.forge.state['projects']['bundles']['mrs'] == {}


def test_data_is_bounded_in_size_and_depth(forge):
    deep = {}
    node = deep
    for _ in range(stream.DATA_MAX_DEPTH + 1):
        node['n'] = {}
        node = node['n']
    big = {'blob': 'x' * stream.DATA_MAX_BYTES}
    for data, what in ((deep, 'deep'), (big, 'larger')):
        code, message = rejected(forge, {
            'action': 'open_mr', 'project': 'bundles', 'title': 'T',
            'data': data})
        assert code == 400 and what in message, message
    assert forge.forge.state['projects']['bundles']['mrs'] == {}
    # The limits themselves are fine.
    ok = {}
    node = ok
    for _ in range(stream.DATA_MAX_DEPTH - 1):
        node['n'] = {}
        node = node['n']
    assert open_mr(forge, data=ok)['state'] == 'opened'


def test_data_survives_a_restart(forge):
    mr = open_mr(forge, data={'v': 1})
    forge.send({'action': 'merge', 'project': 'bundles', 'mr': 'b-1'})
    again = Forge(forge.forge.data_dir, forge.url)
    try:
        assert again.mr_data('bundles', 1)['data'] == {'v': 1}
        assert again.mr_data('bundles', 1, mr['sha'])['data'] == {'v': 1}
        assert again.project_data('bundles')['data'] == {'v': 1}
    finally:
        again.close()


def test_the_data_is_in_the_message_log(forge):
    open_mr(forge, data={'v': 1})
    row = get_log(forge)['messages'][0]
    assert row['body']['data'] == {'v': 1}


def get_log(forge):
    return get(forge, '/api/stream/messages')
