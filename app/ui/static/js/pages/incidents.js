import { h, api, fmtDateTime, fmtDuration, ago, badge, status, severityLevel, mono } from '../dom.js';
import { card, kv, table } from '../components.js';

export async function render(root, ctx) {
  const st = { origin: 'all', resolution: 'all', selected: null, data: null };
  const summary = h('div', { class: 'grid tiles' });
  const listBox = h('div', {});
  const detailBox = h('div', { class: 'section' });

  const seg = (key, options) => {
    const box = h('div', { class: 'segmented', role: 'group', 'aria-label': key });
    for (const [value, label] of options) {
      box.appendChild(h('button', { type: 'button', 'aria-pressed': String(st[key] === value), text: label,
        onclick: () => { st[key] = value; box.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.textContent === label))); draw(); } }));
    }
    return box;
  };
  root.append(
    h('p', { class: 'muted', text: 'Detected symptoms from deterministic rules (latency, error rate, readiness, incorrect outcomes). A trace is an example affected request, not proof of root cause. History is in memory and resets when the process restarts.' }),
    summary,
    h('div', { class: 'toolbar section' }, seg('origin', [['all', 'All origins'], ['live', 'Live traffic'], ['demo', 'Demo runs']]),
      seg('resolution', [['all', 'Any status'], ['open', 'Open'], ['resolved', 'Resolved']])),
    listBox, detailBox);

  function draw() {
    const d = st.data;
    if (!d) return;
    const items = d.incidents.filter((i) => (st.origin === 'all' || i.origin === st.origin) && (st.resolution === 'all' || i.resolution === st.resolution));
    summary.replaceChildren(
      ...[['Open', d.counts.open], ['Total recorded', d.counts.total], ...Object.entries(d.counts.by_type).map(([k, v]) => [k.replace('_', ' '), v])]
        .map(([k, v]) => h('section', { class: 'card' }, h('div', { class: 'tile-label', text: k }), h('div', { class: 'tile-value', text: String(v) }))));
    const rows = items.map((i) => h('tr', { class: `click ${i.id === st.selected ? 'selected' : ''}`, tabindex: '0', role: 'button', 'aria-label': `Open incident ${i.type}`,
      onclick: () => { st.selected = i.id; draw(); }, onkeydown: (e) => { if (e.key === 'Enter') { st.selected = i.id; draw(); } } },
      h('td', { title: fmtDateTime(i.detected_at) }, ago(i.detected_at)),
      h('td', { text: i.type.replace('_', ' ') }),
      h('td', {}, status(severityLevel(i.severity), i.severity)),
      h('td', {}, h('span', { class: 'mono', text: i.affected_component.replace('recommend.', '') })),
      h('td', {}, i.resolution === 'open' ? status('warning', 'open') : status('good', 'condition cleared')),
      h('td', {}, i.origin === 'demo' ? badge('demo') : badge('live')),
      h('td', {}, i.trace_link ? h('a', { href: i.trace_link, text: 'trace' }) : '—')));
    listBox.replaceChildren(card(`Incidents (${items.length})`, 'live', items.length
      ? table(['Detected', 'Type', 'Severity', 'Affected component', 'Status', 'Origin', 'Trace'], rows)
      : h('div', { class: 'empty', text: 'No incidents match. Run scenario B or C on the Scenarios page to see the detector work.' })));
    const sel = d.incidents.find((i) => i.id === st.selected);
    detailBox.replaceChildren(sel ? detail(sel) : h('p', { class: 'muted', text: 'Select an incident for its evidence, investigation steps and timeline.' }));
  }

  function detail(i) {
    return card(`${i.type.replace('_', ' ')} incident`, i.origin === 'demo' ? 'sandbox' : 'live',
      h('div', {}, status(severityLevel(i.severity), i.severity), ' · ', i.resolution === 'open' ? 'open' : `condition cleared after ${fmtDuration(i.duration_s)}`),
      h('p', { text: i.summary }),
      kv([['Detected', fmtDateTime(i.detected_at)],
        ['Where it was observed', `${i.affected_component} (${i.affected_component_basis === 'trace' ? 'from the trace' : 'from the rule'})`],
        ['Evidence', Object.entries(i.evidence).map(([k, v]) => `${k}=${v}`).join(', ')],
        ['Trace', i.trace_link ? h('a', { href: i.trace_link, text: i.trace_id }) : 'none available'],
        ['Root cause confirmed', i.root_cause_confirmed ? 'yes' : 'no: detected symptom only']]),
      h('h3', { class: 'section', text: 'Suggested investigation' }),
      h('ol', { class: 'steps' }, i.investigation_steps.map((x) => h('li', { text: x }))),
      h('h3', { class: 'section', text: 'Recommended remediation' }), h('p', { text: i.recommended_remediation }),
      h('h3', { class: 'section', text: 'Timeline' }),
      h('ul', { class: 'timeline' }, i.timeline.map((e) => h('li', {}, h('div', { class: 't', text: fmtDateTime(e.t) }), h('div', {}, h('b', { text: e.event }), ` · ${e.detail}`)))),
      h('div', { class: 'callout', text: i.caveat }), mono(`id ${i.id}`));
  }

  ctx.poll(async () => { st.data = await api('/api/incidents'); draw(); }, 5000);
}
