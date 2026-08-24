"""GET /scan and POST /scan: two doors, one set of guards.

`GET /scan` is the original entry point - a scan and its live log in one
request, which is what a script or a `curl` uses. It was kept rather than
removed, on one condition: it must not be a second admission path. It hands
the query string to the same `dashboard._admit` the POST hands its body to,
so `guardrails.sanitize_params`, the hard caps, the rate limiter, both
verification gates and the SSRF check on a DNS override are one
implementation reached two ways.

This file is what makes that a fact rather than a claim: every guard is
exercised through both doors and the two answers are compared. The one
deliberate difference is credentials - the POST body has a door for them and
a query string never will, because a query string reaches access logs,
`Referer` headers and the user's own history.
"""

import json

import pytest

import dashboard
import guardrails
import jobqueue
import verification
from config import settings

SECRET = {"auth_user": "admin", "auth_pass": "hunter2"}


@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


@pytest.fixture
def seen(monkeypatch):
    """Every set of parameters that actually reached the pipeline."""
    calls = []

    def fake_pipeline(url, device, samples, deep, max_pages, concurrency,
                      security, categories, log, a11y=False, standards=None,
                      scan_config=None, credentials=None):
        calls.append({"url": url, "device": device, "samples": samples,
                      "deep": deep, "max_pages": max_pages,
                      "concurrency": concurrency, "security": security,
                      "categories": categories, "a11y": a11y,
                      "standards": standards, "scan_config": scan_config})
        return "run-folder"

    monkeypatch.setattr(dashboard, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(dashboard, "run_artifacts", lambda name: [])
    return calls


def query(fields):
    return "&".join(f"{k}={v}" for k, v in fields.items())


def via_get(client, fields):
    """Run a scan through the GET route; hand back its refusal, if any."""
    resp = client.get("/scan?" + query(fields))
    reason = resp.headers.get("X-Scan-Rejected")
    body = resp.get_data(as_text=True)
    return reason, body


def via_post(client, fields):
    """The same through POST, following the stream to the end.

    Reading the stream body is what makes this synchronous: the job runs on a
    worker thread, and the SSE generator does not return until that thread has
    finished with it.
    """
    resp = client.post("/scan", json=fields)
    if resp.status_code != 200:
        payload = resp.get_json()
        return payload["reason"], payload["message"]
    return None, client.get(resp.get_json()["stream"]).get_data(as_text=True)


def both(client, fields):
    """`(get_reason, post_reason)` for one request - the pair a parity test
    compares. Each runs in its own session, so neither is refused for the
    other's rate-limit budget."""
    get_reason, _ = via_get(client, fields)
    client.delete_cookie(dashboard.SESSION_COOKIE)
    post_reason, _ = via_post(client, fields)
    return get_reason, post_reason


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    """Parity is about admission, not about budgets - and every test here
    submits the same request twice by design."""
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)


# --------------------------------------------------------------------------
# the caps
# --------------------------------------------------------------------------

OVERSIZED = {"url": "https://x.test", "categories": "performance",
             "max_pages": 5000, "samples": 99, "concurrency": 99}


def test_both_doors_clamp_a_scan_to_the_same_size(client, seen):
    via_get(client, OVERSIZED)
    client.delete_cookie(dashboard.SESSION_COOKIE)
    via_post(client, OVERSIZED)

    assert len(seen) == 2
    got, posted = seen
    assert got == posted
    assert got["max_pages"] == settings.CAP_MAX_PAGES
    assert got["samples"] == settings.CAP_SAMPLES
    assert got["concurrency"] == settings.CAP_PARALLEL


def test_both_doors_report_the_same_clamping(client, seen):
    """The `capped` list the client is shown comes from the same place."""
    _, body = via_get(client, OVERSIZED)
    accepted = json.loads(body.split("event: accepted\ndata: ", 1)[1]
                          .split("\n\n", 1)[0])
    client.delete_cookie(dashboard.SESSION_COOKIE)
    posted = client.post("/scan", json=OVERSIZED).get_json()
    assert sorted(accepted["capped"]) == sorted(posted["capped"])
    assert accepted["params"] == posted["params"]


def test_a_lowered_cap_binds_both_doors(client, seen, monkeypatch):
    monkeypatch.setattr(settings, "CAP_MAX_PAGES", 2)
    fields = dict(OVERSIZED, max_pages=40)
    via_get(client, fields)
    client.delete_cookie(dashboard.SESSION_COOKIE)
    via_post(client, fields)
    assert [call["max_pages"] for call in seen] == [2, 2]


# --------------------------------------------------------------------------
# sanitisation
# --------------------------------------------------------------------------

def test_both_doors_sanitise_a_request_identically(client, seen):
    """One request with something wrong in every field, through both doors."""
    fields = {"url": "https://x.test", "device": "nonsense",
              "categories": "performance,made-up,seo", "standards": "WCAG21AA!!",
              "throttling": "hyperspeed", "device_profile": "../../etc/passwd",
              "viewport_width": 99999, "dpr": 400,
              "user_agent": "Bot/1.0", "block_patterns": "*ads*,*ads*,*beacon*",
              "a11y": "1", "security": "1"}
    via_get(client, fields)
    client.delete_cookie(dashboard.SESSION_COOKIE)
    via_post(client, fields)

    assert len(seen) == 2
    got, posted = seen
    assert got == posted
    assert got["device"] == "mobile"                  # unknown device -> default
    assert got["categories"] == ["performance", "seo"]     # junk dropped
    assert got["standards"] == ["wcag21aa"]
    cfg = got["scan_config"]
    assert cfg.throttling == "default" and cfg.device_profile == ""
    assert cfg.width == 3840 and cfg.dpr == 5.0       # clamped, not rejected
    assert cfg.block_patterns == ("*ads*", "*beacon*")


