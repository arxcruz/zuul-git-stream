"""Bare git repositories, changed without a work tree.

Every write goes through a temporary index: read the parent tree, apply
the file changes, write a tree and a commit, then move the ref with a
compare-and-swap so a concurrent writer can't be lost.
"""

import os
import subprocess
import tempfile

ZERO_SHA = '0' * 40


class GitError(Exception):
    pass


class Repo:
    def __init__(self, path):
        self.path = path

    def git(self, *args, env=None, input=None, check=True):
        full_env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
        full_env.update(env or {})
        proc = subprocess.run(
            ['git', '--git-dir', self.path] + list(args),
            env=full_env, input=input, capture_output=True)
        if check and proc.returncode != 0:
            raise GitError('git %s: %s' % (
                ' '.join(args), proc.stderr.decode('utf8', 'replace').strip()))
        return proc

    def out(self, *args, **kw):
        return self.git(*args, **kw).stdout.decode('utf8').strip()

    @classmethod
    def create(cls, path, branch='master'):
        os.makedirs(path, exist_ok=True)
        repo = cls(path)
        repo.git('init', '-q', '--bare', '-b', branch)
        # Serve every ref (including refs/merge-requests/*) over HTTP.
        # http-backend is a CGI program and does no access control of its
        # own; git-stream enforces the git token (see server.py:_git).
        repo.git('config', 'http.uploadpack', 'true')
        return repo

    def exists(self):
        return os.path.exists(os.path.join(self.path, 'HEAD'))

    def resolve(self, ref):
        """The sha a ref points to, or None."""
        proc = self.git('rev-parse', '--verify', '-q', ref + '^{commit}',
                        check=False)
        if proc.returncode != 0:
            return None
        return proc.stdout.decode('utf8').strip()

    def branches(self):
        """{branch name: sha}."""
        out = self.out('for-each-ref', '--format=%(refname:short) %(objectname)',
                       'refs/heads/')
        return dict(line.split(' ', 1) for line in out.splitlines())

    def commit(self, parent, files, message, author, email,
               extra_parents=(), tree=None):
        """Create a commit on top of parent and return its sha.

        files maps path -> new content (str or bytes), or None to delete.
        The commit isn't referenced by anything until update_ref().
        """
        env = {
            'GIT_AUTHOR_NAME': author, 'GIT_AUTHOR_EMAIL': email,
            'GIT_COMMITTER_NAME': author, 'GIT_COMMITTER_EMAIL': email,
        }
        if tree is None:
            fd, index = tempfile.mkstemp(prefix='git-stream-index-')
            os.close(fd)
            os.unlink(index)
            try:
                ienv = dict(env, GIT_INDEX_FILE=index)
                if parent:
                    self.git('read-tree', parent, env=ienv)
                for path, content in sorted((files or {}).items()):
                    path = path.lstrip('/')
                    if content is None:
                        # Mode 0 removes the entry; --force-remove would
                        # need a work tree.
                        self.git('update-index', '--index-info', env=ienv,
                                 input=('0 %s\t%s\n' % (ZERO_SHA, path))
                                 .encode('utf8'))
                        continue
                    if isinstance(content, str):
                        content = content.encode('utf8')
                    blob = self.out('hash-object', '-w', '--stdin',
                                    input=content)
                    self.git('update-index', '--add', '--cacheinfo',
                             '100644,%s,%s' % (blob, path), env=ienv)
                tree = self.out('write-tree', env=ienv)
            finally:
                if os.path.exists(index):
                    os.unlink(index)
        args = ['commit-tree', tree, '-m', message]
        for p in ([parent] if parent else []) + list(extra_parents):
            args += ['-p', p]
        return self.out(*args, env=env)

    def update_ref(self, ref, new, old=None):
        """Point ref at new; fails if it moved away from old meanwhile.

        old=None means "don't check", ZERO_SHA means "must not exist".
        """
        args = ['update-ref', ref, new]
        if old is not None:
            args.append(old)
        self.git(*args)

    def delete_ref(self, ref):
        self.git('update-ref', '-d', ref)

    def merge_tree(self, ours, theirs):
        """Tree of merging theirs into ours, or None on conflicts."""
        proc = self.git('merge-tree', '--write-tree', '--no-messages',
                        ours, theirs, check=False)
        if proc.returncode == 0:
            return proc.stdout.decode('utf8').split('\n', 1)[0].strip()
        if proc.returncode == 1:
            return None
        raise GitError('git merge-tree: %s' % proc.stderr.decode('utf8'))

    def merge_base(self, a, b):
        proc = self.git('merge-base', a, b, check=False)
        return proc.stdout.decode('utf8').strip() or None

    def is_ancestor(self, a, b):
        return self.git('merge-base', '--is-ancestor', a, b,
                        check=False).returncode == 0

    def read_file(self, rev, path):
        proc = self.git('show', '%s:%s' % (rev, path), check=False)
        if proc.returncode != 0:
            return None
        return proc.stdout.decode('utf8', 'replace')

    def commits(self, before, after, limit=20):
        """Commits in before..after, oldest first, as GitLab push data."""
        rng = after if before in (None, ZERO_SHA) else '%s..%s' % (
            before, after)
        shas = self.out('rev-list', '--reverse', '--first-parent',
                        '--max-count=%d' % limit, rng).split()
        result = []
        for sha in shas:
            info = self.out('show', '-s', '--format=%B%x00%an%x00%ae%x00%aI',
                            sha).split('\x00')
            changes = {'added': [], 'modified': [], 'removed': []}
            status = self.out('diff-tree', '-r', '--root', '--no-commit-id',
                              '--name-status', '-m', '--first-parent', sha)
            for line in status.splitlines():
                kind, _, path = line.partition('\t')
                key = {'A': 'added', 'D': 'removed'}.get(kind[:1], 'modified')
                changes[key].append(path)
            result.append(dict(
                id=sha, message=info[0].strip() + '\n', title=info[0].strip()
                .split('\n', 1)[0], timestamp=info[3].strip(),
                author={'name': info[1], 'email': info[2]}, **changes))
        return result

    def count_commits(self, before, after):
        rng = after if before in (None, ZERO_SHA) else '%s..%s' % (
            before, after)
        return int(self.out('rev-list', '--count', '--first-parent', rng))
