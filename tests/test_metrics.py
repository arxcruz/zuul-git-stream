import http.server
import json
import threading
import urllib.error
import urllib.request

from conftest import FAST_POLL
from git_stream.metrics import Metrics


def test_record_and_summary(tmp_path):
    m = Metrics(str(tmp_path / 'metrics.db'))
    builds = [
        {'job_name': 'bundle-api', 'result': 'SUCCESS', 'uuid': 'u1',
         'log_url': 'http://l/api/', 'start_time': 1000.0,
         'end_time': 1005.0},
        {'job_name': 'bundle-security', 'result': 'FAILURE', 'uuid': 'u2'},
        {'job_name': 'bundle-auth', 'result': 'SKIPPED', 'uuid': 'u3'},
    ]
    m.record_results('bundles', 1, 'shaA', builds, 'http://zuul/build')
    # A retry of the same version adds more rows.
    m.record_results('bundles', 1, 'shaA',
                     [{'job_name': 'bundle-api', 'result': 'SUCCESS',
                       'uuid': 'u4'}], 'http://zuul/build')
    s = m.summary()
    by_job = {j['job']: j for j in s['jobs']}
    assert s['runs'] == 3  # the SKIPPED build is not a run
    assert by_job['bundle-api']['runs'] == 2
    assert by_job['bundle-api']['pass'] == 2 and by_job['bundle-api']['fail'] == 0
    assert by_job['bundle-api']['last'] == 1005.0
    assert by_job['bundle-api']['fail_rate'] == 0.0
    assert by_job['bundle-api']['results'] == {'SUCCESS': 2}
    assert by_job['bundle-security']['fail'] == 1
    assert by_job['bundle-security']['fail_rate'] == 1.0
    # Newest job first.
    assert [j['job'] for j in s['jobs']] == [
        'bundle-api', 'bundle-security']
    m.close()


def test_filter_by_project_and_days(tmp_path):
    import time
    m = Metrics(str(tmp_path / 'metrics.db'))
    t = time.time()
    m.record_results('bundles', 1, 's',
                     [{'job_name': 'a', 'result': 'FAILURE', 'uuid': '1',
                       'start_time': t - 3600, 'end_time': t}], 'http://z')
    m.record_results('other', 2, 's',
                     [{'job_name': 'a', 'result': 'SUCCESS', 'uuid': '2',
                       'start_time': t - 3600, 'end_time': t}], 'http://z')
    assert m.summary()['runs'] == 2
    assert m.summary(project='bundles')['runs'] == 1
    assert m.summary(project='other')['runs'] == 1
    # A window that ends before the runs returns nothing; a wide one, all.
    assert m.summary(days=-1)['runs'] == 0
    assert m.summary(days=1)['runs'] == 2
    m.close()


def test_escalations_and_deliveries(tmp_path):
    m = Metrics(str(tmp_path / 'metrics.db'))
    m.record_escalation({'event': 'escalation', 'project': 'bundles',
                         'mr': 1, 'key': 'b-1', 'sha': 's', 'time': 5.0,
                         'max_attempts': 3,
                         'failing': [{'job': 'bundle-security', 'attempts': 3}]})
    [esc] = m.escalations()
    assert esc['key'] == 'b-1' and esc['max_attempts'] == 3
    assert esc['failing'][0]['job'] == 'bundle-security'
    m.record_delivery({'time': 30.0, 'url': 'http://z',
                       'kind': 'merge_request', 'action': 'open',
                       'project': 'bundles', 'status': 200, 'attempts': 1})
    m.record_delivery({'time': 10.0, 'url': 'http://z', 'kind': 'note',
                       'action': None, 'project': 'bundles',
                       'status': 'timed out', 'attempts': 5})
    [ok, bad] = m.deliveries()
    assert int(ok['status']) == 200 and ok['ok'] == 1
    assert bad['status'] == 'timed out' and bad['ok'] == 0
    m.close()


