# Conjure: infrastructure and workflow context for the build agent

> **Status:** this was the briefing written before the build. What preflight actually found (no
> schedulable GPU, YOLO boxes available, `/app` reused) is recorded in the
> [README](README.md#build-day-preflight-results). Items marked UNVERIFIED below reflect what was
> known beforehand.

**Read this before `conjure_feature_list.md`.** That file says *what* to build. This file says
*where* you are building it and what we learned the hard way building the previous project
(SafeRoute) on the same infrastructure on 2026-10-09.

**Your goal:** build every feature in `conjure_feature_list.md`, in its priority order, deployed
on the workshop cluster, with demo evidence committed to this repo as you go.

How to read this file:

- **[VERIFIED]** means SafeRoute did exactly this on this infrastructure and it worked.
- **[UNVERIFIED]** means nobody has tried it here. Test it in preflight before you depend on it.
- Anything about credentials gives variable **names** only. Never print values.

Reference implementation: https://github.com/madhu-garudala/SafeRoute (code in
`tools/saferoute/`). When this file says "SafeRoute did X", the working code is there.

---

## 1. Where you are running

You are on the VAST Builders Challenge workshop VM (the "VDI"), reached through
https://workshop.thecosmoslabs.com → **Open Desktop**. This is not the user's laptop.

| Item | Value | Status |
|------|-------|--------|
| Workshop helper repo on the VM | `~/vast-builders-challenge` (pre-cloned) | VERIFIED |
| Upstream of that repo | https://github.com/vast-data/vast-builders-challenge | VERIFIED |
| Skills (standard `SKILL.md` files; you do not need Cursor to read them) | `~/vast-builders-challenge/.cursor/skills/` | VERIFIED |
| Team config file | `/config/<team>.config` (holds `USERNAME`, `INGRESS_URL`, credentials) | VERIFIED |
| Kubeconfig | `/config/${NS}-k8s.yaml` where `NS` is the team name | VERIFIED |
| Team / namespace last time | `team-1` | VERIFIED for 2026-10-09; **re-check, it may differ** |
| VM tools | `google-chrome`, `node` v24, `ffmpeg` 6.1 (libx264), `kubectl`, `python3` | VERIFIED |
| System Python on the VM | No `pip`. Do not try to install packages into it | VERIFIED |
| Text-to-speech on the VM | None | VERIFIED |
| Concurrent VM sessions | Maximum two per team | VERIFIED |

The laptop **cannot** reach this environment: the VM hostname does not resolve from outside and
the credentials exist only on the VM and in the cluster. Everything (build, deploy, test) happens
on the VM. The app is not meant to run on `localhost` as the deliverable; it must be deployed to
the team namespace.

### Skills to read first (on the VM)

```bash
cd ~/vast-builders-challenge
find .cursor/skills -name SKILL.md | sort
```

- `retrieval/login/SKILL.md`: VSS login and JWT
- `retrieval/search/SKILL.md`: `POST /api/v1/search`
- `retrieval/videos/SKILL.md`: playback, metadata, YOLO detections
- `retrieval/list-metadata/SKILL.md`: valid `camera_id` values
- `deployment/deploy-app-no-registry/SKILL.md`: **the required deployment pattern; follow it literally**
- `deployment/health/SKILL.md`: cluster and backend health
- `README.md`, `ARCHITECTURE_REFERENCE.md`, `config.example`

The skill files are the source of truth for API shapes. Where this file and a skill disagree,
trust the skill and what a real response looks like.

---

## 2. Hard rules

1. **Never print secrets.** No bare `env` or `printenv`. To see what is set, print names only:
   `env | cut -d= -f1 | sort | grep -E 'USERNAME|INGRESS|WANDB|KUBE|VSS|GPU|COSMOS|YOLO'`
2. **No secrets** in the repo, ConfigMap, HTML, JS, logs, API responses, Weave traces,
   screenshots or recordings. Credentials go in a Kubernetes Secret and are read server-side.
3. **Stay in your own namespace.** Do not deploy to, or read from, another team's namespace.
4. **Do not redeploy or modify the VAST ingestion/VSS stack** running in the namespace.
5. **Do not take down SafeRoute without asking the user.** See section 6.1.
6. **Do not ingest internet video.** Use the indexed archive or footage the user supplies.
7. **Do not record the W&B website** (the project is private). Show traces in-app instead.
8. **Commit and push evidence after every milestone.** The infrastructure is torn down after
   the window. Whatever is not in GitHub is lost.

