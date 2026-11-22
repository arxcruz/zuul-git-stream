"""A body nested thousands of levels deep is a 400, never a 500.

Python's JSON parser and encoder give up with RecursionError past about a
thousand levels; each place that reads or walks a body must turn that into
a plain 4xx (the security rule: malformed input is never a 500 or a hang).
"""

import json
import urllib.error
import urllib.request

import pytest

DEPTHS = [50, 900, 5000, 100000]


def post(forge, body):
    req = urllib.request.Request(
        forge.url + '/api/stream', data=body.encode(), method='POST',
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


@pytest.mark.parametrize('depth', DEPTHS)
def test_deeply_nested_data_is_a_400(forge, depth):
    nested = '[' * depth + ']' * depth
    status, reply = post(forge, '{"action": "open_mr", "project": "bundles",'
                         '"title": "t", "data": {"a": %s}}' % nested)
    assert status == 400, reply
    assert reply['message'] in ('"data" is nested more than 8 levels deep',
                                'invalid JSON body')
    assert forge.forge.state['projects']['bundles']['mrs'] == {}


@pytest.mark.parametrize('depth', DEPTHS)
def test_a_deeply_nested_body_is_never_a_500(forge, depth):
    nested = '[' * depth + ']' * depth
    for body in (nested, '{"action": "comment", "project": "bundles",'
                 '"mr": 1, "note": %s}' % nested):
        status, _ = post(forge, body)
        assert status < 500, (depth, body[:40], status)


@pytest.mark.parametrize('depth', DEPTHS)
def test_a_deeply_nested_form_body_is_never_a_500(forge, depth):
    nested = '[' * depth + ']' * depth
    req = urllib.request.Request(
        forge.url + '/api/v4/projects/bundles/merge_requests/1/notes',
        data=('{"body": %s}' % nested).encode(), method='POST',
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Bearer secret-api-token'})
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        assert e.code < 500, e.code


def test_a_message_just_inside_the_limit_is_still_logged(forge):
    data = {}
    node = data
    for _ in range(7):
        node['n'] = {}
        node = node['n']
    forge.send({'action': 'open_mr', 'project': 'bundles', 'title': 't',
                'data': data})
    row = forge.forge.messages({'action': 'open_mr'})[0]
    assert row['body']['data'] == data
