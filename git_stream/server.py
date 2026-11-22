"""HTTP front end of the forge.

  /api/v4/...                 the part of GitLab's REST API Zuul uses
  /<project>.git/...          read-only git over HTTP (git http-backend)
  /api/stream                 POST stream messages (see stream.py)
  /api/stream/deliveries      recent webhook deliveries
  /api/stream/escalations     merge requests escalated by the retry policy
  /api/stream/messages        the log of every stream message (stream token)
  /, /escalations, /<project>, /<project>/merge_requests/<iid>
                              small web pages to look at the forge
  /health
"""

import datetime
import html
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse
import http.server

from git_stream import stream
from git_stream.forge import ForgeError

log = logging.getLogger('git_stream.server')

API = '/api/v4/projects'
NO_API_TOKENS = ('no API tokens are configured, so writes are refused; add '
                 '[api_tokens] (or set [server] insecure = true for a demo)')
NO_STREAM_TOKENS = ('no stream tokens are configured, so the stream is '
                    'refused; add [stream] tokens (or set [server] insecure '
                    '= true for a demo)')
# Request limits (see [server] in config.py): a body over MAX_BODY bytes is
# refused, a client silent for READ_TIMEOUT seconds is dropped, and a whole
# body must arrive within BODY_TIMEOUT seconds however steadily it trickles.
MAX_BODY = 10 * 1024 * 1024
READ_TIMEOUT = 30
BODY_TIMEOUT = 120
# At most MAX_CONNECTIONS connections are served at once; another is refused
# with a 503 that is written within REFUSAL_TIMEOUT seconds, so the accept
# loop never waits on a client.
MAX_CONNECTIONS = 64
# GET /api/stream/messages pages: the default and the largest page.
MESSAGE_PAGE_DEFAULT = 100
MESSAGE_PAGE_MAX = 200
REFUSAL_TIMEOUT = 1
_BUSY = b'server busy: too many connections, retry shortly\n'
REFUSAL = (b'HTTP/1.1 503 Service Unavailable\r\n'
           b'Retry-After: 1\r\n'
           b'Connection: close\r\n'
           b'Content-Type: text/plain; charset=utf-8\r\n'
           b'Content-Length: %d\r\n\r\n' % len(_BUSY)) + _BUSY
# A chunk size line is hex digits and an optional ";extension"; a line
# longer than this is not one.
CHUNK_LINE_MAX = 128
CHUNK_RE = re.compile(rb'([0-9a-fA-F]+)(?:;[^\r\n]*)?')
GIT_RE = re.compile(r'^/(?P<repo>.+?\.git)/(?P<rest>info/refs|'
                    r'git-upload-pack|git-receive-pack|HEAD)$')
MR_PAGE_RE = re.compile(r'^/(?P<project>.+?)/(?:-/)?merge_requests/'
                        r'(?P<iid>[0-9]{1,9})/?$')


def _number(raw, name):
    """A positive integer from a request, or a 400 that names it."""
    if not re.fullmatch(r'[0-9]{1,9}', str(raw)) or int(raw) < 1:
        raise ForgeError('%s must be a positive number' % name)
    return int(raw)


