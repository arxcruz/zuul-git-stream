"""Counting what git-stream recorded: the query behind the metrics charts.

GET /api/stream/metrics/{runs,escalations,deliveries,messages} answer with
counts, optionally grouped by one field and bucketed by the hour or the day,
so a page can draw charts without downloading the log.  This module checks
the query (parse) and builds the SQL (aggregate); the tables are read by
metrics.py (runs, escalations, deliveries) and forge.py (messages).

Everything is bounded: a value is at most MAX_VALUE characters, a bucketed
query covers at most MAX_BUCKETS buckets, and an answer has at most
MAX_GROUPS groups (MAX_ROWS rows when bucketed); a longer one is cut and says
`truncated`.  A filter value only ever reaches SQL as a bound parameter, and
a group or a column only from the fixed tables below.

Standard library only.
"""

import datetime
import math
import re
import time

MAX_VALUE = 200
MAX_BUCKETS = 500
MAX_GROUPS = 200
MAX_ROWS = 2000
BUCKETS = {'hour': 3600, 'day': 86400}

# What each kind can be grouped by and filtered by (besides the time range).
KINDS = {
    'runs': {'groups': ('job', 'result', 'pipeline', 'project', 'mr'),
             'filters': ('project', 'job', 'result', 'pipeline', 'mr')},
    'escalations': {'groups': ('job', 'project', 'mr'),
                    'filters': ('project', 'job', 'mr')},
    'deliveries': {'groups': ('kind', 'action', 'ok', 'status', 'project'),
                   'filters': ('project', 'kind', 'action', 'ok')},
    'messages': {'groups': ('action', 'sender', 'outcome', 'project'),
                 'filters': ('project', 'action', 'sender', 'outcome')},
}
OK_VALUES = {'1': 1, 'true': 1, '0': 0, 'false': 0}
NUMBER_RE = re.compile(r'[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?')


class QueryError(ValueError):
    """A query that cannot be answered: the message says what is valid."""


class Query:
    """A checked query.  group is a name or None; size is the bucket's
    seconds or None; since and until are what was asked (epoch seconds or
    None); lo and hi are the bounds the SQL uses (a bucketed query always has
    them); filters maps a filter name to its value."""

    def __init__(self, kind, group, bucket, since, until, filters, lo, hi):
        self.kind, self.group, self.bucket = kind, group, bucket
        self.size = BUCKETS.get(bucket)
        self.since, self.until, self.filters = since, until, filters
        self.lo, self.hi = lo, hi

    @property
    def limit(self):
        return MAX_ROWS if self.bucket else MAX_GROUPS

    def describe(self, rows, truncated):
        return {'kind': self.kind, 'group': self.group or 'none',
                'bucket': self.bucket, 'since': self.since,
                'until': self.until, 'truncated': truncated, 'rows': rows}


def _time(name, value):
    what = ('%s must be a number of seconds since 1970 or an ISO 8601 time, '
            'such as 2023-11-15T01:00:00Z' % name)
    if len(value) > 64:
        raise QueryError(what)
    if NUMBER_RE.fullmatch(value):
        seconds = float(value)
    else:
        try:
            when = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            raise QueryError(what) from None
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        try:
            seconds = when.timestamp()
        except (OverflowError, OSError, ValueError):
            raise QueryError(what) from None
    if not math.isfinite(seconds):
        raise QueryError(what)
    return seconds


