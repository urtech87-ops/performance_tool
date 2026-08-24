"""The DNS override, pointed somewhere it must not go.

Mapping a hostname to an address is how a staging server is measured before
its DNS is switched. Pointed at an address that only means something on the
scanning host - its own loopback, the private network it sits on, the cloud
metadata service at 169.254.169.254 that hands out credentials to anything
that asks - it is instead a way to use the scanner as a proxy into a network
the requester cannot otherwise reach.

So the address is classified (`scanconfig.is_private_target`, a pure function
that knows only about ranges) and the deployment decides what to do about it
(`guardrails`, which knows about settings). What is pinned here is that the
classification covers every spelling of "inside", that a refusal is a refusal
rather than a silent downgrade to the public site, and that the one escape
hatch is the only thing that opens it.
"""

import pytest

import dashboard
import guardrails
import scanconfig
from config import settings

# Every range that must not be reachable through an override, and a plausible
# address in it. The metadata endpoint is called out by name because it is the
# one that turns an SSRF into stolen cloud credentials.
BLOCKED = [
    ("0.0.0.0", "this network / localhost"),
    ("10.0.0.9", "RFC1918 10/8"),
    ("100.64.0.1", "carrier-grade NAT 100.64/10"),
    ("127.0.0.1", "loopback 127/8"),
    ("169.254.169.254", "cloud metadata, inside link-local 169.254/16"),
    ("169.254.1.1", "link-local 169.254/16"),
    ("172.16.0.1", "RFC1918 172.16/12, low end"),
    ("172.31.255.254", "RFC1918 172.16/12, high end"),
    ("192.0.0.1", "IETF protocol assignments 192.0.0/24"),
    ("192.168.1.1", "RFC1918 192.168/16"),
    ("198.18.0.1", "benchmarking 198.18/15"),
    ("224.0.0.1", "multicast"),
    ("255.255.255.255", "broadcast, inside 240/4"),
    ("::", "IPv6 unspecified"),
    ("::1", "IPv6 loopback"),
    ("fc00::1", "IPv6 unique local fc00::/7"),
    ("fd12:3456::1", "IPv6 unique local, the half people actually use"),
    ("fe80::1", "IPv6 link-local"),
    ("ff02::1", "IPv6 multicast"),
    ("::ffff:10.0.0.9", "10/8 wearing an IPv4-mapped IPv6 address"),
    ("::ffff:169.254.169.254", "the metadata IP, mapped"),
    ("2002:0a00:0009::", "10.0.0.9 wearing a 6to4 address"),
]

# Addresses that are somewhere on the public internet, including the
# documentation ranges the README's own examples use - refusing those would
# only make the examples a lie.
ALLOWED = ["8.8.8.8", "1.1.1.1", "203.0.113.10", "192.0.2.5", "198.51.100.7",
           "9.255.255.255", "11.0.0.1", "172.32.0.1", "2606:4700:4700::1111"]


@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


def request_with(dns_ip, **extra):
    return dict({"url": "https://x.test", "dns_host": "staging.x.test",
                 "dns_ip": dns_ip}, **extra)


# --------------------------------------------------------------------------
# the classification itself
# --------------------------------------------------------------------------

@pytest.mark.parametrize("address,why", BLOCKED, ids=[a for a, _ in BLOCKED])
def test_an_address_inside_the_scanning_host_is_recognised(address, why):
    assert scanconfig.is_private_target(address) is True, why


@pytest.mark.parametrize("address", ALLOWED)
def test_a_public_address_is_not(address):
    assert scanconfig.is_private_target(address) is False


def test_text_that_is_not_an_address_is_not_a_private_target():
    """`clean_ip` has already dropped it - it is not a target at all."""
    for junk in ("", None, "not-an-address", "10.0.0", "localhost", "999.1.1.1"):
        assert scanconfig.is_private_target(junk) is False


def test_a_config_carrying_one_says_so():
    assert scanconfig.ScanConfig(dns_ip="10.0.0.9").targets_private_network
    assert not scanconfig.ScanConfig(dns_ip="203.0.113.9").targets_private_network
    assert not scanconfig.ScanConfig().targets_private_network


