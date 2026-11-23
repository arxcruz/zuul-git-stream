"""The demo UI's metrics page: its back end, its page and its JavaScript.

Spec: docs/specs/metrics-view.md, scenarios 4 to 7.  The demo UI serves
/metrics (metrics.html and metrics.js) and proxies the aggregate endpoints at
/api/metrics/<kind>.  The page's logic (filters in the URL, the query each
chart sends, what an error says) is plain JavaScript in metrics.js, run here
under node; the charts themselves are looked at in the demo.
"""

import http.server
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from conftest import FAST_POLL
from test_demo_scripts import DEVENV, load
from test_metrics_api import DAY, T0, TOKENS, fill

UI = os.path.join(DEVENV, 'demo-ui')
KINDS = ('runs', 'escalations', 'deliveries', 'messages')
SECTIONS = ('jobs', 'bundles', 'webhooks', 'messages')
GROUPS = {
    'runs': {'job', 'result', 'pipeline', 'project', 'mr', 'none'},
    'escalations': {'job', 'project', 'mr', 'none'},
    'deliveries': {'kind', 'action', 'ok', 'status', 'project', 'none'},
    'messages': {'action', 'sender', 'outcome', 'project', 'none'},
}
CHART_IDS = {
    'jobs-results', 'jobs-failrate', 'runs-over-time', 'jobs-duration',
    'pipelines-results', 'bundle-attempts', 'escalations-over-time',
    'escalations-jobs', 'webhooks-over-time', 'webhooks-ok',
    'webhooks-kinds', 'messages-over-time', 'messages-actions',
    'messages-senders', 'messages-outcomes'}
FILTER_IDS = ('project', 'job', 'result', 'pipeline', 'bundle', 'range',
              'since', 'until', 'kind', 'daction', 'ok', 'maction', 'sender',
              'outcome')


# -- the demo UI, served on a free port, in front of a git-stream ----------

@pytest.fixture
def ui(make_api, monkeypatch):
    api = make_api(stream_tokens=TOKENS)
    fill(api.metrics)
    app = load('demo_ui_metrics_app', 'demo-ui/app.py')
    app.FORGE = api.url
    app.STREAM_TOKEN = 'tok-b'
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
    threading.Thread(target=server.serve_forever, kwargs=FAST_POLL,
                     daemon=True).start()
    ui = type('UI', (), {})()
    ui.app, ui.api = app, api
    ui.url = 'http://127.0.0.1:%d' % server.server_address[1]

    def get(path):
        try:
            with urllib.request.urlopen(ui.url + path) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()
    ui.get = get
    yield ui
    server.shutdown()
    server.server_close()


def json_of(response):
    status, _, body = response
    return status, json.loads(body)


# -- the page and its files ----------------------------------------------------

def test_the_metrics_page_and_its_script_are_served(ui):
    status, headers, body = ui.get('/metrics')
    assert status == 200 and headers['Content-Type'].startswith('text/html')
    assert b'<title>' in body
    status, headers, body = ui.get('/metrics.js')
    assert status == 200
    assert 'javascript' in headers['Content-Type']


def test_the_main_page_links_to_the_metrics_page():
    with open(os.path.join(UI, 'index.html')) as f:
        assert re.search(r'href="/metrics"', f.read())


def page():
    with open(os.path.join(UI, 'metrics.html')) as f:
        return f.read()


def test_the_page_loads_chartjs_from_one_pinned_cdn_url_and_nothing_else():
    html = page()
    urls = set(re.findall(r'(?:src|href)="(https?://[^"]+)"', html))
    assert len(urls) == 1, urls
    [url] = urls
    assert re.fullmatch(
        r'https://cdnjs\.cloudflare\.com/ajax/libs/Chart\.js/'
        r'\d+\.\d+\.\d+/chart\.umd(\.min)?\.js', url), url
    assert not re.search(r'@import|url\(\s*[\'"]?https?:', html)


def test_the_chart_library_has_an_integrity_hash():
    """Spec constraint: the pinned script carries its hash."""
    html = page()
    assert re.search(r'integrity="sha(256|384|512)-[A-Za-z0-9+/=]{40,}"',
                     html)
    assert 'crossorigin="anonymous"' in html


