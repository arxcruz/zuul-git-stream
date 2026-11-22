"""Demo UI: announce bundles to git-stream and watch what Zuul does.

Sends events to git-stream (POST /api/stream; git-stream turns them into
merge requests, comments and pushes), and reads git-stream's GitLab API
and Zuul's API so the browser doesn't need CORS.  Standard library only.
"""

import collections
import http.server
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

FORGE = os.environ.get('FORGE', 'http://git-stream:8090')
# Sent to git-stream's message charts (they need a stream token).  The demo's
# stream is open, so it is normally empty.
STREAM_TOKEN = os.environ.get('STREAM_TOKEN', '')
ZUUL_API = os.environ.get(
    'ZUUL_API', 'http://web:9000/api/tenant/example-tenant')
HERE = os.path.dirname(os.path.abspath(__file__))

BUNDLE_PROJECT = 'bundles'
TOOL_PROJECT = 'ci-tool'
TOOL_FIX_KEY = 'fix-parse-manifest'
BUNDLE_JOBS = ['bundle-networking', 'bundle-storage', 'bundle-auth',
               'bundle-api', 'bundle-upgrade', 'bundle-rollback',
               'bundle-scale', 'bundle-security', 'bundle-logging',
               'bundle-metrics']
# Results that don't say whether a job works for a bundle; the same rule
# as the bundle-dispatch job.
NOT_A_VERDICT = {'SKIPPED', 'RETRY', None}
DEPENDS_ON_RE = re.compile(r'^Depends-On:\s*(\S+)', re.M | re.I)

# What ci-tool announces, read by the bundle tests: jobs listed crash because
# of a known ci-tool bug (parse_manifest() segfaults on signed bundles).
TOOL_BROKEN = {'broken_jobs': ['bundle-security']}
TOOL_FIXED = {'broken_jobs': []}

sent = collections.deque(maxlen=30)
sent_lock = threading.Lock()
bundle_counter = int(time.time()) % 10000


def send(key, msg):
    """Send an event to git-stream; returns what git-stream made of it."""
    payload = json.dumps(msg)
    req = urllib.request.Request(
        FORGE + '/api/stream', data=payload.encode('utf8'), method='POST',
        headers={'Content-Type': 'application/json'})
    entry = dict(time=time.time(), key=key, value=payload)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.load(r)
        entry['result'] = (result.get('web_url') or result.get('ref') or
                           ('note %s' % result['id'] if 'body' in result
                            else ''))
    except urllib.error.HTTPError as e:
        entry['error'] = json.load(e).get('message', str(e))
    with sent_lock:
        sent.appendleft(entry)
    return entry


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        body = r.read()
    return json.loads(body) if body[:1] in (b'[', b'{') else body.decode()


METRIC_KINDS = ('runs', 'escalations', 'deliveries', 'messages')
MAX_QUERY = 2000


def forge_metrics(kind, query):
    """git-stream's answer to a metrics query: (status, parsed JSON).

    Its errors pass through with their status and message, so the page can
    say what is wrong; only git-stream being unreachable is a 502.
    """
    headers = {'X-Stream-Token': STREAM_TOKEN} if STREAM_TOKEN else {}
    url = '%s/api/stream/metrics/%s' % (FORGE, kind) + (
        '?' + query if query else '')
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, headers=headers), timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, {'message': 'git-stream answered %d' % e.code}
    except (urllib.error.URLError, OSError):
        return 502, {'error': 'git-stream is not answering'}


def forge_api(path):
    return get(FORGE + '/api/v4/projects/' + path)


def zuul_get(path):
    return get(ZUUL_API + path)


def elapsed_ms(job):
    # elapsed_time in the status API is a snapshot that only changes
    # when the build is updated; compute it from the start time instead.
    if not job.get('start_time'):
        return None
    if job.get('result'):
        return job.get('elapsed_time')
    return int((time.time() - job['start_time']) * 1000)


def summarize_status(status):
    """Flatten the Zuul status API into pipeline -> items -> jobs."""
    items = []
    for pipeline in status.get('pipelines', []):
        for queue in pipeline.get('change_queues', []):
            for head in queue.get('heads', []):
                by_id = {i.get('id'): i for i in head}
                for item in head:
                    if not item.get('live', True):
                        continue
                    # Changes this item is tested with (from Depends-On).
                    ahead, up = [], by_id.get(item.get('item_ahead'))
                    while up:
                        ahead += [r.get('url') for r in up.get('refs', [])]
                        up = by_id.get(up.get('item_ahead'))
                    refs = item.get('refs') or [item]
                    ref = refs[0]
                    project = ref.get('project')
                    if isinstance(project, dict):
                        project = project.get('name')
                    jobs = [{
                        'name': job.get('name'),
                        'result': job.get('result'),
                        'started': bool(job.get('start_time')),
                        'elapsed': elapsed_ms(job),
                        'estimated': job.get('estimated_time') or None,
                        'waiting': job.get('waiting_status'),
                        'uuid': job.get('uuid'),
                    } for job in item.get('jobs', [])]
                    change = ref.get('change') or (
                        str(ref.get('id', '')).split(',')[0] or None)
                    items.append({
                        'pipeline': pipeline['name'],
                        'project': project,
                        'change': str(change) if change else None,
                        'ref': ref.get('ref'),
                        'url': ref.get('url'),
                        'ahead': [u for u in ahead if u] or None,
                        'enqueue_time': item.get('enqueue_time'),
                        'jobs': jobs,
                    })
    return items


