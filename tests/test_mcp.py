"""The hive MCP server, driven in-process through the MCP client."""
import json

import anyio
import httpx
import pytest
from mcp import Client
from mcp.types import ElicitResult

from hivemind.config.cluster import ClusterConfig, WorkerNode
from hivemind.mcp.server import app
from hivemind.runner.state import RunState, WorkerRunState

RUN_ID = "20261107-120000-abcd1234"
ARI = WorkerNode(id="ari", host="10.0.0.2", agent_engine="aider")


def call(tool, args=None, elicit=None):
    """Call one tool; return (is_error, parsed JSON or error text)."""
    async def go():
        async with Client(app, elicitation_callback=elicit) as client:
            return await client.call_tool(tool, args or {})

    result = anyio.run(go)
    text = result.content[0].text if result.content else ""
    return result.is_error, (text if result.is_error else json.loads(text))


def tools():
    async def go():
        async with Client(app) as client:
            return (await client.list_tools()).tools

    return {t.name: t for t in anyio.run(go)}


def answer(content=None, action="accept"):
    async def callback(context, params):
        callback.asked = params
        return ElicitResult(action=action, content=content)

    callback.asked = None
    return callback


def test_tools_exposed_without_keygen():
    found = tools()
    assert set(found) == {
        "hive_list", "hive_check", "hive_runs", "hive_discover", "hive_add_workers",
        "hive_dispatch", "hive_status", "hive_logs", "hive_cancel", "hive_merge",
    }
    # The approval comes from the human through elicitation; the agent cannot pass it.
    assert set(found["hive_add_workers"].input_schema["properties"]) == {"ips", "port"}


def test_tools_need_a_fleet_key():
    ClusterConfig(workers=[ARI]).save()
    is_error, text = call("hive_check")
    assert is_error and "hive keygen" in text


def test_dispatch_then_status(fleet_key, fake_http):
    ClusterConfig(repo_url="https://x/repo.git", workers=[ARI]).save()
    fake_http.handler = lambda request: httpx.Response(200, json={"status": "starting"})

    is_error, run = call("hive_dispatch", {"prompt": "fix it", "strategies": ["a", "b"], "test": "pytest -q"})
    assert not is_error, run
    assert [w["label"] for w in run["workers"]] == ["ari:a", "ari:b"]
    assert json.loads(fake_http.requests[0].content)["test_command"] == "pytest -q"

    fake_http.handler = lambda request: httpx.Response(200, json={"status": "succeeded", "log_tail": "x" * 50})
    is_error, status = call("hive_status", {"run_id": run["run_id"]})
    assert not is_error and status["finished"] is True
    assert "log_tail" not in status["workers"][0]  # logs come from hive_logs


def test_merge_previews_then_merges_only_with_confirm(fleet_key, fake_http, origin_repo, tmp_path, git):
    origin, local = origin_repo
    branch = f"exp/{RUN_ID}/ari-simd"
    worker = tmp_path / "w"
    git(tmp_path, "clone", str(origin), str(worker))
    git(worker, "checkout", "-b", branch)
    (worker / "f.py").write_text("x = 1\n", encoding="utf-8")
    git(worker, "add", "-A")
    git(worker, "commit", "-m", "hive: simd")
    git(worker, "push", "origin", branch)
    RunState(run_id=RUN_ID, prompt="p", repo_url=str(origin), workers=[WorkerRunState(
        worker_id="ari", host=ARI.host, agent_engine="aider", strategy="simd", branch=branch,
        remote_run_dir=f"{RUN_ID}-1", status="succeeded", pushed=True,
    )]).save()
    fake_http.handler = lambda request: httpx.Response(200, json={"status": "succeeded", "branch": branch, "pushed": True})

    is_error, preview = call("hive_merge", {"repo_path": str(local)})
    assert not is_error, preview
    assert preview["merged"] is False and "hive: simd" in preview["commits"]
    assert not (local / "f.py").exists()

    is_error, merged = call("hive_merge", {"repo_path": str(local), "confirm": True})
    assert not is_error, merged
    assert merged["merged"] is True and (local / "f.py").exists()


def test_merge_never_picks_a_failed_strategy_by_default(fleet_key, fake_http, tmp_path):
    RunState(run_id=RUN_ID, prompt="p", repo_url="r", workers=[WorkerRunState(
        worker_id="ari", host=ARI.host, agent_engine="aider", strategy="simd",
        branch=f"exp/{RUN_ID}/ari-simd", status="failed", pushed=True,
    )]).save()
    fake_http.handler = lambda request: httpx.Response(200, json={"status": "failed", "pushed": True})
    is_error, text = call("hive_merge", {"repo_path": str(tmp_path), "confirm": True})
    assert is_error and "No strategy succeeded" in text


