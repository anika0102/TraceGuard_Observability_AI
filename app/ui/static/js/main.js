// App shell: navigation, page loading, polling with a visible error state. One HTML shell serves every section.
import { h, api, badge, fmtClock, clear } from './dom.js';

const SECTIONS = [
  ['overview', 'Overview', '/ui/', '◧'],
  ['metrics', 'Metrics', '/ui/metrics', '∿'],
  ['traces', 'Traces', '/ui/traces', '⛓'],
  ['incidents', 'Incidents', '/ui/incidents', '⚑'],
  ['scenarios', 'Scenarios', '/ui/scenarios', '▶'],
  ['infrastructure', 'Infrastructure', '/ui/infrastructure', '▤'],
];

const segment = location.pathname.split('/').filter(Boolean)[1];
const current = SECTIONS.some(([k]) => k === segment) ? segment : 'overview';

const bannerEl = document.getElementById('banner');
function banner(text, level = 'warning') {
  if (!text) { bannerEl.hidden = true; return; }
  bannerEl.textContent = text;
  bannerEl.className = `banner ${level === 'error' ? 'error' : ''}`;
  bannerEl.hidden = false;
}

function buildNav() {
  const nav = document.getElementById('nav');
  for (const [key, label, href, icon] of SECTIONS) {
    nav.appendChild(h('a', { href, 'aria-current': key === current ? 'page' : null },
      h('span', { 'aria-hidden': 'true', text: icon }), label));
  }
  const foot = document.getElementById('side-foot');
  foot.append(h('div', { text: 'Every number is labelled:' }),
    h('div', {}, badge('live')), h('div', {}, badge('sandbox')), h('div', {}, badge('recorded')));
}

const pollers = [];
function poll(fn, ms = 5000) {
  let timer = null, running = false;
  const tick = async () => {
    if (running || document.hidden) return;
    running = true;
    try {
      await fn();
      banner('');
      document.getElementById('updated').textContent = `updated ${fmtClock(Date.now() / 1000)}`;
    } catch (err) {
      if (err.status === 429) banner(`Rate limit reached; retrying in ${err.retryAfter || 'a few'} s.`);
      else banner('Cannot reach the backend right now (a free-tier instance may be waking up, which can take about a minute). Retrying automatically.', 'error');
    } finally { running = false; }
  };
  tick();
  if (ms) timer = setInterval(tick, ms);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
  pollers.push(() => clearInterval(timer));
  return tick;
}

async function start() {
  buildNav();
  const label = SECTIONS.find(([k]) => k === current)[1];
  document.getElementById('page-title').textContent = label;
  document.title = `${label} · TraceGuard`;
  let meta = null;
  try { meta = await api('/api/meta'); } catch (_) { banner('Cannot reach the backend right now; retrying.', 'error'); }
  const modeEl = document.getElementById('mode-badge');
  modeEl.textContent = meta ? (meta.mode === 'public' ? 'PUBLIC DEMO' : 'LOCAL') : 'OFFLINE';
  modeEl.title = meta && meta.mode === 'public' ? 'Public demo: global fault injection does not exist' : 'Local development environment';
  const root = document.getElementById('main');
  clear(root);
  const ctx = { meta: meta || { mode: 'local', links: {}, thresholds: {}, limits: {} }, api, poll, banner };
  const page = await import(`./pages/${current}.js`);
  await page.render(root, ctx);
}

start().catch((err) => { banner(`The page failed to load: ${err.message}`, 'error'); });
