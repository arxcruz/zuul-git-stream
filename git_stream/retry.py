"""Retry failed jobs automatically, and escalate when retries run out.

The policy watches the webhooks the forge sends.  When Zuul reports a
failure on a merge request (a comment from a Zuul user starting with
"Build failed"), it asks Zuul's builds API how often each failing job has
failed on the merge request's current version:

  - jobs that failed fewer than max_attempts times are re-run, with a
    "rerun failed" (or "rerun <jobs>") comment;
  - when no failing job is left to retry, the merge request is escalated
    once: the escalation is recorded (GET /api/stream/escalations and the
    /escalations page), the merge request is labeled "escalated" and
    commented, and every escalation URL, if any, gets it as JSON (e.g. to
    open a ticket).

max_attempts counts runs, so 3 means the first run and two retries.  A
merge request can override it with "max_attempts" when it's opened; 0
turns the policy off for it.
"""

import json
import logging
import queue
import re
import threading
import time
import urllib.parse
import urllib.request

from git_stream.forge import ForgeError

log = logging.getLogger('git_stream.retry')

FAILED = {'FAILURE', 'TIMED_OUT', 'POST_FAILURE', 'NODE_FAILURE'}
# Results that don't say whether a job works.
NOT_A_VERDICT = {'SKIPPED', 'RETRY', 'CANCELED', 'ABORTED', None}
ESCALATED_LABEL = 'escalated'


class RetryPolicy(threading.Thread):
    def __init__(self, forge, zuul_api, max_attempts=3, escalate_urls=(),
                 report_users=('zuul',), failed_re=r'^\s*Build failed',
                 author='git-stream', settle=2, metrics=None):
        super().__init__(daemon=True, name='retry')
        self.forge = forge
        self.zuul_api = zuul_api.rstrip('/')
        self.max_attempts = max_attempts
        self.escalate_urls = list(escalate_urls)
        self.report_users = set(report_users)
        self.failed_re = re.compile(failed_re)
        self.author = author
        # Optional Metrics store: logs what we learn so it survives and can
        # be queried (see metrics.py).
        self.metrics = metrics
        # Seconds to wait before reading results, so Zuul's database has
        # every build of the buildset it just reported.
        self.settle = settle
        self.queue = queue.Queue()

    def observe(self, payload):
        """Called with every webhook payload the forge emits."""
        if (payload.get('object_kind') == 'note' and
                payload['user']['username'] in self.report_users and
                self.failed_re.match(payload['object_attributes']['note'])):
            mr = payload['merge_request']
            self.queue.put((payload['project']['path_with_namespace'],
                            mr['iid'], mr['last_commit']['id']))

    def run(self):
        while True:
            project, iid, sha = self.queue.get()
            try:
                time.sleep(self.settle)
                self.handle(project, iid, sha)
            except Exception:
                log.exception('Retry policy failed for %s!%s', project, iid)

    def handle(self, project, iid, sha):
        with self.forge.lock:
            p = self.forge.project(project)
            mr = self.forge.mr(p, iid)
            if mr['state'] != 'opened' or mr['sha'] != sha:
                return  # closed, or a newer version is being tested
            max_attempts = mr.get('max_attempts')
            if max_attempts is None:
                max_attempts = self.max_attempts
        if max_attempts <= 0:
            return
        failing = {job: r for job, r in self.results(project, iid, sha)
                   .items() if r['last'] in FAILED}
        if not failing:
            log.info('%s!%s reported a failure, but no job failed', project,
                     iid)
            return
        retry = sorted(j for j, r in failing.items()
                       if r['failures'] < max_attempts)
        if retry:
            note = ('rerun failed' if len(retry) == len(failing)
                    else 'rerun ' + ' '.join(retry))
            log.info('%s!%s: %s', project, iid, note)
            self.forge.comment(project, iid, note, self.author)
            return
        self.escalate(project, iid, sha, failing, max_attempts)

    def results(self, project, iid, sha):
        """Per job: latest result, failure count and latest build."""
        query = urllib.parse.urlencode({
            'project': project, 'change': iid, 'patchset': sha,
            'complete': 'true', 'limit': 1000})
        with urllib.request.urlopen('%s/builds?%s' % (self.zuul_api, query),
                                    timeout=10) as r:
            builds = json.load(r)
        if self.metrics is not None:
            self.metrics.record_results(project, iid, sha, builds,
                                        self.zuul_api)
        results = {}
        for build in builds:  # newest first
            if build['result'] in NOT_A_VERDICT:
                continue
            r = results.setdefault(build['job_name'], {
                'last': build['result'], 'failures': 0,
                'log_url': build.get('log_url'), 'uuid': build['uuid']})
            if build['result'] in FAILED:
                r['failures'] += 1
        return results

    def escalate(self, project, iid, sha, failing, max_attempts):
        jobs = [{'job': job, 'attempts': r['failures'], 'result': r['last'],
                 'log_url': r['log_url'],
                 'build_url': '%s/build/%s' % (self.zuul_api, r['uuid'])}
                for job, r in sorted(failing.items())]
        with self.forge.lock:
            p = self.forge.project(project)
            mr = self.forge.mr(p, iid)
            if mr.get('escalated_sha') == sha:
                return
            event = {
                'event': 'escalation', 'project': project, 'mr': iid,
                'key': mr.get('key'), 'title': mr['title'],
                'url': self.forge.mr_url(p, mr), 'sha': sha,
                'time': time.time(), 'max_attempts': max_attempts,
                'failing': jobs,
            }
            mr['escalated_sha'] = sha
            mr['escalation'] = event
            self.forge.save()
            fields = {k: v for k, v in event.items() if k != 'event'}
            self.forge.notify_outcome(p, mr, 'escalated', fields)
        if self.metrics is not None:
            self.metrics.record_escalation(event)
        log.warning('Escalating %s!%s: %s', project, iid,
                    ', '.join(j['job'] for j in jobs))
        lines = ['Escalated: still failing after %d attempts.' % max_attempts,
                 ''] + ['- %s (%d failures) %s' % (
                     j['job'], j['attempts'], j['log_url'] or '')
                     for j in jobs]
        self.forge.comment(project, iid, '\n'.join(lines), self.author)
        try:
            self.forge.update_mr(project, iid, self.author,
                                 add_labels=[ESCALATED_LABEL])
        except ForgeError:
            pass
        for url in self.escalate_urls:
            post(url, event)


def post(url, data, attempts=5):
    body = json.dumps(data).encode('utf8')
    for i in range(attempts):
        req = urllib.request.Request(url, data=body, method='POST', headers={
            'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=10):
                return True
        except OSError as e:
            log.warning('Escalation to %s failed (%s)', url, e)
            time.sleep(2 ** i)
    return False
