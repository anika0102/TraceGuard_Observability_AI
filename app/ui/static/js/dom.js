// Tiny DOM helpers. Everything is built with createElement/textContent: server data is never parsed as HTML.
export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(typeof c === 'object' ? c : document.createTextNode(String(c)));
  }
  return el;
}

const SVG_NS = 'http://www.w3.org/2000/svg';
export function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) el.setAttribute(k, v);
  append(el, children);
  return el;
}

export const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };

// ---- formatting -------------------------------------------------------------------------------
export const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
export function fmtMs(v) {
  if (!isNum(v)) return null;
  return v >= 1000 ? `${(v / 1000).toFixed(2)} s` : v >= 100 ? `${Math.round(v)} ms` : `${v.toFixed(1)} ms`;
}
export const fmtPct = (v) => (isNum(v) ? `${(v * 100).toFixed(v > 0 && v < 0.001 ? 2 : 1)}%` : null);
export const fmtRps = (v) => (isNum(v) ? (v >= 10 ? v.toFixed(1) : v.toFixed(2)) : null);
export function fmtDuration(sec) {
  if (!isNum(sec)) return '';
  const d = Math.floor(sec / 86400), hh = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return d ? `${d}d ${hh}h` : hh ? `${hh}h ${m}m` : m ? `${m}m ${Math.floor(sec % 60)}s` : `${Math.floor(sec)}s`;
}
export function fmtClock(epochSeconds, withSeconds = true) {
  const d = new Date(epochSeconds * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return withSeconds ? `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}` : `${p(d.getHours())}:${p(d.getMinutes())}`;
}
export function fmtDateTime(epochSeconds) {
  const d = new Date(epochSeconds * 1000);
  return `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${fmtClock(epochSeconds)}`;
}
export function ago(epochSeconds) {
  const sec = Math.max(0, Date.now() / 1000 - epochSeconds);
  return sec < 5 ? 'just now' : `${fmtDuration(sec)} ago`;
}

// ---- badges: source and status are always icon + label, never color alone ----------------------
const SOURCES = {
  live: ['live', '●', 'LIVE', 'Measured by this running instance right now'],
  live_sandbox: ['sandbox', '◆', 'LIVE · SANDBOX', 'Real requests in an isolated demo run that just executed'],
  sandbox: ['sandbox', '◆', 'LIVE · SANDBOX', 'Real requests in an isolated demo run that just executed'],
  demo: ['sandbox', '◆', 'DEMO RUN', 'Produced by a sandboxed scenario run'],
  recorded: ['recorded', '◷', 'RECORDED', 'Replayed from a verified local run; not happening now'],
  na: ['na', '○', 'NOT AVAILABLE', 'Not collected in this deployment'],
};
export function badge(kind) {
  const [cls, icon, label, title] = SOURCES[String(kind).toLowerCase()] || SOURCES.na;
  return h('span', { class: `badge ${cls}`, title }, h('i', { 'aria-hidden': 'true', text: icon }), label);
}

const LEVELS = {
  good: ['good', '✓'], warning: ['warning', '▲'], serious: ['serious', '▲'], critical: ['critical', '✕'],
};
export function status(level, text) {
  const [cls, icon] = LEVELS[level] || LEVELS.warning;
  return h('span', { class: `status ${cls}` }, h('i', { 'aria-hidden': 'true', text: icon }), text);
}
export const severityLevel = (sev) => (sev === 'critical' ? 'critical' : sev === 'warning' ? 'warning' : 'serious');

export const noData = (why = 'No data') => h('span', { class: 'nodata', title: why, text: 'No data' });
export const mono = (t) => h('span', { class: 'mono', text: t });

// ---- API ----------------------------------------------------------------------------------------
export async function api(path, options = {}) {
  const res = await fetch(path, { headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}) }, ...options });
  let data = null;
  try { data = await res.json(); } catch (_) { /* non-JSON error body */ }
  if (!res.ok) {
    const err = new Error((data && (data.detail || data.error)) || `HTTP ${res.status}`);
    err.status = res.status;
    err.retryAfter = res.headers.get('Retry-After');
    throw err;
  }
  return data;
}
