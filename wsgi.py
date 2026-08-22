#!/usr/bin/env python3
"""
WSGI entry point
================
    gunicorn -c gunicorn.conf.py wsgi:app

Gunicorn serves the app; the scans themselves happen in worker.py processes.
Because the scan stream is Server-Sent Events, the web process needs a worker
class that can hold many idle connections at once - see gunicorn.conf.py.
"""

import logging

from config import settings
from dashboard import app

if not settings.REDIS_URL:
    logging.getLogger("dashboard").warning(
        "REDIS_URL is not set: this process is using its own in-memory queue. "
        "Every gunicorn worker would then have a separate queue and separate "
        "rate-limit counters. Set REDIS_URL and run worker.py for anything "
        "public or multi-process.")

application = app

__all__ = ["app", "application"]
