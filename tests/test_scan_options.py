"""Scan configuration: throttling, device emulation, viewport and User-Agent.

Two things are checked here, and only these two:

  1. a configuration turns into the right flags on the `lighthouse` and
     `unlighthouse-ci` command lines, and
  2. an unconfigured scan still produces the exact command the pipeline ran
     before any of this existed.

Nothing about scoring is touched: these are inputs to the tools, and what the
tools then report is their business.
"""

import json
import subprocess
from pathlib import Path

import pytest

import dashboard
import guardrails
import scanconfig

from conftest import FakeRunner


ROUTES = [{"path": "/"}]


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    d = tmp_path / "runs"
    d.mkdir()
    monkeypatch.setattr(dashboard, "RUNS_DIR", d)
    return d


@pytest.fixture
def crawl(monkeypatch):
    """Record the crawl's argv without running unlighthouse."""
    monkeypatch.setattr(dashboard, "DRYRUN", False)
    calls = []

    def fake_run_stream(cmd, cwd, log, env=None, **kw):
        calls.append(list(cmd))
        d = Path(cwd) / ".unlighthouse"
        d.mkdir(parents=True, exist_ok=True)
        (d / "ci-result.json").write_text(json.dumps({"routes": ROUTES}), encoding="utf-8")
        return 0

    monkeypatch.setattr(dashboard, "run_stream", fake_run_stream)
    return calls


class Subprocesses:
    """Node calls are the axe runner; everything else is lighthouse."""

    def __init__(self):
        self.axe = FakeRunner()
        self.lighthouse = []

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        if Path(str(cmd[0])).name in ("node", "node.exe"):
            return self.axe(cmd, **kwargs)
        self.lighthouse.append(cmd)
        out = next((a for a in cmd if a.startswith("--output-path=")), None)
        if out:
            stem = Path(out.split("=", 1)[1])
            stem.parent.mkdir(parents=True, exist_ok=True)
            stem.with_name(stem.name + ".report.json").write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    def flag(self, prefix):
        """Every lighthouse argument starting with `prefix`, first call only."""
        return [a for a in self.lighthouse[0] if a.startswith(prefix)]


@pytest.fixture
def procs(monkeypatch):
    stub = Subprocesses()
    monkeypatch.setattr(subprocess, "run", stub)
    return stub


@pytest.fixture
def report(monkeypatch):
    """Keep the report builder off the real loaders and off Chrome."""
    monkeypatch.setattr(dashboard.cr, "load_ci_results", lambda roots: [])
    monkeypatch.setattr(dashboard.cr, "load_lhr_pages", lambda roots: [])
    monkeypatch.setattr(dashboard.cr, "build_html", lambda pages, roots, **kw: "<html></html>")


def pipeline(**over):
    kw = dict(url="https://x.test", device="desktop", samples=1, deep=True, max_pages=30,
              concurrency=1, security=False, categories=["performance"],
              log=lambda *_: None)
    kw.update(over)
    return dashboard.run_pipeline(kw.pop("url"), kw.pop("device"), kw.pop("samples"),
                                  kw.pop("deep"), kw.pop("max_pages"), kw.pop("concurrency"),
                                  kw.pop("security"), kw.pop("categories"), kw.pop("log"),
                                  **kw)


# --------------------------------------------------------------------------
# the default is the old behaviour, exactly
# --------------------------------------------------------------------------

def test_an_unconfigured_scan_runs_the_command_it_always_ran(runs_dir, crawl, procs, report):
    pipeline()
    lh = procs.lighthouse[0]
    assert "--preset=desktop" in lh
    assert not [a for a in lh if a.startswith("--throttling")]
    assert not [a for a in lh if a.startswith("--screenEmulation")]
    assert not [a for a in lh if a.startswith("--emulatedUserAgent")]
    assert "--chrome-flags=--headless=new --no-sandbox --ignore-certificate-errors" in lh
    assert "--desktop" in crawl[0] and "--throttle" in crawl[0]


def test_mobile_still_gets_no_preset(runs_dir, crawl, procs, report):
    pipeline(device="mobile")
    assert "--preset=desktop" not in procs.lighthouse[0]
    assert "--desktop" not in crawl[0]


