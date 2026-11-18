"""git over HTTP is read-only, however a push is spelled.

git-stream refuses receive-pack by looking for the text "receive-pack" in
the raw request path and query (server.py:_git).  The real push protection
underneath is `git http-backend`, which serves receive-pack only when the
CGI variable REMOTE_USER is set; git-stream builds the CGI environment from
its own (os.environ), so a request cannot set it.

These tests ask two different questions:
  * can a push succeed at all (with and without REMOTE_USER)?
  * does git-stream's own check catch the encoded spellings, or does it
    lean entirely on http-backend?
"""

import subprocess
import urllib.error
import urllib.request

import pytest

ADVERTISING = '# service=git-receive-pack'

PLAIN = 'service=git-receive-pack'
# Spellings http-backend decodes into the real service name.
ENCODED = [
    'service=git%2Dreceive%2Dpack',
    'service=git%2dreceive%2dpack',
    'service=git-receive%2Dpack',
    'x=1&service=git%2Dreceive%2Dpack',
]
# Not a service name to http-backend, so it refuses this one itself.
DOUBLE_ENCODED = 'service=git%252Dreceive%252Dpack'
SERVICE_SPELLINGS = [PLAIN] + ENCODED + [DOUBLE_ENCODED]

# strict: once the check is fixed these start passing, which fails the run
# until the marker is removed (see docs/backlog.md, section 1).
KNOWN_GAP = pytest.mark.xfail(
    strict=True, reason='known gap: receive-pack check matches raw text, '
    'see backlog')


def known_gap(*queries):
    return [pytest.param(q, marks=KNOWN_GAP) for q in queries]

PUSH_PATHS = [
    '/bundles.git/git-receive-pack',
    '/bundles.git/git%2Dreceive%2Dpack',
    '/bundles.git/git-receive-%70ack',
    '/bundles.git/Git-Receive-Pack',
    '/bundles.git//git-receive-pack',
    '/bundles.git/git-receive-pack/',
    '/bundles.git/git-upload-pack?x=git-receive-pack',
]


def call(forge, method, path, data=None, ctype=None):
    req = urllib.request.Request(
        forge.url + path, data=data, method=method,
        headers={'Content-Type': ctype} if ctype else {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def git(*args, cwd, **kw):
    return subprocess.run(['git'] + list(args), cwd=cwd, check=True,
                          capture_output=True, **kw).stdout


@pytest.fixture
def attempt(forge, tmp_path):
    """A complete, valid receive-pack request that would move master to a new
    commit, and a way to tell whether master moved."""
    repo = forge.forge.repo(forge.forge.project('bundles'))
    base = repo.resolve('refs/heads/master')
    clone = tmp_path / 'clone'
    git('clone', '-q', forge.url + '/bundles.git', str(clone), cwd=tmp_path)
    (clone / 'pushed.txt').write_text('not allowed\n')
    git('add', '.', cwd=clone)
    git('-c', 'user.name=a', '-c', 'user.email=a@example.com', 'commit', '-q',
        '-m', 'pushed', cwd=clone)
    new = git('rev-parse', 'HEAD', cwd=clone).decode().strip()
    pack = subprocess.run(
        ['git', 'pack-objects', '--stdout', '--revs'], cwd=clone,
        input=('%s\n^%s\n' % (new, base)).encode(), check=True,
        capture_output=True).stdout
    command = ('%s %s refs/heads/master\0 report-status\n' % (base, new)
               ).encode()
    body = b'%04x' % (len(command) + 4) + command + b'0000' + pack

    class Attempt:
        request_body = body
        content_type = 'application/x-git-receive-pack-request'
        clone_dir = clone

        @staticmethod
        def moved():
            return repo.resolve('refs/heads/master') != base
    return Attempt


# -- can a push succeed? -------------------------------------------------------

@pytest.mark.parametrize('remote_user', [None, 'attacker'],
                         ids=['no-REMOTE_USER', 'REMOTE_USER-set'])
def test_no_push_succeeds_by_any_path(forge, attempt, monkeypatch,
                                      remote_user):
    if remote_user is None:
        monkeypatch.delenv('REMOTE_USER', raising=False)
    else:
        monkeypatch.setenv('REMOTE_USER', remote_user)
    for path in PUSH_PATHS:
        status, _ = call(forge, 'POST', path, attempt.request_body,
                         attempt.content_type)
        assert status in (403, 404), (path, status)
        assert not attempt.moved(), path


@pytest.mark.parametrize('remote_user', [None, 'attacker'],
                         ids=['no-REMOTE_USER', 'REMOTE_USER-set'])
def test_a_real_git_push_is_refused(forge, attempt, monkeypatch,
                                    remote_user):
    if remote_user is None:
        monkeypatch.delenv('REMOTE_USER', raising=False)
    else:
        monkeypatch.setenv('REMOTE_USER', remote_user)
    pushed = subprocess.run(
        ['git', 'push', forge.url + '/bundles.git', 'HEAD:refs/heads/master'],
        cwd=attempt.clone_dir, capture_output=True, text=True)
    assert pushed.returncode != 0
    assert not attempt.moved()


def test_a_request_cannot_set_remote_user(forge, attempt, monkeypatch):
    """REMOTE_USER comes from the server's environment, never from headers."""
    monkeypatch.delenv('REMOTE_USER', raising=False)
    for headers in ({'REMOTE_USER': 'attacker'}, {'Remote-User': 'attacker'},
                    {'X-Remote-User': 'attacker'}, {'HTTP_REMOTE_USER': 'x'}):
        req = urllib.request.Request(
            forge.url + '/bundles.git/info/refs?service=git%2Dreceive%2Dpack',
            headers=headers)
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 403, headers


# -- does git-stream's own check catch the spellings? -------------------------

@pytest.mark.parametrize('query', SERVICE_SPELLINGS)
def test_receive_pack_is_not_advertised_without_remote_user(
        forge, monkeypatch, query):
    monkeypatch.delenv('REMOTE_USER', raising=False)
    status, body = call(forge, 'GET', '/bundles.git/info/refs?' + query)
    assert status == 403
    assert ADVERTISING.encode() not in body


@pytest.mark.parametrize(
    'query', [PLAIN, DOUBLE_ENCODED] + known_gap(*ENCODED))
def test_receive_pack_is_not_advertised_even_if_remote_user_is_set(
        forge, monkeypatch, query):
    """The property that matters: with REMOTE_USER set, http-backend would
    serve receive-pack, so only git-stream's own check can stop it."""
    monkeypatch.setenv('REMOTE_USER', 'attacker')
    status, body = call(forge, 'GET', '/bundles.git/info/refs?' + query)
    assert status == 403
    assert ADVERTISING.encode() not in body


@pytest.mark.parametrize(
    'query', [PLAIN] + known_gap(*ENCODED, DOUBLE_ENCODED))
def test_gitstreams_own_check_refuses_every_spelling(forge, monkeypatch,
                                                    query):
    """The refusal should come from git-stream (it says "read-only"), not
    depend on what http-backend happens to do with the spelling."""
    monkeypatch.delenv('REMOTE_USER', raising=False)
    status, body = call(forge, 'GET', '/bundles.git/info/refs?' + query)
    assert status == 403
    assert b'read-only' in body


def test_upload_pack_is_still_served(forge):
    status, body = call(forge, 'GET',
                        '/bundles.git/info/refs?service=git-upload-pack')
    assert status == 200 and body.startswith(b'001e# service=git-upload-pack')