def test_the_page_has_every_filter_section_and_chart():
    html = page()
    for name in FILTER_IDS:
        assert 'id="f-%s"' % name in html, name
    for name in SECTIONS:
        assert 'id="sec-%s"' % name in html, name
    for ident in ('totals', 'notice', 'lib-missing'):
        assert 'id="%s"' % ident in html, ident
    for chart in charts()['CHARTS']:
        assert 'id="chart-%s"' % chart['id'] in html, chart['id']


# -- the back end: the proxy ---------------------------------------------------

def test_the_proxy_returns_what_git_stream_returns(ui):
    status, body = json_of(ui.get('/api/metrics/runs?group=job'))
    direct = ui.api.get('runs', group='job')[1]
    assert status == 200 and body == direct
    assert body['rows'][0]['group'] == 'bundle-api'


def test_the_proxy_passes_the_query_through(ui):
    status, body = json_of(ui.get(
        '/api/metrics/deliveries?group=ok&since=%d&until=%d' % (
            T0, T0 + DAY)))
    assert status == 200
    assert {r['group']: r['count'] for r in body['rows']} == {
        'ok': 3, 'failed': 2}


def test_the_proxy_only_forwards_the_four_kinds(ui):
    for path in ('/api/metrics/secrets', '/api/metrics/', '/api/metrics/runs/x',
                 '/api/metrics/..%2Fescalations',
                 '/api/metrics/runs%2F..%2F..%2Fv4'):
        assert ui.get(path)[0] == 404, path


def test_the_old_summary_proxy_still_works(ui):
    status, body = json_of(ui.get('/api/metrics?days=5000'))
    assert status == 200 and 'jobs' in body


def test_a_git_stream_error_reaches_the_page_with_its_status_and_message(ui):
    status, body = json_of(ui.get('/api/metrics/runs?group=bogus'))
    assert status == 400 and 'group' in body['message']
    status, body = json_of(ui.get('/api/metrics/runs?colour=red'))
    assert status == 400 and 'colour' in body['message']


def test_metrics_turned_off_is_a_404_with_its_message(make_api):
    api = make_api(metrics=False)
    app = load('demo_ui_metrics_off', 'demo-ui/app.py')
    app.FORGE = api.url
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
    threading.Thread(target=server.serve_forever, kwargs=FAST_POLL,
                     daemon=True).start()
    try:
        url = 'http://127.0.0.1:%d/api/metrics/runs' % server.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url)
        assert e.value.code == 404
        assert 'metrics not enabled' in json.load(e.value)['message']
    finally:
        server.shutdown()
        server.server_close()


def test_git_stream_being_down_is_a_502_that_says_so(ui):
    ui.app.FORGE = 'http://127.0.0.1:9'
    status, body = json_of(ui.get('/api/metrics/runs'))
    assert status == 502 and ('error' in body or 'message' in body)


def test_the_stream_token_is_sent_for_messages_and_never_returned(ui):
    ui.api.forge.send({'action': 'create_project', 'project': 'p'},
                      token='tok-b')
    ui.app.STREAM_TOKEN = ''  # no token configured: git-stream refuses
    assert ui.get('/api/metrics/messages?group=outcome')[0] == 401
    assert ui.get('/api/metrics/runs')[0] == 200  # those need no token
    ui.app.STREAM_TOKEN = 'tok-b'
    status, body = json_of(ui.get('/api/metrics/messages?group=outcome'))
    assert status == 200 and body['rows']
    assert 'tok-b' not in json.dumps(body)
    status, headers, text = ui.get('/api/metrics/messages')
    assert b'tok-b' not in text and 'tok-b' not in str(headers)


def test_the_proxy_answers_a_400_for_a_query_that_cannot_be_sent(ui):
    status, body = ui.app.forge_metrics('runs', 'job=a b\x00c')
    assert status == 400 and 'message' in body


def test_the_stream_token_goes_to_the_messages_query_only(ui):
    seen = []
    real = ui.app.urllib.request.Request

    def spy(url, headers=None, **kw):
        seen.append((url, dict(headers or {})))
        return real(url, headers=headers or {}, **kw)
    ui.app.urllib.request.Request = spy
    try:
        ui.app.forge_metrics('runs', '')
        ui.app.forge_metrics('messages', '')
    finally:
        ui.app.urllib.request.Request = real
    assert seen[0][1] == {} and seen[1][1] == {'X-Stream-Token': 'tok-b'}


