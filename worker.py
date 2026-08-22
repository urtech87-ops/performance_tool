#!/usr/bin/env python3
"""
Scan worker
===========
Runs WORKER_CONCURRENCY RQ workers against the scan queue. Each one pulls a
job and runs `run_pipeline` exactly as the old in-request thread did - the
difference is that there are only ever this many of them, no matter how many
people click "Generate report".

    REDIS_URL=redis://localhost:6379/0 WORKER_CONCURRENCY=2 python worker.py

RQ enforces the per-scan timeout (SCAN_TIMEOUT) by killing the work horse, so a
site that hangs cannot hold a worker forever.
"""

import logging
import sys

import redis
from rq import Queue, Worker

from config import settings

log = logging.getLogger("scan-worker")


def _connection():
    if not settings.REDIS_URL:
        sys.exit("REDIS_URL is not set - workers need Redis. "
                 "For a single-process local run just use `python dashboard.py`.")
    return redis.Redis.from_url(settings.REDIS_URL)


def run_pool():
    conn = _connection()
    count = settings.WORKER_CONCURRENCY
    log.info("starting %d worker(s) on queue %r (timeout %ds)",
             count, settings.QUEUE_NAME, settings.SCAN_TIMEOUT)
    if count == 1:
        Worker([Queue(settings.QUEUE_NAME, connection=conn)],
               connection=conn).work(with_scheduler=False)
        return
    try:
        from rq.worker_pool import WorkerPool
    except ImportError:
        # Older RQ: one process per worker, supervised by the caller
        # (systemd/compose `replicas`) rather than by us.
        sys.exit("This RQ has no WorkerPool. Start WORKER_CONCURRENCY separate "
                 "`rq worker` processes instead, or upgrade RQ.")
    WorkerPool([Queue(settings.QUEUE_NAME, connection=conn)],
               connection=conn, num_workers=count).start(burst=False)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run_pool()
