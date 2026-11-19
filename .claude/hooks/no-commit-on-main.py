#!/usr/bin/env python3
"""PreToolUse hook for the Bash tool: no `git commit` or `git push` on main,
and no `git commit` on a detached HEAD.

Claude Code runs this before every Bash command and passes the tool call as
JSON on stdin.  If the command would run `git commit` or `git push` while the
repository it targets is on main or master, or `git commit` while it is on a
detached HEAD (a commit no branch holds is how work gets lost), the hook
prints how to fix it on stderr and exits 2, which blocks the command and
shows that message to Claude.  Anything else exits 0 and the command goes
ahead.

A rebase, cherry-pick, merge or revert that stopped on a conflict leaves HEAD
detached on purpose and is finished with `git commit`, so a commit is allowed
while git's marker for one of them exists (`rebase-merge`, `rebase-apply`,
`CHERRY_PICK_HEAD`, `MERGE_HEAD`, `REVERT_HEAD`).  The markers live in each
worktree's own git directory; the hook asks git for their absolute paths, so
it finds them from a subdirectory, with `git -C`, and in a linked worktree.

It follows `cd <dir>`, `git -C <dir>` and `bash -c "..."`, and ignores quoted
text (so `echo "git commit"` is fine).  It is a guardrail for an agent that
forgot the workflow in AGENTS.md, not a security boundary: aliases, scripts
that call git themselves, and `git push origin main` from another branch are
not caught.  Standard library only; runs on the system Python.
"""

import json
import os
import re
import shlex
import subprocess
import sys

PROTECTED = ('main', 'master')
GUARDED = ('commit', 'push')
SEPARATORS = ';&|()\n'
# Commands that only run the next command.
WRAPPERS = ('command', 'env', 'exec', 'nohup', 'sudo', 'time')
SHELLS = ('bash', 'sh', 'zsh')
# What git leaves in the git directory while one of those operations waits.
IN_PROGRESS = ('rebase-merge', 'rebase-apply', 'CHERRY_PICK_HEAD',
               'MERGE_HEAD', 'REVERT_HEAD')
# git's global options that take a value as the next word.
VALUE_OPTIONS = ('-c', '-C', '--git-dir', '--work-tree', '--namespace',
                 '--exec-path')
# The ones that decide which repository git acts on.
REPO_OPTIONS = ('-C', '--git-dir', '--work-tree')


def tokens(command):
    """Split a shell command line into words and separator tokens."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=SEPARATORS)
    lex.whitespace = ' \t\r'
    lex.whitespace_split = True
    return list(lex)


def segments(command):
    """The simple commands in a command line, each as a list of words."""
    try:
        words = tokens(command)
    except ValueError:  # unbalanced quote: split crudely rather than give up
        return [part.split() for part in re.split('[;&|()\n]+', command)
                if part.split()]
    result, current = [], []
    for word in words:
        if all(char in SEPARATORS for char in word):
            if current:
                result.append(current)
            current = []
        else:
            current.append(word)
    if current:
        result.append(current)
    return result


def git_invocation(words):
    """(subcommand, repo options) of a `git ...` command, else None."""
    if os.path.basename(words[0]) != 'git':
        return None
    repo_options, index = [], 1
    while index < len(words):
        word = words[index]
        if word in VALUE_OPTIONS and index + 1 < len(words):
            if word in REPO_OPTIONS:
                repo_options += [word, words[index + 1]]
            index += 2
        elif word.startswith('-'):
            if word.split('=', 1)[0] in REPO_OPTIONS and '=' in word:
                repo_options.append(word)
            index += 1
        else:
            return word, repo_options
    return None


def guarded_commands(command, directory):
    """Yield (subcommand, directory, repo options) for each guarded git call."""
    for words in segments(command):
        while words and (re.match(r'[A-Za-z_]\w*=', words[0]) or
                         words[0] in WRAPPERS):
            words = words[1:]
        if not words:
            continue
        if words[0] == 'cd' and len(words) > 1:
            directory = os.path.join(directory, os.path.expanduser(words[1]))
        elif os.path.basename(words[0]) in SHELLS and '-c' in words[1:3]:
            inner = words[words.index('-c') + 1:][:1]
            if inner:
                for found in guarded_commands(inner[0], directory):
                    yield found
        else:
            git = git_invocation(words)
            if git and git[0] in GUARDED:
                yield git[0], directory, git[1]


def git_output(directory, repo_options, *args):
    """Stdout of git run where the command would run, None if it failed."""
    proc = subprocess.run(['git', '-C', directory] + repo_options +
                          list(args), capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def current_branch(directory, repo_options):
    """The branch the repository is on; '' if detached, None if it isn't a
    repository."""
    return git_output(directory, repo_options, 'branch', '--show-current')


def operation_in_progress(directory, repo_options):
    """Is a rebase, cherry-pick, merge or revert waiting on the user?"""
    for name in IN_PROGRESS:
        # Absolute, so it does not matter which directory git ran in.
        path = git_output(directory, repo_options, 'rev-parse',
                          '--path-format=absolute', '--git-path', name)
        if path and os.path.exists(path):
            return True
    return False


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        sys.stderr.write('no-commit-on-main: could not read the hook input\n')
        return 1  # a non-blocking error; only exit 2 blocks
    command = (data.get('tool_input') or {}).get('command') or ''
    directory = data.get('cwd') or os.getcwd()
    for subcommand, where, repo_options in guarded_commands(command,
                                                            directory):
        branch = current_branch(where, repo_options)
        if branch in PROTECTED:
            sys.stderr.write(
                "Blocked: `git %s` on '%s'.\n"
                'Create a branch first: git switch -c <type>/<topic>\n'
                % (subcommand, branch))
            return 2
        if (branch == '' and subcommand == 'commit' and
                not operation_in_progress(where, repo_options)):
            sys.stderr.write(
                'Blocked: `git commit` on a detached HEAD.\n'
                'Create a branch first: git switch -c <type>/<topic>\n')
            return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
