"""Failures must exit non-zero so scripts (and a future MCP server) can detect them."""
import httpx
import pytest
from click.testing import CliRunner

from hivemind.cli import cli
from hivemind.config.cluster import ClusterConfig, WorkerNode, get_default_cluster_path
from hivemind.discovery.scanner import DiscoveredDevice
from hivemind.discovery.tailscale import TailscaleScanner
from hivemind.runner.state import RunState


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cluster():
    config = ClusterConfig(repo_url="git@github.com:me/repo.git", workers=[WorkerNode(id="ari", host="10.0.0.2")])
    config.save()
    return config


def test_check_exits_1_when_a_worker_is_offline(runner, fleet_key, fake_http, cluster):
    def offline(request):
        raise httpx.ConnectError("refused", request=request)

    fake_http.handler = offline
    result = runner.invoke(cli, ["check"])
    assert result.exit_code == 1
    assert "OFFLINE" in result.output


def test_dispatch_exits_1_when_a_worker_rejects_but_saves_run(runner, fleet_key, fake_http, cluster):
    fake_http.handler = lambda request: httpx.Response(401)
    result = runner.invoke(cli, ["dispatch", "fix it"])
    assert result.exit_code == 1
    assert "1 of 1 strategies could not be dispatched" in result.output
    assert RunState.load_latest().workers[0].status == "failed"


@pytest.mark.parametrize("args", [["list"], ["check"], ["dispatch", "x"]])
def test_missing_cluster_exits_1(runner, fleet_key, fake_http, args):
    result = runner.invoke(cli, args)
    assert result.exit_code == 1
    assert "not found" in result.output


@pytest.mark.parametrize("content", ["{not json", '{"workers": [{"id": "a", "host": "a b"}]}'])
@pytest.mark.parametrize("args", [["list"], ["check"], ["status"]])
def test_unreadable_cluster_gives_error_not_traceback(runner, fleet_key, fake_http, content, args):
    path = get_default_cluster_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    RunState(run_id="20261107-120000-abcd1234", prompt="p", repo_url="r").save()

    result = runner.invoke(cli, args)

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Could not read cluster file" in result.output


@pytest.mark.parametrize("args", [["status"], ["merge"], ["status", "--run", "../evil"]])
def test_missing_or_invalid_run_exits_1(runner, fleet_key, fake_http, args):
    result = runner.invoke(cli, args)
    assert result.exit_code == 1


def test_discover_never_assigns_one_id_to_two_devices(runner, fleet_key, fake_http, monkeypatch):
    first = DiscoveredDevice(ip="100.64.0.1", hostname="box", daemon_open=False)
    second = DiscoveredDevice(ip="100.64.0.2", hostname="box", daemon_open=False)
    monkeypatch.setattr(TailscaleScanner, "discover_peers", classmethod(lambda cls: [first, second]))
    monkeypatch.setattr(TailscaleScanner, "backend_state", classmethod(lambda cls: "Running"))
    fake_http.handler = lambda request: httpx.Response(
        200, json={"name": "HiveMind Worker Daemon", "version": "0.1.0"} if request.url.path == "/" else {}
    )

    # device 1 takes the default "box"; on device 2 the user types "box" again, is re-prompted, takes the default
    answers = ["all", "", "", "", "", "box", "", "", "", ""]
    result = runner.invoke(cli, ["discover", "--tailscale"], input="\n".join(answers) + "\n")

    assert result.exit_code == 0, result.output
    assert "already used" in result.output
    assert [w.id for w in ClusterConfig.load().workers] == ["box", "box-2"]
