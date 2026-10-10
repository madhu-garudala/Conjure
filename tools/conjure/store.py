"""On-disk state: clips, immutable artifacts, versions. Everything reloads at startup."""

import hashlib
import json
import os
import threading
import time

DATA = os.environ.get("CONJURE_DATA", "/data")
CLIPS = os.path.join(DATA, "clips")
os.makedirs(CLIPS, exist_ok=True)

_lock = threading.RLock()
_clip_locks = {}
_artifacts = {}


def clip_dir(clip_id):
    return os.path.join(CLIPS, clip_id)


def read_json(path, default=None):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    tmp = f"{path}.tmp{threading.get_ident()}"
    with open(tmp, "w") as handle:
        json.dump(obj, handle)
    os.replace(tmp, path)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def register_artifact(path):
    sha = sha256_file(path)
    with _lock:
        _artifacts[sha] = path
    return sha


def artifact_path(sha):
    with _lock:
        return _artifacts.get(sha)


def clip_lock(clip_id):
    with _lock:
        return _clip_locks.setdefault(clip_id, threading.RLock())


def meta(clip_id):
    return read_json(os.path.join(clip_dir(clip_id), "meta.json"))


def save_meta(clip_id, data):
    write_json(os.path.join(clip_dir(clip_id), "meta.json"), data)


def versions(clip_id):
    return read_json(os.path.join(clip_dir(clip_id), "versions.json"), {"head": None, "items": []})


def save_versions(clip_id, data):
    write_json(os.path.join(clip_dir(clip_id), "versions.json"), data)


def tracks(clip_id):
    return read_json(os.path.join(clip_dir(clip_id), "tracks.json"), [])


def head_version(clip_id):
    data = versions(clip_id)
    for item in data["items"]:
        if item["version"] == data["head"]:
            return item
    return data["items"][-1] if data["items"] else None


def add_version(clip_id, file_path, parent, edit_id, operation, label, excluded, extra=None):
    with clip_lock(clip_id):
        data = versions(clip_id)
        number = len(data["items"])
        sha = register_artifact(file_path)
        item = {
            "version": number,
            "file": os.path.basename(file_path),
            "artifact": sha,
            "parent": parent,
            "edit_id": edit_id,
            "operation": operation,
            "label": label,
            "excluded_tracks": sorted(set(excluded)),
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if extra:
            item.update(extra)
        data["items"].append(item)
        data["head"] = number
        save_versions(clip_id, data)
        return item


def list_clips():
    out = []
    for clip_id in os.listdir(CLIPS):
        data = meta(clip_id)
        if data:
            out.append(data)
    out.sort(key=lambda m: m.get("created", ""), reverse=True)
    return out


def load_all():
    count = 0
    for clip_id in os.listdir(CLIPS):
        folder = clip_dir(clip_id)
        data = meta(clip_id)
        if not data:
            continue
        if data.get("original_artifact") and os.path.exists(os.path.join(folder, "original.mp4")):
            with _lock:
                _artifacts[data["original_artifact"]] = os.path.join(folder, "original.mp4")
        for item in versions(clip_id)["items"]:
            path = os.path.join(folder, item["file"])
            if os.path.exists(path):
                with _lock:
                    _artifacts[item["artifact"]] = path
        count += 1
    return count
