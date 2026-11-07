import json

import httpx
import pytest
from click.testing import CliRunner

from hivemind import cli as cli_mod
from hivemind.cli import cli
from hivemind.config.cluster import ClusterConfig, WorkerNode
from hivemind.runner.state import RunState, WorkerRunState

RUN_ID = "20261107-120000-abcd1234"
ARI = WorkerNode(id="ari", host="10.0.0.2")


@pytest.fixture
def runner():
    return CliRunner()


def slot(strategy, n, status="running", **fields):
    return WorkerRunState(
        worker_id="ari", host=ARI.host, port=ARI.port, agent_engine="aider", strategy=strategy,
        branch=f"exp/{RUN_ID}/ari-{strategy}", remote_run_dir=f"{RUN_ID}-{n}", status=status, **fields,
    )


def save_run(*slots, run_id=RUN_ID, prompt="fix the bug", created_at=None):
    run = RunState(run_id=run_id, prompt=prompt, repo_url="r", workers=list(slots))
    if created_at:
        run.created_at = created_at
    run.save()
    return run


def statuses(mapping):
    """Answer /status/<remote id> with the given status and record /cancel calls."""
    def handler(request):
        remote_id = request.url.path.rsplit("/", 1)[-1]
        if request.method == "DELETE":
            mapping[remote_id] = "cancelled"
            return httpx.Response(200, json={"run_id": remote_id, "cancelled": True, "message": "ok"})
        return httpx.Response(200, json={"status": mapping[remote_id], "log_tail": f"tail of {remote_id}"})
    return handler


# ---------------------------------------------------------------- cancel


def test_cancel_stops_only_unfinished_strategies(runner, fleet_key, fake_http):
    save_run(slot("simd", 1), slot("actor", 2, status="succeeded"))
    state = {f"{RUN_ID}-1": "running", f"{RUN_ID}-2": "succeeded"}
    fake_http.handler = statuses(state)

    result = runner.invoke(cli, ["cancel"])

    assert result.exit_code == 0, result.output
    deletes = [r for r in fake_http.requests if r.method == "DELETE"]
    assert [r.url.path for r in deletes] == [f"/cancel/{RUN_ID}-1"]
    assert deletes[0].headers["authorization"] == f"Bearer {fleet_key}"
    assert [w.status for w in RunState.load(RUN_ID).workers] == ["cancelled", "succeeded"]


def test_cancel_one_strategy_by_label(runner, fleet_key, fake_http):
    save_run(slot("simd", 1), slot("actor", 2))
    fake_http.handler = statuses({f"{RUN_ID}-1": "running", f"{RUN_ID}-2": "running"})

    result = runner.invoke(cli, ["cancel", "--worker", "ari:actor"])

    assert result.exit_code == 0, result.output
    assert [r.url.path for r in fake_http.requests if r.method == "DELETE"] == [f"/cancel/{RUN_ID}-2"]


def test_cancel_reports_failure_with_exit_1(runner, fleet_key, fake_http):
    save_run(slot("simd", 1))

    def handler(request):
        if request.method == "DELETE":
            return httpx.Response(404)
        return httpx.Response(200, json={"status": "running"})

    fake_http.handler = handler
    result = runner.invoke(cli, ["cancel"])
    assert result.exit_code == 1
    assert "not running on the worker" in result.output


def test_cancel_with_nothing_running(runner, fleet_key, fake_http):
    save_run(slot("simd", 1, status="succeeded"))
    fake_http.handler = statuses({f"{RUN_ID}-1": "succeeded"})
    result = runner.invoke(cli, ["cancel"])
    assert result.exit_code == 0
    assert "Nothing to cancel" in result.output
    assert not [r for r in fake_http.requests if r.method == "DELETE"]


# ---------------------------------------------------------------- status --watch


def test_status_watch_polls_until_finished(runner, fleet_key, fake_http, monkeypatch):
    save_run(slot("simd", 1))
    answers = iter(["starting", "running", "succeeded"])
    fake_http.handler = lambda request: httpx.Response(200, json={"status": next(answers)})
    sleeps = []
    monkeypatch.setattr(cli_mod.time, "sleep", sleeps.append)

    result = runner.invoke(cli, ["status", "--watch", "--interval", "2"])

    assert result.exit_code == 0, result.output
    assert sleeps == [2.0, 2.0]
    assert "All workers finished" in result.output


def test_status_without_watch_polls_once(runner, fleet_key, fake_http, monkeypatch):
    save_run(slot("simd", 1))
    fake_http.handler = lambda request: httpx.Response(200, json={"status": "running"})
    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: pytest.fail("should not sleep"))
    assert runner.invoke(cli, ["status"]).exit_code == 0
    assert len(fake_http.requests) == 1


