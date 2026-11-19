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
# What git leaves in the repository while a rebase, cherry-pick, merge or
# revert waits for the user; each worktree has its own.
MARKERS = {'rebase': 'rebase-merge', 'cherry-pick': 'CHERRY_PICK_HEAD',
           'merge': 'MERGE_HEAD', 'revert': 'REVERT_HEAD'}


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


def test_not_a_repository_is_not_blocked(tmp_path):
    plain = tmp_path / 'plain'
    plain.mkdir()
    assert run_hook('git commit -m x', plain) == (0, '')


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


# A detached HEAD: `git commit` there makes a commit no branch holds, which
# is how work gets lost.  Refused, unless git itself is mid-way through a
# rebase, cherry-pick, merge or revert, which leave HEAD detached on purpose.

def git(repo, *args, check=True):
    return subprocess.run(
        ['git', '-C', str(repo), '-c', 'user.name=t', '-c',
         'user.email=t@example.com'] + list(args),
        check=check, capture_output=True, text=True)


def conflicting_repo(path, branch='main'):
    """A repository where `topic` and `branch` change file f differently."""
    make_repo(path, branch)
    (path / 'f').write_text('base\n')
    git(path, 'add', 'f')
    git(path, 'commit', '-q', '-m', 'base')
    git(path, 'switch', '-q', '-c', 'topic')
    (path / 'f').write_text('topic\n')
    git(path, 'commit', '-q', '-am', 'topic')
    git(path, 'switch', '-q', branch)
    (path / 'f').write_text('other\n')
    git(path, 'commit', '-q', '-am', 'other')
    return path


def marker(repo, name):
    out = git(repo, 'rev-parse', '--git-path', name).stdout.strip()
    return os.path.exists(os.path.join(str(repo), out))


def start(repo, operation, base='main'):
    """Leave `operation` stopped on a conflict, and check git did."""
    if operation == 'rebase':
        git(repo, 'switch', '-q', 'topic')
        git(repo, 'rebase', base, check=False)
    else:
        git(repo, 'switch', '-q', '--detach')
        if operation == 'cherry-pick':
            git(repo, 'cherry-pick', 'topic', check=False)
        elif operation == 'merge':
            git(repo, 'merge', 'topic', check=False)
        else:
            git(repo, 'revert', 'HEAD~1', check=False)
    assert marker(repo, MARKERS[operation]), operation
    return repo


def detach(repo):
    git(repo, 'switch', '-q', '--detach')
    assert git(repo, 'branch', '--show-current').stdout.strip() == ''
    return repo


@pytest.fixture
def conflicts(tmp_path):
    return conflicting_repo(tmp_path / 'conflicts')


@pytest.mark.parametrize('command', [
    'git commit -m x', 'git commit --amend --no-edit',
    'git add -A && git commit -m x', 'git -C . commit -m x'])
def test_a_commit_is_refused_on_a_detached_head(on_branch, command):
    detach(on_branch)
    code, err = run_hook(command, on_branch)
    assert code == 2, command
    assert 'detached HEAD' in err
    assert MESSAGE in err


def test_the_refusal_does_not_call_a_detached_head_a_branch(on_branch):
    detach(on_branch)
    err = run_hook('git commit -m x', on_branch)[1]
    assert "on ''" not in err and "on 'main'" not in err


@pytest.mark.parametrize('command', [
    'git status', 'git log --oneline', 'git switch -c fix/example',
    'git switch main', 'git checkout -b fix/example', 'git branch x',
    'git push origin HEAD:refs/heads/x'])
def test_everything_but_commit_is_allowed_on_a_detached_head(
        on_branch, command):
    detach(on_branch)
    assert run_hook(command, on_branch) == (0, '')


@pytest.mark.parametrize('operation', sorted(MARKERS))
def test_a_commit_is_allowed_while_git_waits_on_a_conflict(
        conflicts, operation):
    start(conflicts, operation)
    assert run_hook('git commit -m resolved', conflicts) == (0, '')


def test_a_rebase_is_still_detached_while_it_waits(conflicts):
    """The reason for the exemption: the session really is detached."""
    start(conflicts, 'rebase')
    assert git(conflicts, 'branch', '--show-current').stdout.strip() == ''


def test_a_commit_is_refused_again_once_the_operation_is_aborted(conflicts):
    start(conflicts, 'cherry-pick')
    git(conflicts, 'cherry-pick', '--abort')
    assert not marker(conflicts, MARKERS['cherry-pick'])
    code, err = run_hook('git commit -m x', conflicts)
    assert code == 2 and 'detached HEAD' in err