def test_the_default_config_object_is_the_default(runs_dir, crawl, procs, report):
    """Passing an explicit ScanConfig() must change nothing."""
    pipeline(scan_config=scanconfig.ScanConfig())
    assert scanconfig.ScanConfig().is_default
    assert "--preset=desktop" in procs.lighthouse[0]
    assert not [a for a in procs.lighthouse[0] if a.startswith("--throttling")]


# --------------------------------------------------------------------------
# throttling
# --------------------------------------------------------------------------

@pytest.mark.parametrize("preset,latency,down,up", [
    ("broadband_fast", 5, 100_000, 50_000),
    ("broadband", 20, 20_000, 10_000),
    ("lte", 70, 12_000, 12_000),
    ("4g", 85, 9_000, 9_000),
    ("3g", 300, 1_600, 768),
])
def test_each_connection_preset_reaches_lighthouse(runs_dir, crawl, procs, report,
                                                   preset, latency, down, up):
    pipeline(scan_config=scanconfig.ScanConfig(throttling=preset))
    lh = procs.lighthouse[0]
    assert "--throttling-method=devtools" in lh
    assert f"--throttling.requestLatencyMs={latency}" in lh
    assert f"--throttling.downloadThroughputKbps={down}" in lh
    assert f"--throttling.uploadThroughputKbps={up}" in lh
    assert "--throttling.cpuSlowdownMultiplier=1" in lh


def test_unthrottled_says_provided_and_drops_the_crawl_throttle(runs_dir, crawl, procs, report):
    pipeline(scan_config=scanconfig.ScanConfig(throttling="none"))
    lh = procs.lighthouse[0]
    assert "--throttling-method=provided" in lh
    assert not [a for a in lh if a.startswith("--throttling.download")]
    assert "--throttle" not in crawl[0]


def test_a_custom_connection_is_passed_through(runs_dir, crawl, procs, report):
    pipeline(scan_config=scanconfig.ScanConfig(throttling="custom", down_kbps=7500,
                                               up_kbps=2500, latency_ms=42))
    lh = procs.lighthouse[0]
    assert "--throttling.downloadThroughputKbps=7500" in lh
    assert "--throttling.uploadThroughputKbps=2500" in lh
    assert "--throttling.requestLatencyMs=42" in lh


def test_a_custom_connection_is_clamped_not_trusted():
    cfg = scanconfig.ScanConfig(throttling="custom", down_kbps=10 ** 12,
                                up_kbps=-4, latency_ms=10 ** 9)
    assert cfg.down_kbps == scanconfig.MAX_KBPS
    assert cfg.up_kbps == 0
    assert cfg.latency_ms == scanconfig.MAX_LATENCY_MS


def test_custom_with_nothing_filled_in_falls_back_to_the_default():
    assert scanconfig.ScanConfig(throttling="custom").throttling == "default"


def test_an_unknown_preset_falls_back_rather_than_raising():
    assert scanconfig.ScanConfig(throttling="; rm -rf /").throttling == "default"


# --------------------------------------------------------------------------
# device emulation
# --------------------------------------------------------------------------

def test_a_handset_profile_sets_form_factor_screen_and_agent(runs_dir, crawl, procs, report):
    pipeline(scan_config=scanconfig.ScanConfig(device_profile="pixel_7"))
    lh = procs.lighthouse[0]
    assert "--form-factor=mobile" in lh
    assert "--screenEmulation.mobile=true" in lh
    assert "--screenEmulation.width=412" in lh
    assert "--screenEmulation.height=915" in lh
    assert "--screenEmulation.deviceScaleFactor=2.625" in lh
    assert any(a.startswith("--emulatedUserAgent=") and "Pixel 7" in a for a in lh)
    # an explicit emulation replaces the preset instead of fighting it
    assert "--preset=desktop" not in lh


def test_an_iphone_profile_is_offered_and_emulated(runs_dir, crawl, procs, report):
    pipeline(scan_config=scanconfig.ScanConfig(device_profile="iphone_14_pro"))
    lh = procs.lighthouse[0]
    assert "--screenEmulation.width=393" in lh and "--screenEmulation.height=852" in lh
    assert any("iPhone" in a for a in lh if a.startswith("--emulatedUserAgent="))


