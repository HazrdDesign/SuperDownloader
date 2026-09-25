import json
import threading
import time
from pathlib import Path

from app import history
from app.engine import DownloadJob, DownloadResult, Progress
from app.history import History, HistoryEntry, relative_time
from app.jobs import JobQueue


# ---- history ----------------------------------------------------------------------------------

def test_history_add_remove_clear_and_persist():
    h = History()
    a = HistoryEntry(url="https://a", title="A", ok=True, files=["x.mp4"])
    b = HistoryEntry(url="https://b", title="B", ok=False, error="Removed")
    h.add(a)
    h.add(b)
    assert [e.title for e in History().entries] == ["B", "A"]  # newest first, saved to disk
    h.remove(b.id)
    assert [e.title for e in History().entries] == ["A"]
    h.clear()
    assert History().entries == []


def test_history_is_capped():
    h = History(persist=False)
    for i in range(history.MAX_ENTRIES + 25):
        h.add(HistoryEntry(url=f"https://{i}", title=str(i), ok=True))
    assert len(h.entries) == history.MAX_ENTRIES
    assert h.entries[0].title == str(history.MAX_ENTRIES + 24)


def test_history_file_tolerates_junk_and_unknown_fields(tmp_path):
    f = history._file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps([{"url": "https://a", "title": "A", "ok": True, "future_field": 1},
                             {"no_url": True}, "junk"]), encoding="utf-8")
    [e] = history.load()
    assert e.title == "A"
    f.write_text("{broken", encoding="utf-8")
    assert history.load() == []


def test_history_never_stores_passwords():
    assert "password" not in {f for f in HistoryEntry.__dataclass_fields__}


def test_first_file(tmp_path):
    real = tmp_path / "x.mp4"
    real.write_bytes(b"x")
    e = HistoryEntry(url="u", title="t", ok=True, files=[str(tmp_path / "gone.mp4"), str(real)])
    assert e.first_file() == real
    assert HistoryEntry(url="u", title="t", ok=True).first_file() is None


def test_relative_time():
    now = 1_000_000_000
    assert relative_time(now - 5, now) == "just now"
    assert relative_time(now - 300, now) == "5 min ago"
    assert relative_time(now - 7200, now) == "2 h ago"
    assert relative_time(now - 86400 * 1.5, now) == "yesterday"
    assert relative_time(now - 86400 * 3, now) == "3 days ago"


# ---- queue ------------------------------------------------------------------------------------

class SlowEngine:
    def __init__(self, seconds=0.2):
        self.seconds = seconds
        self._cancel = threading.Event()
        self.order = []

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def cancel(self):
        self._cancel.set()

    def download(self, job, on_progress):
        self._cancel.clear()
        self.order.append(job.urls[0])
        end = time.time() + self.seconds
        while time.time() < end:
            if self._cancel.is_set():
                return DownloadResult(cancelled=True)
            on_progress(Progress(50, None, None, None, None, "downloading", title="Real title"))
            time.sleep(0.01)
        return DownloadResult(files=[Path(job.urls[0].rsplit("/", 1)[-1] + ".mp4")])


def run_queue(engine):
    events = []
    q = JobQueue(lambda: engine, lambda kind, payload: events.append((kind, payload)))
    return q, events


def job(url):
    return DownloadJob([url], None, Path("."))


def test_jobs_run_one_after_another_in_order():
    eng = SlowEngine(0.1)
    q, events = run_queue(eng)
    a = q.add(job("https://x/a"), "https://x/a")
    b = q.add(job("https://x/b"), "https://x/b")
    assert q.busy
    q.join(5)
    assert eng.order == ["https://x/a", "https://x/b"]
    assert [p.id for k, p in events if k == "job_done"] == [a.id, b.id]
    assert a.status == b.status == "done"
    assert a.title == "Real title"  # the URL placeholder is replaced by the video title
    assert not q.busy


def test_cancel_running_and_waiting_jobs():
    eng = SlowEngine(2)
    q, events = run_queue(eng)
    a = q.add(job("https://x/a"), "a")
    b = q.add(job("https://x/b"), "b")
    time.sleep(0.2)
    q.cancel(b.id)   # waiting: removed at once
    assert b.status == "cancelled"
    q.cancel(a.id)   # running: the engine stops
    q.join(5)
    assert a.status == "cancelled"
    assert eng.order == ["https://x/a"]


def test_queue_restarts_after_going_idle():
    eng = SlowEngine(0.05)
    q, _ = run_queue(eng)
    q.add(job("https://x/1"), "1")
    q.join(5)
    q.add(job("https://x/2"), "2")
    q.join(5)
    assert eng.order == ["https://x/1", "https://x/2"]


def test_engine_crash_becomes_a_failed_job():
    class Boom:
        cancelled = False

        def cancel(self):
            pass

        def download(self, job, on_progress):
            raise RuntimeError("kaboom")

    q, events = run_queue(Boom())
    j = q.add(job("https://x/a"), "a")
    q.join(5)
    assert j.status == "failed"
    assert j.result.failures