---

## 3. Preflight (do all of this before writing app code)

```bash
# Team identity, names only
mapfile -t TEAM_CONFIGS < <(find /config -maxdepth 1 -type f -name '*.config' | sort)   # expect one
# parse USERNAME and INGRESS_URL from it without echoing the password
NS="$USERNAME"
export KUBECONFIG=/config/${NS}-k8s.yaml
TEAM_N="${USERNAME#team-}"
APP_HOST="video-lab-team-${TEAM_N}.cosmos.vastdata.com"

kubectl cluster-info
kubectl -n "$NS" get pods,deploy,svc,ingress,secret,configmap
kubectl -n "$NS" auth can-i create deployments
```

Then answer these, in this order. Write the answers at the top of the repo README.

1. **Is SafeRoute still deployed?** Look for `deploy/saferoute`, ConfigMap `saferoute-code`,
   Secret `saferoute-vss-creds`, and an Ingress on path `/app`. This decides section 6.1.
2. **Does the Secret `saferoute-vss-creds` still exist?** If yes, Conjure can reference the same
   Secret (list its key names with
   `kubectl -n "$NS" get secret saferoute-vss-creds -o jsonpath='{.data}' | jq 'keys'`; never
   decode values to the terminal).
3. **Do VSS login, search and stream still work?** (section 4.1)
4. **Do the GPU endpoints still answer?** (section 4.2) `GET $COSMOS3_REASON_URL/v1/models`.
5. **What does the YOLO endpoint actually return?** Conjure needs **bounding boxes**. SafeRoute
   only ever used the `object_counts` field. Post one clip and inspect every key (section 4.2).
6. **Can a pod in this namespace get a GPU?** (section 5) This is the single biggest unknown
   for Conjure.
7. **Does W&B inference answer, and does Weave init work from inside a pod?** (section 4.3)
8. **Can you `git push` from the VM to `git@github.com:madhu-garudala/Conjure.git`?** Check now.
   If the VM has no GitHub credentials, tell the user immediately; do not discover it at hour 5.
9. **Does the pod have outbound internet** for `pip install` and model-weight downloads?
   SafeRoute's pod installed from PyPI and reached `api.inference.wandb.ai`,
   `api.open-meteo.com` and `tile.openstreetmap.org`, so general egress worked. Hugging Face and
   GitHub release downloads are UNVERIFIED.

---

## 4. Services available

Environment variable names used by SafeRoute's pod, all supplied from the Secret:

```text
VSS_URL  VSS_USERNAME  VSS_PASSWORD            VAST video search and stream API
GPU_BEARER_TOKEN  COSMOS3_REASON_URL  YOLO_URL  hosted GPU model endpoints
WANDB_API_KEY  WANDB_TEAM  WANDB_PROJECT        W&B Inference and Weave
```

### 4.1 VAST VSS (video search and playback) [VERIFIED]

**Inside a pod, `VSS_URL` must be `http://video-backend-service:8000`.** The hostname in the
VM's `INGRESS_URL` does **not** resolve inside the pod. From the VM shell, use `$INGRESS_URL`.

Login, with a cached token and re-login on 401 (JWT cached 20 minutes in SafeRoute):

```python
r = requests.post(f"{VSS_URL}/api/v1/auth/login",
                  json={"username": VSS_USERNAME, "password": VSS_PASSWORD}, timeout=15)
token = r.json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}
```

Search:

```python
body = {"query": "crosswalk with people", "top_k": 100, "include_public": True,
        "metadata_filters": {"camera_id": "nyc_streets_cam-1"}}
rows = requests.post(f"{VSS_URL}/api/v1/search", headers=headers, json=body, timeout=60).json().get("results") or []
```

- Other accepted body fields seen in the skills: `llm_top_n`, `min_similarity`.
- Fields SafeRoute read from each result row: `source` (the clip URI, your key for everything),
  `camera_id`, `reasoning_content` (the Cosmos caption), `original_video`, `segment_number`,
  `segment_start_sec`, `segment_end_sec`. Inspect one real response before assuming more.
