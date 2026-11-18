"""Metrics: what the jobs did, in a database.

A SQLite file in the data directory records, as events happen, what
Zuul's jobs did and how git-stream's webhooks were delivered.  This is the
durable, queryable version of the numbers retry.py recomputes from Zuul's
builds API on every report, plus the webhook delivery history.  It is
read-mostly: the retry policy appends rows, and nothing else writes.

Three tables:

  run         one row per completed build of a job (a Zuul build)
  escalation  one row per escalation event
  delivery    one row per webhook delivery

A "run" is a single job execution in Zuul.  git-stream logs the builds of
a merge request version (the MR's sha) when it acts on one of Zuul's
reports, so the table accumulates the retry history over time and is
independent of Zuul's API: after a restart, "how often did this job fail"
can be answered from the database without asking Zuul.  It is a *log of
what git-stream saw*, not a source of truth (Zuul is the source of truth);
a new version of a merge request starts its rows fresh, like the retry
counter does.

Standard library only.  One process (single writer: the retry policy;
readers are the HTTP handlers), so no locking or WAL is needed.
"""

import os
import sqlite3
import threading

SCHEMA = '''
CREATE TABLE IF NOT EXISTS run (
  id          INTEGER PRIMARY KEY,
  project     TEXT,
  mr          INTEGER,
  change      TEXT,
  patchset    TEXT,
  job         TEXT,
  result      TEXT,
  uuid        TEXT,
  log_url     TEXT,
  build_url   TEXT,
  started_at  REAL,
  finished_at REAL,
  elapsed_ms  REAL
);
CREATE INDEX IF NOT EXISTS run_idx ON
  run(project, change, patchset, job);
CREATE TABLE IF NOT EXISTS escalation (
  id          INTEGER PRIMARY KEY,
  project     TEXT,
  mr          INTEGER,
  key         TEXT,
  sha         TEXT,
  time        REAL,
  max_attempts INTEGER,
  failing_json TEXT
);
CREATE TABLE IF NOT EXISTS delivery (
  id          INTEGER PRIMARY KEY,
  time        REAL,
  url         TEXT,
  kind        TEXT,
  action      TEXT,
  project     TEXT,
  status      TEXT,
  attempts    INTEGER,
  ok          INTEGER
);
'''


class Metrics:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()

    # -- writers -----------------------------------------------------------

    def record_results(self, project, iid, sha, builds, build_url_prefix):
        """Log the completed builds of one merge request version.

        builds is the list from Zuul's /builds API (newest first).  Builds
        whose result is not a verdict (skipped/retry/none) are not logged.
        """
        rows = []
        for b in builds:
            if b.get('result') in (None, 'SKIPPED', 'RETRY'):
                continue
            finished_at, elapsed = _times(b)
            rows.append((
                project, iid, str(iid), sha, b.get('job_name'),
                b['result'], b.get('uuid'), b.get('log_url'),
                build_url(b.get('uuid'), build_url_prefix),
                finished_at, finished_at, elapsed))
        if not rows:
            return
        with self.lock:
            self.conn.executemany(
                'INSERT INTO run(project, mr, change, patchset, job, result,'
                ' uuid, log_url, build_url, started_at, finished_at,'
                ' elapsed_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', rows)
            self.conn.commit()

    def record_escalation(self, event):
        import json
        with self.lock:
            self.conn.execute(
                'INSERT INTO escalation(project, mr, key, sha, time,'
                ' max_attempts, failing_json)'
                ' VALUES (?,?,?,?,?,?,?)',
                (event['project'], event['mr'], event.get('key'),
                 event.get('sha'), event.get('time'),
                 event.get('max_attempts'), json.dumps(event.get('failing'))))
            self.conn.commit()

    def record_delivery(self, entry):
        status = entry.get('status')
        with self.lock:
            self.conn.execute(
                'INSERT INTO delivery(time, url, kind, action, project,'
                ' status, attempts, ok) VALUES (?,?,?,?,?,?,?,?)',
                (entry.get('time'), entry.get('url'), entry.get('kind'),
                 entry.get('action'), entry.get('project'),
                 status if status is None else str(status),
                 entry.get('attempts') or 0,
                 1 if (status is not None and
                       (status == 200 or (isinstance(status, int) and
                                           200 <= status < 300))) else 0))
            self.conn.commit()

    # -- readers -----------------------------------------------------------

    def summary(self, project=None, days=None, limit=100):
        """Aggregate the run table into per-job counts, newest last."""
        sql = ('SELECT job, result, count(*) AS n, sum(elapsed_ms) AS ms,'
               ' max(finished_at) AS last FROM run WHERE 1=1')
        args = []
        if project:
            sql += ' AND project = ?'
            args.append(project)
        if days is not None:
            import time
            sql += (' AND COALESCE(finished_at, 0) <= ? AND '
                    'COALESCE(finished_at, 0) >= ?')
            args += [time.time(), time.time() - days * 86400]
        sql += (' GROUP BY job, result ORDER BY '
                'COALESCE(MAX(finished_at), MAX(id))')
        cur = self.conn.execute(sql, args)
        by_job = {}
        for row in cur:
            j = by_job.setdefault(row['job'], {'job': row['job'], 'runs': 0,
                                               'pass': 0, 'fail': 0,
                                               'other': 0, 'last': None})
            n = row['n']
            j['runs'] += n
            j.setdefault('results', {}).setdefault(row['result'], 0)
            j['results'][row['result']] += n
            if row['result'] in ('SUCCESS',):
                j['pass'] += n
            elif row['result'] in ('FAILURE', 'TIMED_OUT', 'POST_FAILURE',
                                   'NODE_FAILURE'):
                j['fail'] += n
            else:
                j['other'] += n
            if row['ms']:
                j['elapsed_ms'] = j.get('elapsed_ms', 0) + row['ms']
            last = row['last']
            if last is not None and (j['last'] is None or last > j['last']):
                j['last'] = last
        jobs = sorted(by_job.values(),
                      key=lambda j: -(j['last'] or 0))[:limit]
        for j in jobs:
            j.pop('elapsed_ms', None)
            if j['runs']:
                j['fail_rate'] = round(j['fail'] / j['runs'], 3)
        return {'jobs': jobs,
                'runs': sum(j['runs'] for j in by_job.values())}

    def escalations(self, limit=100):
        import json
        rows = self.conn.execute(
            'SELECT * FROM escalation ORDER BY time DESC LIMIT ?',
            (limit,)).fetchall()
        return [dict(r, failing=json.loads(r['failing_json'] or '[]'))
                for r in rows]

    def deliveries(self, limit=50):
        return [dict(r) for r in self.conn.execute(
            'SELECT time, url, kind, action, project, status, attempts, ok'
            ' FROM delivery ORDER BY time DESC, id DESC LIMIT ?',
            (limit,)).fetchall()]


def build_url(uuid, prefix):
    return prefix.rstrip('/') + '/' + uuid if uuid and prefix else None


def _number(value):
    """A number, or None if value is a string or otherwise not numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _times(build):
    """(finished_at, elapsed_ms) for a build, or (None, None).

    finished_at is the build's end time, or None if Zuul reported no usable
    times.  Zuul may report start_time/end_time as epoch floats, as ISO
    strings, or omit them, so only numeric values are used for the window
    and the elapsed calculation.
    """
    start, end = _number(build.get('start_time')), _number(build.get('end_time'))
    if start is not None and end is not None:
        return end, (end - start) * 1000
    if build.get('elapsed_time') is not None:
        elapsed = _number(build.get('elapsed_time'))
        if elapsed is not None:
            return None, elapsed * 1000
    return None, None