def make_handler(max_body=MAX_BODY, read_timeout=READ_TIMEOUT,
                 body_timeout=BODY_TIMEOUT):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = 'git-stream'
        # Applied to the socket: a silent client is dropped while waiting
        # for a request line or between keep-alive requests.
        timeout = read_timeout

        def handle(self):
            # A client that vanished (reset, broken pipe, or not reading its
            # reply) is not an error worth a traceback.
            try:
                super().handle()
            except (ConnectionError, TimeoutError) as e:
                log.debug('%s: connection lost: %s', self.address_string(), e)
                self.close_connection = True

        def log_message(self, fmt, *args):
            log.debug('%s %s', self.address_string(), fmt % args)

        @property
        def forge(self):
            return self.server.forge

        @property
        def sender(self):
            return getattr(self.server, 'sender', None)

        @property
        def metrics(self):
            return getattr(self.server, 'metrics', None)

        # -- plumbing --------------------------------------------------------

        def _send(self, code, body, ctype, headers=None):
            if isinstance(body, str):
                body = body.encode('utf8')
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def _json(self, data, code=200, headers=None):
            self._send(code, json.dumps(data, indent=1), 'application/json',
                       headers)

        def _error(self, code, message):
            self._json({'message': message}, code)

        # A body is read once, before the forge's lock is taken, so a slow
        # client can't hold it.  Every refusal closes the connection: the
        # rest of the body is still in the stream.

        def _fail(self, status, message):
            self.close_connection = True
            raise ForgeError(message, status)

        def _read(self, size, deadline):
            """size bytes of the body (fewer if the client closes).  Each
            receive waits at most read_timeout, and none past the deadline."""
            data = bytearray()
            while len(data) < size:
                left = deadline - time.monotonic()
                if left <= 0:
                    self._fail(408, 'request timed out')
                self.connection.settimeout(min(read_timeout, left))
                try:
                    more = self.rfile.read1(min(size - len(data), 65536))
                except TimeoutError:
                    self._fail(408, 'request timed out')
                if not more:
                    break
                data += more
            return bytes(data)

        def _read_exactly(self, size, deadline):
            data = self._read(size, deadline)
            if len(data) < size:
                self._fail(400, 'incomplete request body')
            return data

        def _readline(self, deadline):
            line = b''
            while not line.endswith(b'\n'):
                if len(line) >= CHUNK_LINE_MAX:
                    self._fail(400, 'invalid chunked body')
                byte = self._read(1, deadline)
                if not byte:
                    break
                line += byte
            return line

        def _read_chunked(self, deadline):
            data = bytearray()
            while True:
                line = self._readline(deadline)
                if not line:
                    return bytes(data)  # connection closed mid-body
                m = CHUNK_RE.fullmatch(line.rstrip(b'\r\n'))
                if not m:
                    self._fail(400, 'invalid chunked body: bad chunk size')
                size = int(m.group(1), 16)
                if len(data) + size > max_body:
                    self._fail(413, self._too_large())
                if size == 0:
                    while self._readline(deadline).strip():
                        pass  # trailers
                    return bytes(data)
                data += self._read_exactly(size, deadline)
                if self._readline(deadline).strip():
                    self._fail(400, 'invalid chunked body: bad chunk end')

        def _too_large(self):
            return 'request body too large: the limit is %d bytes' % max_body

        def _read_body(self):
            deadline = time.monotonic() + body_timeout
            try:
                if 'chunked' in self.headers.get('Transfer-Encoding', ''):
                    return self._read_chunked(deadline)
                length = self.headers.get('Content-Length')
                if length is None:
                    return b''
                if not re.fullmatch(r'[0-9]+', length):
                    self._fail(400, 'invalid Content-Length')
                if len(length) > 18 or int(length) > max_body:
                    self._fail(413, self._too_large())
                return self._read_exactly(int(length), deadline)
            finally:
                self.connection.settimeout(read_timeout)

        def _body(self):
            """The request body, bounded by max_body and body_timeout."""
            if self._raw is None:
                self._raw = self._read_body()
            return self._raw

        def _params(self, query):
            """Request parameters: query string, form or JSON body."""
            params = {k: v[-1] for k, v in urllib.parse.parse_qs(
                query, keep_blank_values=True).items()}
            if self.command in ('POST', 'PUT'):
                body = self._body()
                ctype = self.headers.get('Content-Type', '')
                if body and 'json' in ctype:
                    try:
                        parsed = json.loads(body)
                    except (ValueError, RecursionError):
                        raise ForgeError('invalid JSON body')
                    if not isinstance(parsed, dict):
                        raise ForgeError('the JSON body must be an object')
                    params.update(parsed)
                elif body:
                    try:
                        text = body.decode('utf8')
                    except UnicodeDecodeError:
                        raise ForgeError('the body is not valid UTF-8')
                    params.update({k: v[-1] for k, v in urllib.parse.parse_qs(
                        text, keep_blank_values=True).items()})
            return params

        def _user(self):
            """The user an API token belongs to (None if unknown)."""
            auth = self.headers.get('Authorization', '')
            token = auth[7:] if auth.startswith('Bearer ') else \
                self.headers.get('PRIVATE-TOKEN')
            if token is None:
                return None
            return self.forge.user_for_token(token)

        def _stream_user(self):
            """The sender a stream token names, or None.  With no stream
            tokens configured, that is the git-stream user in insecure mode
            and nobody otherwise."""
            auth = self.headers.get('Authorization', '')
            if auth.startswith('Bearer '):
                token = auth[len('Bearer '):]
            elif self.headers.get('X-Stream-Token'):
                token = self.headers['X-Stream-Token']
            else:
                token = None
            return self.forge.stream_user_for_token(token)

        def _dispatch(self):
            url = urllib.parse.urlsplit(self.path)
            path = url.path
            self._raw = None
            try:
                if self.command in ('POST', 'PUT'):
                    self._body()
                if path == '/health':
                    return self._json({'status': 'ok'})
                m = GIT_RE.match(path)
                if m:
                    return self._git(m.group('repo'), path, url.query)
                # The forge's lock is re-entrant; holding it here keeps
                # readers from seeing the state while it changes.
                with self.forge.lock:
                    if path.startswith(API):
                        return self._api(path[len(API):], url.query)
                    if path.startswith('/api/stream'):
                        return self._stream(path, url.query)
                    if self.command in ('GET', 'HEAD'):
                        return self._page(path)
                self._error(404, '404 Not Found')
            except ForgeError as e:
                self._error(e.status, str(e))
            except (ConnectionError, TimeoutError):
                raise  # the client is gone: handle() drops the connection
            except Exception:
                log.exception('Error handling %s %s', self.command, self.path)
                self._error(500, 'internal error')

        do_GET = do_POST = do_PUT = do_HEAD = _dispatch

        # -- GitLab API ------------------------------------------------------

        def _api(self, path, query):
            params = self._params(query)
            parts = path.strip('/').split('/') if path.strip('/') else []
            if not parts:
                if self.command != 'GET':
                    return self._error(405, '405 Method Not Allowed')
                return self._json([self.forge.project_json(p) for p in
                                   self.forge.state['projects'].values()])
            project = urllib.parse.unquote(parts[0])
            rest = parts[1:]
            p = self.forge.project(project)
            if self.command != 'GET':
                by = self._user()
                if by is None:
                    if not self.forge.users:
                        return self._error(401, NO_API_TOKENS)
                    return self._error(401, '401 Unauthorized')

            if not rest:
                return self._json(self.forge.project_json(p))
            if rest == ['data'] and self.command == 'GET':
                return self._json(self.forge.project_data(
                    p['name'], params.get('sha')))
            if rest[:2] == ['repository', 'branches']:
                return self._branches(p, rest[2:], params)
            if (rest[:2] == ['repository', 'files'] and len(rest) > 3 and
                    rest[-1] == 'raw' and self.command == 'GET'):
                content = self.forge.repo(p).read_file(
                    params.get('ref', p['default_branch']),
                    urllib.parse.unquote('/'.join(rest[2:-1])))
                if content is None:
                    return self._error(404, '404 File Not Found')
                return self._send(200, content, 'text/plain; charset=utf-8')
            if rest[0] != 'merge_requests':
                return self._error(404, '404 Not Found')
            if len(rest) == 1:
                return self._list_mrs(p, params)
            mr = self.forge.mr(p, _number(rest[1], 'the merge request id'))
            sub = rest[2] if len(rest) > 2 else None
            if self.command == 'GET':
                if sub is None:
                    return self._json(self.forge.mr_json(p, mr))
                if sub == 'approvals':
                    return self._json(self.forge.approvals_json(p, mr))
                if sub == 'data':
                    return self._json(self.forge.mr_data(
                        p['name'], mr['iid'], params.get('sha')))
                if sub == 'notes':
                    notes = self.forge.notes(p['name'], mr['iid'])
                    if params.get('sort', 'desc') == 'desc':
                        notes.reverse()
                    return self._json(notes)
            elif self.command == 'POST':
                if sub == 'notes':
                    return self._json(self.forge.comment(
                        p['name'], mr['iid'], params.get('body', ''), by), 201)
                if sub == 'approve':
                    return self._json(self.forge.approve(
                        p['name'], mr['iid'], by, params.get('sha')), 201)
                if sub == 'unapprove':
                    return self._json(self.forge.unapprove(
                        p['name'], mr['iid'], by), 201)
            elif self.command == 'PUT':
                if sub == 'merge':
                    squash = str(params.get('squash', '')).lower() in (
                        'true', '1')
                    return self._json(self.forge.merge(p['name'], mr['iid'], by,
                                                  squash=squash))
                if sub is None:
                    add = stream._labels(params.get('add_labels'))
                    remove = stream._labels(params.get('remove_labels'))
                    if 'labels' in params:
                        wanted = stream._labels(params['labels'])
                        add += wanted
                        remove += [x for x in mr['labels']
                                   if x not in wanted]
                    return self._json(self.forge.update_mr(
                        p['name'], mr['iid'], by,
                        title=params.get('title'),
                        description=params.get('description'),
                        add_labels=add, remove_labels=remove,
                        state_event=params.get('state_event')))
            self._error(405, '405 Method Not Allowed')

        def _branches(self, p, rest, params):
            if self.command != 'GET':
                return self._error(405, '405 Method Not Allowed')
            branches = self.forge.repo(p).branches()
            if rest:
                name = urllib.parse.unquote('/'.join(rest))
                if name not in branches:
                    return self._error(404, '404 Branch Not Found')
                return self._json(self.forge.branch_json(p, name, branches[name]))
            page = _number(params.get('page', 1), 'page')
            per_page = _number(params.get('per_page', 20), 'per_page')
            names = sorted(branches)[(page - 1) * per_page:page * per_page]
            return self._json([self.forge.branch_json(p, n, branches[n])
                               for n in names])

        def _list_mrs(self, p, params):
            if self.command != 'GET':
                return self._error(405, '405 Method Not Allowed')
            state = params.get('state', 'all')
            mrs = sorted(p['mrs'].values(), key=lambda m: -m['iid'])
            per_page = _number(params.get('per_page', 20), 'per_page')
            return self._json([self.forge.mr_json(p, m) for m in mrs
                               if state in ('all', m['state'])][:per_page])

        # -- stream ----------------------------------------------------------

        def _stream(self, path, query):
            if path == '/api/stream/deliveries' and self.command == 'GET':
                return self._json(list(self.sender.recent)
                                  if self.sender else [])
            if path == '/api/stream/escalations' and self.command == 'GET':
                return self._json(self.forge.escalations())
            if path == '/api/stream/metrics' and self.command == 'GET':
                if self.metrics is None:
                    return self._error(404, 'metrics not enabled')
                params = self._params(query)
                return self._json(self.metrics.summary(
                    params.get('project'), _days(params)))
            if path == '/api/stream/messages' and self.command == 'GET':
                # Bodies may carry callback URLs: same token as the stream.
                if self._stream_user() is None:
                    return self._unauthorized_stream()
                return self._json(self._messages(self._params(query)))
            if path != '/api/stream' or self.command != 'POST':
                return self._error(404, '404 Not Found')
            sender = self._stream_user()
            if sender is None:
                return self._unauthorized_stream()
            try:
                msgs = json.loads(self._body() or b'null')
            except (ValueError, RecursionError):
                return self._error(400, 'invalid JSON body')
            if isinstance(msgs, list):
                results = []
                for msg in msgs:
                    try:
                        results.append(stream.apply(self.forge, msg, sender))
                    except ForgeError as e:
                        results.append({'error': str(e)})
                return self._json(results)
            return self._json(stream.apply(self.forge, msgs, sender))

        def _unauthorized_stream(self):
            if not self.forge.stream_tokens:
                return self._error(401, NO_STREAM_TOKENS)
            return self._error(401, '401 Unauthorized')

        def _messages(self, params):
            """GET /api/stream/messages: the logged messages, newest first.

            Filters: project, key, action, outcome, since, until (ISO times);
            paging: limit (at most MESSAGE_PAGE_MAX) and before (a message id,
            the "next" of the previous page).
            """
            filters = {}
            for name in ('project', 'key', 'action', 'outcome', 'since',
                         'until'):
                if name in params:
                    if len(params[name]) > 200:
                        raise ForgeError('%s is too long' % name)
                    filters[name] = params[name]
            limit = min(_number(params.get('limit', MESSAGE_PAGE_DEFAULT),
                                'limit'), MESSAGE_PAGE_MAX)
            before = (_number(params['before'], 'before')
                      if 'before' in params else None)
            found = self.forge.messages(filters, limit, before)
            return {'messages': found,
                    'next': found[-1]['id'] if len(found) == limit else None}

        # -- git over HTTP ---------------------------------------------------

        def _git(self, repo, path, query):
            if repo[:-4] not in self.forge.state['projects']:
                return self._send(404, 'Repository not found\n', 'text/plain')
            if 'receive-pack' in path or 'receive-pack' in query:
                return self._send(403, 'This forge is read-only over git; '
                                      'send a "push" stream message instead.\n',
                                  'text/plain')
            body = self._body() if self.command == 'POST' else b''  # cached
            env = dict(os.environ,
                       GIT_PROJECT_ROOT=self.forge.repo_root,
                       GIT_HTTP_EXPORT_ALL='1',
                       PATH_INFO=path, QUERY_STRING=query,
                       REQUEST_METHOD=self.command,
                       CONTENT_TYPE=self.headers.get('Content-Type', ''),
                       CONTENT_LENGTH=str(len(body)),
                       REMOTE_ADDR=self.client_address[0])
            if self.headers.get('Content-Encoding'):
                env['HTTP_CONTENT_ENCODING'] = self.headers['Content-Encoding']
            if self.headers.get('Git-Protocol'):
                env['GIT_PROTOCOL'] = self.headers['Git-Protocol']
            proc = subprocess.run(['git', 'http-backend'], input=body,
                                  env=env, capture_output=True)
            head, _, out = proc.stdout.partition(b'\r\n\r\n')
            if not _:
                head, _, out = proc.stdout.partition(b'\n\n')
            code, headers = 200, {}
            for line in head.decode('latin1').splitlines():
                key, _, value = line.partition(':')
                if key.lower() == 'status':
                    code = int(value.split()[0])
                elif key:
                    headers[key] = value.strip()
            ctype = headers.pop('Content-Type', 'text/plain')
            headers.pop('Content-Length', None)
            self._send(code, out, ctype, headers)

        # -- web pages -------------------------------------------------------

        def _page(self, path):
            path = urllib.parse.unquote(path)
            m = MR_PAGE_RE.match(path)
            if m and m.group('project') in self.forge.state['projects']:
                p = self.forge.project(m.group('project'))
                return self._html(*mr_page(self.forge, p, self.forge.mr(
                    p, int(m.group('iid')))))
            name = path.strip('/')
            if not name:
                return self._html(*index_page(self.forge))
            if name == 'escalations':
                return self._html(*escalations_page(self.forge))
            if name == 'metrics':
                if self.metrics is None:
                    return self._error(404, 'metrics not enabled')
                return self._html(*metrics_page(self.metrics,
                                                self._params(
                                                    urllib.parse.urlsplit(path)
                                                    .query)))
            if name in self.forge.state['projects']:
                return self._html(*project_page(self.forge, self.forge.project(name)))
            self._send(404, 'Not found\n', 'text/plain')

        def _html(self, title, body):
            self._send(200, PAGE % {'title': html.escape(title),
                                    'body': body},
                       'text/html; charset=utf-8')

    return Handler


