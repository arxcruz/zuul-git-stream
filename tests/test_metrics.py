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
