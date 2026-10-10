"""Prepare a clip once (download, detect, track, clean plate), then run edits on it as jobs."""

import hashlib
import os
import shutil
import time

import cv2
import numpy as np

import commands
import editor
import models
import plate as plates
import provenance
import sprites
import store
import tracker
import video

SEGMENTER = "clean-plate difference mask inside YOLO box (CPU; SAM2 needs a GPU, none schedulable)"
TRACKER = "IoU tracker over per-frame YOLO11 boxes, gap interpolation + smoothing"
PREPARE_STAGES = ("downloading", "transcoding", "detecting", "tracking", "aligning", "clean plate", "sprites")
EDIT_STAGES = ("parsing", "detecting", "masking", "tracking", "editing", "rendering", "reviewing")


def clip_id_for(source):
    return hashlib.sha256(source.encode()).hexdigest()[:12]


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def prepare(job, source=None, upload_path=None, row=None):
    clip_id = job.clip_id
    folder = store.clip_dir(clip_id)
    os.makedirs(folder, exist_ok=True)
    existing = store.meta(clip_id)
    if existing and existing.get("status") == "ready":
        return {"clip_id": clip_id, "already": True}
    row = row or {}
    meta = {"clip_id": clip_id, "status": "preparing", "created": _now(), "source": source,
            "camera_id": row.get("camera_id"), "caption": (row.get("reasoning_content") or "")[:600],
            "segment": [row.get("segment_start_sec"), row.get("segment_end_sec")],
            "origin": "vss-archive" if source else "user-upload",
            "title": (row.get("original_video") or source or "upload").split("/")[-1][:80]}
    store.save_meta(clip_id, meta)

    job.enter("downloading")
    original = os.path.join(folder, "original.mp4")
    if source:
        if not os.path.exists(original):
            import vss

            vss.download(source, original)
    else:
        shutil.move(upload_path, original)
    meta["original_artifact"] = store.register_artifact(original)

    job.enter("transcoding")
    info = video.probe(original)
    work_w, work_h = video.work_size(info["width"], info["height"])
    v0 = os.path.join(folder, "v0.mp4")
    video.transcode(original, v0, work_w, work_h)
    frames = video.read_frames(v0, work_w, work_h)
    count = len(frames)
    meta.update(orig_w=info["width"], orig_h=info["height"], fps=info["fps"], work_w=work_w, work_h=work_h,
                n_frames=count, duration=round(count / info["fps"], 2))

    job.enter("detecting")
    raw, detector = None, None
    if source:
        try:
            import vss

            sidecar = vss.detections(source)
            if sidecar and len(sidecar.get("frames") or []) >= 0.8 * count:
                raw, detector = sidecar["frames"], "yolo11 (VSS detection sidecar)"
        except Exception:
            raw = None
    if raw is None:
        if not models.gpu_configured():
            raise RuntimeError("No detections available: YOLO endpoint not configured")
        raw, detector = models.yolo_frames(v0)["frames"], "yolo11 (hosted GPU endpoint)"
    dets = tracker.normalize_frames(raw, count, work_w, work_h)
    meta["detector"] = detector

    job.enter("tracking")
    tracks = tracker.build_tracks(dets, count, work_w, work_h)
    store.write_json(os.path.join(folder, "tracks.json"), tracks)
    meta["track_count"] = len(tracks)
    meta["labels"] = sorted({t["label"] for t in tracks})

    job.enter("aligning")
    affines, static, worst = plates.estimate_motion(frames, tracks)
    np.save(os.path.join(folder, "affines.npy"), np.array(affines))
    meta.update(camera_static=static, camera_motion_px=worst)

    job.enter("clean plate")
    plate, holes = plates.build_plate(frames, tracks, affines, static, progress=job.progress)
    cv2.imwrite(os.path.join(folder, "plate.png"), plate)
    np.save(os.path.join(folder, "holes.npy"), holes)
    meta["plate_hole_pct"] = round(float(holes.mean()) * 100, 2)

    job.enter("sprites")
    try:
        meta["sprites_harvested"] = len(sprites.harvest(clip_id, frames, tracks, plate))
        meta["sprites_v"] = sprites.HARVEST_VERSION
    except Exception as exc:
        meta["sprites_harvested"] = 0
        meta["sprites_error"] = type(exc).__name__
    video.thumbnail(v0, os.path.join(folder, "thumb.jpg"), at=min(1.0, meta["duration"] / 2))
    if not meta["caption"] and models.gpu_configured():
        meta["caption"] = models.cosmos_caption(v0)

    store.add_version(clip_id, v0, None, None, "ingest", "Original (720p working copy)", [])
    entry = provenance.append(clip_id, {
        "operation": "ingest",
        "command_text": None,
        "source": source or "user upload",
        "models_used": {"detector": detector, "tracker": TRACKER},
        "parameters": {"original_resolution": [info["width"], info["height"]],
                       "working_resolution": [work_w, work_h], "fps": info["fps"], "frames": count},
        "input_artifact": "sha256:" + meta["original_artifact"],
        "output_artifact": "sha256:" + store.head_version(clip_id)["artifact"],
        "note": "Original stored untouched; working copy transcoded to 720p H.264. Edits never overwrite either.",
    })
    meta["status"] = "ready"
    meta["ready_at"] = _now()
    store.save_meta(clip_id, meta)
    return {"clip_id": clip_id, "provenance_entry": entry}


