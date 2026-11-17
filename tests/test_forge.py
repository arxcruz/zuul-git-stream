import json
import subprocess
import urllib.error
import urllib.request

import pytest

from git_stream.forge import Forge


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


def test_bad_messages_are_rejected(forge):
    for msg, code in [({'action': 'explode', 'project': 'bundles'}, 400),
                      ({'action': 'comment', 'project': 'bundles',
                        'mr': 'nope', 'note': 'x'}, 404),
                      ({'action': 'push', 'project': 'missing',
                        'files': {'a': 'b'}}, 404)]:
        with pytest.raises(urllib.error.HTTPError) as e:
            forge.send(msg)
        assert e.value.code == code


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
