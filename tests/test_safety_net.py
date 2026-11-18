"""The safety net itself: how the contract tests behave without Zuul, and
what scripts/check.sh does.  Both are what a person relies on to know the
suite is trustworthy, so both are tested."""

import os
import re
import subprocess
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Run pytest in a child process that can't import Zuul (or sees a broken one).
CHILD = ("import sys; sys.modules.update(dict.fromkeys(sys.argv[1].split(), "
         "None)) if sys.argv[1] else None; import pytest; "
         "sys.exit(pytest.main(sys.argv[2:]))")


def child_pytest(*args, block='', env=None):
    proc = subprocess.run(
        [sys.executable, '-c', CHILD, block, '-p', 'no:cacheprovider', '-q',
         '--no-header', '-W', 'ignore', *args],
        cwd=ROOT, capture_output=True, text=True,
        env=dict(os.environ, **(env or {})))
    return proc.returncode, proc.stdout + proc.stderr


def test_contract_tests_fail_when_zuul_is_missing():
    code, out = child_pytest('tests/test_zuul_contract.py', block='zuul')
    assert code != 0, out
    assert 'Zuul is not importable' in out
    assert 'pytest -m "not contract"' in out
    assert 'skipped' not in out


def test_contract_tests_can_be_deselected_without_zuul():
    code, out = child_pytest('tests/test_zuul_contract.py', '-m',
                             'not contract', block='zuul')
    assert code in (0, 5), out  # 5: everything was deselected
    assert re.search(r'\d+ deselected', out), out
    assert 'Zuul is not importable' not in out
    assert 'error' not in out.lower()


def test_a_broken_zuul_install_is_not_reported_as_a_missing_one(tmp_path):
    """Zuul is there but one of its own dependencies isn't: say so."""
    pkg = tmp_path / 'zuul'
    pkg.mkdir()
    (pkg / '__init__.py').write_text(
        'raise ModuleNotFoundError("No module named \'some_dep\'", '
        'name=\'some_dep\')\n')
    code, out = child_pytest('tests/test_zuul_contract.py',
                             env={'PYTHONPATH': str(tmp_path)})
    assert code != 0, out
    assert 'some_dep' in out
    assert 'Zuul is not importable' not in out


@pytest.fixture
def check(tmp_path):
    """Run scripts/check.sh with a fake python that only logs its calls.

    The fake answers `-m ruff --version` with FAKE_RUFF_VERSION and
    `-m ruff check .` with FAKE_RUFF; every call is appended to a log.
    """
    fake = tmp_path / 'python'
    fake.write_text(textwrap.dedent('''\
        #!/bin/sh
        echo "$*" >> "$FAKE_LOG"
        case "$*" in
          "-m ruff --version") exit "${FAKE_RUFF_VERSION:-0}" ;;
          "-m ruff check .") exit "${FAKE_RUFF:-0}" ;;
        esac
        exit "${FAKE_PYTEST:-0}"
        '''))
    fake.chmod(0o755)
    log = tmp_path / 'calls.log'

    def run(**env):
        proc = subprocess.run(
            [os.path.join(ROOT, 'scripts', 'check.sh')], capture_output=True,
            text=True, env=dict(os.environ, PYTHON=str(fake),
                                FAKE_LOG=str(log), **env))
        calls = log.read_text().splitlines() if log.exists() else []
        return proc, calls
    return run


def test_check_runs_lint_then_the_full_suite(check):
    proc, calls = check()
    assert proc.returncode == 0, proc.stderr
    assert calls == ['-m ruff --version', '-m ruff check .', '-m pytest']


def test_check_stops_at_lint_errors(check):
    proc, calls = check(FAKE_RUFF='1')
    assert proc.returncode != 0
    assert '-m pytest' not in calls


def test_check_fails_when_tests_fail(check):
    proc, calls = check(FAKE_PYTEST='1')
    assert proc.returncode != 0
    assert calls[-1] == '-m pytest'


def test_check_says_how_to_install_a_missing_ruff(check):
    proc, calls = check(FAKE_RUFF_VERSION='1')
    assert proc.returncode == 2
    assert 'ruff is not installed' in proc.stderr
    assert "-e '.[test,lint]'" in proc.stderr
    assert calls == ['-m ruff --version']  # neither lint nor tests ran


def test_check_never_deselects_the_contract_tests(check):
    proc, calls = check()
    assert not any('not contract' in c or '-m not' in c for c in calls)
