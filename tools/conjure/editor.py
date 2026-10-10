"""Edit operations on decoded frames. Every op follows a track through the whole clip."""

import cv2
import numpy as np

import plate as plates
import sprites
import tracker

VEHICLES = ("car", "truck", "bus", "motorcycle")


class Scene:
    """Everything an edit needs about one clip, loaded from the prepare step."""

    def __init__(self, meta, tracks, plate, holes, affines, frames_v0):
        self.meta = meta
        self.tracks = tracks
        self.by_id = {t["id"]: t for t in tracks}
        self.plate = plate
        self.holes = holes
        self.affines = affines
        self.static = meta.get("camera_static", True)
        self.v0 = frames_v0
        self.height, self.width = plate.shape[:2]
        self._person_fit = None

    def plate_crop(self, plate, frame, x, y, w, h):
        return plates.plate_view(plate, self.affines[frame], self.static, x, y, w, h)

    def person_height(self, y):
        """Expected person height in px for feet at row y, fitted from detected people (perspective)."""
        if self._person_fit is None:
            ys, hs = [], []
            for track in self.tracks:
                if track["label"] != "person":
                    continue
                for box in track["boxes"][::5]:
                    if box[3] < self.height - 3 and box[1] > 2:
                        ys.append(box[3])
                        hs.append(box[3] - box[1])
            fit = None
            if len(ys) >= 8:
                slope, intercept = np.polyfit(ys, hs, 1)
                if slope > 0:
                    fit = (float(slope), float(intercept))
            self._person_fit = fit or (0.45, -0.12 * self.height)
        slope, intercept = self._person_fit
        return float(np.clip(slope * y + intercept, 0.04 * self.height, 0.9 * self.height))

    def ground_y(self, x_frac):
        feet = []
        for track in self.tracks:
            if track["label"] == "person":
                for box in track["boxes"][::5]:
                    cx = (box[0] + box[2]) / 2 / self.width
                    if abs(cx - x_frac) < 0.2 and box[3] < self.height - 3:
                        feet.append(box[3])
        y = float(np.percentile(feet, 90)) if len(feet) >= 4 else 0.8 * self.height
        return float(np.clip(y, 0.62 * self.height, 0.9 * self.height))


def _pad(box, width, height, frac=0.12, px=6, bottom=0.10):
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    return (int(max(0, x1 - px - frac * bw)), int(max(0, y1 - px - frac * bh)),
            int(min(width, x2 + px + frac * bw)), int(min(height, y2 + px + (frac + bottom) * bh)))


def _feather(shape, rect, radius):
    alpha = np.zeros(shape, np.float32)
    x1, y1, x2, y2 = rect
    alpha[y1:y2, x1:x2] = 1.0
    k = max(3, int(radius) | 1)
    return cv2.GaussianBlur(alpha, (k, k), radius / 2.5)


