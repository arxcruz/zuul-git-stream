"""The fake forge: projects, merge requests, notes and approvals.

All state lives in one JSON file next to the bare repositories.  Every
change made through the stream (actions sent by other systems) or the
GitLab API (calls made by Zuul) emits the webhook GitLab would send.
"""

import copy
import datetime
import json
import logging
import os
import threading
import zlib

from git_stream.repos import Repo, ZERO_SHA

log = logging.getLogger('git_stream.forge')

# How many processed message ids to remember for de-duplication.
SEEN_IDS = 1000


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


class Forge:
    def __init__(self, data_dir, baseurl, emit=None, users=None,
                 stream_tokens=None):
        """emit(payload) is called with every webhook payload, in order.

        users maps API tokens to user names; with none configured, any
        token is accepted and acts as the user "zuul".  stream_tokens does
        the same for POST /api/stream senders.
        """
        self.data_dir = data_dir
        self.repo_root = os.path.join(data_dir, 'repos')
        self.state_file = os.path.join(data_dir, 'state.json')
        self.baseurl = baseurl.rstrip('/')
        self.emit = emit or (lambda payload: None)
        self.users = users or {}
        self.stream_tokens = stream_tokens or {}
        self.lock = threading.RLock()
        os.makedirs(self.repo_root, exist_ok=True)
        self.state = {'projects': {}, 'next_project_id': 1, 'next_mr_id': 1,
                      'next_note_id': 1, 'seen': []}
        if os.path.exists(self.state_file):
            with open(self.state_file) as f:
                self.state.update(json.load(f))

    # -- persistence and lookups ------------------------------------------

    def save(self):
        tmp = self.state_file + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(self.state, f, indent=1, sort_keys=True)
        os.replace(tmp, self.state_file)

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
                'updated_at': ts, 'key': None if key is None else str(key),
                'max_attempts': max_attempts,
            }
            self.state['next_mr_id'] += 1
            p['mrs'][str(iid)] = mr
            if key is not None:
                p['keys'][str(key)] = iid
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
