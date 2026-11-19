"""The /merge project skill: .claude/skills/merge/SKILL.md.

A skill is a prompt, so most of this checks what can be checked without a
model: the frontmatter (user-invoked only, runs inline, exactly the tools
approved), that the commands it injects are allowed, that its text says what
it must (refusals, gate before and after, --no-ff, branch -d and never -D,
never push), and that AGENTS.md carries the approval rule it relies on.

Two more groups run real git in a temporary repository: the commands the
skill prescribes do what it assumes (merge-tree exits 1 on a conflict, a
--no-ff merge makes a two-parent commit, branch -d refuses an unmerged
branch), and the branch-guard hook lets all of them through while still
blocking commit and push on main.  Whether Claude Code accepts the
allowed-tools patterns, and how the interview-free flow reads, is a manual
dry run of /merge (docs/backlog.md, section 4).
"""

import fnmatch
import json
import os
import re
import shlex
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, '.claude', 'skills', 'merge', 'SKILL.md')
REVIEW_SKILL = os.path.join(ROOT, '.claude', 'skills', 'review', 'SKILL.md')
HOOK = os.path.join(ROOT, '.claude', 'hooks', 'no-commit-on-main.py')
AGENTS_MD = os.path.join(ROOT, 'AGENTS.md')

TOOLS = [
    'Bash(git branch --show-current)',
    'Bash(git status --short)',
    'Bash(git log --oneline main..HEAD)',
    'Bash(git log --oneline -3)',
    'Bash(git merge-tree --write-tree main HEAD)',
    'Bash(git diff --name-only --no-renames main...HEAD)',
    'Bash(scripts/check.sh)',
    'Bash(git switch main)',
    'Bash(git merge --no-ff -m *)',
    'Bash(git branch -d --end-of-options *)',
]

# The commands of the flow, as the skill writes them (<branch> is the
# current branch, <summary> a one-line summary).
MERGE_TREE = 'git merge-tree --write-tree main HEAD'
DIFF = 'git diff --name-only --no-renames main...HEAD'
SWITCH = 'git switch main'
MERGE = 'git merge --no-ff -m "Merge <branch>: <summary>" --end-of-options <branch>'
DELETE = 'git branch -d --end-of-options <branch>'
LOG = 'git log --oneline -3'

# The paths the sandbox protects, as docs/specs/merge-protected-path-check.md
# lists them: relative to the repository root, a trailing "/" for a
# directory, "*" within one path segment.  .git/hooks and .git/config are
# not here: git never lists anything under .git/ in a diff.
PROTECTED = ['.claude/settings*.json', '.mcp.json', '.claude/skills/',
             '.claude/agents/', '.claude/hooks/']
PHRASE = 'merge from a terminal outside claude code'


def read(path):
    assert os.path.exists(path), (
        '%s does not exist yet' % os.path.relpath(path, ROOT))
    with open(path) as f:
        return f.read()


def split(path):
    """The frontmatter as a dict of strings, and the body."""
    parts = read(path).split('---\n', 2)
    assert parts[0] == '', 'a skill starts with a --- frontmatter block'
    meta = {}
    for line in parts[1].splitlines():
        key, _, value = line.partition(':')
        meta[key.strip()] = value.strip().strip('"')
    return meta, parts[2]


@pytest.fixture(scope='module')
def skill():
    return split(SKILL)


def allowed(meta):
    return re.findall(r'\w+(?:\([^)]*\))?', meta['allowed-tools'])


def words(body):
    return ' '.join(body.split()).lower()


# -- the frontmatter -----------------------------------------------------

def test_it_is_named_merge_and_takes_an_optional_branch(skill):
    meta, _ = skill
    assert meta['name'] == 'merge'
    assert meta['description']
    assert meta['argument-hint'] == '[branch]'


def test_only_the_user_can_invoke_it(skill):
    # Typing /merge is the human's approval; the model must not be able to
    # start it.
    meta, _ = skill
    assert meta['disable-model-invocation'] == 'true'


def test_it_runs_inline_because_it_writes_to_git(skill):
    meta, _ = skill
    assert 'context' not in meta and 'agent' not in meta
    assert split(REVIEW_SKILL)[0]['context'] == 'fork'


def test_its_tools_are_exactly_the_approved_ones(skill):
    meta, _ = skill
    assert allowed(meta) == TOOLS


