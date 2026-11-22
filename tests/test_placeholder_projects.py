"""Projects made on first use are placeholders, and old repos are adopted.

open_mr and push create a project that does not exist yet: one commit with
an empty tree, nothing from the sender.  A repository already on disk (the
state was lost, or an old instance left it) is adopted, not committed to.
"""

import json
import os
import urllib.error
import urllib.request

import pytest

from conftest import Running
from git_stream.forge import Forge, ForgeError
from git_stream.repos import Repo

EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'  # sha of no files


def get(forge, path):
    with urllib.request.urlopen(forge.url + path) as r:
        return json.load(r)


def repo_of(forge, name):
    return forge.forge.repo(forge.forge.project(name))


def test_open_mr_creates_a_missing_project_as_a_placeholder(forge):
    mr = forge.send({'action': 'open_mr', 'project': 'new/thing',
                     'title': 'T', 'key': 'k', 'data': {'a': 1}})
    assert mr['state'] == 'opened'
    project = get(forge, '/api/v4/projects/new%2Fthing')
    assert project['path_with_namespace'] == 'new/thing'
    repo = repo_of(forge, 'new/thing')
    root = repo.out('rev-list', '--max-parents=0', 'master')
    assert repo.out('rev-parse', root + '^{tree}') == EMPTY_TREE
    # The MR commit changes no file either.
    assert repo.out('rev-parse', mr['sha'] + '^{tree}') == EMPTY_TREE
    assert repo.out('ls-tree', '-r', 'master') == ''


def test_open_mr_creates_the_project_on_the_requested_branch(forge):
    forge.send({'action': 'open_mr', 'project': 'p2', 'title': 'T',
                'target_branch': 'main', 'data': {}})
    assert get(forge, '/api/v4/projects/p2')['default_branch'] == 'main'


def test_push_creates_a_missing_project(forge):
    out = forge.send({'action': 'push', 'project': 'fresh',
                      'data': {'broken': True}})
    assert out['ref'] == 'refs/heads/master'
    assert get(forge, '/api/v4/projects/fresh/data')['data'] == {
        'broken': True}


def test_a_create_project_with_no_files_is_a_placeholder(forge):
    forge.send({'action': 'create_project', 'project': 'plain'})
    repo = repo_of(forge, 'plain')
    assert repo.out('ls-tree', '-r', 'master') == ''


def test_other_actions_on_a_missing_project_stay_a_404(forge):
    for msg in ({'action': 'tag', 'project': 'ghost', 'tag': 'v1'},
                {'action': 'comment', 'project': 'ghost', 'mr': 1,
                 'note': 'x'},
                {'action': 'approve', 'project': 'ghost', 'mr': 1},
                {'action': 'merge', 'project': 'ghost', 'mr': 1}):
        with pytest.raises(urllib.error.HTTPError) as e:
            forge.send(msg)
        assert e.value.code == 404, msg
    assert 'ghost' not in forge.forge.state['projects']


def test_a_numeric_or_odd_project_is_not_created(forge):
    for project in (999, ['x'], {'a': 1}):
        with pytest.raises(urllib.error.HTTPError) as e:
            forge.send({'action': 'open_mr', 'project': project,
                        'title': 'T', 'data': {}})
        assert e.value.code in (400, 404), project
    assert set(forge.forge.state['projects']) == {'bundles'}


def test_a_bad_project_name_is_a_400_and_creates_nothing(forge):
    for name in ('a..b', 'x.git'):
        with pytest.raises(urllib.error.HTTPError) as e:
            forge.send({'action': 'open_mr', 'project': name, 'title': 'T',
                        'data': {}})
        assert e.value.code == 400, name
    assert set(forge.forge.state['projects']) == {'bundles'}
    assert os.listdir(forge.forge.repo_root) == ['bundles.git']


def test_a_repository_on_disk_without_state_is_adopted(tmp_path):
    """The known crash-loop: repos on disk, no forge state."""
    first = Forge(str(tmp_path / 'data'), 'http://x')
    first.create_project('keep', {'README.md': 'mine\n'})
    sha = first.repo(first.project('keep')).resolve('master')
    first.close()
    os.unlink(str(tmp_path / 'data' / 'state.db'))

    again = Forge(str(tmp_path / 'data'), 'http://x')
    try:
        p = again.create_project('keep')  # used to crash in update-ref
        assert again.repo(p).resolve('master') == sha  # not committed to
        mr = again.open_mr('keep', 'T', {}, 'me', data={'x': 1})
        assert mr['state'] == 'opened'
    finally:
        again.close()


def test_a_repository_on_disk_without_the_branch_gets_a_commit(tmp_path):
    data = tmp_path / 'data'
    Repo.create(str(data / 'repos' / 'half.git'), 'master')
    f = Forge(str(data), 'http://x')
    try:
        p = f.create_project('half')
        assert f.repo(p).resolve('master') is not None
    finally:
        f.close()


def test_a_restart_keeps_placeholder_projects(tmp_path):
    first = Running(tmp_path / 'data')
    try:
        first.send({'action': 'open_mr', 'project': 'p', 'title': 'T',
                    'key': 'k', 'data': {'v': 1}})
    finally:
        first.stop()
    again = Running(tmp_path / 'data')
    try:
        assert get(again, '/api/v4/projects/p/merge_requests/1/data')[
            'data'] == {'v': 1}
        with pytest.raises(ForgeError):
            again.forge.project('absent')
    finally:
        again.stop()