def _in_front_mask(scene, frame_index, frame, plate_crop, ox, oy, w, h, ref_bottom, exclude, target_box=None):
    """Foreground pixels of other moving objects that must survive the edit.

    Objects closer to the camera (feet lower than ref_bottom) keep all their pixels. Objects clearly
    behind the target are hidden by it inside its box, so they keep only pixels outside that box.
    Objects beside the target keep the pixels nearer their own box centre than the target's, so a
    companion walking next to a removed person is not erased with them.
    """
    protect = np.zeros((h, w), np.float32)
    for other in scene.tracks:
        if other["id"] in exclude or not other["dynamic"]:
            continue
        box = tracker.box_at(other, frame_index)
        if not box:
            continue
        in_front = box[3] > ref_bottom + 2
        if not in_front and target_box is None:
            continue
        x1, y1 = max(box[0] - ox, 0), max(box[1] - oy, 0)
        x2, y2 = min(box[2] - ox, w), min(box[3] - oy, h)
        if x2 <= x1 or y2 <= y1:
            continue
        diff = np.abs(frame[y1:y2, x1:x2].astype(np.int16) - plate_crop[y1:y2, x1:x2].astype(np.int16)).max(axis=2)
        fg = (diff > 30).astype(np.uint8)
        fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        if not in_front and box[3] < ref_bottom - 0.08 * max(1, target_box[3] - target_box[1]):
            px, py = 0.06 * (target_box[2] - target_box[0]), 0.06 * (target_box[3] - target_box[1])
            tx1, ty1 = max(int(target_box[0] - px) - ox - x1, 0), max(int(target_box[1] - py) - oy - y1, 0)
            tx2, ty2 = max(int(target_box[2] + px) - ox - x1, 0), max(int(target_box[3] + py) - oy - y1, 0)
            fg[ty1:ty2, tx1:tx2] = 0
        elif not in_front:
            xs = np.arange(x1, x2, dtype=np.float32)[None, :] + ox
            ocx, ow = (box[0] + box[2]) / 2, max(1.0, box[2] - box[0])
            tcx, tw = (target_box[0] + target_box[2]) / 2, max(1.0, target_box[2] - target_box[0])
            mine = np.abs(xs - ocx) / ow < np.abs(xs - tcx) / tw
            fg = fg * np.repeat(mine, y2 - y1, axis=0).astype(np.uint8)
        fg = cv2.dilate(fg, np.ones((5, 5), np.uint8))
        protect[y1:y2, x1:x2] = np.maximum(protect[y1:y2, x1:x2], fg.astype(np.float32))
    if protect.any():
        protect = cv2.GaussianBlur(protect, (5, 5), 1.5)
    return protect


CARRIED = ("handbag", "backpack", "umbrella", "suitcase")


def attached_items(scene, track):
    """Bags/umbrellas carried by a person: mostly inside the person's box whenever both are visible."""
    if track["label"] != "person":
        return []
    out = []
    for other in scene.tracks:
        if other["label"] not in CARRIED or other["id"] == track["id"]:
            continue
        inside = total = 0
        for offset, box in enumerate(other["boxes"]):
            outer = tracker.box_at(track, other["first"] + offset)
            if not outer:
                continue
            total += 1
            ix = max(0, min(box[2], outer[2]) - max(box[0], outer[0]))
            iy = max(0, min(box[3], outer[3]) - max(box[1], outer[1]))
            area = max(1, (box[2] - box[0]) * (box[3] - box[1]))
            inside += (ix * iy) / area > 0.6
        if total >= 0.5 * len(other["boxes"]) and inside >= 0.7 * max(total, 1):
            out.append(other)
    return out


def remove(scene, frames, track):
    """Replace the track's pixels (and anything it carries) with the clean plate in every frame."""
    items = attached_items(scene, track)
    exclude = {track["id"], *(i["id"] for i in items)}
    info = _remove_one(scene, frames, track, exclude)
    for item in items:
        _remove_one(scene, frames, item, exclude)
    info["carried_items_removed"] = [f"{i['label']} #{i['id']}" for i in items]
    info["carried_track_ids"] = [i["id"] for i in items]
    return info


def _remove_one(scene, frames, track, exclude):
    filled, info = plates.fill_for_target(scene.plate, scene.holes, scene.v0, track, scene.affines, scene.static)
    for offset, box in enumerate(track["boxes"]):
        index = track["first"] + offset
        if index >= len(frames):
            break
        rect = _pad(box, scene.width, scene.height)
        bw, bh = rect[2] - rect[0], rect[3] - rect[1]
        margin = int(max(6, 0.1 * min(bw, bh)))
        reach = margin + int(0.12 * max(bw, bh))
        ox, oy = max(0, rect[0] - reach), max(0, rect[1] - reach)
        ex, ey = min(scene.width, rect[2] + reach), min(scene.height, rect[3] + reach)
        w, h = ex - ox, ey - oy
        crop = frames[index, oy:ey, ox:ex]
        plate_crop = scene.plate_crop(filled, index, ox, oy, w, h).astype(np.float32)
        alpha = _feather((h, w), (rect[0] - ox, rect[1] - oy, rect[2] - ox, rect[3] - oy), margin)
        alpha = np.maximum(alpha, _spill(scene.v0[index, oy:ey, ox:ex], plate_crop, alpha))
        ring = (alpha < 0.02)
        if ring.sum() > 50:
            src = scene.v0[index, oy:ey, ox:ex][ring].astype(np.float32)
            ref = plate_crop[ring]
            gain = np.clip(np.median(src, axis=0) / np.maximum(np.median(ref, axis=0), 1.0), 0.85, 1.18)
            plate_crop = np.clip(plate_crop * gain, 0, 255)
        protect = _in_front_mask(scene, index, scene.v0[index, oy:ey, ox:ex], plate_crop.astype(np.uint8),
                                 ox, oy, w, h, box[3], exclude, target_box=box)
        alpha = alpha * (1.0 - protect)
        a = alpha[:, :, None]
        frames[index, oy:ey, ox:ex] = (crop.astype(np.float32) * (1 - a) + plate_crop * a).astype(np.uint8)
    return {"editor": "temporal-clean-plate" + ("" if info["temporal"] else "+telea"), **info}


