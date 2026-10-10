"""Optional Weave tracing. Falls back to plain calls if Weave is missing or misconfigured.

Every call is also kept in a small log so the app can show model calls without the W&B console.
Counters are persisted to the data dir so a process restart does not reset them.
"""

import functools
import itertools
import json
import os
import threading
import time
from collections import deque

_lock = threading.Lock()
_weave = None
_ops = {}
_local = threading.local()
_log = deque(maxlen=300)
_stats = {}
_seq = itertools.count(1)
_STATS_PATH = os.path.join(os.environ.get("CONJURE_DATA", "/data"), "model_calls.json")


def _load():
    try:
        with open(_STATS_PATH) as handle:
            saved = json.load(handle)
        _stats.update(saved.get("stats", {}))
        _log.extend(saved.get("calls", []))
    except (OSError, ValueError):
        pass


_load()


def _save():
    try:
        tmp = _STATS_PATH + ".tmp"
        with open(tmp, "w") as handle:
            json.dump({"stats": _stats, "calls": list(_log)[-100:]}, handle)
        os.replace(tmp, _STATS_PATH)
    except OSError:
        pass


def _client():
    global _weave
    with _lock:
        if _weave is None:
            try:
                import weave

                weave.init(f"{os.environ['WANDB_TEAM']}/{os.environ['WANDB_PROJECT']}")
                _weave = weave
            except Exception:
                _weave = False
    return _weave or None


def _traced(client, name, fn):
    @functools.wraps(fn)
    def run(*args, **kwargs):
        record = getattr(_local, "record", None)
        if record is not None:
            try:
                current = client.get_current_call()
                record["trace_id"] = getattr(current, "id", None)
            except Exception:
                pass
        return fn(*args, **kwargs)

    return client.op(name=name)(run)


def _short(value, limit=160):
    if isinstance(value, str):
        return value if len(value) <= limit else value[: limit - 1] + "…"
    if isinstance(value, dict):
        return {k: _short(v, limit) for k, v in value.items() if isinstance(v, (str, int, float, bool, list, dict, type(None)))}
    if isinstance(value, list):
        return [_short(v, limit) for v in value[:8]]
    return value


def call(name, fn, *args, label=None, **kwargs):
    client = _client()
    record = {"op": name, "label": label, "trace_id": None, "weave": client is not None}
    _local.record = record
    started = time.time()
    ok, output = True, None
    try:
        if client is None:
            output = fn(*args, **kwargs)
        else:
            if name not in _ops:
                _ops[name] = _traced(client, name, fn)
            output = _ops[name](*args, **kwargs)
        return output
    except Exception as exc:
        ok, output = False, f"{type(exc).__name__}"
        raise
    finally:
        _local.record = None
        ms = round((time.time() - started) * 1000)
        record.update(seq=next(_seq), at=int(started * 1000), ms=ms, ok=ok, output=_short(output))
        with _lock:
            _log.append(record)
            stat = _stats.setdefault(name, {"count": 0, "errors": 0, "total_ms": 0, "last_ms": 0})
            stat["count"] += 1
            stat["errors"] += 0 if ok else 1
            stat["total_ms"] += ms
            stat["last_ms"] = ms
            _save()


def weave_enabled():
    return bool(_weave)


def recent(limit=60):
    with _lock:
        calls = list(_log)[-limit:][::-1]
        stats = {name: dict(s, avg_ms=round(s["total_ms"] / s["count"]) if s["count"] else 0) for name, s in _stats.items()}
    project = f"{os.environ.get('WANDB_TEAM', '')}/{os.environ.get('WANDB_PROJECT', '')}"
    return {"calls": calls, "stats": stats, "weave": {"enabled": bool(_weave), "project": project}}
