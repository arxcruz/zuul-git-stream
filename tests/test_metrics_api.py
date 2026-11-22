"""The aggregate metrics endpoints behind the demo UI's metrics view.

GET /api/stream/metrics/{runs,escalations,deliveries,messages} answer with
counts (grouped, optionally bucketed in time) so a page never downloads the
log.  Spec: docs/specs/metrics-view.md, scenarios 3 and 7.

The data is fixed: T0 is a day boundary long ago, and every row sits at a
known offset from it, so each expected count below can be worked out by hand.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest


T0 = 1700006400  # 2023-11-15T00:00:00Z: a day (and hour) boundary
HOUR, DAY = 3600, 86400
TOKENS = {'build-system': 'tok-b', 'release-manager': 'tok-r'}


def build(job, result, pipeline, end, duration, uuid):
    times = {} if end is None else {'start_time': end - duration,
                                    'end_time': end, 'duration': duration}
    return dict(job_name=job, result=result, pipeline=pipeline, uuid=uuid,
                **times)


def fill(m):
    """Eight runs, three escalations and five deliveries (see the spec)."""
    def rec(project, iid, key, *args):
        m.record_results(project, iid, 's', [build(*args)], 'http://z',
                         key=key)
    rec('bundles', 1, 'b-1', 'bundle-api', 'SUCCESS', 'bundle-test',
        T0 + 100, 10, 'r1')
    rec('bundles', 1, 'b-1', 'bundle-api', 'FAILURE', 'bundle-test',
        T0 + 3700, 30, 'r2')
    rec('bundles', 1, 'b-1', 'bundle-security', 'FAILURE', 'bundle-test',
        T0 + 200, 20, 'r3')
    rec('bundles', 1, 'b-1', 'bundle-security', 'FAILURE', 'bundle-rerun',
        T0 + 3800, 40, 'r4')
    rec('bundles', 1, 'b-1', 'bundle-security', 'FAILURE', 'bundle-rerun',
        T0 + DAY + 100, 60, 'r5')
    rec('bundles', 2, 'b-2', 'bundle-api', 'SUCCESS', 'bundle-test',
        T0 + DAY + 500, 20, 'r6')
    rec('tools', 1, 'fix', 'lint', 'TIMED_OUT', 'bundle-test',
        T0 + 300, 100, 'r7')
    rec('bundles', 2, 'b-2', 'bundle-auth', 'SUCCESS', 'bundle-test',
        None, 0, 'r8')  # Zuul gave no times
    for key, iid, project, at, jobs in (
            ('b-1', 1, 'bundles', T0 + 1000,
             ['bundle-security', 'bundle-api']),
            ('b-2', 2, 'bundles', T0 + DAY + 1000, ['bundle-security']),
            ('fix', 1, 'tools', T0 + 2000, ['lint'])):
        m.record_escalation({
            'project': project, 'mr': iid, 'key': key, 'sha': 's',
            'time': at, 'max_attempts': 3,
            'failing': [{'job': j, 'attempts': 3} for j in jobs]})
    for kind, action, project, status, attempts, at in (
            ('merge_request', 'open', 'bundles', 200, 1, T0 + 10),
            ('note', None, 'bundles', 200, 1, T0 + 20),
            ('note', None, 'bundles', 'timed out', 5, T0 + 30),
            ('push', None, 'tools', 500, 3, T0 + 40),
            ('merge_request', 'update', 'bundles', 200, 2, T0 + 3700)):
        m.record_delivery({'time': at, 'url': 'http://hook/secret-token',
                           'kind': kind, 'action': action,
                           'project': project, 'status': status,
                           'attempts': attempts})


@pytest.fixture
def api(make_api):
    api = make_api()
    fill(api.metrics)
    return api


def by(rows, field='group'):
    return {r[field]: r for r in rows}


# -- runs ------------------------------------------------------------------

def test_runs_grouped_by_job(api):
    rows = api.rows('runs', group='job')
    assert [(r['group'], r['count']) for r in rows] == [
        ('bundle-api', 3), ('bundle-security', 3), ('bundle-auth', 1),
        ('lint', 1)]  # most runs first, ties by name
    api_row = by(rows)['bundle-api']
    assert (api_row['pass'], api_row['fail'], api_row['other']) == (2, 1, 0)
    assert api_row['avg_ms'] == 20000 and api_row['max_ms'] == 30000
    assert api_row['timed'] == 3
    assert by(rows)['bundle-security']['fail'] == 3
    assert by(rows)['lint']['fail'] == 1  # a timeout is a failure
    auth = by(rows)['bundle-auth']  # no times: counted, not timed
    assert (auth['pass'], auth['timed'], auth['avg_ms'],
            auth['max_ms']) == (1, 0, None, None)
    assert all(r['bucket'] is None for r in rows)


def test_runs_grouped_by_the_other_dimensions(api):
    assert [(r['group'], r['count']) for r in api.rows(
        'runs', group='result')] == [('FAILURE', 4), ('SUCCESS', 3),
                                     ('TIMED_OUT', 1)]
    assert [(r['group'], r['count']) for r in api.rows(
        'runs', group='pipeline')] == [('bundle-test', 6),
                                       ('bundle-rerun', 2)]
    assert [(r['group'], r['count']) for r in api.rows(
        'runs', group='project')] == [('bundles', 7), ('tools', 1)]
    assert [(r['group'], r['count']) for r in api.rows(
        'runs', group='mr')] == [('b-1', 5), ('b-2', 2), ('fix', 1)]


def test_runs_without_a_group_are_one_total(api):
    [row] = api.rows('runs')
    assert (row['group'], row['count'], row['pass'], row['fail'],
            row['other']) == (None, 8, 3, 5, 0)
    assert api.rows('runs', group='none') == [row]


@pytest.mark.parametrize('query, count', [
    ({'project': 'tools'}, 1),
    ({'job': 'bundle-security'}, 3),
    ({'result': 'FAILURE'}, 4),
    ({'pipeline': 'bundle-rerun'}, 2),
    ({'mr': 'b-1'}, 5),
    ({'mr': 'b-2'}, 2),
    ({'mr': '2'}, 2),  # by the merge request number as well as its key
    ({'project': 'bundles', 'job': 'bundle-api', 'result': 'SUCCESS'}, 2),
    ({'job': 'nothing'}, 0),
])
def test_runs_filters(api, query, count):
    rows = api.rows('runs', **query)
    assert (rows[0]['count'] if rows else 0) == count


def test_a_time_range_leaves_out_runs_without_times(api):
    assert api.rows('runs')[0]['count'] == 8
    [row] = api.rows('runs', since=T0 + HOUR, until=T0 + DAY)
    assert row['count'] == 2  # the runs ending at +3700 and +3800
    [row] = api.rows('runs', since=T0)  # open ended, still timed runs only
    assert row['count'] == 7
    [row] = api.rows('runs', until=T0 + 150)
    assert row['count'] == 1


def test_times_are_numbers_or_iso_strings(api):
    iso = '2023-11-15T01:00:00Z'  # T0 + one hour
    assert api.rows('runs', since=iso, until=T0 + DAY)[0]['count'] == 2
    assert api.rows('runs', since='2023-11-15T01:00:00',
                    until='2023-11-16')[0]['count'] == 2  # no zone is UTC
    assert api.rows('runs', since=str(T0 + HOUR),
                    until=T0 + DAY)[0]['count'] == 2


def test_runs_bucketed_by_the_hour(api):
    rows = api.rows('runs', bucket='hour', since=T0, until=T0 + 2 * HOUR)
    assert [(r['bucket'], r['count']) for r in rows] == [
        (T0, 3), (T0 + HOUR, 2)]


def test_runs_bucketed_by_the_day_and_grouped(api):
    rows = api.rows('runs', group='job', bucket='day', since=T0,
                    until=T0 + 2 * DAY)
    assert [(r['bucket'], r['group'], r['count']) for r in rows] == [
        (T0, 'bundle-api', 2), (T0, 'bundle-security', 2), (T0, 'lint', 1),
        (T0 + DAY, 'bundle-api', 1), (T0 + DAY, 'bundle-security', 1)]


def test_bucketing_leaves_out_runs_without_times(api):
    rows = api.rows('runs', bucket='day', since=T0, until=T0 + 2 * DAY)
    assert sum(r['count'] for r in rows) == 7  # not the eighth


def test_a_bucketed_query_without_a_start_covers_the_last_500_buckets(
        make_api):
    import time
    api = make_api()
    now = time.time()
    api.metrics.record_results('p', 1, 's', [
        build('recent', 'SUCCESS', 'x', now - 100, 10, 'a'),
        build('ancient', 'SUCCESS', 'x', now - 600 * HOUR, 10, 'b')],
        'http://z')
    rows = api.rows('runs', bucket='hour', group='job')
    assert [r['group'] for r in rows] == ['recent']


# -- escalations -----------------------------------------------------------

def test_escalations_grouped(api):
    assert api.rows('escalations')[0]['count'] == 3
    assert [(r['group'], r['count']) for r in api.rows(
        'escalations', group='project')] == [('bundles', 2), ('tools', 1)]
    assert [(r['group'], r['count']) for r in api.rows(
        'escalations', group='mr')] == [('b-1', 1), ('b-2', 1), ('fix', 1)]
    # One escalation counts once for each job that was still failing.
    assert [(r['group'], r['count']) for r in api.rows(
        'escalations', group='job')] == [
        ('bundle-security', 2), ('bundle-api', 1), ('lint', 1)]


@pytest.mark.parametrize('query, count', [
    ({'project': 'tools'}, 1), ({'mr': 'b-1'}, 1), ({'mr': '1'}, 2),
    ({'job': 'bundle-api'}, 1), ({'job': 'bundle-security'}, 2),
    ({'since': T0 + DAY}, 1), ({'until': T0 + HOUR}, 2),
    ({'job': 'nothing'}, 0)])
def test_escalation_filters(api, query, count):
    rows = api.rows('escalations', **query)
    assert (rows[0]['count'] if rows else 0) == count


def test_escalations_bucketed_by_the_day(api):
    rows = api.rows('escalations', bucket='day', since=T0,
                    until=T0 + 2 * DAY)
    assert [(r['bucket'], r['count']) for r in rows] == [
        (T0, 2), (T0 + DAY, 1)]


# -- deliveries ------------------------------------------------------------

def test_deliveries_grouped(api):
    [total] = api.rows('deliveries')
    assert (total['count'], total['ok'], total['failed'],
            total['attempts']) == (5, 3, 2, 12)
    kinds = by(api.rows('deliveries', group='kind'))
    assert (kinds['note']['count'], kinds['note']['ok'],
            kinds['note']['failed'], kinds['note']['attempts']) == (2, 1, 1, 6)
    assert kinds['merge_request']['ok'] == 2
    assert kinds['push']['failed'] == 1
    assert {r['group']: r['count'] for r in api.rows(
        'deliveries', group='ok')} == {'ok': 3, 'failed': 2}
    assert {r['group']: r['count'] for r in api.rows(
        'deliveries', group='status')} == {
        '200': 3, '500': 1, 'timed out': 1}
    assert {r['group']: r['count'] for r in api.rows(
        'deliveries', group='action')} == {None: 3, 'open': 1, 'update': 1}
    assert {r['group']: r['count'] for r in api.rows(
        'deliveries', group='project')} == {'bundles': 4, 'tools': 1}


@pytest.mark.parametrize('query, count', [
    ({'ok': '1'}, 3), ({'ok': 'true'}, 3), ({'ok': '0'}, 2),
    ({'ok': 'false'}, 2), ({'kind': 'note'}, 2), ({'action': 'open'}, 1),
    ({'project': 'tools'}, 1), ({'since': T0 + HOUR}, 1),
    ({'until': T0 + HOUR}, 4), ({'kind': 'nothing'}, 0)])
def test_delivery_filters(api, query, count):
    rows = api.rows('deliveries', **query)
    assert (rows[0]['count'] if rows else 0) == count


def test_deliveries_bucketed_by_the_hour(api):
    rows = api.rows('deliveries', bucket='hour', since=T0,
                    until=T0 + 2 * HOUR)
    assert [(r['bucket'], r['count'], r['failed']) for r in rows] == [
        (T0, 4, 2), (T0 + HOUR, 1, 0)]


def test_a_webhook_url_is_never_returned(api):
    for group in ('kind', 'status', 'ok', 'project', 'action'):
        assert 'secret-token' not in json.dumps(api.get(
            'deliveries', group=group)[1])
    status, body = api.get('deliveries', url='http://hook/secret-token')
    assert status == 400 and 'url' in body['message']
    status, body = api.get('deliveries', group='url')
    assert status == 400


# -- messages --------------------------------------------------------------

@pytest.fixture
def messages(make_api):
    api = make_api(metrics=False, stream_tokens=TOKENS)
    send = api.forge.send
    send({'action': 'create_project', 'project': 'bundles'}, token='tok-b')
    open_ = {'action': 'open_mr', 'project': 'bundles', 'title': 'T',
             'key': 'b-1', 'id': 'm1', 'data': {'id': 'b-1'}}
    send(open_, token='tok-b')
    send(open_, token='tok-b')  # a duplicate
    send(dict(open_, key='b-2', id='m2'), token='tok-r')
    for msg, token in (
            ({'action': 'explode', 'project': 'bundles'}, 'tok-b'),
            ({'action': 'comment', 'project': 'bundles', 'mr': 'b-1',
              'note': 'x', 'author': 'release-manager'}, 'tok-b'),
            ({'action': 'comment', 'project': 'bundles', 'mr': 'b-1',
              'note': 'y'}, 'tok-r')):
        try:
            send(msg, token=token)
        except urllib.error.HTTPError:
            pass
    return api


def test_messages_grouped(messages):
    def count(group):
        status, body = messages.get('messages', token='tok-r', group=group)
        assert status == 200, body
        return {r['group']: r['count'] for r in body['rows']}
    assert count('action') == {'open_mr': 3, 'comment': 2,
                               'create_project': 1, 'explode': 1}
    assert count('sender') == {'build-system': 5, 'release-manager': 2}
    assert count('outcome') == {'applied': 4, 'duplicate': 1, 'refused': 1,
                                'rejected': 1}
    assert count('project') == {'bundles': 7}
    assert count('none') == {None: 7}


@pytest.mark.parametrize('query, count', [
    ({'outcome': 'applied'}, 4), ({'outcome': 'refused'}, 1),
    ({'sender': 'release-manager'}, 2), ({'action': 'comment'}, 2),
    ({'project': 'bundles'}, 7), ({'project': 'other'}, 0),
    ({'since': '2999-01-01'}, 0), ({'until': '2000-01-01'}, 0),
    ({'since': '2000-01-01', 'until': '2999-01-01'}, 7)])
def test_message_filters(messages, query, count):
    status, body = messages.get('messages', token='tok-b', **query)
    assert status == 200, body
    assert (body['rows'][0]['count'] if body['rows'] else 0) == count


def test_messages_bucketed_by_the_hour(messages):
    import time
    now = time.time()
    status, body = messages.get('messages', token='tok-b', bucket='hour',
                                since=now - 2 * HOUR, until=now + 2 * HOUR)
    assert status == 200, body
    assert sum(r['count'] for r in body['rows']) == 7
    assert all(r['bucket'] % HOUR == 0 for r in body['rows'])


def test_messages_need_the_stream_token(messages):
    assert messages.get('messages')[0] == 401
    assert messages.get('messages', token='wrong')[0] == 401
    assert messages.get('messages', token='tok-r')[0] == 200


def test_messages_never_return_a_body_or_a_message_id(messages):
    for group in ('action', 'sender', 'outcome', 'project', 'none'):
        status, body = messages.get('messages', token='tok-b', group=group)
        text = json.dumps(body)
        assert 'build-system' in text or group != 'sender'
        for secret in ('"title"', '"note"', '"body"', 'm1', 'm2'):
            assert secret not in text, (group, secret)
        assert all(set(r) == {'group', 'bucket', 'count'}
                   for r in body['rows'])


def test_messages_work_with_metrics_off_and_the_others_are_a_404(messages):
    assert messages.metrics is None
    assert messages.get('messages', token='tok-b')[0] == 200
    for kind in ('runs', 'escalations', 'deliveries'):
        status, body = messages.get(kind)
        assert status == 404 and 'metrics not enabled' in body['message']


def test_without_sqlite_the_messages_aggregate_answers_501(make_api,
                                                           monkeypatch):
    import os
    from git_stream import forge as forge_module
    monkeypatch.setattr(
        forge_module, '_load_store',
        lambda d: forge_module.JsonStore(os.path.join(d, 'state.json')))
    api = make_api(metrics=False)
    assert api.get('messages')[0] == 501


# -- the endpoints are anonymous, except the messages -----------------------

def test_runs_escalations_and_deliveries_need_no_token(make_api):
    api = make_api(stream_tokens=TOKENS)
    fill(api.metrics)
    for kind in ('runs', 'escalations', 'deliveries'):
        assert api.get(kind)[0] == 200, kind
    assert api.get('messages')[0] == 401


# -- bad input is a 400 that says what is valid ------------------------------

KINDS = ['runs', 'escalations', 'deliveries', 'messages']
VALID_GROUPS = {'runs': 'job', 'escalations': 'job', 'deliveries': 'kind',
                'messages': 'action'}


@pytest.fixture
def full(make_api):
    api = make_api(stream_tokens=TOKENS)
    fill(api.metrics)
    return api


@pytest.mark.parametrize('kind', KINDS)
def test_an_unknown_group_is_a_400_that_lists_the_valid_ones(full, kind):
    status, body = full.get(kind, token='tok-b', group='bogus')
    assert status == 400
    assert 'group' in body['message'] and VALID_GROUPS[kind] in body['message']
    assert 'none' in body['message']


@pytest.mark.parametrize('kind', KINDS)
def test_an_unknown_bucket_is_a_400(full, kind):
    status, body = full.get(kind, token='tok-b', bucket='week')
    assert status == 400 and 'hour' in body['message'] \
        and 'day' in body['message']


@pytest.mark.parametrize('kind', KINDS)
def test_an_unknown_parameter_is_a_400(full, kind):
    status, body = full.get(kind, token='tok-b', colour='red')
    assert status == 400 and 'colour' in body['message']


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('name, value', [
    ('since', 'yesterday'), ('until', '12:00'), ('since', ''),
    ('until', 'nan'), ('since', 'inf'), ('since', '-1e400'),
    ('since', '9' * 400)])
def test_a_bad_time_is_a_400(full, kind, name, value):
    status, body = full.get(kind, token='tok-b', **{name: value})
    assert status == 400 and name in body['message']


@pytest.mark.parametrize('kind', KINDS)
def test_since_after_until_is_a_400(full, kind):
    status, body = full.get(kind, token='tok-b', since=T0 + 10, until=T0)
    assert status == 400 and 'since' in body['message']


@pytest.mark.parametrize('kind', KINDS)
def test_too_many_buckets_is_a_400_and_the_limit_is_fine(full, kind):
    status, body = full.get(kind, token='tok-b', bucket='hour', since=T0,
                            until=T0 + 501 * HOUR)
    assert status == 400 and '500' in body['message']
    assert full.get(kind, token='tok-b', bucket='hour', since=T0,
                    until=T0 + 500 * HOUR)[0] == 200
    assert full.get(kind, token='tok-b', bucket='day', since=T0,
                    until=T0 + 500 * DAY)[0] == 200


@pytest.mark.parametrize('kind', KINDS)
def test_a_too_long_filter_is_a_400(full, kind):
    name = {'runs': 'job', 'escalations': 'job', 'deliveries': 'kind',
            'messages': 'action'}[kind]
    status, body = full.get(kind, token='tok-b', **{name: 'x' * 201})
    assert status == 400 and name in body['message']


def test_a_bad_ok_is_a_400(full):
    status, body = full.get('deliveries', ok='maybe')
    assert status == 400 and 'ok' in body['message']


def test_a_filter_the_kind_does_not_have_is_a_400(full):
    for kind, name in (('runs', 'ok'), ('runs', 'sender'),
                       ('escalations', 'result'), ('deliveries', 'job'),
                       ('messages', 'pipeline')):
        status, body = full.get(kind, token='tok-b', **{name: 'x'})
        assert status == 400 and name in body['message'], (kind, name)


def test_a_filter_is_a_value_never_sql(full):
    for evil in ("x' OR '1'='1", "'; DROP TABLE run; --", '%', '_',
                 'bundle-api" OR 1=1 --'):
        assert full.rows('runs', job=evil) == []
    assert full.rows('runs', group='job')[0]['count'] == 3  # still there


def test_only_a_get_is_answered(full):
    for kind in KINDS:
        request = urllib.request.Request(
            '%s/api/stream/metrics/%s' % (full.url, kind), data=b'{}',
            method='POST', headers={'X-Stream-Token': 'tok-b'})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(request)
        assert e.value.code in (404, 405)


def test_an_unknown_kind_is_a_404(full):
    assert full.get('nothing')[0] == 404


# -- bounds ----------------------------------------------------------------

def test_a_long_list_of_groups_is_cut_and_says_so(make_api):
    api = make_api()
    api.metrics.record_results('p', 1, 's', [
        build('job-%03d' % n, 'SUCCESS', 'x', T0 + n, 1, 'u%d' % n)
        for n in range(250)], 'http://z')
    status, body = api.get('runs', group='job')
    assert status == 200
    assert len(body['rows']) == 200 and body['truncated'] is True
    status, body = api.get('runs', group='job', job='job-007')
    assert body['truncated'] is False and len(body['rows']) == 1


def test_the_answer_echoes_what_was_asked(full):
    status, body = full.get('runs', group='job', bucket='day', since=T0,
                            until=T0 + DAY)
    assert (body['kind'], body['group'], body['bucket']) == (
        'runs', 'job', 'day')
    assert (body['since'], body['until']) == (T0, T0 + DAY)
    assert body['truncated'] is False


def test_the_old_summary_endpoint_is_unchanged(full):
    with urllib.request.urlopen(full.url + '/api/stream/metrics') as r:
        data = json.load(r)
    assert data['runs'] == 8
    assert {j['job'] for j in data['jobs']} == {
        'bundle-api', 'bundle-security', 'bundle-auth', 'lint'}
