import ipaddress

import httpx
import pytest

from hivemind.discovery.prober import SystemProber
from hivemind.discovery.scanner import NetworkScanner, parse_arp_output

WINDOWS_ARP = """
Interface: 192.168.1.10 --- 0x5
  Internet Address      Physical Address      Type
  192.168.1.1           a0-b1-c2-d3-e4-f5     dynamic
  192.168.1.20          00-11-22-33-44-55     dynamic
  192.168.1.255         ff-ff-ff-ff-ff-ff     static
  224.0.0.22            01-00-5e-00-00-16     static
  255.255.255.255       ff-ff-ff-ff-ff-ff     static
"""

LINUX_ARP = """
? (192.168.1.30) at 66:77:88:99:aa:bb [ether] on eth0
? (192.168.1.31) at <incomplete> on eth0
? (169.254.1.5) at 66:77:88:99:aa:cc [ether] on eth0
"""

MACOS_ARP = """
? (192.168.1.40) at 0:11:22:33:44:5 on en0 ifscope [ethernet]
? (192.168.1.0) at 0:11:22:33:44:6 on en0 ifscope [ethernet]
"""


def test_parse_arp_output_windows():
    assert parse_arp_output(WINDOWS_ARP) == {
        "192.168.1.1": "a0:b1:c2:d3:e4:f5",
        "192.168.1.20": "00:11:22:33:44:55",
    }


def test_parse_arp_output_linux_skips_incomplete_and_link_local():
    assert parse_arp_output(LINUX_ARP) == {"192.168.1.30": "66:77:88:99:aa:bb"}


def test_parse_arp_output_macos_skips_network_address():
    network = ipaddress.IPv4Network("192.168.1.0/24")
    assert parse_arp_output(MACOS_ARP, network=network) == {"192.168.1.40": "0:11:22:33:44:5"}


def test_candidate_hosts_small_network_lists_every_host():
    hosts = NetworkScanner._candidate_hosts(ipaddress.IPv4Network("192.168.1.0/24"), {})
    assert len(hosts) == 254
    assert hosts[0] == "192.168.1.1" and hosts[-1] == "192.168.1.254"


def test_candidate_hosts_caps_large_network_to_local_slash24_plus_arp():
    arp = {"10.0.200.7": "aa", "10.0.5.9": "bb", "172.16.0.1": "cc"}
    hosts = NetworkScanner._candidate_hosts(
        ipaddress.IPv4Network("10.0.0.0/16"), arp, interface_ip="10.0.5.20"
    )
    assert len(hosts) == 255  # 254 hosts in 10.0.5.0/24 + one ARP entry outside it
    assert "10.0.200.7" in hosts
    assert "172.16.0.1" not in hosts


def worker_root(request):
    return httpx.Response(200, json={"name": "HiveMind Worker Daemon", "version": "0.1.0"})


def test_probe_presence_never_sends_fleet_key(fleet_key, fake_http):
    fake_http.handler = worker_root
    info, err = SystemProber.probe_presence("10.0.0.2")
    assert err is None and info["version"] == "0.1.0"
    (req,) = fake_http.requests
    assert req.url.path == "/"
    assert "authorization" not in req.headers


@pytest.mark.parametrize(
    "response, message",
    [
        (httpx.Response(403), "allowed_networks"),
        (httpx.Response(200, json={"name": "Some Other API"}), "Not a HiveMind worker"),
        (httpx.Response(200, text="<html>router</html>"), "non-JSON"),
        (httpx.Response(500), "HTTP 500"),
    ],
)
def test_probe_presence_rejects_non_workers(fake_http, response, message):
    fake_http.handler = lambda request: response
    info, err = SystemProber.probe_presence("10.0.0.2")
    assert info is None and message in err


def test_probe_daemon_sends_key_and_builds_tags(fleet_key, fake_http):
    fake_http.handler = lambda request: httpx.Response(200, json={
        "os": "linux", "arch": "x86_64", "cpu_cores": 32, "ram_gb": 64.0,
        "engines": ["aider"], "git_ready": True,
    })
    data, tags, err = SystemProber.probe_daemon("10.0.0.2")
    assert err is None and data["os"] == "linux"
    assert fake_http.requests[0].headers["authorization"] == f"Bearer {fleet_key}"
    assert {"os-linux", "high-cpu", "engine:aider", "git", "64.0gb-ram"} <= set(tags)


def test_probe_daemon_reports_wrong_key(fleet_key, fake_http):
    fake_http.handler = lambda request: httpx.Response(401)
    data, tags, err = SystemProber.probe_daemon("10.0.0.2")
    assert data is None and tags == [] and "Auth Failed" in err