def build_change(b):
    ref = b.get('ref') if isinstance(b.get('ref'), dict) else b
    change = ref.get('change')
    return str(change) if change is not None else None


def bundles():
    """Per bundle MR: the latest verdict of each job, plus what's running."""
    mrs = forge_api('%s/merge_requests?per_page=8' % BUNDLE_PROJECT)
    builds = zuul_get('/builds?project=%s&limit=1000' % BUNDLE_PROJECT)
    status = summarize_status(zuul_get('/status'))
    out = []
    for mr in mrs:
        iid = str(mr['iid'])
        info = {
            'id': mr.get('stream_key') or 'mr-%s' % iid, 'iid': mr['iid'],
            'title': mr['title'], 'url': mr['web_url'], 'state': mr['state'],
            'depends_on': DEPENDS_ON_RE.findall(mr['description'] or ''),
            'approved': forge_api('%s/merge_requests/%s/approvals' % (
                BUNDLE_PROJECT, iid))['approved'],
            'escalated': mr.get('escalated', False),
            'busy': False, 'runs': 0, 'jobs': {},
        }
        for b in builds:  # newest first
            if build_change(b) != iid:
                continue
            if b['job_name'] == 'bundle-dispatch':
                info['runs'] += 1
            elif b['result'] not in NOT_A_VERDICT:
                info['jobs'].setdefault(b['job_name'], {
                    'state': b['result'], 'log': b.get('log_url'),
                    'uuid': b['uuid']})
        for item in status:
            if item['project'] != BUNDLE_PROJECT or item['change'] != iid:
                continue
            info['busy'] = True
            for j in item['jobs']:
                if j['name'] not in BUNDLE_JOBS or j['result'] == 'SKIPPED':
                    continue
                if j['result'] is None:
                    info['jobs'][j['name']] = {
                        'state': 'RUNNING' if j['started'] else 'QUEUED',
                        'uuid': j['uuid']}
                else:
                    info['jobs'][j['name']] = {'state': j['result'],
                                               'uuid': j['uuid']}
        out.append(info)
    return {'jobs': BUNDLE_JOBS, 'bundles': out}


def ci_tool():
    """ci-tool's master (broken or not) and its fix merge request."""
    try:
        broken = forge_api('%s/data' % TOOL_PROJECT)['data'].get(
            'broken_jobs', [])
    except urllib.error.HTTPError:
        broken = []  # ci-tool has announced nothing yet
    fix = next((m for m in forge_api('%s/merge_requests?per_page=20'
                                     % TOOL_PROJECT)
                if m.get('stream_key') == TOOL_FIX_KEY), None)
    return {
        'broken_jobs': broken,
        'fix': fix and {'iid': fix['iid'], 'url': fix['web_url'],
                        'state': fix['state'], 'title': fix['title']},
    }


def bundle_message(req):
    """The stream message for an action on a bundle."""
    global bundle_counter
    action = req.get('action')
    flaky = float(req.get('flaky', 0.3))
    if action == 'new':
        bundle_counter += 1
        bundle_id = 'b-%04d' % bundle_counter
        return bundle_id, {
            'action': 'open_mr', 'project': BUNDLE_PROJECT,
            'key': bundle_id, 'author': 'build-system',
            'title': 'Bundle %s' % bundle_id,
            'description': 'Container bundle %s, announced by the build '
                           'system.' % bundle_id,
            'data': {'id': bundle_id, 'flaky': flaky,
                     'images': ['registry.local/api:%s' % bundle_id,
                                'registry.local/worker:%s' % bundle_id]},
        }
    bundle_id = req['bundle_id']
    busy = [b for b in bundles()['bundles']
            if b['id'] == bundle_id and b['busy']]
    if busy:
        # Zuul would ignore it: the merge request is already being tested.
        raise ValueError('%s is still running; wait for it to finish'
                         % bundle_id)
    note = None
    if action == 'failed':
        note = 'rerun failed flaky=%s' % flaky
    elif action == 'all':
        note = 'rerun all flaky=%s' % flaky
    elif action == 'selected':
        note = 'rerun %s flaky=%s' % (' '.join(
            j[len('bundle-'):] for j in req['jobs']), flaky)
    if note:
        return bundle_id, {'action': 'comment', 'project': BUNDLE_PROJECT,
                           'mr': bundle_id, 'note': note,
                           'author': 'release-manager'}
    if action == 'with-fix':
        fix = ci_tool()['fix']
        if not fix or fix['state'] != 'opened':
            raise ValueError('open the ci-tool fix first')
        mr = forge_api('%s/merge_requests/%s' % (
            BUNDLE_PROJECT, next(b['iid'] for b in bundles()['bundles']
                                 if b['id'] == bundle_id)))
        if fix['url'] in mr['description']:
            raise ValueError('%s already depends on the fix' % bundle_id)
        msg = {
            'action': 'update_mr', 'project': BUNDLE_PROJECT,
            'mr': bundle_id, 'author': 'release-manager',
            'description': '%s\n\nDepends-On: %s' % (
                mr['description'].rstrip(), fix['url'])}
        # The whole bundle is tested again; use the flakiness chosen now.
        bundle = forge_api('%s/merge_requests/%s/data' % (
            BUNDLE_PROJECT, mr['iid']))['data']
        if bundle.get('flaky') != flaky:
            msg['data'] = dict(bundle, flaky=flaky)
            msg['message'] = 'Set network flakiness to %s' % flaky
        return bundle_id, msg
    raise ValueError('unknown action %r' % action)