- Search is occasionally slow or returns 5xx. SafeRoute retried 3 times with 3 s, 6 s, 9 s
  back-off and a 60 s timeout.

Camera ids known to exist:

| `camera_id` | Content |
|-------------|---------|
| `nyc_streets_cam-1`, `nyc_streets_cam-2` | NYC street intersections: pedestrians, taxis, buses, trucks. Best fit for Conjure ("remove the person on the left") |
| `i24_cam-1` | I-24 Nashville highway traffic |
| `pie_cam-3` | PIE driving footage |

Indexed segments are short (SafeRoute treated them as about 5 s at about 15 fps), which matches
Conjure's 5 to 15 s clip budget.

Download or proxy a clip (supports `Range`):

```python
resp = requests.get(f"{VSS_URL}/api/v1/videos/stream",
                    params={"source": source, "token": token},
                    headers={"Range": range_header} if range_header else {}, stream=True, timeout=60)
```

The token goes in a query parameter here, so this URL must **never** reach the browser. Download
the clip server-side and serve your own copy, which is also what the feature list's
`GET /evidence/<id>` requires.

Other endpoints from the skills, not used by SafeRoute [UNVERIFIED]:
`GET /api/v1/videos/detections?source=...` (YOLO sidecar; 404 means none) and
`GET /api/v1/videos/metadata?source=...`.

### 4.2 Hosted GPU model endpoints [VERIFIED for the calls shown]

These are HTTP inference services. They are **not** a GPU your pod can run PyTorch on.
Auth for both: `Authorization: Bearer $GPU_BEARER_TOKEN`.

**NVIDIA Cosmos3-Reason** (video understanding, OpenAI-compatible). Model id seen:
`nvidia/cosmos3-nano-reasoner`; discover it rather than hard-coding:

```python
model = requests.get(f"{COSMOS3_REASON_URL}/v1/models", headers=gpu).json()["data"][0]["id"]
video_b64 = base64.b64encode(open(path, "rb").read()).decode()
r = requests.post(f"{COSMOS3_REASON_URL}/v1/chat/completions", headers=gpu, timeout=90, json={
    "model": model, "max_tokens": 260, "temperature": 0,
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "Return JSON only with keys ..."},
        {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{video_b64}"}},
    ]}]})
text = r.json()["choices"][0]["message"]["content"]
```

- It returns prose around the JSON sometimes. Extract with `re.search(r"\{.*\}", text, re.S)`
  and fail closed if it does not parse.
- About 2.6 s per 5 s clip on average.
- Use for Conjure: describing a clip, checking "did the removal look natural", listing objects
  for search. It does not return boxes or masks.

**YOLO11**:

```python
r = requests.post(f"{YOLO_URL}/v1/infer", headers=gpu, timeout=90,
                  json={"video_base64": video_b64, "filename": os.path.basename(path)})
r.json()["object_counts"]      # e.g. {"person": 331, "car": 90, ...} summed over frames
```

- About 1.9 s per clip on average.
- **SafeRoute only used `object_counts`. Whether the response also contains per-frame boxes is
  UNVERIFIED.** Dump `r.json().keys()` and one nested sample in preflight. If boxes are there,
  target resolution (pipeline step 1) is solved. If not, run `ultralytics` YOLO11n inside the
  pod on CPU (section 5).

Both endpoints handled 8 to 16 concurrent requests fine in SafeRoute (700 clips in about 7 min).

### 4.3 Weights & Biases [VERIFIED]

Inference, OpenAI-compatible. Model that worked: `meta-llama/Llama-3.1-8B-Instruct`. List
`GET https://api.inference.wandb.ai/v1/models` to see what else the key can use.

```python
r = requests.post("https://api.inference.wandb.ai/v1/chat/completions", timeout=20,
    headers={"Authorization": f"Bearer {WANDB_API_KEY}",
             "OpenAI-Project": f"{WANDB_TEAM}/{WANDB_PROJECT}"},
    json={"model": "meta-llama/Llama-3.1-8B-Instruct", "max_tokens": 120, "temperature": 0.2,
          "messages": [{"role": "user", "content": prompt}]})
```

About 0.9 s per call. This is the natural-language command parser for feature 3. Demand strict
JSON, validate it, and ask the user instead of guessing when it does not parse.

Weave:

