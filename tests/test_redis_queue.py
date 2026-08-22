"""The production path: RQ + Redis, exercised end to end against fakeredis.

A job goes onto a real RQ queue, a real RQ worker takes it off, and the state,
log and result the browser reads all come back through Redis - which is what
lets one gunicorn process stream a scan another one accepted.
"""

import logging

import pytest

import dashboard
import jobqueue

fakeredis = pytest.importorskip("fakeredis")
rq = pytest.importorskip("rq")


PARAMS = {"url": "https://x.test", "device": "desktop", "samples": 1, "deep": True,
          "max_pages": 3, "concurrency": 2, "security": False,
          "categories": ["performance"], "a11y": False, "standards": []}


@pytest.fixture
def redis_conn():
    return fakeredis.FakeStrictRedis()


@pytest.fixture
def backend(redis_conn):
    return jobqueue.RedisQueueBackend(redis_conn=redis_conn, queue_name="test-scans",
                                      max_depth=3, timeout=60)


@pytest.fixture
def stub_pipeline(monkeypatch):
    calls = []

    def fake_pipeline(url, device, samples, deep, max_pages, concurrency,
                      security, categories, log, a11y=False, standards=None):
        calls.append({"url": url, "max_pages": max_pages, "concurrency": concurrency})
        log("crawling " + url)
        return "stub-run"

    monkeypatch.setattr(dashboard, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(dashboard, "run_artifacts", lambda name: ["report.html"])
    return calls


def drain(redis_conn, queue_name="test-scans"):
    """Run every queued job in-process, the way an `rq worker` process would."""
    logging.disable(logging.CRITICAL)
    try:
        worker = rq.SimpleWorker([rq.Queue(queue_name, connection=redis_conn)],
                                 connection=redis_conn)
        worker.work(burst=True, logging_level="CRITICAL")
    finally:
        logging.disable(logging.NOTSET)


def test_a_job_survives_the_trip_through_redis(backend, redis_conn, stub_pipeline):
    job = backend.enqueue(PARAMS)
    assert backend.state(job)["state"] == jobqueue.QUEUED
    assert backend.state(job)["position"] == 1
    assert stub_pipeline == []                     # the web side ran nothing

    drain(redis_conn)

    state = backend.state(job)
    assert state["state"] == jobqueue.DONE
    assert state["result"] == {"base": "/runs/stub-run/", "files": ["report.html"]}
    assert stub_pipeline == [{"url": "https://x.test", "max_pages": 3, "concurrency": 2}]


def test_log_lines_come_back_through_redis(backend, redis_conn, stub_pipeline):
    job = backend.enqueue(PARAMS)
    drain(redis_conn)
    lines, cursor = backend.logs(job)
    assert lines == ["crawling https://x.test"]
    assert backend.logs(job, cursor) == ([], cursor)


def test_positions_reflect_the_order_jobs_were_accepted(backend, stub_pipeline):
    jobs = [backend.enqueue(PARAMS) for _ in range(3)]
    assert [backend.state(j)["position"] for j in jobs] == [1, 2, 3]


def test_the_redis_queue_refuses_work_past_its_depth(backend, stub_pipeline):
    for _ in range(3):
        backend.enqueue(PARAMS)
    with pytest.raises(jobqueue.QueueFull):
        backend.enqueue(PARAMS)


def test_a_crashing_worker_leaves_a_failed_job_not_a_stuck_one(backend, redis_conn,
                                                               monkeypatch):
    """Nothing writes 'failed' when a work horse is killed on timeout, so the
    backend has to reconcile RQ's own view of the job."""
    def boom(*a, **kw):
        raise RuntimeError("chrome vanished")

    monkeypatch.setattr(dashboard, "run_pipeline", boom)
    job = backend.enqueue(PARAMS)
    drain(redis_conn)

    state = backend.state(job)
    assert state["state"] == jobqueue.FAILED
    assert "chrome vanished" in state["error"]


def test_the_job_timeout_is_handed_to_rq(backend, redis_conn, stub_pipeline):
    """The hard stop lives with RQ, which can actually kill a stuck scan."""
    job_id = backend.enqueue(PARAMS)
    job = rq.job.Job.fetch(job_id, connection=redis_conn)
    assert job.timeout == 60
    assert job.func_name == "tasks.run_scan_job_rq"
