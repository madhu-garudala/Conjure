"""Conjure: plain-English video object editing with a signed provenance log."""

import os
import subprocess
import threading
import uuid
from urllib.parse import urlencode

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

import commands
import jobs
import models
import pipeline
import provenance
import sprites
import store
import tracing
import video
import vss

HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024
SEARCH_THUMBS = os.path.join(store.DATA, "search_thumbs")
os.makedirs(SEARCH_THUMBS, exist_ok=True)
_thumb_gate = threading.Semaphore(4)
_rows = {}
SEED_QUERIES = (("pedestrians crossing the street at a crosswalk", "nyc_streets_cam-1"),
                ("a bus or truck driving through the intersection", "nyc_streets_cam-2"))


@app.get("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.get("/static/<path:name>")
def static_file(name):
    if name not in ("app.js", "app.css"):
        abort(404)
    return send_from_directory(HERE, name)


@app.get("/health")
def health():
    names = ("VSS_URL", "VSS_USERNAME", "VSS_PASSWORD", "GPU_BEARER_TOKEN", "COSMOS3_REASON_URL", "YOLO_URL",
             "WANDB_API_KEY", "WANDB_TEAM", "WANDB_PROJECT", "CONJURE_SIGNING_KEY")
    return jsonify({
        "status": "ok",
        "secrets_present": {n: bool(os.environ.get(n)) for n in names},
        "clips": len(store.list_clips()),
        "active_jobs": len(jobs.active()),
        "weave": tracing.weave_enabled(),
        "signing": provenance.key_info(),
    })


@app.get("/clips")
def clips_list():
    out = []
    for meta in store.list_clips():
        head = store.head_version(meta["clip_id"]) if meta.get("status") == "ready" else None
        out.append({**_meta_view(meta), "head": head["version"] if head else None})
    return jsonify({"clips": out, "jobs": jobs.active()})


def _meta_view(meta):
    keys = ("clip_id", "status", "title", "camera_id", "caption", "origin", "duration", "fps", "n_frames",
            "work_w", "work_h", "orig_w", "orig_h", "labels", "detector", "camera_static", "created", "segment")
    view = {k: meta.get(k) for k in keys}
    view["thumb_url"] = f"thumbs/{meta['clip_id']}/thumb.jpg"
    return view


@app.post("/clips")
def clips_create():
    if request.files.get("file"):
        upload = request.files["file"]
        tmp = os.path.join(store.DATA, f"upload_{uuid.uuid4().hex}.mp4")
        upload.save(tmp)
        clip_id = store.sha256_file(tmp)[:12]
        if (store.meta(clip_id) or {}).get("status") == "ready":
            os.remove(tmp)
            return jsonify({"clip_id": clip_id, "status": "ready"})
        job = jobs.submit("prepare", clip_id, pipeline.PREPARE_STAGES, pipeline.prepare, None, tmp, None)
        return jsonify({"clip_id": clip_id, "job_id": job.id, "status": "preparing"}), 202
    body = request.get_json(force=True, silent=True) or {}
    source = str(body.get("source") or "")
    if not source.startswith("s3://"):
        return jsonify({"error": "source must be an indexed clip URI from search"}), 400
    return jsonify(_prepare_source(source, body.get("row") or _rows.get(source))), 202


def _prepare_source(source, row=None):
    clip_id = pipeline.clip_id_for(source)
    meta = store.meta(clip_id)
    if meta and meta.get("status") == "ready":
        return {"clip_id": clip_id, "status": "ready"}
    running = [j for j in jobs.active(clip_id) if j["kind"] == "prepare"]
    if running:
        return {"clip_id": clip_id, "job_id": running[0]["job_id"], "status": "preparing"}
    safe_row = {k: (row or {}).get(k) for k in ("camera_id", "reasoning_content", "segment_start_sec",
                                                 "segment_end_sec", "original_video")}
    job = jobs.submit("prepare", clip_id, pipeline.PREPARE_STAGES, pipeline.prepare, source, None, safe_row)
    return {"clip_id": clip_id, "job_id": job.id, "status": "preparing"}


@app.get("/clips/<clip_id>")
def clip_detail(clip_id):
    meta = store.meta(clip_id)
    if not meta:
        abort(404)
    data = store.versions(clip_id)
    original = next((i for i in data["items"] if i["version"] == 0), None)
    items = [{**i, "url": f"evidence/{i['artifact']}"} for i in data["items"]]
    return jsonify({
        "meta": _meta_view(meta), "versions": items, "head": data["head"],
        "original_url": f"evidence/{original['artifact']}" if original else None,
        "tracks": [{k: t[k] for k in ("id", "label", "first", "last", "boxes", "moving")} for t in store.tracks(clip_id)],
        "excluded_tracks": (store.head_version(clip_id) or {}).get("excluded_tracks", []),
        "jobs": jobs.active(clip_id),
    })


@app.post("/clips/<clip_id>/revert")
def clip_revert(clip_id):
    body = request.get_json(force=True, silent=True) or {}
    try:
        entry = pipeline.revert(clip_id, int(body.get("version")))
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "provenance_entry": entry})


@app.post("/edit")
def edit():
    body = request.get_json(force=True, silent=True) or {}
    clip_id = body.get("clip_id")
    meta = store.meta(clip_id) if clip_id else None
    if not meta or meta.get("status") != "ready":
        return jsonify({"error": "clip not ready"}), 400
    text = (body.get("command_text") or "").strip()
    reason = commands.guardrail(text)
    if reason:
        entry = pipeline.log_refusal(clip_id, text, reason)
        return jsonify({"refused": True, "reason": reason, "provenance_entry": entry}), 403
    job = jobs.submit("edit", clip_id, pipeline.EDIT_STAGES, pipeline.edit, body)
    return jsonify({"job_id": job.id, "status": job.status}), 202


