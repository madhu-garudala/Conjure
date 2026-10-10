"""Plain-English command -> validated edit intent -> concrete target tracks.

Fail closed: if the command cannot be parsed, or several objects match and nothing
disambiguates them, return a question or a candidate list instead of guessing.
"""

import re

import cv2
import numpy as np

import models
import sprites
import tracker

ACTIONS = ("remove", "replace", "add", "blur", "anonymize")
COCO = (
    "person bicycle car motorcycle airplane bus train truck boat traffic light fire hydrant stop sign "
    "parking meter bench bird cat dog horse sheep cow elephant bear zebra giraffe backpack umbrella handbag "
    "tie suitcase frisbee skis snowboard sports ball kite baseball bat baseball glove skateboard surfboard "
    "tennis racket bottle wine glass cup fork knife spoon bowl banana apple sandwich orange broccoli carrot "
    "hot dog pizza donut cake chair couch potted plant bed dining table toilet tv laptop mouse remote keyboard "
    "cell phone microwave oven toaster sink refrigerator book clock vase scissors teddy bear hair drier toothbrush"
)
MULTI = ("traffic light", "fire hydrant", "stop sign", "parking meter", "sports ball", "baseball bat",
         "baseball glove", "tennis racket", "wine glass", "hot dog", "potted plant", "dining table",
         "cell phone", "teddy bear", "hair drier")
COCO_CLASSES = set(MULTI) | {w for w in COCO.split() if not any(w in m.split() for m in MULTI)}
SYNONYMS = {
    "people": "person", "pedestrian": "person", "pedestrians": "person", "man": "person", "men": "person",
    "woman": "person", "women": "person", "guy": "person", "girl": "person", "boy": "person", "kid": "person",
    "child": "person", "lady": "person", "persons": "person", "walker": "person", "jogger": "person",
    "sedan": "car", "taxi": "car", "cab": "car", "suv": "car", "van": "car", "cars": "car", "vehicle": "car",
    "lorry": "truck", "pickup": "truck", "trucks": "truck", "buses": "bus", "bike": "bicycle",
    "bikes": "bicycle", "cyclist": "bicycle", "motorbike": "motorcycle", "scooter": "motorcycle",
    "bag": "handbag", "purse": "handbag", "hydrant": "fire hydrant", "plant": "potted plant",
    "cone": "traffic_cone", "traffic cone": "traffic_cone", "barricade": "barrier", "post": "bollard",
}
SPATIAL = ("left", "right", "center", "largest", "smallest", "top", "bottom")
SPATIAL_WORDS = {
    "leftmost": "left", "left": "left", "rightmost": "right", "right": "right", "middle": "center",
    "center": "center", "centre": "center", "biggest": "largest", "largest": "largest", "closest": "largest",
    "nearest": "largest", "front": "largest", "smallest": "smallest", "farthest": "smallest",
    "furthest": "smallest", "background": "smallest", "top": "top", "upper": "top", "bottom": "bottom", "lower": "bottom",
}
COLORS = ("red", "orange", "yellow", "green", "blue", "purple", "pink", "white", "black", "gray", "brown")

AUTHORITY = r"(police|cops?|court|judge|jury|insurance|insurer|investigat\w*|prosecut\w*|detective|authorit\w*|officer|lawyer|attorney)"
INTENT = (r"(fool|trick|deceiv\w*|mislead\w*|hide|conceal|fake|cover\s*up|frame|tamper\w*|\blie\b|"
          r"so\s+(?:that\s+)?(?:they|nobody|no\s+one)|(?:can'?t|cannot|won'?t\s+be\s+able\s+to)\s+(?:prove|identify|tell|trace|find|see)|"
          r"won'?t\s+(?:know|find|see|notice)|before\s+(?:the\s+)?\w+\s+(?:sees?|gets?|reviews?|watch\w*)|"
          r"never\s+(?:know|find)|get\s+away|avoid\s+(?:the\s+|a\s+)?(?:ticket|fine|charges?|liability|blame|claim)|"
          r"pretend|without\s+(?:them|anyone|anybody)\s+knowing|no\s+one\s+will\s+know)")
PHRASES = r"(fake\s+evidence|fabricat\w*\s+evidence|tamper\w*\s+with\s+evidence|destroy\w*\s+evidence|doctor\w*\s+(?:the\s+)?evidence|frame\s+(?:him|her|them|someone|somebody)|false\s+alibi|alibi)"


