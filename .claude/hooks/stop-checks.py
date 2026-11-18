#!/usr/bin/env python3
"""Stop hook: run the fast checks before the agent finishes a turn.

Claude Code runs this when the main agent is about to stop and passes the
event as JSON on stdin.  If code or test files changed in the working tree it
runs ruff and then the fast parallel test loop; if either fails it prints
`{"decision": "block", "reason": <output>}` on stdout, which keeps the agent
working with the failure output as its next instruction.  Every other outcome
prints nothing and exits 0, so the agent may stop.

It does nothing (and costs nothing) when:
  - the repository is on main or master, or on a detached HEAD;
  - no tracked or untracked file under git_stream/, tests/, scripts/,
    devenv/, .claude/hooks/, no `*.py` and not pyproject.toml changed
    (markdown never counts), so docs-only and chat-only turns stay instant;
  - the working tree is the same one that already passed, or the same one
    that already failed (see below);
  - anything it needs is missing or broken (no .venv, no ruff, bad input, a
    check that takes longer than STOP_HOOK_TIMEOUT seconds, default 90).  A
    broken hook must not trap the agent, so these print a note on stderr and
    exit 0.

Loops.  `stop_hook_active` is true while Claude Code is continuing because of
a stop hook, and Claude Code ends the turn after 8 blocks in a row.  The hook
also keeps a fingerprint (HEAD, `git status`, the changed files' contents) of
the last tree it checked, and its result, in
`$TMPDIR/git-stream-stop-hook/<session_id>.json`.  A tree that passed is not
checked again.  A tree that failed and has not changed since is let through:
blocking again cannot help when the agent changed nothing.  Any change to the
tree, whether or not `stop_hook_active` is set, gets a fresh check.

This is not the gate: it skips the slow tests.  `scripts/check.sh` decides
"done".  The hook never modifies the repository.  Standard library only.
"""

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

PROTECTED = ('main', 'master')
CODE_PREFIXES = ('git_stream/', 'tests/', 'scripts/', 'devenv/',
                 '.claude/hooks/')
LIMIT = 4000
DEFAULT_TIMEOUT = 90
RUFF = ['-m', 'ruff', 'check', '.']
PYTEST = ['-m', 'pytest', '-n', 'auto', '-m', 'not slow', '-x', '-q']
NO_TESTS_COLLECTED = 5


def note(message):
    sys.stderr.write('stop-checks: %s\n' % message)


def git(cwd, *args):
    proc = subprocess.run(['git', '-C', cwd] + list(args),
                          capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def changed_paths(status):
    """The paths in `git status --porcelain -z`, renames included."""
    entries = status.split('\0')
    paths, index = [], 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if 'R' in entry[:2] or 'C' in entry[:2]:  # the next entry is the source
            index += 1
    return paths


def is_code(path):
    if path.endswith('.md'):
        return False
    return (path.endswith('.py') or path == 'pyproject.toml' or
            path.startswith(CODE_PREFIXES))


def fingerprint(root, head, status, paths):
    digest = hashlib.sha256()
    digest.update(head.encode() + b'\0' + status.encode())
    for path in sorted(paths):
        digest.update(b'\0' + path.encode() + b'\0')
        try:
            with open(os.path.join(root, path), 'rb') as handle:
                digest.update(handle.read())
        except OSError:  # deleted, or a directory: the status line says so
            pass
    return digest.hexdigest()


def state_path(session):
    name = re.sub(r'[^A-Za-z0-9_.-]', '_', session or 'unknown')
    return os.path.join(tempfile.gettempdir(), 'git-stream-stop-hook',
                        name + '.json')


def load_state(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_state(path, fingerprint_, result):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as handle:
            json.dump({'fingerprint': fingerprint_, 'result': result}, handle)
    except OSError:
        pass  # without state the hook just checks again next time


def run(command, cwd, timeout):
    """(exit code, output), or None if it ran out of time and was killed."""
    proc = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            start_new_session=True)
    try:
        output, _ = proc.communicate(timeout=max(timeout, 1))
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)  # the whole group, workers too
        proc.communicate()
        return None
    return proc.returncode, output


def tail(text):
    # pytest's -ra (pyproject.toml) lists every xfail after the real failure,
    # which would push it out of the tail.
    text = '\n'.join(line for line in text.splitlines()
                     if not line.startswith('XFAIL ')).strip()
    if len(text) <= LIMIT:
        return text
    return '... (output truncated, showing the last %d characters)\n%s' % (
        LIMIT, text[-LIMIT:])


def block(title, output):
    reason = '%s\n\n%s' % (title, tail(output))
    print(json.dumps({'decision': 'block', 'reason': reason}))


def main():
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            raise ValueError('not an object')
    except ValueError:
        note('could not read the hook input')
        return 0
    cwd = data.get('cwd') or os.getcwd()

    root = git(cwd, 'rev-parse', '--show-toplevel')
    if root is None:
        return 0
    root = root.strip()
    branch = (git(root, 'branch', '--show-current') or '').strip()
    if branch in PROTECTED or not branch:
        return 0
    status = git(root, 'status', '--porcelain', '-z', '--untracked-files=all')
    if status is None:
        note('git status failed')
        return 0
    paths = changed_paths(status)
    if not any(is_code(path) for path in paths):
        return 0

    head = (git(root, 'rev-parse', 'HEAD') or '').strip()
    current = fingerprint(root, head, status, paths)
    path = state_path(data.get('session_id'))
    if load_state(path).get('fingerprint') == current:
        return 0  # already checked this exact tree

    python = os.path.join(root, '.venv', 'bin', 'python')
    if not os.access(python, os.X_OK):
        note('no %s; set up the venv as in AGENTS.md' % python)
        return 0
    try:
        timeout = float(os.environ.get('STOP_HOOK_TIMEOUT') or
                        DEFAULT_TIMEOUT)
    except ValueError:
        timeout = DEFAULT_TIMEOUT
    deadline = time.monotonic() + timeout

    probe = run([python, '-m', 'ruff', '--version'], root, 10)
    if probe is None or probe[0] != 0:
        note('ruff is not installed for %s; skipping the checks' % python)
        return 0

    for title, args in (('ruff check . failed:', RUFF),
                        ('pytest -n auto -m "not slow" -x -q failed:',
                         PYTEST)):
        outcome = run([python] + args, root, deadline - time.monotonic())
        if outcome is None:
            note('the checks timed out after %d seconds; skipping them'
                 % timeout)
            return 0
        code, output = outcome
        if code != 0 and not (args is PYTEST and code == NO_TESTS_COLLECTED):
            save_state(path, current, 'fail')
            block(title, output)
            return 0
    save_state(path, current, 'pass')
    return 0


if __name__ == '__main__':
    sys.exit(main())
