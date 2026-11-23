/* The metrics page: its logic, then its drawing.
 *
 * The logic (filters in the URL, the query each chart sends, what an error
 * says, the table shown when the chart library is missing) is plain
 * functions with no browser in them, exported for node so tests/
 * test_metrics_view.py can run them.  The drawing at the bottom runs only
 * in a page.  Every value that came from git-stream is escaped before it is
 * put in HTML.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.MetricsPage = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const RANGES = ['1h', '6h', '24h', '7d', '30d', 'all', 'custom'];
  const RANGE_SECONDS = {'1h': 3600, '6h': 6 * 3600, '24h': 86400,
                         '7d': 7 * 86400, '30d': 30 * 86400};
  const DEFAULTS = {range: '24h'};
  // Filter name in the page's URL -> what it is called to git-stream, per
  // kind.  A filter a kind does not have is not sent to it.
  const FILTER_MAP = {
    runs: {project: 'project', job: 'job', result: 'result',
           pipeline: 'pipeline', bundle: 'mr'},
    escalations: {project: 'project', job: 'job', bundle: 'mr'},
    deliveries: {project: 'project', kind: 'kind', daction: 'action',
                 ok: 'ok'},
    messages: {project: 'project', maction: 'action', sender: 'sender',
               outcome: 'outcome'},
  };
  const FILTER_NAMES = ['project', 'job', 'result', 'pipeline', 'bundle',
                        'range', 'since', 'until', 'kind', 'daction', 'ok',
                        'maction', 'sender', 'outcome'];
  const MAX_BUCKETS = 500;

  // type: bar | stackedBar | line | doughnut.  A timeseries chart is
  // bucketed by time; the others count by their group.
  const CHARTS = [
    {id: 'jobs-results', title: 'Results per job', section: 'jobs',
     kind: 'runs', group: 'job', type: 'stackedBar', timeseries: false,
     series: ['pass', 'fail', 'other']},
    {id: 'jobs-failrate', title: 'Failure rate per job (%)', section: 'jobs',
     kind: 'runs', group: 'job', type: 'bar', timeseries: false,
     value: 'failrate'},
    {id: 'runs-over-time', title: 'Runs over time, by result',
     section: 'jobs', kind: 'runs', group: 'result', type: 'line',
     timeseries: true},
    {id: 'jobs-duration', title: 'Duration per job (seconds)',
     section: 'jobs', kind: 'runs', group: 'job', type: 'bar',
     timeseries: false, series: ['avg_ms', 'max_ms'], scale: 0.001},
    {id: 'pipelines-results', title: 'Results per pipeline', section: 'jobs',
     kind: 'runs', group: 'pipeline', type: 'stackedBar', timeseries: false,
     series: ['pass', 'fail', 'other']},
    {id: 'bundle-attempts', title: 'Runs per bundle (attempts)',
     section: 'bundles', kind: 'runs', group: 'mr', type: 'bar',
     timeseries: false, top: 20},
    {id: 'escalations-over-time', title: 'Escalations over time',
     section: 'bundles', kind: 'escalations', group: 'none', type: 'bar',
     timeseries: true},
    {id: 'escalations-jobs', title: 'Jobs that were escalated',
     section: 'bundles', kind: 'escalations', group: 'job', type: 'bar',
     timeseries: false},
    {id: 'webhooks-over-time', title: 'Webhook deliveries over time',
     section: 'webhooks', kind: 'deliveries', group: 'ok',
     type: 'stackedBar', timeseries: true},
    {id: 'webhooks-ok', title: 'Webhooks delivered or not',
     section: 'webhooks', kind: 'deliveries', group: 'ok', type: 'doughnut',
     timeseries: false},
    {id: 'webhooks-kinds', title: 'Webhooks by kind', section: 'webhooks',
     kind: 'deliveries', group: 'kind', type: 'stackedBar',
     timeseries: false, series: ['ok', 'failed']},
    {id: 'messages-over-time', title: 'Messages over time, by outcome',
     section: 'messages', kind: 'messages', group: 'outcome',
     type: 'stackedBar', timeseries: true},
    {id: 'messages-actions', title: 'Messages by action',
     section: 'messages', kind: 'messages', group: 'action', type: 'bar',
     timeseries: false},
    {id: 'messages-senders', title: 'Messages by sender',
     section: 'messages', kind: 'messages', group: 'sender', type: 'bar',
     timeseries: false},
    {id: 'messages-outcomes', title: 'Messages by outcome',
     section: 'messages', kind: 'messages', group: 'outcome',
     type: 'doughnut', timeseries: false},
  ];

  // The numbers at the top: one query each, one value out of its rows.
  const TOTALS = [
    {id: 'runs', title: 'Runs', kind: 'runs', group: 'none', field: 'count'},
    {id: 'pass-rate', title: 'Pass rate', kind: 'runs', group: 'none',
     field: 'passrate'},
    {id: 'escalations', title: 'Escalations', kind: 'escalations',
     group: 'none', field: 'count'},
    {id: 'webhooks-failed', title: 'Webhooks failed', kind: 'deliveries',
     group: 'none', field: 'failed'},
    {id: 'messages-rejected', title: 'Messages rejected', kind: 'messages',
     group: 'outcome', field: 'count', pick: 'rejected'},
  ];

  function escapeHtml(text) {
    return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // -- the filters, in the page's URL ---------------------------------------

  function parseFilters(search) {
    const found = new URLSearchParams(search || '');
    const filters = Object.assign({}, DEFAULTS);
    for (const name of FILTER_NAMES) {
      const value = found.get(name);
      if (value === null || value === '') continue;
      if (name === 'range' && RANGES.indexOf(value) < 0) continue;
      filters[name] = value;
    }
    return filters;
  }

  function toSearch(filters) {
    const parts = [];
    for (const name of FILTER_NAMES) {
      const value = filters[name];
      if (value === undefined || value === null || value === '') continue;
      if (DEFAULTS[name] === value) continue;
      parts.push(name + '=' + encodeURIComponent(value));
    }
    return parts.length ? '?' + parts.join('&') : '';
  }

  // -- the query a chart sends ----------------------------------------------

  function spanSeconds(since, until, now) {
    const start = since ? Date.parse(since) / 1000 : NaN;
    const end = until ? Date.parse(until) / 1000 : now;
    return isNaN(start) || isNaN(end) ? null : end - start;
  }

  // What to ask git-stream for one chart (or total) under the filters:
  // {kind, params} or {error}.  now is epoch seconds.
  function apiQuery(chart, filters, now) {
    const f = Object.assign({}, DEFAULTS, filters);
    const params = {group: chart.group};
    const map = FILTER_MAP[chart.kind];
    for (const name of Object.keys(map)) {
      if (f[name] !== undefined && f[name] !== '') params[map[name]] = f[name];
    }
    let bucket = 'day';
    if (RANGE_SECONDS[f.range]) {
      params.since = now - RANGE_SECONDS[f.range];
      bucket = RANGE_SECONDS[f.range] <= 7 * 86400 ? 'hour' : 'day';
    } else if (f.range === 'custom') {
      if (f.since) params.since = f.since;
      if (f.until) params.until = f.until;
      const span = f.since ? spanSeconds(f.since, f.until, now) : null;
      if (span !== null) {
        if (span / 86400 > MAX_BUCKETS) {
          return {error: 'The range is longer than ' + MAX_BUCKETS +
                  ' days; choose a shorter one (a chart has at most ' +
                  MAX_BUCKETS + ' buckets).'};
        }
        bucket = span <= 20 * 86400 ? 'hour' : 'day';
      }
    }
    if (chart.timeseries) params.bucket = bucket;
    return {kind: chart.kind, params: params};
  }

  // -- what the page says ---------------------------------------------------

  function describeError(status, body) {
    const message = body && body.message ? String(body.message) : '';
    if (!status || status >= 502 || (status >= 500 && !body)) {
      return 'git-stream is not answering. Is the demo running?';
    }
    if (status === 404 && /metrics not enabled/.test(message)) {
      return 'Metrics are turned off in git-stream. Set [metrics] path in ' +
        'its config (the demo has it on) to record builds, escalations ' +
        'and deliveries.';
    }
    if (status === 401) {
      return 'git-stream wants a stream token for this section. Start the ' +
        'demo UI with STREAM_TOKEN set to one.';
    }
    if (status === 400) return 'git-stream refused the query: ' + message;
    return 'git-stream answered ' + status + (message ? ': ' + message : '');
  }

  function isEmpty(rows) {
    return !rows || rows.length === 0 || rows.every(function (r) {
      return !r.count;
    });
  }

  function libraryAvailable(scope) {
    return !!scope && typeof scope.Chart === 'function';
  }

  function formatBucket(seconds) {
    const d = new Date(seconds * 1000);
    const pad = function (n) { return String(n).padStart(2, '0'); };
    return d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' +
      pad(d.getUTCDate()) + ' ' + pad(d.getUTCHours()) + ':00';
  }

  // The same numbers as a table, for when the chart library did not load.
  function tableFallback(chart, rows) {
    const extra = [];
    for (const r of rows || []) {
      for (const k of Object.keys(r)) {
        if (['group', 'bucket', 'count'].indexOf(k) < 0 &&
            extra.indexOf(k) < 0) extra.push(k);
      }
    }
    const head = ['group', 'bucket', 'count'].concat(extra).map(function (k) {
      return '<th>' + escapeHtml(k) + '</th>';
    }).join('');
    const body = (rows || []).map(function (r) {
      const cells = ['group', 'bucket', 'count'].concat(extra).map(
        function (k) {
          let v = r[k];
          if (k === 'bucket' && v !== null && v !== undefined) {
            v = formatBucket(v);
          }
          return '<td>' + (v === null || v === undefined ? '' :
                           escapeHtml(v)) + '</td>';
        });
      return '<tr>' + cells.join('') + '</tr>';
    }).join('');
    return '<table class="fallback"><caption>' + escapeHtml(chart.title) +
      '</caption><thead><tr>' + head + '</tr></thead><tbody>' + body +
      '</tbody></table>';
  }

  // -- drawing (browser only) -------------------------------------------------

  function start(win) {
    const doc = win.document;
    const $ = function (id) { return doc.getElementById(id); };
    const charts = {};
    let filters = parseFilters(win.location.search);
    let timer = null;
    const ready = libraryAvailable(win);
    if (!ready) $('lib-missing').hidden = false;

    const css = function (name, fallback) {
      return win.getComputedStyle(doc.documentElement)
        .getPropertyValue(name).trim() || fallback;
    };
    const palette = function () {
      return [css('--run', '#1c7ed6'), css('--warn', '#e8590c'),
              css('--accent', '#3b5bdb'), '#9c36b5', '#0c8599', '#f08c00',
              '#5c940d', '#c2255c'];
    };
    const colourFor = function (label, index) {
      if (/^(SUCCESS|ok|applied|pass)$/i.test(label)) return css('--ok', '#2f9e44');
      if (/^(FAILURE|failed|rejected|refused|error|fail|TIMED_OUT)$/i
          .test(label)) return css('--bad', '#e03131');
      if (/^(duplicate|other)$/i.test(label)) return css('--skip', '#868e96');
      return palette()[index % palette().length];
    };

    function fetchJson(kind, params) {
      const query = new URLSearchParams(params).toString();
      return win.fetch('/api/metrics/' + kind + '?' + query)
        .then(function (r) {
          return r.json().catch(function () { return null; }).then(
            function (body) { return {status: r.status, body: body}; });
        }, function () { return {status: 0, body: null}; });
    }

    function note(section, text) {
      const el = $(section ? 'notice-' + section : 'notice');
      if (!el) return;
      el.textContent = text || '';
      el.hidden = !text;
    }

    // Chart.js data for one chart from the rows git-stream answered.
    function datasets(chart, rows) {
      const scale = chart.scale || 1;
      if (chart.timeseries) {
        const buckets = Array.from(new Set(rows.map(function (r) {
          return r.bucket;
        }))).sort(function (a, b) { return a - b; });
        const groups = Array.from(new Set(rows.map(function (r) {
          return r.group;
        })));
        return {
          labels: buckets.map(formatBucket),
          datasets: groups.map(function (g, i) {
            const label = g === null ? 'count' : String(g);
            return {label: label, backgroundColor: colourFor(label, i),
                    borderColor: colourFor(label, i), tension: 0.25,
                    data: buckets.map(function (b) {
                      const hit = rows.find(function (r) {
                        return r.bucket === b && r.group === g;
                      });
                      return hit ? hit.count : 0;
                    })};
          }),
        };
      }
      const shown = chart.top ? rows.slice(0, chart.top) : rows;
      const labels = shown.map(function (r) {
        return r.group === null ? 'all' : String(r.group);
      });
      if (chart.type === 'doughnut') {
        return {labels: labels, datasets: [{
          data: shown.map(function (r) { return r.count; }),
          backgroundColor: labels.map(colourFor)}]};
      }
      if (chart.value === 'failrate') {
        return {labels: labels, datasets: [{
          label: 'failed %', backgroundColor: colourFor('failed', 0),
          data: shown.map(function (r) {
            return r.count ? Math.round(1000 * r.fail / r.count) / 10 : 0;
          })}]};
      }
      const series = chart.series || ['count'];
      return {labels: labels, datasets: series.map(function (s, i) {
        return {label: s.replace('_ms', ''), backgroundColor:
                colourFor(s.replace('_ms', ''), i),
                data: shown.map(function (r) {
                  const v = r[s] === null || r[s] === undefined ? 0 : r[s];
                  return Math.round(v * scale * 100) / 100;
                })};
      })};
    }

    function draw(chart, rows) {
      const canvas = $('chart-' + chart.id);
      const table = $('table-' + chart.id);
      const empty = $('empty-' + chart.id);
      const hasData = !isEmpty(rows);
      empty.hidden = hasData;
      canvas.parentNode.hidden = !hasData || !ready;
      table.innerHTML = (!ready && hasData) ? tableFallback(chart, rows) : '';
      if (!ready || !hasData) {
        if (charts[chart.id]) { charts[chart.id].destroy(); delete charts[chart.id]; }
        return;
      }
      const data = datasets(chart, rows);
      const stacked = chart.type === 'stackedBar';
      const type = chart.type === 'stackedBar' ? 'bar' : chart.type;
      const muted = css('--muted', '#677087');
      const line = css('--line', '#e2e5ec');
      if (charts[chart.id] && charts[chart.id].config.type === type) {
        charts[chart.id].data = data;
        charts[chart.id].update('none');
        return;
      }
      if (charts[chart.id]) charts[chart.id].destroy();
      charts[chart.id] = new win.Chart(canvas, {
        type: type, data: data,
        options: {
          responsive: true, maintainAspectRatio: false, animation: false,
          plugins: {legend: {display: data.datasets.length > 1 ||
                             type === 'doughnut',
                             labels: {color: muted}}},
          scales: type === 'doughnut' ? {} : {
            x: {stacked: stacked, ticks: {color: muted}, grid: {color: line}},
            y: {stacked: stacked, beginAtZero: true, ticks: {color: muted},
                grid: {color: line}}},
        },
      });
    }

    function total(def, rows) {
      if (def.pick) {
        const hit = rows.find(function (r) { return r.group === def.pick; });
        return hit ? hit.count : 0;
      }
      const r = rows[0];
      if (!r) return 0;
      if (def.field === 'passrate') {
        return r.count ? Math.round(1000 * r.pass / r.count) / 10 + '%' : '–';
      }
      return r[def.field] === null ? 0 : r[def.field];
    }

    function load() {
      const now = Math.floor(Date.now() / 1000);
      const failed = {};
      const jobs = TOTALS.map(function (def) {
        const q = apiQuery(def, filters, now);
        if (q.error) { note('', q.error); return Promise.resolve(); }
        return fetchJson(q.kind, q.params).then(function (r) {
          const el = $('total-' + def.id);
          if (r.status === 200) {
            el.querySelector('.value').textContent = total(def, r.body.rows);
          } else {
            el.querySelector('.value').textContent = '–';
            failed[def.kind] = describeError(r.status, r.body);
          }
        });
      }).concat(CHARTS.map(function (chart) {
        const q = apiQuery(chart, filters, now);
        if (q.error) { note('', q.error); return Promise.resolve(); }
        return fetchJson(q.kind, q.params).then(function (r) {
          if (r.status === 200) {
            draw(chart, r.body.rows);
          } else {
            failed[chart.section] = describeError(r.status, r.body);
            draw(chart, []);
          }
          if (r.status === 200 && r.body.truncated) {
            failed[chart.section] = 'Some groups were cut off: narrow the ' +
              'filters to see them all.';
          }
        });
      }));
      return Promise.all(jobs).then(function () {
        ['jobs', 'bundles', 'webhooks', 'messages'].forEach(function (s) {
          const own = failed[s] || (s === 'jobs' ? failed.runs : '') ||
            (s === 'bundles' ? failed.escalations : '') ||
            (s === 'webhooks' ? failed.deliveries : '');
          note(s, own || '');
        });
      });
    }

    // The filter controls: their options come from git-stream.
    const OPTION_SOURCES = [
      ['project', 'runs', 'project'], ['job', 'runs', 'job'],
      ['result', 'runs', 'result'], ['pipeline', 'runs', 'pipeline'],
      ['bundle', 'runs', 'mr'], ['kind', 'deliveries', 'kind'],
      ['daction', 'deliveries', 'action'], ['maction', 'messages', 'action'],
      ['sender', 'messages', 'sender'], ['outcome', 'messages', 'outcome']];

    function fillOptions() {
      OPTION_SOURCES.forEach(function (src) {
        fetchJson(src[1], {group: src[2]}).then(function (r) {
          const select = $('f-' + src[0]);
          if (r.status !== 200) return;
          const keep = filters[src[0]] || '';
          const values = r.body.rows.map(function (row) { return row.group; })
            .filter(function (v) { return v !== null; }).sort();
          if (keep && values.indexOf(keep) < 0) values.unshift(keep);
          select.innerHTML = '<option value="">any</option>' + values.map(
            function (v) {
              return '<option>' + escapeHtml(v) + '</option>';
            }).join('');
          select.value = keep;
        });
      });
    }

    function readControls() {
      const next = {};
      FILTER_NAMES.forEach(function (name) {
        const el = $('f-' + name);
        if (el && el.value !== '') next[name] = el.value;
      });
      if (!next.range) next.range = DEFAULTS.range;
      filters = next;
      $('custom-range').hidden = filters.range !== 'custom';
      win.history.replaceState(null, '', win.location.pathname +
                               toSearch(filters));
      note('', '');
      load();
    }

    FILTER_NAMES.forEach(function (name) {
      const el = $('f-' + name);
      if (!el) return;
      if (filters[name] !== undefined) el.value = filters[name];
      el.addEventListener('change', readControls);
    });
    $('custom-range').hidden = filters.range !== 'custom';
    $('f-reset').addEventListener('click', function () {
      win.location.search = '';
    });
    TOTALS.forEach(function (def) {
      $('totals').insertAdjacentHTML('beforeend',
        '<div class="total" id="total-' + escapeHtml(def.id) +
        '"><div class="value">…</div><div class="label">' +
        escapeHtml(def.title) + '</div></div>');
    });
    fillOptions();
    load();
    timer = win.setInterval(load, 10000);
    return function stop() { win.clearInterval(timer); };
  }

  if (typeof document !== 'undefined' && typeof window !== 'undefined') {
    document.addEventListener('DOMContentLoaded', function () {
      start(window);
    });
  }

  return {
    RANGES: RANGES, DEFAULTS: DEFAULTS, FILTER_NAMES: FILTER_NAMES,
    CHARTS: CHARTS, TOTALS: TOTALS, parseFilters: parseFilters,
    toSearch: toSearch, apiQuery: apiQuery, describeError: describeError,
    isEmpty: isEmpty, libraryAvailable: libraryAvailable,
    tableFallback: tableFallback, escapeHtml: escapeHtml,
    formatBucket: formatBucket, start: start,
  };
});
