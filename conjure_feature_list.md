# Conjure — Feature Spec & Implementation Brief

**Purpose of this file:** Self-contained context for a coding agent (Claude Code / Cursor) building Conjure on remote infrastructure. Assume the agent has NOT seen any prior conversation. Everything needed to understand the product, prioritize features, and make implementation decisions is in this document.

---

## 1. What Conjure Is

**Tagline:** *Conjure it in. Conjure it away. Conjure it up.*

Conjure is an AI-powered **video editing agent**. A user describes an edit in plain English ("remove the person on the left," "replace the sedan with a truck"); Conjure finds the object, tracks it through the clip, and applies the edit across every frame while preserving the original camera angle, lighting, and motion. Every edit is recorded in a signed provenance log — **auditable editing, not silent manipulation**.

**One-line repo description:**
> Conjure — an AI video editing agent: add, remove, or replace objects in real footage with plain-English commands, with tracked edits across frames, object search, one-click anonymization, and a signed provenance log of every change.

---

## 2. Environment & Constraints (read first)

- **Infrastructure:** Remote workshop environment (VAST Data hackathon stack), available for a **limited window (~6 hours)**. The environment will be torn down afterward — the deliverable is a working demo, a GitHub repo, and screenshots/recordings committed to the repo *before* teardown.
- **Available stack (verify in preflight, do not assume):**
  - VAST VSS: video storage + semantic search over pre-indexed clips (JWT auth, `POST /api/v1/search`, metadata filters by `camera_id`, Cosmos `reasoning_content` captions, optional YOLO detections endpoint, stream playback)
  - Weights & Biases serverless inference (`https://api.inference.wandb.ai/v1`, OpenAI-compatible client) + Weave tracing
  - Kubernetes team namespace with Ingress at `/app` (ConfigMap-based deploy, `python:3.12-slim`, no registry)
  - GPU access via the workshop cluster (check what's actually schedulable before committing to heavy models)
- **Security rules carried over from the workshop:**
  - No secrets in repo, ConfigMap, HTML, JS, logs, or Weave traces. VSS JWT and W&B keys live in a K8s Secret, server-side only.
  - Video playback goes through a same-origin backend proxy (`GET /evidence/<id>`), never a raw presigned URL in the browser.
  - Do not ingest random internet video (licensing). Use the already-indexed archive or footage the user supplies/records.
- **Model availability is uncertain.** Preflight must confirm which of these can actually run here: SAM2 (or FastSAM/MobileSAM), a video inpainting model (ProPainter, E2FGVI, or LaMa per-frame as fallback), a grounding/detection model (YOLO11, optionally GroundingDINO for open-vocabulary). **If a model can't run, degrade the feature per the fallback column in §4 — do not block the build.**

---

## 3. Core Pipeline (shared by all editing features)

```
User text command
      │
      ▼
[1] TARGET RESOLUTION
    Parse command → object description + spatial hint ("person on the left")
    Detection: YOLO11 (closed-set classes) or GroundingDINO (open vocabulary)
    Spatial filter: resolve "left/right/center/largest/nearest" against bbox positions
    Ambiguity rule: if multiple candidates and no disambiguator → return candidates
    with thumbnails and ask user to click one. Never guess silently.
      │
      ▼
[2] MASK GENERATION
    Selected bbox → SAM2 (or FastSAM) → pixel-accurate mask on anchor frame
      │
      ▼
[3] TEMPORAL PROPAGATION
    SAM2 video mode propagates mask across frames (handles occlusion re-entry).
    Fallback: per-frame detect+match by IoU/embedding similarity (jittery but works).
      │
      ▼
[4] EDIT OPERATION (one of)
    REMOVE  → video inpainting over masked region (ProPainter/E2FGVI;
              fallback LaMa frame-by-frame + temporal blending)
    ADD     → composite generated/library object into region, match scale via
              scene geometry; feather edges; (stretch: relight)
    REPLACE → REMOVE then ADD on the same mask region
    BLUR    → gaussian/pixelate masked region (cheap path used by Anonymize)
      │
      ▼
[5] RENDER + PROVENANCE
    Re-encode clip (ffmpeg). Write provenance entry (§5). Store original untouched;
    edits always produce a NEW artifact. Originals are immutable.
```

**Key engineering rules:**
- **Originals are immutable.** Every edit creates a new versioned output file. Never overwrite source footage.
- Process at a working resolution (e.g., 720p) for speed; note original resolution in provenance.
- Keep clips short for the demo (5–15 s segments). Long-video support is out of scope.
- All model calls wrapped in `@weave.op` for tracing if Weave is available; app must still run if Weave init fails.

---

## 4. Feature List (priority order — build top-down, cut bottom-up)

| # | Feature | What it does | Implementation notes | Fallback if blocked |
|---|---------|--------------|----------------------|---------------------|
| 1 | **Remove** | Erase any object from footage; scene fills naturally behind it | Pipeline steps 1–3 + inpainting. This is the hero feature — get it working end-to-end on a single frame first, then propagate | Per-frame LaMa + crossfade blending; if even that fails, ship BLUR as "redact" and be honest about it |
| 2 | **Track-and-edit** | The edit follows the object through the entire clip | SAM2 video propagation. **Table stakes** — without this it's an image tool, not a video tool. Budget it early | Per-frame detection matching (IoU > 0.5 between consecutive frames); accept jitter |
| 3 | **Natural-language targeting** | "Remove the person on the left" — no manual masking | LLM (W&B inference) parses command → `{action, object_class, spatial_hint, attributes}` strict JSON; detection resolves it to a bbox. Fail-closed: unparseable → ask, don't guess | Click-to-select UI (user clicks object, SAM segments from click point). Ship click-select *anyway* as the disambiguation path |
| 4 | **Before/after viewer** | Scrub original and edited video side by side, or slider overlay | Pure frontend: two synced `<video>` elements + range input clipping one. ~30 min, doubles demo impact of everything else | None needed — this cannot be blocked |
| 5 | **Provenance log** | Every edit recorded as a signed JSON diff | See §5. Cheap to build; it is the product's differentiator and the answer to "isn't this a deepfake tool?" | HMAC-sign with a server secret if no key infra; even unsigned structured logs beat nothing |
| 6 | **Replace** | Swap one object for another in a single operation | Remove + Add on the same mask. Nearly free once 1 and 7 exist | Cut if Add is cut |
| 7 | **Add** | Insert a new object matching the scene's lighting/perspective | Hardest feature. Pragmatic hackathon version: small library of pre-cut PNG/video sprites (cone, barrier, vehicle, person silhouette) composited with scale estimation from nearby detections + edge feathering. Generative insertion only if an image-gen model is reachable | Cut first among the big three. Replace degrades to Remove-only |
| 8 | **Object search** | Find every appearance of an object across indexed footage ("red SUV", "person in yellow vest") | Reuse VSS semantic search + Cosmos captions + YOLO class filters — this is largely already built in the workshop stack. Results = clips + timestamps + thumbnails; clicking a result loads it into the editor | Caption keyword search only |
| 9 | **Anonymize mode** | One click blurs/removes all faces and license plates in a clip | Face detection (YOLO11 face variant or OpenCV DNN) + plate detection → BLUR path for every hit, auto-propagated. Reuses the whole pipeline with zero new ML | Faces only (plates are harder); still demos well |
| 10 | **Edit history / undo** | List of applied edits per clip; revert to any version | Falls out of immutable originals + provenance log: each entry references input/output artifact IDs | Cut silently if out of time |

**Cut order if behind:** 7 (Add) → 6 (Replace) → 9 (Anonymize) → 8 (Search). **Never cut 1, 2, 4, 5** — Remove + tracking + before/after + provenance is a complete, honest product.

---

## 5. Provenance Log (the differentiator — do not skip)

Every edit operation appends one entry to a per-clip provenance file (JSON Lines). Entry schema:

```json
{
  "edit_id": "uuid",
  "timestamp": "2026-10-10T11:42:03Z",
  "operation": "remove | add | replace | blur",
  "command_text": "remove the person on the left",
  "target": {
    "object_class": "person",
    "resolution_method": "nl_detection | user_click",
    "anchor_frame": 42,
    "bbox": [x, y, w, h],
    "frames_affected": [12, 180]
  },
  "models_used": {
    "detector": "yolo11n",
    "segmenter": "sam2_hiera_small",
    "editor": "propainter | lama | composite",
    "nl_parser": "model-id-from-wandb"
  },
  "input_artifact": "sha256:…",
  "output_artifact": "sha256:…",
  "signature": "hmac-sha256 over the entry with server-side key"
}
```

Rules:
- Hash input and output files; chain entries (each entry includes previous entry's hash) so the log is tamper-evident, not just descriptive.
- Expose `GET /provenance/<clip_id>` and render it in the UI as a human-readable edit history.
- README and UI must state: *"Conjure never silently alters footage. Every output carries a verifiable record of what was changed."*

**Positioning note for README/demo:** the obvious critique of any object-removal tool is evidence tampering. Provenance flips the frame — Conjure is the *auditable* editor. Lead demos with a fun edit; close with the provenance log.

---

## 6. Ethical Guardrails (enforce in code, not just prose)

- Refuse edits whose explicit purpose is deception of authorities/courts if stated in the command (keyword check is fine at this scale; log the refusal).
- Anonymize mode is the sanctioned privacy feature; removal of faces for other purposes still gets logged like any edit.
- Watermark option: stretch goal — subtle "edited with Conjure" burn-in toggle, default ON for exports.
- Do not train on, redistribute, or ingest third-party internet footage.

---

## 7. App Shape

- **One Flask (or FastAPI) app, one process, vertical slices** — no separate frontend project. Inline HTML/JS template. Same deployment pattern as prior workshop apps (ConfigMap + Secret + Deployment + Service + Ingress `/app`, readiness on `/health`).
- **Endpoints:**
  - `GET /` — editor UI
  - `GET /health` — `{"status":"ok"}`
  - `POST /clips` — pick a clip (from VSS search result or uploaded demo file)
  - `POST /edit` — `{clip_id, command_text}` or `{clip_id, click_point, action}` → runs pipeline, returns `{edit_id, output_url, provenance_entry, candidates?}`
  - `GET /evidence/<artifact_id>` — proxied video bytes (Range supported)
  - `GET /provenance/<clip_id>` — edit history
  - `POST /search` — object search over indexed footage (proxies VSS)
  - `POST /anonymize` — `{clip_id}` → auto-detect + blur all faces/plates
- **UI layout:** video player center; command box below ("describe your edit"); click-to-select overlay on the video; right panel = edit history + provenance; before/after slider toggle; search tab.
- **Async:** edits take seconds-to-minutes. `POST /edit` returns a job id; poll `GET /jobs/<id>`; UI shows per-stage progress (detecting → masking → tracking → editing → rendering). Do not block HTTP requests on GPU work.

---

## 8. Six-Hour Build Order

| Hour | Milestone | Done when |
|------|-----------|-----------|
| 0–0.5 | **Preflight:** confirm GPU access; confirm which of SAM2/FastSAM, ProPainter/E2FGVI/LaMa, YOLO11/GroundingDINO actually load and run on one test frame; confirm VSS search + playback still work; pick the real model set and write it at the top of the repo README | One masked frame + one inpainted frame produced by hand |
| 0.5–1.5 | **Single-frame remove, end-to-end:** click a point → SAM mask → inpaint → result displayed | Hero screenshot exists; commit it |
| 1.5–3 | **Temporal propagation:** mask follows object, inpainting across a 5–10 s clip | Before/after video of a tracked removal; commit recording |
| 3–4 | **Natural-language targeting + disambiguation UI** | "remove the X" works; ambiguous commands show candidate picker |
| 4–4.75 | **Provenance log + before/after slider + edit history panel** | `GET /provenance` renders; slider works |
| 4.75–5.5 | **One of:** Replace (if Add sprites ready) OR Anonymize OR Search tab — pick whichever preflight showed is cheapest | One bonus feature demoed |
| 5.5–6 | **Harvest before teardown:** screen recordings of every working feature, screenshots, provenance JSON samples, README feature list trimmed to what actually works, final commit + push | Repo stands alone after infra dies |

**Hard rule:** commit demo evidence (recordings/screenshots) *incrementally* after each milestone, not at the end. The infrastructure disappearing is the known failure mode; the repo is the product.

**Known time sinks to pre-empt:**
- Video inpainting model weights are large; start the download during preflight, in the background.
- ffmpeg re-encode flags: use `-c:v libx264 -pix_fmt yuv420p -movflags +faststart` so browsers actually play outputs.
- SAM2 video mode wants frames extracted to disk; extract once per clip and cache.
- If temporal consistency looks bad, ship per-segment edits ("edit any moment") rather than burning hours chasing flicker-free output — say so honestly in the README.

---

## 9. README Honesty Rules

- Feature list in the README must match what actually ships. Trim aspirational features at the end of the build, not before.
- State explicitly: what footage was used, that originals are immutable, that outputs carry provenance, and which parts are hackathon-grade (e.g., "temporal blending is approximate").
- Include: architecture diagram, the provenance schema, one full provenance log example, and links to all demo recordings.