def tool_message(req):
    action = req.get('action')
    if action == 'fix-open':
        return TOOL_PROJECT, {
            'action': 'open_mr', 'project': TOOL_PROJECT,
            'key': TOOL_FIX_KEY, 'author': 'ci-tool-dev',
            'title': 'Fix parse_manifest() crash on signed bundles',
            'description': 'parse_manifest() read past the end of the '
                           'signature block. bundle-security no longer '
                           'crashes.',
            'data': TOOL_FIXED}
    if action == 'fix-merge':
        return TOOL_PROJECT, {'action': 'merge', 'project': TOOL_PROJECT,
                              'mr': TOOL_FIX_KEY, 'author': 'ci-tool-dev'}
    if action == 'break':
        return TOOL_PROJECT, {
            'action': 'push', 'project': TOOL_PROJECT,
            'author': 'ci-tool-dev', 'data': TOOL_BROKEN,
            'message': 'Rewrite parse_manifest() for speed'}
    raise ValueError('unknown action %r' % action)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, data, code=200):
        body = json.dumps(data).encode('utf8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name, content_type):
        with open(os.path.join(HERE, name), 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            path, _, query = self.path.partition('?')
            if path in ('/', '/index.html'):
                self._file('index.html', 'text/html; charset=utf-8')
            elif path == '/metrics':
                self._file('metrics.html', 'text/html; charset=utf-8')
            elif path == '/metrics.js':
                self._file('metrics.js', 'text/javascript; charset=utf-8')
            elif path.startswith('/api/metrics/'):
                kind = path[len('/api/metrics/'):]
                if kind not in METRIC_KINDS:
                    self._json({'error': 'not found'}, 404)
                elif len(query) > MAX_QUERY:
                    self._json({'message': 'the query is too long'}, 414)
                else:
                    self._json(*reversed(forge_metrics(kind, query)))
            elif self.path == '/api/sent':
                with sent_lock:
                    self._json(list(sent))
            elif self.path == '/api/deliveries':
                self._json(get(FORGE + '/api/stream/deliveries'))
            elif self.path == '/api/status':
                self._json(summarize_status(zuul_get('/status')))
            elif self.path == '/api/bundles':
                self._json(bundles())
            elif self.path == '/api/ci-tool':
                self._json(ci_tool())
            elif self.path == '/api/escalations':
                self._json(get(FORGE + '/api/stream/escalations'))
            elif path == '/api/metrics':
                query = ('?' + self.path.split('?', 1)[1]) if '?' in self.path else ''
                self._json(get(FORGE + '/api/stream/metrics' + query))
            elif self.path == '/api/builds':
                self._json(zuul_get('/builds?limit=12'))
            else:
                self._json({'error': 'not found'}, 404)
        except Exception as e:
            self._json({'error': str(e)}, 502)

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            req = json.loads(self.rfile.read(length) or b'{}')
            if self.path == '/api/bundle':
                key, msg = bundle_message(req)
            elif self.path == '/api/ci-tool':
                key, msg = tool_message(req)
            elif self.path == '/api/send':
                key, msg = req.get('key') or 'custom', req['value']
            else:
                return self._json({'error': 'not found'}, 404)
            entry = send(key, msg)
            self._json(entry, 400 if 'error' in entry else 200)
        except Exception as e:
            self._json({'error': str(e)}, 400)


if __name__ == '__main__':
    print('Demo UI on http://0.0.0.0:8080 (git-stream %s)' % FORGE,
          flush=True)
    http.server.ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
