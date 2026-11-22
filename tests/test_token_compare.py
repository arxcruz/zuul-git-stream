"""Tokens are compared in constant time, against every configured token.

`==` and a dict lookup both leave early or take a path that depends on the
secret, so the check goes through hmac.compare_digest for each configured
token and does not stop at the first match.  (That wall-clock time is
independent of the token is not testable here; these pin the mechanism.)
"""

import hmac
import urllib.error
import urllib.request

import pytest

STREAM = {'a': 'tok-a', 'b': 'tok-b', 'c': 'tok-c'}
API = {'t-a': 'ua', 't-b': 'ub', 't-c': 'uc'}


@pytest.fixture
def secure(make_forge):
    return make_forge(stream_tokens=STREAM, users=API, insecure=False).forge


@pytest.fixture
def digests(monkeypatch):
    """Every hmac.compare_digest call, as (a, b) pairs, still real."""
    calls = []
    real = hmac.compare_digest

    def recording(a, b):
        calls.append((a, b))
        return real(a, b)
    monkeypatch.setattr(hmac, 'compare_digest', recording)
    return calls


def test_stream_token_is_compared_with_every_configured_token(secure, digests):
    # the first user's token matches, and the other two are still compared
    assert secure.stream_user_for_token('tok-a') == 'a'
    assert len(digests) == len(STREAM)
    assert all(isinstance(x, bytes) for pair in digests for x in pair)


def test_api_token_is_compared_with_every_configured_token(secure, digests):
    assert secure.user_for_token('t-a') == 'ua'
    assert len(digests) == len(API)
    assert all(isinstance(x, bytes) for pair in digests for x in pair)


def test_an_unknown_token_is_compared_with_every_configured_token(
        secure, digests):
    assert secure.stream_user_for_token('nope') is None
    assert secure.user_for_token('nope') is None
    assert len(digests) == len(STREAM) + len(API)


@pytest.mark.parametrize('token', [
    '', 'tok-', 'tok-ax', 'TOK-A', 'tok-a ', 'tok-a\x00'])
def test_only_the_exact_token_matches(secure, token):
    assert secure.stream_user_for_token(token) is None
    assert secure.user_for_token(token) is None


@pytest.mark.parametrize('token', ['tök', 'é', '中文'])
def test_a_non_ascii_token_is_unknown_not_an_error(secure, token):
    # hmac.compare_digest raises TypeError on a non-ASCII str
    assert secure.stream_user_for_token(token) is None
    assert secure.user_for_token(token) is None


def test_the_first_user_wins_a_duplicated_stream_token(make_forge):
    # Refusing duplicates is a separate backlog item; until then the first
    # one in the map keeps winning, and scanning everything must not change
    # that.
    forge = make_forge(stream_tokens={'first': 'same', 'second': 'same'},
                       insecure=False).forge
    assert forge.stream_user_for_token('same') == 'first'


def test_no_tokens_configured_still_means_refused_or_insecure(make_forge):
    closed = make_forge(stream_tokens={}, users={}, insecure=False).forge
    assert closed.stream_user_for_token('x') is None
    assert closed.user_for_token('x') is None
    open_ = make_forge(stream_tokens={}, users={}, insecure=True).forge
    assert open_.stream_user_for_token(None) == 'git-stream'
    assert open_.user_for_token('x') == 'zuul'


def request_status(forge, path, headers, data=b'{}'):
    req = urllib.request.Request(forge.url + path, data=data, method='POST',
                                 headers=dict(headers, **{
                                     'Content-Type': 'application/json'}))
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.parametrize('header', ['X-Stream-Token', 'Authorization'])
def test_a_non_ascii_stream_token_header_is_401(make_forge, header):
    forge = make_forge(stream_tokens=STREAM, insecure=False)
    value = 'Bearer tök' if header == 'Authorization' else 'tök'
    assert request_status(forge, '/api/stream', {header: value}) == 401


@pytest.mark.parametrize('header', ['PRIVATE-TOKEN', 'Authorization'])
def test_a_non_ascii_api_token_header_is_401(make_forge, header):
    forge = make_forge(users=API, stream_tokens=STREAM, insecure=False)
    forge.send({'action': 'create_project', 'project': 'bundles'}, token='tok-a')
    value = 'Bearer tök' if header == 'Authorization' else 'tök'
    path = '/api/v4/projects/bundles/merge_requests/1/notes'  # needs auth
    assert request_status(forge, path, {header: value}) == 401