# ---------------------------------------------------------------- runs


def test_runs_lists_newest_first_without_contacting_workers(runner, fake_http):
    save_run(slot("simd", 1, status="succeeded"), run_id="20261101-000000-aaaaaaaa", prompt="old task",
             created_at="2026-11-01T00:00:00Z")
    save_run(slot("simd", 1, status="failed"), slot("actor", 2, status="succeeded"),
             run_id="20261105-000000-bbbbbbbb", prompt="new [bold]task[/bold]", created_at="2026-11-05T00:00:00Z")

    result = runner.invoke(cli, ["runs"])

    assert result.exit_code == 0, result.output
    assert result.output.index("20261105-000000-bbbbbbbb") < result.output.index("20261101-000000-aaaaaaaa")
    assert "new [bold]task[/bold]" in result.output
    assert fake_http.requests == []


def test_runs_skips_unreadable_files(runner, home):
    save_run(slot("simd", 1))
    (home / ".hivemind" / "runs" / "broken.json").write_text("{nope", encoding="utf-8")
    result = runner.invoke(cli, ["runs"])
    assert result.exit_code == 0 and RUN_ID in result.output


def test_runs_when_empty(runner):
    result = runner.invoke(cli, ["runs"])
    assert result.exit_code == 0 and "No runs yet" in result.output


# ---------------------------------------------------------------- logs


def test_logs_prints_worker_output_literally(runner, fleet_key, fake_http):
    save_run(slot("simd", 1), slot("actor", 2))
    fake_http.handler = lambda request: httpx.Response(
        200, json={"status": "failed", "error": "Test suite failed", "log_tail": "FAILED [red]test_x[/red]"}
    )

    result = runner.invoke(cli, ["logs", "ari:simd"])

    assert result.exit_code == 0, result.output
    assert "FAILED [red]test_x[/red]" in result.output
    assert "Test suite failed" in result.output
    assert "actor" not in result.output.split("simd", 1)[1]


# ---------------------------------------------------------------- dispatch --engine, merge flags, check versions


def test_dispatch_engine_overrides_cluster(runner, fleet_key, fake_http):
    ClusterConfig(repo_url="r", workers=[WorkerNode(id="ari", host="10.0.0.2", agent_engine="aider")]).save()
    fake_http.handler = lambda request: httpx.Response(200, json={"status": "starting"})

    result = runner.invoke(cli, ["dispatch", "fix it", "--engine", "claude-code"])

    assert result.exit_code == 0, result.output
    assert json.loads(fake_http.requests[0].content)["agent_engine"] == "claude-code"
    assert RunState.load_latest().workers[0].agent_engine == "claude-code"
    assert ClusterConfig.load().workers[0].agent_engine == "aider"  # cluster file unchanged


def test_dispatch_engine_rejects_unknown(runner, fleet_key, fake_http):
    result = runner.invoke(cli, ["dispatch", "fix it", "--engine", "custom"])
    assert result.exit_code == 2
    assert fake_http.requests == []


def test_merge_non_interactive(runner, fleet_key, fake_http, origin_repo, tmp_path, git, monkeypatch):
    origin, local = origin_repo
    branch = f"exp/{RUN_ID}/ari-simd"
    worker = tmp_path / "w"
    git(tmp_path, "clone", str(origin), str(worker))
    git(worker, "checkout", "-b", branch)
    (worker / "f.py").write_text("x = 1\n", encoding="utf-8")
    git(worker, "add", "-A")
    git(worker, "commit", "-m", "hive: simd")
    git(worker, "push", "origin", branch)
    run = save_run(slot("simd", 1, status="succeeded", pushed=True))
    run.repo_url = str(origin)
    run.save()
    fake_http.handler = lambda request: httpx.Response(200, json={"status": "succeeded", "branch": branch, "pushed": True})
    monkeypatch.chdir(local)

    result = runner.invoke(cli, ["merge", "--worker", "ari", "--yes"], input="")

    assert result.exit_code == 0, result.output
    assert (local / "f.py").exists()


def test_check_warns_on_mixed_worker_versions(runner, fleet_key, fake_http):
    ClusterConfig(workers=[WorkerNode(id="ari", host="10.0.0.2"), WorkerNode(id="hermes", host="10.0.0.3")]).save()
    versions = {"10.0.0.2": "0.1.0", "10.0.0.3": "0.2.0"}
    fake_http.handler = lambda request: httpx.Response(
        200, json={"status": "ready", "engines": ["claude-code"], "version": versions[request.url.host]}
    )

    result = runner.invoke(cli, ["check"])

    assert result.exit_code == 0, result.output
    assert "different hivemind-worker versions" in result.output
    assert "0.1.0: ari" in result.output and "0.2.0: hermes" in result.output
