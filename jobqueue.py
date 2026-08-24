#!/usr/bin/env python3
"""
Scan job queue
==============
The scan itself is unchanged: a job is still exactly one `run_pipeline` call.
What changed is *where* that call happens - no longer in a thread inside the
web request, but in a bounded pool of workers pulling from a queue.

Two interchangeable backends:

  RedisQueueBackend  production. RQ + Redis; the web process only enqueues and
                     reads state, separate worker processes run the pipeline.
                     Log lines travel through a Redis list so any web process
                     can stream a job started by any other one.
  InlineBackend      local dev and the test suite. A bounded thread pool in the
                     same process; identical API, no Redis needed.

Both expose the same surface:

    enqueue(params, client, credentials) -> job_id      (raises QueueFull)
    state(job_id)           -> dict            state/position/result/error
    logs(job_id, cursor)    -> (lines, cursor)
    wait(job_id, timeout)                      block until there may be more
    depth()                 -> queued jobs

Job states: queued -> running -> done | failed.

Credentials travel apart from the job
-------------------------------------
`params` is the public description of a scan: it is stored in the job record,
read back by any web process, and echoed to the browser. A scan's credentials
are none of those things, so they never go in it.

  * Inline, the queue holds its own copy in this process's memory and hands it
    straight to the worker thread; `state()` cannot return it, because
    `state()` builds its answer field by field. It is a copy because the web
    request wipes its own as soon as the job is queued, and inline that is the
    same process.
  * With Redis, the credentials live in their own key with a short TTL, apart
    from the job hash and from the RQ job's own arguments. The worker takes
    that key with GETDEL - one read, then it is gone - so the window in which
    they exist at all is the time the job spends waiting for a worker.

    That window is real: a Redis with persistence enabled could flush the key
    to its RDB/AOF before the worker takes it. Run the queue's Redis with
    persistence off (`--save "" --appendonly no`) if you accept credentials,
    or set ALLOW_SCAN_AUTH=0 and don't.

    In public mode that stops being advice: `PERSIST_SCAN_AUTH` is forced off,
    and `credential_storage_ok()` then refuses credentials unless the server
    itself reports both RDB and AOF disabled. A deployment that cannot prove
    it - a managed Redis with `CONFIG GET` locked down, say - is told so
    rather than quietly writing somebody's password to a disk it does not own.
"""

import json
import threading
import time
import uuid
from collections import deque

from config import settings

QUEUED, RUNNING, DONE, FAILED, UNKNOWN = "queued", "running", "done", "failed", "unknown"
TERMINAL = (DONE, FAILED, UNKNOWN)


class QueueFull(Exception):
    """The queue is at MAX_QUEUE_DEPTH - the caller should be told to retry."""


class CredentialsRefused(Exception):
    """This queue will not carry a credential - the message says why.

    Raised rather than silently dropping them: a scan that quietly ran as an
    anonymous visitor would report the login page as the site.
    """


# What a backend says when it will not take a credential. The message is the
# operator's to act on, and is shown to the client as-is: it names a setting,
# never a value anybody sent.
CREDENTIALS_OK = (True, "")


def new_job_id():
    return uuid.uuid4().hex[:16]


def _detach(credentials):
    """A private copy of `credentials`, so the caller can wipe its own."""
    if not credentials:
        return None
    import scanauth

    return scanauth.Credentials.from_payload(credentials.payload())


# --------------------------------------------------------------------------
# in-process backend
# --------------------------------------------------------------------------

