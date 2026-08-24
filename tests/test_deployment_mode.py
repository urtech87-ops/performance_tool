"""DEPLOYMENT_MODE: one setting that decides the security posture.

An operator hardening this tool should not have to get five booleans right.
`DEPLOYMENT_MODE=public` is the whole configuration, and every flag it governs
is *forced* rather than defaulted - an environment that also sets the
individual variable does not get its way.

What is pinned here:

  1. the defaults of both modes, and that `public` cannot be loosened;
  2. that an unrecognised value reads as `public`, so a typo in the one
     variable that matters buys the strict posture and not the loose one;
  3. the three things public mode actually does - no DNS override, no
     credential written to a Redis that might keep it, no credentials for a
     domain nobody has proved they own;
  4. that /api/limits says all of it, so the UI can hide what would only be
     refused.
"""

import pytest

import config
import dashboard
import guardrails
import jobqueue
import scanauth
import verification
from config import settings


@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


@pytest.fixture
def instant_pipeline(monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard, "run_pipeline",
                        lambda *a, **kw: calls.append(kw) or "run-folder")
    monkeypatch.setattr(dashboard, "run_artifacts", lambda name: [])
    return calls


def built(**env):
    """A Settings built from an environment, without touching the live one."""
    return config.Settings(env)


CREDENTIALS = {"auth_user": "admin", "auth_pass": "hunter2"}


# --------------------------------------------------------------------------
# the mode itself
# --------------------------------------------------------------------------

def test_local_is_the_default():
    assert built().DEPLOYMENT_MODE == config.MODE_LOCAL
    assert built(DEPLOYMENT_MODE="").DEPLOYMENT_MODE == config.MODE_LOCAL


def test_local_keeps_every_convenience():
    s = built(DEPLOYMENT_MODE="local")
    assert s.ALLOW_DNS_OVERRIDE is True
    assert s.ALLOW_SCAN_AUTH is True
    assert s.PERSIST_SCAN_AUTH is True
    assert s.REQUIRE_VERIFIED_DOMAIN_FOR_AUTH is False
    # The one thing local does not hand out by default: an override into its
    # own network still has to be asked for.
    assert s.ALLOW_PRIVATE_DNS_TARGETS is False


def test_public_forces_the_posture():
    s = built(DEPLOYMENT_MODE="public")
    assert s.ALLOW_DNS_OVERRIDE is False
    assert s.PERSIST_SCAN_AUTH is False
    assert s.REQUIRE_VERIFIED_DOMAIN_FOR_AUTH is True
    assert s.ALLOW_PRIVATE_DNS_TARGETS is False


def test_public_cannot_be_loosened_one_variable_at_a_time():
    """The whole point of the mode: an environment that sets the individual
    flags the permissive way does not get them."""
    s = built(DEPLOYMENT_MODE="public",
              ALLOW_DNS_OVERRIDE="1",
              ALLOW_PRIVATE_DNS_TARGETS="1",
              PERSIST_SCAN_AUTH="1",
              REQUIRE_VERIFIED_DOMAIN_FOR_AUTH="0")
    assert s.ALLOW_DNS_OVERRIDE is False
    assert s.ALLOW_PRIVATE_DNS_TARGETS is False
    assert s.PERSIST_SCAN_AUTH is False
    assert s.REQUIRE_VERIFIED_DOMAIN_FOR_AUTH is True


def test_public_can_still_be_tightened_further():
    """Forcing is one-directional: it closes things, it never opens them."""
    s = built(DEPLOYMENT_MODE="public", ALLOW_SCAN_AUTH="0")
    assert s.ALLOW_SCAN_AUTH is False


def test_local_can_be_tightened_by_hand():
    """A local deployment that wants one of the public rules can have it."""
    s = built(DEPLOYMENT_MODE="local", ALLOW_DNS_OVERRIDE="0",
              REQUIRE_VERIFIED_DOMAIN_FOR_AUTH="1")
    assert s.ALLOW_DNS_OVERRIDE is False
    assert s.REQUIRE_VERIFIED_DOMAIN_FOR_AUTH is True


@pytest.mark.parametrize("value", ["publik", "PUBLIC", "prod", "Public", "1", "yes"])
def test_anything_unrecognised_reads_as_public(value):
    """A typo in the setting that decides the posture must not buy the loose
    one. `local` is spelled exactly one way; everything else is public."""
    s = built(DEPLOYMENT_MODE=value)
    assert s.DEPLOYMENT_MODE == config.MODE_PUBLIC
    assert s.ALLOW_DNS_OVERRIDE is False


@pytest.mark.parametrize("value", ["local", "LOCAL", " local ", "Local"])
def test_local_is_recognised_however_it_is_spelled(value):
    assert built(DEPLOYMENT_MODE=value).DEPLOYMENT_MODE == config.MODE_LOCAL


# --------------------------------------------------------------------------
# what public mode does to a request
# --------------------------------------------------------------------------