```python
import weave
weave.init(f"{WANDB_TEAM}/{WANDB_PROJECT}")     # wrap in try/except; the app must run without it
```

- **`WANDB_*` variables in the VM shell are not in the pod automatically.** They must be wired
  from the Secret into the Deployment env.
- SafeRoute's `tools/saferoute/tracing.py` is a ready-made wrapper: it traces to Weave when
  available, falls back to plain calls when not, and keeps an in-memory log (op name, ms, ok,
  Weave trace id) that the UI shows in a "Model calls" panel. Copy it. It lets the demo prove
  the model calls without showing the private W&B site.
- That in-memory log resets when the process restarts, and the panel then shows "0 calls" for
  anything cached on disk. Persist the counters if the panel matters to the demo.

### 4.4 Not available

No image or video **generation** endpoint was found. Feature 7 (Add) should plan on the sprite
library approach from the feature list unless preflight finds one.

---

## 5. The big unknown: running Conjure's own models

Conjure needs SAM2 (or FastSAM/MobileSAM), an inpainting model (ProPainter/E2FGVI/LaMa) and a
box detector. SafeRoute never ran a model inside its pod; it only called the hosted endpoints
above. So **nothing in this section is verified.** Settle it in the first 30 minutes.

Check, in order:

```bash
kubectl get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.'nvidia\.com/gpu'
kubectl -n "$NS" describe quota 2>/dev/null; kubectl -n "$NS" describe limitrange 2>/dev/null
```

Then try a throwaway pod requesting `nvidia.com/gpu: 1` and run `nvidia-smi`. Ask the onsite
VAST support (or `/ask-cosmos`) whether teams may schedule GPUs; do not burn an hour guessing.

Decision table:

| Preflight result | Model set |
|------------------|-----------|
| GPU pod schedulable | SAM2 small (video mode) + ProPainter or LaMa, on a CUDA PyTorch image |
| CPU only | Box detector: hosted YOLO if it returns boxes, else `ultralytics` YOLO11n on CPU. Segmenter: FastSAM-s or MobileSAM on CPU. Inpainting: LaMa per frame on CPU at 480p to 720p if it is fast enough, else `cv2.inpaint` (Telea) with temporal blending. Tracking: per-frame detection matched by IoU |
| Cannot install torch at all | `cv2` only: hosted YOLO boxes (if available) + GrabCut refinement + `cv2.inpaint`, and ship BLUR/redact honestly, as the feature list's fallback column allows |

Practical constraints of this deployment pattern that bite ML workloads:

- The pod image is `python:3.12-slim` and runs `pip install -r requirements.txt` **on every
  start**. With `torch` that is a multi-GB download and minutes of startup. Use the CPU wheel
  index (`--extra-index-url https://download.pytorch.org/whl/cpu`) unless you have a GPU, and
  check that every package has a Python 3.12 wheel.
- `python:3.12-slim` has no system `ffmpeg` and no `libGL`. SafeRoute got ffmpeg from the
  `imageio-ffmpeg` pip package (`imageio_ffmpeg.get_ffmpeg_exe()`). Use
  `opencv-python-headless`, not `opencv-python`.
- **Model weights cannot go in the ConfigMap** (about 1 MiB limit for all code). Download them
  at startup into the data volume, in a background thread, with progress on `/health` or
  `/state`. Start that download during preflight.
- SafeRoute's data volume was an `emptyDir`: a new pod starts empty. See section 6.3.
- Set memory requests/limits deliberately; SAM2 and inpainting on CPU can use several GB.
  Check the namespace quota first.

---

## 6. Deployment [VERIFIED pattern]

Follow `deployment/deploy-app-no-registry/SKILL.md` literally. What SafeRoute ended up with:

- App code in one **flat** folder (`tools/saferoute/`), mounted from a ConfigMap at `/code`.
  **ConfigMaps do not mount subdirectories.** `index.html`, JS, CSS and Python all sit in the
  same folder; Flask used `template_folder="."` and served assets from that folder.
- Total ConfigMap payload must stay under about 1 MiB. SafeRoute vendored Leaflet (148 KB) and
  was fine. No images, sprites or weights in there; fetch or generate those at runtime.
