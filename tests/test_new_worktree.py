"""scripts/new-worktree.sh: a sibling worktree with its own .venv.

The script is run for real against throwaway repositories under tmp_path.
`uv` is a stub first on PATH that records where it ran and with which
arguments (and fails on request), so nothing is downloaded or installed.
The parent of each repository holds only that repository, so "nothing was
created" is a comparison of directory listings.
"""

import os
import shlex
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'scripts', 'new-worktree.sh')
AGENTS_MD = os.path.join(ROOT, 'AGENTS.md')

UV_STUB = """#!/bin/sh
printf '%s\\t%s\\n' "$PWD" "$*" >> "$STUB_UV_LOG"
if [ -n "${STUB_UV_FAIL:-}" ]; then
  case "$*" in *"$STUB_UV_FAIL"*) echo "uv: simulated failure" >&2; exit 1;;
  esac
fi
if [ "$1" = venv ]; then
  for last; do :; done
  mkdir -p "$last/bin"
fi
exit 0
"""


def git(repo, *args, check=True):
    return subprocess.run(
        ['git', '-C', str(repo), '-c', 'user.name=t',
         '-c', 'user.email=t@example.com'] + list(args),
        check=check, capture_output=True, text=True).stdout.strip()


def setup_commands():
    """The uv commands of AGENTS.md's setup, as argument lists (no `uv`)."""
    with open(AGENTS_MD) as handle:
        lines = [line.strip() for line in handle]
    commands = [shlex.split(line)[1:] for line in lines
                if line.startswith('uv ') and '.venv' in line]
    assert len(commands) == 3, commands
    return commands


VENV, PROJECT, ZUUL = setup_commands()


@pytest.fixture
def work(tmp_path):
    """The directory that holds the repository and, later, its siblings."""
    path = tmp_path / 'work'
    path.mkdir()
    return path


@pytest.fixture
def repo(work):
    """The original checkout, 'proj', on a detached HEAD at main."""
    path = work / 'proj'
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(path)], check=True)
    (path / 'README.md').write_text('readme\n')
    git(path, 'add', '-A')
    git(path, 'commit', '-q', '-m', 'init')
    git(path, 'switch', '-q', '--detach')
    return path