def reharvest_stale():
    """Re-cut library sprites for clips prepared by an older harvester."""
    for meta in store.list_clips():
        if meta.get("status") != "ready" or meta.get("sprites_v") == sprites.HARVEST_VERSION:
            continue
        try:
            scene = load_scene(meta["clip_id"])
            meta["sprites_harvested"] = len(sprites.harvest(meta["clip_id"], scene.v0, scene.tracks, scene.plate))
            meta["sprites_v"] = sprites.HARVEST_VERSION
            store.save_meta(meta["clip_id"], meta)
            del scene
        except Exception as exc:
            print(f"reharvest failed for {meta['clip_id']}: {type(exc).__name__}", flush=True)


def load_scene(clip_id, frames_v0=None):
    folder = store.clip_dir(clip_id)
    meta = store.meta(clip_id)
    plate = cv2.imread(os.path.join(folder, "plate.png"))
    holes = np.load(os.path.join(folder, "holes.npy"))
    affines = list(np.load(os.path.join(folder, "affines.npy")))
    if frames_v0 is None:
        frames_v0 = video.read_frames(os.path.join(folder, "v0.mp4"), meta["work_w"], meta["work_h"])
    return editor.Scene(meta, store.tracks(clip_id), plate, holes, affines, frames_v0)


def _orig_bbox(meta, box):
    sx, sy = meta["orig_w"] / meta["work_w"], meta["orig_h"] / meta["work_h"]
    x1, y1, x2, y2 = box
    return [round(x1 * sx), round(y1 * sy), round((x2 - x1) * sx), round((y2 - y1) * sy)]


def _candidate_thumbs(clip_id, frames, candidates):
    folder = store.clip_dir(clip_id)
    for cand in candidates:
        x1, y1, x2, y2 = cand["bbox"]
        h, w = frames.shape[1:3]
        pad = int(0.25 * max(x2 - x1, y2 - y1)) + 8
        crop = frames[cand["frame"], max(0, y1 - pad):min(h, y2 + pad), max(0, x1 - pad):min(w, x2 + pad)].copy()
        cv2.rectangle(crop, (x1 - max(0, x1 - pad), y1 - max(0, y1 - pad)),
                      (x2 - max(0, x1 - pad), y2 - max(0, y1 - pad)), (60, 220, 255), 2)
        name = f"cand_{cand['track_id']}_{cand['frame']}.jpg"
        cv2.imwrite(os.path.join(folder, name), crop)
        cand["thumb_url"] = f"thumbs/{clip_id}/{name}"


def log_refusal(clip_id, command_text, reason):
    head = store.head_version(clip_id)
    return provenance.append(clip_id, {
        "operation": "refused",
        "command_text": command_text,
        "reason": reason,
        "input_artifact": "sha256:" + head["artifact"] if head else None,
        "output_artifact": None,
    })


