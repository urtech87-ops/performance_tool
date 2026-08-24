"""Named presets: save a set of scan settings, pick it again later.

Two things are checked here, and the second one matters more than the first:

  1. a preset round-trips - what is saved is what comes back, and what comes
     back really configures the scan it says it does (create, rename, update,
     delete, and which one a fresh page starts on);
  2. **a preset never contains a credential.** Not in the store, not in the
     API's answer, not in the field list the browser is given, not even when a
     client posts the whole sign-in form at it. That rule is the reason
     `scanpresets` builds a preset from a fixed field list instead of
     filtering what it was sent, and the tests below come at it from every
     side that could get one in.

Nothing here touches the pipeline: a preset is settings for a request, and the
request then takes the path every other request takes.
"""

import json
import shutil
import subprocess

import pytest

import dashboard
import guardrails
import scanauth
import scanconfig
import scanpresets

OWNER = "owner-under-test"

# A full, realistic set of settings - one of everything a preset may hold.
SETTINGS = {
    "device": "mobile", "deep": True, "samples": 2, "max_pages": 12,
    "concurrency": 2, "categories": "performance,seo", "security": True,
    "a11y": True, "standards": "wcag21aa,section508",
    "throttling": "3g", "down_kbps": 0, "up_kbps": 0, "latency_ms": 0,
    "device_profile": "pixel_7", "viewport_width": 480, "viewport_height": 900,
    "dpr": 2, "user_agent": "AuditBot/2.0",
    "block_patterns": "*doubleclick.net*\n*/ads/*",
    "dns_host": "staging.x.test", "dns_ip": "203.0.113.9",
}

# What a browser would post if it sent the whole form, sign-in fields and all.
CREDENTIALS = {
    "auth_user": "admin", "auth_pass": "hunter2",
    "cookies": "session=sess-abc-123; consent=accepted",
    "login_url": "https://x.test/login", "login_user_selector": "#u",
    "login_pass_selector": "#p", "login_submit_selector": "#go",
    "login_user": "someone", "login_pass": "s3cret",
}

SECRET_VALUES = ("hunter2", "sess-abc-123", "s3cret", "admin", "someone")