- Image `python:3.12-slim`; start command installs `requirements.txt`, then runs `main.py`.
- Flask on port 8080, `host="0.0.0.0"`, `threaded=True`. Readiness probe on `/health`.
- Credentials from a Secret via `secretKeyRef` env entries.
- Service, plus Ingress on host `video-lab-team-<N>.cosmos.vastdata.com`, path
  `/app(/|$)(.*)` rewritten to `/$2`. Inside the app, routes are at `/`.

```bash
APP_DIR=tools/conjure; APP_NAME=conjure
kubectl -n "$NS" create configmap "${APP_NAME}-code" --from-file="$APP_DIR" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" rollout status deploy/$APP_NAME
kubectl -n "$NS" logs -l app=$APP_NAME --tail=100
curl -sS "http://${APP_HOST}/app/health"
```

SafeRoute's pinned requirements, known to install on this image:
`flask==3.1.2  requests==2.32.5  numpy==2.3.3  imageio-ffmpeg==0.6.0  weave==0.53.11`

### 6.1 The `/app` path is already taken (decide this with the user)

SafeRoute is (or was) live at Ingress path `/app`, and its public link is in its README:

- Public URL: `https://team-1-app.thecosmoslabs.com/app/`
- Also reached by https://workshop.thecosmoslabs.com → **App** button
- Internal: `http://video-lab-team-1.cosmos.vastdata.com/app/` (VM only)

The workshop **App** button and the public hostname are only known to work for `/app`. Options:

1. **Preferred to try first:** deploy Conjure as its own Deployment/Service with a second
   Ingress path (for example `/conjure(/|$)(.*)`) and check whether
   `https://team-<N>-app.thecosmoslabs.com/conjure/` is reachable from outside. UNVERIFIED.
2. If only `/app` is exposed publicly: **ask the user** before pointing `/app` at Conjure,
   because that takes SafeRoute's public demo link offline.

Do not delete `deploy/saferoute`, its ConfigMap or its Secret on your own initiative.

### 6.2 The app runs under a path prefix

Because of the Ingress rewrite, the browser sees `/app/...` while Flask sees `/...`. Absolute
URLs like `fetch("/edit")` will break. SafeRoute's fix, which works for any prefix:

```html
<script>document.write('<base href="' + location.pathname.replace(/\/?$/, "/") + '">');</script>
```
```js
const prefix = location.pathname.replace(/\/?$/, "/");
fetch(prefix + "edit", { method: "POST", ... });
```

Use relative URLs for every video, image and API call.

### 6.3 Updating code without losing data

A `kubectl rollout restart` creates a **new pod with an empty `emptyDir`**. For SafeRoute that
meant a 7-minute re-analysis. For Conjure it would mean re-downloading weights and losing every
edited clip and provenance log. To update code in place:

```bash
kubectl -n "$NS" create configmap conjure-code --from-file=tools/conjure \
  --dry-run=client -o yaml | kubectl replace -f -
# wait about 10 s for the mounted files to sync, then restart only the process:
kubectl -n "$NS" exec deploy/conjure -- sh -c 'kill -INT 1'
```

The container restarts in the same pod, so the volume survives (pip packages are reinstalled).
Design for this:

- Write clips, outputs, provenance logs and weights under one data dir (env `CONJURE_DATA`,
  default `/data`), and reload state from disk at startup.
- If `kubectl get storageclass` shows one you may use, a PersistentVolumeClaim is better than
  `emptyDir`. UNVERIFIED.
- **Provenance logs and demo outputs must also be copied out of the pod and committed**
  (`kubectl cp`), because the whole cluster disappears at the end.

---

## 7. App patterns that worked

- **One Flask process, flat folder, inline UI.** No build step, no frontend project.
- **Never block a request on model work.** Run jobs in a background thread, expose progress on a
  polling endpoint, show per-stage progress in the UI. (Matches the feature list's
  `POST /edit` → job id → `GET /jobs/<id>`.)
- **Cache everything on disk** keyed by clip and parameters: downloaded clips, extracted frames,
  masks, outputs. Load the cache at startup so a restart never shows an empty app.
- **Serve media from your own disk** with `send_file(path, mimetype="video/mp4",
  conditional=True)`. `conditional=True` gives Range support, which `<video>` seeking needs.
- **Encode for browsers:** `-c:v libx264 -preset veryfast -crf 26 -pix_fmt yuv420p
  -movflags +faststart -an`. Thumbnails: `-ss 1.0 -i clip -frames:v 1 -vf scale=320:180`.