def test_none_of_its_tools_can_push_or_destroy(skill):
    meta, _ = skill
    for tool in allowed(meta):
        assert tool not in ('Bash', 'Bash(*)', 'Write', 'Edit'), tool
        for forbidden in ('push', 'reset', 'revert', 'checkout', 'rebase',
                          'commit', 'stash', 'clean', ' -D', ' -f',
                          '--force', '--abort'):
            assert forbidden not in tool, tool
    # The delete is the lower-case, end-of-options form only, so neither
    # `-D` nor `-d -f` matches; the log and merge-tree patterns are exact.
    assert 'Bash(git branch -d --end-of-options *)' in allowed(meta)
    assert not any(t.endswith(' *)') and t.startswith('Bash(git log')
                   for t in allowed(meta))


def test_every_injected_command_is_allowed(skill):
    meta, body = skill
    commands = re.findall(r'!`([^`]+)`', body)
    patterns = [t[len('Bash('):-1] for t in allowed(meta)
                if t.startswith('Bash(')]
    for command in commands:
        assert any(fnmatch.fnmatchcase(command, p) for p in patterns), (
            '%r is not in allowed-tools' % command)
    joined = ' '.join(commands)
    assert 'git branch --show-current' in joined
    assert 'git status --short' in joined
    assert 'git log --oneline main..HEAD' in joined


def test_every_command_of_the_flow_is_allowed(skill):
    meta, body = skill
    patterns = [t[len('Bash('):-1] for t in allowed(meta)
                if t.startswith('Bash(')]
    for command in (MERGE_TREE, DIFF, SWITCH, MERGE, DELETE, LOG,
                    'scripts/check.sh'):
        assert any(fnmatch.fnmatchcase(command, p) for p in patterns), (
            '%r is not in allowed-tools' % command)
        assert command in body, '%r is not in the skill' % command


# -- the text ------------------------------------------------------------

def test_it_refuses_on_main_a_dirty_tree_another_branch_or_an_unclean_merge(
        skill):
    _, body = skill
    text = words(body)
    assert 'refuse' in text and 'say why' in text
    assert '`main`' in body and 'detached' in text
    assert 'dirty' in text
    assert 'must equal the current branch' in text
    assert 'no commits' in text and 'nothing to merge' in text
    assert 'conflict' in text and MERGE_TREE in body


def section_one(body):
    return body.split('## 2.')[0]


def test_the_protected_path_check_runs_before_the_gate(skill):
    _, body = skill
    assert DIFF in body
    assert body.index(DIFF) < body.index('scripts/check.sh')
    assert body.index(DIFF) < body.index('## 2.')
    # It fires after the existing refusals.
    assert body.index(MERGE_TREE) < body.index(DIFF)


def test_it_refuses_with_the_terminal_phrase_and_lists_the_paths(skill):
    _, body = skill
    text = words(section_one(body))
    assert PHRASE in text
    assert 'protected' in text
    assert 'list' in text and 'matching path' in text


def test_the_skill_names_every_protected_path(skill):
    _, body = skill
    for path in PROTECTED:
        assert '`%s`' % path in body, path


def test_the_protected_table_follows_the_spec_matching_rules(skill):
    _, body = skill
    for path in PROTECTED:
        assert path.endswith('/') or path.endswith('.json'), path
    # git never lists .git/ in a diff, so an entry there could not match.
    assert '`.git/hooks' not in body and '`.git/config' not in body
    assert 'within one path segment' in words(body)


def test_a_failed_diff_is_a_refusal_not_a_pass(skill):
    _, body = skill
    text = words(section_one(body))
    # words() lowercases, and DIFF ends in "HEAD".
    after = text[text.index(DIFF.lower()):]
    assert re.search(r'if (that|the diff) command fails', after)
    assert 'report the error' in after and 'stop' in after


def test_the_gate_runs_on_the_branch_first_and_on_main_after(skill):
    _, body = skill
    text = words(body)
    assert body.count('scripts/check.sh') >= 2
    assert body.index('scripts/check.sh') < body.index(MERGE)
    assert body.rindex('scripts/check.sh') > body.index(MERGE)
    assert 'red' in text and 'stop' in text
    assert 'timeout' in text and '600000' in body
    assert 'report only' in text and 'do not undo' in text


def test_it_stops_and_reports_when_the_merge_fails(skill):
    _, body = skill
    assert 'if the merge fails, stop and report' in words(body)


def test_it_deletes_with_the_safe_option_and_never_force(skill):
    _, body = skill
    assert DELETE in body
    for line in body.splitlines():
        if '-D' in line or '--force' in line or '-f' in line.split():
            assert 'never' in line.lower(), line


def test_it_never_pushes_and_says_so(skill):
    _, body = skill
    text = words(body)
    assert 'never push' in text or 'does not push' in text
    assert 'git push' not in body.replace('never git push', '')


