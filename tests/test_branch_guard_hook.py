"""The PreToolUse hook that stops `git commit` / `git push` on main.

.claude/hooks/no-commit-on-main.py is run the way Claude Code runs it: the
tool call as JSON on stdin, exit code 2 plus a message on stderr to block.
"""

import json
import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, '.claude', 'hooks', 'no-commit-on-main.py')
SETTINGS = os.path.join(ROOT, '.claude', 'settings.json')
MESSAGE = 'Create a branch first: git switch -c <type>/<topic>'


def make_repo(path, branch):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q', '-b', branch, str(path)], check=True)
    subprocess.run(['git', '-C', str(path), '-c', 'user.name=t', '-c',
                    'user.email=t@example.com', 'commit', '-q',
                    '--allow-empty', '-m', 'init'], check=True)
    return path


def run_hook(command, cwd, raw=None):
    """(exit code, stderr) for a Bash call whose session directory is cwd."""
    payload = raw if raw is not None else json.dumps({
        'session_id': 's', 'cwd': str(cwd), 'hook_event_name': 'PreToolUse',
        'tool_name': 'Bash', 'tool_input': {'command': command}})
    proc = subprocess.run([HOOK], input=payload, capture_output=True,
                          text=True, cwd=str(cwd))
    assert proc.stdout == ''  # nothing to stdout: no JSON decision is made
    return proc.returncode, proc.stderr


@pytest.fixture
def on_main(tmp_path):
    return make_repo(tmp_path / 'on-main', 'main')


@pytest.fixture
def on_branch(tmp_path):
    return make_repo(tmp_path / 'on-branch', 'fix/example')


@pytest.mark.parametrize('branch', ['main', 'master'])
@pytest.mark.parametrize('command, shown', [
    ('git commit -m x', 'git commit'),
    ('git commit --amend --no-edit', 'git commit'),
    ('git push', 'git push'),
    ('git push origin HEAD', 'git push')])
def test_commit_and_push_are_blocked_on_a_protected_branch(
        tmp_path, branch, command, shown):
    repo = make_repo(tmp_path / 'repo', branch)
    code, err = run_hook(command, repo)
    assert code == 2
    assert MESSAGE in err
    assert '`%s`' % shown in err and "'%s'" % branch in err


@pytest.mark.parametrize('command', ['git commit -m x', 'git push origin x'])
def test_they_are_allowed_on_a_branch(on_branch, command):
    assert run_hook(command, on_branch) == (0, '')


@pytest.mark.parametrize('command', [
    'git status', 'git log --oneline', 'git diff', 'git pull',
    'git switch -c fix/example', 'git checkout -b feat/example',
    'git branch -m fix/example', 'ls -la', 'pytest -q',
    'echo "git commit"', "echo 'git push origin main'",
    'git log --grep="git commit"'])
def test_everything_else_is_allowed_on_main(on_main, command):
    """In particular, the commands that follow the advice in the message."""
    assert run_hook(command, on_main) == (0, '')


@pytest.mark.parametrize('command', [
    'git status && git commit -m x',
    'git add -A; git commit -m x',
    'git add -A\ngit commit -m x',
    'false || git push',
    '(git commit -m x)',
    'FOO=1 git commit -m x',
    'git -c user.name=a -c user.email=a@b.c commit -m x',
    'git --no-pager commit -m x',
    '/usr/bin/git commit -m x',
    'env GIT_AUTHOR_NAME=a git commit -m x',
    'bash -c "git commit -m x"',
    "sh -c 'git add . && git push'",
    'git commit -m "unbalanced'])
def test_compound_and_wrapped_commands_are_caught(on_main, command):
    code, err = run_hook(command, on_main)
    assert code == 2, command
    assert MESSAGE in err


def test_the_repository_the_command_targets_decides(tmp_path, on_main,
                                                    on_branch):
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    # Session directory is neither repository.
    assert run_hook('git -C %s commit -m x' % on_main, elsewhere)[0] == 2
    assert run_hook('cd %s && git commit -m x' % on_main, elsewhere)[0] == 2
    assert run_hook('git --git-dir=%s/.git commit -m x' % on_main,
                    elsewhere)[0] == 2
    assert run_hook('git -C %s commit -m x' % on_branch, elsewhere)[0] == 0
    # Session directory is on main, but the command works in a branch repo.
    assert run_hook('git -C %s commit -m x' % on_branch, on_main)[0] == 0
    assert run_hook('cd %s && git commit -m x' % on_branch, on_main)[0] == 0
    # Relative paths are relative to the session directory.
    assert run_hook('git -C on-main commit -m x', tmp_path)[0] == 2
    assert run_hook('cd on-main && git commit -m x', tmp_path)[0] == 2


def test_not_a_repository_or_a_detached_head_is_not_blocked(tmp_path, on_main):
    plain = tmp_path / 'plain'
    plain.mkdir()
    assert run_hook('git commit -m x', plain) == (0, '')
    sha = subprocess.run(['git', '-C', str(on_main), 'rev-parse', 'HEAD'],
                         capture_output=True, text=True).stdout.strip()
    subprocess.run(['git', '-C', str(on_main), 'checkout', '-q', sha],
                   check=True, capture_output=True)
    assert run_hook('git commit -m x', on_main) == (0, '')


@pytest.mark.parametrize('payload', [
    '{}', '{"tool_name": "Bash"}', '{"tool_input": {}}',
    '{"tool_input": {"command": ""}}', '{"tool_input": null}'])
def test_input_without_a_command_is_allowed(on_main, payload):
    assert run_hook(None, on_main, raw=payload) == (0, '')


def test_unreadable_input_is_a_non_blocking_error(on_main):
    """Only exit 2 blocks; any other failure lets the command through."""
    code, err = run_hook(None, on_main, raw='not json')
    assert code == 1 and 'could not read' in err


def test_it_is_wired_into_the_project_settings():
    with open(SETTINGS) as f:
        settings = json.load(f)
    [group] = [g for g in settings['hooks']['PreToolUse']
               if g['matcher'] == 'Bash']
    [hook] = [h for h in group['hooks'] if h['type'] == 'command']
    assert hook['command'] == '${CLAUDE_PROJECT_DIR}/.claude/hooks/' \
        'no-commit-on-main.py'
    assert os.access(HOOK, os.X_OK)
    # And the configured command, resolved, is the script that is tested here.
    resolved = hook['command'].replace('${CLAUDE_PROJECT_DIR}', ROOT)
    assert os.path.samefile(resolved, HOOK)
