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

Standard library only.  One process, one writer connection under a lock.
The counting endpoints read through their own read-only connection (the
database is in WAL mode), so a long scan never holds up a write.
"""

import datetime
import json
import math
import sqlite3
import threading
import urllib.parse

from git_stream import aggregate

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
  elapsed_ms  REAL,
  pipeline    TEXT,
  key         TEXT
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


FAILED = "('FAILURE', 'TIMED_OUT', 'POST_FAILURE', 'NODE_FAILURE')"


def _equals(value):
    return [value]


# How the aggregate endpoints read the run and delivery tables (see
# aggregate.py): fixed SQL, the query's values only as parameters.
RUN_SPEC = {
    'table': 'run', 'time': 'finished_at',
    'groups': {'job': 'job', 'result': 'result', 'pipeline': 'pipeline',
               'project': 'project', 'mr': 'COALESCE(key, CAST(mr AS TEXT))'},
    'filters': {
        'project': ('project = ?', _equals), 'job': ('job = ?', _equals),
        'result': ('result = ?', _equals),
        'pipeline': ('pipeline = ?', _equals),
        'mr': ('(key = ? OR CAST(mr AS TEXT) = ?)', lambda v: [v, v])},
    'extra': [
        ('pass', "SUM(result = 'SUCCESS')"),
        ('fail', 'SUM(result IN %s)' % FAILED),
        ('other', "SUM(CASE WHEN result = 'SUCCESS' OR result IN %s THEN 0 "
                  "ELSE 1 END)" % FAILED),
        ('avg_ms', 'AVG(elapsed_ms)'), ('max_ms', 'MAX(elapsed_ms)'),
        ('timed', 'COUNT(elapsed_ms)')],
}
DELIVERY_SPEC = {
    'table': 'delivery', 'time': 'time',
    'groups': {'kind': 'kind', 'action': 'action', 'status': 'status',
               'project': 'project',
               'ok': "CASE WHEN ok = 1 THEN 'ok' ELSE 'failed' END"},
    'filters': {
        'project': ('project = ?', _equals), 'kind': ('kind = ?', _equals),
        'action': ('action = ?', _equals), 'ok': ('ok = ?', _equals)},
    'extra': [('ok', 'SUM(ok)'), ('failed', 'COUNT(*) - SUM(ok)'),
              ('attempts', 'SUM(attempts)')],
}

# Columns added after the first release: a database made by an older
# version gets them when it is opened (Metrics.migrate).
ADDED_RUN_COLUMNS = (('pipeline', 'TEXT'), ('key', 'TEXT'))


class Metrics:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript(SCHEMA)
        self.migrate()
        self.conn.commit()

    def migrate(self):
        """Bring an older database up to date, keeping every row.

        Adds the columns it lacks, then makes a build uuid unique: every
        Zuul report used to log all the builds of a version again, so an old
        database has the same build several times.  One row per uuid is kept
        (the first); builds with no uuid are all kept.
        """
        have = {r['name'] for r in self.conn.execute('PRAGMA table_info(run)')}
        for name, kind in ADDED_RUN_COLUMNS:
            if name not in have:
                self.conn.execute(
                    'ALTER TABLE run ADD COLUMN %s %s' % (name, kind))
        self.conn.execute(
            'DELETE FROM run WHERE uuid IS NOT NULL AND id NOT IN '
            '(SELECT MIN(id) FROM run WHERE uuid IS NOT NULL GROUP BY uuid)')
        self.conn.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS run_uuid ON run(uuid)')

    def close(self):
        with self.lock:
            self.conn.close()

    # -- writers -----------------------------------------------------------

    def record_results(self, project, iid, sha, builds, build_url_prefix,
                       key=None):
        """Log the completed builds of one merge request version.

        builds is the list from Zuul's /builds API (newest first).  Builds
        whose result is not a verdict (skipped/retry/none) are not logged.
        Zuul reports again after every retry, with all the builds of the
        version, so a build already logged (same uuid) is left alone.  key
        is the merge request's key (the bundle id), if it has one.
        """
        rows = []
        for b in builds:
            if b.get('result') in (None, 'SKIPPED', 'RETRY'):
                continue
            started_at, finished_at, elapsed = _times(b)
            rows.append((
                project, iid, str(iid), sha, b.get('job_name'),
                b['result'], b.get('uuid'), b.get('log_url'),
                build_url(b.get('uuid'), build_url_prefix),
                started_at, finished_at, elapsed, b.get('pipeline'), key))
        if not rows:
            return
        with self.lock:
            self.conn.executemany(
                'INSERT OR IGNORE INTO run(project, mr, change, patchset,'
                ' job, result, uuid, log_url, build_url, started_at,'
                ' finished_at, elapsed_ms, pipeline, key)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
            self.conn.commit()

    def record_escalation(self, event):
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

    def aggregate(self, query):
        """Count runs, escalations or deliveries as a checked query asks
        (aggregate.parse)."""
        reader = sqlite3.connect('file:%s?mode=ro' % urllib.parse.quote(
            self.path), uri=True)
        try:
            reader.row_factory = sqlite3.Row
            if query.kind == 'runs':
                return aggregate.aggregate(reader, query, RUN_SPEC)
            if query.kind == 'deliveries':
                return aggregate.aggregate(reader, query, DELIVERY_SPEC)
            return self._aggregate_escalations(reader, query)
        finally:
            reader.close()

    def _aggregate_escalations(self, conn, query):
        """Escalations are few and each lists several failing jobs, so they
        are counted here rather than in SQL."""
        where, args = [], []
        project = query.filters.get('project')
        if project is not None:
            where.append('project = ?')
            args.append(project)
        mr = query.filters.get('mr')
        if mr is not None:
            where.append('(key = ? OR CAST(mr AS TEXT) = ?)')
            args += [mr, mr]
        if query.lo is not None:
            where.append('time >= ?')
            args.append(query.lo)
        if query.hi is not None:
            where.append('time < ?')
            args.append(query.hi)
        rows = conn.execute(
            'SELECT project, mr, key, time, failing_json FROM escalation%s'
            % (' WHERE ' + ' AND '.join(where) if where else ''),
            args).fetchall()
        want_job = query.filters.get('job')
        entries = []
        for r in rows:
            jobs = sorted({f.get('job') for f in
                           json.loads(r['failing_json'] or '[]')
                           if isinstance(f, dict)} - {None})
            if want_job is not None and want_job not in jobs:
                continue
            if query.group == 'job':
                entries += [(job, r['time']) for job in jobs]
            else:
                group = {None: None, 'project': r['project'],
                         'mr': r['key'] or str(r['mr'])}[query.group]
                entries.append((group, r['time']))
        return aggregate.count_rows(query, entries)

    def escalations(self, limit=100):
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
    """A finite number, or None if value is a string or otherwise not."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _epoch(value):
    """Seconds since the epoch of a time Zuul reported, or None.

    Zuul's builds API gives times as ISO 8601 strings without a zone (UTC);
    a number is taken as epoch seconds already.
    """
    if isinstance(value, str):
        try:
            when = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        return when.timestamp()
    return _number(value)


def _times(build):
    """(started_at, finished_at, elapsed_ms) for a build; None for each
    that Zuul did not give in a usable form.

    The elapsed time is end minus start when both are there, else the build's
    duration (Zuul's field, in seconds), else its elapsed_time.
    """
    start, end = _epoch(build.get('start_time')), _epoch(build.get('end_time'))
    if start is not None and end is not None:
        return start, end, (end - start) * 1000
    for name in ('duration', 'elapsed_time'):
        seconds = _number(build.get(name))
        if seconds is not None:
            return start, end, seconds * 1000
    return start, end, None
