import { h, api, fmtMs, fmtPct, fmtRps, fmtDuration, isNum, status, severityLevel, badge, noData } from '../dom.js';
import { tile, card } from '../components.js';
import { lineChart } from '../charts.js';

export async function render(root, ctx) {
  const tiles = h('div', { class: 'grid tiles' });
  const charts = h('div', { class: 'grid cols2 section' });
  const notes = h('div', { class: 'section' });
  root.append(
    h('p', { class: 'muted', text: 'Live view of this running instance. Values come from its own metrics, spans and incident detector; ' +
      'a value with no traffic behind it says "No data" instead of showing 0.' }),
    tiles, charts, notes);

  ctx.poll(async () => {
    const [o, hist] = await Promise.all([api('/api/overview'), api('/api/metrics/history?window=900')]);
    const w = o.window.seconds ? `last ${fmtDuration(o.window.seconds)}` : 'no window yet';
    const lat = o.latency;
    const slow = isNum(lat.p95_ms) && lat.p95_ms > lat.threshold_p95_ms;
    const err = o.http_error_rate, bad = o.incorrect_outcome_rate;

    tiles.replaceChildren(
      tile({ label: 'Service availability', source: 'live',
        value: o.service.ready && o.service.healthy ? 'Ready' : o.service.healthy ? 'Not ready' : 'Unhealthy',
        level: { kind: o.service.ready && o.service.healthy ? 'good' : 'critical',
          text: o.service.ready && o.service.healthy ? 'Healthy and accepting traffic' : 'Not serving traffic' },
        sub: `Up ${fmtDuration(o.service.uptime_s)} · ${o.service.scope}` }),
      tile({ label: 'Request throughput', source: 'live', value: fmtRps(o.throughput.rps), unit: 'req/s',
        sub: isNum(o.throughput.requests) ? `${o.throughput.requests} requests, ${w}` : 'No requests in the window',
        why: 'No requests in the window' }),
      tile({ label: 'Latency p95', source: 'live', value: fmtMs(lat.p95_ms), sub: `p50 ${fmtMs(lat.p50_ms) || 'no data'} · threshold ${fmtMs(lat.threshold_p95_ms)}`,
        level: isNum(lat.p95_ms) ? { kind: slow ? 'warning' : 'good', text: slow ? 'Above threshold' : 'Within threshold' } : null,
        why: 'No requests in the window' }),
      tile({ label: 'HTTP error rate (5xx)', source: 'live', value: fmtPct(err.value), sub: `threshold ${fmtPct(err.threshold)}`,
        level: isNum(err.value) ? { kind: err.value > err.threshold ? 'critical' : 'good', text: err.value > err.threshold ? 'Above threshold' : 'Within threshold' } : null,
        why: 'No requests in the window' }),
      tile({ label: 'Incorrect outcome rate', source: 'live', value: fmtPct(bad.value),
        sub: 'Business-result mismatches, even when HTTP is 200',
        level: isNum(bad.value) ? { kind: bad.value > bad.threshold ? 'critical' : 'good', text: bad.value > bad.threshold ? 'Mismatches detected' : 'All outcomes correct' } : null,
        why: 'No validated outcomes in the window' }),
      tile({ label: 'Incidents', source: 'live', value: String(o.incidents.active), unit: 'active',
        sub: `${o.incidents.opened_by_detector} opened by live traffic · ${o.incidents.opened_by_demo_runs} from demo runs`,
        level: { kind: o.incidents.active ? 'warning' : 'good', text: o.incidents.active ? 'Needs attention' : 'None open' } }),
      alertsCard(o),
      recoveryCard(o));

    const pts = hist.points;
    charts.replaceChildren(
      card('Latency (p50 / p95)', 'live', lineChart({
        series: [{ name: 'p95', cls: 's2', points: pts.map((p) => ({ t: p.t, v: p.p95_ms })) },
                 { name: 'p50', cls: 's1', points: pts.map((p) => ({ t: p.t, v: p.p50_ms })) }],
        format: (v) => fmtMs(v), thresholds: [{ value: lat.threshold_p95_ms, label: 'p95 threshold' }],
        emptyText: 'No requests yet: run a scenario or send traffic' })),
      card('Request throughput', 'live', lineChart({
        series: [{ name: 'requests per second', cls: 's1', points: pts.map((p) => ({ t: p.t, v: p.rps })) }],
        format: (v) => `${v.toFixed(2)} /s`, emptyText: 'No requests yet: run a scenario or send traffic' })));

    notes.replaceChildren(...(o.notes.length ? [h('div', { class: 'callout', text: o.notes.join(' ') })] : []),
      h('p', { class: 'muted', text: `Rates use a ${hist.rate_window_s}s trailing window; history is in memory (${hist.stats.points} of ${hist.stats.max_points} points) and starts at the last process start.` }));
  }, 5000);
}

function alertsCard(o) {
  const list = o.alerts.active;
  return h('section', { class: 'card', 'aria-label': 'Active alerts' },
    h('div', { class: 'card-head' }, h('div', { class: 'tile-label', text: 'Active alerts' }), badge('live')),
    list.length
      ? h('ul', { class: 'steps' }, list.map((a) => h('li', {}, status(severityLevel(a.severity), a.type.replace('_', ' ')), a.summary)))
      : h('div', {}, h('div', { class: 'tile-value' }, '0'), status('good', 'No alert firing')),
    h('div', { class: 'tile-sub', text: o.alerts.kind }));
}

function recoveryCard(o) {
  const refs = Object.entries(o.recovery.recorded_reference || {});
  return h('section', { class: 'card', 'aria-label': 'Recovery status' },
    h('div', { class: 'card-head' }, h('div', { class: 'tile-label', text: 'Recovery status' }), badge('live')),
    h('div', { class: 'tile-value' }, o.recovery.status === 'recently restarted' ? 'Recently restarted' : 'Stable'),
    h('div', { class: 'tile-sub', text: `This process has run for ${fmtDuration(o.recovery.uptime_s)}.` }),
    refs.length ? h('div', { class: 'section' }, h('div', { class: 'tile-label', text: 'Recorded recoveries' }),
      ...refs.map(([k, v]) => h('div', { class: 'tile-sub' }, badge('recorded'), ` Scenario ${k}: ${v.headline || 'see Scenarios'}`))) : null);
}
