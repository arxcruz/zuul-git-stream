"""The Stop hook that runs the fast checks before the agent finishes a turn.

.claude/hooks/stop-checks.py is run the way Claude Code runs it: the event as
JSON on stdin.  Each test builds a throwaway git repository with a stub
`.venv/bin/python` that records its arguments and fails on request, so no
nested pytest runs.  The hook keeps its state under TMPDIR, which each test
points at its own directory.
"""

import importlib.util
import json
import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, '.claude', 'hooks', 'stop-checks.py')
SETTINGS = os.path.join(ROOT, '.claude', 'settings.json')

STUB = """#!/bin/sh
# Stands in for .venv/bin/python: `python -m ruff ...` / `python -m pytest ...`
case "$2 $3" in
  "ruff --version")
    [ -n "${STUB_RUFF_MISSING:-}" ] && exit 1
    echo "ruff 0.0"; exit 0;;
esac
echo "$*" >> "$STUB_LOG"
if [ "$2" = ruff ]; then
  if [ -n "${STUB_RUFF_FAIL:-}" ]; then echo "$STUB_RUFF_FAIL"; exit 1; fi
  echo "All checks passed!"; exit 0
fi
[ -n "${STUB_SLEEP:-}" ] && sleep "$STUB_SLEEP"
if [ -n "${STUB_PYTEST_FAIL:-}" ]; then
  seq 1 "${STUB_PYTEST_LINES:-1}"
  echo "$STUB_PYTEST_FAIL"
  for i in $(seq 1 "${STUB_XFAILS:-0}"); do
    [ "$i" -gt 0 ] && echo "XFAIL tests/test_x.py::test_$i - known gap"
  done
  exit 1
fi
echo "1 passed"
exit 0
"""


def git(repo, *args):
    return subprocess.run(
        ['git', '-C', str(repo), '-c', 'user.name=t',
         '-c', 'user.email=t@example.com'] + list(args),
        check=True, capture_output=True, text=True).stdout


def write(repo, name, text):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def make_repo(path, branch='feat/example'):
    path.mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', '-b', branch, str(path)], check=True)
    write(path, '.gitignore', '.venv/\n')
    write(path, 'README.md', 'readme\n')
    write(path, 'docs/guide.md', 'guide\n')
    write(path, 'git_stream/mod.py', 'x = 1\n')
    write(path, 'tests/test_mod.py', 'def test_x():\n    pass\n')
    write(path, 'pyproject.toml', '[project]\n')
    write(path, 'scripts/run.sh', '#!/bin/sh\n')
    write(path, '.claude/hooks/hook.py', 'x = 1\n')
    write(path, 'devenv/compose.yaml', 'a: 1\n')
    stub = write(path, '.venv/bin/python', STUB)
    stub.chmod(0o755)
    git(path, 'add', '-A')
    git(path, 'commit', '-q', '-m', 'init')
    return path


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / 'repo')


class Result:
    def __init__(self, proc, log):
        self.code = proc.returncode
        self.out = proc.stdout
        self.err = proc.stderr
        self.calls = log.read_text().splitlines() if log.exists() else []

    @property
    def blocked(self):
        return bool(self.out) and json.loads(self.out)['decision'] == 'block'

    @property
    def reason(self):
        return json.loads(self.out)['reason']


@pytest.fixture
def run_hook(tmp_path):
    """run(repo, ...) -> Result; the stub's calls are collected per run."""
    counter = iter(range(1000))

    def run(repo, session='s1', active=False, cwd=None, raw=None, **env):
        log = tmp_path / ('calls-%d.log' % next(counter))
        payload = raw if raw is not None else json.dumps({
            'session_id': session, 'cwd': str(cwd or repo),
            'hook_event_name': 'Stop', 'stop_hook_active': active,
            'last_assistant_message': 'done'})
        environment = dict(os.environ, TMPDIR=str(tmp_path / 'tmp'),
                           STUB_LOG=str(log), **env)
        (tmp_path / 'tmp').mkdir(exist_ok=True)
        proc = subprocess.run([HOOK], input=payload, capture_output=True,
                              text=True, cwd=str(repo), env=environment)
        return Result(proc, log)

    return run


RUFF = '-m ruff check .'
PYTEST = '-m pytest -n auto -m not slow -x -q'


