import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request

import pytest

from git_stream.forge import Forge, ForgeError
from git_stream.repos import Repo
from conftest import Running, rejected


def git(*args, cwd=None):
    return subprocess.run(['git'] + list(args), cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_clone_and_fetch_mr_over_http(forge, tmp_path):
    mr = forge.send({'action': 'open_mr', 'project': 'bundles',
                     'title': 'Add bundle', 'files': {'bundle.yaml': 'x'}})
    work = tmp_path / 'work'
    git('clone', '-q', forge.url + '/bundles.git', str(work))
    assert (work / 'README.md').read_text() == 'bundles\n'
    # This is what Zuul's merger does to test a merge request.
    git('fetch', '-q', 'origin', mr_ref := 'refs/merge-requests/1/head',
        cwd=work)
    assert git('rev-parse', 'FETCH_HEAD', cwd=work) == mr['sha']
    assert mr_ref in git('ls-remote', 'origin', cwd=work)


def test_git_push_is_refused(forge, tmp_path):
    work = tmp_path / 'work'
    git('clone', '-q', forge.url + '/bundles.git', str(work))
    (work / 'x').write_text('x')
    git('add', 'x', cwd=work)
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'x',
        cwd=work)
    with pytest.raises(subprocess.CalledProcessError):
        git('push', '-q', 'origin', 'HEAD:master', cwd=work)


def test_keys_address_mrs_and_ids_deduplicate(forge):
    first = forge.send({'action': 'open_mr', 'project': 'bundles',
                        'key': 'b-7', 'title': 'b-7', 'id': 'm1',
                        'files': {'bundle.yaml': '1'}})
    dup = forge.send({'action': 'open_mr', 'project': 'bundles',
                      'key': 'b-7', 'title': 'b-7', 'id': 'm1',
                      'files': {'bundle.yaml': '2'}})
    assert dup == {'duplicate': True, 'id': 'm1'}
    note = forge.send({'action': 'comment', 'project': 'bundles',
                       'mr': 'b-7', 'note': 'rerun failed'})
    assert note['noteable_iid'] == first['iid']
    hooks = [h['object_kind'] for h in forge.hooks]
    assert hooks.count('merge_request') == 1


def test_conflicting_mr_cannot_merge(forge):
    a = forge.send({'action': 'open_mr', 'project': 'bundles', 'title': 'a',
                    'files': {'README.md': 'a\n'}})
    b = forge.send({'action': 'open_mr', 'project': 'bundles', 'title': 'b',
                    'files': {'README.md': 'b\n'}})
    forge.send({'action': 'merge', 'project': 'bundles', 'mr': a['iid']})
    with urllib.request.urlopen('%s/api/v4/projects/bundles/merge_requests/%s'
                                % (forge.url, b['iid'])) as r:
        assert json.load(r)['merge_status'] == 'cannot_be_merged'
    with pytest.raises(urllib.error.HTTPError) as e:
        forge.send({'action': 'merge', 'project': 'bundles',
                    'mr': b['iid']})
    assert e.value.code == 406


def test_raw_file(forge):
    forge.send({'action': 'push', 'project': 'bundles',
                'files': {'dir/a.yaml': 'a: 1\n'}})
    url = '%s/api/v4/projects/bundles/repository/files/dir%%2Fa.yaml/raw' \
        '?ref=master' % forge.url
    with urllib.request.urlopen(url) as r:
        assert r.read() == b'a: 1\n'


def test_raw_file_ref_cannot_be_a_git_option(forge, tmp_path):
    """?ref= is passed to git; "--output=<file>" must not write a file."""
    target = tmp_path / 'pwned'
    ref = urllib.parse.quote('--output=%s' % target, safe='')
    url = '%s/api/v4/projects/bundles/repository/files/README.md/raw' \
        '?ref=%s' % (forge.url, ref)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(url)
    assert e.value.code == 404
    assert not list(tmp_path.glob('pwned*'))


