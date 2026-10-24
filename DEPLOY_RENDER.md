# Deploying the public TraceGuard demo on Render (free)

One free Render web service, built from this repository's Dockerfile, serving the dashboard and API on one HTTPS URL.
No database, disk, Prometheus, Grafana, Jaeger or Kubernetes is involved.

## What you need
- This repository on GitHub (public is fine; it contains no secrets).
- A free Render account (https://render.com). **Render may ask for a payment card** when you create the service. Community
  reports say this is anti-fraud verification (a $1 check that is reversed) and the official pages do not state it either way.
  The free plan itself costs nothing. Read "Cost guard" below before adding a card.

## 1. Push the code (after the owner has approved the exact changes)
```bash
cd ~/traceguard
git push -u origin main        # never force-push
```

## 2. Create the service (Blueprint: fastest)
1. Render dashboard -> **New +** -> **Blueprint**.
2. Connect your GitHub account and pick the TraceGuard repository (branch `main`).
3. Render reads `render.yaml` and shows one service, `traceguard`. **Check that the plan says "Free".** Click **Apply**.
4. Wait for the first build (about 3-6 minutes). In **Logs** you should see
   `Uvicorn running on http://0.0.0.0:10000` and then the service turns **Live**.

### Manual alternative (if you prefer clicking through)
**New +** -> **Web Service** -> your repo -> Language **Docker** -> Dockerfile path `./docker/Dockerfile` ->
Instance type **Free** -> Health Check Path `/health` -> Auto-Deploy **Off** -> Environment variables:

| Key | Value |
|---|---|
| `TRACEGUARD_PUBLIC_DEMO` | `true` |
| `TRACEGUARD_ENV` | `production` |
| `CLIENT_IP_HEADER` | `True-Client-IP` |

No secrets are required. Do **not** add `TRACEGUARD_ENABLE_FAULT_INJECTION`.

## 3. Open it
Your URL is shown at the top of the service page: `https://<name>.onrender.com`. It redirects to `/ui/`.
A free service sleeps after 15 minutes without traffic; the first request after that takes about a minute to wake.

## 4. Verify the public deployment (copy-paste; replace `URL`)
```bash
URL=https://<name>.onrender.com

# pages and API respond
for p in / /ui/ /ui/metrics /ui/traces /ui/incidents /ui/scenarios /ui/infrastructure /api/meta /api/overview /health; do
  printf "%s %s\n" "$(curl -s -o /dev/null -w '%{http_code}' -L "$URL$p")" "$p"; done      # all 200

# operator routes must NOT exist (all 404)
for p in /metrics /incidents /console /simulate-failure /docs /openapi.json; do
  printf "%s %s\n" "$(curl -s -o /dev/null -w '%{http_code}' "$URL$p")" "$p"; done
curl -s -o /dev/null -w '%{http_code} POST /simulate-failure\n' -X POST "$URL/simulate-failure" -H 'Content-Type: application/json' -d '{"scenario":"slow"}'

# security headers on the UI
curl -sI "$URL/ui/" | grep -iE 'content-security-policy|x-frame-options|x-content-type-options'

# scenarios A-C run live and are detected; D and E are recordings
for s in A B C; do curl -s -X POST "$URL/api/scenarios/$s/run" -H 'Content-Type: application/json' -d '{}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['scenario'],d['label'],'detected',d['detection']['detected'],'matched',d['detection']['matched'])"; done
curl -s -o /dev/null -w '%{http_code} (409 = recorded only)\n' -X POST "$URL/api/scenarios/D/run" -H 'Content-Type: application/json' -d '{}'
curl -s "$URL/api/scenarios/D/recording" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['label'],d['git_commit'],d['summary']['headline'])"
```

### The one assumption to verify: Render overwrites `True-Client-IP`
Rate limits are keyed on that header. If a client could forge it, limits would be bypassable (the global ceilings would still
cap total load). Test with forged values; you should see `429` after about 10 runs:
```bash
for i in $(seq 1 14); do
  curl -s -o /dev/null -w '%{http_code} ' -X POST "$URL/api/scenarios/A/run" -H 'Content-Type: application/json' \
    -H "True-Client-IP: 198.51.100.$i" -d '{"requests":1}'; done; echo
```
- **`200 200 ... 429 429`**: Render overwrites the header; the limit holds. Done.
- **All `200`** (until the global ceiling of 60/min): the header is forgeable. In Render -> Environment, **delete
  `CLIENT_IP_HEADER`**, redeploy, and re-run the test. The app then keys on the socket peer (all visitors share one bucket:
  safe, just stricter).

Also check in Render -> **Logs**: no `Traceback` or `ERROR`, and the browser console on `/ui/` shows no errors.

## Cost guard
- `render.yaml` sets `plan: free` explicitly (Render defaults to a paid instance if omitted) and `autoDeployTrigger: "off"`, so
  pushes do not consume build minutes; update with **Manual Deploy**.
- Render's docs list 750 free instance hours per month and say overages are billed only if a payment method is on file (without
  one, free services are suspended instead). Do not add a disk, a second instance, a database or a paid instance type.
- Watch **Account -> Usage** after the first week. To stop everything: service -> **Settings -> Suspend** (or Delete).
- Free-tier facts used here (Render docs): web services only, 512 MB RAM, 0.1 CPU, no persistent disk, spin-down after 15 minutes
  idle. This app uses about 50-75 MB RAM and keeps everything in memory, so a restart or sleep clears traces, metric history
  and incidents by design.

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| Deploy fails with "no open port detected" | The service must bind `0.0.0.0:$PORT`; the Dockerfile does. Check Logs for a Python import error. |
| First visit hangs about a minute | Free instance waking from sleep; normal. |
| `/metrics`, `/incidents`, `/docs` return 404 | Intended in public mode. |
| Scenario run returns 429 | Rate limit (10 runs/min per client, 60/min overall). Wait a few seconds. |
| Dashboard shows "Recording not available" for D/E | The fixtures in `app/ui/recordings/` were not in the build; confirm they are committed. |
