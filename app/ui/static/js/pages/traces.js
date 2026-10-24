import { h, s, api, fmtMs, fmtClock, ago, badge, status, mono } from '../dom.js';
import { card, kv, table } from '../components.js';

export async function render(root, ctx) {
  const params = new URLSearchParams(location.search);
  const st = { origin: '', errorsOnly: false, selected: params.get('id') || null, showInternal: false, detail: null, selSpan: null };

  const input = h('input', { type: 'text', placeholder: 'Paste a 32-character trace ID', 'aria-label': 'Trace ID', value: st.selected || '', maxlength: '32', spellcheck: 'false' });
  const msg = h('span', { class: 'muted', role: 'status' });
  const originSel = h('select', { 'aria-label': 'Filter by origin', onchange: () => { st.origin = originSel.value; refresh(); } },
    h('option', { value: '', text: 'All traces' }), h('option', { value: 'request', text: 'Real requests' }), h('option', { value: 'sandbox', text: 'Demo runs' }));
  const errBox = h('input', { type: 'checkbox', onchange: () => { st.errorsOnly = errBox.checked; refresh(); } });
  const find = () => open(input.value.trim().toLowerCase());
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') find(); });

  const listBox = h('div', {});
  const detailBox = h('div', { class: 'section' });
  root.append(
    h('p', { class: 'muted', text: 'Real OpenTelemetry spans captured by this instance (the most recent are kept in a bounded in-memory store). Pick a trace to see where its time went.' }),
    h('div', { class: 'toolbar' }, input, h('button', { type: 'button', class: 'primary', onclick: find, text: 'Find trace' }), originSel,
      h('label', { class: 'check' }, errBox, 'Errors only'), msg),
    listBox, detailBox);

  async function open(id) {
    msg.textContent = '';
    if (!/^[0-9a-f]{32}$/.test(id)) { msg.textContent = 'A trace ID is exactly 32 hexadecimal characters.'; return; }
    try {
      st.detail = await api(`/api/traces/${id}`);
      st.selected = id; st.selSpan = null; input.value = id;
      history.replaceState(null, '', `?id=${id}`);
      drawDetail(); drawList.last && drawList(drawList.last);
      detailBox.scrollIntoView({ block: 'nearest' });
    } catch (e) { msg.textContent = e.status === 404 ? 'Not found: that trace was never recorded here or has been evicted (only recent traces are kept).' : e.message; }
  }

  function drawList(d) {
    drawList.last = d;
    const rows = d.traces.map((t) => h('tr', { class: `click ${t.trace_id === st.selected ? 'selected' : ''}`, tabindex: '0', role: 'button',
      'aria-label': `Open trace ${t.trace_id}`, onclick: () => open(t.trace_id), onkeydown: (e) => { if (e.key === 'Enter') open(t.trace_id); } },
      h('td', { title: fmtClock(t.started_at) }, ago(t.started_at)),
      h('td', {}, h('span', { class: 'mono', title: t.trace_id, text: `${t.trace_id.slice(0, 12)}…` })),
      h('td', {}, h('span', { class: 'mono', text: t.root_name })),
      h('td', {}, t.origin === 'sandbox' ? badge('demo') : badge('live')),
      h('td', { text: t.fault_scenario && t.fault_scenario !== 'none' ? t.fault_scenario : '—' }),
      h('td', { class: 'num', text: fmtMs(t.duration_ms) }),
      h('td', { class: 'num', text: String(t.span_count) }),
      h('td', {}, t.status === 'error' ? status('critical', 'error') : t.outcome_correct === false ? status('warning', 'wrong result') : status('good', 'ok')),
      h('td', {}, t.slowest_operation ? [t.slowest_operation.slow ? status('warning', '') : null, `${t.slowest_operation.name.replace('recommend.', '')} ${fmtMs(t.slowest_operation.duration_ms)}`] : '—')));
    listBox.replaceChildren(card(`Recent traces (${d.traces.length})`, 'live',
      d.traces.length ? table(['When', 'Trace ID', 'Request', 'Origin', 'Fault', { label: 'Duration', num: true }, { label: 'Spans', num: true }, 'Outcome', 'Slowest operation'], rows)
        : h('div', { class: 'empty', text: 'No traces yet. Run a scenario on the Scenarios page or send a request to /recommend.' }),
      h('p', { class: 'muted', text: `Store: ${d.store.traces}/${d.store.max_traces} traces. Operations slower than ${fmtMs(d.slow_operation_threshold_ms)} are flagged ▲.` })));
  }

  function drawDetail() {
    const d = st.detail;
    if (!d) { detailBox.replaceChildren(); return; }
    const sum = d.summary, total = Math.max(sum.duration_ms, ...d.spans.map((x) => x.offset_ms + x.duration_ms), 0.001);
    const internalCount = d.spans.filter((x) => x.internal).length;
    const spans = d.spans.filter((x) => st.showInternal || !x.internal);
    const slowest = sum.slowest_operation;
    const rows = spans.map((sp) => {
      const track = s('svg', { viewBox: '0 0 1000 14', preserveAspectRatio: 'none', role: 'img', 'aria-label': `${sp.name} starts at ${sp.offset_ms} ms and lasts ${sp.duration_ms} ms` },
        s('rect', { class: `wf-bar ${sp.status === 'error' ? 'error' : sp.slow ? 'slow' : ''}`, x: (sp.offset_ms / total) * 1000, y: 1, width: Math.max((sp.duration_ms / total) * 1000, 3), height: 12, rx: 3 }));
      const name = h('div', { class: 'wf-name', title: sp.name }, sp.slow ? status('warning', '') : null, sp.status === 'error' ? status('critical', '') : null, sp.name);
      name.style.paddingLeft = `${sp.depth * 14}px`;
      return h('div', { class: `wf-row ${st.selSpan === sp.span_id ? 'sel' : ''}`, tabindex: '0', role: 'button', 'aria-label': `Span ${sp.name}`,
        onclick: () => { st.selSpan = st.selSpan === sp.span_id ? null : sp.span_id; drawDetail(); },
        onkeydown: (e) => { if (e.key === 'Enter') { st.selSpan = st.selSpan === sp.span_id ? null : sp.span_id; drawDetail(); } } },
        name, h('div', { class: 'wf-track' }, track), h('div', { class: 'wf-ms', text: fmtMs(sp.duration_ms) }));
    });
    const picked = d.spans.find((x) => x.span_id === st.selSpan);
    detailBox.replaceChildren(card('Trace detail', 'live',
      h('div', {}, mono(sum.trace_id), ' ', sum.status === 'error' ? status('critical', 'error') : status('good', 'ok'),
        ` · ${fmtMs(sum.duration_ms)} · ${sum.span_count} spans`, d.jaeger_url ? [' · ', h('a', { href: d.jaeger_url, target: '_blank', rel: 'noopener', text: 'Open in Jaeger' })] : null),
      slowest ? h('div', { class: 'callout' }, slowest.slow ? status('warning', 'Slow operation: ') : 'Slowest operation: ',
        `${slowest.name} took ${fmtMs(slowest.duration_ms)}${slowest.share_pct !== null ? ` (${slowest.share_pct}% of the request)` : ''}. `,
        'This shows where time went in this request; it does not by itself prove why.') : null,
      sum.outcome_correct === false ? h('div', { class: 'callout' }, status('warning', 'Business outcome mismatch: '), 'the response may be HTTP 200 but the result differs from the expected one (see the validate_business_rules span).') : null,
      h('div', { class: 'toolbar' }, h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: st.showInternal || null, onchange: (e) => { st.showInternal = e.target.checked; drawDetail(); } }),
        `Show ${internalCount} framework spans`)),
      h('div', { role: 'list', 'aria-label': 'Span waterfall' }, rows),
      picked ? h('div', { class: 'section' }, h('h3', { text: picked.name }),
        kv([['Start offset', fmtMs(picked.offset_ms)], ['Duration', fmtMs(picked.duration_ms)], ['Status', picked.status],
          ...Object.entries(picked.attributes).map(([k, v]) => [k, String(v)]),
          ...picked.events.map((ev) => [`event: ${ev.name}`, Object.entries(ev.attributes).map(([k, v]) => `${k}=${v}`).join(', ') || '—'])]))
        : h('p', { class: 'muted', text: 'Select a span to see its attributes. Only an allow-list of non-sensitive attributes is stored.' }),
      d.dropped_spans ? h('p', { class: 'muted', text: `${d.dropped_spans} spans were dropped by the per-trace cap.` }) : null));
  }

  const refresh = ctx.poll(async () => {
    const qs = new URLSearchParams({ limit: '30' });
    if (st.origin) qs.set('origin', st.origin);
    if (st.errorsOnly) qs.set('errors_only', 'true');
    drawList(await api(`/api/traces?${qs}`));
  }, 5000);
  if (st.selected) open(st.selected);
}