def edit(job, req):
    clip_id = job.clip_id
    meta = store.meta(clip_id)
    head = store.head_version(clip_id)
    excluded = set(head.get("excluded_tracks") or [])
    tracks = store.tracks(clip_id)
    by_id = {t["id"]: t for t in tracks}
    fps, count = meta["fps"], meta["n_frames"]
    anchor = int(round(float(req.get("time") or 0) * fps)) if req.get("time") is not None else count // 2
    anchor = max(0, min(count - 1, anchor))
    text = (req.get("command_text") or "").strip()
    present = sorted({t["label"] for t in tracks if t["id"] not in excluded})

    job.enter("parsing")
    parser, method, intent = None, None, None
    picked = []
    if req.get("one_click") and req.get("action") == "anonymize":
        intent = {"action": "anonymize", "object_class": None, "quantity": "all"}
        method = "one_click_anonymize"
    elif req.get("track_ids"):
        picked = [by_id[i] for i in req["track_ids"] if i in by_id and i not in excluded]
        if not picked:
            raise ValueError("Selected object no longer exists in this version.")
        intent = dict(req.get("intent") or {})
        intent.setdefault("action", req.get("action") or "remove")
        intent["object_class"] = picked[0]["label"]
        if req.get("new_object"):
            intent["new_object"] = commands.canonical_class(req["new_object"])
        method = req.get("method") or "user_click"
        parser = req.get("parser")
    elif req.get("click_point") is not None:
        x, y = req["click_point"]
        px, py = float(x) * meta["work_w"], float(y) * meta["work_h"]
        action = req.get("action") or "remove"
        intent = {"action": action, "new_object": commands.canonical_class(req.get("new_object"))}
        method = "user_click"
        if action == "add":
            intent["position"] = [px, py]
            picked = []
        else:
            track = commands.track_at_point(tracks, anchor, px, py, excluded)
            if not track:
                return {"needs_input": True, "question": "No detected object at that point. Click on a person, "
                        "vehicle or other detected object (boxes show what Conjure can see)."}
            picked = [track]
            intent["object_class"] = track["label"]
    else:
        if not text:
            raise ValueError("Describe the edit, or click an object.")
        try:
            intent, parser = commands.parse(text, present)
        except commands.NeedsInput as need:
            return {"needs_input": True, "question": need.question, "intent": need.parsed}
        method = "nl_detection"
        picked = []

    action = intent["action"]
    if action in ("add", "replace") and intent.get("new_object") not in sprites.labels():
        return {"needs_input": True, "question": "What should I insert? Available: " + ", ".join(sprites.labels()),
                "intent": intent}

    job.enter("detecting")
    scene = load_scene(clip_id)
    if method == "nl_detection" and action in ("remove", "blur", "replace"):
        try:
            picked, anchor = commands.resolve(intent, tracks, scene.v0, anchor, excluded, meta["work_w"], meta["work_h"])
        except commands.NeedsInput as need:
            _candidate_thumbs(clip_id, scene.v0, need.candidates)
            return {"needs_input": True, "question": need.question, "candidates": need.candidates,
                    "intent": need.parsed or intent, "parser": parser, "anchor": anchor}

    job.enter("masking")
    head_path = os.path.join(store.clip_dir(clip_id), head["file"])
    frames = scene.v0.copy() if head["version"] == 0 else video.read_frames(head_path, meta["work_w"], meta["work_h"])
    job.progress(1.0)

    job.enter("tracking")
    affected = [min(t["first"] for t in picked), max(t["last"] for t in picked)] if picked else [0, count - 1]
    job.progress(1.0)

    job.enter("editing")
    details, new_excluded = [], set(excluded)
    if action == "anonymize":
        details.append(editor.anonymize(scene, frames))
        picked = [t for t in tracks if t["label"] == "person" or t["label"] in editor.VEHICLES]
    elif action == "add":
        item = next((i for i in sprites.index() if i["name"] == req.get("sprite")), None) \
            or sprites.pick(intent["new_object"], clip_id)
        if intent.get("position"):
            cx, bottom = intent["position"]
        else:
            cx, bottom = _place(scene, intent, anchor, excluded)
        details.append(editor.add_static(scene, frames, item, cx, bottom))
        details[-1]["sprite"] = item["name"]
    else:
        for i, track in enumerate(picked):
            if action == "remove":
                details.append(editor.remove(scene, frames, track))
                new_excluded.add(track["id"])
                new_excluded.update(details[-1]["carried_track_ids"])
            elif action == "blur":
                details.append(editor.blur(scene, frames, track))
            elif action == "replace":
                details.append(editor.remove(scene, frames, track))
                new_excluded.update(details[-1]["carried_track_ids"])
                boxes = np.array(track["boxes"], dtype=np.float64)
                aspect = float(np.median((boxes[:, 2] - boxes[:, 0]) / np.maximum(boxes[:, 3] - boxes[:, 1], 1)))
                item = sprites.pick(intent["new_object"], clip_id, aspect=aspect)
                details.append(editor.add_following(scene, frames, item, track))
                details[-1]["sprite"] = item["name"]
                new_excluded.add(track["id"])
            job.progress((i + 1) / len(picked))

    job.enter("rendering")
    use_mark = req.get("watermark", True)
    if use_mark:
        editor.watermark(frames, "Edited with Conjure")
    number = len(store.versions(clip_id)["items"])
    out = os.path.join(store.clip_dir(clip_id), f"v{number}.mp4")
    video.write_video(frames, fps, out)
    del frames

    description = _describe(action, intent, picked)
    job.enter("reviewing")
    qa = None
    if models.gpu_configured() and req.get("review", True):
        baseline = meta.get("qa_baseline")
        if baseline is None:
            first = models.cosmos_review(os.path.join(store.clip_dir(clip_id), "v0.mp4"), "unedited original")
            if first.get("ok") and first.get("score") is not None:
                baseline = first["score"]
                with store.clip_lock(clip_id):
                    fresh = store.meta(clip_id)
                    fresh["qa_baseline"] = baseline
                    store.save_meta(clip_id, fresh)
        qa = models.cosmos_review(out, description)
        if qa.get("ok") and qa.get("score") is not None and baseline is not None:
            qa["baseline_score"] = baseline
            qa["delta"] = round(qa["score"] - baseline, 2)
            qa["natural"] = qa["score"] >= baseline - 0.1

    target = None
    if picked:
        first = picked[0]
        box = tracker.box_at(first, anchor) or first["boxes"][0]
        target = {"object_class": "faces + plates" if action == "anonymize" else first["label"],
                  "resolution_method": method, "anchor_frame": anchor,
                  "bbox": _orig_bbox(meta, box), "frames_affected": affected,
                  "track_ids": [t["id"] for t in picked]}
    elif action == "add":
        target = {"object_class": intent["new_object"], "resolution_method": method, "anchor_frame": anchor,
                  "placed_at_original_px": [round(details[0]["placed_at"][0] * meta["orig_w"] / meta["work_w"]),
                                            round(details[0]["placed_at"][1] * meta["orig_h"] / meta["work_h"])],
                  "frames_affected": affected}
    editors = sorted({d["editor"] for d in details})
    entry_body = {
        "operation": action,
        "command_text": text or None,
        "description": description,
        "intent": intent,
        "target": target,
        "models_used": {
            "detector": meta.get("detector"),
            "segmenter": SEGMENTER,
            "tracker": TRACKER,
            "editor": " + ".join(editors),
            "nl_parser": parser,
            "reviewer": (qa or {}).get("model"),
        },
        "parameters": {"working_resolution": [meta["work_w"], meta["work_h"]],
                       "original_resolution": [meta["orig_w"], meta["orig_h"]],
                       "watermark": bool(use_mark), "details": details},
        "input_version": head["version"],
        "input_artifact": "sha256:" + head["artifact"],
        "qa": qa,
    }
    version = store.add_version(clip_id, out, head["version"], None, action, description, new_excluded,
                                extra={"qa": qa})
    entry_body["output_version"] = version["version"]
    entry_body["output_artifact"] = "sha256:" + version["artifact"]
    entry = provenance.append(clip_id, entry_body)
    with store.clip_lock(clip_id):
        data = store.versions(clip_id)
        for item in data["items"]:
            if item["version"] == version["version"]:
                item["edit_id"] = entry["edit_id"]
        store.save_versions(clip_id, data)
    return {"edit_id": entry["edit_id"], "version": version["version"],
            "output_url": f"evidence/{version['artifact']}", "provenance_entry": entry, "qa": qa}


