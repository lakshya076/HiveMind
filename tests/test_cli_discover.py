import os
import stat
import sys

import httpx
import pytest
from click.testing import CliRunner

from hivemind.cli import cli
from hivemind.config.cluster import ClusterConfig, WorkerNode, get_default_cluster_path
from hivemind.discovery.scanner import DiscoveredDevice
from hivemind.discovery.tailscale import TailscaleScanner

MASTER = DiscoveredDevice(ip="100.77.177.65", hostname="Artemis", ssh_open=False, is_local_host=True)
ARI = DiscoveredDevice(ip="100.101.58.76", hostname="ari", ssh_open=False, os_type="linux")
STRANGER = DiscoveredDevice(ip="100.99.0.9", hostname="phone", ssh_open=False, os_type="android")

WORKER_ROOT = {"name": "HiveMind Worker Daemon", "version": "0.1.0"}
WORKER_HEALTH = {"os": "linux", "arch": "x86_64", "cpu_cores": 4, "ram_gb": 7.6, "engines": ["aider"], "git_ready": True}


@pytest.fixture
def peers(monkeypatch):
    devices = [MASTER, ARI, STRANGER]
    monkeypatch.setattr(TailscaleScanner, "discover_peers", classmethod(lambda cls, probe_ssh=False: list(devices)))
    monkeypatch.setattr(TailscaleScanner, "is_available", classmethod(lambda cls: True))
    return devices


def only_ari_is_worker(request):
    if request.url.host != ARI.ip:
        raise httpx.ConnectError("refused", request=request)
    if request.url.path == "/":
        return httpx.Response(200, json=WORKER_ROOT)
    if request.url.path == "/health":
        return httpx.Response(200, json=WORKER_HEALTH)
    return httpx.Response(404)


def keyed_hosts(fake_http):
    return {r.url.host for r in fake_http.requests if "authorization" in r.headers}


def test_all_with_no_detected_workers_sends_key_to_nobody(fleet_key, fake_http, peers):
    fake_http.handler = lambda request: httpx.Response(404)

    # Plenty of Enter presses, so old code would have gone on to probe every device with the key.
    result = CliRunner().invoke(cli, ["discover", "--tailscale"], input="\n" * 20)

    assert "No HiveMind workers were detected" in result.output
    assert keyed_hosts(fake_http) == set()
    assert not get_default_cluster_path().exists()


def test_all_selects_only_detected_workers(fleet_key, fake_http, peers):
    fake_http.handler = only_ari_is_worker

    # selection, then per-device prompts: id, port, workspace, engine, capabilities
    result = CliRunner().invoke(cli, ["discover", "--tailscale"], input="\n" * 6)

    assert result.exit_code == 0, result.output
    assert keyed_hosts(fake_http) == {ARI.ip}
    saved = ClusterConfig.load()
    assert [w.id for w in saved.workers] == ["ari"]
    assert saved.workers[0].agent_engine == "aider"


def test_unreadable_cluster_file_is_never_overwritten(fleet_key, fake_http, peers):
    path = get_default_cluster_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"workers": [{"id": "old", "host": "bad host"}]}', encoding="utf-8")
    before = path.read_bytes()

    result = CliRunner().invoke(cli, ["discover", "--tailscale"], input="\n" * 6)

    assert result.exit_code == 1
    assert "Could not read" in result.output
    assert path.read_bytes() == before
    assert fake_http.requests == []


def test_discover_keeps_existing_workers(fleet_key, fake_http, peers):
    ClusterConfig(
        repo_url="git@github.com:me/repo.git",
        workers=[WorkerNode(id="hermes", host="100.115.119.6")],
    ).save()
    fake_http.handler = only_ari_is_worker

    result = CliRunner().invoke(cli, ["discover", "--tailscale"], input="\n" * 6)

    assert result.exit_code == 0, result.output
    saved = ClusterConfig.load()
    assert [w.id for w in saved.workers] == ["hermes", "ari"]
    assert saved.repo_url == "git@github.com:me/repo.git"


def test_keygen_points_workers_at_hidden_prompt(home):
    result = CliRunner().invoke(cli, ["keygen"])

    assert result.exit_code == 0
    key = (home / ".hivemind" / "fleet.key").read_text(encoding="utf-8")
    assert key.startswith("hive-key-") and len(key) == len("hive-key-") + 40
    assert "hivemind-worker key set" in result.output
    assert "--key" not in result.output


def test_keygen_keeps_existing_key_without_force(fleet_key, home):
    result = CliRunner().invoke(cli, ["keygen"])
    assert (home / ".hivemind" / "fleet.key").read_text(encoding="utf-8") == fleet_key
    assert "Existing Fleet API Key" in result.output

    CliRunner().invoke(cli, ["keygen", "--force"])
    assert (home / ".hivemind" / "fleet.key").read_text(encoding="utf-8") != fleet_key


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_keygen_writes_owner_only_key(home):
    key_file = home / ".hivemind" / "fleet.key"
    key_file.parent.mkdir()
    key_file.write_text("old", encoding="utf-8")
    os.chmod(key_file, 0o644)

    CliRunner().invoke(cli, ["keygen", "--force"])

    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