def test_public_mode_drops_a_dns_override(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_DNS_OVERRIDE", False)
    params, error, _ = guardrails.sanitize_params(
        {"url": "https://x.test", "dns_host": "x.test", "dns_ip": "203.0.113.9"})
    assert error is None
    assert params["scan_config"]["dns_ip"] == ""


def test_public_mode_wants_ownership_before_it_takes_a_password(monkeypatch):
    """Somebody else's password aimed at somebody else's site is the one
    request a deployment open to strangers should not take on trust."""
    monkeypatch.setattr(settings, "REQUIRE_VERIFIED_DOMAIN_FOR_AUTH", True)
    monkeypatch.setattr(verification, "is_verified", lambda domain, **kw: False)
    allowed, detail = verification.credential_gate("x.test", credentialed=True)
    assert allowed is False
    assert "x.test" in detail["message"] and detail["token"].startswith("sav-")


def test_the_credential_gate_ignores_a_scan_without_credentials(monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_VERIFIED_DOMAIN_FOR_AUTH", True)
    monkeypatch.setattr(verification, "is_verified", lambda domain, **kw: False)
    assert verification.credential_gate("x.test", credentialed=False) == (True, None)


def test_a_verified_domain_may_be_signed_in_to(monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_VERIFIED_DOMAIN_FOR_AUTH", True)
    monkeypatch.setattr(verification, "is_verified", lambda domain, **kw: True)
    assert verification.credential_gate("x.test", credentialed=True) == (True, None)


def test_the_gate_is_off_locally(monkeypatch):
    """Locally the person scanning and the person who owns the site are the
    same person, so nothing is asked of them."""
    monkeypatch.setattr(settings, "REQUIRE_VERIFIED_DOMAIN_FOR_AUTH", False)
    monkeypatch.setattr(verification, "is_verified", lambda domain, **kw: False)
    assert verification.credential_gate("x.test", credentialed=True) == (True, None)


def test_the_post_route_refuses_credentials_for_an_unverified_domain(
        client, instant_pipeline, monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_VERIFIED_DOMAIN_FOR_AUTH", True)
    monkeypatch.setattr(verification, "is_verified", lambda domain, **kw: False)
    resp = client.post("/scan", json=dict(CREDENTIALS, url="https://x.test",
                                          categories="performance"))
    assert resp.status_code == 400
    payload = resp.get_json()
    assert payload["reason"] == "unverified"
    assert payload["verification"]["token"].startswith("sav-")
    assert instant_pipeline == []              # and it never reached a worker


def test_the_same_scan_without_credentials_runs(client, instant_pipeline, monkeypatch):
    """The gate is about the password, not about the domain: a single-page
    scan of an unverified site is still open to anyone."""
    monkeypatch.setattr(settings, "REQUIRE_VERIFIED_DOMAIN_FOR_AUTH", True)
    monkeypatch.setattr(verification, "is_verified", lambda domain, **kw: False)
    resp = client.post("/scan", json={"url": "https://x.test",
                                      "categories": "performance"})
    assert resp.status_code == 200


def test_the_credential_gate_does_not_care_how_big_the_scan_is(
        client, instant_pipeline, monkeypatch):
    """Unlike the crawl gate, which is about what a scan costs. This one is
    about whose password it is, so one page is gated exactly like twenty."""
    monkeypatch.setattr(settings, "REQUIRE_VERIFIED_DOMAIN_FOR_AUTH", True)
    monkeypatch.setattr(settings, "REQUIRE_DOMAIN_VERIFICATION", False)
    monkeypatch.setattr(verification, "is_verified", lambda domain, **kw: False)
    resp = client.post("/scan", json=dict(CREDENTIALS, url="https://x.test",
                                          categories="performance", max_pages=1))
    assert resp.status_code == 400
    assert resp.get_json()["reason"] == "unverified"


# --------------------------------------------------------------------------
# the credential key and the disk it might reach
# --------------------------------------------------------------------------

class FakeRedis:
    """Just enough Redis to answer the persistence question."""

    def __init__(self, save="", appendonly="no", answers=True):
        self._config = {"save": save, "appendonly": appendonly}
        self._answers = answers
        self.asked = 0

    def config_get(self, name):
        self.asked += 1
        if not self._answers:
            raise RuntimeError("ERR unknown command 'config'")
        return {name: self._config[name]}


def backend_with(conn):
    """A RedisQueueBackend whose only live part is the connection."""
    backend = jobqueue.RedisQueueBackend.__new__(jobqueue.RedisQueueBackend)
    backend.r = conn
    backend._persistence = None
    return backend


def test_a_local_queue_writes_the_key_as_it_always_has(monkeypatch):
    monkeypatch.setattr(settings, "PERSIST_SCAN_AUTH", True)
    conn = FakeRedis(save="900 1", appendonly="yes")     # persistence blazing
    assert backend_with(conn).credential_storage_ok() == (True, "")
    assert conn.asked == 0                     # not even asked - it is allowed


def test_public_mode_wants_the_server_to_say_persistence_is_off(monkeypatch):
    monkeypatch.setattr(settings, "PERSIST_SCAN_AUTH", False)
    ok, reason = backend_with(FakeRedis(save="", appendonly="no")).credential_storage_ok()
    assert ok is True and reason == ""


@pytest.mark.parametrize("save,appendonly", [
    ("900 1", "no"),            # RDB snapshots on
    ("", "yes"),                # AOF on
    ("3600 1 300 100", "yes"),  # both, which is the Redis default
])
def test_a_redis_that_writes_to_disk_does_not_get_a_password(save, appendonly,
                                                             monkeypatch):
    monkeypatch.setattr(settings, "PERSIST_SCAN_AUTH", False)
    ok, reason = backend_with(FakeRedis(save, appendonly)).credential_storage_ok()
    assert ok is False
    assert "--appendonly no" in reason


def test_a_redis_that_will_not_answer_does_not_get_one_either(monkeypatch):
    """Not answering is not the same as answering "no". A managed Redis with
    CONFIG locked down cannot prove it, so it does not get the benefit of the
    doubt - somebody's password is the thing being bet."""
    monkeypatch.setattr(settings, "PERSIST_SCAN_AUTH", False)
    ok, reason = backend_with(FakeRedis(answers=False)).credential_storage_ok()
    assert ok is False and "cannot confirm" in reason


def test_the_question_is_asked_once(monkeypatch):
    """It sits in the path of every credentialed submission, and a running
    Redis does not change its persistence between two scans."""
    monkeypatch.setattr(settings, "PERSIST_SCAN_AUTH", False)
    conn = FakeRedis(save="", appendonly="no")
    backend = backend_with(conn)
    for _ in range(5):
        backend.credential_storage_ok()
    assert conn.asked == 2                     # `save` and `appendonly`, once


def test_the_refusal_reaches_the_client_rather_than_scanning_anyway(
        client, instant_pipeline, monkeypatch):
    """Dropping the credentials silently would scan the logged-out site and
    report the login page as the result."""
    class Refusing(jobqueue.InlineBackend):
        def credential_storage_ok(self):
            return False, "This deployment will not accept sign-in details."

    jobqueue.set_backend(Refusing())
    dashboard.reset_services()
    resp = client.post("/scan", json=dict(CREDENTIALS, url="https://x.test",
                                          categories="performance"))
    assert resp.status_code == 400
    assert "sign-in details" in resp.get_json()["message"]
    assert instant_pipeline == []


def test_the_inline_queue_has_no_disk_to_reach():
    """Nothing leaves the process; the credential is an object a worker thread
    pops out of a dict."""
    assert jobqueue.InlineBackend().credential_storage_ok() == (True, "")


# --------------------------------------------------------------------------
# what the UI is told
# --------------------------------------------------------------------------

def test_the_limits_endpoint_names_the_mode(client, monkeypatch):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", config.MODE_PUBLIC)
    assert client.get("/api/limits").get_json()["mode"] == "public"
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", config.MODE_LOCAL)
    assert client.get("/api/limits").get_json()["mode"] == "local"


def test_the_limits_endpoint_hides_what_public_mode_refuses(client, monkeypatch):
    for name, value in (("DEPLOYMENT_MODE", config.MODE_PUBLIC),
                        ("ALLOW_DNS_OVERRIDE", False),
                        ("ALLOW_PRIVATE_DNS_TARGETS", False),
                        ("REQUIRE_VERIFIED_DOMAIN_FOR_AUTH", True)):
        monkeypatch.setattr(settings, name, value)
    payload = client.get("/api/limits").get_json()
    assert payload["dns_override_enabled"] is False
    assert payload["private_dns_targets_enabled"] is False
    assert payload["auth_requires_verification"] is True


def test_the_limits_endpoint_says_when_the_queue_refuses_credentials(client):
    class Refusing(jobqueue.InlineBackend):
        def credential_storage_ok(self):
            return False, "Redis writes to disk."

    jobqueue.set_backend(Refusing())
    dashboard.reset_services()
    payload = client.get("/api/limits").get_json()
    assert payload["scan_auth_enabled"] is False
    assert payload["scan_auth_disabled_reason"] == "Redis writes to disk."


def test_a_local_deployment_offers_everything(client):
    payload = client.get("/api/limits").get_json()
    assert payload["mode"] == "local"
    assert payload["scan_auth_enabled"] is True
    assert payload["scan_auth_disabled_reason"] == ""
    assert payload["dns_override_enabled"] is True
    assert payload["auth_requires_verification"] is False


def test_the_form_carries_the_note_the_limits_endpoint_fills_in():
    """The fields are offered but conditional, so there is somewhere to say
    so - rather than letting the scan be refused after a password is typed."""
    assert 'id="authnote"' in dashboard.PAGE
    assert "auth_requires_verification" in dashboard.PAGE


def test_the_secret_field_list_is_the_one_the_gate_uses():
    """The gate keys off `bool(credentials)`, which is built from exactly
    these fields - so a new credential field is gated the day it is added."""
    assert set(scanauth.SECRET_FIELDS) >= {"auth_user", "auth_pass", "cookies",
                                           "login_url", "login_user", "login_pass"}