@app.post("/anonymize")
def anonymize():
    body = request.get_json(force=True, silent=True) or {}
    clip_id = body.get("clip_id")
    if not (store.meta(clip_id) or {}).get("status") == "ready":
        return jsonify({"error": "clip not ready"}), 400
    req = {"clip_id": clip_id, "action": "anonymize", "one_click": True, "watermark": body.get("watermark", True)}
    job = jobs.submit("edit", clip_id, pipeline.EDIT_STAGES, pipeline.edit, req)
    return jsonify({"job_id": job.id, "status": job.status}), 202


@app.get("/jobs/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        abort(404)
    return jsonify(job.view())


@app.get("/evidence/<artifact_id>")
def evidence(artifact_id):
    path = store.artifact_path(artifact_id)
    if not path or not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="video/mp4", conditional=True, max_age=3600)


@app.get("/thumbs/<clip_id>/<name>")
def thumbs(clip_id, name):
    if not name.endswith(".jpg") or "/" in name or ".." in name:
        abort(404)
    return send_from_directory(store.clip_dir(clip_id), name, max_age=600)


@app.get("/provenance/<clip_id>")
def provenance_view(clip_id):
    if not store.meta(clip_id):
        abort(404)
    return jsonify({"entries": provenance.entries(clip_id), "verification": provenance.verify(clip_id),
                    "statement": "Conjure never silently alters footage. Every output carries a verifiable "
                                 "record of what was changed."})


@app.get("/provenance/<clip_id>.jsonl")
def provenance_raw(clip_id):
    path = os.path.join(store.clip_dir(clip_id), "provenance.jsonl")
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="application/x-ndjson", as_attachment=True,
                     download_name=f"conjure_provenance_{clip_id}.jsonl")


@app.post("/search")
def search():
    body = request.get_json(force=True, silent=True) or {}
    query = str(body.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query required"}), 400
    camera = body.get("camera_id") or None
    wanted = commands.canonical_class(body.get("object_class")) if body.get("object_class") else None
    try:
        rows = tracing.call("vss.search", vss.search, query, 40 if wanted else 24, camera, label=query[:80])
    except Exception as exc:
        return jsonify({"error": f"search failed ({type(exc).__name__})", "results": []}), 502
    results = []
    for row in rows:
        source = row.get("source")
        if not source:
            continue
        classes = row.get("object_classes") or []
        if wanted and wanted not in classes:
            continue
        _rows[source] = row
        clip_id = pipeline.clip_id_for(source)
        results.append({
            "source": source, "clip_id": clip_id, "camera_id": row.get("camera_id"),
            "caption": (row.get("reasoning_content") or "")[:400],
            "segment": [row.get("segment_start_sec"), row.get("segment_end_sec")],
            "similarity": row.get("similarity_score"), "object_classes": classes,
            "object_counts": row.get("object_counts") or {},
            "thumb_url": f"search_thumb/{clip_id}.jpg?{urlencode({'source': source})}",
            "loaded": (store.meta(clip_id) or {}).get("status") == "ready",
        })
    return jsonify({"results": results[:24], "query": query, "filter": wanted})


@app.get("/search_thumb/<clip_id>.jpg")
def search_thumb(clip_id):
    source = request.args.get("source", "")
    if not source.startswith("s3://") or pipeline.clip_id_for(source) != clip_id:
        abort(404)
    out = os.path.join(SEARCH_THUMBS, f"{clip_id}.jpg")
    if not os.path.exists(out):
        with _thumb_gate:
            if not os.path.exists(out):
                try:
                    subprocess.run(
                        [video.ffmpeg_bin(), "-v", "quiet", "-y", "-ss", "1.0", "-i", vss.stream_url(source),
                         "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", out],
                        capture_output=True, timeout=60,
                    )
                except Exception:
                    pass
    if not os.path.exists(out):
        abort(404)
    return send_file(out, mimetype="image/jpeg", max_age=3600)


@app.get("/sprites")
def sprite_list():
    items = sprites.index()
    return jsonify({"sprites": [{**i, "url": f"sprite/{i['name']}.png"} for i in items], "labels": sprites.labels()})


@app.get("/sprite/<name>.png")
def sprite_png(name):
    return send_from_directory(sprites.LIB, f"{os.path.basename(name)}.png", max_age=600)


@app.get("/calls")
def calls():
    return jsonify(tracing.recent())


def _seed():
    if store.list_clips() or not vss.configured():
        return
    for query, camera in SEED_QUERIES:
        try:
            rows = tracing.call("vss.search", vss.search, query, 5, camera, label=query)
        except Exception:
            continue
        for row in rows[:1]:
            if row.get("source"):
                _rows[row["source"]] = row
                _prepare_source(row["source"], row)


def startup():
    store.load_all()
    sprites.ensure_props()
    for meta in store.list_clips():
        if meta.get("status") != "ready" and meta.get("source"):
            _prepare_source(meta["source"], {"camera_id": meta.get("camera_id"), "reasoning_content": meta.get("caption")})
    threading.Thread(target=_seed, daemon=True).start()
    threading.Thread(target=pipeline.reharvest_stale, daemon=True).start()
    threading.Thread(target=models.cosmos_model_id, daemon=True).start()


startup()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
