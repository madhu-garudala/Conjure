"""Sprite library for Add and Replace.

Two kinds, both made at runtime (nothing binary ships in the ConfigMap):
- procedural props drawn with OpenCV (traffic cone, barrier, bollard)
- cutouts harvested from the indexed archive: real people and vehicles cut out of the footage
  with the clean-plate difference mask, so they share the camera's look and lighting.
"""

import os
import threading

import cv2
import numpy as np

import store
import tracker

LIB = os.path.join(store.DATA, "sprites")
os.makedirs(LIB, exist_ok=True)
_INDEX = os.path.join(LIB, "index.json")
_lock = threading.Lock()

REAL_HEIGHT_M = {
    "person": 1.7, "car": 1.5, "truck": 3.0, "bus": 3.1, "bicycle": 1.05, "motorcycle": 1.2,
    "traffic_cone": 0.75, "barrier": 1.0, "bollard": 0.95, "dog": 0.55,
}
PROPS = ("traffic_cone", "barrier", "bollard")
HARVEST = ("person", "car", "truck", "bus", "bicycle")
HARVEST_VERSION = 4
ASPECT = {"person": (0.22, 0.7), "bicycle": (0.8, 2.6), "car": (0.8, 3.4), "truck": (0.7, 3.8), "bus": (0.8, 5.0)}


def _shade(img, alpha):
    h, w = alpha.shape
    ramp = np.linspace(1.12, 0.78, w, dtype=np.float32)[None, :, None]
    out = np.clip(img.astype(np.float32) * ramp, 0, 255).astype(np.uint8)
    return out