def test_repo_refuses_refs_that_look_like_options(tmp_path):
    repo = Repo.create(str(tmp_path / 'r.git'))
    sha = repo.commit(None, {'a': 'x\n'}, 'init', 'u', 'u@example.com')
    repo.update_ref('refs/heads/master', sha)
    assert repo.read_file('master', 'a') == 'x\n'
    assert repo.read_file('-master', 'a') is None
    assert repo.read_file('--output=%s' % (tmp_path / 'out'), 'a') is None
    assert not list(tmp_path.glob('out*'))


def test_resolve_rejects_refs_that_look_like_options(tmp_path, monkeypatch):
    """Rejected up front, not by how git happens to parse "<ref>^{commit}"."""
    repo = Repo.create(str(tmp_path / 'r.git'))
    sha = repo.commit(None, {'a': 'x\n'}, 'init', 'u', 'u@example.com')
    repo.update_ref('refs/heads/master', sha)
    # A branch that really is called "-x": once git is told where options
    # end, it would resolve, so only an explicit check keeps it refused.
    repo.update_ref('refs/heads/-x', sha)
    assert repo.resolve('master') == sha

    calls = []
    real_git = repo.git
    monkeypatch.setattr(repo, 'git', lambda *a, **kw: (
        calls.append(a), real_git(*a, **kw))[1])
    for ref in ('-x', '--short', '--output=%s' % (tmp_path / 'out')):
        assert repo.resolve(ref) is None
    assert calls == []  # git was never asked
    assert repo.resolve('master') == sha  # normal refs still go through
    assert not list(tmp_path.glob('out*'))


# Names git refuses, or would read as an option or an expression.  5 is
# not even a string.
INVALID_BRANCHES = ('-x', '--bare', '--template=/etc', 'a..b', 'HEAD', 'a b',
                    '@', '@{-1}', 'x.lock', 5)


def assert_invalid_branch(forge, msg, branch):
    code, message = rejected(forge, msg)
    assert code == 400, (branch, code, message)
    assert 'invalid branch name' in message, (branch, message)
    assert repr(branch) in message, (branch, message)


def test_create_project_rejects_invalid_branch_names(forge):
    for branch in INVALID_BRANCHES:
        assert_invalid_branch(forge, {
            'action': 'create_project', 'project': 'branchy',
            'branch': branch}, branch)
    assert 'branchy' not in forge.forge.state['projects']
    assert not os.path.exists(
        os.path.join(forge.forge.repo_root, 'branchy.git'))


def test_create_project_accepts_ordinary_branch_names(forge):
    for project, branch in (('p1', 'main'), ('p2', 'release/1.0')):
        made = forge.send({'action': 'create_project', 'project': project,
                           'branch': branch})
        assert made['default_branch'] == branch


def test_push_rejects_invalid_branch_names(forge):
    repo = forge.forge.repo(forge.forge.project('bundles'))
    refs_before = repo.out('for-each-ref', '--format=%(refname)')
    pushes_before = len(forge.hooks_of('push'))
    for branch in INVALID_BRANCHES:
        assert_invalid_branch(forge, {
            'action': 'push', 'project': 'bundles', 'branch': branch,
            'files': {'a.yaml': 'a: 1\n'}}, branch)
    # Nothing was created and no webhook announced a change.
    assert repo.out('for-each-ref', '--format=%(refname)') == refs_before
    assert len(forge.hooks_of('push')) == pushes_before


def test_push_accepts_ordinary_branch_names(forge):
    pushed = forge.send({'action': 'push', 'project': 'bundles',
                         'branch': 'release/1.0',
                         'files': {'a.yaml': 'a: 1\n'}})
    assert pushed['ref'] == 'refs/heads/release/1.0'
    # No branch means the project's default branch.
    default = forge.send({'action': 'push', 'project': 'bundles',
                          'files': {'b.yaml': 'b: 1\n'}})
    assert default['ref'] == 'refs/heads/master'