def test_a_handset_profile_beats_the_desktop_toggle(runs_dir, crawl, procs, report):
    """Picking a phone means the crawl runs as a phone too, and the run says so."""
    name = pipeline(device="desktop",
                    scan_config=scanconfig.ScanConfig(device_profile="iphone_se"))
    assert "--desktop" not in crawl[0]
    assert "-mobile-" in name        # the run folder records the real form factor


def test_the_desktop_profile_keeps_a_desktop_screen(runs_dir, crawl, procs, report):
    pipeline(device="mobile", scan_config=scanconfig.ScanConfig(device_profile="desktop"))
    lh = procs.lighthouse[0]
    assert "--form-factor=desktop" in lh
    assert "--screenEmulation.mobile=false" in lh
    assert "--screenEmulation.width=1350" in lh
    assert "--desktop" in crawl[0]


def test_an_unknown_device_profile_is_ignored():
    assert scanconfig.ScanConfig(device_profile="../../etc/passwd").device_profile == ""


# --------------------------------------------------------------------------
# viewport, pixel ratio, user agent
# --------------------------------------------------------------------------

def test_a_custom_viewport_reaches_lighthouse_and_chrome(runs_dir, crawl, procs, report):
    pipeline(scan_config=scanconfig.ScanConfig(width=1024, height=768, dpr=2))
    lh = procs.lighthouse[0]
    assert "--screenEmulation.width=1024" in lh
    assert "--screenEmulation.height=768" in lh
    assert "--screenEmulation.deviceScaleFactor=2" in lh
    # Chrome's own window has to hold the emulated viewport
    flags = next(a for a in lh if a.startswith("--chrome-flags="))
    assert "--window-size=1024,768" in flags


def test_a_viewport_override_wins_over_the_profile(runs_dir, crawl, procs, report):
    pipeline(scan_config=scanconfig.ScanConfig(device_profile="iphone_se",
                                               width=500, height=900))
    lh = procs.lighthouse[0]
    assert "--screenEmulation.width=500" in lh and "--screenEmulation.height=900" in lh
    assert "--screenEmulation.deviceScaleFactor=2" in lh      # the profile's own


def test_a_user_agent_override_reaches_lighthouse_and_the_crawl(runs_dir, crawl, procs, report):
    pipeline(scan_config=scanconfig.ScanConfig(user_agent="AuditBot/2.0"))
    assert "--emulatedUserAgent=AuditBot/2.0" in procs.lighthouse[0]
    assert crawl[0][crawl[0].index("--user-agent") + 1] == "AuditBot/2.0"


def test_a_user_agent_cannot_smuggle_a_second_header():
    cfg = scanconfig.ScanConfig(user_agent="Bot/1.0\r\nX-Admin: yes")
    assert "\r" not in cfg.user_agent and "\n" not in cfg.user_agent
    assert cfg.user_agent.startswith("Bot/1.0")


def test_a_viewport_is_clamped_to_something_a_browser_can_render():
    cfg = scanconfig.ScanConfig(width=99999, height=1, dpr=99)
    assert cfg.width == scanconfig.MAX_VIEWPORT
    assert cfg.height == scanconfig.MIN_VIEWPORT
    assert cfg.dpr == scanconfig.MAX_DPR


# --------------------------------------------------------------------------
# the request layer
# --------------------------------------------------------------------------

def test_the_request_layer_reads_and_clamps_every_knob():
    params, error, _ = guardrails.sanitize_params({
        "url": "https://x.test", "throttling": "3g", "device_profile": "pixel_5",
        "viewport_width": "1200", "viewport_height": "800", "dpr": "1.5",
        "user_agent": "AuditBot/2.0"})
    assert error is None
    cfg = scanconfig.ScanConfig.from_dict(params["scan_config"])
    assert cfg.throttling == "3g" and cfg.device_profile == "pixel_5"
    assert (cfg.width, cfg.height, cfg.dpr) == (1200, 800, 1.5)
    assert cfg.user_agent == "AuditBot/2.0"