HEALTH = {"hostname": "Hermes.local", "status": "ready", "engines": ["aider"], "git_ready": True, "version": "0.3.0"}


def test_add_workers_saves_only_what_the_human_ticked(fleet_key, fake_http):
    ClusterConfig(workers=[ARI]).save()
    fake_http.handler = lambda request: httpx.Response(200, json=HEALTH)
    human = answer({"add_0": False, "add_1": True})

    is_error, result = call("hive_add_workers", {"ips": ["10.0.0.7", "10.0.0.8"]}, elicit=human)

    assert not is_error, result
    assert "fleet key" in human.asked.message
    assert [w["host"] for w in result["added"]] == ["10.0.0.8"]
    # Only the ticked machine was sent the key; the other was never contacted.
    assert {r.url.host for r in fake_http.requests} == {"10.0.0.8"}
    saved = ClusterConfig.load().workers
    assert [(w.id, w.host, w.agent_engine) for w in saved] == [("ari", "10.0.0.2", "aider"), ("hermes", "10.0.0.8", "aider")]


def test_add_workers_declined_adds_nothing(fleet_key, fake_http):
    ClusterConfig(workers=[ARI]).save()
    is_error, _ = call("hive_add_workers", {"ips": ["10.0.0.8"]}, elicit=answer(action="decline"))
    assert is_error
    assert fake_http.requests == []
    assert [w.id for w in ClusterConfig.load().workers] == ["ari"]


def test_add_workers_without_elicitation_falls_back_to_the_cli(fleet_key, fake_http):
    is_error, result = call("hive_add_workers", {"ips": ["10.0.0.8"]})
    assert not is_error
    assert result["needs_human"] is True and "hive discover" in result["message"]
    assert fake_http.requests == []


@pytest.mark.parametrize("ip", ["10.0.0.8/evil", "http://x"])
def test_add_workers_rejects_bad_hosts(fleet_key, ip):
    human = answer({"add_0": True})
    is_error, _ = call("hive_add_workers", {"ips": [ip]}, elicit=human)
    assert is_error
    assert human.asked is None  # rejected before the human is asked


def test_discover_is_read_only(fleet_key, fake_http, monkeypatch):
    from hivemind.discovery.scanner import DiscoveredDevice
    from hivemind.discovery.tailscale import TailscaleScanner

    monkeypatch.setattr(TailscaleScanner, "backend_state", classmethod(lambda cls: "Running"))
    monkeypatch.setattr(TailscaleScanner, "discover_peers", classmethod(lambda cls: [
        DiscoveredDevice(ip="100.0.0.5", hostname="hermes", daemon_open=True, os_type="windows"),
        DiscoveredDevice(ip="100.0.0.1", hostname="me", daemon_open=False, is_local_host=True),
    ]))
    fake_http.handler = lambda request: httpx.Response(200, json={"name": "HiveMind Worker Daemon", "version": "0.3.0"})

    is_error, result = call("hive_discover")

    assert not is_error, result
    assert [(d["ip"], d["worker_detected"]) for d in result["devices"]] == [("100.0.0.5", True)]
    assert all("authorization" not in r.headers for r in fake_http.requests)  # no key sent
    with pytest.raises(FileNotFoundError):
        ClusterConfig.load()  # nothing saved


def test_logs_and_runs(fleet_key, fake_http):
    RunState(run_id=RUN_ID, prompt="fix the bug", repo_url="r", workers=[WorkerRunState(
        worker_id="ari", host=ARI.host, agent_engine="aider", strategy="simd",
        branch=f"exp/{RUN_ID}/ari-simd", remote_run_dir=f"{RUN_ID}-1", status="running",
    )]).save()

    def handler(request):
        if request.url.path.startswith("/logs/"):
            return httpx.Response(200, json={"run_id": f"{RUN_ID}-1", "file": "test", "size": 9, "truncated": False,
                                             "content": "FAILED x\n"})
        return httpx.Response(200, json={"status": "failed", "error": "tests failed", "log_tail": "tail"})

    fake_http.handler = handler
    is_error, result = call("hive_logs", {"full": True, "file": "test"})
    assert not is_error, result
    assert result["logs"] == [{"label": "ari", "status": "failed", "error": "tests failed", "log": "FAILED x\n",
                               "truncated": False, "size": 9}]

    is_error, result = call("hive_runs")
    assert result["runs"][0]["statuses"] == {"failed": 1} and result["runs"][0]["prompt"] == "fix the bug"