class NeedsInput(Exception):
    def __init__(self, question, candidates=None, parsed=None):
        super().__init__(question)
        self.question = question
        self.candidates = candidates or []
        self.parsed = parsed


def guardrail(text):
    """Return a refusal reason if the command states a deceptive purpose, else None."""
    lowered = (text or "").lower()
    if re.search(PHRASES, lowered):
        return "The command asks to fabricate or tamper with evidence."
    if re.search(AUTHORITY, lowered) and re.search(INTENT, lowered):
        return "The command states an intent to deceive authorities, courts or investigators."
    return None


def canonical_class(name):
    if not name:
        return None
    name = str(name).strip().lower().replace("-", " ")
    name = SYNONYMS.get(name, name)
    if name in COCO_CLASSES or name in sprites.REAL_HEIGHT_M:
        return name
    if name.endswith("s") and name[:-1] in COCO_CLASSES:
        return name[:-1]
    return None


def _prompt(text, present, library):
    return (
        "You convert video editing commands into strict JSON. Reply with JSON only, no prose.\n"
        "Schema: {\"action\": one of remove|replace|add|blur|anonymize|unknown, "
        "\"object_class\": COCO class name of the existing object to edit (null for add/anonymize), "
        "\"quantity\": \"one\" or \"all\", "
        "\"spatial_hint\": one of left|right|center|largest|smallest|top|bottom or null, "
        "\"attributes\": list of color words describing the target (from: red orange yellow green blue purple pink white black gray brown), "
        "\"new_object\": for add/replace, the object to insert, chosen from the library list, else null, "
        "\"position_hint\": for add only: left|right|center or \"near <coco class>\" or null}\n"
        "Rules: 'nearest'/'closest'/'in front' => largest; 'far'/'in the back' => smallest. "
        "'erase', 'delete', 'get rid of', 'take out' => remove. 'pixelate', 'hide the face of', 'redact' => blur. "
        "'swap X for Y', 'turn X into Y', 'replace X with Y' => replace. 'put', 'place', 'insert' => add. "
        "'anonymize', 'blur all faces', 'hide faces and plates' => anonymize. "
        "Map synonyms to COCO: pedestrian/man/woman => person, sedan/suv/van => car, bike => bicycle. "
        "taxi/cab => object_class car with attribute yellow.\n"
        f"Objects detected in this clip: {', '.join(present) or 'unknown'}.\n"
        f"Insertable library: {', '.join(library)}.\n"
        "Examples:\n"
        "remove the person on the left => {\"action\":\"remove\",\"object_class\":\"person\",\"quantity\":\"one\",\"spatial_hint\":\"left\",\"attributes\":[],\"new_object\":null,\"position_hint\":null}\n"
        "replace the taxi with a bus => {\"action\":\"replace\",\"object_class\":\"car\",\"quantity\":\"one\",\"spatial_hint\":null,\"attributes\":[],\"new_object\":\"bus\",\"position_hint\":null}\n"
        "put a traffic cone on the right => {\"action\":\"add\",\"object_class\":null,\"quantity\":\"one\",\"spatial_hint\":null,\"attributes\":[],\"new_object\":\"traffic_cone\",\"position_hint\":\"right\"}\n"
        "blur everyone => {\"action\":\"blur\",\"object_class\":\"person\",\"quantity\":\"all\",\"spatial_hint\":null,\"attributes\":[],\"new_object\":null,\"position_hint\":null}\n"
        f"Command: {text}\nJSON:"
    )