def edit(repo, name='git_stream/mod.py', text='x = 2\n'):
    write(repo, name, text)


@pytest.mark.parametrize('branch', ['main', 'master'])
def test_it_never_runs_on_a_protected_branch(tmp_path, run_hook, branch):
    repo = make_repo(tmp_path / 'protected', branch)
    edit(repo)
    result = run_hook(repo)
    assert (result.code, result.out, result.calls) == (0, '', [])


def test_no_changes_skip_the_checks(repo, run_hook):
    result = run_hook(repo)
    assert (result.code, result.out, result.calls) == (0, '', [])


def test_docs_only_changes_skip_the_checks(repo, run_hook):
    edit(repo, 'docs/guide.md', 'changed\n')
    edit(repo, 'README.md', 'changed\n')
    edit(repo, 'docs/new.md', 'new\n')
    edit(repo, 'tests/notes.md', 'notes\n')
    result = run_hook(repo)
    assert (result.code, result.out, result.calls) == (0, '', [])


@pytest.mark.parametrize('name', [
    'git_stream/mod.py',          # tracked, modified
    'tests/test_mod.py',
    'pyproject.toml',
    'scripts/run.sh',
    '.claude/hooks/hook.py',
    'devenv/compose.yaml',
    'git_stream/new_module.py',   # untracked
    'tests/test_new.py',
    'tools/helper.py'])
def test_a_code_or_test_change_runs_ruff_then_pytest(repo, run_hook, name):
    edit(repo, name, 'changed = True\n')
    result = run_hook(repo)
    assert (result.code, result.out) == (0, '')
    assert result.calls == [RUFF, PYTEST]


def test_a_file_renamed_from_docs_into_code_counts(repo, run_hook):
    git(repo, 'mv', 'docs/guide.md', 'git_stream/guide.py')
    result = run_hook(repo)
    assert result.calls == [RUFF, PYTEST]


def test_the_session_directory_may_be_a_subdirectory(repo, run_hook):
    edit(repo)
    result = run_hook(repo, cwd=repo / 'git_stream')
    assert result.calls == [RUFF, PYTEST]


def test_failing_lint_blocks_with_the_output_and_skips_pytest(repo, run_hook):
    edit(repo)
    result = run_hook(repo, STUB_RUFF_FAIL='E501 line too long')
    assert result.code == 0
    assert result.blocked
    assert 'E501 line too long' in result.reason
    assert result.calls == [RUFF]


def test_failing_tests_block_with_the_output(repo, run_hook):
    edit(repo)
    result = run_hook(repo, STUB_PYTEST_FAIL='1 failed, 3 passed')
    assert result.code == 0
    assert result.blocked
    assert '1 failed, 3 passed' in result.reason
    assert result.calls == [RUFF, PYTEST]


def test_long_output_is_cut_to_its_tail(repo, run_hook):
    edit(repo)
    result = run_hook(repo, STUB_PYTEST_FAIL='THE SUMMARY',
                      STUB_PYTEST_LINES='5000')
    assert result.blocked
    assert len(result.reason) < 5000
    assert result.reason.endswith('THE SUMMARY')
    assert 'truncated' in result.reason


def test_expected_failures_do_not_push_the_real_one_out(repo, run_hook):
    """pytest's -ra lists every xfail after the failure; they are noise."""
    edit(repo)
    result = run_hook(repo, STUB_PYTEST_FAIL='THE REAL FAILURE',
                      STUB_XFAILS='300')
    assert result.blocked
    assert 'THE REAL FAILURE' in result.reason
    assert 'XFAIL' not in result.reason


def test_a_passed_tree_is_not_checked_again(repo, run_hook):
    edit(repo)
    assert run_hook(repo).calls == [RUFF, PYTEST]
    again = run_hook(repo)
    assert (again.code, again.out, again.calls) == (0, '', [])


def test_a_change_after_a_pass_is_checked_again(repo, run_hook):
    edit(repo)
    run_hook(repo)
    edit(repo, text='x = 3\n')
    assert run_hook(repo).calls == [RUFF, PYTEST]