class Server(http.server.ThreadingHTTPServer):
    # The default of 5 refuses (on macOS, resets) a burst of connections
    # that the accept loop has not reached yet.
    request_queue_size = 128

    def __init__(self, address, handler, max_connections=MAX_CONNECTIONS):
        # One slot per connection, taken on the accept thread before a
        # handler thread exists and given back when that thread ends.  A
        # BoundedSemaphore raises on a second release, so a slot freed twice
        # is a loud bug rather than a silently higher cap.
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self._refuse(request, client_address)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            # The thread never ran (it could not be started), so nothing
            # else will give the slot back.
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def _refuse(self, request, client_address):
        """Answer 503 and close, on the accept thread, without waiting for
        a client that will not read."""
        try:
            request.settimeout(REFUSAL_TIMEOUT)
            request.sendall(REFUSAL)
        except OSError:
            pass
        log.debug('%s: refused, too many connections', client_address[0])
        self.shutdown_request(request)


def serve(forge, sender, host, port, metrics=None, max_body=MAX_BODY,
          read_timeout=READ_TIMEOUT, body_timeout=BODY_TIMEOUT,
          max_connections=MAX_CONNECTIONS):
    handler = make_handler(max_body, read_timeout, body_timeout)
    server = Server((host, port), handler, max_connections)
    server.daemon_threads = True
    server.forge = forge
    server.sender = sender
    server.metrics = metrics
    return server


