"""Secure by default.

With no [api_tokens], REST writes are refused instead of accepting any
token as "zuul"; with no [stream] tokens, POST /api/stream is refused
instead of open.  `[server] insecure = true` turns both back on for demos
and throwaway setups, and never weakens tokens that are configured.
"""

import json
import urllib.error
import urllib.request

import pytest

from git_stream.forge import Forge

NOTES = '/api/v4/projects/bundles/merge_requests/1/notes'
STREAM_NOTE = {'action': 'comment', 'project': 'bundles', 'mr': 'k',
               'note': 'hi'}


def seeded(make_forge, **settings):
    """A forge with a project and an MR, made in-process because the stream
    itself may be closed to us."""
    forge = make_forge(**settings)
    forge.forge.create_project('bundles', {'README.md': 'x\n'})
    forge.forge.open_mr('bundles', 'T', {'a.yaml': 'a: 1\n'}, 'builder',
                        key='k')
    return forge


def request(forge, path, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        forge.url + path, data=data, method='GET' if body is None else 'POST',
        headers=dict({'Content-Type': 'application/json'}, **(headers or {})))
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def note(forge, headers):
    return request(forge, NOTES, {'body': 'hi'}, headers)


def notes_on(forge):
    return request(forge, NOTES + '?sort=asc')[1]


# -- the defaults ------------------------------------------------------------

def test_forge_is_secure_unless_told_otherwise(tmp_path):
    forge = Forge(str(tmp_path / 'data'), 'http://x')
    assert forge.user_for_token('anything') is None
    assert forge.stream_user_for_token(None) is None
    assert forge.stream_user_for_token('anything') is None
    forge.close()


# -- API writes --------------------------------------------------------------

@pytest.mark.parametrize('header', [
    {'Authorization': 'Bearer anything'}, {'PRIVATE-TOKEN': 'anything'}])
def test_api_writes_are_refused_when_no_tokens_are_configured(
        make_forge, header):
    forge = seeded(make_forge, users={}, insecure=False)
    status, body = note(forge, header)
    assert status == 401
    assert 'no API tokens are configured' in body['message']
    assert '[api_tokens]' in body['message']  # says what to do
    assert notes_on(forge) == []
    assert request(forge, '/api/v4/projects/bundles')[0] == 200  # reads work


def test_configured_api_tokens_work_when_secure(make_forge):
    forge = seeded(make_forge, insecure=False)  # the usual one token
    assert note(forge, {'Authorization': 'Bearer secret-api-token'})[0] == 201
    status, body = note(forge, {'Authorization': 'Bearer wrong'})
    assert status == 401
    assert 'no API tokens' not in body['message']


# -- the stream --------------------------------------------------------------

@pytest.mark.parametrize('headers', [
    {}, {'X-Stream-Token': 'anything'}, {'Authorization': 'Bearer anything'}])
def test_stream_is_refused_when_no_tokens_are_configured(
        make_forge, headers):
    forge = seeded(make_forge, insecure=False)
    status, body = request(forge, '/api/stream', STREAM_NOTE, headers)
    assert status == 401
    assert 'no stream tokens are configured' in body['message']
    assert '[stream]' in body['message']
    assert notes_on(forge) == []


def test_stream_with_tokens_works_when_secure(make_forge):
    forge = seeded(make_forge, insecure=False,
                   stream_tokens={'builder': 'tok'})
    assert request(forge, '/api/stream', STREAM_NOTE)[0] == 401
    assert request(forge, '/api/stream', STREAM_NOTE,
                   {'X-Stream-Token': 'tok'})[0] == 200
    assert len(notes_on(forge)) == 1


# -- the explicit opt-in -----------------------------------------------------

def test_insecure_restores_any_token_and_the_open_stream(make_forge):
    forge = seeded(make_forge, users={}, insecure=True)
    assert note(forge, {'Authorization': 'Bearer anything'})[0] == 201
    assert request(forge, '/api/stream', STREAM_NOTE)[0] == 200
    assert [n['author']['username'] for n in notes_on(forge)] == [
        'zuul', 'git-stream']


def test_insecure_does_not_weaken_configured_tokens(make_forge):
    forge = seeded(make_forge, insecure=True,
                   stream_tokens={'builder': 'tok'})
    assert note(forge, {'Authorization': 'Bearer wrong'})[0] == 401
    assert request(forge, '/api/stream', STREAM_NOTE)[0] == 401
    assert request(forge, '/api/stream', STREAM_NOTE,
                   {'X-Stream-Token': 'wrong'})[0] == 401
    assert notes_on(forge) == []
