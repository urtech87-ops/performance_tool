"""The execution layer: a scan is queued work, not something a web request does.

Nothing here runs a real scan - the pipeline is stubbed at
`dashboard.run_pipeline`, which is exactly the boundary this change was not
allowed to move. What is tested is everything around it: enqueue/dequeue,
states and queue positions, the full-queue refusal, and the per-scan timeout.
"""

import threading
import time

import pytest

import config
import dashboard
import jobqueue
import tasks


@pytest.fixture
def stub_pipeline(monkeypatch):
    """A stand-in pipeline that records its calls and can be held mid-run."""
    class Stub:
        def __init__(self):
            self.calls = []
            self.started = threading.Semaphore(0)
            self.release = threading.Event()
            self.release.set()
            self.boom = None

        def __call__(self, url, device, samples, deep, max_pages, concurrency,
                     security, categories, log, a11y=False, standards=None,
                     scan_config=None, credentials=None):
            self.calls.append({"url": url, "device": device, "samples": samples,
                               "deep": deep, "max_pages": max_pages,
                               "concurrency": concurrency, "security": security,
                               "categories": categories, "a11y": a11y,
                               "standards": standards})
            self.started.release()
            log(f"stub scanning {url}")
            self.release.wait(5)
            if self.boom:
                raise RuntimeError(self.boom)
            return "stub-run"

    stub = Stub()
    monkeypatch.setattr(dashboard, "run_pipeline", stub)
    monkeypatch.setattr(dashboard, "run_artifacts", lambda name: ["report.html"])
    return stub