def test_the_scan_config_survives_the_queue(runs_dir, monkeypatch):
    """A worker in another process rebuilds the same configuration."""
    import tasks

    seen = {}

    def fake_pipeline(*args, **kwargs):
        seen.update(kwargs)
        return "run-folder"

    monkeypatch.setattr(dashboard, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(dashboard, "run_artifacts", lambda name: [])
    params, _, _ = guardrails.sanitize_params({"url": "https://x.test",
                                               "throttling": "lte",
                                               "device_profile": "pixel_8_pro"})
    tasks.run_scan_job(json.loads(json.dumps(params)), lambda *_: None)
    assert seen["scan_config"] == scanconfig.ScanConfig(throttling="lte",
                                                        device_profile="pixel_8_pro")


def test_the_scan_route_passes_the_configuration_through(monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard, "run_pipeline",
                        lambda *a, **kw: calls.append(kw) or "run-folder")
    monkeypatch.setattr(dashboard, "run_artifacts", lambda name: [])
    dashboard.app.config["TESTING"] = True
    client = dashboard.app.test_client()
    resp = client.post("/scan", json={"url": "https://x.test", "categories": "performance",
                                      "throttling": "4g", "device_profile": "iphone_12",
                                      "user_agent": "AuditBot/2.0"})
    assert resp.status_code == 200
    client.get(resp.get_json()["stream"])
    cfg = calls[0]["scan_config"]
    assert cfg.throttling == "4g" and cfg.device_profile == "iphone_12"
    assert cfg.user_agent == "AuditBot/2.0"


def test_json_booleans_are_read_the_same_as_query_string_ones():
    """The form posts JSON, where a checked box is `true`, not `"1"`. Reading
    only the string spelling silently turns every switch off."""
    from_json, _, _ = guardrails.sanitize_params({"url": "https://x.test", "deep": True,
                                                  "security": True, "a11y": True})
    from_query, _, _ = guardrails.sanitize_params({"url": "https://x.test", "deep": "1",
                                                   "security": "1", "a11y": "1"})
    assert from_json["deep"] and from_json["security"] and from_json["a11y"]
    assert from_json == from_query

    off, _, _ = guardrails.sanitize_params({"url": "https://x.test", "deep": False,
                                            "security": 0, "a11y": "0"})
    assert not off["deep"] and not off["security"] and not off["a11y"]


def test_categories_may_arrive_as_a_json_list():
    params, _, _ = guardrails.sanitize_params({"url": "https://x.test",
                                               "categories": ["seo", "performance"],
                                               "standards": ["wcag21aa"]})
    assert params["categories"] == ["performance", "seo"]     # canonical order
    assert params["standards"] == ["wcag21aa"]


def test_the_posted_form_really_runs_a_deep_scan(monkeypatch):
    """The round trip the browser makes, end to end."""
    calls = []
    monkeypatch.setattr(dashboard, "run_pipeline",
                        lambda *a, **kw: calls.append(a) or "run-folder")
    monkeypatch.setattr(dashboard, "run_artifacts", lambda name: [])
    dashboard.app.config["TESTING"] = True
    client = dashboard.app.test_client()
    resp = client.post("/scan", json={"url": "https://x.test", "device": "desktop",
                                      "deep": 1, "samples": 1, "max_pages": 5,
                                      "concurrency": 1, "security": 0,
                                      "categories": "performance", "a11y": 0,
                                      "standards": ""})
    client.get(resp.get_json()["stream"])
    assert calls[0][3] is True                    # deep


# --------------------------------------------------------------------------
# the form
# --------------------------------------------------------------------------

def test_the_form_offers_every_preset_and_profile_the_server_knows():
    page = dashboard.PAGE
    for tid, label in scanconfig.throttling_choices():
        assert f'<option value="{tid}"' in page, tid
    for did, label in scanconfig.device_choices():
        assert f'<option value="{did}"' in page, did


def test_the_form_has_the_advanced_controls():
    page = dashboard.PAGE
    for field in ("throttling", "down_kbps", "up_kbps", "latency_ms", "device_profile",
                  "viewport_width", "viewport_height", "dpr", "user_agent"):
        assert f'id="{field}"' in page, field
    assert "Advanced options" in page
    # still collapsed behind the same <details>, GTmetrix-style
    assert '<details class="adv">' in page


def test_the_existing_controls_are_untouched():
    page = dashboard.PAGE
    assert 'id="maxpages"' in page and 'id="concurrency"' in page and 'id="deep"' in page
    assert 'id="samples"' in page
