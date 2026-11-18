"""The PreToolUse hook that keeps the branch-reviewer subagent's Bash read-only.

.claude/hooks/review-readonly-bash.py is run the way Claude Code runs it: the
tool call as JSON on stdin, exit code 2 plus a message on stderr to block.
"""

import json
import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, '.claude', 'hooks', 'review-readonly-bash.py')
AGENT = os.path.join(ROOT, '.claude', 'agents', 'branch-reviewer.md')
PY = '.venv/bin/python'


def run_hook(command=None, raw=None):
    payload = raw if raw is not None else json.dumps({
        'hook_event_name': 'PreToolUse', 'tool_name': 'Bash',
        'tool_input': {'command': command}})
    proc = subprocess.run([HOOK], input=payload, capture_output=True,
                          text=True)
    assert proc.stdout == ''
    return proc.returncode, proc.stderr


@pytest.mark.parametrize('command', [
    'git diff main..HEAD',
    'git diff --stat main..HEAD',
    'git --no-pager log --oneline -8 main..HEAD',
    'git log -p --follow -- tests/test_callbacks.py',
    'git show 1bad86c:docs/specs/callback-url.md',
    'git status --short',
    'git merge-base main HEAD',
    'git rev-parse --short HEAD',
    'git ls-tree -r --name-only HEAD docs',
    'git grep -n "notify_outcome" -- git_stream',
    'git branch --show-current',
    'git branch --list',
    'git branch -a --contains abc123',
    'git config --get user.name',
    PY + ' -m pytest tests/test_callbacks.py -q -p no:cacheprovider',
    'python3 -m pytest -m contract -p no:cacheprovider',
    PY + ' -m ruff check --no-cache .',
    'ls docs/specs',
    'cat AGENTS.md',
    'wc -l AGENTS.md',
    'head -20 README.md',
    'pwd',
])
def test_read_only_commands_are_allowed(command):
    assert run_hook(command) == (0, '')


@pytest.mark.parametrize('command', [
    'git commit -m x',
    'git push origin main',
    'git switch -c x',
    'git checkout main',
    'git reset --hard',
    'git merge feat/x',
    'git add .',
    'git stash',
    'git branch newbranch',
    'git branch -D old',
    'git branch -m old new',
    'git config user.name x',
    'git diff --output=/tmp/x main..HEAD',
    'git log --output /tmp/x',
    'git -c core.pager=x log',
    'git -C /tmp log',
    'git',
    'git --version',
])
def test_git_that_writes_is_blocked(command):
    code, message = run_hook(command)
    assert code == 2 and message.startswith('Blocked (the reviewer is read-only)')


@pytest.mark.parametrize('command', [
    'git log; rm -rf x',
    'git log && git commit -m x',
    'git log || true',
    'git log | head',
    'git log > out.txt',
    'cat AGENTS.md > copy.md',
    'cat < AGENTS.md',
    'git log $(echo x)',
    'git log `echo x`',
    'git log\ngit commit -m x',
    'echo "unbalanced',
    '(git log)',
])
def test_shell_operators_are_blocked(command):
    code, message = run_hook(command)
    assert code == 2 and 'Blocked' in message


@pytest.mark.parametrize('command', [
    'rm -rf .',
    'touch x',
    'mv a b',
    'curl http://example.com',
    'sed -i s/a/b/ AGENTS.md',
    'tee x',
    'find . -delete',
    'sh -c "git log"',
    'env git log',
    'GIT_DIR=x git log',
    PY + ' -c "open(\'x\', \'w\')"',
    PY + ' -m pip install x',
    PY + ' -m pytest tests -q',                 # would write .pytest_cache
    PY + ' -m ruff check .',                    # would write .ruff_cache
    PY + ' -m ruff check --no-cache --fix .',
    PY + ' -m ruff format .',
    PY + ' scripts/check.sh',
    'scripts/check.sh',
])
def test_other_commands_are_blocked(command):
    code, message = run_hook(command)
    assert code == 2 and 'Blocked' in message


@pytest.mark.parametrize('raw', [
    'not json', '', '[]', '"x"', '{"tool_input": 5}',
    '{"tool_input": {"command": 5}}'])
def test_unreadable_input_fails_closed(raw):
    code, _ = run_hook(raw=raw)
    assert code == 2


def test_an_empty_command_is_blocked():
    assert run_hook('')[0] == 2


def test_the_agent_has_no_edit_tools_and_uses_the_hook():
    with open(AGENT) as f:
        head = f.read().split('---')[1]
    assert 'tools: Read, Grep, Glob, Bash' in head
    for tool in ('Write', 'Edit', 'NotebookEdit'):
        assert tool in head.split('disallowedTools:')[1].split('\n')[0]
        assert tool not in head.split('\n')[3]  # the `tools:` line
    assert 'review-readonly-bash.py' in head
    assert os.access(HOOK, os.X_OK)
