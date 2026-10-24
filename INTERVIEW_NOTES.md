# TraceGuard. interview notes

Be upfront about status (details in README "Verification status"): the whole local stack was run and verified (84 automated tests; Docker image; Compose; app -> Collector -> Jaeger; Prometheus; Grafana via its API; kind deployment; scenarios A-E; Ansible dry run, restart and rollback; `scripts/verify.sh` 7/7). Not verified: Minikube, macOS, how the Grafana/Jaeger pages look visually, and anything beyond single-run timings. Before a live demo, run `scripts/verify.sh` once. This project does **not** use Dynatrace.

Honest talking points that show engineering judgement (each was found and fixed during validation, with a test): the allow-list in the Ansible playbook was overridable by `-e` and is now literals; Prometheus counters created lazily hid the first burst of errors; port-forwards died with a one-shot shell and after pod replacement; a hard-coded "Prometheus up=0" claim was wrong in about one run in four, so the demo now reports only what Prometheus recorded.

## 30-second pitch

"A service can be up, fast and returning 200 while giving customers the wrong answer. TraceGuard is a small AI-style recommendation API on Kubernetes where I inject slow requests, failures and silent wrong results. OpenTelemetry traces, Prometheus metrics, JSON logs and an outcome validator expose each one, a deterministic detector turns them into incident records with real trace IDs, and Kubernetes plus an allow-listed Ansible playbook handle recovery."

## Two-minute architecture walkthrough

1. FastAPI `/recommend` runs five traced steps: validate input, catalog lookup (simulated DB/tool call), compute, business-rule validation, build response.
2. The validator compares the result with an independent golden reference and records `outcome_correct` on the span, a counter, and the response.
3. The OTel SDK exports OTLP/gRPC to a Collector (memory limiter, batch), which forwards to Jaeger. Prometheus scrapes low-cardinality metrics; Grafana shows rate, errors, p50/p95, readiness, correctness, incidents.
4. The incident detector keeps a sliding window of real requests and applies thresholds for latency p95, error rate, correctness and readiness. Each incident has evidence, a real trace ID (or null), investigation steps, remediation, and `root_cause_confirmed=false`.
5. Kubernetes: probes, requests/limits, rolling update with `maxUnavailable: 0`. Container crash -> kubelet restart; pod deleted -> ReplicaSet recreates.
6. Ansible: validate against allow-lists, check state, `rollout restart`/`undo`, wait, probe readiness plus a real request, summarise. `--check` for dry run.
7. Fault injection requires `TRACEGUARD_ENV=local` plus an explicit flag.

## Why OpenTelemetry instead of vendor-specific instrumentation alone

Instrument once with a standard API and move or fan out backends in the Collector (Jaeger now, Dynatrace or another OTLP backend later) without touching application code. Semantic conventions make traces comparable across services and languages, and it avoids lock-in on the code path. A vendor agent can still add value (auto-discovery, AI-assisted root cause); the two can coexist.

## Why Kubernetes and Ansible have different responsibilities

Kubernetes is a continuous reconciler for declared state: it restarts dead containers, removes unready pods from traffic, replaces lost pods. It is automatic, fast and local, but it only knows what probes tell it. Ansible is an operator-triggered, auditable procedure: it applies judgement-level actions (restart to clear bad state, roll back a release) with pre-checks, allow-lists, dry run and post-verification. Use Kubernetes for "the process is broken"; use the playbook for "the process runs but the release or state is bad."

## Why a successful HTTP response does not guarantee business correctness

Status codes and probes describe transport and process state. A stale ranking, bad feature data or a regressed model still returns 200 with well-formed JSON. In the demo `/health`, `/ready` and error rate stay green while 100% of outcomes are wrong; only the outcome validator, the `outcome_correct` span attribute and the correctness-rate metric show it. For real AI systems the same applies to hallucinations or wrong tool results, so you need outcome-level checks (golden sets, shadow comparison, sampled evaluation).

## Extending to a real AI agent / Dynatrace

- Agent: trace each LLM call and tool call as child spans (model, prompt/response token counts, latency, tool name; not raw user content), add evaluation spans (groundedness, policy checks, golden-set comparison), and record evaluation results as metrics with bounded labels.
- Dynatrace: add an OTLP exporter in the Collector pointing at the tenant's OTLP endpoint with an API token from a Kubernetes Secret; keep Jaeger in parallel at first to compare. Send incidents as custom events or metrics so they correlate with Dynatrace problems. Not built or verified here.

## Three engineering trade-offs

1. **Deterministic rules before ML/LLM diagnosis.** Predictable, testable, work offline, explainable in an incident review; cost: thresholds need tuning and miss novel patterns. An LLM explanation can be layered on top later without becoming the source of truth.
2. **In-process incident detection and fault state, one replica.** Keeps the demo and tests simple and the data real, and lets incidents carry actual trace IDs; cost: state is per-pod and lost on restart, so production should compute incidents centrally from Prometheus/trace data.
3. **Trace IDs on incidents and the console, not in metric labels.** Avoids unbounded cardinality in Prometheus; cost: Grafana cannot list trace IDs directly, so the console/API links to Jaeger instead.

Also be ready to say: the golden reference shares a codebase with the pipeline (pattern demo, not independent ground truth), and the Ansible probe uses `kubectl exec` rather than the Service to avoid depending on a port-forward.
