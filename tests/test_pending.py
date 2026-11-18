"""tests/pending.txt: listed tests are strict xfails, everything else is
untouched.  Run in a child pytest with a throwaway copy of conftest.py."""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = '''
def test_fails_for_now():
    assert False


def test_passes_already():
    assert True


def test_not_listed_and_failing():
    assert False


import pytest


@pytest.mark.parametrize('n', [1, 2])
def test_one_case_pending(n):
    assert n == 1
'''
PENDING = '''# group: sample group
tests/test_sample.py::test_fails_for_now
tests/test_sample.py::test_passes_already
tests/test_sample.py::test_one_case_pending[2]
'''


def run_sample(tmp_path):
    tests = tmp_path / 'tests'
    tests.mkdir()
    shutil.copy(os.path.join(HERE, 'conftest.py'), tests / 'conftest.py')
    (tmp_path / 'pytest.ini').write_text('[pytest]\n')  # roots the node ids
    (tests / 'pending.txt').write_text(PENDING)
    (tests / 'test_sample.py').write_text(SAMPLE)
    proc = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-rfxX', '-p', 'no:cacheprovider',
         'tests'],
        cwd=tmp_path, capture_output=True, text=True)
    return proc.returncode, proc.stdout


def test_a_failing_listed_test_is_an_expected_failure(tmp_path):
    code, out = run_sample(tmp_path)
    assert 'XFAIL tests/test_sample.py::test_fails_for_now' in out
    assert 'XFAIL tests/test_sample.py::test_one_case_pending[2]' in out


def test_a_listed_test_that_passes_fails_the_run_and_says_why(tmp_path):
    code, out = run_sample(tmp_path)
    assert code != 0
    assert '[XPASS(strict)] specified, not implemented (sample group)' in out
    assert 'tests/pending.txt' in out and 'docs/backlog.md' in out


def test_only_the_listed_tests_and_cases_are_affected(tmp_path):
    code, out = run_sample(tmp_path)
    assert 'FAILED tests/test_sample.py::test_not_listed_and_failing' in out
    # case [1] of the parametrized test is not listed: it just passes.
    assert '1 passed' in out and '2 xfailed' in out


def test_every_pending_group_is_described_in_the_backlog():
    """pending.txt says what is pending; docs/backlog.md says what it means."""
    root = os.path.dirname(HERE)
    with open(os.path.join(HERE, 'pending.txt')) as f:
        groups = [line.split(':', 1)[1].strip() for line in f
                  if line.startswith('# group:')]
    with open(os.path.join(root, 'docs', 'backlog.md')) as f:
        backlog = f.read()
    assert groups
    for group in groups:
        assert '**%s**' % group in backlog, group