def _days(params):
    """The ?days=N window for the metrics endpoint (None = all time)."""
    raw = params.get('days')
    if raw in (None, ''):
        return None
    try:
        return max(0, float(raw))
    except ValueError:
        return None


# -- HTML ---------------------------------------------------------------------

e = html.escape

PAGE = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s</title>
<style>
:root { --bg:#f6f7f9; --panel:#fff; --ink:#1f2430; --muted:#6b7385;
  --line:#e3e6ec; --accent:#6b4fbb; --ok:#2f9e44; --bad:#e03131;
  --code:#f1f2f6; }
@media (prefers-color-scheme: dark) { :root { --bg:#14161c; --panel:#1c1f27;
  --ink:#e6e8ee; --muted:#9aa1b2; --line:#2b2f3a; --accent:#a48df0;
  --ok:#51cf66; --bad:#ff6b6b; --code:#12141a; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
header { padding:14px 20px; border-bottom:1px solid var(--line);
  background:var(--panel); }
header a { color:var(--ink); font-weight:700; text-decoration:none; }
header a.nav { float:right; font-weight:600; color:var(--accent); }
.escalation { border-left:4px solid var(--bad); }
.escalation h2 { color:var(--bad); }
.old { opacity:.6; }
main { max-width:960px; margin:0 auto; padding:20px 16px; }
a { color:var(--accent); }
h1 { font-size:22px; margin:0 0 6px; }
.meta { color:var(--muted); font-size:13px; }
.box { background:var(--panel); border:1px solid var(--line);
  border-radius:10px; padding:14px 16px; margin:14px 0; }
.box h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); margin:0 0 8px; }
pre { white-space:pre-wrap; word-break:break-word; margin:0;
  font:13px/1.45 ui-monospace,Menlo,monospace; }
.pill { display:inline-block; font-size:12px; font-weight:700;
  padding:1px 9px; border-radius:999px; border:1px solid var(--line); }
.opened { color:var(--ok); } .merged { color:var(--accent); }
.closed { color:var(--bad); }
.note { border-top:1px solid var(--line); padding:10px 0; }
.note:first-of-type { border-top:0; }
table { border-collapse:collapse; width:100%%; }
td, th { text-align:left; padding:6px 4px; border-top:1px solid var(--line);
  font-size:14px; }
th { border-top:0; color:var(--muted); font-size:12px; }
</style></head>
<body><header><a href="/">git-stream</a>
<span class="meta">&nbsp; a GitLab-compatible forge fed by events</span>
<a class="nav" href="/escalations">Escalations</a>
</header><main>%(body)s</main></body></html>
'''


def index_page(forge):
    rows = ''.join(
        '<tr><td><a href="/%s">%s</a></td><td>%d</td><td>%d</td>'
        '<td class="meta"><code>git clone %s/%s.git</code></td></tr>' % (
            e(p['name']), e(p['name']),
            sum(m['state'] == 'opened' for m in p['mrs'].values()),
            len(p['mrs']), e(forge.baseurl), e(p['name']))
        for p in forge.state['projects'].values())
    return 'git-stream', (
        '<h1>Projects</h1><div class="box"><table><tr><th>Project</th>'
        '<th>Open MRs</th><th>All MRs</th><th>Clone</th></tr>%s</table>'
        '</div>' % rows)


def project_page(forge, p):
    branches = ''.join('<tr><td>%s</td><td><code>%s</code></td></tr>' % (
        e(b), sha[:10]) for b, sha in sorted(forge.repo(p).branches().items()))
    mrs = ''.join(
        '<tr><td><a href="/%s/merge_requests/%d">!%d</a></td><td>%s</td>'
        '<td><span class="pill %s">%s</span></td><td class="meta">%s</td>'
        '</tr>' % (e(p['name']), m['iid'], m['iid'], e(m['title']),
                   m['state'], m['state'], e(m['updated_at'][:19]))
        for m in sorted(p['mrs'].values(), key=lambda m: -m['iid']))
    return p['name'], (
        '<h1>%s</h1><div class="meta"><code>git clone %s/%s.git</code></div>'
        '<div class="box"><h2>Merge requests</h2><table>%s</table></div>'
        '<div class="box"><h2>Branches</h2><table>%s</table></div>' % (
            e(p['name']), e(forge.baseurl), e(p['name']),
            mrs or '<tr><td class="meta">None yet</td></tr>', branches))


def _log_link(log_url):
    """A link to a job's log, only for an http(s) URL.

    The URL comes from Zuul's builds API.  Escaping keeps it inside the
    attribute, but a "javascript:" URL would still run when clicked, so any
    other scheme is shown as text instead.
    """
    if not log_url:
        return ''
    if log_url.lower().startswith(('http://', 'https://')):
        return '<a href="%sjob-output.txt">log</a>' % html.escape(log_url)
    return html.escape(log_url)


def escalation_box(e, link=False):
    title = ('<a href="%s">%s!%s</a> %s' % (
        e_url(e), html.escape(e['project']), e['mr'], html.escape(e['title']))
        if link else 'Escalated')
    jobs = ''.join(
        '<tr><td>%s</td><td>%d failures</td><td>%s</td></tr>' % (
            html.escape(j['job']), j['attempts'], _log_link(j.get('log_url')))
        for j in e['failing'])
    when = datetime.datetime.fromtimestamp(
        e['time'], datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    stale = '' if e.get('current', True) else (
        ' · <b>a newer version is being tested</b>')
    return ('<div class="box escalation%s"><h2>%s</h2><div class="meta">'
            'Still failing after %d runs · %s · version <code>%s</code>%s'
            '</div><table>%s</table></div>' % (
                '' if e.get('current', True) else ' old', title,
                e['max_attempts'], when, e['sha'][:10], stale, jobs))


def e_url(e):
    return '/%s/merge_requests/%s' % (html.escape(e['project']), e['mr'])


def escalations_page(forge):
    found = forge.escalations()
    return 'Escalations', (
        '<h1>Escalations</h1><div class="meta">Merge requests whose jobs '
        'kept failing after the automatic re-runs.</div>%s' % (
            ''.join(escalation_box(x, link=True) for x in found) or
            '<div class="box meta">Nothing escalated.</div>'))


def metrics_page(metrics, params):
    project = params.get('project') or None
    days = _days(params)
    data = metrics.summary(project, days)
    scope = project or 'all projects'
    window = ('last %s days' % days) if days else 'all time'
    if data['jobs']:
        rows = ''.join(
            '<tr><td>%s</td><td>%d</td><td class="opened">%d</td>'
            '<td class="closed">%d</td><td class="meta">%s</td></tr>' % (
                e(j['job']), j['runs'], j['pass'], j['fail'],
                ('%.0f%%' % (j.get('fail_rate', 0) * 100))
                if j['runs'] else '')
            for j in data['jobs'])
    else:
        rows = '<tr><td class="meta">No runs recorded yet.</td></tr>'
    return 'Metrics', (
        '<h1>Metrics</h1><div class="meta">Job results recorded by the '
        'retry policy · %s · %s · %d runs</div>'
        '<div class="box"><table><tr><th>Job</th><th>Runs</th><th>Pass</th>'
        '<th>Fail</th><th>Fail rate</th></tr>%s</table></div>' % (
            e(scope), window, data['runs'], rows))


def mr_page(forge, p, mr):
    repo = forge.repo(p)
    files = repo.out('diff', '--name-status', mr['start_sha'], mr['sha'])
    notes = ''.join(
        '<div class="note"><b>%s</b> <span class="meta">%s</span>'
        '<pre>%s</pre></div>' % (e(n['author']), e(n['created_at'][:19]),
                                 e(n['body'])) for n in mr['notes'])
    approved = ', '.join(mr['approved_by']) or 'nobody'
    return '%s!%d' % (p['name'], mr['iid']), (
        '<meta http-equiv="refresh" content="10">'
        '<div class="meta"><a href="/%s">%s</a> !%d</div>'
        '<h1>%s</h1><div class="meta"><span class="pill %s">%s</span> '
        '%s wants to merge into <code>%s</code> · head <code>%s</code> · '
        'approved by %s%s</div>'
        '%s'
        '<div class="box"><h2>Description</h2><pre>%s</pre></div>'
        '<div class="box"><h2>Changes</h2><pre>%s</pre></div>'
        '<div class="box"><h2>Activity</h2>%s</div>' % (
            e(p['name']), e(p['name']), mr['iid'], e(mr['title']),
            mr['state'], mr['state'], e(mr['author']),
            e(mr['target_branch']), mr['sha'][:10], e(approved),
            (' · labels ' + e(', '.join(mr['labels'])))
            if mr['labels'] else '',
            escalation_box(dict(mr['escalation'], current=mr.get(
                'escalated_sha') == mr['sha'])) if mr.get('escalation')
            else '',
            e(mr['description'] or '(no description)'), e(files),
            notes or '<div class="meta">No comments yet</div>'))