def _rule_parse(text):
    lowered = text.lower()
    action = "unknown"
    for words, name in (
        (("anonymi", "all faces", "faces and plates"), "anonymize"),
        (("replace", "swap", "turn ", "change "), "replace"),
        (("add ", "put ", "place ", "insert "), "add"),
        (("blur", "pixelate", "redact"), "blur"),
        (("remove", "erase", "delete", "get rid", "take out"), "remove"),
    ):
        if any(w in lowered for w in words):
            action = name
            break
    words = re.findall(r"[a-z]+", lowered)
    found = []
    for i, word in enumerate(words):
        pair = " ".join(words[i:i + 2])
        for candidate in (pair, word):
            name = canonical_class(candidate)
            if name:
                found.append(name)
                break
    spatial = next((SPATIAL_WORDS[w] for w in words if w in SPATIAL_WORDS), None)
    parsed = {
        "action": action,
        "object_class": None,
        "quantity": "all" if re.search(r"\b(all|every|everyone|everybody)\b", lowered) else "one",
        "spatial_hint": spatial,
        "attributes": [w for w in words if w in COLORS],
        "new_object": None,
        "position_hint": spatial if action == "add" else None,
    }
    if action == "add":
        parsed["new_object"] = found[0] if found else None
        near = re.search(r"near (?:the |a )?([a-z ]+)", lowered)
        if near and canonical_class(near.group(1).split()[0]):
            parsed["position_hint"] = "near " + canonical_class(near.group(1).split()[0])
    elif action == "replace":
        parsed["object_class"] = found[0] if found else None
        parsed["new_object"] = found[1] if len(found) > 1 else None
    else:
        parsed["object_class"] = found[0] if found else None
    if action == "blur" and parsed["object_class"] is None and re.search(r"\b(everyone|everybody|faces?)\b", lowered):
        parsed["object_class"], parsed["quantity"] = "person", "all"
    return parsed


def parse(text, present):
    """Returns (intent dict, parser id). Raises NeedsInput when the command cannot be read."""
    library = sprites.labels()
    parser = None
    parsed = None
    if models.wandb_configured():
        try:
            reply, model = models.wandb_chat(_prompt(text, present, library), label=text[:80])
            parsed, parser = models.extract_json(reply), model
            if parsed is None:
                raise NeedsInput("I couldn't read that command. Try e.g. \"remove the person on the left\" "
                                 "or click an object in the video.")
        except NeedsInput:
            raise
        except Exception:
            parsed = None
    if parsed is None:
        parsed, parser = _rule_parse(text), "rule-parser (LLM unavailable)"
    intent = validate(parsed, present, library)
    if re.search(r"\b(taxi|cab|yellow cab)\b", text.lower()) and intent["object_class"] == "car" \
            and "yellow" not in intent["attributes"]:
        intent["attributes"].append("yellow")
    return intent, parser


def validate(parsed, present, library):
    action = str(parsed.get("action") or "").lower()
    if action not in ACTIONS:
        raise NeedsInput("I couldn't tell what edit you want. Use remove, blur, replace, add or anonymize, "
                         "or click an object in the video.")
    intent = {
        "action": action,
        "object_class": canonical_class(parsed.get("object_class")),
        "quantity": "all" if str(parsed.get("quantity")).lower() == "all" else "one",
        "spatial_hint": parsed.get("spatial_hint") if parsed.get("spatial_hint") in SPATIAL else None,
        "attributes": [c for c in (parsed.get("attributes") or []) if c in COLORS][:2],
        "new_object": canonical_class(parsed.get("new_object")),
        "position_hint": None,
    }
    hint = str(parsed.get("position_hint") or "").lower().strip()
    if hint in ("left", "right", "center"):
        intent["position_hint"] = hint
    elif hint.startswith("near "):
        near = canonical_class(hint[5:])
        intent["position_hint"] = f"near {near}" if near else None
    if action in ("remove", "blur", "replace") and not intent["object_class"]:
        raise NeedsInput("Which object should I edit? Name it (e.g. person, car, bus) or click it in the video.",
                         parsed=intent)
    if action in ("remove", "blur", "replace") and intent["object_class"] not in present:
        raise NeedsInput(f"I don't see any {intent['object_class']} in this clip. "
                         f"Detected objects: {', '.join(present) or 'none'}.", parsed=intent)
    if action in ("add", "replace"):
        if not intent["new_object"] or intent["new_object"] not in library:
            raise NeedsInput("What should I insert? Available: " + ", ".join(library) + ".", parsed=intent)
    return intent


def _color_score(frame, box, color):
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    crop = frame[y1 + int(0.1 * h):y2 - int(0.1 * h), x1 + int(0.2 * w):x2 - int(0.2 * w)]
    if crop.size == 0:
        return 0.0
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.int32)
    hue, sat, val = hsv[:, 0], hsv[:, 1], hsv[:, 2]
    colored = (sat > 80) & (val > 60)
    ranges = {"red": ((hue < 6) | (hue > 170)), "orange": (hue >= 5) & (hue < 17), "yellow": (hue >= 12) & (hue < 34),
              "green": (hue >= 34) & (hue < 85), "blue": (hue >= 90) & (hue < 130), "purple": (hue >= 130) & (hue < 155),
              "pink": (hue >= 155) & (hue <= 170), "brown": (hue >= 8) & (hue < 20) & (val < 150)}
    if color == "white":
        hit = (sat < 45) & (val > 175)
    elif color == "black":
        hit = val < 55
    elif color == "gray":
        hit = (sat < 40) & (val >= 70) & (val <= 175)
    else:
        hit = colored & ranges[color]
    return float(hit.mean())