@pytest.mark.parametrize('active', [True, False])
def test_an_unchanged_tree_after_a_failure_is_let_through(
        repo, run_hook, active):
    """Blocking again cannot help: nothing changed since the failure."""
    edit(repo)
    assert run_hook(repo, STUB_PYTEST_FAIL='boom').blocked
    again = run_hook(repo, active=active, STUB_PYTEST_FAIL='boom')
    assert (again.code, again.out, again.calls) == (0, '', [])


def test_a_changed_tree_is_rechecked_while_the_hook_is_continuing(
        repo, run_hook):
    edit(repo)
    assert run_hook(repo, STUB_PYTEST_FAIL='boom').blocked
    edit(repo, text='x = 4\n')
    again = run_hook(repo, active=True, STUB_PYTEST_FAIL='still boom')
    assert again.blocked
    assert 'still boom' in again.reason
    assert again.calls == [RUFF, PYTEST]


def test_a_fix_after_a_failure_lets_the_agent_stop(repo, run_hook):
    edit(repo)
    assert run_hook(repo, STUB_PYTEST_FAIL='boom').blocked
    edit(repo, text='x = 5\n')
    fixed = run_hook(repo, active=True)
    assert (fixed.code, fixed.out) == (0, '')
    assert fixed.calls == [RUFF, PYTEST]


def test_state_is_per_session(repo, run_hook):
    edit(repo)
    run_hook(repo, session='one')
    assert run_hook(repo, session='two').calls == [RUFF, PYTEST]


@pytest.mark.slow
def test_a_hung_check_is_cut_off_and_lets_the_agent_stop(repo, run_hook):
    edit(repo)
    result = run_hook(repo, STUB_SLEEP='30', STOP_HOOK_TIMEOUT='1')
    assert (result.code, result.out) == (0, '')
    assert 'timed out' in result.err


@pytest.mark.slow
def test_a_check_that_exits_as_it_is_killed_is_still_a_timeout(monkeypatch):
    spec = importlib.util.spec_from_file_location('stop_checks', HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def already_gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(module.os, 'killpg', already_gone)
    assert module.run(['sleep', '3'], '.', 1) is None


def test_unreadable_input_does_not_block(repo, run_hook):
    result = run_hook(repo, raw='not json')
    assert (result.code, result.out) == (0, '')
    assert 'stop-checks' in result.err


def test_a_missing_venv_does_not_block(repo, run_hook):
    edit(repo)
    os.remove(repo / '.venv' / 'bin' / 'python')
    result = run_hook(repo)
    assert (result.code, result.out) == (0, '')
    assert '.venv' in result.err


def test_ruff_not_installed_does_not_block(repo, run_hook):
    edit(repo)
    result = run_hook(repo, STUB_RUFF_MISSING='1')
    assert (result.code, result.out, result.calls) == (0, '', [])
    assert 'ruff' in result.err


def test_a_detached_head_does_not_run(repo, run_hook):
    git(repo, 'checkout', '-q', '--detach')
    edit(repo)
    result = run_hook(repo)
    assert (result.code, result.out, result.calls) == (0, '', [])


def test_outside_a_repository_it_does_nothing(tmp_path, run_hook):
    plain = tmp_path / 'plain'
    plain.mkdir()
    result = run_hook(plain)
    assert (result.code, result.out, result.calls) == (0, '', [])


def test_it_changes_nothing_in_the_repository(repo, run_hook):
    edit(repo)
    edit(repo, 'tests/test_new.py', 'y = 1\n')
    before = (git(repo, 'status', '--porcelain'), git(repo, 'diff'),
              git(repo, 'rev-parse', 'HEAD'))
    run_hook(repo)
    run_hook(repo, STUB_PYTEST_FAIL='boom')
    after = (git(repo, 'status', '--porcelain'), git(repo, 'diff'),
             git(repo, 'rev-parse', 'HEAD'))
    assert before == after


def test_the_hook_is_registered_for_stop():
    with open(SETTINGS) as handle:
        settings = json.load(handle)
    entries = settings['hooks']['Stop']
    commands = [hook for entry in entries for hook in entry['hooks']]
    assert len(commands) == 1
    hook = commands[0]
    assert hook['type'] == 'command'
    assert hook['command'] == '${CLAUDE_PROJECT_DIR}/.claude/hooks/stop-checks.py'
    assert hook['timeout'] == 120
    assert os.access(HOOK, os.X_OK)
