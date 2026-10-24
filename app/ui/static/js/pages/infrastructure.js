import { h, api, fmtDateTime, fmtDuration, badge, status } from '../dom.js';
import { card, kv, table } from '../components.js';

export async function render(root, ctx) {
  const box = h('div', { class: 'grid' });
  root.append(
    h('p', { class: 'muted', text: 'What this instance can truthfully report about itself, kept separate from cluster behaviour captured in recorded local runs.' }),
    box);

  ctx.poll(async () => {
    const d = await api('/api/infrastructure');
    const l = d.live, sv = l.service, rt = l.runtime;
    box.replaceChildren(
      h('div', { class: 'grid cols2' },
        card('Service', 'live', kv([['Name', sv.name], ['Version', sv.version], ['Deployment environment', sv.environment],
          ['Mode', sv.mode === 'public' ? 'public demo (global fault injection does not exist)' : 'local development'],
          ['Python', sv.python], ['Started', fmtDateTime(sv.started_at)], ['Uptime', fmtDuration(sv.uptime_s)],
          ['Memory (RSS)', sv.memory_rss_mb !== null ? `${sv.memory_rss_mb} MB` : 'not available on this platform']])),
        card('Health checks', 'live', table(['Check', 'Endpoint', 'Status'], l.probes.map((p) =>
          h('tr', {}, h('td', { text: p.name }), h('td', {}, h('span', { class: 'mono', text: p.path })),
            h('td', {}, p.status === 'ok' ? status('good', 'passing') : status('critical', 'failing'))))),
          h('p', { class: 'muted', text: 'Liveness failure makes Kubernetes restart the container; readiness failure only removes the pod from the Service.' }))),
      h('div', { class: 'grid cols2' },
        card('Runtime', 'live', kv([['Running in Kubernetes', rt.in_kubernetes ? 'yes' : 'no'], ['Pod', rt.pod_name], ['Namespace', rt.namespace],
          ['Container restart count', 'not visible from inside the process']]),
          h('p', { class: 'muted', text: rt.restart_count_note })),
        card('Embedded telemetry', 'live', kv([['Span store', `${l.telemetry.trace_store.traces}/${l.telemetry.trace_store.max_traces} traces (${l.telemetry.trace_store.spans} spans)`],
          ['Metric history', `${l.telemetry.metrics_history.points}/${l.telemetry.metrics_history.max_points} samples every ${l.telemetry.metrics_history.interval_s}s`],
          ['OTLP export to a collector', l.telemetry.otlp_export_configured ? 'configured' : 'not configured (dashboard uses the embedded store)'],
          ['Not traced', l.telemetry.tracing_excludes]]))),
      card('Cluster behaviour (recorded local runs)', 'recorded',
        h('div', { class: 'callout', text: d.recorded.note }),
        d.recorded.events.length ? h('ul', { class: 'steps' }, d.recorded.events.map((e) => h('li', {}, h('b', { text: `Scenario ${e.scenario}: ${e.title}` }),
          ` · recorded ${fmtDateTime(Date.parse(e.recorded_at) / 1000)} at commit ${e.git_commit}. ${e.headline || ''} `, h('a', { href: '/ui/scenarios', text: 'See the recording' }))))
          : h('p', { class: 'muted', text: 'No recordings are bundled in this build.' })),
      h('div', { class: 'callout' }, badge('na'), ' Node, pod and restart state of a cluster are not shown live: reading them needs Kubernetes API credentials, which this service deliberately does not have and a public visitor must never get.'));
  }, 10000);
}
