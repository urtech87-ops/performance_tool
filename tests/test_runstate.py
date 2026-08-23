"""The ledger itself: what it records, and the words it puts to a failure.

Nothing here touches a scanner. These are the rules the pipeline leans on -
which raw output means "blocked" rather than "broken", when a run is partial
rather than failed, and what a person is told in each case.
"""

import json

import pytest

import runstate


# --------------------------------------------------------------------------
# reading a scanner's own words
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,kind", [
    ("Runtime error encountered: Status code: 403", "blocked"),
    ("HTTP 401 Unauthorized", "blocked"),
    ("429 Too Many Requests", "blocked"),
    ("Attention Required! | Cloudflare", "blocked"),
    ("Just a moment... please enable JS and cookies", "blocked"),
    ("net::ERR_BLOCKED_BY_CLIENT", "blocked"),
    ("getaddrinfo ENOTFOUND nope.example", "dns"),
    ("net::ERR_NAME_NOT_RESOLVED", "dns"),
    ("URLError: [Errno -2] Name or service not known", "dns"),
    ("net::ERR_CERT_AUTHORITY_INVALID", "tls"),
    ("SSLCertVerificationError: certificate verify failed", "tls"),
    ("self-signed certificate in certificate chain", "tls"),
    ("Chrome didn't collect any screenshots: PAGE_HUNG", "timeout"),
    ("navigation timed out after 30000ms", "timeout"),
    ("connect ECONNREFUSED 10.0.0.1:443", "unreachable"),
    ("net::ERR_CONNECTION_RESET", "unreachable"),
])
def test_known_conditions_are_recognised(text, kind):
    assert runstate.classify(text) == kind


@pytest.mark.parametrize("text", ["", None, "something went wrong", "score: 0.42"])
def test_anything_else_is_left_unclassified(text):
    """A guess dressed as a diagnosis is worse than none: unknown stays unknown."""
    assert runstate.classify(text) is None


def test_dns_and_tls_win_over_the_words_they_happen_to_contain():
    assert runstate.classify("SSL handshake timed out") == "tls"
    assert runstate.classify("getaddrinfo ENOTFOUND after timeout") == "dns"


def test_each_condition_maps_to_a_page_outcome():
    assert runstate.page_status("blocked") == runstate.BLOCKED
    assert runstate.page_status("timeout") == runstate.TIMEOUT
    assert runstate.page_status("dns") == runstate.ERROR
    assert runstate.page_status(None) == runstate.ERROR


def test_every_condition_has_a_sentence_without_a_stack_trace():
    for kind in runstate.FAILURE_KINDS:
        notice = runstate.friendly(kind)
        assert notice["title"] and notice["message"]
        assert "Traceback" not in notice["message"]
        assert "Error:" not in notice["message"]


def test_the_blocked_message_offers_the_way_out():
    message = runstate.friendly("blocked")["message"]
    assert "site you control" in message and "verify domain ownership" in message


def test_a_queue_timeout_becomes_the_timeout_banner():
    notice = runstate.friendly_from_error("scan exceeded the 900s limit")
    assert notice["kind"] == "timeout"
    assert notice["title"] == "The scan timed out"
    assert notice["status"] == runstate.FAILED


def test_an_unrecognised_error_still_gets_a_civil_answer():
    notice = runstate.friendly_from_error("KeyError: 'routes'")
    assert notice["kind"] == "error"
    assert "KeyError" not in notice["message"]


# --------------------------------------------------------------------------
# counting what happened
# --------------------------------------------------------------------------

def state(**kw):
    return runstate.RunState(url="https://x.test", **kw)


def test_coverage_counts_the_primary_stage():
    s = state()
    s.primary_stage = "lighthouse"
    for i in range(4):
        s.record(f"/p{i}", "lighthouse", runstate.OK)
    s.record("/p4", "lighthouse", runstate.BLOCKED, "blocked")
    cov = s.coverage()
    assert (cov["ok"], cov["attempted"], cov["failed"], cov["percent"]) == (4, 5, 1, 80)
    assert cov["by_status"] == {"ok": 4, "blocked": 1}


def test_a_run_that_measured_some_pages_is_partial():
    s = state()
    s.primary_stage = "lighthouse"
    s.record("/a", "lighthouse", runstate.OK)
    s.record("/b", "lighthouse", runstate.TIMEOUT, "timeout")
    assert s.status == runstate.PARTIAL
    assert s.notice()["kind"] == "timeout"
    assert "1 of 2 page(s) that were measured" in s.notice()["message"]


def test_a_run_that_measured_every_page_is_complete():
    s = state()
    s.primary_stage = "lighthouse"
    s.record("/a", "lighthouse", runstate.OK)
    assert s.status == runstate.COMPLETE
    assert s.notice() is None


def test_a_run_that_measured_nothing_is_failed():
    s = state()
    s.primary_stage = "lighthouse"
    s.record("/a", "lighthouse", runstate.BLOCKED, "blocked")
    assert s.status == runstate.FAILED
    assert s.notice()["title"] == "This site is blocking automated scans"


def test_a_stage_that_failed_on_top_of_a_crawl_that_did_not_is_partial():
    """The crawl scored pages: that is a real result, not a failed run."""
    s = state()
    s.record("/a", "crawl", runstate.OK)
    s.primary_stage = "lighthouse"
    s.record("/a", "lighthouse", runstate.BLOCKED, "blocked")
    assert s.status == runstate.PARTIAL


