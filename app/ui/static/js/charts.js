// SVG line chart: hairline grid, 2px lines, gaps for missing data, hover crosshair + tooltip, legend, table view.
import { h, s, clear, isNum, fmtClock } from './dom.js';

function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

/**
 * opts: { series: [{ name, cls: 's1'|'s2'|'s3', points: [{t, v|null}] }], height, format(v), yLabel,
 *         thresholds: [{ value, label }], emptyText, caption }
 */
export function lineChart(opts) {
  const { series, height = 210, format = (v) => String(v), thresholds = [], emptyText = 'No data in this window',
    caption = '' } = opts;
  const root = h('div', { class: 'chart' });
  const legend = series.length >= 2
    ? h('div', { class: 'legend' }, series.map((x) => h('span', {}, h('i', { class: `sw ${x.cls}` }), x.name)))
    : null;
  const stage = h('div', {});
  const tip = h('div', { class: 'tip', hidden: true });
  const table = tableView(series, format, caption);
  root.append(...[legend, stage, tip, table].filter(Boolean));

  let lastWidth = 0;
  const draw = () => {
    const width = Math.max(260, Math.floor(stage.clientWidth || root.clientWidth || 600));
    lastWidth = width;
    clear(stage);
    stage.appendChild(render(series, { width, height, format, thresholds, emptyText, tip, root, stage }));
  };
  const ro = typeof ResizeObserver !== 'undefined'
    ? new ResizeObserver(() => { if (Math.abs((stage.clientWidth || 0) - lastWidth) > 4) draw(); })
    : null;
  requestAnimationFrame(() => { draw(); if (ro) ro.observe(stage); });
  return root;
}

