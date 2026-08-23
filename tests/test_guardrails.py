"""What the server does with a request it does not trust.

Caps, rate limits, the full-queue refusal and the domain-ownership gate, both
at the unit level and through the /scan route the browser actually hits.
"""

import json
import threading
import time

import pytest

import config
import dashboard
import guardrails
import jobqueue
import verification


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


@pytest.fixture
def instant_pipeline(monkeypatch):
    """A pipeline that returns immediately; records what it was asked for."""
    calls = []

    def fake_pipeline(url, device, samples, deep, max_pages, concurrency,
                      security, categories, log, a11y=False, standards=None,
                      scan_config=None, credentials=None):
        calls.append({"url": url, "samples": samples, "max_pages": max_pages,
                      "concurrency": concurrency})
        return "run-folder"

    monkeypatch.setattr(dashboard, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(dashboard, "run_artifacts", lambda name: [])
    return calls


def events(body):
    """Parse an SSE body into [(event_name, data), ...]; plain lines are 'message'."""
    out = []
    for chunk in body.split("\n\n"):
        name, data = "message", []
        for line in chunk.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
        if data:
            out.append((name, "\n".join(data)))
    return out


def first_event(body, name):
    for event, data in events(body):
        if event == name:
            return json.loads(data)
    return None


def event_names(body):
    return [name for name, _ in events(body)]


# --------------------------------------------------------------------------
# hard caps: what the client asks for never decides the size of a scan
# --------------------------------------------------------------------------

def test_caps_clamp_everything_the_client_oversends(monkeypatch):
    monkeypatch.setattr(config.settings, "CAP_MAX_PAGES", 10)
    monkeypatch.setattr(config.settings, "CAP_SAMPLES", 2)
    monkeypatch.setattr(config.settings, "CAP_PARALLEL", 3)

    params, error, capped = guardrails.sanitize_params({
        "url": "https://x.test", "max_pages": "5000", "samples": "99",
        "concurrency": "64"})

    assert error is None
    assert (params["max_pages"], params["samples"], params["concurrency"]) == (10, 2, 3)
    assert set(capped) == {"max_pages", "samples", "parallel"}


def test_values_under_the_cap_are_left_alone(monkeypatch):
    monkeypatch.setattr(config.settings, "CAP_MAX_PAGES", 10)
    params, _, capped = guardrails.sanitize_params({"url": "https://x.test",
                                                    "max_pages": "4"})
    assert params["max_pages"] == 4
    assert capped == []


@pytest.mark.parametrize("raw", ["-5", "0", "abc", "", "1e9", "3.7", None])
def test_junk_numbers_fall_back_to_a_sane_value(raw):
    params, error, _ = guardrails.sanitize_params({"url": "https://x.test",
                                                   "max_pages": raw})
    assert error is None
    assert 1 <= params["max_pages"] <= config.settings.CAP_MAX_PAGES


def test_the_scan_route_enforces_the_caps_the_client_ignores(client, instant_pipeline,
                                                             monkeypatch):
    """The client is free to send anything - the pipeline still gets the cap."""
    monkeypatch.setattr(config.settings, "CAP_MAX_PAGES", 6)
    monkeypatch.setattr(config.settings, "CAP_SAMPLES", 1)
    monkeypatch.setattr(config.settings, "CAP_PARALLEL", 2)

    body = client.get("/scan?url=https://x.test&categories=performance"
                      "&max_pages=200&samples=5&concurrency=6").get_data(as_text=True)

    assert "event: done" in body
    assert instant_pipeline[0]["max_pages"] == 6
    assert instant_pipeline[0]["samples"] == 1
    assert instant_pipeline[0]["concurrency"] == 2
    # and the user is told, rather than silently given a smaller scan
    accepted = first_event(body, "accepted")
    assert set(accepted["capped"]) == {"max_pages", "samples", "parallel"}
    assert "Server limits applied" in body


def test_a_url_that_is_not_http_is_refused(client):
    body = client.get("/scan?url=file:///etc/passwd").get_data(as_text=True)
    assert first_event(body, "rejected")["reason"] == "invalid"


def test_a_url_with_credentials_is_refused(client):
    body = client.get("/scan?url=https://user:pw@x.test/").get_data(as_text=True)
    assert first_event(body, "rejected")["reason"] == "invalid"


def test_a_bare_hostname_still_gets_a_scheme():
    params, error, _ = guardrails.sanitize_params({"url": " x.test/page "})
    assert error is None and params["url"] == "https://x.test/page"


# --------------------------------------------------------------------------
# a full queue is refused politely, and costs nothing
# --------------------------------------------------------------------------

class FullQueue:
    """A backend that is always full - what a busy public deployment looks
    like at the moment a request arrives."""

    name = "inline"
    max_depth = 3

    def __init__(self):
        self.attempts = 0

    def enqueue(self, params, client=None, job_id=None, credentials=None):
        self.attempts += 1
        raise jobqueue.QueueFull("full")

    def depth(self):
        return self.max_depth

    def state(self, job_id):
        return {"id": job_id, "state": jobqueue.UNKNOWN, "position": None,
                "result": None, "error": None}


def test_a_full_queue_is_rejected_cleanly(client, instant_pipeline):
    backend = FullQueue()
    jobqueue.set_backend(backend)
    dashboard.reset_services()

    resp = client.get("/scan?url=https://x.test&categories=performance")
    body = resp.get_data(as_text=True)
    rejected = first_event(body, "rejected")

    assert rejected["reason"] == "queue_full"
    assert "try again shortly" in rejected["message"].lower()
    assert resp.headers["X-Scan-Rejected"] == "queue_full"
    assert backend.attempts == 1
    assert instant_pipeline == []                 # nothing ran
    assert "event: fail" not in body              # a refusal is not a failure


def test_a_refused_scan_is_not_charged_to_the_client(client, instant_pipeline,
                                                     monkeypatch):
    """A full queue must not eat the hourly budget - the user got nothing."""
    monkeypatch.setattr(config.settings, "MAX_SCANS_PER_HOUR", 2)
    jobqueue.set_backend(FullQueue())
    dashboard.reset_services()
    for _ in range(4):
        client.get("/scan?url=https://x.test&categories=performance")

    jobqueue.set_backend(jobqueue.InlineBackend())
    dashboard.reset_services()
    body = client.get("/scan?url=https://x.test&categories=performance").get_data(as_text=True)
    assert "event: done" in body


def test_the_health_endpoint_reports_queue_depth(client):
    payload = client.get("/healthz").get_json()
    assert payload["ok"] is True
    assert payload["depth"] == 0
    assert payload["max_depth"] == config.settings.MAX_QUEUE_DEPTH


# --------------------------------------------------------------------------
# per-client rate limits
# --------------------------------------------------------------------------

def limiter(finished=True):
    return guardrails.RateLimiter(guardrails.MemoryLimitStore(),
                                  is_finished=lambda job_id: finished)


def test_scans_per_hour_is_enforced(monkeypatch):
    monkeypatch.setattr(config.settings, "MAX_SCANS_PER_HOUR", 3)
    monkeypatch.setattr(config.settings, "MAX_CONCURRENT_SCANS", 10)
    rl = limiter()
    keys = guardrails.client_keys("1.2.3.4", "sess")

    for n in range(3):
        rl.check(keys)
        rl.charge(keys, f"job{n}")

    with pytest.raises(guardrails.RateLimitError) as exc:
        rl.check(keys)
    assert exc.value.reason == "hourly"


def test_the_hourly_window_expires(monkeypatch):
    monkeypatch.setattr(config.settings, "MAX_SCANS_PER_HOUR", 1)
    monkeypatch.setattr(config.settings, "MAX_CONCURRENT_SCANS", 10)
    monkeypatch.setattr(config.settings, "RATE_LIMIT_WINDOW", 3600)
    rl = limiter()
    keys = guardrails.client_keys("1.2.3.4", None)
    now = time.time()

    rl.check(keys, now=now)
    rl.charge(keys, "job1", now=now)
    with pytest.raises(guardrails.RateLimitError):
        rl.check(keys, now=now + 10)
    rl.check(keys, now=now + 3601)                # the window has rolled over


def test_concurrent_scans_per_client_is_enforced(monkeypatch):
    monkeypatch.setattr(config.settings, "MAX_CONCURRENT_SCANS", 1)
    monkeypatch.setattr(config.settings, "MAX_SCANS_PER_HOUR", 100)
    rl = limiter(finished=False)                  # the first job is still running
    keys = guardrails.client_keys("1.2.3.4", "sess")

    rl.check(keys)
    rl.charge(keys, "job1")
    with pytest.raises(guardrails.RateLimitError) as exc:
        rl.check(keys)
    assert exc.value.reason == "concurrent"

    rl.release(keys, "job1")
    rl.check(keys)                                # the slot is free again


def test_a_finished_job_never_holds_a_slot(monkeypatch):
    """A worker that dies mid-scan must not lock its client out forever."""
    monkeypatch.setattr(config.settings, "MAX_CONCURRENT_SCANS", 1)
    monkeypatch.setattr(config.settings, "MAX_SCANS_PER_HOUR", 100)
    rl = limiter(finished=True)                   # the job is over, nobody released it
    keys = guardrails.client_keys("1.2.3.4", "sess")
    rl.charge(keys, "ghost")
    rl.check(keys)


def test_ip_and_session_are_separate_buckets(monkeypatch):
    monkeypatch.setattr(config.settings, "MAX_SCANS_PER_HOUR", 1)
    monkeypatch.setattr(config.settings, "MAX_CONCURRENT_SCANS", 10)
    rl = limiter()
    rl.charge(guardrails.client_keys("1.2.3.4", "sess-a"), "job1")

    # same session behind a different IP is still over its own budget
    with pytest.raises(guardrails.RateLimitError):
        rl.check(guardrails.client_keys("9.9.9.9", "sess-a"))
    # a different visitor on a different IP is untouched
    rl.check(guardrails.client_keys("9.9.9.9", "sess-b"))


def test_limits_can_be_switched_off_entirely(monkeypatch):
    monkeypatch.setattr(config.settings, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(config.settings, "MAX_SCANS_PER_HOUR", 1)
    rl = limiter(finished=False)
    keys = guardrails.client_keys("1.2.3.4", "sess")
    for n in range(20):
        rl.check(keys)
        rl.charge(keys, f"job{n}")


def test_the_route_refuses_a_client_over_its_hourly_budget(client, instant_pipeline,
                                                           monkeypatch):
    monkeypatch.setattr(config.settings, "MAX_SCANS_PER_HOUR", 2)
    monkeypatch.setattr(config.settings, "MAX_CONCURRENT_SCANS", 5)

    for _ in range(2):
        client.get("/scan?url=https://x.test&categories=performance").get_data()
    resp = client.get("/scan?url=https://x.test&categories=performance")
    rejected = first_event(resp.get_data(as_text=True), "rejected")

    assert rejected["reason"] == "hourly"
    assert resp.headers["X-Scan-Rejected"] == "hourly"
    assert len(instant_pipeline) == 2            # the third never reached a worker


def test_the_route_refuses_a_second_concurrent_scan(client, monkeypatch):
    monkeypatch.setattr(config.settings, "MAX_CONCURRENT_SCANS", 1)
    monkeypatch.setattr(config.settings, "MAX_SCANS_PER_HOUR", 50)
    held = threading.Event()
    monkeypatch.setattr(dashboard, "run_pipeline",
                        lambda *a, **kw: held.wait(5) or "run-folder")
    monkeypatch.setattr(dashboard, "run_artifacts", lambda name: [])

    started = threading.Thread(
        target=lambda: client.get("/scan?url=https://x.test&categories=performance")
        .get_data())
    started.start()
    try:
        deadline = time.time() + 5
        while jobqueue.get_backend().depth() == 0 and \
                not any(s["state"] == jobqueue.RUNNING
                        for s in [jobqueue.get_backend().state(i)
                                  for i in getattr(jobqueue.get_backend(), "_jobs", {})]):
            if time.time() > deadline:
                break
            time.sleep(0.01)
        body = client.get("/scan?url=https://y.test&categories=performance").get_data(as_text=True)
        assert first_event(body, "rejected")["reason"] == "concurrent"
    finally:
        held.set()
        started.join(10)


# --------------------------------------------------------------------------
# queue feedback reaches the browser
# --------------------------------------------------------------------------

def test_the_stream_reports_queue_position_then_running_then_done(client, monkeypatch):
    monkeypatch.setattr(config.settings, "MAX_CONCURRENT_SCANS", 5)
    gate = threading.Event()

    def slow_pipeline(url, *a, **kw):
        log = a[7]
        gate.wait(5)
        log("scanning " + url)
        return "run-folder"

    monkeypatch.setattr(dashboard, "run_pipeline", slow_pipeline)
    monkeypatch.setattr(dashboard, "run_artifacts", lambda name: ["report.html"])
    jobqueue.set_backend(jobqueue.InlineBackend(concurrency=1, max_depth=5))
    dashboard.reset_services()

    bodies = {}

    def scan(key, url):
        bodies[key] = client.get(f"/scan?url={url}&categories=performance").get_data(as_text=True)

    first = threading.Thread(target=scan, args=("first", "https://a.test"))
    first.start()
    time.sleep(0.3)                          # the worker is now busy with `first`
    second = threading.Thread(target=scan, args=("second", "https://b.test"))
    second.start()
    time.sleep(0.3)
    gate.set()
    first.join(10)
    second.join(10)

    queued = first_event(bodies["second"], "queued")
    assert queued["position"] == 1
    names = event_names(bodies["second"])
    assert names.index("queued") < names.index("running") < names.index("done")
    assert "scanning https://b.test" in bodies["second"]


def test_a_failed_job_streams_a_fail_event_with_the_reason(client, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("lighthouse crashed")

    monkeypatch.setattr(dashboard, "run_pipeline", boom)
    body = client.get("/scan?url=https://x.test&categories=performance").get_data(as_text=True)
    assert first_event(body, "fail")["message"] == "lighthouse crashed"


def test_job_state_is_readable_after_the_stream_ends(client, instant_pipeline):
    resp = client.get("/scan?url=https://x.test&categories=performance")
    resp.get_data()
    job_id = resp.headers["X-Scan-Job"]
    assert client.get(f"/api/job/{job_id}").get_json()["state"] == jobqueue.DONE


def test_the_limits_endpoint_publishes_the_caps(client, monkeypatch):
    monkeypatch.setattr(config.settings, "CAP_MAX_PAGES", 7)
    payload = client.get("/api/limits").get_json()
    assert payload["max_pages"] == 7
    assert payload["queue_depth"] == config.settings.MAX_QUEUE_DEPTH


# --------------------------------------------------------------------------
# domain-ownership gate
# --------------------------------------------------------------------------

def test_the_gate_is_off_by_default(monkeypatch):
    monkeypatch.setattr(config.settings, "REQUIRE_DOMAIN_VERIFICATION", False)
    allowed, detail = verification.gate("x.test", multipage=True)
    assert allowed and detail is None


def test_a_single_page_scan_never_needs_verification(monkeypatch):
    monkeypatch.setattr(config.settings, "REQUIRE_DOMAIN_VERIFICATION", True)
    monkeypatch.setattr(verification, "is_verified",
                        lambda *a, **kw: pytest.fail("must not be checked"))
    allowed, _ = verification.gate("x.test", multipage=False)
    assert allowed


def test_a_multipage_scan_of_an_unverified_domain_is_blocked(monkeypatch):
    monkeypatch.setattr(config.settings, "REQUIRE_DOMAIN_VERIFICATION", True)
    monkeypatch.setattr(verification, "is_verified", lambda *a, **kw: False)
    allowed, detail = verification.gate("x.test", multipage=True)

    assert not allowed
    assert detail["token"] == verification.token_for("x.test")
    assert detail["meta_tag"].startswith("<meta name=")
    assert detail["dns_record"].endswith(detail["token"])


def test_max_pages_one_is_what_makes_a_scan_single_page():
    assert guardrails.is_multipage_scan({"max_pages": 2})
    assert not guardrails.is_multipage_scan({"max_pages": 1})


def test_a_verified_domain_passes(monkeypatch):
    monkeypatch.setattr(config.settings, "REQUIRE_DOMAIN_VERIFICATION", True)
    monkeypatch.setattr(verification, "is_verified", lambda *a, **kw: True)
    allowed, detail = verification.gate("x.test", multipage=True)
    assert allowed and detail is None


def test_the_meta_tag_proves_ownership(monkeypatch):
    token = verification.token_for("x.test")
    page = f'<html><head><meta name="site-audit-verification" content="{token}">'
    monkeypatch.setattr(verification, "_fetch", lambda url, timeout: page)
    monkeypatch.setattr(verification, "_txt_records", lambda d, t: [])
    assert verification.is_verified("x.test", use_cache=False)


def test_a_meta_tag_with_the_wrong_token_proves_nothing(monkeypatch):
    page = '<html><head><meta name="site-audit-verification" content="sav-not-it">'
    monkeypatch.setattr(verification, "_fetch", lambda url, timeout: page)
    monkeypatch.setattr(verification, "_txt_records", lambda d, t: [])
    assert not verification.is_verified("x.test", use_cache=False)


def test_a_dns_txt_record_proves_ownership(monkeypatch):
    token = verification.token_for("x.test")
    monkeypatch.setattr(verification, "_txt_records",
                        lambda d, t: ["v=spf1 -all", f"site-audit-verification={token}"])
    monkeypatch.setattr(verification, "_fetch",
                        lambda url, timeout: pytest.fail("DNS already settled it"))
    assert verification.is_verified("x.test", use_cache=False)


def test_a_domain_gets_a_token_of_its_own():
    assert verification.token_for("a.test") != verification.token_for("b.test")
    assert verification.token_for("a.test") == verification.token_for("A.TEST")


def test_the_route_blocks_a_multipage_scan_and_hands_back_the_instructions(
        client, instant_pipeline, monkeypatch):
    monkeypatch.setattr(config.settings, "REQUIRE_DOMAIN_VERIFICATION", True)
    monkeypatch.setattr(verification, "is_verified", lambda *a, **kw: False)

    body = client.get("/scan?url=https://x.test&categories=performance"
                      "&max_pages=30").get_data(as_text=True)
    rejected = first_event(body, "rejected")

    assert rejected["reason"] == "unverified"
    assert rejected["verification"]["token"] == verification.token_for("x.test")
    assert instant_pipeline == []


def test_the_route_still_allows_the_single_page_scan_of_that_domain(
        client, instant_pipeline, monkeypatch):
    monkeypatch.setattr(config.settings, "REQUIRE_DOMAIN_VERIFICATION", True)
    monkeypatch.setattr(verification, "is_verified", lambda *a, **kw: False)

    body = client.get("/scan?url=https://x.test&categories=performance"
                      "&max_pages=1").get_data(as_text=True)
    assert "event: done" in body
    assert len(instant_pipeline) == 1


def test_the_verify_endpoint_hands_out_the_token(client, monkeypatch):
    monkeypatch.setattr(config.settings, "REQUIRE_DOMAIN_VERIFICATION", True)
    monkeypatch.setattr(verification, "is_verified", lambda *a, **kw: False)
    payload = client.get("/api/verify?url=https://www.x.test/page").get_json()

    assert payload["domain"] == "x.test"          # www and path are not the domain
    assert payload["token"] == verification.token_for("x.test")
    assert payload["verified"] is False
    assert payload["required"] is True