def _spill(frame_crop, plate_crop, alpha):
    """Foreground connected to the box but outside it (limbs, shadow, an under-sized detection)."""
    diff = np.abs(frame_crop.astype(np.int16) - plate_crop.astype(np.int16)).max(axis=2)
    fg = cv2.morphologyEx((diff > 30).astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    core = alpha > 0.5
    count, labels = cv2.connectedComponents(fg)
    touching = np.unique(labels[core & (fg > 0)])
    grow = np.isin(labels, touching[touching > 0]) & ~core
    if not grow.any() or grow.sum() > 0.35 * max(1, (~core).sum()):
        return np.zeros_like(alpha)
    grow = cv2.dilate(grow.astype(np.uint8), np.ones((7, 7), np.uint8)).astype(np.float32)
    return cv2.GaussianBlur(grow, (0, 0), 2.0)


def _blur_region(crop, mask, block):
    h, w = crop.shape[:2]
    small = cv2.resize(crop, (max(1, w // block), max(1, h // block)), interpolation=cv2.INTER_AREA)
    pix = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)
    pix = cv2.GaussianBlur(pix, (0, 0), max(1.0, block / 3))
    a = mask[:, :, None]
    return (crop.astype(np.float32) * (1 - a) + pix.astype(np.float32) * a).astype(np.uint8)


def _object_mask(scene, index, box, plate_crop, frame_crop):
    """Foreground pixels inside the box (clean-plate difference); ellipse fallback."""
    h, w = frame_crop.shape[:2]
    diff = np.abs(frame_crop.astype(np.int16) - plate_crop.astype(np.int16)).max(axis=2)
    fg = (diff > 28).astype(np.uint8)
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    fg = cv2.dilate(fg, np.ones((9, 9), np.uint8))
    if fg.mean() < 0.2:
        fg = np.zeros((h, w), np.uint8)
        cv2.ellipse(fg, (w // 2, h // 2), (max(1, w // 2), max(1, h // 2)), 0, 0, 360, 1, -1)
    return fg.astype(np.float32)


def blur(scene, frames, track):
    for offset, box in enumerate(track["boxes"]):
        index = track["first"] + offset
        if index >= len(frames):
            break
        x1, y1, x2, y2 = _pad(box, scene.width, scene.height, frac=0.06, px=4, bottom=0.0)
        frame_crop = frames[index, y1:y2, x1:x2]
        plate_crop = scene.plate_crop(scene.plate, index, x1, y1, x2 - x1, y2 - y1)
        mask = _object_mask(scene, index, box, plate_crop, scene.v0[index, y1:y2, x1:x2])
        mask = cv2.GaussianBlur(mask, (9, 9), 3)
        block = max(6, (x2 - x1) // 7)
        frames[index, y1:y2, x1:x2] = _blur_region(frame_crop, mask, block)
    return {"editor": "pixelate+gaussian (clean-plate mask)"}


def _ellipse_blur(frames, index, cx, cy, rx, ry, width, height):
    x1, y1 = int(max(0, cx - rx * 1.4)), int(max(0, cy - ry * 1.4))
    x2, y2 = int(min(width, cx + rx * 1.4)), int(min(height, cy + ry * 1.4))
    if x2 - x1 < 2 or y2 - y1 < 2:
        return
    mask = np.zeros((y2 - y1, x2 - x1), np.float32)
    cv2.ellipse(mask, (int(cx - x1), int(cy - y1)), (max(1, int(rx)), max(1, int(ry))), 0, 0, 360, 1.0, -1)
    mask = cv2.GaussianBlur(mask, (7, 7), 2)
    block = max(4, int(rx / 2.5))
    frames[index, y1:y2, x1:x2] = _blur_region(frames[index, y1:y2, x1:x2], mask, block)


def anonymize(scene, frames):
    """Blur every person's head region and every vehicle's plate zone, following each track."""
    faces = plates_n = 0
    cascade = None
    try:
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_russian_plate_number.xml")
        if cascade.empty():
            cascade = None
    except Exception:
        cascade = None
    for track in scene.tracks:
        if track["label"] == "person":
            faces += 1
            for offset, box in enumerate(track["boxes"]):
                index = track["first"] + offset
                if index >= len(frames):
                    break
                x1, y1, x2, y2 = box
                h = y2 - y1
                r = max(5.0, 0.11 * h)
                _ellipse_blur(frames, index, (x1 + x2) / 2, y1 + 0.09 * h, r * 1.05, r * 1.25, scene.width, scene.height)
        elif track["label"] in VEHICLES:
            plates_n += 1
            for offset, box in enumerate(track["boxes"]):
                index = track["first"] + offset
                if index >= len(frames):
                    break
                x1, y1, x2, y2 = box
                w, h = x2 - x1, y2 - y1
                hits = []
                if cascade is not None and w > 60:
                    gray = cv2.cvtColor(frames[index, y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
                    hits = cascade.detectMultiScale(gray, 1.1, 4, minSize=(16, 6))
                if len(hits):
                    for px, py, pw, ph in hits:
                        _ellipse_blur(frames, index, x1 + px + pw / 2, y1 + py + ph / 2, pw * 0.65, ph * 0.8,
                                      scene.width, scene.height)
                else:
                    _ellipse_blur(frames, index, (x1 + x2) / 2, y1 + 0.78 * h, 0.2 * w, 0.1 * h,
                                  scene.width, scene.height)
    return {"editor": "anonymize: head-region + plate-zone blur", "people": faces, "vehicles": plates_n}


def _sprite_frame(sprite, height_px, flip):
    h0, w0 = sprite.shape[:2]
    height_px = int(max(8, height_px))
    width_px = int(max(4, round(w0 * height_px / h0)))
    img = cv2.resize(sprite, (width_px, height_px), interpolation=cv2.INTER_AREA if height_px < h0 else cv2.INTER_LINEAR)
    if flip:
        img = img[:, ::-1]
    return img


def _composite(scene, frames, index, sprite, cx, bottom, exclude, light):
    sh, sw = sprite.shape[:2]
    x1, y1 = int(round(cx - sw / 2)), int(round(bottom - sh))
    shadow_h = max(3, int(0.12 * sw))
    ox, oy = max(0, x1 - 4), max(0, y1)
    ex, ey = min(scene.width, x1 + sw + 4), min(scene.height, int(bottom + shadow_h))
    if ex - ox < 2 or ey - oy < 2:
        return
    w, h = ex - ox, ey - oy
    region = frames[index, oy:ey, ox:ex].astype(np.float32)
    shadow = np.zeros((h, w), np.float32)
    cv2.ellipse(shadow, (int(cx - ox), int(bottom - oy)), (max(2, int(sw * 0.5)), max(1, shadow_h // 2)), 0, 0, 360, 0.45, -1)
    shadow = cv2.GaussianBlur(shadow, (0, 0), max(1.0, shadow_h / 2))
    region *= (1 - shadow[:, :, None])
    layer = np.zeros((h, w, 3), np.float32)
    alpha = np.zeros((h, w), np.float32)
    sx1, sy1 = ox - x1, oy - y1
    sx2, sy2 = sx1 + w, sy1 + h
    sx1c, sy1c = max(0, sx1), max(0, sy1)
    sx2c, sy2c = min(sw, sx2), min(sh, sy2)
    if sx2c > sx1c and sy2c > sy1c:
        dx, dy = sx1c - sx1, sy1c - sy1
        part = sprite[sy1c:sy2c, sx1c:sx2c]
        layer[dy:dy + part.shape[0], dx:dx + part.shape[1]] = part[:, :, :3].astype(np.float32) * light
        alpha[dy:dy + part.shape[0], dx:dx + part.shape[1]] = part[:, :, 3].astype(np.float32) / 255.0
    plate_crop = scene.plate_crop(scene.plate, index, ox, oy, w, h)
    protect = _in_front_mask(scene, index, scene.v0[index, oy:ey, ox:ex], plate_crop, ox, oy, w, h, bottom, exclude)
    alpha *= (1 - protect)
    a = alpha[:, :, None]
    frames[index, oy:ey, ox:ex] = np.clip(region * (1 - a) + layer * a, 0, 255).astype(np.uint8)


def _light_match(scene, cx, bottom, sprite_item):
    if sprite_item["kind"] == "cutout":
        return 1.0
    x1, x2 = int(max(0, cx - 60)), int(min(scene.width, cx + 60))
    y1, y2 = int(max(0, bottom - 80)), int(min(scene.height, bottom + 10))
    patch = scene.plate[y1:y2, x1:x2]
    if patch.size == 0:
        return 1.0
    luma = float(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).mean())
    return float(np.clip(luma / 150.0, 0.72, 1.08))


def soften(sprite):
    rgb = cv2.GaussianBlur(sprite[:, :, :3], (0, 0), 0.7)
    return np.dstack([rgb, sprite[:, :, 3]])


def add_static(scene, frames, sprite_item, cx, bottom, first=0, last=None):
    sprite = sprites.load(sprite_item["name"])
    if sprite is None:
        raise RuntimeError("sprite missing")
    real = sprites.REAL_HEIGHT_M.get(sprite_item["label"], 1.0)
    height_px = scene.person_height(bottom) * real / 1.7
    img = soften(_sprite_frame(sprite, height_px, False))
    light = _light_match(scene, cx, bottom, sprite_item)
    last = len(frames) - 1 if last is None else last
    for index in range(first, last + 1):
        _composite(scene, frames, index, img, cx, bottom, set(), light)
    return {"editor": "composite (sprite, perspective scale, contact shadow, occlusion)",
            "placed_at": [int(cx), int(bottom)], "height_px": int(height_px)}


def add_following(scene, frames, sprite_item, track):
    """Insert a sprite that follows a track (used by Replace)."""
    sprite = sprites.load(sprite_item["name"])
    if sprite is None:
        raise RuntimeError("sprite missing")
    real_new = sprites.REAL_HEIGHT_M.get(sprite_item["label"], 1.0)
    real_old = sprites.REAL_HEIGHT_M.get(track["label"], 1.6)
    boxes = np.array(track["boxes"], dtype=np.float64)
    aspect = sprite.shape[1] / sprite.shape[0]
    heights = (boxes[:, 3] - boxes[:, 1]) * real_new / real_old
    heights = np.minimum(heights, 1.35 * np.median(boxes[:, 2] - boxes[:, 0]) / aspect)
    if len(heights) >= 9:
        heights = np.convolve(np.pad(heights, 4, mode="edge"), np.ones(9) / 9, mode="valid")
    centers = (boxes[:, 0] + boxes[:, 2]) / 2
    motion = centers[-1] - centers[0]
    facing = sprite_item.get("facing")
    flip = bool(facing and abs(motion) > 4 and ((motion > 0) != (facing == "right")))
    exclude = {track["id"]}
    for offset, box in enumerate(boxes):
        index = track["first"] + offset
        if index >= len(frames):
            break
        img = soften(_sprite_frame(sprite, heights[offset], flip))
        light = _light_match(scene, centers[offset], box[3], sprite_item)
        _composite(scene, frames, index, img, centers[offset], box[3], exclude, light)
    return {"editor": "composite following track", "flipped": flip}


def watermark(frames, text):
    for frame in frames:
        h, w = frame.shape[:2]
        scale = max(0.4, h / 1400)
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
        org = (w - tw - 12, h - 12)
        cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (235, 235, 235), 1, cv2.LINE_AA)