def test_a_degraded_run_is_never_reported_as_complete():
    s = state()
    s.primary_stage = "lighthouse"
    s.record("/a", "lighthouse", runstate.OK)
    s.note("the security scan failed: boom")
    assert s.status == runstate.PARTIAL


def test_the_worst_outcome_of_a_page_is_the_one_it_carries():
    s = state()
    s.record("/a", "crawl", runstate.OK)
    s.record("/a", "lighthouse", runstate.TIMEOUT, "timeout")
    s.record("/a", "accessibility", runstate.BLOCKED, "blocked")
    assert s.pages["/a"]["status"] == runstate.BLOCKED


def test_pages_a_stage_never_reached_are_recorded_not_forgotten():
    s = state()
    s.primary_stage = "lighthouse"
    s.record("/a", "lighthouse", runstate.OK)
    s.skip_remaining(["/a", "/b", "/c"], "lighthouse", "run time limit reached")
    cov = s.coverage()
    assert cov["attempted"] == 3 and cov["ok"] == 1
    assert {f["url"] for f in s.failures()} == {"/b", "/c"}
    assert s.pages["/a"]["status"] == runstate.OK       # an outcome is never overwritten


def test_an_unreachable_site_keeps_its_own_explanation():
    s = state()
    s.site_signal("dns", "Name or service not known")
    assert s.status == runstate.FAILED
    assert s.notice()["title"] == "We couldn't reach this site"


# --------------------------------------------------------------------------
# the run budget
# --------------------------------------------------------------------------

def test_the_budget_is_what_is_left_of_the_ceiling():
    now = [1000.0]
    s = runstate.RunState(budget=100, clock=lambda: now[0])
    assert s.time_left() == 100 and not s.expired()
    now[0] += 60
    assert s.time_left() == 40 and not s.expired()
    now[0] += 60
    assert s.time_left() == 0 and s.expired()


def test_a_run_with_no_ceiling_never_expires():
    s = runstate.RunState(budget=None)
    assert s.time_left() is None and not s.expired()


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

def test_the_ledger_round_trips_through_the_run_folder(tmp_path):
    s = runstate.RunState(tmp_path, "run-1", url="https://x.test", device="desktop")
    s.primary_stage = "lighthouse"
    s.record("https://x.test/a", "lighthouse", runstate.OK, seconds=3)
    s.record("https://x.test/b", "lighthouse", runstate.BLOCKED, "blocked")
    s.finalize()

    saved = runstate.RunState.load(tmp_path)
    assert saved["run"] == "run-1" and saved["status"] == "partial"
    assert saved["coverage"]["ok"] == 1
    assert saved["notice"]["kind"] == "blocked"
    assert json.loads((tmp_path / runstate.STATUS_FILE).read_text())["url"] == "https://x.test"

    coverage = runstate.report_coverage_from(saved)
    assert coverage["pages_ok"] == 1 and coverage["pages_attempted"] == 2
    assert coverage["failures"][0]["url"] == "https://x.test/b"


def test_a_folder_without_a_ledger_reads_as_none(tmp_path):
    assert runstate.RunState.load(tmp_path) is None
    assert runstate.report_coverage_from(None) is None


def test_a_ledger_can_be_read_back_mid_run(tmp_path):
    """Saved after every stage - so a run that dies is still reportable."""
    s = runstate.RunState(tmp_path, "run-2")
    s.primary_stage = "lighthouse"
    s.record("/a", "lighthouse", runstate.OK)
    s.save()
    assert runstate.RunState.load(tmp_path)["finished_at"] is None
    s.record("/b", "lighthouse", runstate.TIMEOUT, "timeout")
    s.save()
    assert runstate.RunState.load(tmp_path)["coverage"]["attempted"] == 2


def test_the_coverage_sentence_is_the_same_everywhere():
    s = state()
    s.primary_stage = "lighthouse"
    for i in range(39):
        s.record(f"/p{i}", "lighthouse", runstate.OK)
    for i in range(39, 55):
        s.record(f"/p{i}", "lighthouse", runstate.ERROR, "no report")
    assert runstate.coverage_sentence(s.report_coverage()) == \
        "Coverage: 39 of 55 page(s) (71%)"


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status):
        self.status = status

    def close(self):
        pass


def test_preflight_reads_a_refusal_as_a_refusal():
    out = runstate.preflight("https://x.test", opener=lambda r, timeout=0: FakeResponse(403))
    assert out["kind"] == "blocked" and out["code"] == 403


def test_preflight_is_quiet_when_the_site_answers():
    out = runstate.preflight("https://x.test", opener=lambda r, timeout=0: FakeResponse(200))
    assert out["kind"] is None and out["code"] == 200


def test_preflight_never_raises():
    def boom(request, timeout=0):
        raise OSError("[Errno -2] Name or service not known")

    out = runstate.preflight("https://x.test", opener=boom)
    assert out["kind"] == "dns"
    assert "Name or service not known" in out["detail"]


def test_preflight_names_itself_rather_than_pretending_to_be_a_person():
    seen = {}

    def opener(request, timeout=0):
        seen["ua"] = request.get_header("User-agent")
        return FakeResponse(200)

    runstate.preflight("https://x.test", opener=opener)
    assert "SiteAuditBot" in seen["ua"]