def wait_for(predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


PARAMS = {"url": "https://x.test", "device": "desktop", "samples": 1, "deep": True,
          "max_pages": 5, "concurrency": 2, "security": False,
          "categories": ["performance"], "a11y": False, "standards": []}


# --------------------------------------------------------------------------
# enqueue / dequeue
# --------------------------------------------------------------------------

def test_a_queued_job_is_picked_up_and_runs_the_pipeline_once(stub_pipeline):
    backend = jobqueue.InlineBackend(concurrency=1, max_depth=5)
    job = backend.enqueue(PARAMS)

    assert wait_for(lambda: backend.state(job)["state"] == jobqueue.DONE)
    assert len(stub_pipeline.calls) == 1
    assert stub_pipeline.calls[0]["url"] == "https://x.test"
    assert backend.state(job)["result"] == {"base": "/runs/stub-run/",
                                            "files": ["report.html"]}


def test_the_pipeline_receives_the_parameters_unchanged(stub_pipeline):
    backend = jobqueue.InlineBackend(concurrency=1)
    params = dict(PARAMS, a11y=True, standards=["wcag21aa"], security=True,
                  device="mobile", samples=2, max_pages=7, concurrency=3)
    job = backend.enqueue(params)
    assert wait_for(lambda: backend.state(job)["state"] == jobqueue.DONE)

    call = stub_pipeline.calls[0]
    for key in ("device", "samples", "max_pages", "concurrency", "security",
                "categories", "a11y", "standards"):
        assert call[key] == params[key], key


def test_log_lines_stream_out_of_the_job(stub_pipeline):
    backend = jobqueue.InlineBackend(concurrency=1)
    job = backend.enqueue(PARAMS)
    assert wait_for(lambda: backend.state(job)["state"] == jobqueue.DONE)

    lines, cursor = backend.logs(job, 0)
    assert "stub scanning https://x.test" in lines
    assert backend.logs(job, cursor) == ([], cursor)   # the cursor does not repeat


def test_only_worker_concurrency_jobs_run_at_once(stub_pipeline):
    """The whole point: two workers means two scans, however many are waiting."""
    stub_pipeline.release.clear()
    backend = jobqueue.InlineBackend(concurrency=2, max_depth=10)
    jobs = [backend.enqueue(PARAMS) for _ in range(5)]

    assert stub_pipeline.started.acquire(timeout=5)
    assert stub_pipeline.started.acquire(timeout=5)
    time.sleep(0.15)                                   # a third would show up here
    assert len(stub_pipeline.calls) == 2

    states = [backend.state(j)["state"] for j in jobs]
    assert states.count(jobqueue.RUNNING) == 2
    assert states.count(jobqueue.QUEUED) == 3

    stub_pipeline.release.set()
    assert wait_for(lambda: all(backend.state(j)["state"] == jobqueue.DONE
                                for j in jobs), timeout=10)


def test_queue_position_counts_down_as_jobs_are_taken(stub_pipeline):
    stub_pipeline.release.clear()
    backend = jobqueue.InlineBackend(concurrency=1, max_depth=10)
    first = backend.enqueue(PARAMS)
    second = backend.enqueue(PARAMS)
    third = backend.enqueue(PARAMS)

    assert stub_pipeline.started.acquire(timeout=5)     # `first` is running
    assert backend.state(first)["position"] is None
    assert backend.state(second)["position"] == 1
    assert backend.state(third)["position"] == 2

    stub_pipeline.release.set()
    assert wait_for(lambda: backend.state(third)["state"] == jobqueue.DONE, timeout=10)


def test_a_failing_scan_lands_in_the_failed_state(stub_pipeline):
    stub_pipeline.boom = "chrome fell over"
    backend = jobqueue.InlineBackend(concurrency=1)
    job = backend.enqueue(PARAMS)

    assert wait_for(lambda: backend.state(job)["state"] == jobqueue.FAILED)
    state = backend.state(job)
    assert "chrome fell over" in state["error"]
    assert any("chrome fell over" in line for line in backend.logs(job)[0])


def test_an_unknown_job_id_is_not_an_error():
    backend = jobqueue.InlineBackend()
    assert backend.state("nope")["state"] == jobqueue.UNKNOWN


# --------------------------------------------------------------------------
# a full queue is refused, not absorbed
# --------------------------------------------------------------------------

def test_enqueue_raises_when_the_queue_is_full(stub_pipeline):
    stub_pipeline.release.clear()
    backend = jobqueue.InlineBackend(concurrency=1, max_depth=2)
    backend.enqueue(PARAMS)                 # taken by the single worker
    assert stub_pipeline.started.acquire(timeout=5)
    backend.enqueue(PARAMS)                 # waiting
    backend.enqueue(PARAMS)                 # waiting - queue now full

    with pytest.raises(jobqueue.QueueFull):
        backend.enqueue(PARAMS)

    stub_pipeline.release.set()


def test_a_full_queue_never_starts_extra_pipelines(stub_pipeline):
    stub_pipeline.release.clear()
    backend = jobqueue.InlineBackend(concurrency=1, max_depth=1)
    backend.enqueue(PARAMS)
    assert stub_pipeline.started.acquire(timeout=5)
    backend.enqueue(PARAMS)
    for _ in range(5):
        with pytest.raises(jobqueue.QueueFull):
            backend.enqueue(PARAMS)

    assert len(stub_pipeline.calls) == 1     # the refusals cost nothing
    stub_pipeline.release.set()


# --------------------------------------------------------------------------
# per-scan timeout
# --------------------------------------------------------------------------

def test_a_stuck_scan_is_failed_once_it_passes_the_timeout(stub_pipeline):
    stub_pipeline.release.clear()
    backend = jobqueue.InlineBackend(concurrency=1, max_depth=5, timeout=1)
    job = backend.enqueue(PARAMS)
    assert stub_pipeline.started.acquire(timeout=5)
    assert backend.state(job)["state"] == jobqueue.RUNNING

    assert wait_for(lambda: backend.state(job)["state"] == jobqueue.FAILED, timeout=5)
    assert "exceeded" in backend.state(job)["error"]
    stub_pipeline.release.set()


def test_the_timeout_comes_from_the_configured_setting(monkeypatch):
    monkeypatch.setattr(config.settings, "SCAN_TIMEOUT", 42)
    assert jobqueue.InlineBackend().timeout == 42


# --------------------------------------------------------------------------
# the backend the deployment gets
# --------------------------------------------------------------------------

def test_redis_url_selects_the_redis_backend(monkeypatch):
    made = {}

    class FakeRedisBackend:
        name = "redis"

        def __init__(self):
            made["yes"] = True

    monkeypatch.setattr(config.settings, "REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setattr(jobqueue, "RedisQueueBackend", FakeRedisBackend)
    assert jobqueue.make_backend().name == "redis"
    assert made == {"yes": True}


def test_no_redis_url_keeps_the_in_process_queue(monkeypatch):
    monkeypatch.setattr(config.settings, "REDIS_URL", "")
    assert isinstance(jobqueue.make_backend(), jobqueue.InlineBackend)


def test_the_task_wrapper_calls_run_pipeline_and_nothing_else(stub_pipeline):
    """tasks.run_scan_job is the only thing a worker runs: one pipeline call,
    one payload back. It must not reshape the scan."""
    lines = []
    result = tasks.run_scan_job(PARAMS, lines.append)
    assert result == {"base": "/runs/stub-run/", "files": ["report.html"]}
    assert len(stub_pipeline.calls) == 1
    assert lines == ["stub scanning https://x.test"]
