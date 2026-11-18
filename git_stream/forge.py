"""The fake forge: projects, merge requests, notes and approvals.

State lives in a database in the data directory (SQLite by default, or a
JSON file when the sqlite3 module is unavailable): projects, merge
requests, notes, approvals, the deduplication list and the per-MR retry
state.  Every change made through the stream (actions sent by other
systems) or the GitLab API (calls made by Zuul) emits the webhook GitLab
would send.

The database is read in memory at start-up; the in-memory view is
authoritative while the process runs and is persisted on every change.
That keeps the hot path lock-free on the database and survives restarts.
"""

import copy
import datetime
import json
import logging
import os
import sqlite3
import threading
import zlib

from git_stream.repos import Repo, ZERO_SHA, valid_branch

log = logging.getLogger('git_stream.forge')

# How many processed message ids to remember for de-duplication.
SEEN_IDS = 1000

# The in-memory shape a store must present.  A project is a plain dict
# (name, id, default_branch, description, created_at, next_iid, keys, mrs);
# a merge request is a plain dict; a note is a plain dict.
MR_COLUMNS = ('id', 'iid', 'project_id', 'title', 'description', 'state',
              'target_branch', 'source_branch', 'sha', 'start_sha',
              'author', 'labels', 'approved_by', 'created_at', 'updated_at',
              'merged_at', 'closed_at', 'merge_commit_sha', 'key',
              'max_attempts', 'escalated_sha', 'escalation')


class ForgeError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%S.%fZ')


def user(username):
    return {'id': zlib.crc32(username.encode('utf8')) % 100000,
            'username': username,
            'name': username, 'state': 'active'}


# -- stores ------------------------------------------------------------------