@pytest.mark.parametrize("url,reason", [
    ("file:///etc/passwd", "invalid"),
    ("javascript:alert(1)", "invalid"),
    ("https://user:pw@x.test/", "invalid"),
    ("", "invalid"),
])
def test_both_doors_refuse_the_same_urls(client, seen, url, reason):
    got, posted = both(client, {"url": url, "categories": "performance"})
    assert got == posted == reason
    assert seen == []


# --------------------------------------------------------------------------
# the gates
# --------------------------------------------------------------------------

def test_both_doors_refuse_a_private_dns_target(client, seen):
    got, posted = both(client, {"url": "https://x.test",
                                "dns_ip": "169.254.169.254"})
    assert got == posted == "invalid"
    assert seen == []


def test_both_doors_drop_a_dns_override_the_deployment_does_not_offer(
        client, seen, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_DNS_OVERRIDE", False)
    fields = {"url": "https://x.test", "categories": "performance",
              "dns_host": "x.test", "dns_ip": "203.0.113.9"}
    via_get(client, fields)
    client.delete_cookie(dashboard.SESSION_COOKIE)
    via_post(client, fields)
    assert [call["scan_config"].dns_ip for call in seen] == ["", ""]


def test_both_doors_enforce_the_ownership_gate(client, seen, monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_DOMAIN_VERIFICATION", True)
    monkeypatch.setattr(verification, "is_verified", lambda domain, **kw: False)
    got, posted = both(client, {"url": "https://x.test",
                                "categories": "performance", "max_pages": 5})
    assert got == posted == "unverified"
    assert seen == []


def test_both_doors_enforce_the_rate_limit(client, seen, monkeypatch):
    """Turned back on for this one: a budget spent through one door is spent
    for the other, because they charge the same buckets."""
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "MAX_SCANS_PER_HOUR", 1)
    fields = {"url": "https://x.test", "categories": "performance"}
    assert via_get(client, fields)[0] is None
    assert via_post(client, fields)[0] == "hourly"


def test_both_doors_refuse_a_full_queue(client, seen, monkeypatch):
    class Full(jobqueue.InlineBackend):
        def enqueue(self, *a, **kw):
            raise jobqueue.QueueFull("full")

    jobqueue.set_backend(Full())
    dashboard.reset_services()
    got, posted = both(client, {"url": "https://x.test",
                                "categories": "performance"})
    assert got == posted == "queue_full"


# --------------------------------------------------------------------------
# the one difference, and it is deliberate
# --------------------------------------------------------------------------

def test_only_the_post_body_has_a_door_for_a_credential(client, seen):
    """A query string reaches access logs, Referer headers and the user's own
    history, so GET refuses outright rather than scanning without them."""
    fields = dict(SECRET, url="https://x.test", categories="performance")
    got_reason, got_body = via_get(client, fields)
    assert got_reason == "invalid" and "POST /scan" in got_body
    assert seen == []

    client.delete_cookie(dashboard.SESSION_COOKIE)
    assert via_post(client, fields)[0] is None
    assert len(seen) == 1


def test_the_get_route_is_refused_before_anything_is_charged(client, seen,
                                                             monkeypatch):
    """The credential refusal happens before admission, so it costs the
    client nothing: the next scan still runs."""
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "MAX_SCANS_PER_HOUR", 1)
    via_get(client, dict(SECRET, url="https://x.test", categories="performance"))
    assert via_get(client, {"url": "https://x.test",
                            "categories": "performance"})[0] is None


def test_neither_door_lets_a_credential_into_the_scan_parameters(client):
    """Whatever comes in, `sanitize_params` is the only thing that decides
    what is stored - and it has never had a field for one."""
    params, error, _ = guardrails.sanitize_params(
        dict(SECRET, url="https://x.test"))
    assert error is None
    assert not guardrails.has_secrets(params)


# --------------------------------------------------------------------------
# the structural claim
# --------------------------------------------------------------------------

def test_both_routes_call_the_one_admission_function(monkeypatch, client, seen):
    """Not a behaviour test: the reason every test above holds. If a third
    route ever admits a scan without going through `_admit`, this is what
    should have caught it."""
    seen_sources = []
    real_admit = dashboard._admit

    def spy(source, credentials, sid):
        seen_sources.append(source)
        return real_admit(source, credentials, sid)

    monkeypatch.setattr(dashboard, "_admit", spy)
    via_get(client, {"url": "https://x.test", "categories": "performance"})
    client.delete_cookie(dashboard.SESSION_COOKIE)
    via_post(client, {"url": "https://x.test", "categories": "performance"})
    assert len(seen_sources) == 2


def test_the_stream_route_admits_nothing_at_all(client):
    """`/scan/stream/<job>` carries no scan settings, so there is nothing for
    it to sanitise - which is why it is not a third door."""
    resp = client.get("/scan/stream/nosuchjob?t=wrong&max_pages=9999")
    assert resp.headers["X-Scan-Rejected"] == "invalid"
