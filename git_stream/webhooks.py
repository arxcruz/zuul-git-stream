"""Deliver webhooks in order, retrying until the receiver accepts them.

Zuul may still be starting when the first events happen, so a delivery
is retried with backoff; later events wait behind it so Zuul sees them in
the order they happened.
"""

import collections
import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger('git_stream.webhooks')

EVENT_HEADERS = {
    'merge_request': 'Merge Request Hook',
    'note': 'Note Hook',
    'push': 'Push Hook',
    'tag_push': 'Tag Push Hook',
}


class WebhookSender(threading.Thread):
    def __init__(self, urls, token, max_backoff=30):
        super().__init__(daemon=True, name='webhooks')
        self.urls = list(urls)
        self.token = token
        self.max_backoff = max_backoff
        self.queue = queue.Queue()
        # Recent deliveries, newest first, for GET /api/stream/deliveries.
        self.recent = collections.deque(maxlen=50)
        self._stopped = False

    def emit(self, payload):
        self.queue.put(payload)

    def stop(self):
        self._stopped = True
        self.queue.put(None)

    def run(self):
        while not self._stopped:
            payload = self.queue.get()
            if payload is None:
                continue
            for url in self.urls:
                self._deliver(url, payload)

    def _deliver(self, url, payload):
        kind = payload['object_kind']
        body = json.dumps(payload).encode('utf8')
        entry = {'time': time.time(), 'url': url, 'kind': kind,
                 'action': payload.get('object_attributes', {}).get('action'),
                 'project': payload['project']['path_with_namespace'],
                 'summary': summarize(payload), 'attempts': 0,
                 'status': None}
        self.recent.appendleft(entry)
        backoff = 1
        while not self._stopped:
            entry['attempts'] += 1
            req = urllib.request.Request(url, data=body, method='POST')
            req.add_header('Content-Type', 'application/json')
            req.add_header('X-Gitlab-Event', EVENT_HEADERS.get(kind, kind))
            req.add_header('X-Gitlab-Token', self.token)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    entry['status'] = r.status
                log.info('Delivered %s to %s', entry['summary'], url)
                return
            except urllib.error.HTTPError as e:
                entry['status'] = e.code
                if 400 <= e.code < 500:
                    log.error('%s rejected %s: HTTP %s', url,
                              entry['summary'], e.code)
                    return
            except OSError as e:
                entry['status'] = str(e)
            log.warning('Delivering %s to %s failed (%s); retrying in %ss',
                        entry['summary'], url, entry['status'], backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff)


def summarize(payload):
    kind = payload['object_kind']
    project = payload['project']['path_with_namespace']
    if kind == 'merge_request':
        attrs = payload['object_attributes']
        return '%s!%s %s' % (project, attrs['iid'], attrs['action'])
    if kind == 'note':
        return '%s!%s comment %r' % (
            project, payload['merge_request']['iid'],
            payload['object_attributes']['note'][:40])
    return '%s %s %s %s' % (project, kind, payload['ref'],
                            payload['after'][:8])
