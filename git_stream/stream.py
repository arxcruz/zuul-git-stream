"""The stream: what other systems send to change the forge.

A message is a JSON object with an "action", sent to POST /api/stream.
Senders don't have to keep the replies: merge requests can be addressed
by a "key" chosen by the sender (e.g. a bundle id) instead of the iid the
forge assigns, and a message "id" makes a resent message harmless.

  {"action": "create_project", "project": "p"}
  {"action": "push", "project": "p", "branch": "master", "data": {...},
   "message": "..."}
  {"action": "tag", "project": "p", "tag": "v1", "rev": "master"}
  {"action": "open_mr", "project": "p", "key": "b-1", "title": "...",
   "description": "...", "data": {...}, "target_branch": "master",
   "max_attempts": 3, "callback_url": "https://announcer.example.com/hook",
   "data": {...}}
  {"action": "update_mr", "project": "p", "mr": "b-1",
   "title": "...", "description": "...", "data": {...}}
  {"action": "comment", "project": "p", "mr": "b-1", "note": "recheck"}
  {"action": "label", "project": "p", "mr": 3, "add": [...], "remove": [...]}
  {"action": "approve" | "unapprove" | "close" | "reopen" | "merge",
   "project": "p", "mr": 3}

Every message may also carry "author" and "id".  Who a message acts as
comes from the sender's token, not from the message:

- with stream tokens configured, a message acts as the user its token
  names; "author" may repeat that user but naming anyone else is a 403;
- on an open stream (no tokens) the sender is "git-stream" and "author"
  may be any name except the users the retry policy trusts as Zuul's own
  (`[retry] report_users`), which is a 403.

A "callback_url" (an http or https URL of up to 2048 characters, else 400
before anything is created) makes git-stream POST the final outcome of the
merge request there: "passed" when Zuul approves it, "escalated" when the
retries run out, each at most once per sha.  One attempt, in the background;
a redelivered open_mr keeps the first URL.  See the README, "Callbacks".

Senders send messages, not files: "files" in any message is a 400 that
says to send "data".  A "data" object (open_mr, update_mr, push; at most
64 KiB and 8 levels deep, else 400) is kept with the version it came with.
Every change of the data is a new commit, so Zuul sees a changed MR, and jobs
read it back from GET /api/v4/projects/<p>/merge_requests/<iid>/data
(``?sha=`` for an older version).  Merging the MR, or pushing to the default
branch, makes it the project's current data, at GET /api/v4/projects/<p>/data.

open_mr creates a project that does not exist yet (a placeholder: one commit
with an empty tree), as does create_project; every other action on a missing
project, push included, is a 404.

Branch and tag names must be ones git accepts (400 otherwise); tagging an
existing name is a 409.
"""

import json

from git_stream.forge import ForgeError

# "data" is a JSON object of at most this many bytes, nested at most this deep.
DATA_MAX_BYTES = 65536
DATA_MAX_DEPTH = 8


def _require(msg, *names):
    for name in names:
        if msg.get(name) in (None, ''):
            raise ForgeError('%s: "%s" is required' % (msg['action'], name))


def _no_files(msg):
    """Refuse "files": senders send "data", never file content."""
    if msg.get('files') is not None:
        raise ForgeError('"files" is not accepted: send "data", a JSON '
                         'object, instead (git-stream keeps no sender '
                         'files)')


def _deeper_than(value, limit):
    """True if value nests dicts and lists more than limit levels deep.

    Walks with a stack, not recursion: a body nested thousands of levels
    deep must be a 400, not a RecursionError.
    """
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            children = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        if depth + 1 > limit:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def _data(msg):
    """The message's "data" (a bounded JSON object), or None."""
    data = msg.get('data')
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ForgeError('"data" must be a JSON object')
    if _deeper_than(data, DATA_MAX_DEPTH):
        raise ForgeError('"data" is nested more than %d levels deep'
                         % DATA_MAX_DEPTH)
    if len(json.dumps(data).encode('utf8')) > DATA_MAX_BYTES:
        raise ForgeError('"data" is larger than %d bytes' % DATA_MAX_BYTES)
    return data


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


