"""
Gunicorn config for the dashboard.

A scan stream is a long-lived SSE connection that spends nearly all its time
waiting, so the web tier is sized for *connections*, not for CPU: gthread
workers with a generous thread count, and no request timeout that would cut a
stream off mid-scan. The scans themselves are not here - they are in worker.py.
"""

import os

bind = os.environ.get("BIND", "0.0.0.0:8000")
workers = int(os.environ.get("WEB_WORKERS", "2"))
worker_class = os.environ.get("WEB_WORKER_CLASS", "gthread")
threads = int(os.environ.get("WEB_THREADS", "16"))

# SSE streams stay open for the length of a scan; the scan's own limit is
# SCAN_TIMEOUT, enforced by the worker, not by the web tier.
timeout = int(os.environ.get("WEB_TIMEOUT", "0")) or 0
graceful_timeout = 30
keepalive = 65

accesslog = os.environ.get("ACCESS_LOG", "-")
errorlog = os.environ.get("ERROR_LOG", "-")
loglevel = os.environ.get("LOG_LEVEL", "info")