- **`/health` reports which secret names are present** as booleans (never values) plus the
  state of any background build. This saved a lot of debugging.
- **Fail closed and say so.** When a model reply cannot be parsed, return an explicit
  "could not read" state and show it in the UI.
- **Label honestly in the UI:** archived footage, simulated or edited, hackathon-grade.

Files worth copying from SafeRoute and adapting: `vast_client.py` (login, stream),
`cameras.py` (`_search` with retries, `download`), `vision.py` (Cosmos and YOLO calls),
`briefing.py` (W&B inference call), `tracing.py` (Weave wrapper + call log), and the `sim.py`
ffmpeg helper (`ffmpeg_bin`, `_run`).

---

## 8. Workflow lessons from SafeRoute

1. **Deploy a "hello" slice to the cluster in the first hour.** Do not develop on localhost and
   deploy at the end; pod-only problems (VSS hostname, missing `WANDB_*`, pip failures) surface
   only in the pod.
2. **Keep the repo equal to what is deployed.** On SafeRoute the deployed UI moved ahead of the
   repo and the final commit never caught up. After every ConfigMap update, commit and push the
   same files.
3. **Commit evidence as you go:** screenshots, short recordings, provenance JSON samples, under
   `docs/` in the repo. Keep large MP4s out of git (use a git-ignored `demo_video/` and link a
   Drive folder from the README) but do commit screenshots and small clips.
4. **Trim the README at the end to what actually works**, with the real model set written at
   the top.
5. **Do not paste credentials, tokens or internal-only details into commits.** Scrub before
   pushing; the repo is public.

### Repo conventions to match SafeRoute

```text
README.md                 pitch, links, screenshots, what works, built-with table, quick start
LICENSE                   MIT, "Copyright (c) 2026 Conjure contributors"
conjure_feature_list.md   the spec (already here)
helper_context.md         this file
tools/conjure/            the app (flat folder; this is what goes in the ConfigMap)
tools/conjure/README.md   how it works, env var names, run and deploy commands
docs/screenshots/         JPEG screenshots, about 1800 px wide, about 0.5 MB each
docs/                     provenance log example, architecture diagram, demo script
.gitignore                __pycache__/  demo_video/  .DS_Store  .env
```

Git: remote is `git@github.com:madhu-garudala/Conjure.git`, branch `main`. Write commit
messages as short imperative sentences.

---

## 9. Recording the demo on the VM

The VM has no screen recorder and no text-to-speech. What worked for SafeRoute:

- Drive the deployed app with **headless Chrome over the DevTools protocol** from a Node script
  (`google-chrome --headless=new --no-sandbox --disable-gpu
  --autoplay-policy=no-user-gesture-required --remote-debugging-port=9340`), capture with
  `Page.startScreencast` (ack every frame; frames only arrive when the page changes, so use
  each frame's timestamp as its duration), then assemble with ffmpeg's concat demuxer.
- Inject a fake cursor element and click through `element.click()`, since headless has no
  visible pointer.
- Burn captions in; the user records the voice-over separately and it is laid over later.
- The full, working procedure is in SafeRoute's `docs/video_demo_script.md`. Reuse it.
- Keep secrets, terminals, `/health` output and the W&B site out of every frame.

Aim for a 2:00 to 2:30 video: a fun edit first, the before/after slider, then close on the
provenance log.

---

## 10. Preflight checklist (copy into the README and fill in)

```text
[ ] Team / namespace:
[ ] SafeRoute still deployed at /app?            yes / no
[ ] Secret saferoute-vss-creds present, keys:    (names only)
[ ] VSS login + search + stream from a pod:      ok / fail
[ ] Cosmos endpoint, model id:
[ ] YOLO endpoint returns boxes?                 yes / no  (keys seen: ...)
[ ] GPU schedulable in the namespace?            yes / no
[ ] torch installs on python:3.12-slim?          yes / no  (startup time: ...)
[ ] Weights download from the pod?               yes / no
[ ] W&B inference model used:
[ ] Weave init from the pod:                     ok / degraded
[ ] git push from the VM works:                  yes / no
[ ] Conjure URL (path chosen):
[ ] Final model set: detector / segmenter / tracker / inpainter
```
