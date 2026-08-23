#!/usr/bin/env python3
"""
The unit of work a worker runs
==============================
One job = one `run_pipeline` call, with exactly the arguments it has always
taken. Nothing in this module inspects, tunes or post-processes a scan: it
hands the parameters over, streams the pipeline's log lines to whichever sink
the backend provided, and reports the run folder back.

Kept separate from dashboard.py so an RQ worker can import it without ever
importing Flask's routes.
"""

import dashboard


def run_scan_job(params, log):
    """Run the pipeline and return the payload the UI needs.

    `params` is the sanitized dict from guardrails.sanitize_params; `log` is a
    callable taking one line of text.
    """
    run_name = dashboard.run_pipeline(
        params["url"],
        params["device"],
        params["samples"],
        params["deep"],
        params["max_pages"],
        params["concurrency"],
        params["security"],
        params["categories"],
        log,
        a11y=params.get("a11y", False),
        standards=params.get("standards") or None,
    )
    if not run_name:
        raise RuntimeError("the scan produced no run folder")
    # The payload the browser has always received on `event: done`, plus - for
    # a run that recorded one - how much of the site it actually covers. A run
    # folder without a ledger (an older run, or a stubbed pipeline) sends the
    # original two keys and nothing else.
    payload = {"base": f"/runs/{run_name}/",
               "files": dashboard.run_artifacts(run_name)}
    status = dashboard.run_status(run_name) or {}
    if status.get("status"):
        payload["status"] = status["status"]
        payload["coverage"] = status.get("coverage")
        payload["notice"] = status.get("notice")
    return payload


def _worker_backend():
    """The queue backend as seen from inside a worker.

    Reuse the connection RQ handed this job when there is one, so the worker
    writes state and log lines through the exact same Redis it was dispatched
    from; fall back to REDIS_URL otherwise.
    """
    from jobqueue import RedisQueueBackend

    try:
        from rq import get_current_job

        job = get_current_job()
        if job is not None and job.connection is not None:
            return RedisQueueBackend(redis_conn=job.connection)
    except Exception:                                               # noqa: BLE001
        pass
    return RedisQueueBackend()


def run_scan_job_rq(job_id, params):
    """RQ entry point: same work, plus the bookkeeping the web process reads.

    RQ enforces the per-scan timeout by killing this process, so the state a
    dying job leaves behind is reconciled by the backend, not here.
    """
    from jobqueue import DONE, FAILED

    backend = _worker_backend()
    sink = backend.sink(job_id)
    backend.mark_running(job_id)
    try:
        result = run_scan_job(params, sink)
    except Exception as exc:                                        # noqa: BLE001
        sink.append(f"ERROR: {exc}")
        backend.finish(job_id, FAILED, error=str(exc))
        raise
    backend.finish(job_id, DONE, result=result)
    return result
