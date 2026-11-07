import json

import pytest
from pydantic import ValidationError

from hivemind.config.cluster import (
    ClusterConfig,
    WorkerNode,
    get_default_cluster_path,
    get_fleet_key,
    validate_worker_host,
)


@pytest.mark.parametrize(
    "host",
    ["100.101.58.76", "192.168.1.20", "::1", "fd7a:115c:a1e0::1", "ari", "ari.tail1234.ts.net"],
)
def test_valid_worker_hosts_are_accepted(host):
    assert validate_worker_host(host) == host


def test_bracketed_ipv6_host_is_unwrapped():
    assert validate_worker_host("[::1]") == "::1"


@pytest.mark.parametrize(
    "host",
    [
        "",
        "   ",
        "evil.com/path",
        "user@evil.com",
        "host?x=1",
        "host#frag",
        "a\\b",
        "host name",
        "a..b",
        "-host",
        "host:7422",
        "a\x00b",
    ],
)
def test_worker_hosts_with_url_injection_are_rejected(host):
    with pytest.raises(ValueError):
        validate_worker_host(host)


@pytest.mark.parametrize("port", [0, 65536])
def test_worker_port_out_of_range_is_rejected(port):
    with pytest.raises(ValidationError):
        WorkerNode(id="ari", host="10.0.0.2", port=port)


@pytest.mark.parametrize("engine, stored", [("aider", "aider"), (" Claude-Code ", "claude-code"), ("claude", "claude"), ("agy", "agy")])
def test_supported_engines_accepted(engine, stored):
    assert WorkerNode(id="ari", host="10.0.0.2", agent_engine=engine).agent_engine == stored


@pytest.mark.parametrize("engine", ["custom", "cursor", ""])
def test_unsupported_engine_rejected(engine):
    # The worker would refuse every dispatch to it, so fail at config time instead.
    with pytest.raises(ValidationError, match="Unsupported agent_engine"):
        WorkerNode(id="ari", host="10.0.0.2", agent_engine=engine)


def test_worker_id_cannot_be_empty():
    with pytest.raises(ValidationError):
        WorkerNode(id="", host="10.0.0.2")


def test_cluster_round_trips_through_default_path():
    config = ClusterConfig(
        repo_url="git@github.com:me/repo.git",
        workers=[WorkerNode(id="ari", host="100.101.58.76", agent_engine="aider")],
    )
    saved = config.save()
    assert saved == get_default_cluster_path()
    loaded = ClusterConfig.load()
    assert loaded == config


def test_cluster_load_ignores_cluster_json_in_current_directory(tmp_path, monkeypatch):
    # An untrusted repo's ./cluster.json must never be used: it could point
    # the master (and its fleet key) at an attacker's host.
    (tmp_path / "cluster.json").write_text(
        json.dumps({"workers": [{"id": "evil", "host": "203.0.113.9"}]}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        ClusterConfig.load()


def test_fleet_key_file_wins_over_env(fleet_key, monkeypatch):
    monkeypatch.setenv("HIVEMIND_FLEET_KEY", "from-env")
    assert get_fleet_key() == fleet_key


def test_fleet_key_falls_back_to_env(home, monkeypatch):
    (home / ".hivemind").mkdir()
    (home / ".hivemind" / "fleet.key").write_text("  \n", encoding="utf-8")
    monkeypatch.setenv("HIVEMIND_FLEET_KEY", "from-env")
    assert get_fleet_key() == "from-env"


def test_no_fleet_key_returns_none():
    assert get_fleet_key() is None