class InlineBackend:
    """A real queue with a bounded worker pool, inside one process.

    Used by `python dashboard.py` and by the tests. It gives the same states,
    positions and full-queue rejections as the Redis backend, so the serving
    logic above it never branches on which backend is in use.
    """

    name = "inline"

    def __init__(self, concurrency=None, max_depth=None, timeout=None):
        self.concurrency = concurrency or settings.WORKER_CONCURRENCY
        self.max_depth = max_depth or settings.MAX_QUEUE_DEPTH
        self.timeout = timeout or settings.SCAN_TIMEOUT
        self._cond = threading.Condition()
        self._pending = deque()
        self._jobs = {}
        self._workers = []

    # -- worker pool ----------------------------------------------------
    def _ensure_workers(self):
        if self._workers:
            return
        for i in range(self.concurrency):
            t = threading.Thread(target=self._worker_loop, name=f"scan-worker-{i}",
                                 daemon=True)
            t.start()
            self._workers.append(t)

    def _worker_loop(self):
        while True:
            with self._cond:
                while not self._pending:
                    self._cond.wait()
                job_id = self._pending.popleft()
                record = self._jobs.get(job_id)
                if record is None:
                    continue
                record["state"] = RUNNING
                record["started_at"] = time.time()
                self._cond.notify_all()
            self._run(job_id)

    def _run(self, job_id):
        import tasks  # imported late: tasks imports dashboard, which imports us
        sink = InlineLogSink(self, job_id)
        credentials = self.take_credentials(job_id)
        try:
            result = tasks.run_scan_job(self._jobs[job_id]["params"], sink,
                                        credentials=credentials)
            self.finish(job_id, DONE, result=result)
        except Exception as exc:                                  # noqa: BLE001
            sink.append(f"ERROR: {exc}")
            self.finish(job_id, FAILED, error=str(exc))

    def take_credentials(self, job_id):
        """The job's credentials, removed from the queue as they are read."""
        with self._cond:
            record = self._jobs.get(job_id)
            return record.pop("credentials", None) if record else None

    def credential_storage_ok(self):
        """Always. Nothing leaves this process, so there is no disk to reach:
        the credential is an object in memory that the worker thread pops."""
        return CREDENTIALS_OK

    # -- API ------------------------------------------------------------
    def depth(self):
        with self._cond:
            return len(self._pending)

    def enqueue(self, params, client=None, job_id=None, credentials=None):
        with self._cond:
            if len(self._pending) >= self.max_depth:
                raise QueueFull(f"{len(self._pending)} scans already waiting")
            job_id = job_id or new_job_id()
            self._jobs[job_id] = {
                "id": job_id, "state": QUEUED, "params": dict(params),
                "client": client, "created_at": time.time(), "started_at": None,
                "log": [], "result": None, "error": None,
                # A detached copy, held only until the worker thread takes it
                # and never part of anything state() or logs() returns. It is a
                # copy because the web request wipes its own the moment the job
                # is queued, and inline that is the same process.
                "credentials": _detach(credentials),
            }
            self._pending.append(job_id)
            self._cond.notify_all()
        self._ensure_workers()
        return job_id

    def state(self, job_id):
        with self._cond:
            record = self._jobs.get(job_id)
            if record is None:
                return {"id": job_id, "state": UNKNOWN, "position": None,
                        "result": None, "error": "unknown job"}
            state = record["state"]
            if state == RUNNING and record["started_at"] and \
                    time.time() - record["started_at"] > self.timeout:
                # Soft enforcement: a Python thread cannot be killed, so the
                # job is failed for the client here. The Redis backend hands
                # this to RQ, which kills the work horse outright.
                record["state"] = state = FAILED
                record["error"] = f"scan exceeded the {self.timeout}s limit"
                record["log"].append(f"Scan timed out after {self.timeout}s.")
                self._cond.notify_all()
            position = None
            if state == QUEUED:
                try:
                    position = self._pending.index(job_id) + 1
                except ValueError:
                    position = None
            return {"id": job_id, "state": state, "position": position,
                    "result": record["result"], "error": record["error"]}

    def logs(self, job_id, cursor=0):
        with self._cond:
            record = self._jobs.get(job_id)
            if record is None:
                return [], cursor
            lines = record["log"][cursor:]
            return lines, cursor + len(lines)

    def wait(self, job_id, timeout=0.25):
        with self._cond:
            self._cond.wait(timeout)

    # -- used by the worker side ----------------------------------------
    def append_log(self, job_id, line):
        with self._cond:
            record = self._jobs.get(job_id)
            if record is not None:
                record["log"].append(line)
            self._cond.notify_all()

    def finish(self, job_id, state, result=None, error=None):
        with self._cond:
            record = self._jobs.get(job_id)
            if record is not None and record["state"] not in TERMINAL:
                record["state"] = state
                record["result"] = result
                record["error"] = error
            self._cond.notify_all()


class InlineLogSink:
    def __init__(self, backend, job_id):
        self.backend, self.job_id = backend, job_id

    def append(self, line):
        self.backend.append_log(self.job_id, str(line))

    __call__ = append


# --------------------------------------------------------------------------
# Redis / RQ backend
# --------------------------------------------------------------------------

def _config_value(answer, name):
    """One setting out of a `CONFIG GET` answer, or None when it is not in it.

    redis-py hands back `{name: value}` with either half possibly bytes,
    depending on how the connection was built.
    """
    for key, value in (answer or {}).items():
        if (key.decode() if isinstance(key, bytes) else str(key)) == name:
            return (value.decode() if isinstance(value, bytes) else str(value)).strip()
    return None


def _key(job_id, suffix=""):
    return f"scanjob:{job_id}{suffix}"