@pytest.fixture
def uv(tmp_path):
    """The uv stub: .env for the script, .calls() for what it recorded."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    stub = bin_dir / 'uv'
    stub.write_text(UV_STUB)
    stub.chmod(0o755)
    log = tmp_path / 'uv.log'

    class Stub:
        env = {'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
               'STUB_UV_LOG': str(log)}

        @staticmethod
        def calls():
            if not log.exists():
                return []
            return [tuple(line.split('\t', 1))
                    for line in log.read_text().splitlines()]

    return Stub


@pytest.fixture
def run(uv):
    """run(cwd, *args, **env) -> CompletedProcess of the real script."""
    def run(cwd, *args, **env):
        assert os.path.exists(SCRIPT), (
            'scripts/new-worktree.sh does not exist yet')
        environment = {key: value for key, value in os.environ.items()
                       if key != 'NO_ZUUL'}
        environment.update(uv.env)
        environment.update(env)
        return subprocess.run([SCRIPT] + list(args), cwd=str(cwd),
                              capture_output=True, text=True,
                              env=environment)
    return run


def snapshot(repo):
    """Everything a refused run must leave as it found it."""
    return (sorted(os.listdir(repo.parent)),
            git(repo, 'for-each-ref'),
            git(repo, 'worktree', 'list', '--porcelain'),
            git(repo, 'status', '--porcelain'),
            git(repo, 'branch', '--show-current'))


def calls_in(uv):
    """(directory, arguments) of each uv call, directory resolved."""
    return [(os.path.realpath(cwd), args) for cwd, args in uv.calls()]


# The script itself

def test_the_script_is_executable_and_starts_with_a_sh_shebang():
    assert os.path.exists(SCRIPT), 'scripts/new-worktree.sh does not exist yet'
    assert os.access(SCRIPT, os.X_OK)
    with open(SCRIPT) as handle:
        assert handle.readline().rstrip('\n') == '#!/bin/sh'


def test_the_script_has_valid_sh_syntax():
    assert os.path.exists(SCRIPT), 'scripts/new-worktree.sh does not exist yet'
    proc = subprocess.run(['sh', '-n', SCRIPT], capture_output=True,
                          text=True)
    assert proc.returncode == 0, proc.stderr


def test_the_setup_commands_in_agents_md_are_the_expected_ones():
    """The script copies these; if AGENTS.md changes, this says so."""
    assert VENV == ['venv', '-p', '3.12', '.venv']
    assert PROJECT == ['pip', 'install', '-p', '.venv/bin/python', '-e',
                       '.[test,lint]']
    assert ZUUL == ['pip', 'install', '-p', '.venv/bin/python',
                    'git+https://opendev.org/zuul/zuul']


# Refusals: nothing is created

@pytest.mark.parametrize('name', [
    '', '-x', '--force', '-', 'a b', ' ', '..', 'feat/../x', 'a..b',
    '/abs', 'feat/', 'feat//x', '.hidden', 'x.lock', 'a~b', 'a:b'])
def test_a_bad_branch_name_is_refused_and_nothing_is_created(
        repo, run, uv, name):
    before = snapshot(repo)
    result = run(repo, name)
    assert result.returncode != 0, name
    assert result.stderr.strip(), 'say why on stderr'
    assert snapshot(repo) == before
    assert uv.calls() == []


@pytest.mark.parametrize('args', [(), ('a', 'b')])
def test_the_wrong_number_of_arguments_prints_usage(repo, run, uv, args):
    before = snapshot(repo)
    result = run(repo, *args)
    assert result.returncode != 0
    assert 'usage' in result.stderr.lower()
    assert snapshot(repo) == before
    assert uv.calls() == []


def test_an_existing_branch_is_refused(repo, run, uv):
    git(repo, 'branch', 'feat/taken')
    before = snapshot(repo)
    result = run(repo, 'feat/taken')
    assert result.returncode != 0
    assert 'feat/taken' in result.stderr
    assert snapshot(repo) == before
    assert uv.calls() == []


def test_main_itself_is_an_existing_branch(repo, run, uv):
    before = snapshot(repo)
    assert run(repo, 'main').returncode != 0
    assert snapshot(repo) == before


@pytest.mark.parametrize('kind', ['directory', 'file'])
def test_an_existing_folder_is_refused_and_left_alone(repo, run, uv, kind):
    target = repo.parent / 'proj-feat-new'
    if kind == 'directory':
        target.mkdir()
        (target / 'mine.txt').write_text('keep\n')
    else:
        target.write_text('keep\n')
    before = snapshot(repo)
    result = run(repo, 'feat/new')
    assert result.returncode != 0
    assert str(target) in result.stderr
    assert snapshot(repo) == before
    assert git(repo, 'branch', '--list', 'feat/new') == ''
    assert uv.calls() == []
    if kind == 'directory':
        assert (target / 'mine.txt').read_text() == 'keep\n'


def test_two_branches_with_the_same_slug_collide_and_the_second_is_refused(
        repo, run, uv):
    assert run(repo, 'feat/x').returncode == 0
    before = snapshot(repo)
    result = run(repo, 'feat-x')
    assert result.returncode != 0
    assert snapshot(repo) == before


# A good run

def test_it_creates_the_sibling_folder_and_checks_out_the_new_branch(
        repo, run, uv):
    result = run(repo, 'feat/new-thing')
    assert result.returncode == 0, result.stderr
    folder = repo.parent / 'proj-feat-new-thing'
    assert folder.is_dir()
    assert (folder / '.git').is_file()  # a linked worktree, not a clone
    assert git(folder, 'branch', '--show-current') == 'feat/new-thing'
    assert str(folder) in result.stdout  # tells you where it is


def test_a_branch_without_a_slash_gets_the_plain_name(repo, run, uv):
    assert run(repo, 'hotfix').returncode == 0
    assert (repo.parent / 'proj-hotfix').is_dir()


def test_the_new_branch_starts_from_main_not_from_where_it_was_run(
        repo, run, uv):
    main = git(repo, 'rev-parse', 'main')
    git(repo, 'switch', '-q', '-c', 'work')
    (repo / 'extra.txt').write_text('x\n')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', 'on work')
    assert git(repo, 'rev-parse', 'HEAD') != main
    assert run(repo, 'fix/from-main').returncode == 0
    assert git(repo, 'rev-parse', 'fix/from-main') == main
    folder = repo.parent / 'proj-fix-from-main'
    assert not (folder / 'extra.txt').exists()


def test_a_fresh_clone_with_no_local_main_still_gets_the_new_branch(
        tmp_path, run, uv):
    """A clone of a repository whose HEAD is not main has only origin/main.
    git 2.54's `worktree add -b X --end-of-options PATH main` then checks out
    main and ignores X, so the script must not pass --end-of-options."""
    origin = tmp_path / 'origin'
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(origin)],
                   check=True)
    (origin / 'README.md').write_text('readme\n')
    git(origin, 'add', '-A')
    git(origin, 'commit', '-q', '-m', 'init')
    git(origin, 'switch', '-q', '-c', 'feat/elsewhere')  # origin's HEAD
    clone_parent = tmp_path / 'clones'
    clone_parent.mkdir()
    clone = clone_parent / 'proj'
    subprocess.run(['git', 'clone', '-q', str(origin), str(clone)],
                   check=True)
    git(clone, 'switch', '-q', '--detach')
    assert git(clone, 'branch', '--list', 'main') == ''
    result = run(clone, 'feat/new')
    assert result.returncode == 0, result.stderr
    folder = clone_parent / 'proj-feat-new'
    assert git(folder, 'branch', '--show-current') == 'feat/new'
    assert git(folder, 'rev-parse', 'HEAD') == git(origin, 'rev-parse', 'main')
    assert git(clone, 'branch', '--list', 'main') == ''  # no stray main


def test_it_works_from_a_subdirectory(repo, run, uv):
    sub = repo / 'pkg' / 'deep'
    sub.mkdir(parents=True)
    assert run(sub, 'feat/sub').returncode == 0
    assert (repo.parent / 'proj-feat-sub').is_dir()
    assert not (sub / 'proj-feat-sub').exists()


def test_run_from_a_linked_worktree_the_folder_is_next_to_the_main_repo(
        repo, run, uv):
    first = repo.parent / 'proj-fix-first'
    git(repo, 'worktree', 'add', '-q', '-b', 'fix/first', str(first), 'main')
    result = run(first, 'feat/second')
    assert result.returncode == 0, result.stderr
    assert (repo.parent / 'proj-feat-second').is_dir()
    assert not (repo.parent / 'proj-fix-first-feat-second').exists()
    assert git(repo.parent / 'proj-feat-second',
               'branch', '--show-current') == 'feat/second'
    assert git(repo, 'branch', '--show-current') == ''  # still detached


def test_it_leaves_the_original_checkout_as_it_found_it(repo, run, uv):
    before = (git(repo, 'status', '--porcelain'), git(repo, 'rev-parse',
                                                      'HEAD'),
              git(repo, 'branch', '--show-current'))
    assert run(repo, 'feat/new').returncode == 0
    after = (git(repo, 'status', '--porcelain'), git(repo, 'rev-parse',
                                                     'HEAD'),
             git(repo, 'branch', '--show-current'))
    assert before == after


# The venv

def test_it_makes_the_venv_and_installs_the_project_and_zuul(repo, run, uv):
    assert run(repo, 'feat/new').returncode == 0
    folder = os.path.realpath(repo.parent / 'proj-feat-new')
    assert calls_in(uv) == [
        (folder, ' '.join(VENV)), (folder, ' '.join(PROJECT)),
        (folder, ' '.join(ZUUL))]
    assert (repo.parent / 'proj-feat-new' / '.venv' / 'bin').is_dir()


def test_the_venv_belongs_to_the_new_folder_not_the_original(repo, run, uv):
    assert run(repo, 'feat/new').returncode == 0
    assert not (repo / '.venv').exists()
    original = os.path.realpath(repo)
    assert all(cwd != original for cwd, _ in calls_in(uv))


@pytest.mark.parametrize('value', ['1', 'yes'])
def test_no_zuul_skips_only_the_zuul_install(repo, run, uv, value):
    assert run(repo, 'feat/new', NO_ZUUL=value).returncode == 0
    folder = os.path.realpath(repo.parent / 'proj-feat-new')
    assert calls_in(uv) == [
        (folder, ' '.join(VENV)), (folder, ' '.join(PROJECT))]


def test_zuul_is_installed_when_no_zuul_is_empty(repo, run, uv):
    assert run(repo, 'feat/new', NO_ZUUL='').returncode == 0
    assert len(uv.calls()) == 3


# A failing uv keeps the worktree and says how to clean up

@pytest.mark.parametrize('failing, ran', [
    ('venv', 1), ('test,lint', 2), ('zuul', 3)])
def test_a_failing_uv_fails_the_script_and_keeps_the_worktree(
        repo, run, uv, failing, ran):
    result = run(repo, 'feat/new', STUB_UV_FAIL=failing)
    folder = repo.parent / 'proj-feat-new'
    assert result.returncode != 0
    assert len(uv.calls()) == ran  # stops at the first failure
    assert folder.is_dir()
    assert git(folder, 'branch', '--show-current') == 'feat/new'
    assert git(repo, 'branch', '--list', 'feat/new')
    assert 'simulated failure' in result.stderr  # uv's own message shows


@pytest.mark.parametrize('failing', ['venv', 'test,lint', 'zuul'])
def test_a_failing_uv_prints_the_cleanup_hint(repo, run, uv, failing):
    result = run(repo, 'feat/new', STUB_UV_FAIL=failing)
    folder = repo.parent / 'proj-feat-new'
    assert 'git worktree remove' in result.stderr
    assert str(folder) in result.stderr
    assert 'git branch -d' in result.stderr
    assert 'feat/new' in result.stderr


def test_a_failing_uv_deletes_nothing(repo, run, uv):
    run(repo, 'feat/new', STUB_UV_FAIL='test,lint')
    folder = repo.parent / 'proj-feat-new'
    assert (folder / 'README.md').exists()
    assert (folder / '.venv').exists()  # what uv made before it failed


def test_a_second_run_after_a_failure_is_refused_not_overwritten(
        repo, run, uv):
    run(repo, 'feat/new', STUB_UV_FAIL='zuul')
    again = run(repo, 'feat/new')
    assert again.returncode != 0
    assert 'feat/new' in again.stderr


# The original checkout should be detached

def test_a_warning_when_the_original_checkout_is_still_on_main(
        repo, run, uv):
    git(repo, 'switch', '-q', 'main')
    result = run(repo, 'feat/new')
    assert result.returncode == 0, result.stderr
    assert 'warning' in result.stderr.lower()
    assert 'git switch --detach' in result.stderr
    assert (repo.parent / 'proj-feat-new').is_dir()
    assert git(repo, 'branch', '--show-current') == 'main'  # not changed


def test_the_warning_is_about_the_original_checkout_from_a_linked_worktree(
        repo, run, uv):
    git(repo, 'switch', '-q', 'main')
    first = repo.parent / 'proj-fix-first'
    git(repo, 'worktree', 'add', '-q', '-b', 'fix/first', str(first), 'main')
    result = run(first, 'feat/second')
    assert result.returncode == 0, result.stderr
    assert 'warning' in result.stderr.lower()


def test_no_warning_when_the_original_checkout_is_detached(repo, run, uv):
    result = run(repo, 'feat/new')
    assert result.returncode == 0, result.stderr
    assert 'warning' not in result.stderr.lower()