function render(series, o) {
  const { width, height, format, thresholds, tip, root, stage } = o;
  const m = { l: 52, r: 14, t: 10, b: 24 };
  const w = width - m.l - m.r, hh = height - m.t - m.b;
  const svg = s('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': 'Line chart; a table view follows the chart' });
  const all = series.flatMap((x) => x.points.filter((p) => isNum(p.v)));
  if (!all.length) {
    svg.appendChild(s('text', { class: 'nodata-text', x: width / 2, y: height / 2, 'text-anchor': 'middle' }, o.emptyText));
    return svg;
  }
  const ts = series.flatMap((x) => x.points.map((p) => p.t));
  const t0 = Math.min(...ts), t1 = Math.max(...ts, t0 + 1);
  const dataMax = Math.max(...all.map((p) => p.v));
  const visibleThresholds = thresholds.filter((x) => x.value <= dataMax * 3 || x.value <= dataMax * 1.2 + 1e-9);
  const yMax = niceMax(Math.max(dataMax, ...visibleThresholds.map((x) => x.value), 1e-9) * 1.05);
  const X = (t) => m.l + ((t - t0) / (t1 - t0)) * w;
  const Y = (v) => m.t + hh - (v / yMax) * hh;

  for (let i = 0; i <= 4; i++) {            // hairline gridlines + y tick labels
    const v = (yMax / 4) * i, y = Y(v);
    svg.appendChild(s('line', { class: i === 0 ? 'axis-line' : 'grid-line', x1: m.l, x2: m.l + w, y1: y, y2: y }));
    svg.appendChild(s('text', { class: 'tick', x: m.l - 8, y: y + 4, 'text-anchor': 'end' }, format(v)));
  }
  const xTicks = Math.max(2, Math.min(6, Math.floor(w / 110)));
  for (let i = 0; i <= xTicks; i++) {
    const t = t0 + ((t1 - t0) / xTicks) * i;
    svg.appendChild(s('text', { class: 'tick', x: X(t), y: height - 6, 'text-anchor': i === 0 ? 'start' : i === xTicks ? 'end' : 'middle' },
      fmtClock(t, t1 - t0 < 600)));
  }
  for (const th of visibleThresholds) {      // threshold: labelled reference line
    const y = Y(th.value);
    svg.appendChild(s('line', { class: 'thresh', x1: m.l, x2: m.l + w, y1: y, y2: y }));
    svg.appendChild(s('text', { class: 'thresh-label', x: m.l + w, y: y - 4, 'text-anchor': 'end' }, th.label));
  }
  for (const ser of series) {                // lines: break the path wherever data is missing
    let d = '', open = false, count = 0;
    for (const p of ser.points) {
      if (!isNum(p.v)) { open = false; continue; }
      d += `${open ? 'L' : 'M'}${X(p.t).toFixed(1)},${Y(p.v).toFixed(1)}`;
      open = true; count++;
    }
    if (count > 1) svg.appendChild(s('path', { class: `line ${ser.cls}`, d }));
    const last = [...ser.points].reverse().find((p) => isNum(p.v));
    if (last && count > 0) svg.appendChild(s('circle', { class: `dot ${ser.cls}`, cx: X(last.t), cy: Y(last.v), r: count === 1 ? 5 : 4 }));
  }

  // hover layer: crosshair snapped to the nearest sample, tooltip with every series' value
  const cross = s('line', { class: 'cross', y1: m.t, y2: m.t + hh, visibility: 'hidden' });
  const marks = series.map((x) => s('circle', { class: `dot ${x.cls}`, r: 5, visibility: 'hidden' }));
  const hit = s('rect', { x: m.l, y: m.t, width: w, height: hh, fill: 'transparent' });
  svg.append(cross, ...marks, hit);
  const uniq = [...new Set(ts)].sort((a, b) => a - b);
  hit.addEventListener('pointermove', (ev) => {
    const box = svg.getBoundingClientRect();
    const px = ((ev.clientX - box.left) / box.width) * width;
    const t = t0 + ((px - m.l) / w) * (t1 - t0);
    const near = uniq.reduce((a, b) => (Math.abs(b - t) < Math.abs(a - t) ? b : a), uniq[0]);
    cross.setAttribute('x1', X(near)); cross.setAttribute('x2', X(near)); cross.setAttribute('visibility', 'visible');
    tip.replaceChildren(h('b', { text: fmtClock(near) }), ...series.map((ser, i) => {
      const p = ser.points.find((q) => q.t === near);
      const ok = p && isNum(p.v);
      marks[i].setAttribute('visibility', ok ? 'visible' : 'hidden');
      if (ok) { marks[i].setAttribute('cx', X(near)); marks[i].setAttribute('cy', Y(p.v)); }
      return h('div', { class: 'row' }, h('i', { class: `sw ${ser.cls}` }), `${ser.name}: `, ok ? format(p.v) : 'no data');
    }));
    tip.hidden = false;
    const rect = stage.getBoundingClientRect(), base = root.getBoundingClientRect();
    const left = Math.min(Math.max(ev.clientX - base.left + 14, 0), base.width - tip.offsetWidth - 4);
    tip.style.left = `${left}px`;
    tip.style.top = `${rect.top - base.top + (ev.clientY - rect.top) - 10 - tip.offsetHeight}px`;
  });
  hit.addEventListener('pointerleave', () => { tip.hidden = true; cross.setAttribute('visibility', 'hidden'); marks.forEach((x) => x.setAttribute('visibility', 'hidden')); });
  return svg;
}

function tableView(series, format, caption) {
  const times = [...new Set(series.flatMap((x) => x.points.map((p) => p.t)))].sort((a, b) => b - a).slice(0, 120);
  const rows = times.map((t) => h('tr', {}, h('td', { text: fmtClock(t) }),
    ...series.map((x) => { const p = x.points.find((q) => q.t === t); return h('td', { class: 'num', text: p && isNum(p.v) ? format(p.v) : 'no data' }); })));
  return h('details', { class: 'tableview' }, h('summary', { text: 'Table view' }),
    h('div', { class: 'table-wrap' }, h('table', {}, caption ? h('caption', { class: 'muted', text: caption }) : null,
      h('thead', {}, h('tr', {}, h('th', { text: 'Time' }), ...series.map((x) => h('th', { class: 'num', text: x.name })))),
      h('tbody', {}, rows))));
}
