import { h, api, fmtMs, fmtPct } from '../dom.js';
import { card } from '../components.js';
import { lineChart } from '../charts.js';

const WINDOWS = [['15 min', 900], ['30 min', 1800], ['1 hour', 3600]];

export async function render(root, ctx) {
  let windowS = 900;
  const grid = h('div', { class: 'grid cols2' });
  const note = h('p', { class: 'muted' });
  const picker = h('div', { class: 'segmented', role: 'group', 'aria-label': 'Time window' },
    WINDOWS.map(([label, sec]) => h('button', { type: 'button', 'aria-pressed': String(sec === windowS), text: label, 'data-sec': sec,
      onclick: (e) => { windowS = sec; picker.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b === e.currentTarget))); tick(); } })));
  const prom = ctx.meta.links.prometheus_metrics;
  root.append(
    h('div', { class: 'toolbar' }, picker,
      prom ? h('a', { href: prom, text: 'Prometheus scrape endpoint (/metrics)' }) : h('span', { class: 'muted', text: 'Prometheus endpoint is not exposed in the public demo.' })),
    grid, note);

  const draw = (d) => {
    const pts = d.points;
    const series = (key, name, cls) => ({ name, cls, points: pts.map((p) => ({ t: p.t, v: p[key] })) });
    grid.replaceChildren(
      card('Request throughput', 'live', lineChart({ series: [series('rps', 'requests per second', 's1')],
        format: (v) => `${v.toFixed(2)} /s`, caption: 'Requests per second, trailing window' })),
      card('Latency (p50 / p95)', 'live', lineChart({ series: [series('p95_ms', 'p95', 's2'), series('p50_ms', 'p50', 's1')],
        format: (v) => fmtMs(v), thresholds: [{ value: d.thresholds.latency_p95_ms, label: 'p95 threshold' }] })),
      card('Failure rates', 'live', lineChart({ series: [series('error_rate', 'HTTP 5xx rate', 's2'), series('incorrect_rate', 'incorrect outcomes', 's1')],
        format: (v) => fmtPct(v), caption: 'Share of requests / validated outcomes in the trailing window' })),
      card('Open incidents', 'live', lineChart({ series: [series('active_incidents', 'open incidents', 's3')],
        format: (v) => String(Math.round(v * 10) / 10), caption: 'Incident types currently open' })));
    note.textContent = `${d.note} Showing ${pts.length} samples; gaps mean no requests in that interval (not zero).`;
  };
  const tick = ctx.poll(async () => draw(await api(`/api/metrics/history?window=${windowS}`)), 5000);
}
