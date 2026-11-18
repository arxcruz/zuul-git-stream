"""State written before branch names were validated.

An old forge accepted any default branch, so stored state can hold a project
whose default branch is not a valid name (such as "-x").  Starting up must
not fail over that, but it must say so, and must not let such a name be
used for anything that depends on the default branch.
"""

import logging
import sqlite3

import pytest

from conftest import Running, rejected


@pytest.fixture
def data_dir(tmp_path):
    """A data directory as an old version left it: the project "legacy" has
    the default branch "-x" in its stored state."""
    path = tmp_path / 'data'
    first = Running(path)
    first.send({'action': 'create_project', 'project': 'legacy'})
    first.send({'action': 'create_project', 'project': 'fine',
                'branch': 'release/1.0'})
    first.send({'action': 'open_mr', 'project': 'legacy', 'title': 'T',
                'key': 'k', 'files': {'a.yaml': 'a: 1\n'}})
    first.stop()
    db = sqlite3.connect(str(path / 'state.db'))
    db.execute("UPDATE project SET default_branch = '-x' "
               "WHERE name = 'legacy'")
    db.commit()
    db.close()
    return path


@pytest.fixture
def restarted(data_dir):
    forge = Running(data_dir)
    yield forge
    forge.stop()


def warnings_about_defaults(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and r.name == 'git_stream.forge'
            and 'default branch' in r.getMessage()]


def test_startup_names_the_project_and_branch_in_a_warning(
        data_dir, caplog):
    with caplog.at_level(logging.WARNING, logger='git_stream.forge'):
        forge = Running(data_dir)
    try:
        [warning] = warnings_about_defaults(caplog)
        assert "'legacy'" in warning and "invalid default branch '-x'" in (
            warning)
        assert 'fine' not in warning  # a valid unusual name is not flagged
        for _ in range(3):  # not repeated per request
            forge.send({'action': 'comment', 'project': 'legacy',
                        'mr': 'k', 'note': 'hi'})
        assert len(warnings_about_defaults(caplog)) == 1
    finally:
        forge.stop()


def test_the_rest_of_the_forge_keeps_working(restarted):
    pushed = restarted.send({'action': 'push', 'project': 'fine',
                             'files': {'b.yaml': 'b: 1\n'}})
    assert pushed['ref'] == 'refs/heads/release/1.0'


@pytest.mark.parametrize('msg', [
    {'action': 'push', 'files': {'b.yaml': 'b: 1\n'}},
    {'action': 'tag', 'tag': 'v1'},
    {'action': 'open_mr', 'title': 'T2', 'key': 'k2',
     'files': {'c.yaml': 'c: 1\n'}}], ids=['push', 'tag', 'open_mr'])
def test_what_needs_the_default_branch_is_refused(restarted, msg):
    hooks_before = len(restarted.hooks)
    code, message = rejected(restarted, dict(msg, project='legacy'))
    assert code == 409, (code, message)
    assert "project 'legacy'" in message
    assert "invalid default branch '-x'" in message
    assert 'recreate' in message  # says what to do
    assert len(restarted.hooks) == hooks_before


@pytest.mark.parametrize('msg', [
    {'action': 'comment', 'mr': 'k', 'note': 'still here'},
    {'action': 'push', 'branch': 'master', 'files': {'b.yaml': 'b: 1\n'}},
    {'action': 'tag', 'tag': 'v1', 'rev': 'master'},
    {'action': 'open_mr', 'title': 'T2', 'key': 'k2', 'target_branch':
     'master', 'files': {'c.yaml': 'c: 1\n'}}],
    ids=['comment', 'push-explicit-branch', 'tag-explicit-rev',
         'open_mr-explicit-target'])
def test_what_does_not_need_it_still_works(restarted, msg):
    restarted.send(dict(msg, project='legacy'))