def _order(boxes, hint, width, height):
    def key(item):
        x1, y1, x2, y2 = item[1]
        cx, cy, area = (x1 + x2) / 2, (y1 + y2) / 2, (x2 - x1) * (y2 - y1)
        return {"left": cx, "right": -cx, "center": abs(cx - width / 2), "largest": -area,
                "smallest": area, "top": cy, "bottom": -cy}[hint]
    return sorted(boxes, key=key)


def candidates_at(tracks, label, frame, excluded):
    out = []
    for track in tracks:
        if track["id"] in excluded or track["label"] != label:
            continue
        box = tracker.box_at(track, frame)
        if box and (box[2] - box[0]) * (box[3] - box[1]) >= 120:
            out.append((track, box))
    return out


def resolve(intent, tracks, frames, anchor, excluded, width, height):
    """Return (selected tracks, anchor frame). Raises NeedsInput with candidates when ambiguous."""
    label = intent["object_class"]
    found = candidates_at(tracks, label, anchor, excluded)
    if intent["attributes"] and intent["quantity"] != "all":
        def best_color(frame):
            return max([sum(_color_score(frames[frame], b, c) for c in intent["attributes"])
                        for _, b in candidates_at(tracks, label, frame, excluded)] or [0.0])
        if best_color(anchor) < 0.12:
            scan = max(range(0, len(frames), 5), key=best_color)
            if best_color(scan) >= 0.12:
                anchor = scan
                found = candidates_at(tracks, label, anchor, excluded)
    if not found:
        best = max(range(len(frames)), key=lambda f: len(candidates_at(tracks, label, f, excluded)))
        found = candidates_at(tracks, label, best, excluded)
        anchor = best
    if not found:
        raise NeedsInput(f"No {label} is visible in this clip (or it was already edited out).")
    if intent["quantity"] == "all":
        selected = {t["id"]: t for t in tracks if t["label"] == label and t["id"] not in excluded}
        return list(selected.values()), anchor
    if len(found) > 1:
        largest = max((b[2] - b[0]) * (b[3] - b[1]) for _, b in found)
        solid = [(t, b) for t, b in found
                 if t["observed"] >= 12 and (b[2] - b[0]) * (b[3] - b[1]) >= 0.12 * largest
                 and b[0] > 2 and b[2] < width - 3 and b[3] < height - 3]
        found = solid or found
    if len(found) == 1:
        return [found[0][0]], anchor
    pool = found
    if intent["attributes"]:
        frame = frames[anchor]
        scored = sorted(((sum(_color_score(frame, box, c) for c in intent["attributes"]), track, box)
                         for track, box in pool), key=lambda s: -s[0])
        if scored[0][0] >= 0.12 and (len(scored) == 1 or scored[0][0] >= 1.6 * max(scored[1][0], 0.01)):
            if not intent["spatial_hint"]:
                return [scored[0][1]], anchor
        pool = [(t, b) for s, t, b in scored if s >= 0.5 * scored[0][0] and s > 0.04] or pool
        if len(pool) == 1:
            return [pool[0][0]], anchor
    if intent["spatial_hint"]:
        ordered = _order(pool, intent["spatial_hint"], width, height)
        return [ordered[0][0]], anchor
    ordered = _order(pool, "largest", width, height)[:8]
    raise NeedsInput(
        f"I found {len(pool)} {label}s. Which one? Click a thumbnail, or say e.g. \"the {label} on the left\".",
        candidates=[{"track_id": t["id"], "label": t["label"], "bbox": b, "frame": anchor} for t, b in ordered],
        parsed=intent,
    )


def track_at_point(tracks, frame, x, y, excluded):
    hits = []
    for track in tracks:
        if track["id"] in excluded:
            continue
        box = tracker.box_at(track, frame)
        if box and box[0] - 4 <= x <= box[2] + 4 and box[1] - 4 <= y <= box[3] + 4:
            hits.append(((box[2] - box[0]) * (box[3] - box[1]), track))
    hits.sort(key=lambda h: h[0])
    return hits[0][1] if hits else None
