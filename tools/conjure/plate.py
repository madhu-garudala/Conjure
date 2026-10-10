"""Camera alignment and the temporal clean plate.

For each pixel, the clean plate is the median of that pixel over every frame where no moving
object covers it. Removing an object then reveals real background seen at other moments, which is
what flow-based video inpainters approximate. Pixels never seen clean fall back to Telea inpainting
on the plate, so the fill is the same image every frame and does not flicker.
"""

import cv2
import numpy as np

import tracker

SENTINEL = 1000


def estimate_motion(frames, tracks):
    """Per-frame 2x3 affine mapping frame coords -> reference (middle) frame coords."""
    count, height, width, _ = frames.shape
    scale = 640.0 / width
    small = (640, int(round(height * scale)))
    ref = count // 2
    orb = cv2.ORB_create(1500)
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

    def features(index):
        gray = cv2.cvtColor(cv2.resize(frames[index], small, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        mask = np.full(gray.shape, 255, np.uint8)
        for track in tracks:
            box = tracker.box_at(track, index)
            if box and track["dynamic"]:
                x1, y1, x2, y2 = (int(v * scale) for v in box)
                mask[max(0, y1 - 4):y2 + 4, max(0, x1 - 4):x2 + 4] = 0
        return orb.detectAndCompute(gray, mask)

    ref_kp, ref_des = features(ref)
    identity = np.array([[1, 0, 0], [0, 1, 0]], dtype=np.float64)
    affines = [identity.copy() for _ in range(count)]
    corners = np.array([[0, 0, 1], [width, 0, 1], [0, height, 1], [width, height, 1]], dtype=np.float64).T
    worst = 0.0
    if ref_des is None or len(ref_kp) < 30:
        return affines, True, 0.0
    for index in range(count):
        if index == ref:
            continue
        kp, des = features(index)
        if des is None or len(kp) < 30:
            continue
        matches = sorted(matcher.match(des, ref_des), key=lambda m: m.distance)[:400]
        if len(matches) < 20:
            continue
        src = np.float32([kp[m.queryIdx].pt for m in matches]) / scale
        dst = np.float32([ref_kp[m.trainIdx].pt for m in matches]) / scale
        matrix, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=2.0)
        if matrix is None or inliers is None or inliers.sum() < 15:
            continue
        affines[index] = matrix
        moved = np.abs(matrix @ corners - identity @ corners).max()
        worst = max(worst, float(moved))
    static = worst < 1.0
    if static:
        affines = [identity.copy() for _ in range(count)]
    return affines, static, round(worst, 2)


def warp_box(box, matrix):
    x1, y1, x2, y2 = box
    pts = np.array([[x1, y1, 1], [x2, y1, 1], [x1, y2, 1], [x2, y2, 1]], dtype=np.float64).T
    out = matrix @ pts
    return [out[0].min(), out[1].min(), out[0].max(), out[1].max()]


def occupancy(tracks, count, height, width, affines, static, pad=6, only=None):
    """Boolean (N,H,W) in reference coords: True where a moving object covers the pixel."""
    occ = np.zeros((count, height, width), dtype=bool)
    for track in tracks:
        if only is not None and track["id"] not in only:
            continue
        if only is None and not (track["dynamic"] or track["moving"]):
            continue
        for offset, box in enumerate(track["boxes"]):
            index = track["first"] + offset
            if not static:
                box = warp_box(box, affines[index])
            bw, bh = box[2] - box[0], box[3] - box[1]
            px, py = pad + 0.06 * bw, pad + 0.06 * bh
            x1, y1 = int(max(0, box[0] - px)), int(max(0, box[1] - py))
            x2, y2 = int(min(width, box[2] + px)), int(min(height, box[3] + py + 0.08 * bh))
            occ[index, y1:y2, x1:x2] = True
    return occ


def to_reference(frames, affines, static):
    if static:
        return frames, None
    count, height, width, _ = frames.shape
    warped = np.empty_like(frames)
    seen = np.empty((count, height, width), dtype=bool)
    ones = np.full((height, width), 255, np.uint8)
    for index in range(count):
        warped[index] = cv2.warpAffine(frames[index], affines[index], (width, height), flags=cv2.INTER_LINEAR)
        seen[index] = cv2.warpAffine(ones, affines[index], (width, height), flags=cv2.INTER_NEAREST) > 0
    return warped, seen


def masked_median(stack, valid):
    """stack (N,h,w,3) uint8, valid (N,h,w) bool -> (median (h,w,3) uint8, count (h,w))."""
    values = stack.astype(np.uint16)
    values[~valid] = SENTINEL
    values.sort(axis=0)
    count = valid.sum(axis=0)
    k = (np.maximum(count, 1) - 1) // 2
    median = np.take_along_axis(values, np.repeat(k[None, :, :, None], 3, axis=3), axis=0)[0]
    median[median >= SENTINEL] = 0
    return median.astype(np.uint8), count


def build_plate(frames, tracks, affines, static, progress=None):
    count, height, width, _ = frames.shape
    ref_frames, seen = to_reference(frames, affines, static)
    occ = occupancy(tracks, count, height, width, affines, static)
    if seen is not None:
        occ |= ~seen
    plate = np.zeros((height, width, 3), np.uint8)
    holes = np.zeros((height, width), bool)
    step = 48
    for y0 in range(0, height, step):
        y1 = min(height, y0 + step)
        median, seen_count = masked_median(ref_frames[:, y0:y1], ~occ[:, y0:y1])
        hole = seen_count == 0
        if hole.any():
            fallback = np.median(ref_frames[:, y0:y1], axis=0).astype(np.uint8)
            median[hole] = fallback[hole]
        plate[y0:y1] = median
        holes[y0:y1] = hole
        if progress:
            progress(y1 / height)
    return plate, holes


def plate_view(plate, matrix, static, x, y, w, h):
    """The plate as seen by one frame, cropped to (x, y, w, h) in that frame's coords."""
    if static:
        return plate[y:y + h, x:x + w]
    shifted = matrix.copy()
    shifted[:, 2] = matrix[:, 2] + matrix[:, :2] @ np.array([x, y], dtype=np.float64)
    return cv2.warpAffine(plate, shifted, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                          borderMode=cv2.BORDER_REPLICATE)


def fill_for_target(plate, holes, frames, target, affines, static):
    """Fill plate pixels the global plate never saw clean, using frames where only the target is absent."""
    count, height, width, _ = frames.shape
    region = occupancy([target], count, height, width, affines, static, pad=10, only={target["id"]})
    footprint = region.any(axis=0)
    need = holes & footprint
    if not need.any():
        return plate, {"temporal": True, "telea_pixels": 0}
    fixed = plate.copy()
    ys, xs = np.where(need)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    if static:
        crop = frames[:, y0:y1, x0:x1]
        valid = ~region[:, y0:y1, x0:x1]
    else:
        warped, seen = to_reference(frames, affines, static)
        crop = warped[:, y0:y1, x0:x1]
        valid = ~region[:, y0:y1, x0:x1] & seen[:, y0:y1, x0:x1]
    median, seen_count = masked_median(crop, valid)
    local_need = need[y0:y1, x0:x1]
    use = local_need & (seen_count > 0)
    patch = fixed[y0:y1, x0:x1]
    patch[use] = median[use]
    remaining = local_need & (seen_count == 0)
    telea = int(remaining.sum())
    if telea:
        pad = 12
        ya, yb = max(0, y0 - pad), min(height, y1 + pad)
        xa, xb = max(0, x0 - pad), min(width, x1 + pad)
        hole_mask = np.zeros((yb - ya, xb - xa), np.uint8)
        hole_mask[y0 - ya:y1 - ya, x0 - xa:x1 - xa][remaining] = 255
        fixed[ya:yb, xa:xb] = cv2.inpaint(fixed[ya:yb, xa:xb], hole_mask, 7, cv2.INPAINT_TELEA)
    return fixed, {"temporal": telea == 0, "telea_pixels": telea}