def _place(scene, intent, anchor, excluded):
    hint = intent.get("position_hint") or "center"
    if hint.startswith("near "):
        label = hint[5:]
        near = commands.candidates_at(scene.tracks, label, anchor, excluded)
        if near:
            track, box = max(near, key=lambda tb: (tb[1][2] - tb[1][0]) * (tb[1][3] - tb[1][1]))
            bottom = box[3]
            width = scene.person_height(bottom) * 0.35
            cx = box[2] + width if box[2] + width < scene.width - 10 else box[0] - width
            return cx, bottom
        hint = "center"
    frac = {"left": 0.2, "right": 0.8}.get(hint, 0.5)
    return frac * scene.width, scene.ground_y(frac)


def _describe(action, intent, picked):
    label = " ".join([*(intent.get("attributes") or []), intent.get("object_class") or "object"])
    if action == "anonymize":
        return "anonymized faces and plates"
    if action == "add":
        return f"added {intent.get('new_object', 'object').replace('_', ' ')}"
    if action == "replace":
        return f"replaced {label} with {str(intent.get('new_object')).replace('_', ' ')}"
    many = f"{len(picked)} {label}s" if len(picked) > 1 else f"the {label}" + (
        f" ({intent['spatial_hint']})" if intent.get("spatial_hint") else "")
    return f"{'removed' if action == 'remove' else 'blurred'} {many}"


def revert(clip_id, version_number):
    with store.clip_lock(clip_id):
        data = store.versions(clip_id)
        current = store.head_version(clip_id)
        target = next((i for i in data["items"] if i["version"] == version_number), None)
        if target is None:
            raise ValueError("unknown version")
        data["head"] = version_number
        store.save_versions(clip_id, data)
    return provenance.append(clip_id, {
        "operation": "revert",
        "command_text": None,
        "description": f"head moved from v{current['version']} to v{version_number}",
        "input_version": current["version"],
        "output_version": version_number,
        "input_artifact": "sha256:" + current["artifact"],
        "output_artifact": "sha256:" + target["artifact"],
        "note": "No file was modified; every version stays on disk.",
    })