def test_open_mr_rejects_invalid_target_branch_names(forge):
    for branch in INVALID_BRANCHES:
        assert_invalid_branch(forge, {
            'action': 'open_mr', 'project': 'bundles', 'title': 'T',
            'key': 'k', 'target_branch': branch,
            'files': {'a.yaml': 'a: 1\n'}}, branch)
    assert forge.forge.project('bundles')['mrs'] == {}


def test_open_mr_accepts_an_existing_target_branch(forge):
    forge.send({'action': 'push', 'project': 'bundles',
                'branch': 'release/1.0', 'files': {'a.yaml': 'a: 1\n'}})
    mr = forge.send({'action': 'open_mr', 'project': 'bundles',
                     'title': 'T', 'target_branch': 'release/1.0',
                     'files': {'b.yaml': 'b: 1\n'}})
    assert mr['target_branch'] == 'release/1.0'


INVALID_TAGS = ('-x', '--bare', 'a..b', 'a b', '@{-1}', 'x.lock', 5)


def test_tag_rejects_invalid_tag_names(forge):
    repo = forge.forge.repo(forge.forge.project('bundles'))
    refs_before = repo.out('for-each-ref', '--format=%(refname)')
    tags_before = len(forge.hooks_of('tag_push'))
    for tag in INVALID_TAGS:
        code, message = rejected(forge, {
            'action': 'tag', 'project': 'bundles', 'tag': tag})
        assert code == 400, (tag, code, message)
        assert 'invalid tag name' in message, (tag, message)
        assert repr(tag) in message, (tag, message)
    assert repo.out('for-each-ref', '--format=%(refname)') == refs_before
    assert len(forge.hooks_of('tag_push')) == tags_before


def test_tag_accepts_ordinary_names(forge):
    for tag in ('v1.0', 'release/1'):
        tagged = forge.send({'action': 'tag', 'project': 'bundles',
                             'tag': tag})
        assert tagged['ref'] == 'refs/tags/' + tag


def test_tagging_twice_is_a_conflict_and_changes_nothing(forge):
    first = forge.send({'action': 'tag', 'project': 'bundles', 'tag': 'v1'})
    forge.send({'action': 'push', 'project': 'bundles',
                'files': {'a.yaml': 'a: 1\n'}})
    tags_before = len(forge.hooks_of('tag_push'))
    code, message = rejected(forge, {
        'action': 'tag', 'project': 'bundles', 'tag': 'v1'})
    assert code == 409, (code, message)
    assert 'v1' in message and 'already exists' in message
    repo = forge.forge.repo(forge.forge.project('bundles'))
    assert repo.resolve('refs/tags/v1') == first['after']
    assert len(forge.hooks_of('tag_push')) == tags_before


def test_valid_branch_ignores_the_current_repo(tmp_path, monkeypatch):
    """git expands "@{-1}" using the repo it runs in; that is not a name."""
    from git_stream.repos import valid_branch
    repo = tmp_path / 'work'
    repo.mkdir()
    for cmd in (['init', '-q', '-b', 'main'],
                ['commit', '-q', '--allow-empty', '-m', 'i'],
                ['checkout', '-q', '-b', 'other'], ['checkout', '-q', 'main']):
        git('-c', 'user.name=u', '-c', 'user.email=u@example.com', *cmd,
            cwd=repo)
    monkeypatch.chdir(repo)
    assert valid_branch('main')
    assert not valid_branch('@{-1}')


@pytest.mark.parametrize('name', ['@', '', '-x', '--bare'])
def test_valid_tag_refuses_what_the_ref_prefix_would_hide(name):
    """The "refs/tags/" prefix makes "-x" a legal ref; it is not a tag name."""
    from git_stream.repos import valid_tag
    assert not valid_tag(name)