def _cone():
    w, h = 220, 320
    img = np.zeros((h, w, 3), np.uint8)
    alpha = np.zeros((h, w), np.uint8)
    base = np.array([[8, h - 34], [w - 8, h - 34], [w - 8, h - 6], [8, h - 6]], np.int32)
    body = np.array([[w // 2 - 14, 6], [w // 2 + 14, 6], [w - 38, h - 34], [38, h - 34]], np.int32)
    cv2.fillPoly(img, [base], (0, 80, 230))
    cv2.fillPoly(img, [body], (10, 105, 255))
    cv2.fillPoly(alpha, [base, body], 255)
    for top, bottom in ((0.30, 0.42), (0.58, 0.70)):
        y0, y1 = int(6 + top * (h - 40)), int(6 + bottom * (h - 40))
        band = np.zeros_like(alpha)
        cv2.rectangle(band, (0, y0), (w, y1), 255, -1)
        img[(band > 0) & (alpha > 0)] = (235, 235, 235)
    return _shade(img, alpha), alpha


def _barrier():
    w, h = 420, 230
    img = np.zeros((h, w, 3), np.uint8)
    alpha = np.zeros((h, w), np.uint8)
    for x in (40, w - 64):
        cv2.rectangle(img, (x, 40), (x + 24, h - 8), (60, 60, 60), -1)
        cv2.rectangle(alpha, (x, 40), (x + 24, h - 8), 255, -1)
        cv2.rectangle(img, (x - 18, h - 20), (x + 42, h - 4), (40, 40, 40), -1)
        cv2.rectangle(alpha, (x - 18, h - 20), (x + 42, h - 4), 255, -1)
    plank = np.zeros_like(alpha)
    cv2.rectangle(plank, (8, 30), (w - 8, 100), 255, -1)
    stripes = np.zeros((h, w, 3), np.uint8)
    stripes[:] = (240, 240, 240)
    for x in range(-h, w + h, 56):
        cv2.fillPoly(stripes, [np.array([[x, 30], [x + 28, 30], [x + 98, 100], [x + 70, 100]], np.int32)], (0, 90, 245))
    img[plank > 0] = stripes[plank > 0]
    alpha |= plank
    return _shade(img, alpha), alpha


def _bollard():
    w, h = 90, 300
    img = np.zeros((h, w, 3), np.uint8)
    alpha = np.zeros((h, w), np.uint8)
    cv2.rectangle(img, (14, 24), (w - 14, h - 6), (20, 200, 240), -1)
    cv2.ellipse(img, (w // 2, 24), (w // 2 - 14, 16), 0, 180, 360, (20, 200, 240), -1)
    cv2.rectangle(alpha, (14, 24), (w - 14, h - 6), 255, -1)
    cv2.ellipse(alpha, (w // 2, 24), (w // 2 - 14, 16), 0, 180, 360, 255, -1)
    for y in (70, 110):
        cv2.rectangle(img, (14, y), (w - 14, y + 16), (230, 230, 230), -1)
    return _shade(img, alpha), alpha


def _write(name, bgr, alpha):
    rgba = np.dstack([bgr, cv2.GaussianBlur(alpha, (3, 3), 0)])
    cv2.imwrite(os.path.join(LIB, f"{name}.png"), rgba)


def index():
    return store.read_json(_INDEX, [])


def ensure_props():
    with _lock:
        items = index()
        names = {i["name"] for i in items}
        for prop, draw in (("traffic_cone", _cone), ("barrier", _barrier), ("bollard", _bollard)):
            if prop not in names or not os.path.exists(os.path.join(LIB, f"{prop}.png")):
                bgr, alpha = draw()
                _write(prop, bgr, alpha)
                items = [i for i in items if i["name"] != prop]
                items.append({"name": prop, "label": prop, "kind": "procedural", "facing": None, "source_clip": None})
        store.write_json(_INDEX, items)


def load(name):
    path = os.path.join(LIB, f"{os.path.basename(name)}.png")
    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if img is None or img.shape[2] != 4:
        return None
    return img


def labels():
    return sorted({i["label"] for i in index()})


def pick(label, clip_id=None, aspect=None):
    """Best sprite for a label: same clip first (matching light), then closest width/height ratio."""
    items = [i for i in index() if i["label"] == label]
    if not items:
        return None

    def key(item):
        ratio = item.get("aspect") or 1.0
        mismatch = abs(np.log(ratio / aspect)) if aspect else 0.0
        return (item.get("source_clip") != clip_id, round(mismatch, 1), -(item.get("height") or 0))

    return sorted(items, key=key)[0]


def harvest(clip_id, frames, tracks, plate, max_per_label=2):
    """Cut clean, isolated objects out of this clip and add them to the library."""
    count, height, width, _ = frames.shape
    found = []
    by_label = {}
    for track in tracks:
        if track["label"] in HARVEST and track["observed"] >= 10:
            by_label.setdefault(track["label"], []).append(track)
    for label, group in by_label.items():
        group.sort(key=lambda t: -t["mean_area"])
        taken = 0
        for track in group[:8]:
            cut = _best_cutout(track, frames, tracks, plate, width, height)
            if cut is None:
                continue
            rgba, facing = cut
            name = f"{label}_{clip_id[:6]}_{track['id']}"
            cv2.imwrite(os.path.join(LIB, f"{name}.png"), rgba)
            found.append({"name": name, "label": label, "kind": "cutout", "facing": facing,
                          "source_clip": clip_id, "source_track": track["id"], "height": int(rgba.shape[0]),
                          "aspect": round(rgba.shape[1] / rgba.shape[0], 3), "v": HARVEST_VERSION})
            taken += 1
            if taken >= max_per_label:
                break
    with _lock:
        items = [i for i in index() if i.get("source_clip") != clip_id] + found
        store.write_json(_INDEX, items)
    return found


def _best_cutout(track, frames, tracks, plate, width, height):
    best, best_score = None, 0.0
    for offset in range(0, len(track["boxes"]), 3):
        index = track["first"] + offset
        x1, y1, x2, y2 = track["boxes"][offset]
        bw, bh = x2 - x1, y2 - y1
        if bh < 70 or x1 <= 2 or y1 <= 2 or x2 >= width - 3 or y2 >= height - 3:
            continue
        low, high = ASPECT.get(track["label"], (0.2, 5.0))
        if not low <= bw / bh <= high:
            continue
        is_vehicle = tracker.family(track["label"]) == "vehicle"
        crowded = False
        for other in tracks:
            if other["id"] == track["id"] or not other["dynamic"]:
                continue
            other_vehicle = tracker.family(other["label"]) == "vehicle"
            # Vehicle detections drop out for a few frames; treat them as present 20 frames past their ends.
            slack = 20 if other_vehicle else 0
            if index < other["first"] - slack or index > other["last"] + slack:
                continue
            box = tracker.box_at(other, min(max(index, other["first"]), other["last"]))
            if not box or tracker.iou(box, [x1 - 6, y1 - 6, x2 + 6, y2 + 6]) <= 0.0:
                continue
            behind = is_vehicle and not other_vehicle and box[3] < y2 - 0.15 * bh
            if not behind:
                crowded = True
                break
        if crowded:
            continue
        m = 10
        ex1, ey1, ex2, ey2 = max(0, x1 - m), max(0, y1 - m), min(width, x2 + m), min(height, y2 + m)
        wide = np.abs(frames[index, ey1:ey2, ex1:ex2].astype(np.int16)
                      - plate[ey1:ey2, ex1:ex2].astype(np.int16)).max(axis=2) > 28
        ring = np.ones_like(wide)
        ring[y1 - ey1:y2 - ey1, x1 - ex1:x2 - ex1] = False
        sides = [wide[:, :x1 - ex1][ring[:, :x1 - ex1]], wide[:, x2 - ex1:][ring[:, x2 - ex1:]],
                 wide[:y1 - ey1][ring[:y1 - ey1]]]
        if any(s.size and s.mean() > 0.35 for s in sides):
            continue
        frame = frames[index, y1:y2, x1:x2]
        diff = np.abs(frame.astype(np.int16) - plate[y1:y2, x1:x2].astype(np.int16)).max(axis=2)
        mask = (diff > 28).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        if n < 2:
            continue
        largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        mask = np.where(labels == largest, 255, 0).astype(np.uint8)
        flood = cv2.copyMakeBorder(mask, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
        cv2.floodFill(flood, np.zeros((bh + 4, bw + 4), np.uint8), (0, 0), 255)
        mask |= cv2.bitwise_not(flood[1:-1, 1:-1])
        fill = mask.mean() / 255.0
        if not 0.3 < fill < (0.98 if is_vehicle else 0.92):
            continue
        score = fill * bh
        if score > best_score:
            vx = 0
            if offset + 3 < len(track["boxes"]):
                vx = track["boxes"][offset + 3][0] - x1
            facing = "right" if vx > 1 else "left" if vx < -1 else None
            alpha = cv2.GaussianBlur(mask, (5, 5), 0)
            best, best_score = (np.dstack([frame, alpha]), facing), score
    return best
