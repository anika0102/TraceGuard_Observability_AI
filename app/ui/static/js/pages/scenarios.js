import { h, api, fmtMs, fmtPct, fmtDateTime, badge, status, severityLevel, mono } from '../dom.js';
import { card, kv, table, tile } from '../components.js';

export async function render(root, ctx) {
  const data = await api('/api/scenarios');
  const results = h('div', { class: 'section', 'aria-live': 'polite' });
  const buttons = [];

  root.append(
    h('p', { class: 'muted', text: data.safety }),
    h('div', { class: 'grid cols2' }, data.scenarios.map(scenarioCard)),
    results);

  function scenarioCard(sc) {
    const live = sc.runnable;
    const btn = h('button', { type: 'button', class: live ? 'primary' : '', text: live ? 'Run live sandbox' : 'Show recording', disabled: live ? null : (sc.available ? null : true),
      onclick: () => (live ? run(sc, btn) : showRecording(sc)) });
    buttons.push(btn);
    return h('section', { class: 'card', 'aria-label': `Scenario ${sc.id}` },
      h('div', { class: 'card-head' }, h('h2', { text: `${sc.id}. ${sc.title}` }), badge(live ? 'live_sandbox' : 'recorded')),
      h('p', { text: sc.description }),
      h('div', { class: 'tile-label', text: 'Expected behaviour' }), h('p', { text: sc.expected }),
      live ? h('p', { class: 'muted', text: `Runs up to ${data.limits.max_requests} real requests; faults are private to this run. Limit ${data.limits.runs_per_minute} runs/min.` })
        : h('p', { class: 'muted', text: sc.available ? `Cannot run from the browser by design. Recorded ${fmtDateTime(Date.parse(sc.recording.recorded_at) / 1000)} at commit ${sc.recording.git_commit}. ${sc.recording.headline || ''}` : 'Recording not available in this build.' }),
      btn);
  }

  async function run(sc, btn) {
    buttons.forEach((b) => { b.disabled = true; });
    const original = btn.textContent;
    btn.textContent = 'Running…';
    results.replaceChildren(h('p', { class: 'muted', text: `Running scenario ${sc.id}…` }));
    try {
      const r = await api(`/api/scenarios/${sc.id}/run`, { method: 'POST', body: JSON.stringify({}) });
      results.replaceChildren(resultView(r));
    } catch (e) {
      results.replaceChildren(h('div', { class: 'banner error', text: e.status === 429 ? `Rate limit: please wait ${e.retryAfter || 'a few'} s and try again.` : `Could not run the scenario: ${e.message}` }));
    } finally {
      buttons.forEach((b, i) => { b.disabled = data.scenarios[i].kind === 'recorded' && !data.scenarios[i].available; });
      btn.textContent = original;
      results.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  }

  function resultView(r) {
    const m = r.metrics, det = r.detection;
    return h('div', { class: 'grid' },
      card(`Result: scenario ${r.scenario}, ${r.title}`, 'live_sandbox',
        h('p', { class: 'muted' }, 'Run ', mono(r.run_id), ` · ${m.requests} real requests · took ${Math.round(r.wall_ms)} ms`),
        h('div', { class: 'grid cols2' },
          h('div', {}, h('div', { class: 'tile-label', text: 'Expected' }), h('p', { text: r.expected })),
          h('div', {}, h('div', { class: 'tile-label', text: 'Observed (this run)' }), h('p', { text: r.observed }))),
        h('div', {}, det.matched ? status('good', `Detection matched expectation: ${det.expected.length ? det.expected.join(', ') + ' incident opened' : 'no incident, as expected'}`)
          : status('warning', `Detection differed. Expected: ${det.expected.join(', ') || 'none'}; detected: ${det.detected.join(', ') || 'none'}`))),
      h('div', { class: 'grid tiles' },
        tile({ label: 'p50 latency', value: fmtMs(m.p50_ms), source: 'live_sandbox' }),
        tile({ label: 'p95 latency', value: fmtMs(m.p95_ms), source: 'live_sandbox' }),
        tile({ label: 'HTTP error rate', value: fmtPct(m.error_rate), source: 'live_sandbox' }),
        tile({ label: 'Incorrect outcomes', value: fmtPct(m.incorrect_rate), source: 'live_sandbox', why: 'No validated outcomes' })),
      card('Requests', 'live_sandbox', table(['#', 'Input', 'HTTP', { label: 'Latency', num: true }, 'Result', 'Trace'],
        r.requests.map((q) => h('tr', {}, h('td', { text: String(q.index + 1) }), h('td', { text: `${q.input.category} ≤ ${q.input.budget}` }),
          h('td', { text: String(q.status) }), h('td', { class: 'num', text: fmtMs(q.latency_ms) }),
          h('td', {}, q.correct === null ? '—' : q.correct ? status('good', 'correct') : status('warning', 'wrong result')),
          h('td', {}, q.trace_id ? h('a', { href: `/ui/traces?id=${q.trace_id}`, text: `${q.trace_id.slice(0, 10)}…` }) : '—'))))),
      det.incidents.length ? card('Incidents opened by this run', 'demo', h('ul', { class: 'steps' }, det.incidents.map((i) =>
        h('li', {}, status(severityLevel(i.severity), `${i.type}: `), i.summary, ' ', i.trace_id ? h('a', { href: `/ui/traces?id=${i.trace_id}`, text: 'example trace' }) : null)))) : null,
      h('div', { class: 'callout', text: 'Isolation: every simulated request has its own private fault state and its own trace, and this run used its own incident detector. No other visitor and no app-wide state was affected.' }));
  }

  async function showRecording(sc) {
    results.replaceChildren(h('p', { class: 'muted', text: 'Loading recording…' }));
    try { results.replaceChildren(recordingView(await api(`/api/scenarios/${sc.id}/recording`))); }
    catch (e) { results.replaceChildren(h('div', { class: 'banner error', text: e.message })); }
    results.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  function recordingView(rec) {
    return h('div', { class: 'grid' },
      card(`Recording: scenario ${rec.id}, ${rec.title}`, 'recorded',
        h('div', { class: 'callout' }, status('warning', 'This is a replay, not live. '),
          `Captured ${fmtDateTime(Date.parse(rec.recorded_at) / 1000)} from a verified local run at commit ${rec.git_commit}. Nothing on this page is executing now.`),
        h('p', { text: rec.summary.headline }),
        kv(Object.entries(rec.environment).map(([k, v]) => [k, String(v)]))),
      card('What happened (timeline)', 'recorded', h('ul', { class: 'timeline' }, rec.timeline.map((e) =>
        h('li', {}, h('div', { class: 't', text: `+${e.t_s.toFixed(1)} s · ${e.category}` }), h('div', {}, h('b', { text: e.event }), e.detail ? ` · ${e.detail}` : ''))))),
      card('Measurements', 'recorded', table(['Measurement', { label: 'Value', num: true }, 'Note'],
        rec.measurements.map((x) => h('tr', {}, h('td', { text: x.name }), h('td', { class: 'num', text: `${x.value} ${x.unit}` }), h('td', { text: x.note || '' }))))),
      (rec.notes || []).length ? card('Limits of this recording', 'recorded', h('ul', { class: 'steps' }, rec.notes.map((n) => h('li', { text: n })))) : null);
  }
}
