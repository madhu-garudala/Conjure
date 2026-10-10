"""Background jobs with per-stage progress. One heavy worker keeps memory predictable."""

import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

_pool = ThreadPoolExecutor(max_workers=1)
_jobs = {}
_lock = threading.Lock()


class Job:
    def __init__(self, kind, clip_id, stages):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.clip_id = clip_id
        self.stages = list(stages)
        self.stage = None
        self.stage_progress = 0.0
        self.status = "queued"
        self.result = None
        self.error = None
        self.created = time.time()
        self.finished = None

    def enter(self, stage):
        if stage not in self.stages:
            self.stages.append(stage)
        self.stage = stage
        self.stage_progress = 0.0

    def progress(self, value):
        self.stage_progress = max(0.0, min(1.0, float(value)))

    def view(self):
        index = self.stages.index(self.stage) if self.stage in self.stages else 0
        total = len(self.stages) or 1
        overall = 1.0 if self.status in ("done", "needs_input") else (index + self.stage_progress) / total
        return {
            "job_id": self.id, "kind": self.kind, "clip_id": self.clip_id, "status": self.status,
            "stage": self.stage, "stages": self.stages, "progress": round(overall, 3),
            "result": self.result, "error": self.error,
            "elapsed_s": round((self.finished or time.time()) - self.created, 1),
        }


def submit(kind, clip_id, stages, fn, *args):
    job = Job(kind, clip_id, stages)
    with _lock:
        _jobs[job.id] = job
        old = sorted(_jobs.values(), key=lambda j: j.created)[:-200]
        for stale in old:
            _jobs.pop(stale.id, None)

    def run():
        job.status = "running"
        try:
            outcome = fn(job, *args)
            if isinstance(outcome, dict) and outcome.get("needs_input"):
                job.status = "needs_input"
            else:
                job.status = "done"
            job.result = outcome
        except Exception as exc:
            traceback.print_exc()
            job.status = "error"
            job.error = str(exc) or type(exc).__name__
        finally:
            job.finished = time.time()

    _pool.submit(run)
    return job


def get(job_id):
    with _lock:
        return _jobs.get(job_id)


def active(clip_id=None):
    with _lock:
        return [j.view() for j in _jobs.values()
                if j.status in ("queued", "running") and (clip_id is None or j.clip_id == clip_id)]