def test_metrics_endpoint(forge, tmp_path):
    metrics = Metrics(str(tmp_path / 'metrics.db'))
    metrics.record_results('bundles', 1, 'shaA',
                           [{'job_name': 'bundle-api', 'result': 'FAILURE',
                             'uuid': 'u1'}], 'http://zuul/build')
    from git_stream.server import serve
    server = serve(forge.forge, None, '127.0.0.1', 0, metrics=metrics)
    url = 'http://127.0.0.1:%d' % server.server_address[1]
    thread = __import__('threading').Thread(target=server.serve_forever,
                                             kwargs=FAST_POLL, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(url + '/api/stream/metrics') as r:
            data = json.load(r)
        assert data['runs'] == 1
        assert data['jobs'][0]['job'] == 'bundle-api'
        with urllib.request.urlopen(url + '/metrics') as r:
            page = r.read().decode()
        assert 'Fail rate' in page and 'bundle-api' in page
        # With no metrics configured the endpoint is a 404.
        bare = serve(forge.forge, None, '127.0.0.1', 0)
        bare_url = 'http://127.0.0.1:%d' % bare.server_address[1]
        bare_thread = __import__('threading').Thread(
            target=bare.serve_forever, kwargs=FAST_POLL, daemon=True)
        bare_thread.start()
        try:
            urllib.request.urlopen(bare_url + '/api/stream/metrics')
            raised = False
        except urllib.error.HTTPError as e:
            raised = e.code == 404
        assert raised
        bare.shutdown()
        bare.server_close()
    finally:
        server.shutdown()
        server.server_close()
        metrics.close()


def test_config_metrics_defaults(tmp_path):
    from git_stream import config
    s = config.load(environ={})
    assert s['metrics']['path'] == ''
    assert s['metrics']['build_url'] == ''


# -- what a run records: once per build, with its times (metrics-view spec,
#    scenarios 1, 2 and 8) -----------------------------------------------

ZUUL_BUILD = {
    # What Zuul's builds API really sends: ISO strings and a duration.
    'job_name': 'bundle-api', 'result': 'SUCCESS', 'uuid': 'u1',
    'pipeline': 'bundle-test', 'log_url': 'http://l/u1/',
    'start_time': '2026-11-22T23:32:58', 'end_time': '2026-11-22T23:33:15',
    'duration': 17.0}
START = 1795390378.0  # 2026-11-22T23:32:58 UTC


def runs(m):
    return [dict(r) for r in m.conn.execute('SELECT * FROM run ORDER BY id')]


def test_a_build_is_recorded_once(tmp_path):
    m = Metrics(str(tmp_path / 'metrics.db'))
    for _ in range(3):  # Zuul reports again for every retry of a version
        m.record_results('bundles', 1, 'sha', [ZUUL_BUILD], 'http://z')
    assert len(runs(m)) == 1
    assert m.summary()['runs'] == 1
    m.record_results('bundles', 1, 'sha',
                     [dict(ZUUL_BUILD, uuid='u2')], 'http://z')
    assert len(runs(m)) == 2
    m.close()


def test_builds_without_a_uuid_are_all_kept(tmp_path):
    m = Metrics(str(tmp_path / 'metrics.db'))
    build = {'job_name': 'a', 'result': 'SUCCESS'}
    m.record_results('bundles', 1, 's', [build, build], 'http://z')
    assert len(runs(m)) == 2
    m.close()


def test_a_database_with_duplicates_is_deduplicated_when_it_opens(tmp_path):
    path = str(tmp_path / 'metrics.db')
    first = Metrics(path)
    first.close()
    import sqlite3
    db = sqlite3.connect(path)
    db.execute('DROP INDEX IF EXISTS run_uuid')  # as an old version left it
    for n in range(3):
        db.execute("INSERT INTO run(project, mr, job, result, uuid)"
                   " VALUES ('p', 1, 'a', 'SUCCESS', 'dup')")
    db.execute("INSERT INTO run(project, mr, job, result, uuid)"
               " VALUES ('p', 1, 'b', 'FAILURE', 'other')")
    db.execute("INSERT INTO run(project, mr, job, result, uuid)"
               " VALUES ('p', 1, 'c', 'FAILURE', NULL)")
    db.execute("INSERT INTO run(project, mr, job, result, uuid)"
               " VALUES ('p', 1, 'c', 'FAILURE', NULL)")
    db.commit()
    db.close()
    again = Metrics(path)
    got = sorted((r['uuid'] or '-', r['job']) for r in runs(again))
    assert got == [('-', 'c'), ('-', 'c'), ('dup', 'a'), ('other', 'b')]
    again.close()
    Metrics(path).close()  # opening twice is harmless


def test_times_come_from_zuuls_iso_strings(tmp_path):
    m = Metrics(str(tmp_path / 'metrics.db'))
    m.record_results('bundles', 1, 'sha', [ZUUL_BUILD], 'http://z')
    [run] = runs(m)
    assert run['started_at'] == START
    assert run['finished_at'] == START + 17
    assert run['elapsed_ms'] == 17000
    m.close()


def test_a_start_and_an_end_are_not_the_same_instant(tmp_path):
    m = Metrics(str(tmp_path / 'metrics.db'))
    m.record_results('bundles', 1, 'sha',
                     [dict(ZUUL_BUILD, start_time=1000.0, end_time=1005.5)],
                     'http://z')
    [run] = runs(m)
    assert (run['started_at'], run['finished_at']) == (1000.0, 1005.5)
    assert run['elapsed_ms'] == 5500
    m.close()


def test_a_duration_alone_gives_the_elapsed_time(tmp_path):
    m = Metrics(str(tmp_path / 'metrics.db'))
    m.record_results('bundles', 1, 'sha',
                     [{'job_name': 'a', 'result': 'SUCCESS', 'uuid': 'u',
                       'duration': 4.25}], 'http://z')
    [run] = runs(m)
    assert run['elapsed_ms'] == 4250
    assert run['finished_at'] is None
    m.close()


def test_a_build_without_usable_times_is_counted_without_them(tmp_path):
    m = Metrics(str(tmp_path / 'metrics.db'))
    m.record_results('bundles', 1, 'sha',
                     [{'job_name': 'a', 'result': 'FAILURE', 'uuid': 'u1',
                       'start_time': 'not a time', 'end_time': None,
                       'duration': 'soon'}], 'http://z')
    [run] = runs(m)
    assert run['finished_at'] is None and run['elapsed_ms'] is None
    assert m.summary()['runs'] == 1
    m.close()


def test_days_matches_runs_that_have_iso_times(tmp_path):
    import datetime
    m = Metrics(str(tmp_path / 'metrics.db'))
    end = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    start = end - datetime.timedelta(seconds=30)
    m.record_results('bundles', 1, 'sha', [dict(
        ZUUL_BUILD, start_time=start.isoformat(timespec='seconds'),
        end_time=end.isoformat(timespec='seconds'), duration=30.0)],
        'http://z')
    assert m.summary(days=1)['runs'] == 1
    assert m.summary(days=-1)['runs'] == 0
    m.close()


def test_the_pipeline_and_the_bundle_key_are_recorded(tmp_path):
    m = Metrics(str(tmp_path / 'metrics.db'))
    m.record_results('bundles', 1, 'sha', [ZUUL_BUILD], 'http://z',
                     key='b-1')
    [run] = runs(m)
    assert run['pipeline'] == 'bundle-test'
    assert run['key'] == 'b-1'
    m.close()


def test_an_old_database_gains_the_new_columns_and_keeps_its_rows(tmp_path):
    import sqlite3
    path = str(tmp_path / 'old.db')
    db = sqlite3.connect(path)
    db.executescript('''
    CREATE TABLE run (id INTEGER PRIMARY KEY, project TEXT, mr INTEGER,
      change TEXT, patchset TEXT, job TEXT, result TEXT, uuid TEXT,
      log_url TEXT, build_url TEXT, started_at REAL, finished_at REAL,
      elapsed_ms REAL);
    INSERT INTO run(project, mr, job, result, uuid)
      VALUES ('p', 1, 'a', 'SUCCESS', 'u');
    ''')
    db.commit()
    db.close()
    m = Metrics(path)
    [run] = runs(m)
    assert run['job'] == 'a' and run['pipeline'] is None and run['key'] is None
    m.record_results('p', 2, 's', [ZUUL_BUILD], 'http://z', key='k')
    assert len(runs(m)) == 2
    m.close()


def test_the_retry_policy_passes_the_bundle_key(forge, tmp_path):
    """results() logs the builds with the MR's key, so charts can say b-1."""
    from git_stream.retry import RetryPolicy
    forge.send({'action': 'open_mr', 'project': 'bundles', 'title': 'T',
                'key': 'b-1', 'data': {'id': 'b-1'}})
    seen = {}

    class Spy:
        def record_results(self, project, iid, sha, builds, prefix,
                           key=None):
            seen['key'] = key

    class Zuul(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps([]).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    zuul = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Zuul)
    threading.Thread(target=zuul.serve_forever, kwargs=FAST_POLL,
                     daemon=True).start()
    try:
        policy = RetryPolicy(
            forge.forge, 'http://127.0.0.1:%d' % zuul.server_address[1],
            metrics=Spy())
        policy.results('bundles', 1, 'sha')
    finally:
        zuul.shutdown()
        zuul.server_close()
    assert seen == {'key': 'b-1'}
