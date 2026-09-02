"""A tiny job registry for the long-running stages.

An optimisation run is minutes to hours. An MCP tool call that blocks for
that long is useless to an agent -- it cannot report progress, cannot be
interrupted, and will time out in most clients. So the long stages start
a job and return an id, and the agent polls.

One worker thread, deliberately: HFSS solves are serialised by licence
and cores anyway, and a second concurrent study would only make both
slower.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"  # queued | running | done | failed | cancelled
    started_at: float = 0.0
    finished_at: float = 0.0
    result: Any = None
    error: str = ""
    progress: list[str] = field(default_factory=list)
    _future: Future | None = None

    def note(self, msg: str) -> None:
        self.progress.append(f"{time.strftime('%H:%M:%S')} {msg}")

    def summary(self, tail: int = 12) -> dict:
        out = {
            "job_id": self.id,
            "kind": self.kind,
            "status": self.status,
            "elapsed_s": round(
                (self.finished_at or time.time()) - self.started_at, 1
            ) if self.started_at else 0.0,
            "progress": self.progress[-tail:],
        }
        if self.status == "failed":
            out["error"] = self.error
        return out


class JobRegistry:
    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rf-agent-job")
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(self, kind: str, fn: Callable[[Job], Any]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind)
        with self._lock:
            self._jobs[job.id] = job

        def run() -> Any:
            job.status = "running"
            job.started_at = time.time()
            try:
                job.result = fn(job)
                job.status = "done"
                return job.result
            except Exception as e:
                job.status = "failed"
                job.error = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=6)}"
                raise
            finally:
                job.finished_at = time.time()

        job._future = self._pool.submit(run)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[dict]:
        with self._lock:
            return [j.summary(tail=2) for j in self._jobs.values()]


_registry: JobRegistry | None = None


def get_registry() -> JobRegistry:
    global _registry
    if _registry is None:
        _registry = JobRegistry()
    return _registry