def test_it_uses_the_current_branch_not_the_argument_in_commands(skill):
    _, body = skill
    assert '$ARGUMENTS' in body
    for command in (MERGE, DELETE):
        assert '<branch>' in command and '$ARGUMENTS' not in command
    text = words(body)
    assert 'the current branch' in text and 'end-of-options' in text


def test_it_reports_the_log_and_the_tree_at_the_end(skill):
    _, body = skill
    assert LOG in body and 'git status --short' in body
    assert 'not pushed' in words(body) or 'nothing was pushed' in words(body)


def test_every_file_it_names_exists(skill):
    _, body = skill
    for path in set(re.findall(r'(?:scripts|docs|tests)/[\w./-]+\.\w+',
                               body)):
        assert os.path.exists(os.path.join(ROOT, path)), path


# -- AGENTS.md -----------------------------------------------------------

def test_agents_md_lets_the_slash_command_count_as_the_approval():
    text = words(read(AGENTS_MD).split('## Workflow')[1]
                 .split('A Claude Code hook')[0])
    assert '`/merge`' in text
    assert 'only the human can start it' in text or 'user-invoked' in text
    assert 'counts as' in text and 'approval' in text
    assert 'never push' in text


def test_agents_md_lists_the_skill_and_its_test():
    text = read(AGENTS_MD)
    layout = text.split('## Repository layout')[1].split('```')[1]
    assert 'skills/merge/' in layout
    assert 'test_merge_skill.py' in layout


# -- the git commands do what the skill assumes ---------------------------

def git(repo, *args, check=True):
    proc = subprocess.run(['git', '-C', str(repo)] + list(args),
                          capture_output=True, text=True)
    if check:
        assert proc.returncode == 0, proc.stderr
    return proc


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / 'repo'
    path.mkdir()
    git(path, 'init', '-q', '-b', 'main')
    git(path, 'config', 'user.email', 'a@example.com')
    git(path, 'config', 'user.name', 'a')
    (path / 'a.txt').write_text('one\n')
    git(path, 'add', 'a.txt')
    git(path, 'commit', '-qm', 'init')
    return path


def branch_with(repo, name, filename, text):
    git(repo, 'switch', '-qc', name)
    (repo / filename).write_text(text)
    git(repo, 'add', filename)
    git(repo, 'commit', '-qm', 'work on ' + name)


def prescribed(command, branch):
    """The skill's command line as an argument list for a real branch."""
    line = command.replace('<branch>', branch).replace('<summary>', 'x')
    return shlex.split(line)[1:]  # without "git"


def test_the_clean_merge_check_passes_for_a_disjoint_branch(repo):
    assert MERGE_TREE in read(SKILL)
    branch_with(repo, 'feat', 'b.txt', 'two\n')
    assert git(repo, *prescribed(MERGE_TREE, 'feat')).returncode == 0


def test_the_clean_merge_check_fails_on_a_conflict(repo):
    assert MERGE_TREE in read(SKILL)
    branch_with(repo, 'feat', 'a.txt', 'branch\n')
    git(repo, 'switch', '-q', 'main')
    (repo / 'a.txt').write_text('main\n')
    git(repo, 'commit', '-qam', 'main change')
    git(repo, 'switch', '-q', 'feat')
    assert git(repo, *prescribed(MERGE_TREE, 'feat'),
               check=False).returncode == 1


def test_the_merge_is_a_two_parent_commit_even_when_it_could_fast_forward(
        repo):
    assert MERGE in read(SKILL)
    branch_with(repo, 'feat', 'b.txt', 'two\n')
    git(repo, *prescribed(SWITCH, 'feat'))
    git(repo, *prescribed(MERGE, 'feat'))
    parents = git(repo, 'log', '-1', '--format=%p').stdout.split()
    assert len(parents) == 2
    assert git(repo, 'log', '-1', '--format=%s').stdout.strip() == (
        'Merge feat: x')
    assert git(repo, *prescribed(LOG, 'feat')).stdout.splitlines()[0].endswith(
        'Merge feat: x')


def test_the_safe_delete_removes_a_merged_branch(repo):
    assert DELETE in read(SKILL)
    branch_with(repo, 'feat', 'b.txt', 'two\n')
    git(repo, 'switch', '-q', 'main')
    git(repo, *prescribed(MERGE, 'feat'))
    git(repo, *prescribed(DELETE, 'feat'))
    assert 'feat' not in git(repo, 'branch', '--list').stdout


def test_the_safe_delete_refuses_an_unmerged_branch(repo):
    assert DELETE in read(SKILL)
    branch_with(repo, 'feat', 'b.txt', 'two\n')
    git(repo, 'switch', '-q', 'main')
    proc = git(repo, *prescribed(DELETE, 'feat'), check=False)
    assert proc.returncode != 0 and 'not fully merged' in proc.stderr
    assert 'feat' in git(repo, 'branch', '--list').stdout


