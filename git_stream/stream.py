"""The stream: what other systems send to change the forge.

A message is a JSON object with an "action", sent to POST /api/stream.
Senders don't have to keep the replies: merge requests can be addressed
by a "key" chosen by the sender (e.g. a bundle id) instead of the iid the
forge assigns, and a message "id" makes a resent message harmless.

  {"action": "create_project", "project": "p", "files": {...}}
  {"action": "push", "project": "p", "branch": "master", "files": {...},
   "message": "..."}
  {"action": "tag", "project": "p", "tag": "v1", "rev": "master"}
  {"action": "open_mr", "project": "p", "key": "b-1", "title": "...",
   "description": "...", "files": {...}, "target_branch": "master",
   "max_attempts": 3}
  {"action": "update_mr", "project": "p", "mr": "b-1", "files": {...},
   "title": "...", "description": "..."}
  {"action": "comment", "project": "p", "mr": "b-1", "note": "recheck"}
  {"action": "label", "project": "p", "mr": 3, "add": [...], "remove": [...]}
  {"action": "approve" | "unapprove" | "close" | "reopen" | "merge",
   "project": "p", "mr": 3}

Every message may also carry "author" (default "git-stream") and "id".
In "files", a null value deletes the file.
"""

from git_stream.forge import ForgeError

DEFAULT_AUTHOR = 'git-stream'


def _require(msg, *names):
    for name in names:
        if msg.get(name) in (None, ''):
            raise ForgeError('%s: "%s" is required' % (msg['action'], name))


def _files(msg):
    files = msg.get('files') or {}
    if not isinstance(files, dict) or not all(
            isinstance(v, (str, type(None))) for v in files.values()):
        raise ForgeError('"files" must map paths to text (or null)')
    return files


def _int(msg, name):
    if msg.get(name) is None:
        return None
    try:
        return int(msg[name])
    except (TypeError, ValueError):
        raise ForgeError('"%s" must be a number' % name)


def _labels(value):
    if isinstance(value, str):
        value = value.split(',')
    return [x.strip() for x in value or [] if x and x.strip()]


def apply(forge, msg):
    """Apply one stream message to the forge and return the result."""
    if not isinstance(msg, dict):
        raise ForgeError('a message must be a JSON object')
    action = msg.get('action')
    if action not in ACTIONS:
        raise ForgeError('unknown action %r; expected one of %s' % (
            action, ', '.join(sorted(ACTIONS))))
    if msg.get('id') is not None and forge.seen('%s' % msg['id']):
        return {'duplicate': True, 'id': msg['id']}
    _require(msg, 'project')
    by = msg.get('author') or DEFAULT_AUTHOR
    return ACTIONS[action](forge, msg, by)


def _create_project(forge, msg, by):
    p = forge.create_project(msg['project'], _files(msg) or None,
                             msg.get('branch') or 'master',
                             msg.get('description', ''), by)
    return forge.project_json(p)


def _push(forge, msg, by):
    _require(msg, 'files')
    return forge.push(msg['project'], _files(msg), msg.get('message'), by,
                      branch=msg.get('branch'), start=msg.get('start'))


def _tag(forge, msg, by):
    _require(msg, 'tag')
    return forge.tag(msg['project'], msg['tag'], msg.get('rev'), by)


def _open_mr(forge, msg, by):
    _require(msg, 'title')
    return forge.open_mr(
        msg['project'], msg['title'], _files(msg), by,
        description=msg.get('description', ''),
        target_branch=msg.get('target_branch'),
        message=msg.get('message'), key=msg.get('key'),
        labels=_labels(msg.get('labels')),
        max_attempts=_int(msg, 'max_attempts'))


def _update_mr(forge, msg, by):
    _require(msg, 'mr')
    return forge.update_mr(
        msg['project'], msg['mr'], by, files=_files(msg),
        title=msg.get('title'), description=msg.get('description'),
        message=msg.get('message'))


def _comment(forge, msg, by):
    _require(msg, 'mr', 'note')
    return forge.comment(msg['project'], msg['mr'], msg['note'], by)


def _label(forge, msg, by):
    _require(msg, 'mr')
    return forge.update_mr(msg['project'], msg['mr'], by,
                           add_labels=_labels(msg.get('add')),
                           remove_labels=_labels(msg.get('remove')))


def _state(event):
    def run(forge, msg, by):
        _require(msg, 'mr')
        return forge.update_mr(msg['project'], msg['mr'], by,
                               state_event=event)
    return run


def _approve(forge, msg, by):
    _require(msg, 'mr')
    return forge.approve(msg['project'], msg['mr'], by)


def _unapprove(forge, msg, by):
    _require(msg, 'mr')
    return forge.unapprove(msg['project'], msg['mr'], by)


def _merge(forge, msg, by):
    _require(msg, 'mr')
    return forge.merge(msg['project'], msg['mr'], by,
                       squash=bool(msg.get('squash')))


ACTIONS = {
    'create_project': _create_project,
    'push': _push,
    'tag': _tag,
    'open_mr': _open_mr,
    'update_mr': _update_mr,
    'comment': _comment,
    'label': _label,
    'close': _state('close'),
    'reopen': _state('reopen'),
    'approve': _approve,
    'unapprove': _unapprove,
    'merge': _merge,
}
