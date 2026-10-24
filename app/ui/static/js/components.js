import { h, badge, status, noData } from './dom.js';

/** A headline number. `value` null => explicit "No data" (never a fake 0). */
export function tile({ label, value, unit, sub, source = 'live', level, why }) {
  const valueEl = value === null || value === undefined
    ? h('div', { class: 'tile-value' }, noData(why))
    : h('div', { class: 'tile-value' }, value, unit ? h('small', { text: ` ${unit}` }) : null);
  return h('section', { class: 'card', 'aria-label': label },
    h('div', { class: 'card-head' }, h('div', { class: 'tile-label', text: label }), badge(source)),
    valueEl,
    level ? status(level.kind, level.text) : null,
    sub ? h('div', { class: 'tile-sub', text: sub }) : null);
}

export function card(title, source, ...body) {
  return h('section', { class: 'card' },
    h('div', { class: 'card-head' }, h('h3', { text: title }), source ? badge(source) : null), ...body);
}

export function kv(pairs) {
  return h('dl', { class: 'kv' }, pairs.filter(([, v]) => v !== null && v !== undefined && v !== '')
    .flatMap(([k, v]) => [h('dt', { text: k }), h('dd', {}, typeof v === 'object' ? v : String(v))]));
}

export function table(headers, rows, opts = {}) {
  return h('div', { class: 'table-wrap' }, h('table', {},
    opts.caption ? h('caption', { class: 'muted', text: opts.caption }) : null,
    h('thead', {}, h('tr', {}, headers.map((x) => h('th', { class: x.num ? 'num' : '', text: x.label || x })))),
    h('tbody', {}, rows)));
}