def parse(kind, params, now=None):
    """Check the query-string parameters of one kind; return a Query."""
    spec = KINDS[kind]
    valid = sorted({'group', 'bucket', 'since', 'until'} | set(spec['filters']))
    for name in params:
        if name not in valid:
            raise QueryError("unknown parameter %r for %s; valid ones are: %s"
                             % (name, kind, ', '.join(valid)))
        if len(params[name]) > MAX_VALUE:
            raise QueryError('%s is longer than %d characters'
                             % (name, MAX_VALUE))
    group = params.get('group') or 'none'
    if group != 'none' and group not in spec['groups']:
        raise QueryError('group must be one of: %s' % ', '.join(
            sorted(spec['groups'] + ('none',))))
    bucket = params.get('bucket') or None
    if bucket is not None and bucket not in BUCKETS:
        raise QueryError('bucket must be one of: %s' % ', '.join(
            sorted(BUCKETS)))
    since = _time('since', params['since']) if 'since' in params else None
    until = _time('until', params['until']) if 'until' in params else None
    if since is not None and until is not None and since >= until:
        raise QueryError('since must be before until')
    filters = {}
    for name in spec['filters']:
        value = params.get(name)
        if value in (None, ''):
            continue
        if name == 'ok':
            if value.lower() not in OK_VALUES:
                raise QueryError('ok must be 1, 0, true or false')
            value = OK_VALUES[value.lower()]
        filters[name] = value
    lo, hi = since, until
    if bucket:
        size = BUCKETS[bucket]
        hi = until if until is not None else (now or time.time())
        lo = since if since is not None else hi - MAX_BUCKETS * size
        if math.ceil((hi - lo) / size) > MAX_BUCKETS:
            raise QueryError(
                'since to until covers more than %d %s buckets; narrow the '
                'range%s' % (MAX_BUCKETS, bucket,
                             ' or use bucket=day' if bucket == 'hour' else ''))
    return Query(kind, None if group == 'none' else group, bucket, since,
                 until, filters, lo, hi)


def aggregate(conn, query, spec):
    """Count rows of one table, as the answer to a query.

    spec: table, time (an SQL expression: epoch seconds, NULL when unknown),
    groups (name -> SQL expression), filters (name -> (SQL condition with ?
    placeholders, function from the value to its parameters)) and extra
    (more aggregates: list of (alias, SQL)).  All of it is fixed code in
    metrics.py and forge.py; only the query's values are parameters.
    """
    group_sql = spec['groups'][query.group] if query.group else 'NULL'
    select = ['%s AS g' % group_sql]
    keys = ['g']
    if query.size:
        select.append('CAST((%s) / %d AS INTEGER) * %d AS b' % (
            spec['time'], query.size, query.size))
        keys.append('b')
    select.append('count(*) AS n')
    select += ['%s AS x_%s' % (sql, alias) for alias, sql in spec['extra']]
    where, args = [], []
    for name, value in query.filters.items():
        condition, convert = spec['filters'][name]
        where.append(condition)
        args += convert(value)
    if query.lo is not None:
        where.append('(%s) >= ?' % spec['time'])
        args.append(query.lo)
    if query.hi is not None:
        where.append('(%s) < ?' % spec['time'])
        args.append(query.hi)
    # Cut a long series at its old end: the newest buckets are kept.
    order = 'b DESC, g ASC' if query.size else 'n DESC, g ASC'
    sql = 'SELECT %s FROM %s%s GROUP BY %s ORDER BY %s LIMIT %d' % (
        ', '.join(select), spec['table'],
        ' WHERE ' + ' AND '.join(where) if where else '', ', '.join(keys),
        order, query.limit + 1)
    found = conn.execute(sql, args).fetchall()
    truncated = len(found) > query.limit
    rows = []
    for r in found[:query.limit]:
        row = {'group': r['g'], 'bucket': r['b'] if query.size else None,
               'count': r['n']}
        for alias, _ in spec['extra']:
            row[alias] = r['x_' + alias]
        rows.append(row)
    if query.size:
        rows.sort(key=lambda r: (r['bucket'], '' if r['group'] is None
                                 else str(r['group'])))
    return query.describe(rows, truncated)


def count_rows(query, entries):
    """Count (group, bucket) pairs the way aggregate() does, for rows read
    into Python: entries is a list of (group, epoch seconds or None) pairs,
    one per thing counted."""
    counts = {}
    for group, at in entries:
        bucket = None
        if query.size:
            if at is None:
                continue
            bucket = int(at // query.size) * query.size
        counts[group, bucket] = counts.get((group, bucket), 0) + 1

    def order(item):
        (group, bucket), n = item
        text = '' if group is None else str(group)
        return (bucket, text) if query.size else (-n, text)
    ranked = sorted(counts.items(), key=order)
    truncated = len(ranked) > query.limit
    # A long series is cut at its old end: the newest buckets are kept.
    kept = ranked[-query.limit:] if query.size else ranked[:query.limit]
    rows = [{'group': g, 'bucket': b, 'count': n} for (g, b), n in kept]
    return query.describe(rows, truncated)