# --------------------------------------------------------------------------
# what the request layer does about it
# --------------------------------------------------------------------------

@pytest.mark.parametrize("address,why", BLOCKED, ids=[a for a, _ in BLOCKED])
def test_the_request_layer_refuses_every_blocked_range(address, why):
    params, error, _ = guardrails.sanitize_params(request_with(address))
    assert params is None, why
    # The message quotes the address in its canonical form - the same one the
    # scan would have used - so the person can see what was actually read.
    assert scanconfig.clean_ip(address) in error
    assert "ALLOW_PRIVATE_DNS_TARGETS" in error


@pytest.mark.parametrize("address", ALLOWED)
def test_the_request_layer_accepts_a_public_address(address):
    params, error, _ = guardrails.sanitize_params(request_with(address))
    assert error is None
    assert scanconfig.ScanConfig.from_dict(params["scan_config"]).dns_ip == address


def test_it_is_refused_rather_than_quietly_dropped():
    """The difference matters: a scan that silently measured the public site
    instead of the staging box is a wrong answer delivered as a right one."""
    params, error, _ = guardrails.sanitize_params(request_with("10.0.0.9"))
    assert params is None and error


def test_an_ip_with_no_host_is_checked_too():
    """`for_url` fills the hostname in later, so the address is all there is
    to judge at this point - and it is judged."""
    params, error, _ = guardrails.sanitize_params(
        {"url": "https://x.test", "dns_ip": "127.0.0.1"})
    assert params is None and "127.0.0.1" in error


def test_the_escape_hatch_opens_it(monkeypatch):
    """A deployment that really does scan its own network can say so."""
    monkeypatch.setattr(settings, "ALLOW_PRIVATE_DNS_TARGETS", True)
    params, error, _ = guardrails.sanitize_params(request_with("10.0.0.9"))
    assert error is None
    assert scanconfig.ScanConfig.from_dict(params["scan_config"]).dns_ip == "10.0.0.9"


def test_the_hatch_is_off_by_default():
    assert settings.ALLOW_PRIVATE_DNS_TARGETS is False


def test_a_deployment_that_offers_no_override_at_all_drops_it_silently(monkeypatch):
    """Nothing to refuse: the feature is not on offer, so the fields go and
    the rest of the scan runs. That is the behaviour it has always had."""
    monkeypatch.setattr(settings, "ALLOW_DNS_OVERRIDE", False)
    params, error, _ = guardrails.sanitize_params(
        request_with("10.0.0.9", block_patterns="*ads*"))
    assert error is None
    cfg = scanconfig.ScanConfig.from_dict(params["scan_config"])
    assert cfg.dns_ip == "" and cfg.dns_host == ""
    assert cfg.block_patterns == ("*ads*",)


def test_blocking_and_the_rest_of_the_scan_are_untouched_by_the_refusal():
    """The refusal is about the one field. A request without it is unaffected."""
    params, error, _ = guardrails.sanitize_params(
        {"url": "https://x.test", "block_patterns": "*ads*", "max_pages": 3})
    assert error is None and params["max_pages"] == 3


# --------------------------------------------------------------------------
# through the routes a client actually reaches
# --------------------------------------------------------------------------

def test_the_post_route_refuses_it(client):
    resp = client.post("/scan", json=request_with("169.254.169.254"))
    assert resp.status_code == 400
    assert "169.254.169.254" in resp.get_json()["message"]


def test_the_get_route_refuses_it(client):
    resp = client.get("/scan?url=https://x.test&dns_ip=169.254.169.254")
    assert resp.headers["X-Scan-Rejected"] == "invalid"
    assert "169.254.169.254" in resp.get_data(as_text=True)


def test_a_saved_preset_carrying_one_is_refused_when_it_is_used():
    """Storing it is allowed - a deployment's policy can change, and a preset
    is just remembered text. Acting on it is where the answer is no."""
    import scanpresets

    preset = scanpresets.Preset(name="LAN box",
                                settings={"dns_host": "staging.x.test",
                                          "dns_ip": "192.168.1.10"})
    params, error, _ = guardrails.sanitize_params(
        preset.to_request() | {"url": "https://x.test"})
    assert params is None and "192.168.1.10" in error
