# Conjure app (`tools/conjure`)

A flat folder: this whole directory is mounted into the pod as the ConfigMap `conjure-code`.
One Flask process serves the API and the single-page UI. Long work runs on a single background
worker thread, and the UI polls `/jobs/<id>`.

## Modules

| File | Role |
|------|------|
| `main.py` | Flask routes, startup (load clips, seed demo clips, re-harvest sprites) |
| `pipeline.py` | `prepare` (per clip) and `edit` (per command), revert, refusal logging |
| `jobs.py` | job queue with stages and progress |
| `store.py` | clip, version and artifact storage on `/data` (atomic JSON, sha256 artifacts) |
| `vss.py` | VSS login, search, detections sidecar, download |
| `models.py` | YOLO11 endpoint, Cosmos3 review and captions, W&B chat |
| `tracing.py` | Weave wrapper and model-call stats |
| `video.py` | ffmpeg probe, transcode, frame read and write, thumbnails |
| `tracker.py` | per-frame boxes to tracks (IoU, gap interpolation, smoothing) |
| `plate.py` | camera motion, temporal clean plate, per-target fill |
| `editor.py` | remove, blur, anonymize, add, replace compositing, watermark |
| `sprites.py` | procedural props and cutouts harvested from footage |
| `commands.py` | guardrail, NL parsing (LLM with rule fallback), target resolution |
| `provenance.py` | hash-chained, HMAC-signed JSONL log and verification |
| `index.html`, `app.js`, `app.css` | UI (relative URLs, so it works under `/app`) |

## API

| Method and path | Purpose |
|-----------------|---------|
| `GET /health` | status, which secrets are present (booleans only), Weave flag, signing key id |
| `GET/POST /clips`, `GET /clips/<id>` | list clips, add one from a VSS result or an upload, clip detail with tracks and versions |
| `POST /clips/<id>/revert` | move the head to an earlier version (logged) |
| `POST /edit` | `{clip_id, command_text}`, `{clip_id, click_point:[x,y] (0-1), action}`, or `{clip_id, track_ids, action}`; returns a job id |
| `POST /anonymize` | `{clip_id}`; returns a job id |
| `GET /jobs/<id>` | stage, progress, result, or `needs_input` with candidates |
| `GET /evidence/<sha256>` | video bytes (Range supported) |
| `GET /provenance/<clip_id>` and `.jsonl` | log with verification result, raw JSONL |
| `POST /search`, `GET /search_thumb/<id>.jpg` | VSS object search and thumbnails |
| `GET /sprites`, `GET /sprite/<name>.png` | sprite library |
| `GET /calls` | recent model calls and stats |

## Environment variables (names only, all from Secret `conjure-creds`)

`VSS_URL`, `VSS_USERNAME`, `VSS_PASSWORD`, `GPU_BEARER_TOKEN`, `COSMOS3_REASON_URL`, `YOLO_URL`,
`WANDB_API_KEY`, `WANDB_TEAM`, `WANDB_PROJECT`, `CONJURE_SIGNING_KEY`.
Optional: `CONJURE_DATA` (default `/data`) and `PORT` (default `8080`).

Credentials are only read server-side. The browser never sees endpoint URLs or tokens, and VSS
streams are proxied.

## Run locally

```bash
cd tools/conjure
pip install -r requirements.txt
CONJURE_DATA=/tmp/conjure-data python main.py   # needs the env vars above
```

## Deploy on the workshop cluster

```bash
export NS=team-1 KUBECONFIG=/config/team-1-k8s.yaml
kubectl -n "$NS" create configmap conjure-code --from-file=tools/conjure/ --dry-run=client -o yaml | kubectl -n "$NS" apply -f -
sed "s/__APP_HOST__/$APP_HOST/" deploy/conjure.yaml | kubectl -n "$NS" apply -f -
kubectl -n "$NS" rollout status deploy/conjure
```

`deploy/conjure.yaml` creates the PVC `conjure-data`, the Deployment (python:3.12-slim with pip
installs cached on the PVC), the Service, and the Ingress on `/app`. The container runs `main.py`
in a restart loop.

## Update code without a rollout

```bash
NS=team-1 ./deploy/update.sh
```

The script replaces the ConfigMap and waits until the mounted files match the local ones. It then
sends SIGTERM to the Python process, and the loop restarts it. Clips and provenance survive on the
PVC.