@pytest.fixture
def store():
    return scanpresets.PresetStore(scanpresets.MemoryPresetDocs())


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The app, with its preset files in a throwaway directory."""
    monkeypatch.setattr(dashboard, "PRESETS_DIR", tmp_path / "presets")
    dashboard.reset_services()
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


def saved(client, name="Staging box", settings=None, **extra):
    body = {"name": name, "settings": dict(SETTINGS if settings is None else settings)}
    body.update(extra)
    return client.post("/api/presets", json=body)


# --------------------------------------------------------------------------
# round trip: save -> reload -> apply
# --------------------------------------------------------------------------

def test_a_preset_comes_back_with_every_field_it_was_saved_with(store):
    preset = scanpresets.Preset(name="Staging box", settings=SETTINGS)
    store.save(OWNER, preset)

    reloaded = scanpresets.PresetStore(store.docs).get(OWNER, preset.id)
    assert reloaded is not None
    assert reloaded.name == "Staging box"
    assert set(reloaded.settings) == set(scanpresets.PRESET_FIELDS)
    assert reloaded.settings["device"] == "mobile"
    assert reloaded.settings["samples"] == 2
    assert reloaded.settings["max_pages"] == 12
    assert reloaded.settings["concurrency"] == 2
    assert reloaded.settings["categories"] == ["performance", "seo"]
    assert reloaded.settings["security"] is True and reloaded.settings["a11y"] is True
    assert reloaded.settings["standards"] == ["wcag21aa", "section508"]
    assert reloaded.settings["throttling"] == "3g"
    assert reloaded.settings["device_profile"] == "pixel_7"
    assert (reloaded.settings["viewport_width"], reloaded.settings["viewport_height"],
            reloaded.settings["dpr"]) == (480, 900, 2.0)
    assert reloaded.settings["user_agent"] == "AuditBot/2.0"
    assert reloaded.settings["block_patterns"] == ["*doubleclick.net*", "*/ads/*"]
    assert (reloaded.settings["dns_host"], reloaded.settings["dns_ip"]) \
        == ("staging.x.test", "203.0.113.9")


def test_applying_a_reloaded_preset_configures_the_scan_it_describes(store):
    """The point of the round trip: the settings become a real request, and
    the request becomes the scan the preset was saved from."""
    store.save(OWNER, scanpresets.Preset(name="Staging box", settings=SETTINGS))
    preset = scanpresets.PresetStore(store.docs).get(
        OWNER, store.saved(OWNER)[0].id)

    params, error, _ = guardrails.sanitize_params(preset.to_request()
                                                  | {"url": "https://x.test"})
    assert error is None
    assert params["device"] == "mobile"
    assert params["categories"] == ["performance", "seo"]
    assert params["security"] is True and params["a11y"] is True
    assert params["standards"] == ["wcag21aa", "section508"]

    cfg = scanconfig.ScanConfig.from_dict(params["scan_config"])
    assert cfg.throttling == "3g" and cfg.device_profile == "pixel_7"
    assert cfg.block_patterns == ("*doubleclick.net*", "*/ads/*")
    assert cfg.host_rules() == "MAP staging.x.test 203.0.113.9"
    assert cfg.blocked("https://ad.doubleclick.net/x.gif")


def test_a_presets_settings_are_clamped_the_way_a_scan_would_clamp_them(store):
    preset = scanpresets.Preset(name="Silly", settings={
        "samples": 900, "max_pages": -3, "concurrency": "many",
        "viewport_width": 99999, "dpr": 99, "throttling": "; rm -rf /",
        "device_profile": "../../etc/passwd", "dns_ip": "not-an-address",
        "user_agent": "Bot/1.0\r\nX-Admin: yes", "categories": ["seo", "junk"]})
    s = preset.settings
    assert s["samples"] == scanpresets.MAX_SAMPLES
    assert s["max_pages"] == 1 and s["concurrency"] == 3
    assert s["viewport_width"] == scanconfig.MAX_VIEWPORT
    assert s["dpr"] == scanconfig.MAX_DPR
    assert s["throttling"] == scanconfig.DEFAULT_THROTTLING
    assert s["device_profile"] == "" and s["dns_ip"] == ""
    assert "\r" not in s["user_agent"] and "\n" not in s["user_agent"]
    assert s["categories"] == ["seo"]


# --------------------------------------------------------------------------
# create / rename / update / delete
# --------------------------------------------------------------------------

def test_saving_twice_under_one_id_updates_rather_than_duplicates(store):
    preset = store.save(OWNER, scanpresets.Preset(name="Nightly", settings=SETTINGS))
    changed = scanpresets.Preset(name="Nightly", preset_id=preset.id,
                                 settings=dict(SETTINGS, max_pages=99, throttling="lte"))
    store.save(OWNER, changed)
    assert len(store.saved(OWNER)) == 1
    assert store.get(OWNER, preset.id).settings["max_pages"] == 99
    assert store.get(OWNER, preset.id).settings["throttling"] == "lte"


def test_a_rename_keeps_the_id_and_the_settings(store):
    preset = store.save(OWNER, scanpresets.Preset(name="Nightly", settings=SETTINGS))
    store.rename(OWNER, preset.id, "Nightly staging run")
    renamed = store.get(OWNER, preset.id)
    assert renamed.name == "Nightly staging run"
    assert renamed.id == preset.id                      # a rename is not a new preset
    assert renamed.settings == preset.settings


def test_deleting_one_leaves_the_others(store):
    a = store.save(OWNER, scanpresets.Preset(name="A", settings=SETTINGS))
    b = store.save(OWNER, scanpresets.Preset(name="B", settings=SETTINGS))
    assert store.delete(OWNER, a.id) is True
    assert [p.id for p in store.saved(OWNER)] == [b.id]
    assert store.delete(OWNER, a.id) is False           # already gone


def test_two_presets_cannot_share_a_name(store):
    store.save(OWNER, scanpresets.Preset(name="Nightly", settings=SETTINGS))
    with pytest.raises(scanpresets.PresetError):
        store.save(OWNER, scanpresets.Preset(name="nightly", settings=SETTINGS))


def test_a_preset_needs_a_name():
    with pytest.raises(scanpresets.PresetError):
        scanpresets.Preset(name="   ", settings=SETTINGS)


def test_the_list_is_bounded(store):
    for i in range(scanpresets.MAX_PRESETS):
        store.save(OWNER, scanpresets.Preset(name=f"P{i}", settings=SETTINGS))
    with pytest.raises(scanpresets.PresetError):
        store.save(OWNER, scanpresets.Preset(name="one too many", settings=SETTINGS))


def test_one_browsers_presets_are_not_anothers(store):
    store.save(OWNER, scanpresets.Preset(name="Mine", settings=SETTINGS))
    assert [p.name for p in store.saved("someone-else")] == []
    assert len(store.list("someone-else")) == len(scanpresets.BUILTIN)


# --------------------------------------------------------------------------
# the default preset
# --------------------------------------------------------------------------

def test_a_browser_that_has_saved_nothing_starts_on_the_shipped_default(store):
    assert store.default_id(OWNER) == scanpresets.DEFAULT_PRESET_ID
    default = store.default(OWNER)
    assert default.builtin
    # the settings the form has always shipped with: a deep desktop scan of
    # every Lighthouse category, on Lighthouse's own throttling
    assert default.settings["device"] == "desktop"
    assert default.settings["deep"] is True
    assert default.settings["categories"] == list(scanpresets.VALID_CATEGORIES)
    assert default.settings["throttling"] == scanconfig.DEFAULT_THROTTLING
    assert default.scan_config().is_default


def test_the_built_ins_are_always_offered_and_cannot_be_changed(store):
    names = [p.name for p in store.list(OWNER)]
    assert len(names) == len(scanpresets.BUILTIN)
    with pytest.raises(scanpresets.PresetError):
        store.save(OWNER, scanpresets.Preset(name="Hijacked", settings=SETTINGS,
                                             preset_id=scanpresets.DEFAULT_PRESET_ID))
    with pytest.raises(scanpresets.PresetError):
        store.delete(OWNER, scanpresets.DEFAULT_PRESET_ID)


def test_a_saved_preset_can_become_the_default_and_survives_a_reload(store):
    preset = store.save(OWNER, scanpresets.Preset(name="Nightly", settings=SETTINGS),
                        make_default=True)
    fresh = scanpresets.PresetStore(store.docs)
    assert fresh.default_id(OWNER) == preset.id
    assert fresh.default(OWNER).settings["throttling"] == "3g"


def test_deleting_the_default_falls_back_rather_than_pointing_at_nothing(store):
    preset = store.save(OWNER, scanpresets.Preset(name="Nightly", settings=SETTINGS),
                        make_default=True)
    store.delete(OWNER, preset.id)
    assert store.default_id(OWNER) == scanpresets.DEFAULT_PRESET_ID
    assert store.default(OWNER) is not None


def test_the_first_party_built_in_really_blocks_the_third_parties(store):
    preset = store.get(OWNER, "builtin_first_party")
    cfg = preset.scan_config()
    assert cfg.blocked("https://www.google-analytics.com/collect")
    assert cfg.blocked("https://ad.doubleclick.net/pixel.gif")
    assert not cfg.blocked("https://x.test/js/app.js")


# --------------------------------------------------------------------------
# a preset never carries a credential
# --------------------------------------------------------------------------

def test_the_field_list_names_no_credential_field():
    assert not set(scanpresets.PRESET_FIELDS) & set(scanauth.SECRET_FIELDS)


def test_a_body_carrying_sign_in_fields_is_refused_not_quietly_trimmed():
    """Quietly dropping them would leave the user thinking they were saved."""
    with pytest.raises(scanpresets.PresetError):
        scanpresets.Preset.from_request({"name": "Everything", "settings":
                                         dict(SETTINGS, **CREDENTIALS)})
    with pytest.raises(scanpresets.PresetError):
        scanpresets.Preset.from_request(dict(SETTINGS, name="Flat", **CREDENTIALS))


def test_a_preset_built_from_settings_alone_has_no_room_for_one():
    """Not filtered out - never read: the settings are assembled from the
    field list, so a name that is not on it cannot be in the result."""
    settings = scanpresets.settings_from(dict(SETTINGS, **CREDENTIALS))
    assert set(settings) == set(scanpresets.PRESET_FIELDS)
    assert not guardrails.has_secrets(settings)
    assert not scanpresets.contains_secrets(settings)


def test_the_store_refuses_to_persist_one_however_it_got_there(store):
    preset = scanpresets.Preset(name="Sneaky", settings=SETTINGS)
    preset.settings["auth_pass"] = "hunter2"          # bypassing every door above
    with pytest.raises(scanpresets.PresetError):
        store.save(OWNER, preset)
    assert store.saved(OWNER) == []


def test_nothing_secret_is_anywhere_in_what_gets_written(store):
    store.save(OWNER, scanpresets.Preset.from_request(
        {"name": "Staging box", "settings": SETTINGS}))
    written = json.dumps(store.docs.read(OWNER))
    for value in SECRET_VALUES:
        assert value not in written
    for field in scanauth.SECRET_FIELDS:
        assert field not in written


def test_the_form_never_offers_to_save_a_sign_in_field():
    """The browser is handed the server's own field list, so the two cannot
    drift into disagreeing about what a preset may hold."""
    page = dashboard.PAGE
    line = next(l for l in page.splitlines() if "var PRESET_FIELDS =" in l)
    fields = json.loads(line.split("=", 1)[1].strip().rstrip(";"))
    assert fields == list(scanpresets.PRESET_FIELDS)
    assert not set(fields) & set(scanauth.SECRET_FIELDS)


# --------------------------------------------------------------------------
# the API the browser uses
# --------------------------------------------------------------------------

def test_the_api_lists_saves_renames_and_deletes(client):
    listing = client.get("/api/presets").get_json()
    assert listing["default"] == scanpresets.DEFAULT_PRESET_ID
    assert len(listing["presets"]) == len(scanpresets.BUILTIN)
    assert listing["fields"] == list(scanpresets.PRESET_FIELDS)

    created = saved(client).get_json()
    preset_id = created["preset"]["id"]
    assert created["preset"]["name"] == "Staging box"
    assert created["preset"]["settings"]["throttling"] == "3g"

    renamed = client.post("/api/presets", json={
        "id": preset_id, "name": "Staging box (4G)",
        "settings": created["preset"]["settings"]}).get_json()
    assert renamed["preset"]["id"] == preset_id
    assert renamed["preset"]["name"] == "Staging box (4G)"
    assert len(renamed["presets"]) == len(scanpresets.BUILTIN) + 1

    after = client.delete(f"/api/presets/{preset_id}").get_json()
    assert after["deleted"] == preset_id
    assert len(after["presets"]) == len(scanpresets.BUILTIN)


def test_the_api_survives_a_restart(client, tmp_path, monkeypatch):
    """Presets are worth keeping - that is what naming one is for."""
    preset_id = saved(client, name="Nightly").get_json()["preset"]["id"]
    dashboard.reset_services()                 # a fresh process, same files
    listing = client.get("/api/presets").get_json()
    assert preset_id in [p["id"] for p in listing["presets"]]


def test_the_api_can_choose_which_preset_a_fresh_page_starts_on(client):
    preset_id = saved(client, name="Nightly").get_json()["preset"]["id"]
    assert client.get("/api/presets").get_json()["default"] \
        == scanpresets.DEFAULT_PRESET_ID
    body = client.post("/api/presets", json={"id": preset_id, "default": True}).get_json()
    assert body["default"] == preset_id
    assert client.get("/api/presets").get_json()["default"] == preset_id
    # a built-in is a legitimate choice too
    client.post("/api/presets", json={"id": "builtin_mobile_4g", "default": True})
    assert client.get("/api/presets").get_json()["default"] == "builtin_mobile_4g"


def test_the_api_refuses_a_preset_carrying_credentials(client):
    resp = client.post("/api/presets", json={"name": "Everything",
                                             "settings": dict(SETTINGS, **CREDENTIALS)})
    assert resp.status_code == 400
    assert "never saved" in resp.get_json()["message"]
    assert client.get("/api/presets").get_json()["presets"] == \
        [p.as_dict() for p in scanpresets.BUILTIN]


def test_a_flat_form_post_is_refused_the_same_way(client):
    """The whole form, posted as it stands - which is exactly the mistake the
    rule exists for."""
    resp = client.post("/api/presets", json=dict(SETTINGS, name="Flat", **CREDENTIALS))
    assert resp.status_code == 400


def test_nothing_secret_reaches_the_preset_files(client, tmp_path):
    saved(client, name="Staging box")
    client.post("/api/presets", json={"name": "Everything",
                                      "settings": dict(SETTINGS, **CREDENTIALS)})
    written = "".join(p.read_text(encoding="utf-8")
                      for p in (tmp_path / "presets").glob("*.json"))
    assert "Staging box" in written                 # the good one really was stored
    for value in SECRET_VALUES:
        assert value not in written
    for field in scanauth.SECRET_FIELDS:
        assert field not in written


def test_a_deleted_preset_is_a_404_not_a_silent_success(client):
    assert client.delete("/api/presets/does-not-exist").status_code == 404


def test_a_built_in_cannot_be_deleted_through_the_api(client):
    resp = client.delete(f"/api/presets/{scanpresets.DEFAULT_PRESET_ID}")
    assert resp.status_code == 400
    assert len(client.get("/api/presets").get_json()["presets"]) == len(scanpresets.BUILTIN)


# --------------------------------------------------------------------------
# the form
# --------------------------------------------------------------------------

def test_the_form_has_the_preset_controls_inside_advanced_options():
    page = dashboard.PAGE
    advanced = page.split('<details class="adv">', 1)[1]
    for control in ("preset_select", "preset_name", "preset_save", "preset_update",
                    "preset_rename", "preset_delete", "preset_default"):
        assert f'id="{control}"' in advanced, control
    # near the top of the options, above the scan-depth group
    assert advanced.index('id="preset_select"') < advanced.index('id="maxpages"')


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node to parse it")
def test_the_pages_script_parses(tmp_path):
    """The form is one string in a Python module, so a JavaScript escape that
    Python ate first is a broken page nobody notices until it is served."""
    script = dashboard.PAGE.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    path = tmp_path / "page.js"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run(["node", "--check", str(path)], check=False,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_the_existing_controls_are_untouched():
    page = dashboard.PAGE
    for field in ("url", "deep", "samples", "maxpages", "concurrency", "throttling",
                  "device_profile", "user_agent", "auth_user", "cookies"):
        assert f'id="{field}"' in page, field
