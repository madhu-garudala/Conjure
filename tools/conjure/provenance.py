"""Tamper-evident provenance: per-clip JSON Lines, hash-chained and HMAC-signed."""

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid

import store

_lock = threading.Lock()
_key = {"value": None, "source": None}


def _signing_key():
    if _key["value"] is None:
        env = os.environ.get("CONJURE_SIGNING_KEY")
        if env:
            _key["value"], _key["source"] = env.encode(), "k8s-secret"
        else:
            path = os.path.join(store.DATA, "signing.key")
            if not os.path.exists(path):
                with open(path, "w") as handle:
                    handle.write(secrets.token_hex(32))
            with open(path) as handle:
                _key["value"], _key["source"] = handle.read().strip().encode(), "data-volume"
    return _key["value"]


def key_info():
    key = _signing_key()
    return {"key_id": hashlib.sha256(key).hexdigest()[:12], "key_source": _key["source"], "algorithm": "hmac-sha256"}


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _path(clip_id):
    return os.path.join(store.clip_dir(clip_id), "provenance.jsonl")


def entries(clip_id):
    out = []
    try:
        with open(_path(clip_id)) as handle:
            for line in handle:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except OSError:
        pass
    return out


def append(clip_id, entry):
    with _lock:
        existing = entries(clip_id)
        entry = dict(entry)
        entry.setdefault("edit_id", str(uuid.uuid4()))
        entry.setdefault("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        entry["clip_id"] = clip_id
        entry["sequence"] = len(existing)
        entry["prev_entry_hash"] = existing[-1]["entry_hash"] if existing else None
        entry["signing_key_id"] = key_info()["key_id"]
        body = {k: v for k, v in entry.items() if k not in ("entry_hash", "signature")}
        entry["entry_hash"] = "sha256:" + hashlib.sha256(_canonical(body).encode()).hexdigest()
        mac = hmac.new(_signing_key(), entry["entry_hash"].encode(), hashlib.sha256).hexdigest()
        entry["signature"] = "hmac-sha256:" + mac
        with open(_path(clip_id), "a") as handle:
            handle.write(_canonical(entry) + "\n")
        return entry


def verify(clip_id, check_artifacts=True):
    items = entries(clip_id)
    problems = []
    prev = None
    for item in items:
        seq = item.get("sequence")
        body = {k: v for k, v in item.items() if k not in ("entry_hash", "signature")}
        expected = "sha256:" + hashlib.sha256(_canonical(body).encode()).hexdigest()
        if expected != item.get("entry_hash"):
            problems.append(f"entry {seq}: content hash mismatch")
        mac = "hmac-sha256:" + hmac.new(_signing_key(), item.get("entry_hash", "").encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, item.get("signature", "")):
            problems.append(f"entry {seq}: signature invalid")
        if item.get("prev_entry_hash") != prev:
            problems.append(f"entry {seq}: chain broken")
        prev = item.get("entry_hash")
        if check_artifacts:
            for field in ("input_artifact", "output_artifact"):
                ref = item.get(field)
                if not ref:
                    continue
                path = store.artifact_path(ref.split(":", 1)[-1])
                if path and store.sha256_file(path) != ref.split(":", 1)[-1]:
                    problems.append(f"entry {seq}: {field} file no longer matches its hash")
    return {"ok": not problems, "entries": len(items), "problems": problems, **key_info()}
