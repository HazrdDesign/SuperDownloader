"""Download queue: jobs run one after another on a background thread.

The window adds jobs and receives events through ``emit(kind, payload)``, which must be
thread-safe (the main window puts them on its event queue):

- ``("job_added", QueuedJob)``
- ``("job_started", QueuedJob)``
- ``("job_progress", (QueuedJob, Progress))``
- ``("job_done", QueuedJob)``  (``status`` is "done", "failed" or "cancelled"; ``result`` is set)
"""

from __future__ import annotations

import itertools
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .engine import DownloadJob, DownloadResult, Engine, Progress

log = logging.getLogger(__name__)

_ids = itertools.count(1)


@dataclass
class QueuedJob:
    job: DownloadJob
    title: str
    meta: dict = field(default_factory=dict)   # what History needs to re-download it
    id: int = field(default_factory=lambda: next(_ids))
    status: str = "waiting"                    # waiting, running, done, failed, cancelled
    progress: Progress | None = None
    result: DownloadResult | None = None
    engine: Any = None


class JobQueue:
    def __init__(self, engine_factory: Callable[[], Engine], emit: Callable[[str, Any], None]):
        self._engine_factory = engine_factory
        self._emit = emit
        self._lock = threading.Condition()
        self._waiting: list[QueuedJob] = []
        self.current: QueuedJob | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False

    # -- queries ---------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        with self._lock:
            return self.current is not None or bool(self._waiting)

    @property
    def waiting(self) -> list[QueuedJob]:
        with self._lock:
            return list(self._waiting)

    def active(self) -> list[QueuedJob]:
        """Running job first, then the waiting ones in order."""
        with self._lock:
            return ([self.current] if self.current else []) + list(self._waiting)

    # -- changes ---------------------------------------------------------------------------

    def add(self, job: DownloadJob, title: str, meta: dict | None = None) -> QueuedJob:
        qj = QueuedJob(job=job, title=title, meta=meta or {})
        with self._lock:
            self._waiting.append(qj)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="download-queue", daemon=True)
                self._thread.start()
            self._lock.notify_all()
        log.info("Queued download #%d (%d item(s))", qj.id, len(job.urls))
        self._emit("job_added", qj)
        return qj

    def cancel(self, job_id: int) -> None:
        with self._lock:
            if self.current is not None and self.current.id == job_id:
                if self.current.engine is not None:
                    self.current.engine.cancel()
                return
            for qj in self._waiting:
                if qj.id == job_id:
                    self._waiting.remove(qj)
                    qj.status = "cancelled"
                    qj.result = DownloadResult(cancelled=True)
                    break
            else:
                return
        log.info("Removed waiting download #%d from the queue", job_id)
        self._emit("job_done", qj)

    def cancel_all(self) -> None:
        with self._lock:
            waiting, self._waiting = self._waiting, []
            current = self.current
        for qj in waiting:
            qj.status = "cancelled"
            qj.result = DownloadResult(cancelled=True)
            self._emit("job_done", qj)
        if current is not None and current.engine is not None:
            current.engine.cancel()

    def join(self, timeout: float | None = None) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)

    # -- worker ----------------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._waiting:
                    self.current = None
                    self._thread = None
                    return
                qj = self._waiting.pop(0)
                qj.status = "running"
                qj.engine = self._engine_factory()
                self.current = qj
            self._emit("job_started", qj)

            def on_progress(p: Progress, qj=qj) -> None:
                qj.progress = p
                if p.title and qj.title == qj.job.urls[0]:
                    qj.title = p.title
                self._emit("job_progress", (qj, p))

            try:
                result = qj.engine.download(qj.job, on_progress)
            except Exception as e:  # noqa: BLE001 - the engine reports errors itself; this is a safety net
                log.exception("Download #%d crashed", qj.id)
                from .errors import classify_error
                result = DownloadResult(failures=[(qj.title, classify_error(e))])
            qj.result = result
            qj.status = "cancelled" if result.cancelled else ("done" if result.files else "failed")
            with self._lock:
                self.current = None
            log.info("Download #%d %s", qj.id, qj.status)
            self._emit("job_done", qj)