def _author(forge, msg, sender):
    """Who msg acts as: its sender, unless the sender may speak for a name.

    The one place identity is decided.  Zuul's reports are recognised by
    their author (see retry.py), so a forged "Build failed." from "zuul"
    would drive the retry and escalation path.
    """
    author = msg.get('author') or sender
    if not isinstance(author, str):
        raise ForgeError('"author" must be a string')
    if author == sender:
        return author
    if forge.stream_tokens:
        raise ForgeError('token for %r may not act as %r' % (sender, author),
                         403)
    if author in forge.report_users:
        raise ForgeError('%r is reserved for Zuul\'s reports and may not '
                         'be used as an author' % author, 403)
    return author


def apply(forge, msg, sender):
    """Apply one stream message, sent by sender, and return the result.

    Every message is logged (forge.record_message) as received before
    anything acts on it, then given its outcome: applied, duplicate, refused
    (403), rejected (another 4xx) or error (a bug).  The log is written
    first so it never lags behind what Zuul can see.
    """
    entry = forge.record_message(sender, msg)
    try:
        result = _apply(forge, msg, sender)
    except ForgeError as e:
        forge.finish_message(entry, 'refused' if e.status == 403
                             else 'rejected', e.status, str(e))
        raise
    except Exception as e:
        forge.finish_message(entry, 'error', 500, type(e).__name__)
        raise
    duplicate = isinstance(result, dict) and result.get('duplicate') is True
    forge.finish_message(entry, 'duplicate' if duplicate else 'applied', 200)
    return result


def _apply(forge, msg, sender):
    if not isinstance(msg, dict):
        raise ForgeError('a message must be a JSON object')
    action = msg.get('action')
    if action not in ACTIONS:
        raise ForgeError('unknown action %r; expected one of %s' % (
            action, ', '.join(sorted(ACTIONS))))
    by = _author(forge, msg, sender)  # before seen(): a refusal isn't a send
    if msg.get('id') is not None and forge.seen('%s' % msg['id']):
        return {'duplicate': True, 'id': msg['id']}
    _require(msg, 'project')
    if (not isinstance(msg['project'], (str, int))
            or isinstance(msg['project'], bool)):
        raise ForgeError('"project" must be a project name')
    return ACTIONS[action](forge, msg, by)


def _create_project(forge, msg, by):
    _no_files(msg)
    p = forge.create_project(msg['project'], None,
                             msg.get('branch') or 'master',
                             msg.get('description', ''), by)
    return forge.project_json(p)


def _push(forge, msg, by):
    _no_files(msg)
    if msg.get('data') is None:
        raise ForgeError('push: "data" is required')
    return forge.push(msg['project'], {}, msg.get('message'), by,
                      branch=msg.get('branch'), start=msg.get('start'),
                      data=_data(msg))


def _tag(forge, msg, by):
    _require(msg, 'tag')
    return forge.tag(msg['project'], msg['tag'], msg.get('rev'), by)


def _open_mr(forge, msg, by):
    _require(msg, 'title')
    _no_files(msg)
    return forge.open_mr(
        msg['project'], msg['title'], {}, by,
        description=msg.get('description', ''),
        target_branch=msg.get('target_branch'),
        message=msg.get('message'), key=msg.get('key'),
        labels=_labels(msg.get('labels')),
        max_attempts=_int(msg, 'max_attempts'),
        callback_url=msg.get('callback_url'), data=_data(msg))


def _update_mr(forge, msg, by):
    _require(msg, 'mr')
    _no_files(msg)
    return forge.update_mr(
        msg['project'], msg['mr'], by,
        title=msg.get('title'), description=msg.get('description'),
        message=msg.get('message'), data=_data(msg))


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
