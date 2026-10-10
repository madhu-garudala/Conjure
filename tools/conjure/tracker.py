"""Per-frame YOLO boxes -> object tracks (greedy IoU matching with motion prediction)."""

import numpy as np

FAMILY = {"car": "vehicle", "truck": "vehicle", "bus": "vehicle", "motorcycle": "vehicle"}
DYNAMIC = {
    "person", "bicycle", "car", "motorcycle", "bus", "truck", "dog", "horse", "handbag", "backpack",
    "umbrella", "suitcase", "skateboard", "cat", "bird", "sports ball", "train",
}
MIN_CONF = 0.3


def family(label):
    return FAMILY.get(label, label)


def iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0:
        return 0.0
    area = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / area if area > 0 else 0.0


def normalize_frames(raw_frames, n_frames, work_w, work_h):
    """YOLO frames (any resolution) -> {frame_index: [{label, conf, bbox(work px)}]}."""
    out = {}
    for frame in raw_frames or []:
        index = int(frame.get("frame_index", 0))
        if index >= n_frames:
            continue
        shape = frame.get("shape") or [work_h, work_w]
        sy, sx = work_h / float(shape[0]), work_w / float(shape[1])
        dets = []
        for det in frame.get("detections") or []:
            conf = float(det.get("confidence", 0))
            box = det.get("bbox") or []
            if conf < MIN_CONF or len(box) != 4:
                continue
            x1, y1, x2, y2 = box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy
            if x2 - x1 < 3 or y2 - y1 < 3:
                continue
            dets.append({"label": det.get("label", "object"), "conf": conf, "bbox": [x1, y1, x2, y2]})
        out[index] = dets
    return out


def _predict(track, frame):
    box = track["last_box"]
    gap = frame - track["last"]
    vx, vy = track["vel"]
    return [box[0] + vx * gap, box[1] + vy * gap, box[2] + vx * gap, box[3] + vy * gap]


def build_tracks(dets_by_frame, n_frames, work_w, work_h, max_gap=12, min_len=6):
    tracks, next_id = [], 1
    for frame in range(n_frames):
        dets = dets_by_frame.get(frame, [])
        active = [t for t in tracks if frame - t["last"] <= max_gap]
        pairs = []
        for ti, track in enumerate(active):
            predicted = _predict(track, frame)
            for di, det in enumerate(dets):
                if family(det["label"]) != track["family"]:
                    continue
                score = iou(predicted, det["bbox"])
                if score >= 0.25:
                    pairs.append((score, ti, di))
        pairs.sort(reverse=True)
        used_t, used_d = set(), set()
        for _, ti, di in pairs:
            if ti in used_t or di in used_d:
                continue
            used_t.add(ti)
            used_d.add(di)
            track, det = active[ti], dets[di]
            gap = frame - track["last"]
            old = track["last_box"]
            vx = ((det["bbox"][0] + det["bbox"][2]) - (old[0] + old[2])) / 2 / gap
            vy = ((det["bbox"][1] + det["bbox"][3]) - (old[1] + old[3])) / 2 / gap
            track["vel"] = (0.6 * track["vel"][0] + 0.4 * vx, 0.6 * track["vel"][1] + 0.4 * vy)
            track["obs"][frame] = det["bbox"]
            track["labels"].append(det["label"])
            track["confs"].append(det["conf"])
            track["last"], track["last_box"] = frame, det["bbox"]
        for di, det in enumerate(dets):
            if di in used_d:
                continue
            tracks.append({
                "id": next_id, "family": family(det["label"]), "obs": {frame: det["bbox"]},
                "labels": [det["label"]], "confs": [det["conf"]], "last": frame,
                "last_box": det["bbox"], "vel": (0.0, 0.0),
            })
            next_id += 1
    return [_finish(t, work_w, work_h) for t in tracks if len(t["obs"]) >= min_len]


def _finish(track, work_w, work_h):
    frames = sorted(track["obs"])
    first, last = frames[0], frames[-1]
    known = np.array([track["obs"][f] for f in frames], dtype=np.float64)
    span = np.arange(first, last + 1)
    boxes = np.stack([np.interp(span, frames, known[:, k]) for k in range(4)], axis=1)
    if len(span) >= 5:
        kernel = np.ones(5) / 5
        padded = np.pad(boxes, ((2, 2), (0, 0)), mode="edge")
        boxes = np.stack([np.convolve(padded[:, k], kernel, mode="valid") for k in range(4)], axis=1)
    boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, work_w - 1)
    boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, work_h - 1)
    labels = track["labels"]
    label = max(set(labels), key=labels.count)
    centers = (boxes[:, :2] + boxes[:, 2:]) / 2
    travel = float(np.linalg.norm(centers.max(0) - centers.min(0)))
    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    return {
        "id": track["id"],
        "label": label,
        "first": int(first),
        "last": int(last),
        "observed": len(frames),
        "conf": round(float(np.mean(track["confs"])), 3),
        "moving": travel > 0.02 * work_w,
        "dynamic": label in DYNAMIC,
        "mean_area": round(float(areas.mean()), 1),
        "boxes": [[int(round(v)) for v in box] for box in boxes],
    }


def box_at(track, frame):
    if frame < track["first"] or frame > track["last"]:
        return None
    return track["boxes"][frame - track["first"]]