class SqliteStore:
    """Forge state in a SQLite database.

    The in-memory view (built in load) is authoritative while the process
    runs; the database is a persistence mirror written on save().  One
    writer (the forge, under its lock) and single-process readers make
    locking and WAL unnecessary.
    """

    SCHEMA = '''
    CREATE TABLE IF NOT EXISTS project (
      id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
      default_branch TEXT, description TEXT, created_at TEXT,
      next_iid INTEGER);
    CREATE TABLE IF NOT EXISTS mr (
      id INTEGER PRIMARY KEY, iid INTEGER, project_id INTEGER,
      title TEXT, description TEXT, state TEXT,
      target_branch TEXT, source_branch TEXT, sha TEXT, start_sha TEXT,
      author TEXT, labels TEXT, approved_by TEXT,
      created_at TEXT, updated_at TEXT, merged_at TEXT, closed_at TEXT,
      merge_commit_sha TEXT, key TEXT, max_attempts INTEGER,
      escalated_sha TEXT, escalation TEXT);
    CREATE TABLE IF NOT EXISTS note (
      id INTEGER PRIMARY KEY, mr_id INTEGER, body TEXT, author TEXT,
      created_at TEXT);
    CREATE TABLE IF NOT EXISTS seen (id INTEGER PRIMARY KEY, message_id TEXT);
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER);
    '''

    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(self.SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def load(self):
        """Build the in-memory view: {projects, keys, mrs, seen, counters}."""
        state = {'projects': {}, 'keys': {}, 'mrs': {}, 'seen': [],
                 'next_project_id': 1, 'next_mr_id': 1, 'next_note_id': 1}
        for r in self.conn.execute('SELECT * FROM project'):
            state['projects'][r['name']] = {
                'id': r['id'], 'name': r['name'],
                'default_branch': r['default_branch'],
                'description': r['description'], 'created_at': r['created_at'],
                'next_iid': r['next_iid'], 'mrs': {}, 'keys': {}}
        for r in self.conn.execute('SELECT * FROM mr ORDER BY iid'):
            mr = dict(r)
            mr['labels'] = _loads(mr['labels'])
            mr['approved_by'] = _loads(mr['approved_by'])
            mr['notes'] = []
            mr['escalation'] = _loads(mr['escalation'])
            state['mrs'][mr['project_id'], mr['iid']] = mr
            if mr['key']:
                state['keys'][mr['project_id'], str(mr['key'])] = mr['iid']
        # Index MRs and keys into the per-project dicts (keyed by name).
        for (pid, iid), mr in state['mrs'].items():
            for p in state['projects'].values():
                if p['id'] == pid:
                    p['mrs'][str(iid)] = mr
                    if mr['key']:
                        p['keys'][str(mr['key'])] = iid
                    break
        by_id = {mr['id']: mr for mr in state['mrs'].values()}
        for r in self.conn.execute('SELECT id, mr_id, body, author,'
                                   ' created_at FROM note ORDER BY id'):
            mr = by_id.get(r['mr_id'])
            if mr is None:
                continue
            mr['notes'].append({'id': r['id'], 'body': r['body'],
                                'author': r['author'],
                                'created_at': r['created_at']})
        state['seen'] = [r['message_id'] for r in self.conn.execute(
            'SELECT message_id FROM seen ORDER BY id')]
        for key, value in self.conn.execute('SELECT key, value FROM meta'):
            state[key] = value
        return state

    def save(self, state):
        c = self.conn
        c.execute('BEGIN')
        c.execute('DELETE FROM project')
        c.execute('DELETE FROM mr')
        c.execute('DELETE FROM note')
        c.execute('DELETE FROM seen')
        c.execute('DELETE FROM meta')
        c.executemany(
            'INSERT INTO project(id, name, default_branch, description,'
            ' created_at, next_iid) VALUES (?,?,?,?,?,?)',
            [(p['id'], p['name'], p['default_branch'], p['description'],
              p['created_at'], p['next_iid']) for p in
             state['projects'].values()])
        c.executemany(
            'INSERT INTO mr VALUES (%s)' % ','.join(['?'] * len(MR_COLUMNS)),
            [_mr_tuple(mr, pid, iid)
             for (pid, iid), mr in state['mrs'].items()])
        for (pid, _), mr in state['mrs'].items():
            for n in mr['notes']:
                c.execute('INSERT INTO note VALUES (?,?,?,?,?)',
                          (n['id'], mr['id'], n['body'], n['author'],
                           n['created_at']))
        c.executemany('INSERT INTO seen(message_id) VALUES (?)',
                      [(x,) for x in state['seen']])
        for key in ('next_project_id', 'next_mr_id', 'next_note_id'):
            c.execute('INSERT INTO meta VALUES (?,?)', (key, state[key]))
        c.commit()


class JsonStore:
    """Forge state in a JSON file (the previous behavior)."""

    def __init__(self, path):
        self.path = path
        self.state = {'projects': {}, 'next_project_id': 1, 'next_mr_id': 1,
                      'next_note_id': 1, 'seen': []}
        if os.path.exists(path):
            with open(path) as f:
                self.state.update(json.load(f))

    def load(self):
        import copy
        return copy.deepcopy(self.state)

    def save(self, state):
        # The on-disk shape is the legacy one: per-project mrs/keys.  The
        # in-memory `mrs`/`keys` index uses tuple keys (not JSON-able), so it
        # is rebuilt from the per-project data instead of written.
        data = {
            'projects': state['projects'],
            'next_project_id': state['next_project_id'],
            'next_mr_id': state['next_mr_id'],
            'next_note_id': state['next_note_id'],
            'seen': state['seen'],
        }
        tmp = self.path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def close(self):
        pass


def _loads(value):
    return json.loads(value) if value else []


def _mr_tuple(mr, pid, iid):
    """The 20-column value tuple for an MR row, in MR_COLUMNS order."""
    return (mr['id'], iid, pid, mr['title'], mr['description'], mr['state'],
            mr['target_branch'], mr['source_branch'], mr['sha'],
            mr['start_sha'], mr['author'],
            json.dumps(mr.get('labels') or []),
            json.dumps(mr.get('approved_by') or []),
            mr['created_at'], mr['updated_at'], mr.get('merged_at'),
            mr.get('closed_at'), mr.get('merge_commit_sha'), mr.get('key'),
            mr.get('max_attempts'), mr.get('escalated_sha'),
            json.dumps(mr['escalation']) if mr.get('escalation') else None)


def _load_store(data_dir):
    """A SQLite store, falling back to JSON if sqlite3 is unavailable."""
    path = os.path.join(data_dir, 'state.db')
    try:
        store = SqliteStore(path)
        return store
    except sqlite3.Error:
        return JsonStore(os.path.join(data_dir, 'state.json'))


# -- the forge ----------------------------------------------------------------

class Forge:
    def __init__(self, data_dir, baseurl, emit=None, users=None,
                 stream_tokens=None, report_users=()):
        """emit(payload) is called with every webhook payload, in order.

        users maps API tokens to user names; with none configured, any
        token is accepted and acts as the user "zuul".  stream_tokens does
        the same for POST /api/stream senders.  report_users are the names
        Zuul reports under; an open stream may not send as them.
        """
        self.data_dir = data_dir
        self.repo_root = os.path.join(data_dir, 'repos')
        self.baseurl = baseurl.rstrip('/')
        self.emit = emit or (lambda payload: None)
        self.users = users or {}
        self.stream_tokens = stream_tokens or {}
        self.report_users = set(report_users)
        self.lock = threading.RLock()
        os.makedirs(self.repo_root, exist_ok=True)
        self.store = _load_store(data_dir)
        self.state = self.store.load()
        self._normalize()

    def _normalize(self):
        """Rebuild the global MR/key index from the per-project data.

        A legacy JSON file may lack the index entirely; rebuilding it (the
        per-project dicts are the source of truth here) keeps both stores'
        in-memory views canonical.  Idempotent.
        """
        mrs, keys = {}, {}
        for p in self.state['projects'].values():
            for _iid, mr in p['mrs'].items():
                mrs[p['id'], mr['iid']] = mr
                if mr.get('key'):
                    keys[p['id'], str(mr['key'])] = mr['iid']
        self.state['mrs'] = mrs
        self.state['keys'] = keys

    # -- persistence and lookups ------------------------------------------

    def save(self):
        self.store.save(self.state)

    def close(self):
        self.store.close()

    def user_for_token(self, token):
        if not self.users:
            return 'zuul'
        return self.users.get(token)

    def stream_user_for_token(self, token):
        """The sender a stream token belongs to (None if unknown).

        stream_tokens maps user -> token (as in [api_tokens]); look up the
        token on the value side.
        """
        if not self.stream_tokens:
            return 'git-stream'
        if token is None:
            return None
        for user, tok in self.stream_tokens.items():
            if tok == token:
                return user
        return None

    def repo(self, project):
        return Repo(os.path.join(self.repo_root, project['name'] + '.git'))

    def project(self, ref):
        """Find a project by name, URL-encoded name or numeric id."""
        projects = self.state['projects']
        if ref in projects:
            return projects[ref]
        for p in projects.values():
            if str(p['id']) == str(ref):
                return p
        raise ForgeError('404 Project Not Found', 404)

    def mr(self, project, ref):
        """Find a merge request by iid or by its stream key."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            mr = project['mrs'].get(str(ref))
        else:
            iid = project['keys'].get(str(ref))
            mr = project['mrs'].get(str(iid)) if iid else None
        if mr is None:
            raise ForgeError('404 Merge Request Not Found', 404)
        return mr

    def _index_mr(self, p, mr):
        """Keep the in-memory MR index in sync with a project's MR.

        `save()` iterates the global `state['mrs']` index, not the
        per-project `mrs` dict, so every created or re-keyed merge request
        must be registered here before it is persisted.
        """
        iid = mr['iid']
        self.state['mrs'][p['id'], iid] = mr
        if mr.get('key'):
            self.state['keys'][p['id'], str(mr['key'])] = iid

    # -- GitLab representations --------------------------------------------

    def project_json(self, p):
        url = '%s/%s' % (self.baseurl, p['name'])
        return {
            'id': p['id'], 'name': p['name'].rsplit('/', 1)[-1],
            'path': p['name'].rsplit('/', 1)[-1],
            'path_with_namespace': p['name'],
            'namespace': p['name'].rsplit('/', 1)[0] if '/' in p['name']
            else '',
            'description': p.get('description', ''),
            'default_branch': p['default_branch'],
            'web_url': url, 'homepage': url, 'http_url': url + '.git',
            'git_http_url': url + '.git', 'http_url_to_repo': url + '.git',
            'created_at': p['created_at'],
        }

    def mr_url(self, p, mr):
        return '%s/%s/merge_requests/%s' % (self.baseurl, p['name'],
                                            mr['iid'])

    def mr_json(self, p, mr):
        repo = self.repo(p)
        target = repo.resolve('refs/heads/' + mr['target_branch'])
        merge_status = 'can_be_merged'
        if mr['state'] == 'opened' and target:
            if repo.merge_tree(target, mr['sha']) is None:
                merge_status = 'cannot_be_merged'
        base = (repo.merge_base(target, mr['sha']) if target else None) \
            or mr['start_sha']
        return {
            'id': mr['id'], 'iid': mr['iid'], 'project_id': p['id'],
            'title': mr['title'], 'description': mr['description'],
            'state': mr['state'], 'created_at': mr['created_at'],
            'updated_at': mr['updated_at'], 'merged_at': mr.get('merged_at'),
            'closed_at': mr.get('closed_at'),
            'target_branch': mr['target_branch'],
            'source_branch': mr['source_branch'],
            'source_project_id': p['id'], 'target_project_id': p['id'],
            'author': user(mr['author']),
            'labels': list(mr['labels']),
            'draft': False, 'work_in_progress': False,
            'merge_status': merge_status,
            'detailed_merge_status': ('mergeable'
                                      if merge_status == 'can_be_merged'
                                      else 'broken_status'),
            'has_conflicts': merge_status != 'can_be_merged',
            'blocking_discussions_resolved': True,
            'sha': mr['sha'],
            'merge_commit_sha': mr.get('merge_commit_sha'),
            'squash_commit_sha': None,
            'diff_refs': {'base_sha': base, 'head_sha': mr['sha'],
                          'start_sha': mr['start_sha']},
            'references': {'short': '!%s' % mr['iid'],
                           'full': '%s!%s' % (p['name'], mr['iid'])},
            'web_url': self.mr_url(p, mr),
            'user_notes_count': len(mr['notes']),
            # Not in GitLab: the key the stream knows this MR by, and the
            # retry policy's settings and state (see retry.py).
            'stream_key': mr.get('key'),
            'max_attempts': mr.get('max_attempts'),
            'escalated': mr.get('escalated_sha') == mr['sha'],
            'escalation': mr.get('escalation'),
        }

    def approvals_json(self, p, mr):
        return {
            'id': mr['id'], 'iid': mr['iid'], 'project_id': p['id'],
            'title': mr['title'], 'state': mr['state'],
            'approved': bool(mr['approved_by']),
            'approved_by': [{'user': user(u)} for u in mr['approved_by']],
        }

    def note_json(self, p, mr, note):
        return {
            'id': note['id'], 'body': note['body'],
            'author': user(note['author']),
            'created_at': note['created_at'],
            'updated_at': note['created_at'], 'system': False,
            'noteable_id': mr['id'], 'noteable_iid': mr['iid'],
            'noteable_type': 'MergeRequest', 'resolvable': False,
        }

    def branch_json(self, p, name, sha):
        return {
            'name': name, 'merged': False,
            'protected': name == p['default_branch'],
            'default': name == p['default_branch'],
            'developers_can_push': False, 'developers_can_merge': False,
            'can_push': True,
            'web_url': '%s/%s/-/tree/%s' % (self.baseurl, p['name'], name),
            'commit': {'id': sha, 'short_id': sha[:8]},
        }

    # -- webhooks -----------------------------------------------------------

    def _hook_mr_attrs(self, p, mr):
        attrs = copy.deepcopy(self.mr_json(p, mr))
        attrs.pop('author')
        attrs['author_id'] = user(mr['author'])['id']
        attrs['url'] = attrs.pop('web_url')
        attrs['labels'] = [{'title': label} for label in mr['labels']]
        attrs['last_commit'] = {
            'id': mr['sha'], 'url': '%s/%s/-/commit/%s' % (
                self.baseurl, p['name'], mr['sha'])}
        return attrs

    def _emit_mr(self, p, mr, action, by, changes=None, oldrev=None):
        attrs = self._hook_mr_attrs(p, mr)
        attrs['action'] = action
        if oldrev:
            attrs['oldrev'] = oldrev
        self.emit({
            'object_kind': 'merge_request', 'event_type': 'merge_request',
            'user': user(by), 'project': self.project_json(p),
            'repository': self._hook_repository(p),
            'object_attributes': attrs,
            'labels': attrs['labels'],
            'changes': changes or {},
        })

    def _emit_note(self, p, mr, note):
        self.emit({
            'object_kind': 'note', 'event_type': 'note',
            'user': user(note['author']), 'project': self.project_json(p),
            'repository': self._hook_repository(p),
            'object_attributes': {
                'id': note['id'], 'note': note['body'],
                'noteable_type': 'MergeRequest',
                'author_id': user(note['author'])['id'],
                'created_at': note['created_at'],
                'updated_at': note['created_at'],
                'project_id': p['id'],
                'url': '%s#note_%s' % (self.mr_url(p, mr), note['id']),
            },
            'merge_request': self._hook_mr_attrs(p, mr),
        })

    def _emit_push(self, p, ref, before, after, by):
        repo = self.repo(p)
        tag = ref.startswith('refs/tags/')
        commits = [] if tag else repo.commits(before, after)
        self.emit({
            'object_kind': 'tag_push' if tag else 'push',
            'event_name': 'tag_push' if tag else 'push',
            'before': before, 'after': after, 'ref': ref,
            'checkout_sha': after, 'user_username': by,
            'user_name': by, 'project_id': p['id'],
            'project': self.project_json(p),
            'repository': self._hook_repository(p),
            'commits': commits,
            'total_commits_count': (0 if tag else
                                    repo.count_commits(before, after)),
        })

    def _hook_repository(self, p):
        j = self.project_json(p)
        return {'name': j['name'], 'url': j['git_http_url'],
                'homepage': j['web_url'],
                'git_http_url': j['git_http_url']}

    # -- operations ----------------------------------------------------------

    def create_project(self, name, files=None, branch='master',
                       description='', by='git-stream'):
        """Create a project with an initial commit; no-op if it exists."""
        name = name.strip('/')
        if not name or name.endswith('.git') or '..' in name:
            raise ForgeError('invalid project name %r' % name)
        if not valid_branch(branch):
            raise ForgeError('invalid branch name %r; use a plain name such '
                             'as "master" or "release/1.0"' % (branch,))
        with self.lock:
            if name in self.state['projects']:
                return self.state['projects'][name]
            p = {'id': self.state['next_project_id'], 'name': name,
                 'default_branch': branch, 'description': description,
                 'created_at': now(), 'mrs': {}, 'keys': {},
                 'next_iid': 1}
            self.state['next_project_id'] += 1
            repo = Repo.create(self.repo(p).path, branch)
            files = files or {'README.md': '# %s\n' % name}
            sha = repo.commit(None, files, 'Initial commit', by,
                              '%s@git-stream.local' % by)
            repo.update_ref('refs/heads/' + branch, sha, ZERO_SHA)
            self.state['projects'][name] = p
            self.save()
            log.info('Created project %s', name)
            return p

    def push(self, project, files, message, by, branch=None, start=None):
        """Commit files to a branch (created from start if missing)."""
        with self.lock:
            p = self.project(project)
            branch = branch or p['default_branch']
            repo = self.repo(p)
            ref = 'refs/heads/' + branch
            before = repo.resolve(ref)
            parent = before or repo.resolve(start or p['default_branch'])
            if parent is None:
                raise ForgeError('no branch %s to start from' % (
                    start or p['default_branch']), 404)
            sha = repo.commit(parent, files, message or 'Update files', by,
                              '%s@git-stream.local' % by)
            repo.update_ref(ref, sha, before or ZERO_SHA)
            self._emit_push(p, ref, before or ZERO_SHA, sha, by)
            return {'project': p['name'], 'ref': ref, 'before':
                    before or ZERO_SHA, 'after': sha}

    def tag(self, project, tag, rev=None, by='git-stream'):
        with self.lock:
            p = self.project(project)
            repo = self.repo(p)
            sha = repo.resolve(rev or p['default_branch'])
            if sha is None:
                raise ForgeError('unknown revision %s' % rev, 404)
            ref = 'refs/tags/' + tag
            repo.update_ref(ref, sha, ZERO_SHA)
            self._emit_push(p, ref, ZERO_SHA, sha, by)
            return {'project': p['name'], 'ref': ref, 'after': sha}

    def open_mr(self, project, title, files, by, description='',
                target_branch=None, message=None, key=None, labels=(),
                max_attempts=None):
        with self.lock:
            p = self.project(project)
            if key is not None and str(key) in p['keys']:
                mr = p['mrs'][str(p['keys'][str(key)])]
                if mr['state'] == 'opened':
                    # Same key again (e.g. a redelivered message): update.
                    return self.update_mr(project, mr['iid'], by, files=files,
                                          title=title, message=message,
                                          description=description)
            target_branch = target_branch or p['default_branch']
            repo = self.repo(p)
            start = repo.resolve('refs/heads/' + target_branch)
            if start is None:
                raise ForgeError('no branch %s' % target_branch, 404)
            sha = repo.commit(start, files, message or title, by,
                              '%s@git-stream.local' % by)
            iid = p['next_iid']
            p['next_iid'] += 1
            repo.update_ref('refs/merge-requests/%d/head' % iid, sha)
            ts = now()
            mr = {
                'id': self.state['next_mr_id'], 'iid': iid,
                'title': title, 'description': description or '',
                'state': 'opened', 'target_branch': target_branch,
                'source_branch': 'mr-%d' % iid, 'sha': sha,
                'start_sha': start, 'author': by, 'labels': list(labels),
                'approved_by': [], 'notes': [], 'created_at': ts,
                'updated_at': ts, 'merged_at': None, 'closed_at': None,
                'merge_commit_sha': None,
                'key': None if key is None else str(key),
                'max_attempts': max_attempts, 'escalated_sha': None,
                'escalation': None,
            }
            self.state['next_mr_id'] += 1
            p['mrs'][str(iid)] = mr
            if key is not None:
                p['keys'][str(key)] = iid
            self._index_mr(p, mr)
            self.save()
            self._emit_mr(p, mr, 'open', by)
            return self.mr_json(p, mr)

    def update_mr(self, project, ref, by, files=None, title=None,
                  description=None, message=None, add_labels=(),
                  remove_labels=(), state_event=None):
        """Change an MR.  New files become a new commit on top of it."""
        with self.lock:
            p = self.project(project)
            mr = self.mr(p, ref)
            repo = self.repo(p)
            changes, oldrev = {}, None
            if files:
                if mr['state'] != 'opened':
                    raise ForgeError('merge request is %s' % mr['state'], 405)
                oldrev = mr['sha']
                sha = repo.commit(oldrev, files, message or 'Update', by,
                                  '%s@git-stream.local' % by)
                repo.update_ref('refs/merge-requests/%s/head' % mr['iid'],
                                sha, oldrev)
                mr['sha'] = sha
                # Like a rebase-free push: approvals are reset.
                mr['approved_by'] = []
            for field, value in (('title', title),
                                 ('description', description)):
                if value is not None and value != mr[field]:
                    changes[field] = {'previous': mr[field], 'current': value}
                    mr[field] = value
            labels = [x for x in mr['labels'] if x not in remove_labels]
            labels += [x for x in add_labels if x and x not in labels]
            if labels != mr['labels']:
                changes['labels'] = {
                    'previous': [{'title': x} for x in mr['labels']],
                    'current': [{'title': x} for x in labels]}
                mr['labels'] = labels
            action = 'update'
            if state_event == 'close' and mr['state'] == 'opened':
                mr['state'], mr['closed_at'], action = 'closed', now(), 'close'
            elif state_event == 'reopen' and mr['state'] == 'closed':
                mr['state'], mr['closed_at'], action = 'opened', None, 'reopen'
            elif not changes and not oldrev:
                return self.mr_json(p, mr)
            mr['updated_at'] = now()
            self._index_mr(p, mr)
            self.save()
            self._emit_mr(p, mr, action, by, changes, oldrev)
            return self.mr_json(p, mr)

    def comment(self, project, ref, body, by):
        with self.lock:
            p = self.project(project)
            mr = self.mr(p, ref)
            note = {'id': self.state['next_note_id'], 'body': body,
                    'author': by, 'created_at': now()}
            self.state['next_note_id'] += 1
            mr['notes'].append(note)
            self.save()
            self._emit_note(p, mr, note)
            return self.note_json(p, mr, note)

    def notes(self, project, ref):
        with self.lock:
            p = self.project(project)
            mr = self.mr(p, ref)
            return [self.note_json(p, mr, n) for n in mr['notes']]

    def approve(self, project, ref, by, sha=None):
        with self.lock:
            p = self.project(project)
            mr = self.mr(p, ref)
            if sha and sha != mr['sha']:
                raise ForgeError('SHA does not match HEAD of source branch: '
                                 '%s' % mr['sha'], 409)
            if by in mr['approved_by']:
                raise ForgeError('401 Unauthorized', 401)
            mr['approved_by'].append(by)
            mr['updated_at'] = now()
            self.save()
            self._emit_mr(p, mr, 'approved', by)
            return self.approvals_json(p, mr)

    def unapprove(self, project, ref, by):
        with self.lock:
            p = self.project(project)
            mr = self.mr(p, ref)
            if by not in mr['approved_by']:
                raise ForgeError('404 Not Found', 404)
            mr['approved_by'].remove(by)
            mr['updated_at'] = now()
            self.save()
            self._emit_mr(p, mr, 'unapproved', by)
            return self.approvals_json(p, mr)

    def merge(self, project, ref, by, squash=False):
        with self.lock:
            p = self.project(project)
            mr = self.mr(p, ref)
            if mr['state'] != 'opened':
                raise ForgeError('405 Method Not Allowed', 405)
            repo = self.repo(p)
            target_ref = 'refs/heads/' + mr['target_branch']
            before = repo.resolve(target_ref)
            tree = repo.merge_tree(before, mr['sha'])
            if tree is None:
                raise ForgeError('Branch cannot be merged', 406)
            email = '%s@git-stream.local' % by
            if squash:
                sha = repo.commit(before, None, mr['title'], by, email,
                                  tree=tree)
            else:
                sha = repo.commit(
                    before, None, "Merge branch '%s' into '%s'\n\n%s\n\n"
                    "See merge request %s!%s" % (
                        mr['source_branch'], mr['target_branch'],
                        mr['title'], p['name'], mr['iid']),
                    by, email, extra_parents=[mr['sha']], tree=tree)
            repo.update_ref(target_ref, sha, before)
            mr['state'] = 'merged'
            mr['merged_at'] = mr['updated_at'] = now()
            mr['merge_commit_sha'] = sha
            self.save()
            self._emit_push(p, target_ref, before, sha, by)
            self._emit_mr(p, mr, 'merge', by)
            return self.mr_json(p, mr)

    def escalations(self):
        """Every recorded escalation, newest first."""
        with self.lock:
            found = []
            for p in self.state['projects'].values():
                for mr in p['mrs'].values():
                    if mr.get('escalation'):
                        found.append(dict(
                            mr['escalation'], state=mr['state'],
                            current=mr.get('escalated_sha') == mr['sha']))
            return sorted(found, key=lambda e: -e['time'])

    def seen(self, message_id):
        """Remember a message id; True if it was processed before."""
        with self.lock:
            if message_id in self.state['seen']:
                return True
            self.state['seen'] = (self.state['seen'] + [message_id])[
                -SEEN_IDS:]
            self.save()
            return False