def test_a_commit_is_refused_after_the_conflict_was_committed(conflicts):
    start(conflicts, 'merge')
    (conflicts / 'f').write_text('resolved\n')
    git(conflicts, 'add', 'f')
    git(conflicts, 'commit', '-q', '-m', 'merged')
    assert not marker(conflicts, MARKERS['merge'])
    assert run_hook('git commit -m x', conflicts)[0] == 2


def test_the_exemption_is_only_for_a_detached_head(tmp_path):
    """Merging into main and committing there is still refused."""
    repo = conflicting_repo(tmp_path / 'on-main')
    git(repo, 'merge', 'topic', check=False)
    assert marker(repo, 'MERGE_HEAD')
    assert git(repo, 'branch', '--show-current').stdout.strip() == 'main'
    code, err = run_hook('git commit -m x', repo)
    assert code == 2 and "on 'main'" in err


def test_a_marker_in_a_subdirectory_session_is_found(conflicts):
    """git prints the marker path relative to where it ran (../.git/...)."""
    start(conflicts, 'rebase')
    sub = conflicts / 'pkg' / 'deep'
    sub.mkdir(parents=True)
    assert run_hook('git commit -m resolved', sub) == (0, '')


def test_the_repository_the_command_targets_decides_the_exemption(
        tmp_path, conflicts):
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    start(conflicts, 'rebase')
    assert run_hook('git -C %s commit -m x' % conflicts, elsewhere)[0] == 0
    assert run_hook('cd %s && git commit -m x' % conflicts, elsewhere)[0] == 0
    plain = detach(make_repo(tmp_path / 'detached', 'fix/other'))
    assert run_hook('git -C %s commit -m x' % plain, elsewhere)[0] == 2
    assert run_hook('git -C %s commit -m x' % plain, conflicts)[0] == 2
    assert run_hook('cd %s && git commit -m x' % plain, conflicts)[0] == 2


# Linked worktrees: HEAD and the markers are per worktree, so each one is
# judged on its own state.

@pytest.fixture
def original_and_linked(tmp_path):
    original = conflicting_repo(tmp_path / 'original')
    git(original, 'switch', '-q', '--detach')
    linked = tmp_path / 'linked'
    git(original, 'worktree', 'add', '-q', '-b', 'fix/linked', str(linked),
        'main')
    return original, linked


def test_a_linked_worktree_on_a_branch_may_commit(original_and_linked):
    original, linked = original_and_linked
    assert run_hook('git commit -m x', linked) == (0, '')


def test_a_detached_linked_worktree_is_refused(original_and_linked):
    original, linked = original_and_linked
    detach(linked)
    code, err = run_hook('git commit -m x', linked)
    assert code == 2 and 'detached HEAD' in err and MESSAGE in err


def test_the_detached_original_checkout_is_refused(original_and_linked):
    original, linked = original_and_linked
    code, err = run_hook('git commit -m x', original)
    assert code == 2 and 'detached HEAD' in err


def test_a_linked_worktree_on_main_is_still_refused(tmp_path):
    original = conflicting_repo(tmp_path / 'original')
    detach(original)
    linked = tmp_path / 'linked'
    git(original, 'worktree', 'add', '-q', str(linked), 'main')
    code, err = run_hook('git commit -m x', linked)
    assert code == 2 and "on 'main'" in err


@pytest.mark.parametrize('operation', sorted(MARKERS))
def test_a_linked_worktree_waiting_on_a_conflict_may_commit(
        original_and_linked, operation):
    original, linked = original_and_linked
    start(linked, operation)
    (linked / 'sub').mkdir()
    assert run_hook('git commit -m resolved', linked) == (0, '')
    assert run_hook('git commit -m resolved', linked / 'sub') == (0, '')


def test_a_marker_in_one_worktree_does_not_exempt_the_other(
        original_and_linked):
    original, linked = original_and_linked
    start(linked, 'cherry-pick')
    assert not marker(original, MARKERS['cherry-pick'])
    assert run_hook('git commit -m x', linked) == (0, '')
    assert run_hook('git commit -m x', original)[0] == 2
    assert run_hook('git -C %s commit -m x' % original, linked)[0] == 2
    assert run_hook('git -C %s commit -m x' % linked, original)[0] == 0