@pytest.mark.parametrize('name', ['v1.0', 'release/1.0'])
def test_valid_tag_accepts_ordinary_names(name):
    from git_stream.repos import valid_tag
    assert valid_tag(name)


def test_seed_goes_through_create_projects_validation(tmp_path):
    """seed() has no branch input; it must not bypass create_project."""
    from git_stream.__main__ import seed
    (tmp_path / 'seed' / 'a..b').mkdir(parents=True)
    (tmp_path / 'seed' / 'a..b' / 'README.md').write_text('hi\n')
    f = Forge(str(tmp_path / 'data'), 'http://x')
    with pytest.raises(ForgeError, match='invalid project name'):
        seed(f, str(tmp_path / 'seed'))
    assert f.state['projects'] == {}
    assert os.listdir(f.repo_root) == []


def test_bad_messages_are_rejected(forge):
    for msg, code in [({'action': 'explode', 'project': 'bundles'}, 400),
                      ({'action': 'comment', 'project': 'bundles',
                        'mr': 'nope', 'note': 'x'}, 404),
                      # A push or an open_mr creates a missing project (see
                      # tests/test_placeholder_projects.py); the rest don't.
                      ({'action': 'tag', 'project': 'missing',
                        'tag': 'v1'}, 404)]:
        with pytest.raises(urllib.error.HTTPError) as e:
            forge.send(msg)
        assert e.value.code == code


def test_stream_requires_a_token_when_configured(forge, tmp_path):
    # With no stream tokens configured the stream is open (the default).
    forge.send({'action': 'push', 'project': 'bundles', 'files': {'a': '1'}})
    # A fresh forge (its own data dir) with tokens configured: only a valid
    # token is accepted.
    r = Running(str(tmp_path / 'streamdata'),
                stream_tokens={'build-system': 'secret-stream'})
    r.forge.create_project('bundles', {'README.md': 'x'})
    try:
        msg = {'action': 'push', 'project': 'bundles', 'files': {'a': '1'}}
        with pytest.raises(urllib.error.HTTPError) as e:
            r.send(msg)
        assert e.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as e:
            r.send(msg, token='nope')
        assert e.value.code == 401
        r.send(msg, token='secret-stream')  # works
    finally:
        r.stop()


def test_state_survives_restart(forge, tmp_path):
    forge.send({'action': 'open_mr', 'project': 'bundles', 'key': 'k',
                'title': 't', 'files': {'f': 'x'}})
    again = Forge(forge.forge.data_dir, forge.url)
    p = again.project('bundles')
    assert again.mr(p, 'k')['title'] == 't'


def test_web_pages(forge):
    forge.send({'action': 'open_mr', 'project': 'bundles', 'title': 'Hi <b>',
                'files': {'f': 'x'}})
    forge.send({'action': 'comment', 'project': 'bundles', 'mr': 1,
                'note': 'Build succeeded.'})
    for path in ('/', '/bundles', '/bundles/merge_requests/1',
                 '/bundles/-/merge_requests/1'):
        with urllib.request.urlopen(forge.url + path) as r:
            page = r.read().decode()
    assert 'Hi &lt;b&gt;' in page and 'Build succeeded.' in page


def test_seed_skips_caches_and_keeps_binary_files(tmp_path):
    from git_stream.__main__ import seed
    src = tmp_path / 'seed' / 'proj'
    (src / '__pycache__').mkdir(parents=True)
    (src / '__pycache__' / 'x.pyc').write_bytes(b'\xcb\x00')
    (src / 'logo.bin').write_bytes(b'\xcb\x00\xff')
    (src / 'README.md').write_text('hi\n')
    f = Forge(str(tmp_path / 'data'), 'http://x')
    seed(f, str(tmp_path / 'seed'))
    repo = f.repo(f.project('proj'))
    assert repo.out('ls-tree', '--name-only', 'master').split() == [
        'README.md', 'logo.bin']
    assert repo.git('show', 'master:logo.bin').stdout == b'\xcb\x00\xff'