class RedisLogSink:
    """The `log` callable handed to `run_pipeline` inside a worker process."""

    def __init__(self, redis_conn, job_id, ttl):
        self.r, self.job_id, self.ttl = redis_conn, job_id, ttl

    def append(self, line):
        key = _key(self.job_id, ":log")
        pipe = self.r.pipeline()
        pipe.rpush(key, str(line))
        pipe.expire(key, self.ttl)
        pipe.execute()

    __call__ = append


class RedisQueueBackend:
    """RQ-backed queue. The web process never runs a scan; it only enqueues."""

    name = "redis"

    def __init__(self, redis_conn=None, queue_name=None, max_depth=None, timeout=None):
        import redis as redis_lib
        from rq import Queue

        self.r = redis_conn or redis_lib.Redis.from_url(settings.REDIS_URL)
        self.queue_name = queue_name or settings.QUEUE_NAME
        self.max_depth = max_depth or settings.MAX_QUEUE_DEPTH
        self.timeout = timeout or settings.SCAN_TIMEOUT
        self.ttl = settings.JOB_TTL
        self.queue = Queue(self.queue_name, connection=self.r,
                           default_timeout=self.timeout)
        self._persistence = None      # unasked; see credential_storage_ok

    # -- API ------------------------------------------------------------
    def depth(self):
        return self.queue.count

    def enqueue(self, params, client=None, job_id=None, credentials=None):
        if self.depth() >= self.max_depth:
            raise QueueFull(f"{self.depth()} scans already waiting")
        job_id = job_id or new_job_id()
        record = {"id": job_id, "state": QUEUED, "params": json.dumps(params),
                  "client": client or "", "created_at": str(time.time())}
        pipe = self.r.pipeline()
        pipe.hset(_key(job_id), mapping=record)
        pipe.expire(_key(job_id), self.ttl)
        pipe.execute()
        # Credentials go in their own key, never in the record above and never
        # in the RQ job's arguments: the record is kept for JOB_TTL and read by
        # every web process, and neither is a place for someone's password.
        # This key outlives only the queue wait - see the module docstring.
        if credentials:
            ok, reason = self.credential_storage_ok()
            if not ok:
                raise CredentialsRefused(reason)
            # Long enough for a realistic queue wait, short enough that a job
            # nobody ever runs does not leave a password sitting there. A job
            # that outlives it is told so rather than quietly scanning as an
            # anonymous visitor - see `credentials_expected`.
            self.r.set(_key(job_id, ":cred"), json.dumps(credentials.payload()),
                       ex=self.timeout * 2 + 60)
            self.r.hset(_key(job_id), "auth", "1")
        # The worker imports `tasks` itself - referencing it by name keeps the
        # web process free of any pipeline import.
        self.queue.enqueue("tasks.run_scan_job_rq", job_id, params,
                           job_id=job_id, job_timeout=self.timeout,
                           result_ttl=self.ttl, failure_ttl=self.ttl)
        return job_id

    def take_credentials(self, job_id):
        """Read the job's credentials and delete them in the same breath.

        Returns a `scanauth.Credentials`, empty when the job has none. After
        this call the secret is not in Redis any more, whatever happens to the
        scan.
        """
        import scanauth

        key = _key(job_id, ":cred")
        raw = None
        try:
            raw = self.r.getdel(key)          # Redis 6.2+
        except Exception:                                          # noqa: BLE001
            pipe = self.r.pipeline()
            pipe.get(key)
            pipe.delete(key)
            raw = pipe.execute()[0]
        if not raw:
            return scanauth.Credentials()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            return scanauth.Credentials.from_payload(json.loads(raw))
        except ValueError:
            return scanauth.Credentials()

    def credential_storage_ok(self):
        """Whether a credential may be written to this Redis at all.

        With `PERSIST_SCAN_AUTH` on (the local default) this is the behaviour
        the queue has always had: the key is written, and keeping the server's
        persistence off is the operator's business.

        With it off - which public mode forces - the server has to *say* that
        both RDB snapshots and AOF are disabled before a password is handed
        to it. A Redis that will not answer the question does not get the
        benefit of the doubt: the answer is no, with the reason, because the
        alternative is somebody's password on a disk nobody meant to write it
        to. Asked once per process and remembered - a running Redis does not
        change its persistence between two scans, and this sits in the path of
        every credentialed submission.
        """
        if settings.PERSIST_SCAN_AUTH:
            return CREDENTIALS_OK
        if self._persistence is None:
            self._persistence = self._persistence_state()
        state = self._persistence
        if state == "off":
            return CREDENTIALS_OK
        if state == "unknown":
            return False, ("This deployment will not accept sign-in details "
                           "because it cannot confirm its queue's Redis has "
                           "persistence disabled. Run Redis with --save \"\" "
                           "--appendonly no, or scan without sign-in details.")
        return False, ("This deployment will not accept sign-in details "
                       "because its queue's Redis writes to disk. Run Redis "
                       "with --save \"\" --appendonly no, or scan without "
                       "sign-in details.")

    def _persistence_state(self):
        """"off", "on" or "unknown", from the server's own configuration.

        "unknown" covers every way of not getting an answer: CONFIG disabled,
        renamed or denied, and a server that answers without the setting in
        it. Not answering is not the same as answering "no".
        """
        try:
            save = _config_value(self.r.config_get("save"), "save")
            appendonly = _config_value(self.r.config_get("appendonly"), "appendonly")
        except Exception:                                          # noqa: BLE001
            return "unknown"
        if save is None or appendonly is None:
            return "unknown"
        # An empty `save` is "no RDB snapshots"; `appendonly no` is "no AOF".
        return "off" if not save and appendonly.lower() == "no" else "on"

    def credentials_expected(self, job_id):
        """True when this job was queued with credentials.

        Public, not secret: it says that a login was configured, never what it
        was. A worker compares it with what `take_credentials` actually found,
        so a scan whose credentials expired in the queue reports that instead
        of silently measuring the logged-out site.
        """
        return self._record(job_id).get("auth") == "1"

    def _record(self, job_id):
        raw = self.r.hgetall(_key(job_id))
        return {(k.decode() if isinstance(k, bytes) else k):
                (v.decode() if isinstance(v, bytes) else v) for k, v in raw.items()}

    def state(self, job_id):
        record = self._record(job_id)
        if not record:
            return {"id": job_id, "state": UNKNOWN, "position": None,
                    "result": None, "error": "unknown job"}
        state = record.get("state", UNKNOWN)
        error = record.get("error") or None
        if state not in TERMINAL:
            # RQ is the authority on death: a job killed by job_timeout, or
            # lost with its worker, never gets to write its own record.
            rq_state = self._rq_status(job_id)
            if rq_state in ("failed", "stopped", "canceled"):
                # JobStatus is an enum whose str() is "JobStatus.FAILED".
                label = getattr(rq_state, "value", rq_state)
                error = error or f"scan {label} (limit {self.timeout}s)"
                self.finish(job_id, FAILED, error=error)
                state = FAILED
        result = None
        if record.get("result"):
            try:
                result = json.loads(record["result"])
            except ValueError:
                result = None
        position = self._position(job_id) if state == QUEUED else None
        return {"id": job_id, "state": state, "position": position,
                "result": result, "error": error}

    def _rq_status(self, job_id):
        try:
            from rq.job import Job
            return Job.fetch(job_id, connection=self.r).get_status()
        except Exception:                                          # noqa: BLE001
            return None

    def _position(self, job_id):
        try:
            ids = self.queue.get_job_ids()
        except Exception:                                          # noqa: BLE001
            return None
        try:
            return ids.index(job_id) + 1
        except ValueError:
            return None

    def logs(self, job_id, cursor=0):
        lines = self.r.lrange(_key(job_id, ":log"), cursor, -1) or []
        lines = [l.decode() if isinstance(l, bytes) else l for l in lines]
        return lines, cursor + len(lines)

    def wait(self, job_id, timeout=0.5):
        time.sleep(timeout)

    # -- used by the worker side ----------------------------------------
    def mark_running(self, job_id):
        self.r.hset(_key(job_id), mapping={"state": RUNNING,
                                           "started_at": str(time.time())})
        self.r.expire(_key(job_id), self.ttl)

    def finish(self, job_id, state, result=None, error=None):
        mapping = {"state": state, "finished_at": str(time.time())}
        if result is not None:
            mapping["result"] = json.dumps(result)
        if error:
            mapping["error"] = str(error)
        self.r.hset(_key(job_id), mapping=mapping)
        self.r.expire(_key(job_id), self.ttl)

    def sink(self, job_id):
        return RedisLogSink(self.r, job_id, self.ttl)


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

_backend = None
_backend_lock = threading.Lock()


def make_backend():
    """Redis when REDIS_URL is set, otherwise the in-process pool."""
    if settings.REDIS_URL:
        return RedisQueueBackend()
    return InlineBackend()


def get_backend():
    global _backend
    with _backend_lock:
        if _backend is None:
            _backend = make_backend()
        return _backend


def set_backend(backend):
    """Install a specific backend (tests, or a process that built its own)."""
    global _backend
    with _backend_lock:
        _backend = backend
    return backend
