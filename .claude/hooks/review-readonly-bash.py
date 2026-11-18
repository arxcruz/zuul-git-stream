#!/usr/bin/env python3
"""PreToolUse hook for the branch-reviewer subagent: Bash is read-only.

A subagent's `tools` list can allow Bash or not, but cannot say "only
read-only commands".  This hook does: it passes only an allowlist of git
queries, the test and lint commands (with their cache and fix options off),
and a few file viewers, and blocks everything else with exit code 2 and a
message on stderr (Claude Code shows it to the subagent).

Shell operators (`;`, `&`, `|`, `<`, `>`, `$(`, backticks) are refused
outright, so a command can't be chained into something else or write a
file by redirection; use git's own options instead of a pipe.  It is a
guardrail for a reviewer that must not change anything, not a security
boundary against a hostile one.  Standard library only.
"""

import json
import os
import shlex
import sys

OPERATORS = ';&|<>()'
# git subcommands that only read.  branch and config are checked below.
GIT_READ = {
    'blame', 'cat-file', 'describe', 'diff', 'diff-tree', 'for-each-ref',
    'grep', 'log', 'ls-files', 'ls-tree', 'merge-base', 'name-rev',
    'rev-list', 'rev-parse', 'shortlog', 'show', 'show-ref', 'status'}
# Options that make a git query write a file or run another program.
GIT_FORBIDDEN = ('--output', '--ext-diff', '--textconv', '--open-files',
                 '--exec-path', '-c', '-C', '--git-dir', '--work-tree')
BRANCH_FLAGS = {'--show-current', '--list', '-l', '-a', '--all', '-r',
                '--remotes', '-v', '-vv', '--verbose', '--no-color'}
BRANCH_WITH_VALUE = {'--contains', '--no-contains', '--merged',
                     '--no-merged', '--points-at'}
CONFIG_FLAGS = {'--get', '--get-all', '--list', '-l', '--get-regexp'}
VIEWERS = {'ls', 'cat', 'head', 'tail', 'wc', 'pwd'}
# Fixing and caching write files in the checkout.
RUFF_FORBIDDEN = ('--fix', '--unsafe-fixes', '--add-noqa', '--watch')


def words_of(command):
    """The words of a simple command, or None if it uses shell operators."""
    if '$(' in command or '`' in command or '\n' in command:
        return None
    lex = shlex.shlex(command, posix=True, punctuation_chars=OPERATORS)
    lex.whitespace = ' \t\r'
    lex.whitespace_split = True
    try:
        words = list(lex)
    except ValueError:
        return None
    if any(all(char in OPERATORS for char in word) for word in words):
        return None
    return words


def git_problem(words):
    rest = words[1:]
    if not rest:
        return 'git needs a subcommand'
    while rest and rest[0] == '--no-pager':
        rest = rest[1:]
    if not rest or rest[0].startswith('-'):
        return 'only `git <subcommand>` forms are allowed'
    sub, args = rest[0], rest[1:]
    for arg in args:
        if arg.startswith(GIT_FORBIDDEN):
            return '%s writes or runs something' % arg
    if sub in GIT_READ:
        return None
    if sub == 'branch':
        previous = ''
        for arg in args:
            if arg in BRANCH_FLAGS or arg in BRANCH_WITH_VALUE:
                pass
            elif previous in BRANCH_WITH_VALUE:
                pass
            else:
                return '`git branch` may only list (a name would create one)'
            previous = arg
        return None
    if sub == 'config':
        if args and args[0] in CONFIG_FLAGS:
            return None
        return '`git config` may only read (--get, --list)'
    return '`git %s` is not a read-only query' % sub


def python_problem(words):
    if words[1:3] == ['-m', 'pytest']:
        rest = words[3:]
        for index, word in enumerate(rest[:-1]):
            if word == '-p' and rest[index + 1] == 'no:cacheprovider':
                return None
        return 'pytest must run with `-p no:cacheprovider` (no cache files)'
    if words[1:4] == ['-m', 'ruff', 'check']:
        rest = words[4:]
        if any(w.startswith(RUFF_FORBIDDEN) for w in rest):
            return 'ruff may not fix files'
        if '--no-cache' not in rest:
            return 'ruff must run with `--no-cache` (no cache files)'
        return None
    return 'python may only run `-m pytest` or `-m ruff check`'


def problem(command):
    """Why the command is not allowed, or None if it is."""
    words = words_of(command)
    if not words:
        return ('shell operators (; & | < > $( ` newline) are not allowed: '
                'run one plain command')
    name = os.path.basename(words[0])
    if name == 'git':
        return git_problem(words)
    if name in VIEWERS:
        return None
    if name.startswith('python'):
        return python_problem(words)
    return ('`%s` is not allowed: the reviewer may only run read-only git '
            'queries, pytest, ruff check and file viewers' % name)


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        sys.stderr.write('review-readonly-bash: could not read the hook '
                         'input\n')
        return 2  # fail closed: this hook is the only thing keeping Bash safe
    tool_input = data.get('tool_input') if isinstance(data, dict) else None
    command = tool_input.get('command') if isinstance(tool_input, dict) else ''
    why = problem(command if isinstance(command, str) else '')
    if why:
        sys.stderr.write('Blocked (the reviewer is read-only): %s\n' % why)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