def test_the_proxy_refuses_an_absurdly_long_query(ui):
    status, _, _ = ui.get('/api/metrics/runs?job=' + 'x' * 5000)
    assert status in (400, 414)


# -- the page's JavaScript, under node ----------------------------------------

NO_NODE = ('node is needed to run the metrics page\'s JavaScript tests: '
           'install Node.js (https://nodejs.org), then run them again')


def need_node():
    """Fail, never skip: without node the page's logic goes untested."""
    if shutil.which('node') is None:
        pytest.fail(NO_NODE)


def run_js(expression):
    """The JSON value of an expression over metrics.js's exports."""
    need_node()
    script = ('const m = require(%s); '
              'process.stdout.write(JSON.stringify(%s));' % (
                  json.dumps(os.path.join(UI, 'metrics.js')), expression))
    done = subprocess.run(['node', '-e', script], capture_output=True,
                          text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def charts():
    return run_js('{CHARTS: m.CHARTS, TOTALS: m.TOTALS}')


def test_the_catalog_has_every_chart_of_the_spec():
    found = charts()
    assert {c['id'] for c in found['CHARTS']} == CHART_IDS
    assert len({c['id'] for c in found['CHARTS']}) == len(found['CHARTS'])
    for c in found['CHARTS']:
        assert c['kind'] in KINDS and c['section'] in SECTIONS, c
        assert c['group'] in GROUPS[c['kind']], c
        assert c['type'] in ('bar', 'stackedBar', 'line', 'doughnut'), c
        assert isinstance(c['timeseries'], bool) and c['title'], c
    assert {t['id'] for t in found['TOTALS']} == {
        'runs', 'pass-rate', 'escalations', 'webhooks-failed',
        'messages-rejected'}


def test_every_chart_query_is_one_git_stream_accepts(ui):
    """Run each chart's real query, over the range the fixed data is in."""
    filters = {'range': 'custom', 'since': '2023-11-14', 'until': '2023-11-18'}
    found = charts()
    for chart in found['CHARTS'] + found['TOTALS']:
        query = run_js('m.apiQuery(%s, %s, %d)' % (
            json.dumps(chart), json.dumps(filters), T0))
        path = '/api/metrics/%s?%s' % (
            query['kind'], urllib.parse.urlencode(query['params']))
        status, body = json_of(ui.get(path))
        assert status == 200, (chart['id'], path, body)
        assert isinstance(body['rows'], list)
        if query['kind'] != 'messages':  # those are from now, not from 2023
            assert body['rows'], chart['id']  # the data is in range


def test_a_filter_goes_only_to_the_kinds_that_have_it():
    def params(kind, filters):
        chart = {'id': 'x', 'kind': kind, 'group': 'none',
                 'timeseries': False}
        return run_js('m.apiQuery(%s, %s, 1000000)' % (
            json.dumps(chart), json.dumps(filters)))['params']
    filters = {'range': 'all', 'project': 'bundles', 'job': 'a',
               'result': 'FAILURE', 'pipeline': 'p', 'bundle': 'b-1',
               'kind': 'note', 'daction': 'open', 'ok': '1',
               'maction': 'comment', 'sender': 's', 'outcome': 'applied'}
    assert params('runs', filters) == {
        'group': 'none', 'project': 'bundles', 'job': 'a',
        'result': 'FAILURE', 'pipeline': 'p', 'mr': 'b-1'}
    assert params('escalations', filters) == {
        'group': 'none', 'project': 'bundles', 'job': 'a', 'mr': 'b-1'}
    assert params('deliveries', filters) == {
        'group': 'none', 'project': 'bundles', 'kind': 'note',
        'action': 'open', 'ok': '1'}
    assert params('messages', filters) == {
        'group': 'none', 'project': 'bundles', 'action': 'comment',
        'sender': 's', 'outcome': 'applied'}


def test_a_quick_range_becomes_a_start_time_and_a_bucket():
    now = 1700000000

    def query(rng, timeseries=True):
        chart = {'id': 'x', 'kind': 'runs', 'group': 'none',
                 'timeseries': timeseries}
        return run_js('m.apiQuery(%s, {range: %s}, %d)' % (
            json.dumps(chart), json.dumps(rng), now))['params']
    assert query('1h') == {'group': 'none', 'bucket': 'hour',
                           'since': now - 3600}
    assert query('24h')['since'] == now - 86400
    assert query('7d') == {'group': 'none', 'bucket': 'hour',
                           'since': now - 7 * 86400}
    assert query('30d')['bucket'] == 'day'
    assert query('all') == {'group': 'none', 'bucket': 'day'}
    assert 'bucket' not in query('24h', timeseries=False)


def test_a_custom_range_chooses_its_bucket_and_refuses_too_wide_a_span():
    def query(since, until):
        chart = {'id': 'x', 'kind': 'runs', 'group': 'none',
                 'timeseries': True}
        return run_js('m.apiQuery(%s, %s, 1700000000)' % (
            json.dumps(chart), json.dumps(
                {'range': 'custom', 'since': since, 'until': until})))
    assert query('2023-11-14', '2023-11-17')['params']['bucket'] == 'hour'
    assert query('2023-01-01', '2023-11-17')['params']['bucket'] == 'day'
    assert query('2023-11-14', '2023-11-17')['params']['since'] == (
        '2023-11-14')
    wide = query('2000-01-01', '2023-11-17')
    assert wide['error'] and '500' in wide['error']


def test_the_filters_live_in_the_url_and_come_back():
    assert run_js("m.parseFilters('')") == {'range': '24h'}
    got = run_js("m.parseFilters('?project=bundles&job=bundle-api"
                 "&range=7d&bogus=1&bundle=b%2D1')")
    assert got == {'project': 'bundles', 'job': 'bundle-api',
                   'range': '7d', 'bundle': 'b-1'}
    assert run_js("m.parseFilters('?range=nonsense')") == {'range': '24h'}
    assert run_js("m.toSearch({range: '24h'})") == ''
    assert run_js("m.toSearch({project: 'a b', job: '', range: '7d'})") == (
        '?project=a%20b&range=7d')
    for search in ('?project=bundles&range=30d', '?job=a%26b&outcome=applied',
                   '?range=custom&since=2023-11-14&until=2023-11-17'):
        assert run_js('m.toSearch(m.parseFilters(%s))' % json.dumps(
            search)) == run_js('m.toSearch(m.parseFilters(m.toSearch('
                               'm.parseFilters(%s))))' % json.dumps(search))


def test_filter_values_cannot_inject_parameters():
    got = run_js("m.parseFilters('?job=a%26group%3Dresult')")
    assert got['job'] == 'a&group=result'
    chart = {'id': 'x', 'kind': 'runs', 'group': 'none', 'timeseries': False}
    query = run_js('m.apiQuery(%s, %s, 1)' % (
        json.dumps(chart), json.dumps({'range': 'all',
                                       'job': 'a&group=result'})))
    assert query['params']['group'] == 'none'
    assert query['params']['job'] == 'a&group=result'


def test_what_the_page_says_about_each_kind_of_failure():
    def say(status, message):
        return run_js('m.describeError(%d, %s)' % (
            status, json.dumps({'message': message})))
    assert '[metrics] path' in say(404, 'metrics not enabled')
    assert 'token' in say(401, '401 Unauthorized').lower()
    assert 'group' in say(400, 'group must be one of: job')
    assert 'not answering' in run_js('m.describeError(0, null)')
    assert 'not answering' in run_js('m.describeError(502, null)')


def test_empty_data_is_told_from_data():
    assert run_js('m.isEmpty([])') is True
    assert run_js('m.isEmpty(null)') is True
    assert run_js('m.isEmpty([{count: 0}])') is True
    assert run_js('m.isEmpty([{count: 2}])') is False


def test_without_the_library_the_page_falls_back_to_tables():
    assert run_js('m.libraryAvailable({})') is False
    assert run_js('m.libraryAvailable({Chart: function () {}})') is True
    table = run_js("m.tableFallback({id: 'x', title: 'T', kind: 'runs'}, "
                   "[{group: '<script>alert(1)</script>', bucket: null, "
                   "count: 3, pass: 1, fail: 2}])")
    assert '<table' in table and '3' in table
    assert '<script>' not in table and '&lt;script&gt;' in table
    assert run_js("m.escapeHtml('<a href=\"x\">&')") == (
        '&lt;a href=&quot;x&quot;&gt;&amp;')


# -- the drawing code, against a stand-in for the browser ---------------------

SMOKE = r'''
const fs = require('fs');
const html = fs.readFileSync(process.argv[1], 'utf8');
const m = require(process.argv[2]);
const mode = process.argv[3];  // ok | off | nolib
const registry = {};
function element(id) {
  const el = {id, value: '', textContent: '', innerHTML: '',
    hidden: /^(lib-missing|notice|empty-|custom-range)/.test(id),
    listeners: {}, parentNode: {hidden: false},
    addEventListener(name, fn) { this.listeners[name] = fn; },
    querySelector() { return this.valueEl; },
    insertAdjacentHTML(where, text) {
      for (const hit of text.matchAll(/id="(total-[^"]+)"/g)) {
        const child = element(hit[1]);
        child.valueEl = {textContent: ''};
      }
    }};
  registry[id] = el;
  return el;
}
for (const hit of html.matchAll(/ id="([^"]+)"/g)) element(hit[1]);
const charts = [];
class Chart {
  constructor(canvas, config) {
    this.canvas = canvas; this.config = config; this.data = config.data;
    charts.push(this);
  }
  update() {} destroy() {}
}
const rows = {
  runs: [{group: 'bundle-api', bucket: 1700006400, count: 3, pass: 2,
          fail: 1, other: 0, avg_ms: 20000, max_ms: 30000, timed: 3}],
  escalations: [{group: 'bundle-security', bucket: 1700006400, count: 2}],
  deliveries: [{group: 'ok', bucket: 1700006400, count: 4, ok: 3, failed: 1,
                attempts: 5}],
  messages: [{group: '<script>x</script>', bucket: 1700006400, count: 7},
             {group: 'rejected', bucket: 1700006400, count: 2}],
};
const win = {
  document: {getElementById: id => registry[id] || null},
  location: {search: '?project=bundles&range=7d', pathname: '/metrics'},
  history: {replaceState() {}},
  getComputedStyle: () => ({getPropertyValue: () => ''}),
  setInterval: () => 1, clearInterval() {},
  fetch: url => {
    const kind = url.split('/api/metrics/')[1].split('?')[0];
    if (mode === 'off' && kind !== 'messages') {
      return Promise.resolve({status: 404, json: () => Promise.resolve(
        {message: 'metrics not enabled'})});
    }
    return Promise.resolve({status: 200, json: () => Promise.resolve(
      {kind, rows: rows[kind], truncated: false})});
  },
};
if (mode !== 'nolib') win.Chart = Chart;
m.start(win);
setTimeout(() => {
  const out = {
    charts: charts.length,
    types: charts.map(c => c.config.type),
    totals: Object.keys(registry).filter(k => k.startsWith('total-')).map(
      k => String(registry[k].valueEl.textContent)),
    libMissing: !registry['lib-missing'].hidden,
    notices: Object.fromEntries(['jobs', 'bundles', 'webhooks', 'messages']
      .map(s => [s, registry['notice-' + s].textContent])),
    tables: Object.keys(registry).filter(k => k.startsWith('table-')).map(
      k => registry[k].innerHTML).join(''),
    select: registry['f-project'].value, range: registry['f-range'].value,
  };
  process.stdout.write(JSON.stringify(out));
}, 50);
'''


def smoke(mode):
    need_node()
    done = subprocess.run(
        ['node', '-e', SMOKE, os.path.join(UI, 'metrics.html'),
         os.path.join(UI, 'metrics.js'), mode],
        capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_page_draws_every_chart_and_total_from_what_git_stream_says():
    out = smoke('ok')
    assert out['charts'] == len(charts()['CHARTS'])
    assert set(out['types']) <= {'bar', 'line', 'doughnut'}
    assert out['totals'] == ['3', '66.7%', '2', '1', '2']
    assert not out['libMissing'] and out['tables'] == ''
    assert all(text == '' for text in out['notices'].values())
    assert (out['select'], out['range']) == ('bundles', '7d')  # from the URL


def test_with_metrics_off_the_page_says_so_but_still_draws_the_messages():
    out = smoke('off')
    for section in ('jobs', 'bundles', 'webhooks'):
        assert '[metrics] path' in out['notices'][section], section
    assert out['notices']['messages'] == ''
    assert out["charts"] == 4  # only the four message charts have data


def test_without_the_library_it_shows_tables_and_escapes_what_it_shows():
    out = smoke('nolib')
    assert out['libMissing'] and out['charts'] == 0
    assert '<table' in out['tables']
    assert '<script>' not in out['tables']
    assert '&lt;script&gt;x&lt;/script&gt;' in out['tables']