def test_a_branch_named_like_an_option_stays_a_branch(repo):
    # --end-of-options keeps a name that starts with "-" from becoming one.
    assert '--end-of-options' in read(SKILL)
    git(repo, 'switch', '-q', '--detach')
    (repo / 'b.txt').write_text('two\n')
    git(repo, 'add', 'b.txt')
    git(repo, 'commit', '-qm', 'detached work')
    git(repo, 'update-ref', 'refs/heads/-x', 'HEAD')
    git(repo, 'switch', '-q', 'main')
    git(repo, *prescribed(MERGE, '-x'))
    git(repo, *prescribed(DELETE, '-x'))
    assert '-x' not in git(repo, 'branch', '--list').stdout


# -- the protected-path diff does what the skill assumes -------------------

def commit_file(repo, path, text):
    full = repo / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(text)
    git(repo, 'add', path)
    git(repo, 'commit', '-qm', 'touch ' + path)


def changed(repo):
    out = git(repo, *prescribed(DIFF, 'feat')).stdout
    return sorted(out.splitlines())


def test_the_diff_lists_changed_protected_paths(repo):
    assert DIFF in read(SKILL)
    git(repo, 'switch', '-qc', 'feat')
    commit_file(repo, '.claude/settings.json', '{}\n')
    commit_file(repo, '.mcp.json', '{}\n')
    assert changed(repo) == ['.claude/settings.json', '.mcp.json']


def test_the_diff_lists_the_old_path_of_a_rename(repo):
    assert DIFF in read(SKILL)
    commit_file(repo, '.mcp.json', '{"a": 1}\n')
    git(repo, 'switch', '-qc', 'feat')
    git(repo, 'mv', '.mcp.json', 'x.json')
    git(repo, 'commit', '-qm', 'rename')
    assert changed(repo) == ['.mcp.json', 'x.json']


def test_the_diff_lists_a_deleted_protected_path(repo):
    assert DIFF in read(SKILL)
    commit_file(repo, '.claude/hooks/h.py', 'pass\n')
    git(repo, 'switch', '-qc', 'feat')
    git(repo, 'rm', '-q', '.claude/hooks/h.py')
    git(repo, 'commit', '-qm', 'delete')
    assert changed(repo) == ['.claude/hooks/h.py']


def test_the_diff_of_an_unrelated_branch_lists_only_its_own_file(repo):
    assert DIFF in read(SKILL)
    branch_with(repo, 'feat', 'b.txt', 'two\n')
    assert changed(repo) == ['b.txt']


def test_the_diff_fails_when_main_does_not_exist(repo):
    assert DIFF in read(SKILL)
    branch_with(repo, 'feat', 'b.txt', 'two\n')
    git(repo, 'branch', '-m', 'main', 'trunk')
    proc = git(repo, *prescribed(DIFF, 'feat'), check=False)
    assert proc.returncode != 0


def test_the_diff_ignores_changes_made_on_main_after_the_fork(repo):
    # Three dots: the branch's own changes, not what main did since.
    assert DIFF in read(SKILL)
    branch_with(repo, 'feat', 'b.txt', 'two\n')
    git(repo, 'switch', '-q', 'main')
    commit_file(repo, '.mcp.json', '{}\n')
    git(repo, 'switch', '-q', 'feat')
    assert changed(repo) == ['b.txt']


# -- the branch-guard hook lets the flow through --------------------------

def run_hook(repo, command):
    payload = json.dumps({'hook_event_name': 'PreToolUse',
                          'tool_name': 'Bash', 'cwd': str(repo),
                          'tool_input': {'command': command}})
    proc = subprocess.run([HOOK], input=payload, capture_output=True,
                          text=True)
    return proc.returncode, proc.stderr


@pytest.mark.parametrize('command', [
    MERGE_TREE, DIFF, SWITCH, MERGE, DELETE, LOG, 'git status --short',
    'scripts/check.sh'])
def test_the_hook_allows_every_command_of_the_flow_on_main(repo, command):
    assert command in read(SKILL).split('---\n', 2)[2]
    code, message = run_hook(repo, command.replace('<branch>', 'feat')
                             .replace('<summary>', 'x'))
    assert code == 0, message


@pytest.mark.parametrize('command', ['git commit -m x', 'git push'])
def test_the_hook_still_blocks_commit_and_push_on_main(repo, command):
    assert 'never push' in words(read(SKILL))
    assert run_hook(repo, command)[0] == 2
